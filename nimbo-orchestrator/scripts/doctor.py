#!/usr/bin/env python3
"""Environment doctor for nimbo-orchestrator.

Verifies Python version, ffmpeg/ffprobe, and that key Python deps import.
If ffmpeg/ffprobe are missing, prints install instructions and exits non-zero
(per Prompt 0: "Verify ffmpeg + ffprobe are installed; if not, print install
instructions and stop").

Run:  python scripts/doctor.py
"""

from __future__ import annotations

import importlib.util
import shutil
import sys

OK = "✓"
BAD = "✗"

FFMPEG_INSTALL = """\
ffmpeg / ffprobe were not found on PATH. Assembly (Prompt 6) and frame seeding
(Prompt 5) require them. Install:

  macOS (Homebrew):   brew install ffmpeg
  Debian / Ubuntu:    sudo apt-get update && sudo apt-get install -y ffmpeg
  Windows (winget):   winget install Gyan.FFmpeg
  conda:              conda install -c conda-forge ffmpeg

Then re-run:  python scripts/doctor.py
"""

# (import name, why it matters). pip name may differ — shown for guidance only.
PY_DEPS = [
    ("mcp", "MCP server SDK (gen_server)"),
    ("fal_client", "default aggregator client (fal.ai)"),
    ("google.genai", "optional Veo-direct path"),
    ("dotenv", "loads .env"),
    ("pydantic", "data contracts"),
    ("ffmpeg", "ffmpeg-python bindings"),
    ("PIL", "reference-image validation"),
]


def _module_available(dotted: str) -> bool:
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ValueError):
        return False


def main() -> int:
    problems: list[str] = []
    print("nimbo-orchestrator :: environment doctor\n" + "─" * 44)

    # Python version
    py_ok = sys.version_info >= (3, 11)
    print(f"  {OK if py_ok else BAD} Python {sys.version.split()[0]} (need >= 3.11)")
    if not py_ok:
        problems.append("Python >= 3.11 required.")

    # ffmpeg / ffprobe — hard requirements
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    print(f"  {OK if ffmpeg_path else BAD} ffmpeg  {ffmpeg_path or '(not found)'}")
    print(f"  {OK if ffprobe_path else BAD} ffprobe {ffprobe_path or '(not found)'}")
    if not (ffmpeg_path and ffprobe_path):
        problems.append("ffmpeg/ffprobe missing.")

    # Python deps (informational: missing means run `uv sync` / pip install)
    print("  ── python deps ──")
    missing_deps = []
    for mod, why in PY_DEPS:
        present = _module_available(mod)
        print(f"    {OK if present else BAD} {mod:<14} {why}")
        if not present:
            missing_deps.append(mod)

    print("─" * 44)

    if not (ffmpeg_path and ffprobe_path):
        print("\n" + FFMPEG_INSTALL)

    if missing_deps:
        print(
            "\nMissing Python deps: "
            + ", ".join(missing_deps)
            + "\nInstall with:  uv sync   (or)   pip install -e ."
        )

    if problems:
        print("\nDoctor found blocking issues. Resolve the above and re-run.")
        return 1

    print("\nAll good — environment is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
