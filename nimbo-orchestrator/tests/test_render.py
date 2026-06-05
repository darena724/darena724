"""Tests for the resumable render loop (Prompt 5).

Acceptance criteria, each covered (no real API calls, no ffmpeg, no spend):
  - a draft pass populates shots/ and sets per-shot status
  - the manifest reflects per-shot status after each shot
  - kill + rerun RESUMES without regenerating finished shots
  - the cost gate fires before any spend (no --yes -> zero generate_clip calls)
  - failed shots retry up to 3x with backoff, then are left failed; the run continues
  - ref images are attached on every call; rendering is silent (with_audio=False)
  - first-frame seeding: shot 1 -> canonical ref; shot N -> prior clip's last frame
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import character as C
from src import planner as P
from src import render as R
from src.models import Lyrics, LyricSection, ShotStatus, load_manifest, save_json_model


# ──────────────────────────────────────────────────────────────────────────────
# Project setup
# ──────────────────────────────────────────────────────────────────────────────


def _lyrics() -> Lyrics:
    # short song -> a handful of shots
    return Lyrics(
        sections=[
            LyricSection(name="intro", start_s=0.0, end_s=10.0, text="hi"),
            LyricSection(name="outro", start_s=10.0, end_s=20.0, text="bye"),
        ]
    )


def _setup_project(tmp_path) -> Path:
    pdir = tmp_path / "blue-song"
    (pdir / "refs").mkdir(parents=True)
    from PIL import Image

    Image.new("RGB", (32, 32), (245, 240, 230)).save(pdir / "refs" / "nimbo_modelsheet.png")
    save_json_model(_lyrics(), pdir / "lyrics.json")
    C.write_character_json(pdir)
    P.run(
        pdir,
        topic="learning the color blue",
        draft_model="seedance-2.0-fast",
        final_model="seedance-2.0",
        aspect_ratio="16:9",
        resolution="720p",
        save=True,
    )
    return pdir


@pytest.fixture
def fake_generate(monkeypatch):
    """Replace gen.generate_clip with a stub that writes a dummy mp4 and records calls."""
    calls: list[dict] = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        out = Path(kwargs["output_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00FAKE_MP4")  # a real file on disk so seeding can find it
        return {
            "status": "done",
            "output_path": str(out),
            "seconds": kwargs["duration_s"],
            "model": kwargs["model"],
            "est_cost": round(
                R.gen.MODEL_REGISTRY[kwargs["model"]].per_second_cost * kwargs["duration_s"], 4
            ),
        }

    monkeypatch.setattr(R.gen, "generate_clip", _fake)
    # Avoid real ffmpeg: pretend we extracted a seed frame.
    def _fake_extract(clip_path, out_path):
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"PNG")
        return out

    monkeypatch.setattr(R, "extract_last_frame", _fake_extract)
    # No real backoff sleeps.
    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(R, "_sleep", _no_sleep)
    return calls


# ──────────────────────────────────────────────────────────────────────────────
# Cost gate
# ──────────────────────────────────────────────────────────────────────────────


async def test_cost_gate_blocks_spend_without_yes(tmp_path, fake_generate):
    pdir = _setup_project(tmp_path)
    summary = await R.draft_pass(pdir, confirm=False)

    assert fake_generate == []  # ZERO API calls
    assert summary.confirmed is False
    assert summary.est_cost > 0
    assert summary.spent_estimate == 0.0
    assert summary.rendered == []
    # no clips written
    assert not (pdir / "shots").exists() or not list((pdir / "shots").glob("*.mp4"))


async def test_cost_gate_estimate_matches_registry(tmp_path, fake_generate):
    pdir = _setup_project(tmp_path)
    manifest = load_manifest(pdir / "manifest.json")
    expected = round(
        sum(R._estimate("seedance-2.0-fast", s.duration_s) for s in manifest.shots), 4
    )
    summary = await R.draft_pass(pdir, confirm=False)
    assert summary.est_cost == expected


# ──────────────────────────────────────────────────────────────────────────────
# Draft pass populates shots/ + sets status
# ──────────────────────────────────────────────────────────────────────────────


async def test_draft_pass_renders_all_and_sets_status(tmp_path, fake_generate):
    pdir = _setup_project(tmp_path)
    summary = await R.draft_pass(pdir, confirm=True)

    manifest = load_manifest(pdir / "manifest.json")
    n = len(manifest.shots)
    assert len(summary.rendered) == n
    assert len(fake_generate) == n
    # every shot drafted, with an mp4 on disk
    for s in manifest.shots:
        assert s.status is ShotStatus.drafted
        assert s.output_path == f"shots/{s.id}.mp4"
        assert (pdir / s.output_path).exists()


async def test_every_call_is_silent_and_carries_refs(tmp_path, fake_generate):
    pdir = _setup_project(tmp_path)
    await R.draft_pass(pdir, confirm=True)
    assert fake_generate, "no calls recorded"
    for call in fake_generate:
        assert call["with_audio"] is False  # silent — MP3 muxed at assembly
        assert call["reference_image_paths"], "refs must be attached on every call"
        assert call["reference_image_paths"][0].endswith("nimbo_modelsheet.png")
        assert call["model"] == "seedance-2.0-fast"


# ──────────────────────────────────────────────────────────────────────────────
# First-frame seeding
# ──────────────────────────────────────────────────────────────────────────────


async def test_seeding_chain(tmp_path, fake_generate):
    pdir = _setup_project(tmp_path)
    await R.draft_pass(pdir, confirm=True)

    # shot 1 seeds from the canonical ref; shot 2+ seed from the prior clip's last frame.
    first = fake_generate[0]
    assert first["first_frame_path"].endswith("nimbo_modelsheet.png")

    for call in fake_generate[1:]:
        assert call["first_frame_path"].endswith("_seed.png")


# ──────────────────────────────────────────────────────────────────────────────
# Resumability: kill + rerun
# ──────────────────────────────────────────────────────────────────────────────


async def test_rerun_skips_finished_shots(tmp_path, fake_generate):
    pdir = _setup_project(tmp_path)

    # First full pass
    await R.draft_pass(pdir, confirm=True)
    first_count = len(fake_generate)
    assert first_count > 0

    fake_generate.clear()

    # Rerun: everything is drafted -> nothing should be regenerated
    summary = await R.draft_pass(pdir, confirm=True)
    assert fake_generate == []  # no shot re-rendered (no double spend)
    assert summary.rendered == []
    assert len(summary.skipped) == first_count


async def test_partial_failure_then_rerun_resumes(tmp_path, fake_generate, monkeypatch):
    """Simulate a crash: render only the first shot, then 'resume' renders the rest."""
    pdir = _setup_project(tmp_path)
    manifest = load_manifest(pdir / "manifest.json")
    n = len(manifest.shots)
    assert n >= 2

    # Make generate_clip fail on shot_02 the first time around.
    real_calls = fake_generate

    async def _fail_on_2(**kwargs):
        if Path(kwargs["output_path"]).stem == "shot_02":
            real_calls.append(kwargs)
            raise R.gen.GenerationError("simulated transient failure")
        # otherwise behave normally
        real_calls.append(kwargs)
        out = Path(kwargs["output_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00FAKE")
        return {
            "status": "done", "output_path": str(out), "seconds": kwargs["duration_s"],
            "model": kwargs["model"],
            "est_cost": 0.1,
        }

    monkeypatch.setattr(R.gen, "generate_clip", _fail_on_2)

    summary1 = await R.draft_pass(pdir, confirm=True, max_retries=2)
    assert "shot_02" in summary1.failed

    m1 = load_manifest(pdir / "manifest.json")
    assert m1.shot_by_id("shot_01").status is ShotStatus.drafted
    assert m1.shot_by_id("shot_02").status is ShotStatus.failed

    # Now restore a working generate_clip and rerun: only shot_02 (failed) should re-render.
    real_calls.clear()
    monkeypatch.setattr(R.gen, "generate_clip", _make_ok_generate(real_calls))

    summary2 = await R.draft_pass(pdir, confirm=True)
    rerun_ids = {Path(c["output_path"]).stem for c in real_calls}
    assert rerun_ids == {"shot_02"}  # shot_01 (drafted) was skipped
    assert summary2.rendered == ["shot_02"]

    m2 = load_manifest(pdir / "manifest.json")
    assert all(s.status is ShotStatus.drafted for s in m2.shots)


def _make_ok_generate(record: list):
    async def _ok(**kwargs):
        record.append(kwargs)
        out = Path(kwargs["output_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00FAKE")
        return {
            "status": "done", "output_path": str(out), "seconds": kwargs["duration_s"],
            "model": kwargs["model"], "est_cost": 0.1,
        }

    return _ok


# ──────────────────────────────────────────────────────────────────────────────
# Retry + backoff
# ──────────────────────────────────────────────────────────────────────────────


async def test_retries_up_to_max_then_leaves_failed(tmp_path, monkeypatch):
    pdir = _setup_project(tmp_path)

    attempts = {"n": 0}

    async def _always_fail(**kwargs):
        attempts["n"] += 1
        raise R.gen.GenerationError("nope")

    sleeps: list[float] = []

    async def _record_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(R.gen, "generate_clip", _always_fail)
    monkeypatch.setattr(R, "extract_last_frame", lambda c, o: Path(o))
    monkeypatch.setattr(R, "_sleep", _record_sleep)

    # Render just the first shot's worth by limiting retries; check 3 attempts + 2 backoffs.
    manifest = load_manifest(pdir / "manifest.json")
    n_shots = len(manifest.shots)

    summary = await R.draft_pass(pdir, confirm=True, max_retries=3)

    # 3 attempts per shot
    assert attempts["n"] == 3 * n_shots
    # exponential backoff between attempts: 2s, 4s per shot (no sleep after the last attempt)
    assert sleeps[:2] == [2.0, 4.0]
    assert summary.failed == [s.id for s in manifest.shots]
    assert summary.rendered == []

    m = load_manifest(pdir / "manifest.json")
    assert all(s.status is ShotStatus.failed for s in m.shots)


# ──────────────────────────────────────────────────────────────────────────────
# Final pass
# ──────────────────────────────────────────────────────────────────────────────


async def test_final_pass_only_renders_approved(tmp_path, fake_generate):
    pdir = _setup_project(tmp_path)
    await R.draft_pass(pdir, confirm=True)

    # Approve two shots, mark the rest as-is (drafted).
    manifest = load_manifest(pdir / "manifest.json")
    approved_ids = [manifest.shots[0].id, manifest.shots[-1].id]
    for s in manifest.shots:
        if s.id in approved_ids:
            s.status = ShotStatus.approved
    from src.models import save_manifest

    save_manifest(manifest, pdir / "manifest.json")

    fake_generate.clear()
    summary = await R.final_pass(pdir, confirm=True)

    assert set(summary.rendered) == set(approved_ids)
    assert len(fake_generate) == len(approved_ids)
    # finals written, status -> done, using the final model
    m = load_manifest(pdir / "manifest.json")
    for s in m.shots:
        if s.id in approved_ids:
            assert s.status is ShotStatus.done
            assert s.output_path == f"finals/{s.id}.mp4"
            assert (pdir / s.output_path).exists()
        else:
            assert s.status is ShotStatus.drafted  # untouched
    for call in fake_generate:
        assert call["model"] == "seedance-2.0"  # final model


async def test_final_pass_cost_gate(tmp_path, fake_generate):
    pdir = _setup_project(tmp_path)
    await R.draft_pass(pdir, confirm=True)
    manifest = load_manifest(pdir / "manifest.json")
    manifest.shots[0].status = ShotStatus.approved
    from src.models import save_manifest

    save_manifest(manifest, pdir / "manifest.json")

    fake_generate.clear()
    summary = await R.final_pass(pdir, confirm=False)
    assert fake_generate == []  # gate held
    assert summary.est_cost > 0
    assert summary.spent_estimate == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def test_snap_duration_discrete_and_continuous():
    # Veo discrete {4,6,8}
    assert R._snap_duration("veo-3.1-fast", 8.4) == 8
    assert R._snap_duration("veo-3.1-fast", 7.0) in (6, 8)
    # Seedance continuous, rounds + clamps to [4, 15]
    assert R._snap_duration("seedance-2.0-fast", 8.4) == 8
    assert R._snap_duration("seedance-2.0-fast", 100) == 15
    assert R._snap_duration("seedance-2.0-fast", 1) == 4


def test_extract_last_frame_without_ffmpeg_is_loud(monkeypatch, tmp_path):
    monkeypatch.setattr(R.shutil, "which", lambda _x: None)
    with pytest.raises(R.RenderError, match="ffmpeg not found"):
        R.extract_last_frame(tmp_path / "clip.mp4", tmp_path / "out.png")
