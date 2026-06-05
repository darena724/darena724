"""Provider-abstracted video-generation MCP server.

TODO(Prompt 1): implement a FastMCP server exposing:
  - list_models() -> [{id, provider, tier, supports_reference_images, max_ref_images,
                       max_seconds, per_second_cost, supports_audio}, ...]
  - generate_clip(prompt, output_path, model, duration_s=8, reference_image_paths=[],
                  first_frame_path=None, with_audio=False, aspect_ratio="16:9",
                  resolution="720p") -> {status, output_path, seconds, model, est_cost}
  - get_status(job_id)

Default aggregator: fal.ai (Replicate alternative). Optional model="veo-direct" via
google-genai. Verify CURRENT model IDs / endpoint params against live docs at build time —
do not hardcode from memory. Audio defaults OFF.

NOTE: this folder is intentionally NOT a package (no __init__.py) so it never shadows the
`mcp` SDK. Run it as a script:  python mcp/gen_server.py
"""

from __future__ import annotations
