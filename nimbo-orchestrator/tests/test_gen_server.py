"""Tests for the generation MCP server.

Validates what we CAN validate without spending money:
  - list_models() returns sane data for every registered model
  - generate_clip() raises loud, useful errors without keys / for unknown models
  - endpoint selection picks the right variant for each (first_frame, refs) combo
  - argument translation produces the right per-model field names

Smoke-tests for real generate_clip() calls against Veo / Seedance / Kling are NOT here —
those require a live FAL_KEY and cost real money. The bake-off script (Prompt 1.5) is
where you spend.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# The gen_server lives outside the `src` package and shares its name with the MCP SDK
# package. Load it directly via importlib so the SDK import inside it still resolves.
_GEN = Path(__file__).resolve().parent.parent / "mcp" / "gen_server.py"
_spec = importlib.util.spec_from_file_location("nimbo_gen_server", _GEN)
gen = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# Register in sys.modules BEFORE exec so @dataclass can resolve cls.__module__.
sys.modules["nimbo_gen_server"] = gen
_spec.loader.exec_module(gen)


# ──────────────────────────────────────────────────────────────────────────────
# list_models()
# ──────────────────────────────────────────────────────────────────────────────


def test_list_models_returns_required_models():
    """Prompt 1 requires at minimum: Veo 3.1 + variants, Seedance fast+pro, Kling, Wan."""
    models = gen.list_models()
    ids = {m["id"] for m in models}

    assert "veo-3.1" in ids
    assert "veo-3.1-fast" in ids
    assert "veo-direct" in ids
    assert "seedance-2.0-fast" in ids
    assert "seedance-2.0" in ids
    assert "kling-v3-standard" in ids
    assert "kling-v3-pro" in ids
    assert "wan-2.6" in ids


def test_list_models_shape():
    """Every model entry must carry the metadata downstream code relies on."""
    models = gen.list_models()
    required_fields = {
        "id",
        "label",
        "provider",
        "tier",
        "supports_reference_images",
        "max_ref_images",
        "supports_first_frame",
        "max_seconds",
        "allowed_durations",
        "supports_audio",
        "per_second_cost_estimate_usd",
        "endpoints",
    }
    for m in models:
        assert required_fields <= set(m.keys()), f"missing fields on {m['id']}: {required_fields - set(m.keys())}"
        assert m["provider"] in {"fal", "google"}
        assert m["tier"] in {"draft", "standard", "pro"}
        assert m["max_seconds"] >= 4
        assert m["per_second_cost_estimate_usd"] > 0


def test_seedance_has_reference_to_video_endpoint():
    """The bake-off / planner relies on Seedance for multi-image refs."""
    models = {m["id"]: m for m in gen.list_models()}
    seedance = models["seedance-2.0-fast"]
    assert seedance["supports_reference_images"]
    assert seedance["max_ref_images"] >= 4
    assert "r2v" in seedance["endpoints"]
    assert seedance["endpoints"]["r2v"] == "bytedance/seedance-2.0/fast/reference-to-video"


def test_veo_endpoints_present():
    models = {m["id"]: m for m in gen.list_models()}
    veo_fast = models["veo-3.1-fast"]
    assert veo_fast["endpoints"]["t2v"] == "fal-ai/veo3.1/fast"
    assert veo_fast["endpoints"]["i2v"] == "fal-ai/veo3.1/fast/image-to-video"
    assert veo_fast["supports_first_frame"]


def test_kling_uses_start_image_url():
    spec = gen.MODEL_REGISTRY["kling-v3-standard"]
    assert spec.i2v is not None
    assert spec.i2v.image_field == "start_image_url"  # not image_url — Kling-specific
    assert spec.i2v.elements_field == "elements"


# ──────────────────────────────────────────────────────────────────────────────
# Endpoint selection
# ──────────────────────────────────────────────────────────────────────────────


def test_pick_endpoint_first_frame_takes_i2v():
    spec = gen.MODEL_REGISTRY["seedance-2.0-fast"]
    ep = gen._pick_endpoint(spec, first_frame=True, refs=False)
    assert ep is spec.i2v


def test_pick_endpoint_refs_only_takes_r2v_when_available():
    spec = gen.MODEL_REGISTRY["seedance-2.0-fast"]
    ep = gen._pick_endpoint(spec, first_frame=False, refs=True)
    assert ep is spec.r2v


def test_pick_endpoint_no_inputs_takes_t2v():
    spec = gen.MODEL_REGISTRY["seedance-2.0-fast"]
    ep = gen._pick_endpoint(spec, first_frame=False, refs=False)
    assert ep is spec.t2v


def test_pick_endpoint_refs_only_falls_back_to_i2v_when_no_r2v():
    """Veo and Kling have no reference-to-video endpoint — must fall back to i2v."""
    spec = gen.MODEL_REGISTRY["veo-3.1-fast"]
    ep = gen._pick_endpoint(spec, first_frame=False, refs=True)
    assert ep is spec.i2v


# ──────────────────────────────────────────────────────────────────────────────
# Argument translation (without making real calls)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_fal_args_seedance_with_refs(monkeypatch, tmp_path):
    """Seedance r2v: refs should land in image_urls (list)."""
    spec = gen.MODEL_REGISTRY["seedance-2.0-fast"]
    endpoint = spec.r2v

    # Stub the uploader so we don't need fal_client / a key
    async def fake_to_url(p: str) -> str:
        return f"https://fake.fal/{Path(p).name}"

    monkeypatch.setattr(gen, "_to_url", fake_to_url)

    ref1 = tmp_path / "nimbo_front.png"
    ref2 = tmp_path / "nimbo_3q.png"
    ref1.write_bytes(b"x")
    ref2.write_bytes(b"x")

    args = await gen._build_fal_args(
        spec,
        endpoint,
        prompt="Nimbo waves",
        duration_s=8,
        reference_image_paths=[str(ref1), str(ref2)],
        first_frame_path=None,
        with_audio=False,
        aspect_ratio="16:9",
        resolution="720p",
        negative_prompt="neon, busy",
        seed=42,
    )

    assert args["prompt"] == "Nimbo waves"
    assert args["duration"] == "8"
    assert args["aspect_ratio"] == "16:9"
    assert args["resolution"] == "720p"
    assert args["generate_audio"] is False
    assert args["negative_prompt"] == "neon, busy"
    assert args["seed"] == 42
    assert args["image_urls"] == [
        "https://fake.fal/nimbo_front.png",
        "https://fake.fal/nimbo_3q.png",
    ]
    assert "image_url" not in args  # refs go in image_urls, not image_url
    assert "start_image_url" not in args


@pytest.mark.asyncio
async def test_build_fal_args_seedance_with_first_frame(monkeypatch, tmp_path):
    """Seedance i2v: first_frame should land in image_url."""
    spec = gen.MODEL_REGISTRY["seedance-2.0-fast"]
    endpoint = spec.i2v

    async def fake_to_url(p: str) -> str:
        return f"https://fake.fal/{Path(p).name}"

    monkeypatch.setattr(gen, "_to_url", fake_to_url)

    seed_frame = tmp_path / "shot_01_last.png"
    seed_frame.write_bytes(b"x")

    args = await gen._build_fal_args(
        spec,
        endpoint,
        prompt="Nimbo points",
        duration_s=8,
        reference_image_paths=[],
        first_frame_path=str(seed_frame),
        with_audio=False,
        aspect_ratio="16:9",
        resolution="720p",
        negative_prompt=None,
        seed=None,
    )

    assert args["image_url"] == "https://fake.fal/shot_01_last.png"
    assert "image_urls" not in args


@pytest.mark.asyncio
async def test_build_fal_args_kling_uses_start_image_url(monkeypatch, tmp_path):
    spec = gen.MODEL_REGISTRY["kling-v3-standard"]
    endpoint = spec.i2v

    async def fake_to_url(p: str) -> str:
        return f"https://fake.fal/{Path(p).name}"

    monkeypatch.setattr(gen, "_to_url", fake_to_url)

    seed_frame = tmp_path / "seed.png"
    ref = tmp_path / "ref.png"
    seed_frame.write_bytes(b"x")
    ref.write_bytes(b"x")

    args = await gen._build_fal_args(
        spec,
        endpoint,
        prompt="Nimbo glides",
        duration_s=5,
        reference_image_paths=[str(ref)],
        first_frame_path=str(seed_frame),
        with_audio=False,
        aspect_ratio="16:9",
        resolution="720p",
        negative_prompt=None,
        seed=None,
    )

    # Kling-specific: start_image_url for the seed, elements for refs
    assert args["start_image_url"] == "https://fake.fal/seed.png"
    assert args["elements"] == [{"image_url": "https://fake.fal/ref.png"}]


# ──────────────────────────────────────────────────────────────────────────────
# Duration validation
# ──────────────────────────────────────────────────────────────────────────────


def test_validate_duration_enforces_max():
    spec = gen.MODEL_REGISTRY["veo-3.1-fast"]
    with pytest.raises(gen.GenerationError, match="exceeds .* max"):
        gen._validate_duration(spec, 20)


def test_validate_duration_enforces_allowed_set():
    """Veo accepts only {4, 6, 8}."""
    spec = gen.MODEL_REGISTRY["veo-3.1-fast"]
    with pytest.raises(gen.GenerationError, match="discrete durations"):
        gen._validate_duration(spec, 7)


def test_validate_duration_continuous_for_seedance():
    """Seedance accepts continuous 4–15."""
    spec = gen.MODEL_REGISTRY["seedance-2.0-fast"]
    gen._validate_duration(spec, 7)  # no allowed_durations, just <= max_seconds
    with pytest.raises(gen.GenerationError):
        gen._validate_duration(spec, 0)


# ──────────────────────────────────────────────────────────────────────────────
# Error surfaces
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_clip_unknown_model_raises():
    with pytest.raises(gen.GenerationError, match="Unknown model"):
        await gen.generate_clip(
            prompt="x", output_path="/tmp/out.mp4", model="not-a-model"
        )


@pytest.mark.asyncio
async def test_generate_clip_missing_fal_key_is_loud(monkeypatch, tmp_path):
    """No FAL_KEY -> a clear error, not a silent hang."""
    monkeypatch.delenv("FAL_KEY", raising=False)
    with pytest.raises(gen.GenerationError, match="FAL_KEY"):
        await gen.generate_clip(
            prompt="Nimbo waves",
            output_path=str(tmp_path / "out.mp4"),
            model="seedance-2.0-fast",
            duration_s=8,
        )


@pytest.mark.asyncio
async def test_generate_clip_missing_gemini_key_is_loud(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(gen.GenerationError, match="GEMINI_API_KEY"):
        await gen.generate_clip(
            prompt="Nimbo waves",
            output_path=str(tmp_path / "out.mp4"),
            model="veo-direct",
            duration_s=8,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Video URL extraction
# ──────────────────────────────────────────────────────────────────────────────


def test_video_url_extraction_handles_both_response_shapes():
    assert gen._video_url_from_result({"video": {"url": "u"}}) == "u"
    assert gen._video_url_from_result({"video": "u"}) == "u"
    assert gen._video_url_from_result({"url": "u"}) == "u"
    with pytest.raises(gen.GenerationError):
        gen._video_url_from_result({"weird": "shape"})


# ──────────────────────────────────────────────────────────────────────────────
# MCP wiring
# ──────────────────────────────────────────────────────────────────────────────


def test_mcp_server_registers_three_tools():
    """The FastMCP server must expose list_models, generate_clip, get_status."""
    # FastMCP stores tools internally; surface them via the tool manager.
    tool_mgr = gen.mcp._tool_manager
    names = {t.name for t in tool_mgr.list_tools()}
    assert names == {"list_models", "generate_clip", "get_status"}
