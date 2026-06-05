# nimbo-orchestrator

A local, MCP-driven pipeline that turns **a song + the fixed "Nimbo" character** into a
finished **~3-minute kids' music video**, using a **provider-abstracted** video-generation API
and **ffmpeg** for assembly.

- No lip-sync — Nimbo acts/appears over the song.
- Silent clips generated on the cheapest tier; the user's **MP3 is the only audio**, muxed at
  assembly.
- Character consistency via **reference images + first-frame seeding + a fixed style lock**.
- Budget target **$5–10/video**: draft cheap → review → final-render only approved shots.
- The **manifest is the source of truth**; the render loop is resumable and idempotent.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the locked decisions and the build playbook in
[`../docs/PLAYBOOK.md`](../docs/PLAYBOOK.md).

## Project layout

```
nimbo-orchestrator/
├─ .env.example             # FAL_KEY or REPLICATE_API_TOKEN; GEMINI_API_KEY (Veo direct); optional SUNO/ELEVENLABS
├─ ARCHITECTURE.md          # locked decisions (ADR)
├─ pyproject.toml
├─ mcp/
│  └─ gen_server.py         # [Prompt 1] provider-abstracted MCP: generate_clip(model=...), get_status, list_models
├─ scripts/
│  ├─ doctor.py             # verify Python / ffmpeg / ffprobe / deps
│  └─ bakeoff.py            # [Prompt 1.5] one shot across Veo/Seedance/Kling -> pick workhorse
├─ src/
│  ├─ models.py             # [Prompt 0] pydantic data contracts + atomic manifest load/save
│  ├─ lyrics.py             # [Prompt 2] lyrics + section/timing map (or MP3 ingestion)
│  ├─ character.py          # [Prompt 3] Nimbo config + style lock + build_prompt()
│  ├─ planner.py            # [Prompt 4] song+character -> shots.json (folded into manifest)
│  ├─ render.py             # [Prompt 5] resumable draft/final render loop
│  ├─ assemble.py           # [Prompt 6] ffmpeg concat + mux + captions
│  └─ pipeline.py           # [Prompt 7] end-to-end CLI w/ review stops + cost gate
├─ tests/                   # manifest round-trip + contract tests
└─ projects/
   └─ blue-song/
      ├─ manifest.json      # source of truth; per-shot status for resume
      ├─ character.json
      ├─ lyrics.json
      ├─ song.mp3
      ├─ refs/              # canonical Nimbo reference images
      ├─ shots/             # shot_01.mp4 ...
      └─ final.mp4
```

> **Build status:** Prompt 0 complete (scaffold + ADR + data contracts). Prompts 1–7 are
> stubbed and implemented in order — see the `[Prompt N]` tags above.

## Setup

Requires **Python 3.11+** and **ffmpeg/ffprobe** on PATH.

```bash
# 1. install deps (uv recommended)
uv sync                      # or:  python -m venv .venv && . .venv/bin/activate && pip install -e .

# 2. configure keys
cp .env.example .env         # then fill in FAL_KEY (or REPLICATE_API_TOKEN), etc.

# 3. verify the environment
python scripts/doctor.py     # checks Python, ffmpeg/ffprobe, and deps; prints fixes if missing

# 4. run tests
pytest
```

## Data contracts

Defined in [`src/models.py`](src/models.py):

- **`manifest.json`** — project meta + ordered shots (each with `status`, paths, model/tier).
- **`lyrics.json`** — `{ sections: [{ name, start_s, end_s, text }] }`.
- **`character.json`** — `{ name, ref_images, style_lock, negative, palette }`.

`save_manifest()` writes atomically (temp file + `os.replace`) so a killed render never leaves
a half-written manifest.
