"""Tests for assembly (Prompt 6).

Acceptance criteria, covered without ffmpeg:
  - final duration is forced to the song length (±0.5s) via -t + last-frame hold
  - audio mapped is ONLY the user's MP3 (clip audio dropped by concat v=1:a=0)
  - clips concatenated in manifest order, re-encoded to uniform size/fps/format
  - captions (when on) are generated, timed to sections, in the lower third
  - missing clips are caught before any ffmpeg call
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import assemble as A
from src.models import (
    Lyrics,
    LyricSection,
    Manifest,
    ProjectMeta,
    Shot,
    ShotStatus,
    save_json_model,
    save_manifest,
)


# ──────────────────────────────────────────────────────────────────────────────
# Geometry
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "res,aspect,expected",
    [
        ("720p", "16:9", (1280, 720)),
        ("1080p", "16:9", (1920, 1080)),
        ("720p", "9:16", (720, 1280)),
        ("720p", "1:1", (720, 720)),
        ("480p", "16:9", (854, 480)),  # 853.3 -> 854 (even)
    ],
)
def test_resolution_to_dims(res, aspect, expected):
    assert A.resolution_to_dims(res, aspect) == expected


def test_dims_always_even():
    for res in A._RES_SHORT_EDGE:
        for aspect in ("16:9", "9:16", "4:3", "1:1", "21:9"):
            w, h = A.resolution_to_dims(res, aspect)
            assert w % 2 == 0 and h % 2 == 0, (res, aspect, w, h)


def test_resolution_unknown_raises():
    with pytest.raises(A.AssembleError, match="unknown resolution"):
        A.resolution_to_dims("99p", "16:9")


# ──────────────────────────────────────────────────────────────────────────────
# ASS captions
# ──────────────────────────────────────────────────────────────────────────────


def test_ass_timestamp():
    assert A._ass_timestamp(0) == "0:00:00.00"
    assert A._ass_timestamp(75.5) == "0:01:15.50"
    assert A._ass_timestamp(3661.23) == "1:01:01.23"


def test_build_ass_has_style_and_timed_dialogue():
    lyrics = Lyrics(
        sections=[
            LyricSection(name="intro", start_s=0.0, end_s=10.0, text="Blue, blue\nthe sky is blue"),
            LyricSection(name="outro", start_s=10.0, end_s=20.0, text="bye bye"),
        ]
    )
    ass = A.build_ass(lyrics, 1280, 720)
    assert "[V4+ Styles]" in ass
    assert "Style: Nimbo" in ass
    # two dialogue lines, timed to the sections
    assert "Dialogue: 0,0:00:00.00,0:00:10.00,Nimbo" in ass
    assert "Dialogue: 0,0:00:10.00,0:00:20.00,Nimbo" in ass
    # hard line break converted to \N
    assert "Blue, blue\\Nthe sky is blue" in ass
    # margin places it in the lower third
    assert f"{round(720 * 0.12)}" in ass


def test_build_ass_skips_empty_sections():
    lyrics = Lyrics(sections=[LyricSection(name="instrumental", start_s=0, end_s=8, text="  ")])
    ass = A.build_ass(lyrics, 1280, 720)
    assert "Dialogue:" not in ass


# ──────────────────────────────────────────────────────────────────────────────
# Filter graph + ffmpeg argv
# ──────────────────────────────────────────────────────────────────────────────


def test_filter_complex_scales_concats_holds():
    fc = A.build_filter_complex(3, 1280, 720, 24, 180.0)
    # one scale/pad/fps node per clip
    for i in range(3):
        assert f"[{i}:v]scale=1280:720:force_original_aspect_ratio=decrease" in fc
        assert "fps=24" in fc
        assert "format=yuv420p" in fc
    # concat of 3 video streams, no audio
    assert "[v0][v1][v2]concat=n=3:v=1:a=0[vc]" in fc
    # last-frame hold so -t can trim to exact length
    assert "tpad=stop_mode=clone:stop_duration=180.000[vout]" in fc
    # no captions requested
    assert "ass=" not in fc


def test_filter_complex_with_captions_inserts_ass():
    fc = A.build_filter_complex(2, 1280, 720, 24, 60.0, ass_name="captions.ass")
    assert "[vc]ass=captions.ass[vs]" in fc
    assert "[vs]tpad=stop_mode=clone" in fc


def test_build_ffmpeg_cmd_audio_is_only_mp3_and_trimmed():
    clips = [Path("shots/shot_01.mp4"), Path("shots/shot_02.mp4")]
    cmd = A.build_ffmpeg_cmd(
        clips, Path("song.mp3"), Path("final.mp4"),
        width=1280, height=720, fps=24, song_duration=180.0,
    )
    # clips first, song last (input index 2)
    assert cmd.count("-i") == 3
    assert cmd[-1] == "final.mp4"
    # video from the filter graph, audio ONLY from the mp3 (input index == len(clips))
    assert "[vout]" in cmd
    assert "2:a" in cmd  # song is input #2
    # exact-length trim
    ti = cmd.index("-t")
    assert cmd[ti + 1] == "180.000"
    # encoders
    assert "libx264" in cmd
    assert "aac" in cmd
    assert "+faststart" in cmd


def test_build_ffmpeg_cmd_orders_clips_as_given():
    clips = [Path(f"shots/shot_{i:02d}.mp4") for i in range(1, 5)]
    cmd = A.build_ffmpeg_cmd(
        clips, Path("song.mp3"), Path("final.mp4"),
        width=640, height=360, fps=24, song_duration=30.0,
    )
    # the -i clip args appear in order, before the song
    i_positions = [i for i, tok in enumerate(cmd) if tok == "-i"]
    clip_args = [cmd[p + 1] for p in i_positions]
    assert clip_args == [str(c) for c in clips] + ["song.mp3"]


# ──────────────────────────────────────────────────────────────────────────────
# Clip collection
# ──────────────────────────────────────────────────────────────────────────────


def _manifest_with_clips(project_dir: Path, n: int, make_files=True) -> Manifest:
    shots = []
    for i in range(1, n + 1):
        rel = f"shots/shot_{i:02d}.mp4"
        if make_files:
            p = project_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x00clip")
        shots.append(
            Shot(
                id=f"shot_{i:02d}", start_s=(i - 1) * 8, end_s=i * 8, duration_s=8,
                status=ShotStatus.drafted, output_path=rel,
            )
        )
    m = Manifest(project=ProjectMeta(name="blue-song", resolution="720p", aspect_ratio="16:9"), shots=shots)
    save_manifest(m, project_dir / "manifest.json")
    return m


def test_collect_clips_in_order(tmp_path):
    m = _manifest_with_clips(tmp_path, 3)
    clips = A.collect_clips(tmp_path, m)
    assert [c.name for c in clips] == ["shot_01.mp4", "shot_02.mp4", "shot_03.mp4"]


def test_collect_clips_missing_raises(tmp_path):
    m = _manifest_with_clips(tmp_path, 2, make_files=False)
    with pytest.raises(A.AssembleError, match="no clip yet"):
        A.collect_clips(tmp_path, m)


def test_collect_clips_uses_finals_when_set(tmp_path):
    """A 'done' shot points output_path at finals/ — assembly should use that file."""
    rel = "finals/shot_01.mp4"
    (tmp_path / "finals").mkdir()
    (tmp_path / rel).write_bytes(b"\x00")
    shot = Shot(id="shot_01", start_s=0, end_s=8, duration_s=8, status=ShotStatus.done, output_path=rel)
    m = Manifest(project=ProjectMeta(name="p"), shots=[shot])
    clips = A.collect_clips(tmp_path, m)
    assert clips[0].as_posix().endswith("finals/shot_01.mp4")


# ──────────────────────────────────────────────────────────────────────────────
# assemble() orchestration (subprocess + probe mocked)
# ──────────────────────────────────────────────────────────────────────────────


def _full_project(tmp_path, n=3, song_dur=24.0):
    pdir = tmp_path / "blue-song"
    _manifest_with_clips(pdir, n)
    (pdir / "song.mp3").write_bytes(b"ID3song")
    save_json_model(
        Lyrics(sections=[
            LyricSection(name="intro", start_s=0, end_s=12, text="blue blue"),
            LyricSection(name="outro", start_s=12, end_s=24, text="bye"),
        ]),
        pdir / "lyrics.json",
    )
    return pdir


def test_assemble_dry_run_builds_command_no_run(tmp_path, monkeypatch):
    pdir = _full_project(tmp_path)
    monkeypatch.setattr(A.lyrics_mod, "probe_duration_s", lambda p: 24.0)
    # ensure subprocess is never called
    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: pytest.fail("ffmpeg ran in dry-run"))

    out = A.assemble(pdir, captions=False, dry_run=True)
    assert out == pdir / "final.mp4"
    assert not out.exists()


def test_assemble_runs_ffmpeg_and_maps_only_mp3(tmp_path, monkeypatch):
    pdir = _full_project(tmp_path)
    monkeypatch.setattr(A.lyrics_mod, "probe_duration_s", lambda p: 24.0)
    monkeypatch.setattr(A.shutil, "which", lambda _x: "/usr/bin/ffmpeg")

    captured = {}

    class _Proc:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, cwd=None, capture_output=False, text=False):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        # simulate ffmpeg producing the file
        Path(cmd[-1]).write_bytes(b"\x00final")
        return _Proc()

    monkeypatch.setattr(A.subprocess, "run", _fake_run)

    out = A.assemble(pdir, captions=False)
    assert out.exists()
    cmd = captured["cmd"]
    # audio mapped is the mp3 (input index == #clips == 3)
    assert "3:a" in cmd
    assert captured["cwd"] == str(pdir)
    assert "-t" in cmd and cmd[cmd.index("-t") + 1] == "24.000"


def test_assemble_captions_writes_ass_and_references_it(tmp_path, monkeypatch):
    pdir = _full_project(tmp_path)
    monkeypatch.setattr(A.lyrics_mod, "probe_duration_s", lambda p: 24.0)
    monkeypatch.setattr(A.shutil, "which", lambda _x: "/usr/bin/ffmpeg")

    captured = {}

    class _Proc:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, cwd=None, capture_output=False, text=False):
        captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"\x00final")
        return _Proc()

    monkeypatch.setattr(A.subprocess, "run", _fake_run)

    A.assemble(pdir, captions=True)
    # ASS file written next to the project
    ass = pdir / "captions.ass"
    assert ass.exists()
    assert "Dialogue:" in ass.read_text()
    # filtergraph references it by bare name (cwd=project_dir)
    fc = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert "ass=captions.ass" in fc


def test_assemble_missing_song_raises(tmp_path, monkeypatch):
    pdir = tmp_path / "blue-song"
    _manifest_with_clips(pdir, 2)
    # no song.mp3
    with pytest.raises(A.AssembleError, match="song not found"):
        A.assemble(pdir)


def test_assemble_ffmpeg_failure_surfaces_stderr(tmp_path, monkeypatch):
    pdir = _full_project(tmp_path)
    monkeypatch.setattr(A.lyrics_mod, "probe_duration_s", lambda p: 24.0)
    monkeypatch.setattr(A.shutil, "which", lambda _x: "/usr/bin/ffmpeg")

    class _Proc:
        returncode = 1
        stderr = "boom: invalid data"

    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(A.AssembleError, match="boom: invalid data"):
        A.assemble(pdir)


def test_assemble_warns_when_duration_off(tmp_path, monkeypatch, capsys):
    pdir = _full_project(tmp_path)
    # song is 24s but the produced file probes as 30s -> warn
    durations = iter([24.0, 30.0])  # first call = song, second = final.mp4
    monkeypatch.setattr(A.lyrics_mod, "probe_duration_s", lambda p: next(durations))
    monkeypatch.setattr(A.shutil, "which", lambda _x: "/usr/bin/ffmpeg")

    class _Proc:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, cwd=None, capture_output=False, text=False):
        Path(cmd[-1]).write_bytes(b"\x00")
        return _Proc()

    monkeypatch.setattr(A.subprocess, "run", _fake_run)
    A.assemble(pdir)
    assert "off" in capsys.readouterr().err
