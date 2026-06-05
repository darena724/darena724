"""Shot planner (Prompt 4): song + character -> ordered shots folded into manifest.json.

No external LLM calls. Reads lyrics.json + character.json, tiles the song timeline into
shots that each fit the LOCKED model's clip cap (read from the generation MCP's
list_models()), aligned to lyric-section boundaries, covering the WHOLE song with no
gaps/overlaps.

Per shot it sets:
  - id, start_s, end_s, duration_s          (contiguous timeline coverage)
  - scene   : one gentle setting per lyric section (varied across sections)
  - camera  : gentle move, varied per shot
  - action  : what Nimbo DOES + what the CLOUD does for this lyric moment + mood.
              NEVER appearance — identity rides on ref images (ARCHITECTURE.md #4).
  - prompt  : character.build_prompt(shot)  (leads with the locked style)
  - ref_images, seed_from                   (shot 1 -> canonical ref; shot N -> prior shot)
  - status="pending", model+tier = cheapest draft option

CLI:
  python -m src.planner --project blue-song --topic "learning the color blue"
  python -m src.planner --project blue-song --dry-run        # print table, don't save
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import sys
from pathlib import Path
from typing import Optional

from . import lyrics as lyrics_mod
from .character import build_prompt, load_character
from .models import (
    Lyrics,
    Manifest,
    ProjectMeta,
    Shot,
    ShotStatus,
    load_json_model,
    load_manifest,
    save_manifest,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# Load the generation MCP module directly (it lives outside the `src` package and shares
# its dir name with the `mcp` SDK, so we import by path — same pattern as the bake-off).
def _load_gen():
    gen_path = _PROJECT_ROOT / "mcp" / "gen_server.py"
    spec = importlib.util.spec_from_file_location("nimbo_gen_server", gen_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules.setdefault("nimbo_gen_server", mod)
    spec.loader.exec_module(mod)
    return mod


gen = _load_gen()


class PlannerError(RuntimeError):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Defaults: cheapest draft model + a matching final model (same clip cap)
# ──────────────────────────────────────────────────────────────────────────────

# Seedance Fast is the cheapest draft tier AND engineered for cross-scene character
# consistency (Part 1.5) — a sensible default workhorse. Its Pro tier shares the 15s cap,
# so draft and final agree on shot boundaries.
DEFAULT_DRAFT_MODEL = "seedance-2.0-fast"
DEFAULT_FINAL_MODEL = "seedance-2.0"

DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_RESOLUTION = "720p"


# ──────────────────────────────────────────────────────────────────────────────
# Creative-direction vocabularies (gentle, calm, appearance-free)
# ──────────────────────────────────────────────────────────────────────────────

# One scene per lyric section (cycled). Soft settings with negative space per the bible.
_SCENES = [
    "a soft pastel meadow with gentle negative space",
    "a calm open sky with slow drifting clouds",
    "a cozy sunlit room with soft rounded furniture",
    "a quiet pastel garden with room to breathe",
    "a gentle pond with calm, glassy reflections",
    "a soft hillside under a warm low sun",
    "a dreamy starlit evening with a calm horizon",
    "a peaceful seaside with slow, gentle waves",
]

# Gentle camera moves, varied per shot.
_CAMERAS = [
    "slow push-in",
    "gentle slow pan to the right",
    "static framing with a soft gentle bob",
    "slow pull-back to reveal the scene",
    "soft slow orbit around Nimbo",
    "gentle tilt up",
]

# What Nimbo DOES — appearance-free verb phrases, calm in tone.
_ACTIONS = [
    "gazes up with calm, happy wonder",
    "sways slowly from side to side",
    "claps softly and gives a little smile",
    "points up with a gentle, happy gesture",
    "drifts and bobs peacefully",
    "turns slowly in place, calm and gentle",
    "reaches up softly, then settles back down",
    "nods along to the gentle rhythm",
    "tilts to one side with a curious, calm look",
    "rocks gently and looks toward the camera",
]
_FIRST_ACTION = "waves a gentle hello and sways softly"
_LAST_ACTION = "waves a soft goodbye and settles down to rest"


def _cloud_phrase(category: str, concept: str) -> str:
    """Describe what the head-cloud does for this song (core action, never appearance)."""
    concept = (concept or "").strip()
    if category == "color":
        color = concept.split()[-1] if concept else "a soft color"
        return f"while the head-cloud glows a clear, gentle {color}"
    if category == "animal":
        animal = concept.split()[-1] if concept else "animal"
        return f"while the head-cloud softly forms a simple, cute {animal} face"
    # number / shape / general
    return "while the head-cloud glows a soft, warm glow"


# ──────────────────────────────────────────────────────────────────────────────
# Model-cap lookup (via list_models, per spec)
# ──────────────────────────────────────────────────────────────────────────────


def _model_meta(model_id: str) -> dict:
    for m in gen.list_models():
        if m["id"] == model_id:
            return m
    raise PlannerError(
        f"Unknown model '{model_id}'. Known: {[m['id'] for m in gen.list_models()]}"
    )


def _clip_cap_seconds(model_id: str) -> int:
    return int(_model_meta(model_id)["max_seconds"])


# ──────────────────────────────────────────────────────────────────────────────
# Tiling
# ──────────────────────────────────────────────────────────────────────────────


def _tile_section(start_s: float, end_s: float, cap: int) -> list[tuple[float, float]]:
    """Split [start, end] into the fewest equal slots that each fit within `cap` seconds.

    Returns contiguous (start, end) pairs covering the section exactly (no gaps/overlaps).
    """
    duration = end_s - start_s
    if duration <= 0:
        return []
    n = max(1, math.ceil(round(duration, 6) / cap))
    base = duration / n
    slots: list[tuple[float, float]] = []
    for k in range(n):
        s = start_s + k * base
        e = end_s if k == n - 1 else start_s + (k + 1) * base
        slots.append((round(s, 3), round(e, 3)))
    return slots


# ──────────────────────────────────────────────────────────────────────────────
# Planning
# ──────────────────────────────────────────────────────────────────────────────


def plan_shots(
    lyrics: Lyrics,
    character,
    *,
    topic: str,
    draft_model: str,
    draft_tier: str = "draft",
) -> list[Shot]:
    """Build the ordered shot list (no IO). Identity comes from refs; cloud behavior and
    scene/action are driven by the song's category + sections."""
    if not lyrics.sections:
        raise PlannerError("lyrics has no sections — run the song step (Prompt 2) first.")

    cap = _clip_cap_seconds(draft_model)
    concept = lyrics_mod._extract_concept(topic)
    category = lyrics_mod._detect_category(topic, concept)
    cloud = _cloud_phrase(category, concept)

    # First pass: collect (section_index, start, end) slots across the whole song.
    raw: list[tuple[int, float, float]] = []
    for si, section in enumerate(lyrics.sections):
        for (s, e) in _tile_section(section.start_s, section.end_s, cap):
            raw.append((si, s, e))

    total = len(raw)
    width = max(2, len(str(total)))
    shots: list[Shot] = []

    for gidx, (si, s, e) in enumerate(raw):
        is_first = gidx == 0
        is_last = gidx == total - 1

        if is_first:
            action_verb = _FIRST_ACTION
        elif is_last:
            action_verb = _LAST_ACTION
        else:
            action_verb = _ACTIONS[gidx % len(_ACTIONS)]

        action = f"{action_verb} {cloud}"
        scene = _SCENES[si % len(_SCENES)]
        camera = _CAMERAS[gidx % len(_CAMERAS)]

        shot = Shot(
            id=f"shot_{gidx + 1:0{width}d}",
            start_s=s,
            end_s=e,
            duration_s=round(e - s, 3),
            scene=scene,
            camera=camera,
            action=action,
            ref_images=list(character.ref_images),
            # shot 1 seeds from the canonical Nimbo ref (seed_from=None -> render loop uses ref);
            # shot N>1 seeds from the previous shot's last frame.
            seed_from=None if is_first else shots[gidx - 1].id,
            status=ShotStatus.pending,
            model=draft_model,
            tier=draft_tier,
        )
        shot.prompt = build_prompt(shot, character)
        shots.append(shot)

    return shots


def build_manifest(
    project_name: str,
    lyrics: Lyrics,
    shots: list[Shot],
    *,
    topic: str,
    source: str,
    draft_model: str,
    final_model: str,
    aspect_ratio: str,
    resolution: str,
    existing: Optional[Manifest] = None,
) -> Manifest:
    """Fold the shots into a Manifest, preserving prior project meta where sensible."""
    target = lyrics.total_duration_s

    if existing is not None:
        meta = existing.project
        meta.topic = topic or meta.topic
        meta.source = source or meta.source
        meta.target_duration_s = target
        meta.draft_model = draft_model
        meta.draft_tier = "draft"
        meta.final_model = final_model
        meta.final_tier = "pro" if final_model.endswith(("-pro", "2.0")) else "standard"
        meta.aspect_ratio = aspect_ratio
        meta.resolution = resolution
    else:
        meta = ProjectMeta(
            name=project_name,
            topic=topic,
            source=source,
            target_duration_s=target,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            draft_model=draft_model,
            draft_tier="draft",
            final_model=final_model,
            final_tier="pro" if final_model.endswith(("-pro", "2.0")) else "standard",
            song_path="song.mp3",
        )

    return Manifest(project=meta, shots=shots)


# ──────────────────────────────────────────────────────────────────────────────
# Pretty-print review table
# ──────────────────────────────────────────────────────────────────────────────


def _section_for_time(lyrics: Lyrics, t: float) -> str:
    for sec in lyrics.sections:
        if sec.start_s <= t < sec.end_s:
            return sec.name
    return lyrics.sections[-1].name if lyrics.sections else "?"


def print_shot_table(manifest: Manifest, lyrics: Lyrics) -> None:
    shots = manifest.shots
    print(f"\nShot plan — {manifest.project.name}  ({len(shots)} shots, "
          f"{manifest.covered_duration_s:.1f}s covered / {manifest.project.target_duration_s:.1f}s song)")
    print(f"  draft: {manifest.project.draft_model} ({manifest.project.draft_tier})   "
          f"final: {manifest.project.final_model} ({manifest.project.final_tier})   "
          f"{manifest.project.aspect_ratio} {manifest.project.resolution}")
    print("─" * 100)
    header = f"{'id':<8} {'time':<14} {'dur':>5}  {'section':<9} {'seed_from':<8} action"
    print(header)
    print("─" * 100)
    for sh in shots:
        sect = _section_for_time(lyrics, sh.start_s)
        timerange = f"{sh.start_s:6.1f}-{sh.end_s:<6.1f}"
        action = sh.action if len(sh.action) <= 46 else sh.action[:43] + "…"
        seed = sh.seed_from or "ref"
        print(f"{sh.id:<8} {timerange:<14} {sh.duration_s:>4.1f}s  {sect:<9} {seed:<8} {action}")
    print("─" * 100)


# ──────────────────────────────────────────────────────────────────────────────
# Orchestration + CLI
# ──────────────────────────────────────────────────────────────────────────────


def run(
    project_dir: Path,
    *,
    topic: Optional[str],
    draft_model: str,
    final_model: str,
    aspect_ratio: str,
    resolution: str,
    save: bool,
) -> Manifest:
    lyrics_path = project_dir / "lyrics.json"
    if not lyrics_path.exists():
        raise PlannerError(f"{lyrics_path} not found — run the song step (Prompt 2) first.")
    lyrics: Lyrics = load_json_model(Lyrics, lyrics_path)  # type: ignore[assignment]
    character = load_character(project_dir)

    manifest_path = project_dir / "manifest.json"
    existing = load_manifest(manifest_path) if manifest_path.exists() else None

    # Resolve topic: explicit arg > existing manifest > error.
    resolved_topic = topic or (existing.project.topic if existing else None)
    if not resolved_topic:
        raise PlannerError(
            "No topic given and none stored in manifest.json. Pass --topic so the planner "
            "can drive the cloud behavior (color vs animal)."
        )
    source = (existing.project.source if existing else None) or "topic"

    # Validate the draft/final caps agree so shot boundaries are valid for both passes.
    draft_cap = _clip_cap_seconds(draft_model)
    final_cap = _clip_cap_seconds(final_model)
    if final_cap < draft_cap:
        print(
            f"  ! warning: final model '{final_model}' cap ({final_cap}s) < draft cap "
            f"({draft_cap}s). Shots are planned for {draft_cap}s and may exceed the final "
            f"model's limit. Pick a final model with an equal/larger cap.",
            file=sys.stderr,
        )

    shots = plan_shots(lyrics, character, topic=resolved_topic, draft_model=draft_model)
    manifest = build_manifest(
        project_dir.name,
        lyrics,
        shots,
        topic=resolved_topic,
        source=source,
        draft_model=draft_model,
        final_model=final_model,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        existing=existing,
    )

    print_shot_table(manifest, lyrics)

    if save:
        save_manifest(manifest, manifest_path)
        print(f"\nsaved {manifest_path}")
    else:
        print("\n(dry-run — manifest NOT saved)")
    return manifest


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Plan Nimbo shots from lyrics + character.")
    p.add_argument("--project", required=True)
    p.add_argument("--projects-root", default=str(_PROJECT_ROOT / "projects"))
    p.add_argument("--topic", default=None, help="Song topic (drives cloud behavior).")
    p.add_argument("--draft-model", default=DEFAULT_DRAFT_MODEL)
    p.add_argument("--final-model", default=DEFAULT_FINAL_MODEL)
    p.add_argument("--aspect-ratio", default=DEFAULT_ASPECT_RATIO)
    p.add_argument("--resolution", default=DEFAULT_RESOLUTION)
    p.add_argument("--dry-run", action="store_true", help="Print the table but don't save.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    project_dir = Path(args.projects_root) / args.project
    try:
        run(
            project_dir,
            topic=args.topic,
            draft_model=args.draft_model,
            final_model=args.final_model,
            aspect_ratio=args.aspect_ratio,
            resolution=args.resolution,
            save=not args.dry_run,
        )
    except PlannerError as exc:
        print(f"planner error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
