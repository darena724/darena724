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

<!-- Additional playbook sections will be appended as provided. -->



