"""Song step: lyrics + section/timing map, or bring-your-own-MP3 ingestion (Prompt 2).

Two paths:

  A) generate_lyrics(topic, ...) — Claude Code authors the lyrics DIRECTLY here, with NO
     external LLM call. It's a deterministic, template-driven toddler-song generator:
     gentle, repetitive, one core concept reinforced. Produces a Lyrics object whose
     section timings sum exactly to the target duration, plus a music-gen prompt.

  B) ingest_mp3(mp3_path, ...) — bring your own audio. Reads true duration via ffprobe
     and fits sections to it (from supplied lyric text, or as generic markers).

Optional generate_song() (behind a flag) sends the music-gen prompt to a music API if a
key is configured. Supported providers: Gemini/Lyria 3 (default, reuses GEMINI_API_KEY),
ElevenLabs, and Suno. The pipeline MUST work with a manually-supplied MP3 if no music API
is set up — that's the supported default.

CLI:
  python -m src.lyrics --project blue-song --topic "learning the color blue"
  python -m src.lyrics --project blue-song --mp3 /path/to/song.mp3
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from .models import Lyrics, LyricSection, save_json_model

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

DEFAULT_TARGET_DURATION_S = 180.0
MUSIC_PROMPT_FILENAME = "music_gen_prompt.txt"
LYRICS_FILENAME = "lyrics.json"
SONG_FILENAME = "song.mp3"


class LyricsError(RuntimeError):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Concept extraction + category detection
# ──────────────────────────────────────────────────────────────────────────────

# Words stripped when pulling the core concept out of a topic phrase.
_FILLER = {
    "learning", "learn", "the", "a", "an", "about", "song", "of", "color", "colour",
    "number", "numbers", "shape", "shapes", "animal", "animals", "lets", "let's", "for",
    "kids", "toddler", "toddlers", "my", "to", "and", "with", "all", "is", "are",
    "counting", "count",
}

ANIMAL_SOUNDS = {
    "cat": "meow", "kitten": "meow", "duck": "quack", "duckling": "quack",
    "bunny": "hop, hop", "rabbit": "hop, hop", "dog": "woof", "puppy": "woof",
    "cow": "moo", "sheep": "baa", "lamb": "baa", "frog": "ribbit",
    "bird": "tweet", "chick": "cheep", "owl": "hoot", "bee": "buzz",
}
_DEFAULT_ANIMAL_SOUND = "la, la"

_NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "zero",
}


def _extract_concept(topic: str) -> str:
    """Pull the salient concept out of a topic phrase.

    'learning the color blue' -> 'blue'; 'the number three' -> 'three';
    'farm animals' -> 'farm' (best-effort). Falls back to the cleaned topic.
    """
    tokens = re.findall(r"[a-zA-Z']+", topic.lower())
    kept = [t for t in tokens if t not in _FILLER]
    if kept:
        return " ".join(kept)
    # Everything was filler (e.g. "the color") — fall back to the raw topic.
    cleaned = " ".join(tokens)
    return cleaned or topic.strip().lower()


def _detect_category(topic: str, concept: str) -> str:
    t = topic.lower()
    if "colour" in t or "color" in t:
        return "color"
    if "animal" in t or concept in ANIMAL_SOUNDS:
        return "animal"
    if "number" in t or "count" in t or concept in _NUMBER_WORDS:
        return "number"
    if "shape" in t:
        return "shape"
    return "general"


# ──────────────────────────────────────────────────────────────────────────────
# Lyric templates
# ──────────────────────────────────────────────────────────────────────────────
# {C} = concept (e.g. "blue"); {S} = animal sound (animal songs only).
# Tone follows the bible: warm, calm, repetitive; the key word recurs often.

_TEMPLATES: dict[str, dict[str, str]] = {
    "color": {
        "intro": (
            "Hello, hello, it's Nimbo here,\n"
            "a gentle song for you, my dear.\n"
            "Today we learn the color {C},\n"
            "so cozy, calm, and soft and true."
        ),
        "verse1": (
            "Look up high and look around,\n"
            "{C} is here, it can be found.\n"
            "The quiet sky, so wide and {C},\n"
            "softly shining over you."
        ),
        "chorus": (
            "{C}, {C}, the color {C},\n"
            "sing it slow, the whole day through.\n"
            "{C}, {C}, can you say {C}?\n"
            "Nimbo's cloud is glowing {C}."
        ),
        "verse2": (
            "{C} like the gentle sea,\n"
            "{C} as calm as calm can be.\n"
            "close your eyes and picture {C},\n"
            "peaceful {C} for me and you."
        ),
        "bridge": (
            "round and soft the color stays,\n"
            "{C}, {C}, in gentle ways."
        ),
        "outro": (
            "now we know the color {C},\n"
            "thank you, friends, and Nimbo too.\n"
            "wave goodbye, the song is through,\n"
            "sweet and calm, the color {C}."
        ),
    },
    "animal": {
        "intro": (
            "Hello, hello, it's Nimbo here,\n"
            "a gentle song to bring you cheer.\n"
            "Nimbo's cloud will softly show\n"
            "a little {C} that we all know."
        ),
        "verse1": (
            "the {C} is gentle, the {C} is sweet,\n"
            "soft and slow on tippy feet.\n"
            "what does the {C} like to say?\n"
            "\"{S}\", so softly, all the day."
        ),
        "chorus": (
            "{C}, {C}, the gentle {C},\n"
            "\"{S}\", sings the {C} for you.\n"
            "{C}, {C}, can you say {C}?\n"
            "Nimbo's cloud becomes one too."
        ),
        "verse2": (
            "the {C} will rest and the {C} will play,\n"
            "calm and cozy through the day.\n"
            "giving cuddles, soft and slow,\n"
            "the sweetest {C} that we know."
        ),
        "bridge": (
            "round and soft the cloud will stay,\n"
            "a little {C} to end the day."
        ),
        "outro": (
            "now we know the gentle {C},\n"
            "\"{S}\", it says, to me and you.\n"
            "wave goodbye, the song is through,\n"
            "sweet and calm, the {C} too."
        ),
    },
    "general": {
        "intro": (
            "Hello, hello, it's Nimbo here,\n"
            "a gentle song for you, my dear.\n"
            "Today we sing about {C},\n"
            "so cozy, calm, and soft and true."
        ),
        "verse1": (
            "take it slow and look around,\n"
            "{C} is something to be found.\n"
            "gentle, quiet, soft, and clear,\n"
            "{C} is something we hold dear."
        ),
        "chorus": (
            "{C}, {C}, we sing of {C},\n"
            "soft and slow the whole day through.\n"
            "{C}, {C}, can you say {C}?\n"
            "Nimbo's here to sing with you."
        ),
        "verse2": (
            "{C} is calm and {C} is kind,\n"
            "softly resting in your mind.\n"
            "close your eyes and think of {C},\n"
            "peaceful feelings, me and you."
        ),
        "bridge": (
            "round and soft the song will stay,\n"
            "{C}, {C}, in gentle ways."
        ),
        "outro": (
            "now we've sung about {C},\n"
            "thank you, friends, and Nimbo too.\n"
            "wave goodbye, the song is through,\n"
            "soft and calm, of {C}."
        ),
    },
}

# (section name, timing weight, template role). Chorus-heavy, repetitive on purpose.
_STRUCTURE: list[tuple[str, float, str]] = [
    ("intro", 0.75, "intro"),
    ("verse_1", 1.30, "verse1"),
    ("chorus_1", 1.00, "chorus"),
    ("verse_2", 1.30, "verse2"),
    ("chorus_2", 1.00, "chorus"),
    ("bridge", 0.85, "bridge"),
    ("chorus_3", 1.00, "chorus"),
    ("outro", 0.80, "outro"),
]


def _cap_lines(text: str) -> str:
    """Capitalize the first alphabetic character of each line (leaves the rest intact)."""
    out_lines = []
    for line in text.split("\n"):
        for i, ch in enumerate(line):
            if ch.isalpha():
                line = line[:i] + ch.upper() + line[i + 1 :]
                break
        out_lines.append(line)
    return "\n".join(out_lines)


def _fill_template(role: str, category: str, concept: str) -> str:
    tmpl = _TEMPLATES.get(category, {}).get(role) or _TEMPLATES["general"][role]
    text = tmpl.replace("{C}", concept)
    if "{S}" in text:
        sound = ANIMAL_SOUNDS.get(concept, _DEFAULT_ANIMAL_SOUND)
        text = text.replace("{S}", sound)
    return _cap_lines(text)


def _distribute(target_s: float, weights: list[float]) -> list[tuple[float, float]]:
    """Turn weights into contiguous (start, end) spans covering [0, target] with no gaps.

    Boundaries are rounded to 0.1s; the final boundary is pinned to the exact target so the
    sections always sum to the full duration.
    """
    total = sum(weights)
    bounds = [0.0]
    acc = 0.0
    for w in weights:
        acc += w
        bounds.append(round(acc / total * target_s, 1))
    bounds[-1] = round(float(target_s), 1)
    # Enforce monotonic non-decreasing in case rounding nudged a boundary backwards.
    for i in range(1, len(bounds)):
        if bounds[i] < bounds[i - 1]:
            bounds[i] = bounds[i - 1]
    return list(zip(bounds[:-1], bounds[1:]))


# ──────────────────────────────────────────────────────────────────────────────
# Path A: generate lyrics from a topic (no external LLM)
# ──────────────────────────────────────────────────────────────────────────────


def generate_lyrics(topic: str, target_duration_s: float = DEFAULT_TARGET_DURATION_S) -> Lyrics:
    """Author gentle, repetitive toddler-song lyrics for `topic`, timed to `target_duration_s`.

    Deterministic and offline — no model call. Section timings sum exactly to the target.
    """
    if not topic or not topic.strip():
        raise LyricsError("topic must be a non-empty string")
    if target_duration_s <= 0:
        raise LyricsError("target_duration_s must be > 0")

    concept = _extract_concept(topic)
    category = _detect_category(topic, concept)
    weights = [w for _, w, _ in _STRUCTURE]
    spans = _distribute(target_duration_s, weights)

    sections: list[LyricSection] = []
    for (name, _w, role), (start_s, end_s) in zip(_STRUCTURE, spans):
        sections.append(
            LyricSection(
                name=name,
                start_s=start_s,
                end_s=end_s,
                text=_fill_template(role, category, concept),
            )
        )
    return Lyrics(sections=sections)


def build_music_gen_prompt(
    topic: str,
    target_duration_s: float = DEFAULT_TARGET_DURATION_S,
    lyrics: Optional[Lyrics] = None,
) -> str:
    """Produce the genre/tempo/mood brief a music generator (or you) can use to make the MP3."""
    concept = _extract_concept(topic)
    mm, ss = divmod(int(round(target_duration_s)), 60)
    header = (
        "Gentle, melodic children's lullaby-pop for toddlers.\n"
        "Mood: warm, calm, curious, soothing — never frantic or shouty.\n"
        "Tempo: slow and steady, about 70-80 BPM, in a major key.\n"
        "Instrumentation: soft acoustic guitar, gentle piano, light glockenspiel/bells, "
        "warm pads, very light hand percussion (no harsh drums).\n"
        "Vocals: a single warm, friendly voice; simple, repetitive melody a young child "
        "can sing along to.\n"
        f"Length: about {int(round(target_duration_s))} seconds (~{mm}:{ss:02d}).\n"
        f"Theme: {topic} — repeat the key word \"{concept}\" often as a memory aid.\n"
        "Keep it uncluttered and premium: one or two motifs, lots of space, soft dynamics."
    )
    if lyrics is None:
        return header + "\n"

    body = ["", "--- Lyrics (section / timing) ---"]
    for s in lyrics.sections:
        body.append(f"\n[{s.name} {s.start_s:.0f}-{s.end_s:.0f}s]")
        body.append(s.text)
    return header + "\n" + "\n".join(body) + "\n"


# ──────────────────────────────────────────────────────────────────────────────
# Path B: ingest a supplied MP3
# ──────────────────────────────────────────────────────────────────────────────


def probe_duration_s(path: str | os.PathLike) -> float:
    """Read true media duration (seconds) via ffprobe (ffmpeg-python)."""
    p = Path(path)
    if not p.exists():
        raise LyricsError(f"audio file not found: {p}")
    import ffmpeg  # local import so the module loads even if ffmpeg-python is absent

    try:
        info = ffmpeg.probe(str(p))
    except FileNotFoundError as exc:  # ffprobe binary not on PATH
        raise LyricsError(
            "ffprobe not found on PATH. Install ffmpeg (see scripts/doctor.py)."
        ) from exc
    except ffmpeg.Error as exc:  # malformed/unreadable media
        raise LyricsError(f"ffprobe failed to read {p}: {exc}") from exc

    dur = info.get("format", {}).get("duration")
    if dur is None:
        for stream in info.get("streams", []):
            if stream.get("duration"):
                dur = stream["duration"]
                break
    if dur is None:
        raise LyricsError(f"could not determine duration of {p}")
    return float(dur)


def _stanzas(text: str) -> list[str]:
    """Split lyric text into stanzas on blank lines."""
    blocks = re.split(r"\n\s*\n", text.strip())
    return [b.strip() for b in blocks if b.strip()]


def ingest_mp3(
    mp3_path: str | os.PathLike,
    lyric_text: Optional[str] = None,
    target_duration_s: Optional[float] = None,  # unused for fitting; kept for symmetry/warnings
) -> Lyrics:
    """Build a Lyrics map fitted to a real MP3's duration.

    If `lyric_text` is provided, its stanzas become sections fitted to the true duration.
    Otherwise generic ~20s section markers are created so the planner still has boundaries.
    """
    duration = probe_duration_s(mp3_path)

    if lyric_text and lyric_text.strip():
        stanzas = _stanzas(lyric_text)
        weights = [1.0] * len(stanzas)
        spans = _distribute(duration, weights)
        sections = [
            LyricSection(name=f"section_{i + 1:02d}", start_s=s, end_s=e, text=txt)
            for i, (txt, (s, e)) in enumerate(zip(stanzas, spans))
        ]
    else:
        n = max(1, round(duration / 20.0))
        spans = _distribute(duration, [1.0] * n)
        sections = [
            LyricSection(name=f"section_{i + 1:02d}", start_s=s, end_s=e, text="")
            for i, (s, e) in enumerate(spans)
        ]
    return Lyrics(sections=sections)


# ──────────────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────────────


def validate_song(
    lyrics: Lyrics, song_path: str | os.PathLike, tolerance_s: float = 2.0
) -> list[str]:
    """Return a list of human-readable warnings (empty = all good).

    Checks: song.mp3 exists; its true duration matches the lyrics total within tolerance.
    """
    warnings: list[str] = []
    p = Path(song_path)
    if not p.exists():
        warnings.append(f"song file not found: {p}")
        return warnings

    try:
        actual = probe_duration_s(p)
    except LyricsError as exc:
        warnings.append(f"could not probe {p}: {exc}")
        return warnings

    expected = lyrics.total_duration_s
    if abs(actual - expected) > tolerance_s:
        warnings.append(
            f"duration mismatch: song is {actual:.1f}s but lyrics total {expected:.1f}s "
            f"(>{tolerance_s:.0f}s off)"
        )
    return warnings


# ──────────────────────────────────────────────────────────────────────────────
# Optional: generate the song via a music API (behind a flag)
# ──────────────────────────────────────────────────────────────────────────────


_PROVIDER_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "suno": "SUNO_API_KEY",
}


def _resolve_music_provider(provider: str) -> str:
    """Pick a concrete provider. 'auto' prefers Gemini/Lyria, then ElevenLabs, then Suno."""
    provider = (provider or "auto").lower()
    if provider == "auto":
        for name in ("gemini", "elevenlabs", "suno"):  # preference order
            if os.environ.get(_PROVIDER_KEY_ENV[name]):
                return name
        raise LyricsError(
            "No music API key set (GEMINI_API_KEY, ELEVENLABS_API_KEY or SUNO_API_KEY). "
            "Either configure one, or drop a song.mp3 into the project and skip generation."
        )
    if provider not in _PROVIDER_KEY_ENV:
        raise LyricsError(
            f"unknown music provider '{provider}' (use gemini|elevenlabs|suno|auto)"
        )
    key_env = _PROVIDER_KEY_ENV[provider]
    if not os.environ.get(key_env):
        raise LyricsError(f"{key_env} is not set — required for provider='{provider}'.")
    return provider


def generate_song(
    music_prompt: str,
    output_mp3: str | os.PathLike,
    *,
    provider: str = "auto",
    duration_s: float = DEFAULT_TARGET_DURATION_S,
    instrumental: bool = False,
) -> str:
    """OPTIONAL: render an MP3 from the music-gen prompt via a music API.

    Targets the documented provider contracts:
      - Gemini/Lyria 3 (default): google-genai -> client.models.generate_content(
        model="lyria-3-pro-preview", contents=prompt); audio in part.inline_data.data.
        Reuses GEMINI_API_KEY (the same key used for the Veo-direct path).
      - ElevenLabs: official `elevenlabs` SDK -> client.music.compose(...)
      - Suno (sunoapi.org): POST /api/v1/generate then poll for the audio URL.

    The pipeline does NOT depend on this — a manually supplied song.mp3 is the default path.
    Raises LyricsError (never silently fails) so the caller can fall back to manual audio.
    """
    chosen = _resolve_music_provider(provider)
    out = Path(output_mp3)
    out.parent.mkdir(parents=True, exist_ok=True)

    if chosen == "gemini":
        # Lyria 3 via the Gemini API. Pro handles full-length (~3 min) tracks with vocals
        # and timed lyrics; Clip is a fast 30s preview. Pick by requested duration.
        from google import genai

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        model = "lyria-3-pro-preview" if duration_s > 30 else "lyria-3-clip-preview"
        contents = music_prompt
        if instrumental:
            contents = "Instrumental only, no vocals.\n\n" + music_prompt
        response = client.models.generate_content(model=model, contents=contents)

        parts = getattr(response, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline is not None else None
            if data:
                out.write_bytes(data)
                return str(out)
        raise LyricsError(
            f"Gemini/Lyria ({model}) returned no audio data. "
            f"Response had {len(parts)} part(s)."
        )

    if chosen == "elevenlabs":
        try:
            from elevenlabs.client import ElevenLabs
        except ImportError as exc:
            raise LyricsError(
                "elevenlabs SDK not installed. `pip install elevenlabs` "
                "(or `uv pip install -e '.[music]'`), or supply a song.mp3 manually."
            ) from exc
        client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
        audio = client.music.compose(
            prompt=music_prompt,
            music_length_ms=int(round(duration_s * 1000)),
            # output_format codec_samplerate_bitrate per ElevenLabs docs
            output_format="mp3_44100_128",
        )
        # The SDK returns either bytes or an iterator of byte chunks.
        with out.open("wb") as f:
            if isinstance(audio, (bytes, bytearray)):
                f.write(audio)
            else:
                for chunk in audio:
                    f.write(chunk)
        return str(out)

    # Suno (third-party aggregator at sunoapi.org)
    import time

    import httpx

    base = "https://api.sunoapi.org/api/v1"
    headers = {"Authorization": f"Bearer {os.environ['SUNO_API_KEY']}"}
    payload = {
        "prompt": music_prompt,
        "customMode": False,
        "instrumental": bool(instrumental),
        "model": "V4_5",
    }
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(f"{base}/generate", json=payload, headers=headers)
        resp.raise_for_status()
        task_id = resp.json().get("data", {}).get("taskId") or resp.json().get("taskId")
        if not task_id:
            raise LyricsError(f"Suno generate returned no taskId: {resp.text[:200]}")

        # Poll for completion (audio ready in ~2-3 min per docs).
        audio_url = None
        for _ in range(60):
            time.sleep(5)
            info = client.get(
                f"{base}/generate/record-info", params={"taskId": task_id}, headers=headers
            )
            info.raise_for_status()
            data = info.json().get("data", {})
            items = data.get("response", {}).get("sunoData") or data.get("data") or []
            for item in items:
                if item.get("audioUrl") or item.get("audio_url"):
                    audio_url = item.get("audioUrl") or item.get("audio_url")
                    break
            if audio_url:
                break
        if not audio_url:
            raise LyricsError("Suno generation timed out before an audio URL was ready.")

        dl = client.get(audio_url)
        dl.raise_for_status()
        out.write_bytes(dl.content)
    return str(out)


# ──────────────────────────────────────────────────────────────────────────────
# Artifact writing
# ──────────────────────────────────────────────────────────────────────────────


def write_lyrics_artifacts(
    lyrics: Lyrics, music_prompt: str, project_dir: str | os.PathLike
) -> tuple[Path, Path]:
    """Write lyrics.json + music_gen_prompt.txt into the project dir. Returns both paths."""
    pdir = Path(project_dir)
    pdir.mkdir(parents=True, exist_ok=True)
    lyrics_path = pdir / LYRICS_FILENAME
    prompt_path = pdir / MUSIC_PROMPT_FILENAME
    save_json_model(lyrics, lyrics_path)
    prompt_path.write_text(music_prompt, encoding="utf-8")
    return lyrics_path, prompt_path


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def _print_sections(lyrics: Lyrics) -> None:
    print(f"\n{'section':<10}  {'start':>6}  {'end':>6}  text")
    print("-" * 60)
    for s in lyrics.sections:
        first_line = s.text.split("\n", 1)[0] if s.text else ""
        print(f"{s.name:<10}  {s.start_s:>6.1f}  {s.end_s:>6.1f}  {first_line}")
    print(f"\ntotal: {lyrics.total_duration_s:.1f}s across {len(lyrics.sections)} sections\n")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate lyrics from a topic, or ingest an MP3.")
    p.add_argument("--project", required=True, help="Project name under the projects root.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--topic", help="Topic to write a toddler song about (path A).")
    src.add_argument("--mp3", help="Path to a supplied song.mp3 (path B).")
    p.add_argument(
        "--duration-s", type=float, default=DEFAULT_TARGET_DURATION_S, help="Target length (s)."
    )
    p.add_argument("--lyric-text-file", help="(with --mp3) lyric text to fit to the audio.")
    p.add_argument(
        "--generate-song",
        action="store_true",
        help="(with --topic) also call a music API to render song.mp3.",
    )
    p.add_argument(
        "--provider", default="auto", help="Music provider: auto|gemini|elevenlabs|suno."
    )
    p.add_argument("--instrumental", action="store_true", help="(music API) instrumental only.")
    p.add_argument(
        "--projects-root",
        default=str(_PROJECT_ROOT / "projects"),
        help="Root dir that holds project folders (default: ./projects).",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    project_dir = Path(args.projects_root) / args.project

    if args.topic:
        lyrics = generate_lyrics(args.topic, args.duration_s)
        music_prompt = build_music_gen_prompt(args.topic, args.duration_s, lyrics)
        lyrics_path, prompt_path = write_lyrics_artifacts(lyrics, music_prompt, project_dir)
        print(f"Topic: {args.topic!r}  (concept={_extract_concept(args.topic)!r}, "
              f"category={_detect_category(args.topic, _extract_concept(args.topic))})")
        _print_sections(lyrics)
        print(f"wrote {lyrics_path}")
        print(f"wrote {prompt_path}")

        song_path = project_dir / SONG_FILENAME
        if args.generate_song:
            try:
                generate_song(
                    music_prompt,
                    song_path,
                    provider=args.provider,
                    duration_s=args.duration_s,
                    instrumental=args.instrumental,
                )
                print(f"wrote {song_path}")
            except LyricsError as exc:
                print(f"! music generation skipped: {exc}", file=sys.stderr)
                print("  (drop a song.mp3 into the project to supply audio manually)")
        # Validate only if a song is actually present.
        if song_path.exists():
            for w in validate_song(lyrics, song_path):
                print(f"! {w}", file=sys.stderr)
        return 0

    # Path B: ingest an MP3
    lyric_text = None
    if args.lyric_text_file:
        lyric_text = Path(args.lyric_text_file).read_text(encoding="utf-8")
    lyrics = ingest_mp3(args.mp3, lyric_text=lyric_text, target_duration_s=args.duration_s)
    # Music prompt is still useful as documentation of intended vibe.
    music_prompt = build_music_gen_prompt(args.project, args.duration_s, lyrics)
    lyrics_path, prompt_path = write_lyrics_artifacts(lyrics, music_prompt, project_dir)
    _print_sections(lyrics)
    print(f"wrote {lyrics_path}")
    print(f"wrote {prompt_path}")
    for w in validate_song(lyrics, args.mp3):
        print(f"! {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
