"""Resumable render loop (Prompt 5): draft + final passes via the generation MCP.

Talks to the generation server's tool functions in-process (same code path the MCP exposes),
iterating the manifest in order. Key properties:

  - DRAFT pass renders every renderable shot on the cheapest model/tier into shots/.
  - Nimbo ref_images are attached on EVERY call. Continuity comes from first-frame seeding:
    shot 1 seeds from the canonical Nimbo ref; shot N seeds from the prior clip's LAST frame
    (extracted with ffmpeg). If the prior clip is missing (it failed), we fall back to the
    canonical ref and warn — the run keeps going.
  - The manifest is saved after EVERY status change, so a killed run RESUMES: drafted /
    approved / done shots are skipped; pending / failed / interrupted shots are re-rendered.
  - Failed shots retry up to 3x with exponential backoff, then are left `failed` and the run
    continues (one bad shot never aborts the pass).
  - FINAL pass (separate function) re-renders only shots marked `approved` on the locked
    final model/tier, into finals/.
  - Before any pass, the estimated cost is printed and an explicit confirm (--yes) is REQUIRED
    before a single API call is made.

CLI:
  python -m src.render --project blue-song --pass draft           # prints cost, does NOT spend
  python -m src.render --project blue-song --pass draft --yes     # spends
  python -m src.render --project blue-song --pass final --yes
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .character import load_character
from .models import Manifest, Shot, ShotStatus, load_manifest, save_manifest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_gen():
    gen_path = _PROJECT_ROOT / "mcp" / "gen_server.py"
    spec = importlib.util.spec_from_file_location("nimbo_gen_server", gen_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules.setdefault("nimbo_gen_server", mod)
    spec.loader.exec_module(mod)
    return mod


gen = _load_gen()


class RenderError(RuntimeError):
    pass


# Which statuses each pass will (re-)render. Resumability lives here.
#   DRAFT: anything not yet finished — including interrupted (drafting/rendering) and failed.
#   FINAL: only approved shots.
_DRAFT_RENDERABLE = {
    ShotStatus.pending,
    ShotStatus.failed,
    ShotStatus.redo,
    ShotStatus.drafting,
    ShotStatus.rendering,
}
_DRAFT_SKIP = {ShotStatus.drafted, ShotStatus.approved, ShotStatus.done}
_FINAL_RENDERABLE = {ShotStatus.approved}

_BACKOFF_BASE_S = 2.0  # 2s, 4s, 8s between retries


@dataclass
class PassSummary:
    pass_name: str
    model: str
    est_cost: float
    rendered: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    confirmed: bool = False

    @property
    def spent_estimate(self) -> float:
        return round(self.est_cost if self.confirmed else 0.0, 4)


# ──────────────────────────────────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────────────────────────────────


async def _sleep(seconds: float) -> None:
    """Indirection so tests can patch out the backoff wait."""
    await asyncio.sleep(seconds)


def _resolve_refs(project_dir: Path, ref_images: list[str]) -> list[str]:
    """Resolve relative ref paths against the project dir; pass through URLs/abs paths."""
    out: list[str] = []
    for r in ref_images:
        if r.startswith(("http://", "https://", "data:")) or Path(r).is_absolute():
            out.append(r)
        else:
            out.append(str((project_dir / r).resolve()))
    return out


def _snap_duration(model_id: str, duration_s: float) -> int:
    """Snap a (possibly fractional) slot length to a duration the model accepts."""
    spec = gen.MODEL_REGISTRY[model_id]
    if spec.allowed_durations:
        return min(spec.allowed_durations, key=lambda a: (abs(a - duration_s), a))
    return max(4, min(int(round(duration_s)), spec.max_seconds))


def _estimate(model_id: str, duration_s: float) -> float:
    spec = gen.MODEL_REGISTRY.get(model_id)
    if spec is None:
        return 0.0
    return round(spec.per_second_cost * _snap_duration(model_id, duration_s), 4)


def extract_last_frame(clip_path: str | Path, out_path: str | Path) -> Path:
    """Extract the last frame of a video to a PNG using ffmpeg (for first-frame seeding)."""
    if shutil.which("ffmpeg") is None:
        raise RenderError(
            "ffmpeg not found on PATH — required to seed each shot from the prior clip's last "
            "frame. Install ffmpeg (see scripts/doctor.py)."
        )
    clip_path = Path(clip_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # -sseof -0.1 seeks ~0.1s before the end, then grab a single frame.
    cmd = [
        "ffmpeg", "-y", "-sseof", "-0.1", "-i", str(clip_path),
        "-frames:v", "1", "-q:v", "2", str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out_path.exists():
        raise RenderError(f"ffmpeg failed to extract last frame from {clip_path}: {proc.stderr[-400:]}")
    return out_path


def _seed_for_shot(
    project_dir: Path, manifest: Manifest, shot: Shot, character
) -> Optional[str]:
    """Determine the first_frame_path for a shot.

    shot.seed_from is None  -> canonical Nimbo ref (the first reference image).
    shot.seed_from = <id>    -> extract the LAST frame of that prior clip.
    Falls back to the canonical ref (with a warning) if the prior clip is missing.
    """
    canonical = (
        str((project_dir / character.ref_images[0]).resolve())
        if character.ref_images
        else None
    )

    if shot.seed_from is None:
        return canonical

    prior = manifest.shot_by_id(shot.seed_from)
    if prior is not None and prior.output_path:
        clip = project_dir / prior.output_path
        if clip.exists():
            seed_png = project_dir / "shots" / "seeds" / f"{shot.id}_seed.png"
            extract_last_frame(clip, seed_png)
            shot.seed_frame_path = os.path.relpath(seed_png, project_dir)
            return str(seed_png)

    print(
        f"  ! {shot.id}: prior clip for seed_from='{shot.seed_from}' not available — "
        f"falling back to the canonical Nimbo ref for continuity.",
        file=sys.stderr,
    )
    return canonical


# ──────────────────────────────────────────────────────────────────────────────
# Rendering a single shot (with retries)
# ──────────────────────────────────────────────────────────────────────────────


async def _render_shot(
    project_dir: Path,
    manifest: Manifest,
    shot: Shot,
    character,
    *,
    model_id: str,
    tier: str,
    out_dir: Path,
    aspect_ratio: str,
    resolution: str,
    max_retries: int,
) -> bool:
    """Render one shot via the generation MCP, retrying with backoff. Updates the shot
    in place (output_path / est_cost / attempts / error). Returns success."""
    shot.model = model_id
    shot.tier = tier
    shot.error = None

    out_path = out_dir / f"{shot.id}.mp4"
    refs = _resolve_refs(project_dir, shot.ref_images)
    first_frame = _seed_for_shot(project_dir, manifest, shot, character)
    api_duration = _snap_duration(model_id, shot.duration_s)

    for attempt in range(1, max_retries + 1):
        shot.attempts = attempt
        try:
            result = await gen.generate_clip(
                prompt=shot.prompt,
                output_path=str(out_path),
                model=model_id,
                duration_s=api_duration,
                reference_image_paths=refs,
                first_frame_path=first_frame,
                with_audio=False,  # silent — the user's MP3 is muxed at assembly
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                negative_prompt=character.negative or None,
            )
            shot.output_path = os.path.relpath(result["output_path"], project_dir)
            shot.est_cost = result.get("est_cost")
            shot.error = None
            return True
        except Exception as exc:  # noqa: BLE001 — capture + retry
            shot.error = str(exc)
            print(f"  ✗ {shot.id} attempt {attempt}/{max_retries} failed: {exc}", file=sys.stderr)
            if attempt < max_retries:
                await _sleep(_BACKOFF_BASE_S * (2 ** (attempt - 1)))
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Cost gate
# ──────────────────────────────────────────────────────────────────────────────


def _print_cost_gate(pass_name: str, model_id: str, to_render: list[Shot], skipped: int) -> float:
    total = round(sum(_estimate(model_id, s.duration_s) for s in to_render), 4)
    print(f"\n{pass_name.upper()} pass — model '{model_id}'")
    print(f"  to render: {len(to_render)} shot(s)   already done/skipped: {skipped}")
    print("─" * 60)
    for s in to_render:
        d = _snap_duration(model_id, s.duration_s)
        print(f"  {s.id:<8} {d:>2}s   est ${_estimate(model_id, s.duration_s):.3f}")
    print("─" * 60)
    print(f"  TOTAL estimated spend: ${total:.2f}  (estimate — verify on the dashboard)")
    return total


# ──────────────────────────────────────────────────────────────────────────────
# Passes
# ──────────────────────────────────────────────────────────────────────────────


async def _run_pass(
    project_dir: Path,
    *,
    pass_name: str,
    renderable: set[ShotStatus],
    in_progress_status: ShotStatus,
    success_status: ShotStatus,
    model_attr: str,
    tier_attr: str,
    out_subdir: str,
    confirm: bool,
    max_retries: int,
) -> PassSummary:
    manifest_path = project_dir / "manifest.json"
    if not manifest_path.exists():
        raise RenderError(f"{manifest_path} not found — run the planner (Prompt 4) first.")
    manifest = load_manifest(manifest_path)
    character = load_character(project_dir)

    model_id = getattr(manifest.project, model_attr) or manifest.project.draft_model
    tier = getattr(manifest.project, tier_attr) or "draft"
    if model_id is None:
        raise RenderError(f"manifest has no {model_attr} set.")

    to_render = [s for s in manifest.shots if s.status in renderable]
    skipped = [s for s in manifest.shots if s.status not in renderable]

    est = _print_cost_gate(pass_name, model_id, to_render, len(skipped))
    summary = PassSummary(
        pass_name=pass_name,
        model=model_id,
        est_cost=est,
        skipped=[s.id for s in skipped],
        confirmed=confirm,
    )

    if not confirm:
        print(f"\nCost gate: pass --yes to render these {len(to_render)} shot(s). No spend yet.")
        return summary

    out_dir = project_dir / out_subdir
    out_dir.mkdir(parents=True, exist_ok=True)

    for shot in manifest.shots:
        if shot.status not in renderable:
            continue
        shot.status = in_progress_status
        save_manifest(manifest, manifest_path)  # mark in-progress BEFORE the call (resumability)

        print(f"→ {pass_name}: {shot.id} ({_snap_duration(model_id, shot.duration_s)}s) …")
        ok = await _render_shot(
            project_dir, manifest, shot, character,
            model_id=model_id, tier=tier, out_dir=out_dir,
            aspect_ratio=manifest.project.aspect_ratio,
            resolution=manifest.project.resolution,
            max_retries=max_retries,
        )
        shot.status = success_status if ok else ShotStatus.failed
        save_manifest(manifest, manifest_path)  # save AFTER each shot (resumability)

        if ok:
            summary.rendered.append(shot.id)
            print(f"  ✓ {shot.id} -> {shot.output_path}")
        else:
            summary.failed.append(shot.id)
            print(f"  ✗ {shot.id} left failed after {max_retries} attempt(s)")

    print(
        f"\n{pass_name} done: {len(summary.rendered)} rendered, "
        f"{len(summary.failed)} failed, {len(summary.skipped)} skipped."
    )
    return summary


async def draft_pass(project_dir: Path, *, confirm: bool, max_retries: int = 3) -> PassSummary:
    """Render every renderable shot on the cheapest (draft) model/tier into shots/."""
    return await _run_pass(
        project_dir,
        pass_name="draft",
        renderable=_DRAFT_RENDERABLE,
        in_progress_status=ShotStatus.drafting,
        success_status=ShotStatus.drafted,
        model_attr="draft_model",
        tier_attr="draft_tier",
        out_subdir="shots",
        confirm=confirm,
        max_retries=max_retries,
    )


async def final_pass(project_dir: Path, *, confirm: bool, max_retries: int = 3) -> PassSummary:
    """Re-render only APPROVED shots on the locked final model/tier into finals/."""
    return await _run_pass(
        project_dir,
        pass_name="final",
        renderable=_FINAL_RENDERABLE,
        in_progress_status=ShotStatus.rendering,
        success_status=ShotStatus.done,
        model_attr="final_model",
        tier_attr="final_tier",
        out_subdir="finals",
        confirm=confirm,
        max_retries=max_retries,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Resumable render loop for Nimbo shots.")
    p.add_argument("--project", required=True)
    p.add_argument("--projects-root", default=str(_PROJECT_ROOT / "projects"))
    p.add_argument("--pass", dest="which", choices=["draft", "final"], default="draft")
    p.add_argument("--yes", action="store_true", help="Confirm the spend and actually render.")
    p.add_argument("--max-retries", type=int, default=3)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    project_dir = Path(args.projects_root) / args.project
    try:
        if args.which == "draft":
            summary = asyncio.run(draft_pass(project_dir, confirm=args.yes, max_retries=args.max_retries))
        else:
            summary = asyncio.run(final_pass(project_dir, confirm=args.yes, max_retries=args.max_retries))
    except RenderError as exc:
        print(f"render error: {exc}", file=sys.stderr)
        return 2
    # Non-zero if a confirmed pass left any shot failed.
    if summary.confirmed and summary.failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
