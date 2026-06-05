"""Tests for the song step (Prompt 2).

Validated without ffmpeg or any music-API key:
  - generate_lyrics: timings cover the full duration with no gaps; concept repeats;
    category detection; animal sound injection
  - build_music_gen_prompt: tempo/mood/concept present
  - ingest_mp3: sections fit the (monkeypatched) probed duration, from stanzas or generic
  - validate_song: missing file / duration mismatch / clean
  - generate_song: provider resolution + loud errors without keys (no live calls)
  - CLI: --topic writes lyrics.json + music_gen_prompt.txt
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import lyrics as L
from src.models import Lyrics, LyricSection


# ──────────────────────────────────────────────────────────────────────────────
# generate_lyrics
# ──────────────────────────────────────────────────────────────────────────────


def test_blue_topic_timings_cover_full_duration_no_gaps():
    lyr = L.generate_lyrics("learning the color blue", target_duration_s=180.0)
    assert len(lyr.sections) == len(L._STRUCTURE)

    # First starts at 0, last ends exactly at target, no gaps/overlaps between sections.
    assert lyr.sections[0].start_s == 0.0
    assert lyr.sections[-1].end_s == 180.0
    for a, b in zip(lyr.sections, lyr.sections[1:]):
        assert b.start_s == a.end_s, "sections must be contiguous"
        assert a.end_s > a.start_s

    # total span equals the target
    assert lyr.total_duration_s == 180.0


def test_blue_concept_repeats_often():
    lyr = L.generate_lyrics("learning the color blue")
    all_text = "\n".join(s.text for s in lyr.sections).lower()
    # "blue" is the memory-aid key word — should appear many times.
    assert all_text.count("blue") >= 10
    # Style: gentle/calm words present, never appearance-redescribing Nimbo's body.
    assert "calm" in all_text or "gentle" in all_text


def test_section_names_and_structure():
    lyr = L.generate_lyrics("color blue")
    names = [s.name for s in lyr.sections]
    assert names[0] == "intro"
    assert names[-1] == "outro"
    assert names.count("chorus_1") == 1
    assert sum(1 for n in names if n.startswith("chorus")) == 3


def test_arbitrary_duration_sums_exactly():
    for target in (90.0, 120.5, 200.0):
        lyr = L.generate_lyrics("the color green", target_duration_s=target)
        assert lyr.sections[0].start_s == 0.0
        assert lyr.sections[-1].end_s == round(target, 1)


def test_empty_topic_raises():
    with pytest.raises(L.LyricsError):
        L.generate_lyrics("   ")


def test_zero_duration_raises():
    with pytest.raises(L.LyricsError):
        L.generate_lyrics("blue", target_duration_s=0)


# ──────────────────────────────────────────────────────────────────────────────
# Concept extraction + category detection
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "topic,expected_concept",
    [
        ("learning the color blue", "blue"),
        ("the color red", "red"),
        ("the number three", "three"),
        ("a song about the moon", "moon"),
    ],
)
def test_extract_concept(topic, expected_concept):
    assert L._extract_concept(topic) == expected_concept


@pytest.mark.parametrize(
    "topic,concept,category",
    [
        ("learning the color blue", "blue", "color"),
        ("the animal cat", "cat", "animal"),
        ("a cat song", "cat", "animal"),  # cat is a known animal even without 'animal'
        ("the number three", "three", "number"),
        ("learning shapes", "", "shape"),
        ("kindness", "kindness", "general"),
    ],
)
def test_detect_category(topic, concept, category):
    assert L._detect_category(topic, concept) == category


def test_animal_song_injects_sound():
    lyr = L.generate_lyrics("the animal cat")
    all_text = "\n".join(s.text for s in lyr.sections).lower()
    assert "meow" in all_text
    assert "cat" in all_text


def test_color_song_mentions_cloud_glow():
    """Color songs should reference the cloud glowing the color (the bible's signature)."""
    lyr = L.generate_lyrics("the color blue")
    all_text = "\n".join(s.text for s in lyr.sections).lower()
    assert "cloud" in all_text and "glow" in all_text


# ──────────────────────────────────────────────────────────────────────────────
# music_gen_prompt
# ──────────────────────────────────────────────────────────────────────────────


def test_music_prompt_has_tempo_mood_and_concept():
    lyr = L.generate_lyrics("learning the color blue")
    prompt = L.build_music_gen_prompt("learning the color blue", 180.0, lyr)
    low = prompt.lower()
    assert "bpm" in low
    assert "calm" in low or "soothing" in low
    assert "blue" in low
    # When lyrics are passed, the section/timing map is appended.
    assert "Lyrics (section / timing)" in prompt
    assert "[intro 0-" in prompt


def test_music_prompt_without_lyrics_is_just_the_brief():
    prompt = L.build_music_gen_prompt("color blue", 180.0, None)
    assert "Lyrics (section / timing)" not in prompt
    assert "BPM" in prompt.upper()


# ──────────────────────────────────────────────────────────────────────────────
# ingest_mp3 (ffprobe monkeypatched)
# ──────────────────────────────────────────────────────────────────────────────


def test_ingest_mp3_generic_markers(monkeypatch, tmp_path):
    mp3 = tmp_path / "song.mp3"
    mp3.write_bytes(b"fake")
    monkeypatch.setattr(L, "probe_duration_s", lambda p: 95.0)

    lyr = L.ingest_mp3(mp3)
    # ~20s markers -> round(95/20)=5 sections, covering the full duration.
    assert len(lyr.sections) == 5
    assert lyr.sections[0].start_s == 0.0
    assert lyr.sections[-1].end_s == 95.0
    assert all(s.text == "" for s in lyr.sections)
    for a, b in zip(lyr.sections, lyr.sections[1:]):
        assert b.start_s == a.end_s


def test_ingest_mp3_fits_supplied_lyric_stanzas(monkeypatch, tmp_path):
    mp3 = tmp_path / "song.mp3"
    mp3.write_bytes(b"fake")
    monkeypatch.setattr(L, "probe_duration_s", lambda p: 60.0)

    text = "first stanza line\nsecond line\n\nsecond stanza\nmore\n\nthird stanza"
    lyr = L.ingest_mp3(mp3, lyric_text=text)
    assert len(lyr.sections) == 3
    assert lyr.sections[0].text.startswith("first stanza")
    assert lyr.sections[-1].end_s == 60.0
    assert lyr.sections[0].start_s == 0.0


def test_probe_duration_missing_file_raises():
    with pytest.raises(L.LyricsError, match="not found"):
        L.probe_duration_s("/does/not/exist.mp3")


# ──────────────────────────────────────────────────────────────────────────────
# validate_song
# ──────────────────────────────────────────────────────────────────────────────


def test_validate_song_missing_file_warns(tmp_path):
    lyr = L.generate_lyrics("blue", 180.0)
    warnings = L.validate_song(lyr, tmp_path / "nope.mp3")
    assert any("not found" in w for w in warnings)


def test_validate_song_duration_mismatch_warns(monkeypatch, tmp_path):
    mp3 = tmp_path / "song.mp3"
    mp3.write_bytes(b"fake")
    monkeypatch.setattr(L, "probe_duration_s", lambda p: 120.0)  # song is 120s
    lyr = L.generate_lyrics("blue", 180.0)  # lyrics expect 180s
    warnings = L.validate_song(lyr, mp3)
    assert any("mismatch" in w for w in warnings)


def test_validate_song_within_tolerance_is_clean(monkeypatch, tmp_path):
    mp3 = tmp_path / "song.mp3"
    mp3.write_bytes(b"fake")
    monkeypatch.setattr(L, "probe_duration_s", lambda p: 181.0)  # within 2s of 180
    lyr = L.generate_lyrics("blue", 180.0)
    assert L.validate_song(lyr, mp3) == []


# ──────────────────────────────────────────────────────────────────────────────
# Optional music generation: provider resolution + loud errors
# ──────────────────────────────────────────────────────────────────────────────


def _clear_music_keys(monkeypatch):
    for env in ("GEMINI_API_KEY", "ELEVENLABS_API_KEY", "SUNO_API_KEY"):
        monkeypatch.delenv(env, raising=False)


def test_resolve_provider_auto_without_keys_raises(monkeypatch):
    _clear_music_keys(monkeypatch)
    with pytest.raises(L.LyricsError, match="No music API key"):
        L._resolve_music_provider("auto")


def test_resolve_provider_auto_prefers_gemini(monkeypatch):
    """The user's preferred provider: GEMINI_API_KEY wins under 'auto'."""
    _clear_music_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    monkeypatch.setenv("SUNO_API_KEY", "y")
    assert L._resolve_music_provider("auto") == "gemini"


def test_resolve_provider_auto_prefers_elevenlabs_when_no_gemini(monkeypatch):
    _clear_music_keys(monkeypatch)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    monkeypatch.setenv("SUNO_API_KEY", "y")
    assert L._resolve_music_provider("auto") == "elevenlabs"


def test_resolve_provider_auto_falls_back_to_suno(monkeypatch):
    _clear_music_keys(monkeypatch)
    monkeypatch.setenv("SUNO_API_KEY", "y")
    assert L._resolve_music_provider("auto") == "suno"


def test_resolve_provider_explicit_gemini_without_key_raises(monkeypatch):
    _clear_music_keys(monkeypatch)
    with pytest.raises(L.LyricsError, match="GEMINI_API_KEY"):
        L._resolve_music_provider("gemini")


def test_resolve_provider_explicit_without_key_raises(monkeypatch):
    _clear_music_keys(monkeypatch)
    with pytest.raises(L.LyricsError, match="SUNO_API_KEY"):
        L._resolve_music_provider("suno")


def test_generate_song_without_keys_raises(monkeypatch, tmp_path):
    _clear_music_keys(monkeypatch)
    with pytest.raises(L.LyricsError):
        L.generate_song("a gentle song", tmp_path / "song.mp3", provider="auto")


def test_generate_song_gemini_picks_lyria_pro_and_saves(monkeypatch, tmp_path):
    """Mock google-genai: verify Lyria 3 Pro is chosen for full-length and audio is saved
    from part.inline_data.data — without a live API call."""
    monkeypatch.setenv("GEMINI_API_KEY", "g")

    calls = {}

    class _Inline:
        def __init__(self, data):
            self.data = data

    class _Part:
        def __init__(self, data):
            self.inline_data = _Inline(data)

    class _Resp:
        parts = [_Part(b"ID3fake-mp3-bytes")]

    class _Models:
        def generate_content(self, model, contents):
            calls["model"] = model
            calls["contents"] = contents
            return _Resp()

    class _Client:
        def __init__(self, api_key):
            calls["api_key"] = api_key
            self.models = _Models()

    import google.genai as genai_mod

    monkeypatch.setattr(genai_mod, "Client", _Client)

    out = tmp_path / "song.mp3"
    result = L.generate_song(
        "Gentle toddler song about blue", out, provider="gemini", duration_s=180.0
    )

    assert Path(result) == out
    assert out.read_bytes() == b"ID3fake-mp3-bytes"
    assert calls["model"] == "lyria-3-pro-preview"  # >30s -> Pro
    assert calls["api_key"] == "g"


def test_generate_song_gemini_uses_clip_for_short(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "g")

    captured = {}

    class _Resp:
        class _P:
            class inline_data:
                data = b"x"

        parts = [_P()]

    class _Client:
        def __init__(self, api_key):
            self.models = self

        def generate_content(self, model, contents):
            captured["model"] = model
            return _Resp()

    import google.genai as genai_mod

    monkeypatch.setattr(genai_mod, "Client", _Client)

    L.generate_song("short clip", tmp_path / "s.mp3", provider="gemini", duration_s=20.0)
    assert captured["model"] == "lyria-3-clip-preview"  # <=30s -> Clip


# ──────────────────────────────────────────────────────────────────────────────
# Artifacts + CLI
# ──────────────────────────────────────────────────────────────────────────────


def test_write_artifacts_roundtrip(tmp_path):
    lyr = L.generate_lyrics("learning the color blue", 180.0)
    prompt = L.build_music_gen_prompt("learning the color blue", 180.0, lyr)
    lyrics_path, prompt_path = L.write_lyrics_artifacts(lyr, prompt, tmp_path)

    assert lyrics_path.exists() and prompt_path.exists()
    # lyrics.json is valid JSON that reloads into the Lyrics contract
    reloaded = Lyrics.model_validate_json(lyrics_path.read_text())
    assert reloaded.total_duration_s == 180.0
    assert "BPM" in prompt_path.read_text().upper()


def test_cli_topic_writes_artifacts(tmp_path, capsys):
    code = L.main(
        [
            "--project",
            "blue-song",
            "--topic",
            "learning the color blue",
            "--projects-root",
            str(tmp_path),
        ]
    )
    assert code == 0
    lyrics_path = tmp_path / "blue-song" / "lyrics.json"
    prompt_path = tmp_path / "blue-song" / "music_gen_prompt.txt"
    assert lyrics_path.exists()
    assert prompt_path.exists()

    data = json.loads(lyrics_path.read_text())
    assert data["sections"][0]["name"] == "intro"

    out = capsys.readouterr().out
    assert "total:" in out
    assert "wrote" in out
