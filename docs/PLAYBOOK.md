# Nimbo Video Orchestrator — Build Playbook (v2)

A local, MCP-driven pipeline that turns a song + the Nimbo character into a finished
~3-minute kids' music video, using a model-agnostic video-generation API.

## Locked decisions (v2)

- **No lip-sync.** Nimbo acts/appears over the song; he does not mouth the lyrics. The
  pipeline is simpler and there is no separate lip-sync model pass.
- **API access confirmed.** Generation runs through a paid video API (not a consumer
  subscription). Default routing through an aggregator (fal.ai or Replicate) so the model
  is swappable; the chosen "workhorse" model can later move to its direct API for savings.
- **Budget: $5–10 per finished 3-min video.** Achievable on a clean pass with Veo-direct
  or Seedance-Fast. Protected by: draft on the cheapest tier, final-render only approved shots.
- **No Veo/model audio.** Generate silent video; the user's MP3 is the only audio, muxed at
  assembly.

## Part 1 — Pressure Test (read before building)

### Verdict

Feasible and a good fit. Local Python project; Claude Code orchestrates; a
provider-abstracted video-generation MCP does the rendering; ffmpeg assembles.

### The risks that remain, and how the design handles them

1. **Subscription ≠ API access.** RESOLVED — using a paid video API with billing.
   Consumer Gemini/Google AI credits are spendable only inside Flow/Whisk and are not
   callable from code.
2. **No model audio — that's the saving.** Generate silent on the cheapest tier; overlay
   the MP3 at assembly. Never pay for native audio.
3. **Consistency = reference images, not prompts (THE quality risk).** ~22 stitched clips.
   Identity is carried by: (a) Nimbo reference images on EVERY call, (b) first-frame
   seeding from the prior clip's last frame, (c) a fixed style string prepended to every
   prompt. Prompts vary only action/camera/scene and must never redescribe Nimbo's
   appearance.
4. **Claude writes lyrics, not audio.** Step 1 outputs lyrics + a section/timing map + a
   music-gen prompt. The MP3 comes from a music API (optional) or a manual drop. MP3 is a
   pluggable input.
5. **Lip-sync.** RESOLVED — not doing it. No extra model pass.

### Bail criteria (when to drop this for a $59/mo wrapper)

- Cross-clip Nimbo consistency stays unacceptable after the tuning order in Part 4.
- Per-episode assembly annoys you more than a subscription would.

## Part 1.5 — Model selection & the bake-off

Do NOT hardwire one model. Build the generation MCP provider-abstracted (default: fal.ai or
Replicate), with `model` as a parameter. Then run a one-afternoon bake-off before committing.

### The bake-off (~$20, do this in Phase 1.5)

Render the SAME single Nimbo shot (same reference images, same action, same 8s) on each of:

- **Veo 3.1** — cheapest first-party; you have the key; 8s clips.
- **Seedance (1.5/2.0 Fast)** — engineered for cross-scene character consistency; up to 9
  reference images per call; 15s clips; no official API (proxy only).
- **Kling 3.0** — strong subject consistency; multi-shot/storyboard inside one 15s clip,
  which reduces stitching.

Judge on: does Nimbo's face/shape/palette hold, and at what cost. Lock the winner. If Veo
wins, switch the workhorse to direct Google API ($0.03/s). If Seedance wins, stay on the proxy.

### Cost comparison (verify at build — prices move weekly)

| Model                   | Cheapest ~$/sec | Clip max | 3-min raw (1 clean pass) | Notes                                               |
| ----------------------- | --------------- | -------- | ------------------------ | --------------------------------------------------- |
| Veo 3.1 (direct Google) | ~$0.03          | 8s       | ~$5.40                   | Cheapest first-party, stable; more stitches         |
| Veo 3.1 (via fal)       | ~$0.10–0.20     | 8s       | ~$18–36                  | Aggregator markup; use direct if Veo wins           |
| Seedance 1.5/2.0 Fast   | ~$0.022–0.05    | 15s      | ~$4–9                    | Best consistency; no first-party API (fal/ModelArk) |
| Kling 3.0               | ~$0.029–0.13    | 15s      | ~$5–23                   | Multi-shot per clip; pricing varies widely          |
| Wan 2.6                 | ~$0.05–0.07     | 10s      | ~$9–13                   | Open-weight, no audio (fine here)                   |
| Sora 2                  | —               | —        | —                        | Discontinued (API dead) — skip                      |

Budget protection: ALWAYS draft on the cheapest tier (Seedance Fast or Veo direct), review,
then final-render only approved shots. Re-rendering everything on a premium tier is what
breaks the $5–10 target.

## Part 2 — Architecture

```
nimbo-orchestrator/
├─ .env                      # FAL_KEY or REPLICATE_API_TOKEN; GEMINI_API_KEY (if Veo direct);
│                            # optional SUNO/ELEVENLABS keys
├─ ARCHITECTURE.md           # decisions, written in Phase 0
├─ pyproject.toml
├─ mcp/
│  └─ gen_server.py          # provider-abstracted MCP: generate_clip(model=...), get_status, list_models
├─ src/
│  ├─ lyrics.py              # Phase 2: lyrics + section/timing map
│  ├─ character.py           # Phase 3: Nimbo config + style lock
│  ├─ planner.py             # Phase 4: song+character -> shots.json
│  ├─ render.py              # Phase 5: resumable render loop
│  ├─ assemble.py            # Phase 6: ffmpeg concat + mux + captions
│  └─ pipeline.py            # Phase 7: end-to-end w/ review stops + cost gate
└─ projects/
   └─ blue-song/
      ├─ manifest.json       # source of truth; per-shot status for resume
      ├─ character.json
      ├─ lyrics.json
      ├─ song.mp3
      ├─ refs/               # canonical Nimbo reference images
      ├─ shots/             # shot_01.mp4 ...
      └─ final.mp4
```

### Key data contracts

- `manifest.json`: project meta + ordered shot list, each with `status`
  (pending | drafting | drafted | approved | rendering | done | failed), file paths,
  model + tier used per shot.
- `lyrics.json`: `{ sections: [{ name, start_s, end_s, text }] }`.
- `character.json`: `{ name, ref_images:[...], style_lock:"...", negative:"...", palette:[...] }`.
- `shots.json` (folded into manifest): each
  `{ id, start_s, end_s, duration_s, scene, camera, action, prompt, ref_images, seed_from }`.

**Why a manifest**: a multi-clip render WILL have partial failures. Everything keys off the
manifest so re-running resumes instead of regenerating (and re-paying for) finished shots.

## Part 3 — The Claude Code Prompt Series

Run in order. Each is a standalone paste. Each ends with acceptance criteria.

### PROMPT 0 — Scaffold + Architecture Decision Record

You are setting up a local Python project called "nimbo-orchestrator": a pipeline that turns
a song + a fixed character ("Nimbo") into a ~3-minute kids' music video using a video-
generation API (provider-abstracted) and ffmpeg for assembly.

Before writing any feature code:
1. Create the folder structure below and a README explaining each part.
2. Set up the project with uv (or venv+pip), Python 3.11+. Deps: the MCP SDK (mcp/fastmcp),
   the aggregator client (fal-client OR replicate), google-genai (for optional Veo-direct),
   python-dotenv, pydantic, ffmpeg-python. Verify ffmpeg + ffprobe are installed; if not,
   print install instructions and stop.
3. Write .env.example with FAL_KEY (or REPLICATE_API_TOKEN), GEMINI_API_KEY (optional, for
   Veo direct), and optional SUNO_API_KEY / ELEVENLABS_API_KEY.
4. Define pydantic models for manifest.json, lyrics.json, character.json, Shot. Use the data
   contracts above. Add an atomic manifest load/save helper (write temp, rename).
5. Write ARCHITECTURE.md capturing these LOCKED decisions:
   - Video generation goes through a paid API, provider-abstracted, with `model` as a
     parameter (default aggregator: fal.ai or Replicate). The winning model can later move
     to its direct API for cost.
   - NO lip-sync. Nimbo acts over the song; he does not mouth lyrics.
   - Video is generated SILENT on the cheapest tier; the user's MP3 is the only audio, muxed
     at assembly.
   - Character consistency = reference images on every call + first-frame seeding + a fixed
     style string; prompts control only action/camera/scene.
   - Budget target $5–10/video, protected by draft-cheap then final-only-approved.
   - The manifest is the source of truth; the render loop is resumable and idempotent.

DO NOT write the generation client, planner, or render loop yet. Stop after scaffold + ADR.

**Acceptance:** project installs cleanly, the aggregator client imports, ffmpeg/ffprobe
verified, ARCHITECTURE.md exists, manifest helper round-trips a sample file.

### PROMPT 1 — Provider-abstracted Generation MCP

Build a local MCP server at mcp/gen_server.py that wraps a video-generation aggregator
(default: fal.ai; allow Replicate as an alternative) and exposes these tools:

- `list_models()` -> available video models with metadata: {id, provider, tier,
  supports_reference_images, max_ref_images, max_seconds, per_second_cost, supports_audio}.
  Include at minimum: Veo 3.1 (and Lite/Fast), Seedance 1.5/2.0 (Fast + Pro), Kling 3.0,
  Wan 2.6.
- `generate_clip(prompt, output_path, model, duration_s=8, reference_image_paths=[],
  first_frame_path=None, with_audio=False, aspect_ratio="16:9", resolution="720p")` ->
  {status, output_path, seconds, model, est_cost}. Submits the job to the chosen model via
  the aggregator, polls to completion, downloads the mp4, returns metadata. `model` is
  REQUIRED so the caller picks per shot/pass.
- `get_status(job_id)` -> in-flight status.

IMPORTANT:
- I am not certain of current model IDs, the fal/Replicate endpoint names, or each model's
  reference-image / first-frame parameter names. Look them up in the CURRENT fal.ai (or
  Replicate) docs and each model's reference — do not guess from memory. If a model does not
  support reference images or first-frame seeding, surface that in list_models() so the
  planner can avoid relying on it.
- Default to audio OFF (we mute and overlay the user's MP3). Make tier/model selectable.
- Read keys from .env. Fail loudly if the key/billing is missing.
- Exponential backoff on rate limits; hard timeout per job.
- Also implement an OPTIONAL direct-Google path for Veo via google-genai, selectable with
  model="veo-direct", so once Veo is chosen as the workhorse we can drop the aggregator markup.

**Acceptance:** list_models() returns real current IDs + costs; generate_clip with one
reference image on each of Veo, Seedance, and Kling produces a playable silent clip; the
veo-direct path works with GEMINI_API_KEY.

### PROMPT 1.5 — Model bake-off harness

Write a small script scripts/bakeoff.py that renders ONE identical Nimbo test shot across a
list of models (default: Veo 3.1, Seedance Fast, Kling 3.0) using the same reference images,
same action prompt, same duration, same aspect ratio. Save outputs side by side as
bakeoff/<model>.mp4 and print a table of {model, seconds, est_cost, output_path}.

Purpose: let me eyeball which model holds Nimbo's identity best and at what cost, then lock the
workhorse. Keep total spend small (one short clip per model).

**Acceptance:** running it produces one clip per model plus a cost table; total spend is
roughly a few dollars.

### PROMPT 2 — Song step (lyrics + timing) and MP3 ingestion

Implement src/lyrics.py with two paths:

A) Generate-from-scratch (you, Claude Code, do this directly — no external LLM call):
   Given a topic (e.g., "learning the color blue"), produce kids'-song lyrics for a ~3-minute
   toddler song: gentle, repetitive, one core concept reinforced. Output lyrics.json with
   sections [{name, start_s, end_s, text}] whose timings sum to the target duration. Also write
   music_gen_prompt.txt (genre/tempo/mood) for a music generator.

B) Bring-your-own-audio:
   Accept song.mp3. Use ffprobe for true duration. If lyric text is provided, fit sections to
   the real duration; else create generic markers.

Optional: if SUNO_API_KEY / ELEVENLABS is present, add generate_song() that sends
music_gen_prompt.txt to that API and saves song.mp3, behind a flag. The pipeline MUST work
with a manually supplied MP3 if no music API is configured.

Validate: song.mp3 exists; duration matches lyrics.json total within 2s (warn otherwise).

**Acceptance:** the "blue" topic yields lyrics.json + music_gen_prompt.txt; a supplied MP3 has
its duration read and sections fitted.

### PROMPT 3 — Nimbo character config + style lock

Implement src/character.py. No LLM calls — the character is fixed and supplied by me.

- Define character.json for "Nimbo": ref_images (paths in projects/<p>/refs/), style_lock (a
  fixed string prepended to EVERY shot prompt), negative (never-render list), palette (hex).
- I will paste the Nimbo style_lock and negative prompt below; store them verbatim.
- Validate each ref image: exists, is an image, ideally single clear subject on clean
  background (warn, best-effort).
- Provide build_prompt(shot) -> style_lock + scene/action/camera from the shot, always passing
  ref_images. The action text must NOT redescribe Nimbo's appearance — identity comes from refs,
  not words. Strip appearance adjectives for Nimbo.

**Acceptance:** character.json validates; refs checked; build_prompt() always leads with the
locked style and contains no Nimbo appearance descriptors.

### PROMPT 4 — Shot planner (analyze song + character -> shots)

Implement src/planner.py. You (Claude Code) read lyrics.json + character.json and produce an
ordered shot list folded into manifest.json.

Rules:
- Split the full song duration into shots of <= the chosen model's clip cap (8s for Veo, 15s
  for Seedance/Kling — read this from list_models() for the locked model), aligned to lyric
  section boundaries where possible. Cover the ENTIRE duration, no gaps/overlaps.
- For each shot: {id, start_s, end_s, duration_s, scene, camera, action, prompt, ref_images,
  seed_from}. prompt = build_prompt(shot).
- `action` = what Nimbo DOES + environment/mood for that lyric moment — never appearance. Vary
  scenes to match sections; keep tone gentle/calm.
- `seed_from`: for shot N>1, the prior shot's last frame (render loop extracts it) for
  continuity. Shot 1 seeds from a canonical Nimbo ref.
- Every shot: status="pending", model+tier = the cheapest draft option.

Print a human-readable shot table for review before saving.

**Acceptance:** shots cover the full duration, each within the model clip cap, prompts
style-locked and appearance-free, seed_from chain correct, manifest saved.

### PROMPT 5 — Resumable render loop

Implement src/render.py. Iterate manifest shots and generate each via the generation MCP.

- DRAFT pass: render every pending shot on the cheapest model/tier to projects/<p>/shots/.
  Attach Nimbo ref_images on every call. If shot has seed_from, first extract the last frame of
  the referenced clip (ffmpeg) and pass it as first_frame_path.
- Update each shot's status as it progresses (drafting -> drafted | failed). Save manifest after
  EACH shot (resumability). On re-run, skip drafted/approved/done shots.
- Retry failed shots up to 3x with backoff; leave failed and continue (don't abort the run).
- FINAL pass (separate function): only shots I marked status="approved", re-render on the locked
  final model/tier.
- Before any pass, print estimated cost (sum durations x rate) and require an explicit confirm
  flag to proceed.

**Acceptance:** a draft pass populates shots/; manifest reflects per-shot status; kill+rerun
resumes without regenerating finished shots; cost gate fires before spend.

### PROMPT 6 — Assembly (ffmpeg)

Implement src/assemble.py.

- Concatenate shot clips in manifest order into one silent video (re-encode to uniform
  codec/fps/resolution to avoid concat artifacts).
- Mux in projects/<p>/song.mp3 as the ONLY audio (discard any model audio). Trim/pad video to
  match song length exactly.
- Optional captions: with --captions, burn in subtitles from lyrics.json section text, timed to
  section start/end, in a large rounded kid-friendly font near the lower third (replaces
  MagicLight's auto-subtitles for word recognition).
- Export projects/<p>/final.mp4 at the target aspect ratio and resolution.

**Acceptance:** final.mp4 duration == song duration (±0.5s); audio is ONLY the user's MP3;
Nimbo present across the whole runtime; captions (when on) legible and timed.

### PROMPT 7 — End-to-end pipeline with review stops + guardrails

Implement src/pipeline.py with a CLI:
  `make-video --project blue-song [--topic "color blue" | --mp3 path] [--model <locked>] [--captions]`

Flow with mandatory human-review stops:
1. lyrics/song step -> STOP, show lyrics.json + timings for approval.
2. shot plan -> STOP, print shot table for approval.
3. DRAFT render (cheapest) -> STOP, list draft clips; I mark shots "approved" or "redo".
4. FINAL render (locked model/tier) of approved shots.
5. assemble -> final.mp4.

Guardrails:
- Print total estimated cost before steps 3 and 4; require --yes to proceed.
- Every step reads/writes the manifest so any step re-runs independently.
- --dry-run prints the full plan + cost with zero API calls.

**Acceptance:** --dry-run shows plan + cost with no spend; a full run with approvals produces
projects/blue-song/final.mp4 with Nimbo throughout and only the user's MP3 as audio.

## Part 4 — Operating notes

- **Do the bake-off (Prompt 1.5) before anything else expensive.** One Nimbo shot across
  Veo/Seedance/Kling settles the consistency-vs-cost question empirically.
- **Always draft on the cheapest tier.** The expensive mistake is rendering all shots on a
  premium tier before confirming Nimbo holds.
- **Consistency tuning order** if Nimbo drifts: (1) cleaner reference images, (2) confirm
  first-frame seeding is chaining, (3) shorter shots, (4) less scene variety per shot, (5) only
  then touch the prompt.
- **Verify model IDs / SDK names / endpoint params at build time.** They change; the prompts
  tell Claude Code to look them up rather than trust stale strings.
- **Lock the workhorse, then optimize.** Once one model handles ~80% of shots well, move it to
  its direct API (Veo via Google = $0.03/s) to shed the aggregator markup. Seedance has no
  first-party API, so if it wins you stay on a proxy.
- **Keep music pluggable.** Drop your own MP3 or wire Suno/ElevenLabs; the video stage doesn't
  care how the MP3 was made.

### PROMPT 8 — Local web frontend (queued; built after Prompt 7)

Build a **local-only** web UI that wraps the same `make-video` CLI / pipeline functions
and reads/writes the exact same `projects/<name>/` directory. No deployment, no auth, no
external hosting — runs entirely on the user's machine, outputs land in the local
filesystem next to the CLI's outputs.

Stack: **FastAPI + uvicorn**, served on `http://127.0.0.1:8000`. Single-page UI with
plain HTML/CSS/JS (or HTMX for partial updates) — no SPA framework needed.

Surface the four mandatory review stops from Prompt 7 as discrete pages:
1. **Project picker / new project** — list `projects/*`, "+ New" creates a folder + seeds
   `manifest.json` from a topic or an MP3 path.
2. **Lyrics review** — editable `lyrics.json` (sections, timings, text); save + continue.
3. **Shot plan review** — table of shots; edit `scene` / `camera` / `action`; save +
   continue.
4. **Draft review** — grid of thumbnails (extracted via ffmpeg from each `shots/shot_*.mp4`)
   with an inline `<video>` preview; per-shot **Approve / Redo** buttons that write
   `status` to the manifest.
5. **Final assembly** — cost confirm + Render button → tail log → play `final.mp4`.

Backend endpoints (thin shells over `src/`):
- `GET  /api/projects`
- `POST /api/projects` (create) · `GET /api/projects/{name}` (manifest)
- `POST /api/projects/{name}/lyrics` (regenerate or save edits)
- `POST /api/projects/{name}/plan` (run shot planner)
- `POST /api/projects/{name}/render?pass=draft|final&yes=true` (kicks the render loop;
  streams progress via SSE)
- `POST /api/projects/{name}/shots/{id}/status` (approved | redo)
- `POST /api/projects/{name}/assemble`
- `GET  /api/projects/{name}/shots/{id}/video`  (serves the local mp4)
- `GET  /api/projects/{name}/final`             (serves final.mp4)

Constraints:
- Binds to `127.0.0.1` only — never `0.0.0.0`. Single-user, single-machine.
- No upload to external services; all media stays in `projects/<name>/`.
- The CLI continues to work standalone; the web UI is a thin wrapper around the same
  manifest. Anything you can do in the UI you can also do by editing files.

Acceptance: `nimbo-web` command starts uvicorn; opening `127.0.0.1:8000` lets a user run
a full project end-to-end (topic or MP3 → lyrics → plan → draft → approve → final) without
touching the terminal beyond the start command. Outputs match what the CLI would produce.

<!-- Additional playbook sections will be appended as provided. -->





