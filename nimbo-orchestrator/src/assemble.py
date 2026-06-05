"""Assembly (Prompt 6): ffmpeg concat + mux MP3 + optional captions -> final.mp4.

  - Concatenate the shot clips in manifest order into ONE silent video, re-encoding every
    clip to a uniform size / fps / pixel format (via a single concat-filter graph) so there
    are no concat artifacts from mismatched inputs.
  - Mux in projects/<p>/song.mp3 as the ONLY audio. Any audio the model produced on the
    clips is dropped (the concat filter takes video only). The output is trimmed/padded to
    the song's exact length: the video is held on its last frame (tpad clone) and both
    streams are cut to the song duration with -t, so final.mp4 == song length.
  - With captions on, burn the lyrics section text as subtitles (large, rounded, kid-friendly
    font, lower third) generated as an ASS file and applied with the `ass` filter.

The actual ffmpeg execution requires the ffmpeg binary. The command/timeline/caption logic
is fully unit-tested; build_ffmpeg_cmd / build_ass produce the exact argv + ASS without
running anything, and assemble(dry_run=True) prints the command without spending a frame.

CLI:
  python -m src.assemble --project blue-song
  python -m src.assemble --project blue-song --captions
  python -m src.assemble --project blue-song --captions --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import lyrics as lyrics_mod
from .models import Lyrics, Manifest, ShotStatus, load_json_model, load_manifest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

FINAL_FILENAME = "final.mp4"
SONG_FILENAME = "song.mp3"
CAPTIONS_FILENAME = "captions.ass"
DEFAULT_FPS = 24
# Letterbox/pad color — body cream from the bible palette (on-brand bars if any).
DEFAULT_PAD_COLOR = "0xF4EBD8"
DEFAULT_CAPTION_FONT = "Comic Sans MS"  # rounded + kid-friendly; falls back via fontconfig


class AssembleError(RuntimeError):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Geometry
# ──────────────────────────────────────────────────────────────────────────────

# Short-edge pixels per resolution label.
_RES_SHORT_EDGE = {"480p": 480, "540p": 540, "720p": 720, "1080p": 1080, "1440p": 1440, "4k": 2160}


def resolution_to_dims(resolution: str, aspect_ratio: str) -> tuple[int, int]:
    """Map ('720p', '16:9') -> (1280, 720). Width/height are forced even (h.264 needs it)."""
    short = _RES_SHORT_EDGE.get(resolution.lower())
    if short is None:
        raise AssembleError(
            f"unknown resolution '{resolution}'. Known: {sorted(_RES_SHORT_EDGE)}"
        )
    try:
        aw, ah = (int(x) for x in aspect_ratio.split(":"))
    except ValueError as exc:
        raise AssembleError(f"bad aspect_ratio '{aspect_ratio}' (want e.g. '16:9')") from exc

    if aw >= ah:
        # landscape / square: height is the short edge
        height = short
        width = round(short * aw / ah)
    else:
        # portrait: width is the short edge
        width = short
        height = round(short * ah / aw)
    # force even
    width += width % 2
    height += height % 2
    return width, height


# ──────────────────────────────────────────────────────────────────────────────
# Clip collection
# ──────────────────────────────────────────────────────────────────────────────


def collect_clips(project_dir: Path, manifest: Manifest) -> list[Path]:
    """Return the clip files in manifest order. Errors if any shot has no rendered clip."""
    clips: list[Path] = []
    missing: list[str] = []
    for shot in manifest.shots:
        if not shot.output_path:
            missing.append(f"{shot.id} (never rendered)")
            continue
        p = project_dir / shot.output_path
        if not p.exists():
            missing.append(f"{shot.id} ({shot.output_path} missing on disk)")
            continue
        clips.append(p)
    if missing:
        raise AssembleError(
            "cannot assemble — these shots have no clip yet:\n  - "
            + "\n  - ".join(missing)
            + "\nRun the render loop (Prompt 5) until every shot is drafted/done."
        )
    if not clips:
        raise AssembleError("no shots in the manifest to assemble.")
    return clips


# ──────────────────────────────────────────────────────────────────────────────
# Captions (ASS)
# ──────────────────────────────────────────────────────────────────────────────


def _ass_timestamp(seconds: float) -> str:
    """Seconds -> ASS H:MM:SS.cs (centiseconds)."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs == 100:  # rounding spillover
        cs = 0
        s += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass(
    lyrics: Lyrics,
    width: int,
    height: int,
    *,
    font: str = DEFAULT_CAPTION_FONT,
    fontsize: Optional[int] = None,
) -> str:
    """Build an ASS subtitle document from the lyric sections, timed to each section.

    Big rounded font, white fill with a soft dark outline, centered in the lower third
    (Alignment=2 bottom-center + a generous bottom margin).
    """
    if fontsize is None:
        fontsize = max(28, round(height * 0.075))  # ~7.5% of frame height
    margin_v = round(height * 0.12)  # sit in the lower third

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Colours are &HAABBGGRR. White text, dark-brown outline (Eyes Ink #322B22).
        f"Style: Nimbo,{font},{fontsize},&H00FFFFFF,&H000000FF,&H00222B32,&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,4,1,2,60,60,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = []
    for sec in lyrics.sections:
        if not sec.text.strip():
            continue
        # ASS uses \N for hard line breaks; collapse whitespace per line.
        text = "\\N".join(line.strip() for line in sec.text.splitlines() if line.strip())
        text = text.replace("\n", "\\N")
        lines.append(
            f"Dialogue: 0,{_ass_timestamp(sec.start_s)},{_ass_timestamp(sec.end_s)},"
            f"Nimbo,,0,0,0,,{text}"
        )
    return header + "\n".join(lines) + "\n"


# ──────────────────────────────────────────────────────────────────────────────
# ffmpeg command construction
# ──────────────────────────────────────────────────────────────────────────────


def build_filter_complex(
    n_clips: int, width: int, height: int, fps: int, song_duration: float,
    *, pad_color: str = DEFAULT_PAD_COLOR, ass_name: Optional[str] = None,
) -> str:
    """Build the -filter_complex graph: scale+pad+fps each clip, concat, optional captions,
    then hold the last frame (tpad) so -t can trim to the exact song length."""
    parts: list[str] = []
    for i in range(n_clips):
        parts.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={pad_color},"
            f"setsar=1,fps={fps},format=yuv420p[v{i}]"
        )
    concat_inputs = "".join(f"[v{i}]" for i in range(n_clips))
    parts.append(f"{concat_inputs}concat=n={n_clips}:v=1:a=0[vc]")

    last = "vc"
    if ass_name:
        parts.append(f"[vc]ass={ass_name}[vs]")
        last = "vs"

    # Extend (clone last frame) by the full song duration, then -t trims to exact length.
    parts.append(f"[{last}]tpad=stop_mode=clone:stop_duration={song_duration:.3f}[vout]")
    return ";".join(parts)


def build_ffmpeg_cmd(
    clips: list[Path],
    song_path: Path,
    out_path: Path,
    *,
    width: int,
    height: int,
    fps: int,
    song_duration: float,
    ass_name: Optional[str] = None,
    pad_color: str = DEFAULT_PAD_COLOR,
) -> list[str]:
    """Construct the full ffmpeg argv. Song is the last input; its audio is the ONLY audio."""
    cmd: list[str] = ["ffmpeg", "-y"]
    for clip in clips:
        cmd += ["-i", str(clip)]
    cmd += ["-i", str(song_path)]  # input index == len(clips)
    audio_idx = len(clips)

    fc = build_filter_complex(
        len(clips), width, height, fps, song_duration, pad_color=pad_color, ass_name=ass_name
    )
    cmd += [
        "-filter_complex", fc,
        "-map", "[vout]",
        "-map", f"{audio_idx}:a",   # only the MP3
        "-t", f"{song_duration:.3f}",
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    return cmd


# ──────────────────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────────────────


def assemble(
    project_dir: Path,
    *,
    captions: bool = False,
    fps: int = DEFAULT_FPS,
    font: str = DEFAULT_CAPTION_FONT,
    pad_color: str = DEFAULT_PAD_COLOR,
    dry_run: bool = False,
) -> Path:
    """Assemble final.mp4 from the rendered shots + song.mp3."""
    manifest_path = project_dir / "manifest.json"
    if not manifest_path.exists():
        raise AssembleError(f"{manifest_path} not found — run the planner/render first.")
    manifest = load_manifest(manifest_path)

    song_path = project_dir / (manifest.project.song_path or SONG_FILENAME)
    if not song_path.exists():
        raise AssembleError(
            f"song not found: {song_path}. Drop your MP3 there (or run the song step), since "
            f"the user's MP3 is the only audio."
        )

    clips = collect_clips(project_dir, manifest)
    width, height = resolution_to_dims(manifest.project.resolution, manifest.project.aspect_ratio)

    # Real runs need the song's true duration (ffprobe). For a dry-run preview we fall back to
    # the manifest's target duration if ffprobe isn't available, so the command can be shown.
    try:
        song_duration = lyrics_mod.probe_duration_s(song_path)
    except lyrics_mod.LyricsError as exc:
        if not dry_run:
            raise AssembleError(str(exc)) from exc
        song_duration = manifest.project.target_duration_s
        print(f"  (ffprobe unavailable — using target {song_duration:.1f}s for this preview)")

    ass_name: Optional[str] = None
    if captions:
        lyrics_path = project_dir / manifest.project.lyrics_path
        if not lyrics_path.exists():
            raise AssembleError(f"--captions requires {lyrics_path} (run the song step).")
        lyrics: Lyrics = load_json_model(Lyrics, lyrics_path)  # type: ignore[assignment]
        ass_text = build_ass(lyrics, width, height, font=font)
        (project_dir / CAPTIONS_FILENAME).write_text(ass_text, encoding="utf-8")
        ass_name = CAPTIONS_FILENAME  # referenced relative to cwd (= project_dir) when running

    out_path = project_dir / manifest.project.final_path
    cmd = build_ffmpeg_cmd(
        clips, song_path, out_path,
        width=width, height=height, fps=fps, song_duration=song_duration,
        ass_name=ass_name, pad_color=pad_color,
    )

    print(f"Assembling {len(clips)} clips -> {out_path}")
    print(f"  {width}x{height} @ {fps}fps   song {song_duration:.2f}s   captions={captions}")
    if dry_run:
        print("\n(dry-run) ffmpeg command:")
        print("  " + " ".join(cmd))
        return out_path

    if shutil.which("ffmpeg") is None:
        raise AssembleError("ffmpeg not found on PATH. Install it (see scripts/doctor.py).")

    # Run with cwd=project_dir so the ASS filename needs no path escaping in the filtergraph.
    proc = subprocess.run(cmd, cwd=str(project_dir), capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssembleError(f"ffmpeg failed:\n{proc.stderr[-1500:]}")

    # Verify the output duration matches the song (±0.5s).
    final_dur = lyrics_mod.probe_duration_s(out_path)
    if abs(final_dur - song_duration) > 0.5:
        print(
            f"  ! warning: final.mp4 is {final_dur:.2f}s but song is {song_duration:.2f}s "
            f"(>0.5s off).",
            file=sys.stderr,
        )
    print(f"  ✓ wrote {out_path} ({final_dur:.2f}s)")
    return out_path


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Assemble final.mp4 from rendered shots + MP3.")
    p.add_argument("--project", required=True)
    p.add_argument("--projects-root", default=str(_PROJECT_ROOT / "projects"))
    p.add_argument("--captions", action="store_true", help="Burn in lyric captions.")
    p.add_argument("--fps", type=int, default=DEFAULT_FPS)
    p.add_argument("--font", default=DEFAULT_CAPTION_FONT, help="Caption font family.")
    p.add_argument("--dry-run", action="store_true", help="Print the ffmpeg command; don't run.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    project_dir = Path(args.projects_root) / args.project
    try:
        assemble(
            project_dir,
            captions=args.captions,
            fps=args.fps,
            font=args.font,
            dry_run=args.dry_run,
        )
    except AssembleError as exc:
        print(f"assemble error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
