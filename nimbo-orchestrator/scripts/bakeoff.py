#!/usr/bin/env python3
"""Model bake-off harness (Prompt 1.5).

Render ONE identical Nimbo test shot across the candidate workhorse models so you can
eyeball which holds Nimbo's identity best and at what cost, then lock the winner.

Usage:
  python scripts/bakeoff.py                                    # dry-run (no spend)
  python scripts/bakeoff.py --yes                              # default: seedance-fast, veo-3.1-fast, kling-v3-standard
  python scripts/bakeoff.py --yes --models veo-3.1,seedance-2.0
  python scripts/bakeoff.py --refs projects/blue-song/refs/*.png --yes

Outputs go to ``bakeoff/<model>.mp4`` side by side. Per Part 4 of the playbook the
total spend should be a few dollars — defaults render one ~8s clip per model.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# ── Load the generation MCP module directly ──────────────────────────────────
# The MCP server can be spoken-to over stdio (that's the render loop's path in Prompt 5),
# but for a one-shot script, importing the underlying functions is simpler and gives us
# the same code path.
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
_GEN_PATH = _PROJECT_ROOT / "mcp" / "gen_server.py"

_spec = importlib.util.spec_from_file_location("nimbo_gen_server", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
assert _spec is not None and _spec.loader is not None
sys.modules["nimbo_gen_server"] = gen
_spec.loader.exec_module(gen)


# ── Defaults ─────────────────────────────────────────────────────────────────

# Workhorse candidates from Part 1.5 of the playbook.
DEFAULT_MODELS = ("seedance-2.0-fast", "veo-3.1-fast", "kling-v3-standard")

# A deliberately generic action — appearance-free, identity carried by refs.
# Pulled from the episode template in docs/CHARACTER_BIBLE.md.
DEFAULT_PROMPT = (
    "The same character Nimbo (small round cream plush-like creature, oversized gentle "
    "close-set eyes, soft coral cheeks, soft glowing head cloud), consistent design and "
    "proportions. Scene: Nimbo waves a small hello while gently bobbing in place, in a "
    "soft pastel meadow with simple negative space. The cloud glows a clear blue for "
    "the song about the color blue. Style: clean soft 3D render, matte soft-touch "
    "surfaces, simple rounded shapes, gentle even lighting, soothing muted pastel "
    "palette with one accent, NO neon, calm uncluttered composition. Designer-toy / "
    "gentle Pixar-adjacent look. No text, no logos.  --ar 16:9"
)

DEFAULT_NEGATIVE = (
    "neon colors, oversaturated, busy cluttered background, harsh lighting, scary, "
    "sharp teeth, star shape, raindrop shape, human toddler, oversized-head toddler "
    "family, fox mascot, shark family, yellow chick, belly badge, transforming robot, "
    "text, watermark, logo, brand names"
)


# ── Result table ─────────────────────────────────────────────────────────────


@dataclass
class BakeoffRow:
    model: str
    seconds: int
    est_cost: float
    output_path: Optional[str]
    status: str  # "done" | "skipped" | "failed: <reason>"
    error: Optional[str] = None


def _print_table(rows: list[BakeoffRow]) -> None:
    headers = ("model", "seconds", "est_cost", "output_path", "status")
    widths = [
        max(len(h), max((len(getattr(r, h) or "") if h == "output_path" else len(str(getattr(r, h))) for r in rows), default=0))
        for h in headers
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print()
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for r in rows:
        print(
            fmt.format(
                r.model,
                str(r.seconds),
                f"${r.est_cost:.3f}",
                r.output_path or "-",
                r.status,
            )
        )
    print()


def _estimate_cost(model_id: str, seconds: int) -> float:
    spec = gen.MODEL_REGISTRY.get(model_id)
    if spec is None:
        return 0.0
    return round(spec.per_second_cost * seconds, 4)


def _normalize_duration(model_id: str, requested: int) -> int:
    """Snap to the nearest allowed duration if the model uses a discrete set."""
    spec = gen.MODEL_REGISTRY.get(model_id)
    if spec is None or spec.allowed_durations is None:
        return min(requested, spec.max_seconds if spec else requested)
    # snap down to the closest allowed value <= requested, else the smallest
    allowed = sorted(spec.allowed_durations)
    eligible = [d for d in allowed if d <= requested]
    return max(eligible) if eligible else allowed[0]


# ── Main ─────────────────────────────────────────────────────────────────────


async def run_bakeoff(
    models: Iterable[str],
    refs: list[str],
    *,
    prompt: str,
    negative_prompt: str,
    duration_s: int,
    aspect_ratio: str,
    resolution: str,
    output_dir: Path,
    confirm: bool,
) -> list[BakeoffRow]:
    """Run the bake-off. Returns one row per model regardless of pass/fail."""

    # Per-model normalized duration + cost so the user sees the bill before spending.
    plan: list[tuple[str, int, float]] = []
    for m in models:
        if m not in gen.MODEL_REGISTRY:
            print(f"  ! unknown model '{m}' — skipping", file=sys.stderr)
            continue
        d = _normalize_duration(m, duration_s)
        plan.append((m, d, _estimate_cost(m, d)))

    total = sum(c for _, _, c in plan)

    print("Bake-off plan")
    print("─" * 44)
    print(f"  prompt:       {prompt[:80]}{'…' if len(prompt) > 80 else ''}")
    print(f"  refs:         {len(refs)} image(s)")
    print(f"  aspect/res:   {aspect_ratio} / {resolution}")
    print(f"  output_dir:   {output_dir}")
    print()
    print("  Per-model breakdown (cost is an estimate, verify on dashboard):")
    for model_id, d, cost in plan:
        spec = gen.MODEL_REGISTRY[model_id]
        print(f"    - {model_id:<24} duration={d}s  est ${cost:.3f}  ({spec.label})")
    print()
    print(f"  TOTAL estimated spend: ${total:.2f}")
    print()

    if not confirm:
        print("Dry run — no API calls made. Re-run with --yes to spend.")
        return [
            BakeoffRow(
                model=m,
                seconds=d,
                est_cost=c,
                output_path=None,
                status="skipped (dry-run)",
            )
            for m, d, c in plan
        ]

    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[BakeoffRow] = []

    for model_id, d, cost in plan:
        out_path = output_dir / f"{model_id}.mp4"
        print(f"→ rendering {model_id} ({d}s, est ${cost:.3f}) …")
        try:
            result = await gen.generate_clip(
                prompt=prompt,
                output_path=str(out_path),
                model=model_id,
                duration_s=d,
                reference_image_paths=refs,
                first_frame_path=None,
                with_audio=False,  # silent — drafts are previewed muted
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                negative_prompt=negative_prompt,
                seed=None,
            )
            rows.append(
                BakeoffRow(
                    model=model_id,
                    seconds=result["seconds"],
                    est_cost=result["est_cost"],
                    output_path=result["output_path"],
                    status="done",
                )
            )
            print(f"  ✓ saved {result['output_path']}")
        except Exception as exc:  # noqa: BLE001 — surface the error in the row
            rows.append(
                BakeoffRow(
                    model=model_id,
                    seconds=d,
                    est_cost=cost,
                    output_path=None,
                    status=f"failed",
                    error=str(exc),
                )
            )
            print(f"  ✗ {model_id} failed: {exc}", file=sys.stderr)

    return rows


def _expand_refs(patterns: list[str]) -> list[str]:
    out: list[str] = []
    for p in patterns:
        matches = sorted(glob.glob(p))
        if matches:
            out.extend(matches)
        elif Path(p).exists():
            out.append(p)
        else:
            print(f"  ! ref pattern matched nothing: {p}", file=sys.stderr)
    return out


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render one identical Nimbo shot across candidate models."
    )
    parser.add_argument(
        "--models",
        type=lambda s: [m.strip() for m in s.split(",") if m.strip()],
        default=list(DEFAULT_MODELS),
        help=f"Comma-separated model IDs (default: {','.join(DEFAULT_MODELS)})",
    )
    parser.add_argument(
        "--refs",
        nargs="+",
        default=[],
        help="Reference image paths or globs (e.g. projects/blue-song/refs/*.png).",
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Override the test prompt.")
    parser.add_argument(
        "--negative-prompt", default=DEFAULT_NEGATIVE, help="Override the negative prompt."
    )
    parser.add_argument(
        "--duration-s",
        type=int,
        default=8,
        help="Target clip length in seconds (snapped to each model's allowed set).",
    )
    parser.add_argument("--aspect-ratio", default="16:9")
    parser.add_argument("--resolution", default="720p")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_PROJECT_ROOT / "bakeoff",
        help="Where to write <model>.mp4 files (default: ./bakeoff).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the spend and actually call the APIs. Without this it's a dry-run.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    refs = _expand_refs(args.refs)

    rows = asyncio.run(
        run_bakeoff(
            models=args.models,
            refs=refs,
            prompt=args.prompt,
            negative_prompt=args.negative_prompt,
            duration_s=args.duration_s,
            aspect_ratio=args.aspect_ratio,
            resolution=args.resolution,
            output_dir=args.output_dir,
            confirm=args.yes,
        )
    )
    _print_table(rows)

    # Non-zero exit if any model failed in a real run (so CI can flag).
    if args.yes and any(r.status.startswith("failed") for r in rows):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
