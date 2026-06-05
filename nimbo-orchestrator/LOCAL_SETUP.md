# Running nimbo-orchestrator on your local machine

> **Prefer not to use the terminal?** There's a local **web UI** — jump to
> [Using the web UI](#using-the-web-ui-no-cli-needed). You still run one command once to
> start it, then everything happens in your browser.

The pipeline can be driven two ways: the **web UI** (recommended if you're not comfortable
with the command line) or the **CLI**. Both do the exact same thing and share the same
`projects/<name>/` files.

## Using the web UI (no CLI needed)

After the one-time setup (steps 1–5 below: install Python, ffmpeg, deps), start the UI:

```bash
# from the nimbo-orchestrator folder, run ONE of these:
./scripts/start-web.sh           # macOS/Linux (double-clickable in Finder/Files)
# or
python -m web.app
# or, if installed:
nimbo-web
```

Then open **http://127.0.0.1:8000** in your browser. Everything else is point-and-click:

1. **⚙ Settings** → paste your **FAL_KEY** (and optionally GEMINI/Suno/ElevenLabs). It's saved
   to your local `.env`. The Settings panel also shows whether ffmpeg is installed.
2. **New project** → type a name + a topic (e.g. *learning the color red*) **or** upload an
   MP3. Click **Create**.
3. **Review lyrics** → edit any line, **Save**, then **build shot plan**.
4. **Review the shot plan** → **Render drafts** (you'll see the estimated cost and confirm).
   Progress shows live; watch the **📋 Activity Log** for details.
5. **Review drafts** → each shot plays inline. Click **✓ Approve** (keep, re-render at higher
   quality) or **↻ Redo** (re-draft). Then **Final render**, or **Assemble drafts as-is**.
6. **Assemble** → optional captions toggle → **final.mp4** plays in the browser with a
   **Download** button.

### Activity log + diagnosing problems

Click **📋 Activity Log** (top right) any time. It shows a live, timestamped record of every
action — *and every non-action*: cost gates held, "nothing to render", skipped shots,
validation warnings, and errors. So if something doesn't happen when you expect, the log
tells you why.

Flip **Verbose** (in the log panel or in Settings) to also record low-level DEBUG events
(per-shot skips, cost previews) for deeper troubleshooting. The setting is remembered. If you
ever need to share what went wrong, the log lives at `logs/activity.jsonl`.

> The web server binds to `127.0.0.1` only — it's not reachable from other machines.

---

## Using the CLI

Interaction happens in your **terminal + a video player**:

1. you run `make-video --project <name> --topic "..."` (or `--mp3 <path>`)
2. the pipeline **pauses at four review stops**; you inspect files and confirm with `--yes`
3. output lands at `projects/<name>/final.mp4`

> Want a web UI later? It's an additive scope — a small local FastAPI page that shells out
> to the same CLI and shows shot thumbnails + an approve/redo button. Out of scope for the
> v2 playbook, but easy to bolt on after Prompt 7. Say the word.

> **Status check.** As of this writing only Prompt 0 (scaffold + ADR) is done.
> The `make-video` CLI itself is built in **Prompt 7**. Everything below describes the
> **target** workflow so you can prep your machine and your assets now.

---

## 1. Prerequisites (install once)

| Need               | Why                                              | How                                          |
| ------------------ | ------------------------------------------------ | -------------------------------------------- |
| Python **3.11+**   | pipeline runtime                                 | macOS: `brew install python@3.11` · Windows: from python.org |
| **ffmpeg + ffprobe** | concat clips, mux MP3, extract seed frames     | macOS: `brew install ffmpeg` · Ubuntu: `sudo apt-get install ffmpeg` · Windows: `winget install Gyan.FFmpeg` |
| **uv** (recommended) | fast deps install                              | `curl -LsSf https://astral.sh/uv/install.sh \| sh`  (pip works too) |
| **git**            | clone the repo                                   | usually preinstalled                         |
| A video player     | review draft clips before approving             | VLC, QuickTime, or any                       |

API accounts you'll need:
- **fal.ai** with billing enabled — get `FAL_KEY` from <https://fal.ai/dashboard/keys>
- *(optional)* **Google AI Studio** key (`GEMINI_API_KEY`) if you want the Veo-direct path
- *(optional)* **Suno** or **ElevenLabs** key if you want music generated; the pipeline
  works fine with a manually-supplied MP3 instead.

## 2. Clone the repo

```bash
git clone https://github.com/darena724/darena724.git
cd darena724

# while development is ongoing, the project lives on this branch:
git checkout claude/music-video-orchestrator-repo-BYdlJ

cd nimbo-orchestrator
```

(Once we merge to main, the `git checkout` step goes away.)

## 3. Install Python deps

```bash
uv venv --python 3.11
uv pip install -e .

# or with stock pip:
python3.11 -m venv .venv && source .venv/bin/activate && pip install -e .
```

## 4. Configure your keys

```bash
cp .env.example .env
# open .env in your editor and paste:
#   FAL_KEY=<your-fal-key>
#   GEMINI_API_KEY=<optional, only for veo-direct>
#   SUNO_API_KEY=<optional>
#   ELEVENLABS_API_KEY=<optional>
```

The `.env` file is git-ignored — your keys never leave your machine.

## 5. Verify your environment

```bash
python scripts/doctor.py
```

You should see ✓ next to Python, ffmpeg, ffprobe, and every Python dep. If anything
is ✗, the doctor prints the exact install command. Re-run until clean.

## 6. Drop in Nimbo's reference images

Render the Nimbo model sheet (the image-gen prompt is in
[`../docs/CHARACTER_BIBLE.md`](../docs/CHARACTER_BIBLE.md)) on your tool of choice
(Midjourney / Imagen / Whisk) and save:

```
projects/blue-song/refs/nimbo_modelsheet.png       # primary reference
projects/blue-song/refs/nimbo_3q.png               # optional 3/4 view
projects/blue-song/refs/nimbo_side.png             # optional side
```

These are passed on **every** clip render. Cleaner refs = better consistency. If Nimbo
drifts across shots, the consistency-tuning order (Part 4 of the playbook) tells you to
fix refs first, **before** touching prompts.

## 7. (Optional) Drop in your own song MP3

If you want to bring your own audio instead of having Claude write lyrics from a topic:

```bash
cp ~/Downloads/blue-song.mp3 projects/blue-song/song.mp3
```

Then run with `--mp3 projects/blue-song/song.mp3` instead of `--topic`.

## 8. Run the pipeline

```bash
# topic-driven (Claude writes lyrics)
make-video --project blue-song --topic "learning the color blue"

# OR bring-your-own-audio
make-video --project blue-song --mp3 projects/blue-song/song.mp3
```

You'll hit **four review stops**:

| Stop | What you see                                            | What you do                                                                 |
| ---- | ------------------------------------------------------- | --------------------------------------------------------------------------- |
| 1    | `lyrics.json` + timing map                              | Open it in your editor. Tweak words/timings if you want, save, then re-run with `--yes`. |
| 2    | Shot plan table (scene/camera/action per shot)          | Inspect, edit `manifest.json` if needed, re-run with `--yes`.               |
| 3    | Estimated cost for the **draft** pass + list of pending shots | Re-run with `--yes` to spend (drafts on the cheapest tier — typically $1–3 for a 3-min song). |
| 4    | Draft clips in `projects/blue-song/shots/`              | Watch each `shot_*.mp4`. Open `manifest.json`, set each shot's `status` to `"approved"` (keep it) or `"redo"` (will be regenerated). Re-run; the pipeline does the **final** pass only on approved shots, then assembles. |

Cost is printed and **gated by `--yes`** before steps 3 and 4. Use `--dry-run` to see the
full plan and cost with **zero** API spend.

## 9. Find your output

```
projects/blue-song/final.mp4
```

Play it in VLC / QuickTime. Optional captions (large kid-friendly font in the lower third)
are off by default; add `--captions` to burn them in.

## 10. Iterating

Everything is keyed off `manifest.json`. Want to redo just a couple of shots?
- Set their `status` to `"redo"` in `manifest.json`
- Re-run `make-video --project blue-song --yes`
- The render loop **resumes**: it skips finished shots and only re-renders the redos.

Kill the process mid-render? Same thing — re-run and it picks up where it left off.

## Assembly (ffmpeg) — running it manually

The render loop (Prompt 5) and assembly (Prompt 6) are the two steps that need the **ffmpeg
binary** on your machine — they can't run in the cloud sandbox, so here's the manual flow.

```bash
# 0. confirm ffmpeg + ffprobe are installed
python scripts/doctor.py        # both must be ✓

# 1. plan the shots (writes manifest.json)
python -m src.planner --project blue-song --topic "learning the color blue"

# 2. DRAFT render — prints the cost first; nothing is spent until you add --yes
python -m src.render --project blue-song --pass draft           # cost preview only
python -m src.render --project blue-song --pass draft --yes     # actually renders

#    clips land in projects/blue-song/shots/shot_*.mp4 — watch them in VLC.
#    To approve a shot for the final pass, set its "status" to "approved" in manifest.json.
#    (Leave the rest as "drafted"; only approved shots get the pricier final render.)

# 3. FINAL render of approved shots (optional — drafts are fine to assemble too)
python -m src.render --project blue-song --pass final --yes

# 4. assemble — preview the exact ffmpeg command without running it:
python -m src.assemble --project blue-song --captions --dry-run
#    then run it for real:
python -m src.assemble --project blue-song --captions
```

Output: `projects/blue-song/final.mp4` — play it in VLC/QuickTime.

**What assembly does**, so you can eyeball/verify it:
- Concatenates the shot clips **in manifest order**, re-encoding each to a uniform
  `1280x720 @ 24fps` (from your `resolution`/`aspect_ratio`) so there are no concat glitches.
- Muxes `song.mp3` as the **only** audio — any sound the model put on the clips is dropped.
- Forces `final.mp4` to the **song's exact length**: the video holds its last frame if the
  clips run short, then both streams are trimmed with `-t`. So Nimbo is on screen the whole time.
- With `--captions`, burns the lyric lines (timed per section) in a large rounded font in the
  lower third, generated as `captions.ass`.

**Captions font.** The default is `Comic Sans MS` (rounded, kid-friendly). If it's not
installed, libass falls back to a system default. To use another, pass `--font "Baloo 2"`
(or any installed family). On Linux, install a rounded font and refresh the cache:
`sudo apt-get install fonts-comic-neue && fc-cache -f`.

**Verify the result quickly:**
```bash
ffprobe -v error -show_entries format=duration -of csv=p=0 projects/blue-song/final.mp4
# should match your song length within ~0.5s
ffprobe -v error -select_streams a -show_entries stream=codec_name -of csv=p=0 \
  projects/blue-song/final.mp4        # should be 'aac' (your muxed MP3)
```

## Troubleshooting

| Symptom                                | Cause / fix                                                            |
| -------------------------------------- | ---------------------------------------------------------------------- |
| `ffmpeg: command not found`            | step 1; doctor will show the install command for your OS               |
| `KeyError: FAL_KEY`                    | step 4 — paste the key into `.env` (not into your shell)               |
| Nimbo's face drifts across clips       | refresh refs (step 6) → confirm seeding chain (`seed_from` in manifest) → shorten shots → less scene variety — only **then** touch prompts |
| One shot fails repeatedly              | leave its status as `failed`, mark a sibling `approved`, re-run; the render loop retries 3× with backoff and continues past failures |
| Costs creeping above target            | check `manifest.json`: are you on the **draft** tier? Final-pass only **approved** shots — don't approve all of them. |
