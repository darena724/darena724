"""End-to-end pipeline (Prompt 7): the `make-video` CLI with mandatory review stops.

The pipeline is a RESUMABLE state machine driven entirely by the manifest + on-disk
artifacts (every step reads/writes the manifest, so each can re-run independently). A single
invocation advances to the next incomplete stage and STOPs at each of the four review gates:

  1. SONG   — generate lyrics (topic) or ingest your MP3 -> STOP: review lyrics.json.
  2. PLAN   — build the shot plan                         -> STOP: review the shot table.
  3. DRAFT  — render every shot on the cheapest tier      -> STOP: mark shots approved/redo.
  4. FINAL  — re-render approved shots on the locked tier.
  5. ASSEMBLE -> final.mp4.

Guardrails:
  - The estimated cost is printed before the DRAFT and FINAL passes; --yes is REQUIRED to
    spend (without it the pipeline STOPs at the cost gate).
  - --dry-run prints the full plan + draft/final cost with ZERO API calls and no writes.

Usage:
  make-video --project blue-song --topic "learning the color blue"
  make-video --project blue-song --mp3 song.mp3 --captions
  make-video --project blue-song --dry-run
  make-video --project blue-song --yes            # advance through cost gates
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

from . import assemble as assemble_mod
from . import lyrics as lyrics_mod
from . import planner as planner_mod
from . import render as render_mod
from .character import load_character, write_character_json
from .models import Lyrics, Manifest, ShotStatus, load_json_model, load_manifest, save_manifest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class PipelineStop(Exception):
    """Raised to STOP the pipeline at a review gate (not an error)."""


def _hr(title: str) -> None:
    print("\n" + "═" * 70)
    print(f"  {title}")
    print("═" * 70)


# ──────────────────────────────────────────────────────────────────────────────
# Stage 1 — SONG
# ──────────────────────────────────────────────────────────────────────────────


def stage_song(
    project_dir: Path, *, topic: Optional[str], mp3: Optional[str], lyric_text: Optional[str]
) -> Lyrics:
    """Produce lyrics.json (+ music prompt). Idempotent: skips if lyrics.json exists."""
    lyrics_path = project_dir / "lyrics.json"
    if lyrics_path.exists():
        return load_json_model(Lyrics, lyrics_path)  # type: ignore[return-value]

    project_dir.mkdir(parents=True, exist_ok=True)
    if mp3:
        lyrics = lyrics_mod.ingest_mp3(mp3, lyric_text=lyric_text)
        music_prompt = lyrics_mod.build_music_gen_prompt(topic or project_dir.name, lyrics=lyrics)
    elif topic:
        lyrics = lyrics_mod.generate_lyrics(topic)
        music_prompt = lyrics_mod.build_music_gen_prompt(topic, lyrics=lyrics)
    else:
        raise PipelineStop(
            "No lyrics yet. Provide --topic \"...\" (generate a song) or --mp3 <path> "
            "(bring your own audio)."
        )
    lyrics_mod.write_lyrics_artifacts(lyrics, music_prompt, project_dir)
    return lyrics


# ──────────────────────────────────────────────────────────────────────────────
# Stage 2 — PLAN
# ──────────────────────────────────────────────────────────────────────────────


def stage_plan(
    project_dir: Path, *, topic: Optional[str], model: Optional[str], save: bool
) -> Manifest:
    """Build (or load) the shot plan. Ensures character.json exists first."""
    if not (project_dir / "character.json").exists():
        write_character_json(project_dir)

    manifest_path = project_dir / "manifest.json"
    if manifest_path.exists() and load_manifest(manifest_path).shots:
        return load_manifest(manifest_path)

    draft_model = model or planner_mod.DEFAULT_DRAFT_MODEL
    final_model = planner_mod.DEFAULT_FINAL_MODEL
    # If a specific model was locked, use it for both passes when caps allow.
    if model:
        final_model = model
    return planner_mod.run(
        project_dir,
        topic=topic,
        draft_model=draft_model,
        final_model=final_model,
        aspect_ratio=planner_mod.DEFAULT_ASPECT_RATIO,
        resolution=planner_mod.DEFAULT_RESOLUTION,
        save=save,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Driver
# ──────────────────────────────────────────────────────────────────────────────


def _counts(manifest: Manifest) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in manifest.shots:
        out[s.status.value] = out.get(s.status.value, 0) + 1
    return out


def run_pipeline(
    project_dir: Path,
    *,
    topic: Optional[str],
    mp3: Optional[str],
    lyric_text: Optional[str],
    model: Optional[str],
    captions: bool,
    confirm: bool,
    assemble_drafts: bool,
    dry_run: bool,
    max_retries: int = 3,
) -> Optional[Path]:
    """Advance the pipeline one stage at a time, STOPping at each review gate.
    Returns the path to final.mp4 if the run reaches assembly, else None."""

    # ── DRY RUN: lyrics + plan + cost, in memory, zero API calls, no writes ──────
    if dry_run:
        _hr("DRY RUN — plan + cost preview (no API calls, no writes)")
        if mp3:
            lyrics = lyrics_mod.ingest_mp3(mp3, lyric_text=lyric_text)
        elif topic:
            lyrics = lyrics_mod.generate_lyrics(topic)
        else:
            raise PipelineStop("--dry-run needs --topic or --mp3 to preview the plan.")
        # character may not exist yet — use the locked default in memory
        from .character import default_character

        character = default_character()
        draft_model = model or planner_mod.DEFAULT_DRAFT_MODEL
        final_model = model or planner_mod.DEFAULT_FINAL_MODEL
        shots = planner_mod.plan_shots(
            lyrics, character, topic=topic or project_dir.name, draft_model=draft_model
        )
        manifest = planner_mod.build_manifest(
            project_dir.name, lyrics, shots, topic=topic or project_dir.name,
            source="mp3" if mp3 else "topic", draft_model=draft_model, final_model=final_model,
            aspect_ratio=planner_mod.DEFAULT_ASPECT_RATIO, resolution=planner_mod.DEFAULT_RESOLUTION,
        )
        planner_mod.print_shot_table(manifest, lyrics)
        draft_cost = sum(render_mod._estimate(draft_model, s.duration_s) for s in shots)
        final_cost = sum(render_mod._estimate(final_model, s.duration_s) for s in shots)
        print(f"\nEstimated DRAFT cost (all {len(shots)} shots): ${draft_cost:.2f}")
        print(f"Estimated FINAL cost (if all approved):     ${final_cost:.2f}")
        print(f"Estimated MAX total:                         ${draft_cost + final_cost:.2f}")
        print("\n(dry-run) no lyrics/manifest written, no clips rendered.")
        return None

    # ── Stage 1: SONG ────────────────────────────────────────────────────────────
    lyrics_existed = (project_dir / "lyrics.json").exists()
    lyrics = stage_song(project_dir, topic=topic, mp3=mp3, lyric_text=lyric_text)
    if not lyrics_existed:
        _hr("STOP 1/4 — review the lyrics")
        print(f"  wrote {project_dir/'lyrics.json'}  ({len(lyrics.sections)} sections, "
              f"{lyrics.total_duration_s:.0f}s)")
        for sec in lyrics.sections:
            print(f"    {sec.start_s:6.1f}-{sec.end_s:<6.1f}  {sec.name}")
        print("\n  Edit lyrics.json if you like, then re-run to build the shot plan.")
        return None

    # ── Stage 2: PLAN ──────────────────────────────────────────────────────────────
    manifest_existed = (project_dir / "manifest.json").exists()
    manifest = stage_plan(project_dir, topic=topic, model=model, save=True)
    if not manifest_existed:
        _hr("STOP 2/4 — review the shot plan")
        print("  Edit manifest.json if you like, then re-run with --yes to draft-render.")
        return None

    # ── Stage 3: DRAFT ──────────────────────────────────────────────────────────────
    draft_renderable = [s for s in manifest.shots if s.status in render_mod._DRAFT_RENDERABLE]
    if draft_renderable:
        _hr("STAGE 3/4 — DRAFT render (cheapest tier)")
        summary = asyncio.run(
            render_mod.draft_pass(project_dir, confirm=confirm, max_retries=max_retries)
        )
        if not confirm:
            print("\n  STOP — pass --yes to spend the estimate above and render the drafts.")
            return None
        _hr("STOP 3/4 — review the draft clips")
        print(f"  draft clips in {project_dir/'shots'}/  "
              f"({len(summary.rendered)} rendered, {len(summary.failed)} failed)")
        print("  Watch each shot_*.mp4, then in manifest.json set each shot's \"status\" to:")
        print("    \"approved\" — keep it (re-rendered at higher quality in the final pass)")
        print("    \"redo\"     — re-draft it on the next run")
        print("  Then re-run with --yes for the final pass (or --assemble-drafts to skip it).")
        return None

    # All shots are drafted/approved/redo-resolved/done now.
    counts = _counts(manifest)
    approved = [s for s in manifest.shots if s.status is ShotStatus.approved]
    drafted_only = [s for s in manifest.shots if s.status is ShotStatus.drafted]
    done = [s for s in manifest.shots if s.status is ShotStatus.done]

    # ── Stage 4: FINAL ──────────────────────────────────────────────────────────────
    if approved and not assemble_drafts:
        _hr("STAGE 4/4 — FINAL render (approved shots, locked tier)")
        summary = asyncio.run(
            render_mod.final_pass(project_dir, confirm=confirm, max_retries=max_retries)
        )
        if not confirm:
            print("\n  STOP — pass --yes to spend the estimate above and final-render approved shots.")
            return None
        manifest = load_manifest(project_dir / "manifest.json")
        approved = [s for s in manifest.shots if s.status is ShotStatus.approved]
        drafted_only = [s for s in manifest.shots if s.status is ShotStatus.drafted]
        done = [s for s in manifest.shots if s.status is ShotStatus.done]

    # ── Approval gate: nothing approved yet and not told to assemble drafts ──────────
    if not done and drafted_only and not approved and not assemble_drafts:
        _hr("STOP 3/4 — waiting for your approvals")
        print("  Every shot is drafted but none are approved yet.")
        print("  In manifest.json set status \"approved\" (keep) or \"redo\" (re-draft) per shot,")
        print("  then re-run with --yes. Or run with --assemble-drafts to assemble the drafts as-is.")
        return None

    # ── Stage 5: ASSEMBLE ────────────────────────────────────────────────────────────
    _hr("STAGE 5 — ASSEMBLE -> final.mp4")
    out = assemble_mod.assemble(project_dir, captions=captions, dry_run=False)
    _hr("DONE")
    print(f"  ✓ {out}")
    return out


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="make-video", description="Turn a song + Nimbo into a ~3-min music video."
    )
    p.add_argument("--project", required=True)
    p.add_argument("--projects-root", default=str(_PROJECT_ROOT / "projects"))
    src = p.add_mutually_exclusive_group()
    src.add_argument("--topic", default=None, help="Song topic (generate lyrics).")
    src.add_argument("--mp3", default=None, help="Bring-your-own audio file.")
    p.add_argument("--lyric-text", default=None, help="Optional lyric text to fit to an MP3.")
    p.add_argument("--model", default=None, help="Locked video model (default: seedance fast/pro).")
    p.add_argument("--captions", action="store_true", help="Burn in lyric captions at assembly.")
    p.add_argument("--yes", action="store_true", help="Confirm spend at the cost gates.")
    p.add_argument("--assemble-drafts", action="store_true",
                   help="Skip the final pass and assemble the draft clips as-is.")
    p.add_argument("--dry-run", action="store_true", help="Print plan + cost; zero API calls.")
    p.add_argument("--max-retries", type=int, default=3)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    project_dir = Path(args.projects_root) / args.project
    try:
        run_pipeline(
            project_dir,
            topic=args.topic,
            mp3=args.mp3,
            lyric_text=args.lyric_text,
            model=args.model,
            captions=args.captions,
            confirm=args.yes,
            assemble_drafts=args.assemble_drafts,
            dry_run=args.dry_run,
            max_retries=args.max_retries,
        )
    except PipelineStop as stop:
        print(f"\n{stop}", file=sys.stderr)
        return 0  # a review stop is a normal, expected exit
    except (planner_mod.PlannerError, render_mod.RenderError, assemble_mod.AssembleError,
            lyrics_mod.LyricsError) as exc:
        print(f"\npipeline error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
