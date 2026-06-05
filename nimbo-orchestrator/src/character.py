"""Nimbo character config + style lock (Prompt 3).

No LLM calls — Nimbo is fixed and supplied by the user (see ../docs/CHARACTER_BIBLE.md).

Responsibilities:
  - Hold the LOCKED strings verbatim: style_lock (prepended to every prompt), negative
    (never-render list), palette (hex).
  - Discover / validate reference images (exists, is an image, clean-ish background — warn,
    best-effort).
  - build_prompt(shot, character): lead with the style_lock, then a per-shot scene line built
    ONLY from action / scene / camera. The action is stripped of Nimbo appearance descriptors
    — identity is carried by the reference images on every call, not by words (ARCHITECTURE.md
    decision #4). The short identity anchor lives in the fixed style_lock; the variable
    per-shot text stays appearance-free.

CLI:
  python -m src.character --project blue-song            # write/refresh character.json
  python -m src.character --project blue-song --check     # validate existing character.json
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Optional

from .models import Character, Shot, load_json_model, save_json_model

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHARACTER_FILENAME = "character.json"
REFS_DIRNAME = "refs"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


class CharacterError(RuntimeError):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# LOCKED strings (verbatim from docs/CHARACTER_BIBLE.md)
# ──────────────────────────────────────────────────────────────────────────────

# style_lock is prepended to EVERY shot prompt. It carries the short identity anchor
# (the bible's episode-template opening) + the fixed art-direction. This is exactly how
# the user renders each episode. Per-shot prompts add only action/scene/camera.
NIMBO_STYLE_LOCK = (
    "The same character Nimbo (small round cream plush-like creature, oversized gentle "
    "close-set eyes, soft coral cheeks, soft glowing head cloud), consistent design and "
    "proportions. "
    "Style: clean soft 3D render, matte soft-touch surfaces, simple rounded shapes, gentle "
    "even lighting, soothing muted pastel palette with one accent, NO neon, calm uncluttered "
    "composition. Designer-toy / gentle Pixar-adjacent look. No text, no logos. --ar 16:9"
)

NIMBO_NEGATIVE = (
    "neon colors, oversaturated, busy cluttered background, harsh lighting, scary, sharp "
    "teeth, star shape, raindrop shape, human toddler, oversized-head toddler family, fox "
    "mascot, shark family, yellow chick, belly badge, transforming robot, text, watermark, "
    "logo, brand names"
)

NIMBO_PALETTE = [
    "#F4EBD8",  # Body Cream
    "#FBF4E6",  # Belly Light
    "#E0D2B5",  # Edge / Line
    "#F1B49E",  # Cheek Coral
    "#322B22",  # Eyes Ink
    "#F2C84B",  # Cloud Glow
]

DEFAULT_REF_FILENAME = "nimbo_modelsheet.png"


# ──────────────────────────────────────────────────────────────────────────────
# Appearance-term stripping (keeps per-shot prompts identity-free)
# ──────────────────────────────────────────────────────────────────────────────

# Phrases that REDESCRIBE Nimbo's body/material. These must never appear in per-shot
# action text — identity comes from the reference images. Note "cloud" is intentionally
# absent: what the cloud DOES (glows a color / forms an animal face) is core action.
_APPEARANCE_TERMS = [
    "cream-colored", "cream colored", "cream",
    "oatmeal",
    "egg-shaped body", "egg shaped body", "egg-shaped", "egg shaped",
    "plush-like creature", "plush creature", "plush-like", "plush",
    "matte", "soft-touch",
    "designer-toy", "designer toy",
    "oversized eyes", "close-set eyes", "big eyes",
    "coral cheeks", "soft cheeks",
    "stubby little feet", "stubby feet",
    "short rounded arms", "rounded arms",
    "round body", "rounded body",
    "tiny smile",
    "fluffy", "furry",
    "creature", "mascot",
    # standalone size/shape adjectives that describe Nimbo's body
    "round", "small", "tiny",
]

# Longest-first so multi-word phrases match before their sub-words.
_APPEARANCE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in sorted(_APPEARANCE_TERMS, key=len, reverse=True)) + r")\b",
    flags=re.IGNORECASE,
)


# Connector words that are meaningless on their own once the appearance terms they
# joined have been removed (e.g. "with oversized eyes and coral cheeks" -> "with and").
_CLAUSE_STOPWORDS = {"a", "an", "the", "with", "and", "of", "that", "is", "are", "who"}


def _tidy_clause(clause: str) -> str:
    """Drop leading/trailing stopwords left dangling after appearance terms were removed."""
    toks = clause.split()
    while toks and toks[0].lower() in _CLAUSE_STOPWORDS:
        toks.pop(0)
    while toks and toks[-1].lower() in _CLAUSE_STOPWORDS:
        toks.pop()
    return " ".join(toks)


def strip_appearance(text: str) -> str:
    """Remove Nimbo appearance descriptors from a free-text action, then tidy punctuation.

    Works clause-by-clause (comma-separated) so a removed descriptor doesn't leave behind a
    dangling article or connector (e.g. "Nimbo, a cream plush creature, waves" -> "Nimbo, waves").
    """
    if not text:
        return ""
    out = _APPEARANCE_RE.sub("", text)
    out = re.sub(r"\s+", " ", out)
    clauses = [_tidy_clause(c) for c in out.split(",")]
    clauses = [c for c in clauses if c]  # drop clauses that became empty
    out = ", ".join(clauses)
    out = re.sub(r"\s+([.;])", r"\1", out)  # space before sentence punctuation
    out = re.sub(r"\s+", " ", out).strip(" ,;")
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Character construction / IO
# ──────────────────────────────────────────────────────────────────────────────


def discover_refs(project_dir: str | Path) -> list[str]:
    """Return reference image paths (relative to the project dir) found in refs/, sorted."""
    refs_dir = Path(project_dir) / REFS_DIRNAME
    if not refs_dir.is_dir():
        return []
    found = [
        f"{REFS_DIRNAME}/{p.name}"
        for p in sorted(refs_dir.iterdir())
        if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
    ]
    return found


def default_character(name: str = "Nimbo", ref_images: Optional[list[str]] = None) -> Character:
    """Build the locked Nimbo Character. ref_images default to a single model-sheet path."""
    return Character(
        name=name,
        ref_images=list(ref_images) if ref_images else [f"{REFS_DIRNAME}/{DEFAULT_REF_FILENAME}"],
        style_lock=NIMBO_STYLE_LOCK,
        negative=NIMBO_NEGATIVE,
        palette=list(NIMBO_PALETTE),
    )


def write_character_json(
    project_dir: str | Path, character: Optional[Character] = None
) -> Path:
    """Write character.json. If no character is given, build the default and auto-discover refs."""
    pdir = Path(project_dir)
    if character is None:
        refs = discover_refs(pdir)
        character = default_character(ref_images=refs or None)
    path = pdir / CHARACTER_FILENAME
    save_json_model(character, path)
    return path


def load_character(project_dir: str | Path) -> Character:
    path = Path(project_dir) / CHARACTER_FILENAME
    if not path.exists():
        raise CharacterError(f"character.json not found in {project_dir} (run write_character_json)")
    return load_json_model(Character, path)  # type: ignore[return-value]


# ──────────────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────────────


def _looks_clean_background(img) -> bool:
    """Best-effort: are the image corners light + fairly uniform (clean studio bg)?"""
    try:
        rgb = img.convert("RGB")
        w, h = rgb.size
        if w < 8 or h < 8:
            return True  # too small to judge; don't nag
        box = max(2, min(w, h) // 20)
        corners = [
            rgb.crop((0, 0, box, box)),
            rgb.crop((w - box, 0, w, box)),
            rgb.crop((0, h - box, box, h)),
            rgb.crop((w - box, h - box, w, h)),
        ]
        # Average each corner by downscaling it to a single pixel.
        means = [c.resize((1, 1)).getpixel((0, 0)) for c in corners]
        # light: every corner reasonably bright; uniform: corners agree with each other
        brightness_ok = all(sum(m) / 3 >= 180 for m in means)
        spread = max(
            abs(means[i][ch] - means[j][ch])
            for i in range(4)
            for j in range(i + 1, 4)
            for ch in range(3)
        )
        uniform_ok = spread <= 40
        return brightness_ok and uniform_ok
    except Exception:  # noqa: BLE001 — heuristic only; never fail validation on it
        return True


def validate_refs(character: Character, project_dir: str | Path) -> list[str]:
    """Return warnings for the reference images (empty = all good). Best-effort, never raises."""
    warnings: list[str] = []
    pdir = Path(project_dir)

    if not character.ref_images:
        warnings.append(
            "no reference images configured — Nimbo's identity WILL drift across clips. "
            "Render the model sheet (docs/CHARACTER_BIBLE.md) into refs/."
        )
        return warnings

    try:
        from PIL import Image
    except ImportError:
        warnings.append("Pillow not installed; cannot validate reference images.")
        Image = None  # type: ignore[assignment]

    for rel in character.ref_images:
        p = (pdir / rel) if not Path(rel).is_absolute() else Path(rel)
        if not p.exists():
            warnings.append(f"reference image missing: {rel}")
            continue
        if p.suffix.lower() not in _IMAGE_SUFFIXES:
            warnings.append(f"reference is not a known image type: {rel}")
            continue
        if Image is None:
            continue
        try:
            with Image.open(p) as img:
                img.verify()
            with Image.open(p) as img:  # reopen after verify() exhausts the file
                if not _looks_clean_background(img):
                    warnings.append(
                        f"{rel}: background may not be clean/light — a single clear subject on "
                        f"a plain background gives the most consistent results."
                    )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{rel}: not a readable image ({exc})")
    return warnings


def validate_character(character: Character, project_dir: str | Path) -> list[str]:
    """Full validation: refs + palette hex format + presence of the locked strings."""
    warnings = validate_refs(character, project_dir)

    if not character.style_lock.strip():
        warnings.append("style_lock is empty — every prompt must lead with the locked style.")
    if not character.negative.strip():
        warnings.append("negative prompt is empty.")
    for hexc in character.palette:
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", hexc):
            warnings.append(f"palette entry is not a #RRGGBB hex color: {hexc!r}")
    return warnings


# ──────────────────────────────────────────────────────────────────────────────
# build_prompt — the heart of identity consistency
# ──────────────────────────────────────────────────────────────────────────────


def build_prompt(shot: Shot, character: Character) -> str:
    """Compose a shot prompt: lead with the locked style, then an appearance-free scene line.

    The returned prompt ALWAYS starts with character.style_lock. The per-shot portion (scene /
    action / camera) is stripped of Nimbo appearance descriptors so identity rides on the
    reference images, not the words.
    """
    action = strip_appearance(shot.action or "")
    # Avoid "Nimbo Nimbo ..." if the planner's action already starts with the name.
    action = re.sub(r"^(nimbo)\b[\s,]*", "", action, flags=re.IGNORECASE).strip()

    setting = (shot.scene or "").strip().rstrip(".")
    camera = (shot.camera or "").strip().rstrip(".")

    lines = [character.style_lock.strip()]

    if action and setting:
        lines.append(f"Scene: Nimbo {action}, in {setting}.")
    elif action:
        lines.append(f"Scene: Nimbo {action}.")
    elif setting:
        lines.append(f"Scene: Nimbo in {setting}.")

    if camera:
        lines.append(f"Camera: {camera}.")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Write/validate Nimbo's character.json.")
    p.add_argument("--project", required=True, help="Project name under the projects root.")
    p.add_argument(
        "--projects-root",
        default=str(_PROJECT_ROOT / "projects"),
        help="Root dir that holds project folders (default: ./projects).",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="Validate an existing character.json instead of writing one.",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    project_dir = Path(args.projects_root) / args.project

    if args.check:
        character = load_character(project_dir)
        print(f"Loaded {project_dir / CHARACTER_FILENAME}")
    else:
        path = write_character_json(project_dir)
        character = load_character(project_dir)
        print(f"wrote {path}")
        print(f"  refs discovered: {character.ref_images}")

    warnings = validate_character(character, project_dir)
    if warnings:
        print("\nValidation warnings:")
        for w in warnings:
            print(f"  ! {w}")
    else:
        print("\nValidation: all good.")

    # Demonstrate build_prompt with a representative shot.
    demo = Shot(
        id="demo",
        start_s=0.0,
        end_s=8.0,
        duration_s=8.0,
        scene="a soft pastel meadow with gentle negative space",
        camera="slow push-in",
        action="Nimbo, a cream-colored round plush creature, waves hello while the cloud glows blue",
    )
    print("\nbuild_prompt() demo (note the appearance words are stripped from the action):")
    print("-" * 70)
    print(build_prompt(demo, character))
    print("-" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
