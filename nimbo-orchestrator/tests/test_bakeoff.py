"""Tests for the model bake-off harness (Prompt 1.5).

What we validate empirically (no money spent):
  - default model list matches the Part 1.5 candidates
  - dry-run (no --yes) makes ZERO API calls
  - --yes drives generate_clip for each model with correct args
  - per-model duration is snapped to the model's allowed_durations set
  - cost estimate matches the model registry's per_second_cost
  - reference-image globs expand correctly
  - one failing model doesn't kill the rest of the run

The live "does Veo produce a playable mp4" check is intentionally NOT here — that's the
whole point of you running the script with --yes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_HERE = Path(__file__).resolve().parent.parent
_BAKEOFF = _HERE / "scripts" / "bakeoff.py"
_spec = importlib.util.spec_from_file_location("nimbo_bakeoff", _BAKEOFF)
bakeoff = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["nimbo_bakeoff"] = bakeoff
_spec.loader.exec_module(bakeoff)


# ──────────────────────────────────────────────────────────────────────────────
# Defaults
# ──────────────────────────────────────────────────────────────────────────────


def test_defaults_match_playbook_candidates():
    """Part 1.5 says: bake-off Veo 3.1, Seedance Fast, Kling 3.0."""
    assert set(bakeoff.DEFAULT_MODELS) == {
        "veo-3.1-fast",
        "seedance-2.0-fast",
        "kling-v3-standard",
    }


def test_default_prompt_carries_identity_anchor_and_negative():
    """The bake-off prompt should reference Nimbo's signature features (cloud, cream body,
    coral cheeks) so identity is anchored even without refs."""
    p = bakeoff.DEFAULT_PROMPT.lower()
    assert "nimbo" in p
    assert "cloud" in p
    assert "cream" in p or "cream/oatmeal" in p
    # negative prompt covers the canonical "never" list
    n = bakeoff.DEFAULT_NEGATIVE.lower()
    assert "neon" in n
    assert "text" in n
    assert "watermark" in n


# ──────────────────────────────────────────────────────────────────────────────
# Cost / duration planning
# ──────────────────────────────────────────────────────────────────────────────


def test_estimate_cost_matches_registry():
    spec = bakeoff.gen.MODEL_REGISTRY["seedance-2.0-fast"]
    assert bakeoff._estimate_cost("seedance-2.0-fast", 8) == round(spec.per_second_cost * 8, 4)


def test_normalize_duration_snaps_to_allowed_set():
    """Veo allows {4, 6, 8}. Asking for 7 snaps down to 6."""
    assert bakeoff._normalize_duration("veo-3.1-fast", 7) == 6
    assert bakeoff._normalize_duration("veo-3.1-fast", 8) == 8
    assert bakeoff._normalize_duration("veo-3.1-fast", 100) == 8  # caps at model max


def test_normalize_duration_continuous_models_use_cap():
    """Seedance is continuous 4–15 — keeps the requested value but caps at max."""
    assert bakeoff._normalize_duration("seedance-2.0-fast", 8) == 8
    assert bakeoff._normalize_duration("seedance-2.0-fast", 100) == 15  # max_seconds


# ──────────────────────────────────────────────────────────────────────────────
# Dry-run: ZERO API calls
# ──────────────────────────────────────────────────────────────────────────────


async def test_dry_run_makes_no_api_calls(monkeypatch, tmp_path):
    """Without --yes, run_bakeoff must NOT call generate_clip."""
    fake_generate = AsyncMock(side_effect=AssertionError("dry-run leaked an API call"))
    monkeypatch.setattr(bakeoff.gen, "generate_clip", fake_generate)

    rows = await bakeoff.run_bakeoff(
        models=["seedance-2.0-fast", "veo-3.1-fast"],
        refs=[],
        prompt="x",
        negative_prompt="y",
        duration_s=8,
        aspect_ratio="16:9",
        resolution="720p",
        output_dir=tmp_path / "bakeoff",
        confirm=False,
    )

    fake_generate.assert_not_called()
    assert all(r.status == "skipped (dry-run)" for r in rows)
    assert len(rows) == 2
    # Even in dry-run we still report the would-be cost so the user can decide.
    assert all(r.est_cost > 0 for r in rows)


# ──────────────────────────────────────────────────────────────────────────────
# Real run (mocked generate_clip): correct orchestration
# ──────────────────────────────────────────────────────────────────────────────


async def test_yes_drives_generate_clip_per_model(monkeypatch, tmp_path):
    """--yes mode: one generate_clip call per model with snapped duration + the right args."""
    calls: list[dict] = []

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        # mimic the real return shape
        return {
            "status": "done",
            "output_path": kwargs["output_path"],
            "seconds": kwargs["duration_s"],
            "model": kwargs["model"],
            "est_cost": round(
                bakeoff.gen.MODEL_REGISTRY[kwargs["model"]].per_second_cost * kwargs["duration_s"],
                4,
            ),
        }

    monkeypatch.setattr(bakeoff.gen, "generate_clip", fake_generate)

    out = tmp_path / "bakeoff"
    rows = await bakeoff.run_bakeoff(
        models=["seedance-2.0-fast", "veo-3.1-fast", "kling-v3-standard"],
        refs=["/some/ref.png"],
        prompt="Nimbo waves",
        negative_prompt="neon",
        duration_s=8,
        aspect_ratio="16:9",
        resolution="720p",
        output_dir=out,
        confirm=True,
    )

    assert len(calls) == 3
    assert {c["model"] for c in calls} == {
        "seedance-2.0-fast",
        "veo-3.1-fast",
        "kling-v3-standard",
    }
    # Every call: silent (with_audio=False), same refs, same prompt
    for c in calls:
        assert c["with_audio"] is False
        assert c["reference_image_paths"] == ["/some/ref.png"]
        assert c["prompt"] == "Nimbo waves"
        assert c["aspect_ratio"] == "16:9"
        assert c["resolution"] == "720p"
        assert c["output_path"].endswith(f"{c['model']}.mp4")

    # All rows reflect success
    assert all(r.status == "done" for r in rows)


async def test_unknown_models_are_skipped_not_fatal(monkeypatch, tmp_path):
    fake_generate = AsyncMock(
        return_value={
            "status": "done",
            "output_path": "x.mp4",
            "seconds": 8,
            "model": "seedance-2.0-fast",
            "est_cost": 0.176,
        }
    )
    monkeypatch.setattr(bakeoff.gen, "generate_clip", fake_generate)

    rows = await bakeoff.run_bakeoff(
        models=["seedance-2.0-fast", "not-a-real-model"],
        refs=[],
        prompt="x",
        negative_prompt="y",
        duration_s=8,
        aspect_ratio="16:9",
        resolution="720p",
        output_dir=tmp_path / "bakeoff",
        confirm=True,
    )

    fake_generate.assert_awaited_once()  # only the real model
    assert len(rows) == 1
    assert rows[0].model == "seedance-2.0-fast"


async def test_one_failing_model_does_not_kill_the_others(monkeypatch, tmp_path):
    """Per Prompt 5's resumability ethos: one failure leaves the others running.
    Same here — the bake-off should produce comparable rows for every requested model."""

    async def flaky(**kwargs):
        if kwargs["model"] == "veo-3.1-fast":
            raise bakeoff.gen.GenerationError("simulated billing failure")
        return {
            "status": "done",
            "output_path": kwargs["output_path"],
            "seconds": kwargs["duration_s"],
            "model": kwargs["model"],
            "est_cost": 0.176,
        }

    monkeypatch.setattr(bakeoff.gen, "generate_clip", flaky)

    rows = await bakeoff.run_bakeoff(
        models=["seedance-2.0-fast", "veo-3.1-fast", "kling-v3-standard"],
        refs=[],
        prompt="x",
        negative_prompt="y",
        duration_s=8,
        aspect_ratio="16:9",
        resolution="720p",
        output_dir=tmp_path / "bakeoff",
        confirm=True,
    )

    by_model = {r.model: r for r in rows}
    assert by_model["seedance-2.0-fast"].status == "done"
    assert by_model["kling-v3-standard"].status == "done"
    assert by_model["veo-3.1-fast"].status == "failed"
    assert "simulated billing failure" in (by_model["veo-3.1-fast"].error or "")


# ──────────────────────────────────────────────────────────────────────────────
# Ref glob expansion
# ──────────────────────────────────────────────────────────────────────────────


def test_expand_refs_handles_globs_and_literal_paths(tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "b.png").write_bytes(b"x")
    (tmp_path / "c.txt").write_bytes(b"x")

    out = bakeoff._expand_refs([str(tmp_path / "*.png"), str(tmp_path / "c.txt")])
    assert sorted(Path(p).name for p in out) == ["a.png", "b.png", "c.txt"]


def test_expand_refs_warns_on_no_match(tmp_path, capsys):
    out = bakeoff._expand_refs([str(tmp_path / "nothing-*.png")])
    assert out == []
    captured = capsys.readouterr()
    assert "matched nothing" in captured.err


# ──────────────────────────────────────────────────────────────────────────────
# CLI parsing
# ──────────────────────────────────────────────────────────────────────────────


def test_parse_args_default_is_dry_run():
    args = bakeoff.parse_args([])
    assert args.yes is False
    assert args.models == list(bakeoff.DEFAULT_MODELS)


def test_parse_args_models_csv():
    args = bakeoff.parse_args(["--models", "veo-3.1, seedance-2.0"])
    assert args.models == ["veo-3.1", "seedance-2.0"]


def test_parse_args_yes_and_refs():
    args = bakeoff.parse_args(
        ["--yes", "--refs", "a.png", "b.png", "--duration-s", "10"]
    )
    assert args.yes is True
    assert args.refs == ["a.png", "b.png"]
    assert args.duration_s == 10


# ──────────────────────────────────────────────────────────────────────────────
# Main: dry-run exits 0 and prints the cost plan
# ──────────────────────────────────────────────────────────────────────────────


def test_main_dry_run_prints_plan_and_exits_zero(capsys):
    code = bakeoff.main([])  # no --yes
    assert code == 0
    out = capsys.readouterr().out
    assert "Bake-off plan" in out
    assert "TOTAL estimated spend" in out
    assert "Dry run" in out
    # Cost table rendered with all three default models
    for m in bakeoff.DEFAULT_MODELS:
        assert m in out
