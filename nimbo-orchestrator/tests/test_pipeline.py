"""Tests for the end-to-end pipeline (Prompt 7).

Acceptance:
  - --dry-run shows plan + cost with NO API calls and NO writes
  - the pipeline STOPs at each review gate and advances on re-run
  - cost gates block spend without --yes
  - a full run with approvals produces final.mp4 (assembly mocked: ffmpeg-free)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import assemble as assemble_mod
from src import pipeline as PL
from src import render as render_mod
from src.models import ShotStatus, load_manifest, save_manifest


# ──────────────────────────────────────────────────────────────────────────────
# Mocks: no network, no ffmpeg
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def no_spend(monkeypatch):
    """Make generate_clip write a dummy mp4, ffmpeg-extract a fake frame, no sleeps,
    and assembly produce a fake final.mp4. Records generate_clip calls."""
    calls: list[dict] = []

    async def _fake_generate(**kwargs):
        calls.append(kwargs)
        out = Path(kwargs["output_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00FAKE")
        return {
            "status": "done", "output_path": str(out), "seconds": kwargs["duration_s"],
            "model": kwargs["model"],
            "est_cost": round(
                render_mod.gen.MODEL_REGISTRY[kwargs["model"]].per_second_cost * kwargs["duration_s"], 4
            ),
        }

    monkeypatch.setattr(render_mod.gen, "generate_clip", _fake_generate)
    monkeypatch.setattr(render_mod, "extract_last_frame", lambda c, o: Path(o))

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(render_mod, "_sleep", _no_sleep)

    # assembly: skip real ffmpeg, just emit a file
    def _fake_assemble(project_dir, *, captions=False, dry_run=False, **kw):
        out = Path(project_dir) / "final.mp4"
        out.write_bytes(b"\x00FINAL")
        return out

    monkeypatch.setattr(assemble_mod, "assemble", _fake_assemble)
    return calls


def _run(tmp_path, **kwargs):
    project_dir = tmp_path / "blue-song"
    defaults = dict(
        topic="learning the color blue", mp3=None, lyric_text=None, model=None,
        captions=False, confirm=False, assemble_drafts=False, dry_run=False,
    )
    defaults.update(kwargs)
    return PL.run_pipeline(project_dir, **defaults), project_dir


# ──────────────────────────────────────────────────────────────────────────────
# Dry run
# ──────────────────────────────────────────────────────────────────────────────


def test_dry_run_no_writes_no_calls(tmp_path, no_spend, capsys):
    out, pdir = _run(tmp_path, dry_run=True)
    assert out is None
    assert no_spend == []                       # zero generate_clip calls
    assert not pdir.exists() or not (pdir / "lyrics.json").exists()  # no writes
    text = capsys.readouterr().out
    assert "DRY RUN" in text
    assert "Estimated DRAFT cost" in text
    assert "Estimated FINAL cost" in text
    assert "shot_01" in text                     # a real shot table


def test_dry_run_requires_a_source(tmp_path, no_spend):
    project_dir = tmp_path / "blue-song"
    with pytest.raises(PL.PipelineStop, match="needs --topic or --mp3"):
        PL.run_pipeline(
            project_dir, topic=None, mp3=None, lyric_text=None, model=None,
            captions=False, confirm=False, assemble_drafts=False, dry_run=True,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Stage progression with STOPs
# ──────────────────────────────────────────────────────────────────────────────


def test_stop1_after_song(tmp_path, no_spend, capsys):
    out, pdir = _run(tmp_path)
    assert out is None
    assert (pdir / "lyrics.json").exists()       # lyrics written
    assert not (pdir / "manifest.json").exists() # but stopped before planning
    assert "STOP 1/4" in capsys.readouterr().out


def test_stop2_after_plan(tmp_path, no_spend, capsys):
    _run(tmp_path)                  # stage 1 -> lyrics
    out, pdir = _run(tmp_path)      # stage 2 -> plan
    assert out is None
    assert (pdir / "manifest.json").exists()
    m = load_manifest(pdir / "manifest.json")
    assert len(m.shots) > 0
    assert "STOP 2/4" in capsys.readouterr().out
    # no clips yet
    assert not (pdir / "shots").exists()


def test_draft_cost_gate_blocks_without_yes(tmp_path, no_spend, capsys):
    _run(tmp_path)                  # lyrics
    _run(tmp_path)                  # plan
    out, pdir = _run(tmp_path)      # draft stage, no --yes
    assert out is None
    assert no_spend == []           # cost gate held — no spend
    text = capsys.readouterr().out
    assert "DRAFT pass" in text
    assert "pass --yes" in text


def test_draft_renders_with_yes_then_stops_for_approval(tmp_path, no_spend, capsys):
    _run(tmp_path)                  # lyrics
    _run(tmp_path)                  # plan
    out, pdir = _run(tmp_path, confirm=True)   # draft render
    assert out is None
    m = load_manifest(pdir / "manifest.json")
    assert all(s.status is ShotStatus.drafted for s in m.shots)
    assert len(no_spend) == len(m.shots)        # one call per shot
    assert (pdir / "shots").exists()
    assert "STOP 3/4" in capsys.readouterr().out


def test_approval_gate_when_nothing_approved(tmp_path, no_spend, capsys):
    _run(tmp_path); _run(tmp_path)                 # lyrics, plan
    _run(tmp_path, confirm=True)                   # draft
    no_spend.clear()
    out, pdir = _run(tmp_path, confirm=True)       # re-run, nothing approved
    assert out is None
    assert no_spend == []
    assert "waiting for your approvals" in capsys.readouterr().out


# ──────────────────────────────────────────────────────────────────────────────
# Full run to final.mp4
# ──────────────────────────────────────────────────────────────────────────────


def _approve_all(pdir, status=ShotStatus.approved):
    m = load_manifest(pdir / "manifest.json")
    for s in m.shots:
        s.status = status
    save_manifest(m, pdir / "manifest.json")


def test_full_run_with_approvals_produces_final(tmp_path, no_spend, capsys):
    _run(tmp_path)                       # 1: lyrics
    _run(tmp_path)                       # 2: plan
    _run(tmp_path, confirm=True)         # 3: draft
    _approve_all(tmp_path / "blue-song") # user approves all shots
    no_spend.clear()

    # 4: final render of approved (this run renders, then assembly happens on next call
    # because final_pass reloads approved->done within the same invocation and proceeds)
    out, pdir = _run(tmp_path, confirm=True, captions=True)

    # final pass rendered every approved shot, then assembly produced final.mp4
    assert len(no_spend) == len(load_manifest(pdir / "manifest.json").shots)
    m = load_manifest(pdir / "manifest.json")
    assert all(s.status is ShotStatus.done for s in m.shots)
    assert out is not None and out.name == "final.mp4" and out.exists()
    assert "DONE" in capsys.readouterr().out
    # every final call used the locked final model + silent
    for c in no_spend:
        assert c["model"] == "seedance-2.0"
        assert c["with_audio"] is False


def test_assemble_drafts_skips_final(tmp_path, no_spend, capsys):
    _run(tmp_path)                       # lyrics
    _run(tmp_path)                       # plan
    _run(tmp_path, confirm=True)         # draft
    no_spend.clear()

    # assemble drafts directly — no final pass, no extra spend
    out, pdir = _run(tmp_path, confirm=True, assemble_drafts=True)
    assert no_spend == []                # no final render
    assert out is not None and out.exists()
    m = load_manifest(pdir / "manifest.json")
    assert all(s.status is ShotStatus.drafted for s in m.shots)  # untouched drafts


def test_redo_marks_rerender_on_next_draft(tmp_path, no_spend):
    _run(tmp_path); _run(tmp_path); _run(tmp_path, confirm=True)  # through draft
    pdir = tmp_path / "blue-song"
    m = load_manifest(pdir / "manifest.json")
    # mark the first shot redo, approve the rest
    m.shots[0].status = ShotStatus.redo
    for s in m.shots[1:]:
        s.status = ShotStatus.approved
    save_manifest(m, pdir / "manifest.json")
    no_spend.clear()

    # next run: the redo shot goes back through the draft pass (only it re-renders)
    _run(tmp_path, confirm=True)
    rerendered = {Path(c["output_path"]).stem for c in no_spend}
    assert rerendered == {"shot_01"}
    m2 = load_manifest(pdir / "manifest.json")
    assert m2.shots[0].status is ShotStatus.drafted  # redo -> drafted again


# ──────────────────────────────────────────────────────────────────────────────
# MP3 path
# ──────────────────────────────────────────────────────────────────────────────


def test_mp3_source_branch(tmp_path, no_spend, monkeypatch, capsys):
    # ingest_mp3 needs a real duration probe — stub it
    from src.models import Lyrics, LyricSection

    monkeypatch.setattr(
        PL.lyrics_mod, "ingest_mp3",
        lambda mp3, lyric_text=None: Lyrics(
            sections=[LyricSection(name="intro", start_s=0, end_s=20, text="hi")]
        ),
    )
    song = tmp_path / "mysong.mp3"
    song.write_bytes(b"ID3")

    project_dir = tmp_path / "blue-song"
    out = PL.run_pipeline(
        project_dir, topic=None, mp3=str(song), lyric_text=None, model=None,
        captions=False, confirm=False, assemble_drafts=False, dry_run=False,
    )
    assert out is None
    assert (project_dir / "lyrics.json").exists()
    assert "STOP 1/4" in capsys.readouterr().out
