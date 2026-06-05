# Architecture Decision Record — Nimbo Orchestrator

Status: **Locked (Phase 0)**. These decisions are intentionally fixed; changing one means
revisiting the pressure test (see `../docs/PLAYBOOK.md`).

## What this is

A local Python pipeline that turns **a song + the fixed "Nimbo" character** into a finished
**~3-minute kids' music video**. Claude Code orchestrates; a provider-abstracted
video-generation MCP renders the clips; ffmpeg assembles them and muxes the audio.

## Locked decisions

1. **Provider-abstracted video generation.** All generation goes through a *paid* video API,
   wrapped behind a single MCP (`mcp/gen_server.py`) with `model` as a **required parameter**.
   Default aggregator is **fal.ai** (Replicate as an alternative). The winning "workhorse"
   model can later move to its **direct API** (e.g. Veo via Google ≈ $0.03/s) to shed the
   aggregator markup — that's a config change, not a rewrite.

2. **No lip-sync.** Nimbo *acts/appears over* the song; he does **not** mouth the lyrics. No
   separate lip-sync model pass. Simpler pipeline, lower cost.

3. **Silent video + user MP3 is the only audio.** Clips are generated **silent** on the
   cheapest tier (`with_audio=False`). The user's MP3 is the sole audio track, **muxed at
   assembly**. We never pay for native model audio.

4. **Character consistency = images, not prose.** Nimbo's identity is carried by:
   (a) **reference images on every call**, (b) **first-frame seeding** from the prior clip's
   last frame, and (c) a **fixed `style_lock` string** prepended to every prompt. Per-shot
   prompts vary only **action / camera / scene** and must never redescribe Nimbo's appearance.

5. **Budget target $5–10 per finished video.** Protected by a two-pass flow: **draft on the
   cheapest tier**, review, then **final-render only approved shots** on the locked model/tier.
   Cost is estimated and gated (`--yes`) before any spend.

6. **Manifest is the source of truth; the render loop is resumable + idempotent.** A multi-clip
   render *will* have partial failures. Every shot's `status`, paths, and model/tier live in
   `manifest.json`, saved after each shot. Re-running **resumes** instead of regenerating (and
   re-paying for) finished shots.

## Component map

| Component            | File                  | Built in | Responsibility                                           |
| -------------------- | --------------------- | -------- | -------------------------------------------------------- |
| Data models + helper | `src/models.py`       | Prompt 0 | Pydantic contracts + atomic manifest load/save           |
| Generation MCP       | `mcp/gen_server.py`   | Prompt 1 | `list_models` / `generate_clip` / `get_status`, provider-abstracted |
| Bake-off harness     | `scripts/bakeoff.py`  | Prompt 1.5 | Same shot across Veo/Seedance/Kling → pick the workhorse |
| Song step            | `src/lyrics.py`       | Prompt 2 | Lyrics + timing map, or ingest a supplied MP3            |
| Character + style    | `src/character.py`    | Prompt 3 | Nimbo `character.json`, `build_prompt()` (appearance-free actions) |
| Shot planner         | `src/planner.py`      | Prompt 4 | Song + character → ordered shots folded into manifest    |
| Render loop          | `src/render.py`       | Prompt 5 | Resumable draft/final passes via the MCP, cost gate      |
| Assembly             | `src/assemble.py`     | Prompt 6 | ffmpeg concat + mux MP3 + optional captions → final.mp4  |
| Pipeline CLI         | `src/pipeline.py`     | Prompt 7 | End-to-end with mandatory review stops + guardrails      |

## Data contracts (implemented in `src/models.py`)

- **`manifest.json`** — project meta + ordered shot list. Each shot has a `status`
  (`pending | drafting | drafted | approved | rendering | done | failed`), file paths, and the
  `model`/`tier` used.
- **`lyrics.json`** — `{ sections: [{ name, start_s, end_s, text }] }`.
- **`character.json`** — `{ name, ref_images:[...], style_lock, negative, palette:[...] }`.
- **`Shot`** (folded into the manifest) —
  `{ id, start_s, end_s, duration_s, scene, camera, action, prompt, ref_images, seed_from }`
  plus render state (`status`, `model`, `tier`, `output_path`, …).

## Style-lock vs. appearance-free prompts

The character bible's *episode template* keeps a short identity anchor
("small round cream plush-like creature, …"). We resolve this against Prompt 3's
"no appearance redescription" rule by **splitting concerns**:

- The fixed **`style_lock`** (prepended to every prompt) carries the short identity anchor +
  art-direction.
- The per-shot **`action`** stays strictly appearance-free: *what Nimbo does* + setting + what
  the cloud does + topic.

Identity is still carried primarily by the reference images on every call.
