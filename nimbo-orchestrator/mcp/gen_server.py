"""Provider-abstracted video-generation MCP server.

Wraps fal.ai (default) and an optional Veo-direct path (google-genai) behind a single
FastMCP server. Exposes three tools per Prompt 1:

  - list_models()        -> registered video models + metadata
  - generate_clip(...)   -> render a single clip (sync; downloads to output_path)
  - get_status(job_id)   -> in-flight status for a job submitted via this server

Run as a script (stdio transport):

  python mcp/gen_server.py

Notes on layout:
  * This folder is intentionally NOT a Python package (no __init__.py) so it never
    shadows the `mcp` SDK. We import the SDK as `from mcp.server.fastmcp import FastMCP`.
  * Model IDs / parameter names / endpoints come from the CURRENT fal.ai docs (verified
    on the date this file was written). They drift — re-check before treating the
    `per_second_cost` numbers as billable truth.
  * Audio defaults OFF. The render pass mutes the clip; the user's MP3 is muxed at
    assembly (ARCHITECTURE.md decision #3).
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# ──────────────────────────────────────────────────────────────────────────────
# Setup
# ──────────────────────────────────────────────────────────────────────────────

# Load .env from project root (one level up from this file).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

logger = logging.getLogger("nimbo.gen_server")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

mcp = FastMCP("nimbo-gen-server")

# In-memory map: fal request_id -> the endpoint it was submitted to.
# Populated by `generate_clip` via the on_enqueue callback; consumed by `get_status`.
_REQUEST_REGISTRY: dict[str, str] = {}


# ──────────────────────────────────────────────────────────────────────────────
# Model registry
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Endpoint:
    """One fal-ai endpoint variant + the field names it expects for image inputs.

    Each model exposes several endpoints (t2v / i2v / r2v / first-last). We select
    one per call based on what the caller provided (first_frame? refs?). Field names
    differ between models — that's the whole reason we wrap them.
    """

    app: str  # the application slug passed to fal_client.subscribe_async
    image_field: Optional[str] = None  # single-image field (i2v / first-frame)
    image_list_field: Optional[str] = None  # multi-image field (refs)
    last_frame_field: Optional[str] = None  # only veo3.1 first-last-frame
    elements_field: Optional[str] = None  # kling V3 character-injection slot


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: Literal["fal", "google"]
    tier: Literal["draft", "standard", "pro"]
    label: str
    # endpoint variants keyed by intent
    t2v: Optional[Endpoint] = None
    i2v: Optional[Endpoint] = None
    r2v: Optional[Endpoint] = None
    first_last: Optional[Endpoint] = None
    # capabilities
    max_seconds: int = 8
    allowed_durations: Optional[list[int]] = None  # None = continuous; otherwise discrete set
    supports_reference_images: bool = False
    max_ref_images: int = 1
    supports_first_frame: bool = False
    supports_audio: bool = True
    duration_as_string: bool = True  # most fal video endpoints take "8" / "8s" / "auto"
    # cost: lower-bound estimate from fal.ai docs at file-write time.
    # The render loop must print these as "estimated — verify on dashboard."
    per_second_cost: float = 0.0


# Endpoint IDs verified against current fal.ai model pages.
# Sources (June 2026 docs snapshot):
#   - fal-ai/veo3.1, fal-ai/veo3.1/image-to-video, fal-ai/veo3.1/first-last-frame-to-video
#   - fal-ai/veo3.1/fast (and /fast/image-to-video)
#   - bytedance/seedance-2.0/{text|image|reference}-to-video and /fast/...
#   - fal-ai/kling-video/v3/{standard|pro}/image-to-video
#   - wan/v2.6/{image-to-video,reference-to-video}
MODEL_REGISTRY: dict[str, ModelSpec] = {
    # ── Veo 3.1 (Google, via fal aggregator) ──────────────────────────────────
    "veo-3.1-fast": ModelSpec(
        id="veo-3.1-fast",
        provider="fal",
        tier="draft",
        label="Veo 3.1 Fast (via fal)",
        t2v=Endpoint(app="fal-ai/veo3.1/fast"),
        i2v=Endpoint(app="fal-ai/veo3.1/fast/image-to-video", image_field="image_url"),
        first_last=Endpoint(
            app="fal-ai/veo3.1/first-last-frame-to-video",  # full-quality only; no /fast variant
            image_field="first_image_url",
            last_frame_field="last_image_url",
        ),
        max_seconds=8,
        allowed_durations=[4, 6, 8],
        supports_reference_images=False,  # /fast endpoints don't accept ref lists
        supports_first_frame=True,
        supports_audio=True,
        per_second_cost=0.10,  # via fal; verify
    ),
    "veo-3.1": ModelSpec(
        id="veo-3.1",
        provider="fal",
        tier="standard",
        label="Veo 3.1 (via fal)",
        t2v=Endpoint(app="fal-ai/veo3.1"),
        i2v=Endpoint(app="fal-ai/veo3.1/image-to-video", image_field="image_url"),
        first_last=Endpoint(
            app="fal-ai/veo3.1/first-last-frame-to-video",
            image_field="first_image_url",
            last_frame_field="last_image_url",
        ),
        max_seconds=8,
        allowed_durations=[4, 6, 8],
        supports_reference_images=False,
        supports_first_frame=True,
        supports_audio=True,
        per_second_cost=0.20,
    ),
    "veo-direct": ModelSpec(
        # Direct Google path via google-genai. Selectable to drop the aggregator markup
        # once Veo wins the bake-off (ARCHITECTURE.md decision #1).
        id="veo-direct",
        provider="google",
        tier="standard",
        label="Veo 3.1 (direct Google AI)",
        max_seconds=8,
        allowed_durations=[4, 6, 8],
        supports_reference_images=False,
        supports_first_frame=True,
        supports_audio=True,
        per_second_cost=0.03,
    ),
    # ── Seedance 2.0 (ByteDance, via fal) ─────────────────────────────────────
    # Seedance 1.5 is superseded by 2.0; we expose both fast + standard.
    "seedance-2.0-fast": ModelSpec(
        id="seedance-2.0-fast",
        provider="fal",
        tier="draft",
        label="Seedance 2.0 Fast (ByteDance, via fal)",
        t2v=Endpoint(app="bytedance/seedance-2.0/fast/text-to-video"),
        i2v=Endpoint(app="bytedance/seedance-2.0/fast/image-to-video", image_field="image_url"),
        r2v=Endpoint(
            app="bytedance/seedance-2.0/fast/reference-to-video",
            image_list_field="image_urls",
        ),
        max_seconds=15,
        allowed_durations=None,  # 4..15 continuous (passed as string)
        supports_reference_images=True,
        max_ref_images=9,
        supports_first_frame=True,  # via i2v endpoint (image_url = seed frame)
        supports_audio=True,
        per_second_cost=0.022,
    ),
    "seedance-2.0": ModelSpec(
        id="seedance-2.0",
        provider="fal",
        tier="pro",
        label="Seedance 2.0 (ByteDance, via fal)",
        t2v=Endpoint(app="bytedance/seedance-2.0/text-to-video"),
        i2v=Endpoint(app="bytedance/seedance-2.0/image-to-video", image_field="image_url"),
        r2v=Endpoint(
            app="bytedance/seedance-2.0/reference-to-video",
            image_list_field="image_urls",
        ),
        max_seconds=15,
        allowed_durations=None,
        supports_reference_images=True,
        max_ref_images=9,
        supports_first_frame=True,
        supports_audio=True,
        per_second_cost=0.05,
    ),
    # ── Kling Video V3 (Kuaishou, via fal) ────────────────────────────────────
    "kling-v3-standard": ModelSpec(
        id="kling-v3-standard",
        provider="fal",
        tier="draft",
        label="Kling 3.0 Standard (Kuaishou, via fal)",
        t2v=Endpoint(app="fal-ai/kling-video/v3/standard/text-to-video"),
        i2v=Endpoint(
            app="fal-ai/kling-video/v3/standard/image-to-video",
            image_field="start_image_url",
            elements_field="elements",
        ),
        max_seconds=15,
        allowed_durations=[3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
        supports_reference_images=True,  # via the `elements` system
        max_ref_images=4,
        supports_first_frame=True,
        supports_audio=True,
        per_second_cost=0.029,
    ),
    "kling-v3-pro": ModelSpec(
        id="kling-v3-pro",
        provider="fal",
        tier="pro",
        label="Kling 3.0 Pro (Kuaishou, via fal)",
        t2v=Endpoint(app="fal-ai/kling-video/v3/pro/text-to-video"),
        i2v=Endpoint(
            app="fal-ai/kling-video/v3/pro/image-to-video",
            image_field="start_image_url",
            elements_field="elements",
        ),
        max_seconds=15,
        allowed_durations=[3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
        supports_reference_images=True,
        max_ref_images=4,
        supports_first_frame=True,
        supports_audio=True,
        per_second_cost=0.13,
    ),
    # ── Wan 2.6 (Alibaba, via fal) ────────────────────────────────────────────
    "wan-2.6": ModelSpec(
        id="wan-2.6",
        provider="fal",
        tier="standard",
        label="Wan 2.6 (Alibaba, via fal)",
        t2v=Endpoint(app="wan/v2.6/text-to-video"),
        i2v=Endpoint(app="wan/v2.6/image-to-video", image_field="image_url"),
        r2v=Endpoint(app="wan/v2.6/reference-to-video", image_list_field="image_urls"),
        max_seconds=15,
        allowed_durations=[5, 10, 15],
        supports_reference_images=True,
        max_ref_images=4,
        supports_first_frame=True,
        supports_audio=True,
        per_second_cost=0.05,
    ),
}


# ──────────────────────────────────────────────────────────────────────────────
# Key / dependency checks
# ──────────────────────────────────────────────────────────────────────────────


class GenerationError(RuntimeError):
    pass


def _provider(model_id: str) -> str:
    spec = MODEL_REGISTRY.get(model_id)
    if spec is None:
        raise GenerationError(
            f"Unknown model '{model_id}'. Known: {sorted(MODEL_REGISTRY.keys())}"
        )
    return spec.provider


def _require_fal_key() -> None:
    if not os.environ.get("FAL_KEY"):
        raise GenerationError(
            "FAL_KEY is not set. Copy .env.example to .env and paste your fal.ai key. "
            "Get one at https://fal.ai/dashboard/keys"
        )


def _require_gemini_key() -> None:
    if not os.environ.get("GEMINI_API_KEY"):
        raise GenerationError(
            "GEMINI_API_KEY is not set — required for model='veo-direct'. "
            "Get one at https://aistudio.google.com/apikey"
        )


# ──────────────────────────────────────────────────────────────────────────────
# fal helpers
# ──────────────────────────────────────────────────────────────────────────────


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "data:"))


async def _to_url(image: str) -> str:
    """Local path -> hosted fal URL (via fal_client.upload_file_async). URLs pass through."""
    if _is_url(image):
        return image
    import fal_client  # local import: only required when fal is the provider

    path = Path(image).expanduser().resolve()
    if not path.exists():
        raise GenerationError(f"image path does not exist: {image}")
    return await fal_client.upload_file_async(path)


async def _download(url: str, dest: Path, timeout: float = 300.0) -> None:
    """Stream a hosted mp4 down to disk."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)


def _video_url_from_result(result: Any) -> str:
    """Most fal video endpoints return {"video": {"url": "..."}}; some return {"url": "..."}.
    Handle both."""
    if isinstance(result, dict):
        v = result.get("video")
        if isinstance(v, dict) and "url" in v:
            return v["url"]
        if isinstance(v, str):
            return v
        if "url" in result and isinstance(result["url"], str):
            return result["url"]
    raise GenerationError(f"Could not find video URL in fal response: {result!r}")


def _pick_endpoint(spec: ModelSpec, first_frame: bool, refs: bool) -> Endpoint:
    """Choose the right endpoint variant for what the caller provided.

    Precedence:
      first_frame -> i2v (or first_last if last is ever supplied — not yet exposed)
      refs only   -> r2v if supported, else single-ref via i2v if exactly 1, else t2v + warn
      neither     -> t2v
    """
    if first_frame:
        if spec.i2v is None:
            raise GenerationError(
                f"Model '{spec.id}' has no image-to-video endpoint; cannot use first_frame_path."
            )
        return spec.i2v
    if refs:
        if spec.r2v is not None:
            return spec.r2v
        if spec.i2v is not None:
            logger.warning(
                "Model '%s' has no reference-to-video endpoint; falling back to image-to-video "
                "with the first reference image only.",
                spec.id,
            )
            return spec.i2v
        if spec.t2v is None:
            raise GenerationError(f"Model '{spec.id}' has no t2v endpoint either.")
        logger.warning(
            "Model '%s' has no image-conditioned endpoint; refs will NOT be used.", spec.id
        )
        return spec.t2v
    if spec.t2v is None:
        raise GenerationError(f"Model '{spec.id}' requires an image input (no t2v endpoint).")
    return spec.t2v


def _validate_duration(spec: ModelSpec, duration_s: int) -> None:
    if duration_s <= 0:
        raise GenerationError(f"duration_s must be > 0, got {duration_s}")
    if duration_s > spec.max_seconds:
        raise GenerationError(
            f"duration_s={duration_s}s exceeds {spec.id} max of {spec.max_seconds}s"
        )
    if spec.allowed_durations is not None and duration_s not in spec.allowed_durations:
        raise GenerationError(
            f"{spec.id} only accepts discrete durations {spec.allowed_durations}, got {duration_s}"
        )


def _format_duration(spec: ModelSpec, duration_s: int) -> Any:
    """Most fal video endpoints take duration as a string ("8" or "8s")."""
    if not spec.duration_as_string:
        return duration_s
    # Veo accepts "8s"; Seedance/Kling/Wan accept "8". Send the bare integer-as-string;
    # endpoints that need "s" coerce it server-side.
    return str(duration_s)


# ──────────────────────────────────────────────────────────────────────────────
# Provider: fal
# ──────────────────────────────────────────────────────────────────────────────


async def _build_fal_args(
    spec: ModelSpec,
    endpoint: Endpoint,
    *,
    prompt: str,
    duration_s: int,
    reference_image_paths: list[str],
    first_frame_path: Optional[str],
    with_audio: bool,
    aspect_ratio: str,
    resolution: str,
    negative_prompt: Optional[str],
    seed: Optional[int],
) -> dict[str, Any]:
    """Translate the generic interface into model-specific arguments."""

    args: dict[str, Any] = {
        "prompt": prompt,
        "duration": _format_duration(spec, duration_s),
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "generate_audio": bool(with_audio),
    }
    if negative_prompt:
        args["negative_prompt"] = negative_prompt
    if seed is not None:
        args["seed"] = seed

    # Image inputs
    if first_frame_path is not None and endpoint.image_field is not None:
        args[endpoint.image_field] = await _to_url(first_frame_path)
    if reference_image_paths and endpoint.image_list_field is not None:
        # Honour max_ref_images
        urls = []
        for p in reference_image_paths[: spec.max_ref_images]:
            urls.append(await _to_url(p))
        args[endpoint.image_list_field] = urls
    elif reference_image_paths and endpoint.image_field is not None and first_frame_path is None:
        # Single-ref fallback via i2v: use the first ref as the image input.
        args[endpoint.image_field] = await _to_url(reference_image_paths[0])

    # Kling V3 supports per-element character injection via `elements` + start_image_url.
    # When BOTH first_frame and refs are present, attach refs as elements.
    if (
        endpoint.elements_field is not None
        and reference_image_paths
        and first_frame_path is not None
    ):
        elements = []
        for p in reference_image_paths[: spec.max_ref_images]:
            elements.append({"image_url": await _to_url(p)})
        args[endpoint.elements_field] = elements

    return args


async def _generate_via_fal(
    spec: ModelSpec,
    *,
    prompt: str,
    output_path: Path,
    duration_s: int,
    reference_image_paths: list[str],
    first_frame_path: Optional[str],
    with_audio: bool,
    aspect_ratio: str,
    resolution: str,
    negative_prompt: Optional[str],
    seed: Optional[int],
    timeout_s: float,
) -> dict[str, Any]:
    _require_fal_key()
    import fal_client  # local import keeps the module importable when fal isn't installed

    endpoint = _pick_endpoint(
        spec,
        first_frame=first_frame_path is not None,
        refs=bool(reference_image_paths),
    )
    args = await _build_fal_args(
        spec,
        endpoint,
        prompt=prompt,
        duration_s=duration_s,
        reference_image_paths=reference_image_paths,
        first_frame_path=first_frame_path,
        with_audio=with_audio,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        negative_prompt=negative_prompt,
        seed=seed,
    )

    logger.info("fal subscribe: app=%s args.keys=%s", endpoint.app, sorted(args.keys()))

    def _on_enqueue(request_id: str) -> None:
        _REQUEST_REGISTRY[request_id] = endpoint.app
        logger.info("fal enqueued: request_id=%s", request_id)

    # subscribe_async polls until the job completes and returns the final result dict.
    # The fal_client library handles retries on transient HTTP errors internally; we add
    # an outer timeout via asyncio.wait_for in case the server itself is slow.
    result = await asyncio.wait_for(
        fal_client.subscribe_async(
            endpoint.app,
            arguments=args,
            on_enqueue=_on_enqueue,
        ),
        timeout=timeout_s,
    )

    video_url = _video_url_from_result(result)
    await _download(video_url, output_path)

    est_cost = round(spec.per_second_cost * duration_s, 4)
    return {
        "status": "done",
        "output_path": str(output_path),
        "seconds": duration_s,
        "model": spec.id,
        "endpoint": endpoint.app,
        "est_cost": est_cost,
        "video_url": video_url,
        "raw": result,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Provider: Veo direct (google-genai)
# ──────────────────────────────────────────────────────────────────────────────


async def _generate_via_veo_direct(
    spec: ModelSpec,
    *,
    prompt: str,
    output_path: Path,
    duration_s: int,
    reference_image_paths: list[str],
    first_frame_path: Optional[str],
    with_audio: bool,
    aspect_ratio: str,
    resolution: str,
    negative_prompt: Optional[str],
    timeout_s: float,
) -> dict[str, Any]:
    """Direct Google AI path. Polls google-genai's long-running operation until done.

    Model name "veo-3.1-generate-preview" is the current Veo 3.1 publisher model on the
    Gemini API as of mid-2026; google-genai resolves it server-side.
    """
    _require_gemini_key()

    # Imports kept local — google-genai is optional for users who only use fal.
    from google import genai
    from google.genai import types as gtypes

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    video_config_kwargs: dict[str, Any] = {
        "aspect_ratio": aspect_ratio,
        "duration_seconds": duration_s,
        "generate_audio": with_audio,
    }
    if negative_prompt:
        video_config_kwargs["negative_prompt"] = negative_prompt

    image_arg = None
    if first_frame_path is not None:
        # Load the seed frame as bytes
        seed_path = Path(first_frame_path).expanduser().resolve()
        if not seed_path.exists():
            raise GenerationError(f"first_frame_path not found: {first_frame_path}")
        mime, _ = mimetypes.guess_type(str(seed_path))
        image_arg = gtypes.Image(
            image_bytes=seed_path.read_bytes(), mime_type=mime or "image/png"
        )

    async def _run() -> bytes:
        # google-genai's video generation is a long-running operation. We start it,
        # then poll until done. The Python SDK exposes both sync and async surfaces;
        # we run the sync calls in a thread to keep the async tool non-blocking.
        loop = asyncio.get_running_loop()

        def _kick() -> Any:
            kw: dict[str, Any] = {
                "model": "veo-3.1-generate-preview",
                "prompt": prompt,
                "config": gtypes.GenerateVideosConfig(**video_config_kwargs),
            }
            if image_arg is not None:
                kw["image"] = image_arg
            return client.models.generate_videos(**kw)

        operation = await loop.run_in_executor(None, _kick)

        # Poll
        while not operation.done:
            await asyncio.sleep(5)
            operation = await loop.run_in_executor(
                None, lambda op=operation: client.operations.get(op)
            )

        if operation.error is not None:
            raise GenerationError(f"Veo direct failed: {operation.error}")

        videos = getattr(operation.response, "generated_videos", None) or []
        if not videos:
            raise GenerationError(f"Veo direct returned no videos: {operation.response!r}")

        # Pull bytes via the file API
        video_file = videos[0].video
        file_bytes = await loop.run_in_executor(None, lambda: client.files.download(file=video_file))
        return file_bytes

    file_bytes = await asyncio.wait_for(_run(), timeout=timeout_s)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(file_bytes)

    est_cost = round(spec.per_second_cost * duration_s, 4)
    return {
        "status": "done",
        "output_path": str(output_path),
        "seconds": duration_s,
        "model": spec.id,
        "endpoint": "google:veo-3.1",
        "est_cost": est_cost,
    }


# ──────────────────────────────────────────────────────────────────────────────
# MCP tools
# ──────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def list_models() -> list[dict[str, Any]]:
    """Return registered video models with their capabilities and a *cost estimate*.

    `per_second_cost` is a lower-bound estimate from fal.ai docs at file-write time.
    Treat it as a planning input only — the render loop should also surface the live
    cost from the dashboard when possible.
    """
    out: list[dict[str, Any]] = []
    for spec in MODEL_REGISTRY.values():
        out.append(
            {
                "id": spec.id,
                "label": spec.label,
                "provider": spec.provider,
                "tier": spec.tier,
                "supports_reference_images": spec.supports_reference_images,
                "max_ref_images": spec.max_ref_images,
                "supports_first_frame": spec.supports_first_frame,
                "max_seconds": spec.max_seconds,
                "allowed_durations": spec.allowed_durations,
                "supports_audio": spec.supports_audio,
                "per_second_cost_estimate_usd": spec.per_second_cost,
                "endpoints": {
                    intent: ep.app
                    for intent, ep in (
                        ("t2v", spec.t2v),
                        ("i2v", spec.i2v),
                        ("r2v", spec.r2v),
                        ("first_last", spec.first_last),
                    )
                    if ep is not None
                },
            }
        )
    return out


@mcp.tool()
async def generate_clip(
    prompt: str,
    output_path: str,
    model: str,
    duration_s: int = 8,
    reference_image_paths: Optional[list[str]] = None,
    first_frame_path: Optional[str] = None,
    with_audio: bool = False,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
    negative_prompt: Optional[str] = None,
    seed: Optional[int] = None,
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Render a single video clip via the chosen model.

    `model` is required (the caller picks per shot/pass — see ARCHITECTURE.md #1).
    Audio defaults OFF; the user's MP3 is muxed at assembly (decision #3).
    Local image paths are auto-uploaded to fal's CDN; http(s) URLs pass through.
    For shots after the first, pass `first_frame_path` (extracted last frame of the
    prior shot) for continuity.
    """
    reference_image_paths = list(reference_image_paths or [])
    spec = MODEL_REGISTRY.get(model)
    if spec is None:
        raise GenerationError(
            f"Unknown model '{model}'. Use list_models() to see the registered set."
        )
    _validate_duration(spec, duration_s)
    if first_frame_path is not None and not spec.supports_first_frame:
        raise GenerationError(f"Model '{model}' does not support first-frame seeding.")
    if reference_image_paths and not spec.supports_reference_images:
        logger.warning(
            "Model '%s' does not support reference images; refs will be dropped (single-ref "
            "fallback may still apply via i2v).",
            model,
        )

    out_path = Path(output_path).expanduser().resolve()

    if spec.provider == "fal":
        return await _generate_via_fal(
            spec,
            prompt=prompt,
            output_path=out_path,
            duration_s=duration_s,
            reference_image_paths=reference_image_paths,
            first_frame_path=first_frame_path,
            with_audio=with_audio,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            negative_prompt=negative_prompt,
            seed=seed,
            timeout_s=timeout_s,
        )
    if spec.provider == "google":
        return await _generate_via_veo_direct(
            spec,
            prompt=prompt,
            output_path=out_path,
            duration_s=duration_s,
            reference_image_paths=reference_image_paths,
            first_frame_path=first_frame_path,
            with_audio=with_audio,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            negative_prompt=negative_prompt,
            timeout_s=timeout_s,
        )
    raise GenerationError(f"Unsupported provider: {spec.provider}")


@mcp.tool()
async def get_status(request_id: str) -> dict[str, Any]:
    """Look up an in-flight fal job's status by request_id.

    Only fal-backed jobs submitted by THIS server are queryable (we store the
    request_id -> endpoint mapping during `generate_clip`). Veo-direct jobs are
    operation-based and don't surface a stable request_id here.
    """
    endpoint = _REQUEST_REGISTRY.get(request_id)
    if endpoint is None:
        return {
            "request_id": request_id,
            "status": "unknown",
            "reason": "no registered endpoint for this request_id (was it submitted by this server?)",
        }
    _require_fal_key()
    import fal_client

    status = await fal_client.status_async(endpoint, request_id, with_logs=True)
    return {
        "request_id": request_id,
        "endpoint": endpoint,
        "status_type": type(status).__name__,
        "status": getattr(status, "__dict__", {}) or str(status),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    # Stdio transport: the MCP client (render loop / Claude Code) speaks via stdin/stdout.
    mcp.run()


if __name__ == "__main__":
    main()
