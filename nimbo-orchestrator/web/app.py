"""Local web frontend (Prompt 8) — FastAPI on 127.0.0.1.

A single-user, single-machine UI that wraps the SAME pipeline functions and reads/writes the
SAME projects/<name>/ directory as the CLI. No deployment, no auth, binds to localhost only.
Anything you can do here you can also do by editing files; the UI is just a friendlier driver
for the four review stops.

Every action — and every NON-action (cost gate held, nothing to render, validation warnings,
skipped shots, errors) — is recorded to an activity log you can toggle to verbose, so a
problem or a "nothing happened" can be narrowed down.

Run:  nimbo-web         (or)  python -m web.app
Then open http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from src import assemble as assemble_mod
from src import lyrics as lyrics_mod
from src import planner as planner_mod
from src import render as render_mod
from src.activity import ActivityLog
from src.character import discover_refs, load_character, validate_character, write_character_json
from src.models import (
    Lyrics,
    LyricSection,
    ShotStatus,
    load_json_model,
    load_manifest,
    save_json_model,
    save_manifest,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATIC = Path(__file__).resolve().parent / "static"

# Statuses the draft pass will (re-)render — used for UI stage logic.
_DRAFT_RENDERABLE = render_mod._DRAFT_RENDERABLE


# ──────────────────────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────────────────────


def create_app(
    projects_root: Optional[Path] = None,
    logs_root: Optional[Path] = None,
    env_path: Optional[Path] = None,
) -> FastAPI:
    projects_root = Path(projects_root or _PROJECT_ROOT / "projects")
    logs_root = Path(logs_root or _PROJECT_ROOT / "logs")
    env_path = Path(env_path or _PROJECT_ROOT / ".env")
    projects_root.mkdir(parents=True, exist_ok=True)

    log = ActivityLog(logs_root)
    jobs: dict[str, dict] = {}  # project -> current background job status

    app = FastAPI(title="Nimbo Orchestrator", docs_url=None, redoc_url=None)
    app.state.projects_root = projects_root
    app.state.log = log
    app.state.jobs = jobs
    app.state.env_path = env_path

    # ── helpers ───────────────────────────────────────────────────────────────
    def _spawn(coro_factory: Callable[[], Coroutine]) -> None:
        """Run a coroutine in its own thread + event loop, independent of the request
        loop. Works the same under uvicorn and the test client."""
        threading.Thread(target=lambda: asyncio.run(coro_factory()), daemon=True).start()

    def pdir(name: str) -> Path:
        p = projects_root / name
        # guard against path traversal
        if ".." in name or "/" in name or "\\" in name:
            raise HTTPException(400, "invalid project name")
        return p

    def require_project(name: str) -> Path:
        p = pdir(name)
        if not p.exists():
            raise HTTPException(404, f"project '{name}' not found")
        return p

    def compute_state(p: Path) -> dict:
        has_lyrics = (p / "lyrics.json").exists()
        manifest = None
        if (p / "manifest.json").exists():
            try:
                manifest = load_manifest(p / "manifest.json")
            except Exception:  # noqa: BLE001
                manifest = None
        has_plan = bool(manifest and manifest.shots)

        stage = "song"  # no lyrics yet (rare in the web flow — create always makes them)
        counts: dict[str, int] = {}
        if has_lyrics and not has_plan:
            stage = "lyrics"  # review lyrics, then build the shot plan
        elif has_plan:
            assert manifest is not None
            for s in manifest.shots:
                counts[s.status.value] = counts.get(s.status.value, 0) + 1
            draft_renderable = [s for s in manifest.shots if s.status in _DRAFT_RENDERABLE]
            approved = any(s.status is ShotStatus.approved for s in manifest.shots)
            done = any(s.status is ShotStatus.done for s in manifest.shots)
            drafted = any(s.status is ShotStatus.drafted for s in manifest.shots)
            if draft_renderable:
                # a brand-new plan (everything still pending) -> shot-plan review;
                # otherwise we're mid/post draft with some shots still to (re)render.
                all_pending = all(s.status is ShotStatus.pending for s in manifest.shots)
                stage = "plan" if all_pending else "draft"
            elif approved:
                stage = "final"
            elif drafted and not done:
                stage = "approval"
            else:
                stage = "done" if (p / "final.mp4").exists() else "assemble"
        return {"stage": stage, "counts": counts, "has_final": (p / "final.mp4").exists()}

    def project_payload(name: str) -> dict:
        p = require_project(name)
        state = compute_state(p)
        out: dict[str, Any] = {"name": name, **state, "job": jobs.get(name)}

        if (p / "lyrics.json").exists():
            lyrics = load_json_model(Lyrics, p / "lyrics.json")
            out["lyrics"] = lyrics.model_dump()
        if (p / "manifest.json").exists():
            m = load_manifest(p / "manifest.json")
            out["project"] = m.project.model_dump()
            out["shots"] = [
                {
                    "id": s.id, "status": s.status.value, "start_s": s.start_s, "end_s": s.end_s,
                    "duration_s": s.duration_s, "scene": s.scene, "camera": s.camera,
                    "action": s.action, "seed_from": s.seed_from, "output_path": s.output_path,
                    "error": s.error,
                }
                for s in m.shots
            ]
        if (p / "character.json").exists():
            ch = load_character(p)
            out["character"] = {"ref_images": ch.ref_images, "palette": ch.palette}
            out["ref_warnings"] = validate_character(ch, p)
        return out

    # ── static + index ────────────────────────────────────────────────────────
    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(_STATIC / "index.html"))

    # ── health / settings ─────────────────────────────────────────────────────
    @app.get("/api/health")
    def health() -> dict:
        return {
            "python": sys.version.split()[0],
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "ffprobe": shutil.which("ffprobe") is not None,
            "keys": {
                "FAL_KEY": bool(os.environ.get("FAL_KEY")),
                "GEMINI_API_KEY": bool(os.environ.get("GEMINI_API_KEY")),
                "SUNO_API_KEY": bool(os.environ.get("SUNO_API_KEY")),
                "ELEVENLABS_API_KEY": bool(os.environ.get("ELEVENLABS_API_KEY")),
            },
            "verbose_logging": log.verbose,
        }

    @app.get("/api/models")
    def models() -> list[dict]:
        return render_mod.gen.list_models()

    @app.get("/api/settings")
    def get_settings() -> dict:
        return {"verbose_logging": log.verbose}

    @app.post("/api/settings")
    async def set_settings(req: Request) -> dict:
        body = await req.json()
        if "verbose_logging" in body:
            log.set_verbose(bool(body["verbose_logging"]))
        keys = body.get("keys") or {}
        if keys:
            _write_env_keys(env_path, {k: v for k, v in keys.items() if v})
            for k, v in keys.items():
                if v:
                    os.environ[k] = v
            log.info("API keys updated", stage="settings", keys=sorted(keys.keys()))
        return {"ok": True, "verbose_logging": log.verbose}

    # ── projects ──────────────────────────────────────────────────────────────
    @app.get("/api/projects")
    def list_projects() -> list[dict]:
        out = []
        for d in sorted(projects_root.iterdir()) if projects_root.exists() else []:
            if d.is_dir():
                try:
                    out.append({"name": d.name, **compute_state(d)})
                except Exception as exc:  # noqa: BLE001
                    out.append({"name": d.name, "stage": "error", "error": str(exc)})
        return out

    @app.post("/api/projects")
    async def create_project(
        name: str = "", topic: str = "", mp3: Optional[UploadFile] = None
    ) -> dict:
        name = (name or "").strip()
        if not name:
            raise HTTPException(400, "project name is required")
        p = pdir(name)
        if (p / "lyrics.json").exists():
            log.warning("create: project already has lyrics", project=name, stage="song")
            return project_payload(name)
        p.mkdir(parents=True, exist_ok=True)
        (p / "refs").mkdir(exist_ok=True)

        try:
            if mp3 is not None:
                dest = p / "song.mp3"
                with dest.open("wb") as f:
                    f.write(await mp3.read())
                lyrics = await run_in_threadpool(lyrics_mod.ingest_mp3, str(dest))
                music = lyrics_mod.build_music_gen_prompt(topic or name, lyrics=lyrics)
                log.info("created project from MP3", project=name, stage="song",
                         sections=len(lyrics.sections))
            elif topic.strip():
                lyrics = await run_in_threadpool(lyrics_mod.generate_lyrics, topic.strip())
                music = lyrics_mod.build_music_gen_prompt(topic.strip(), lyrics=lyrics)
                log.info("created project from topic", project=name, stage="song",
                         topic=topic.strip(), sections=len(lyrics.sections))
            else:
                raise HTTPException(400, "provide a topic or upload an MP3")
            lyrics_mod.write_lyrics_artifacts(lyrics, music, p)
            write_character_json(p)
            # persist the topic so the planner can drive cloud behavior later
            return project_payload(name)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error(f"create project failed: {exc}", project=name, stage="song")
            raise HTTPException(500, str(exc))

    @app.get("/api/projects/{name}")
    def get_project(name: str) -> dict:
        return project_payload(name)

    # ── reference images / song uploads ───────────────────────────────────────
    @app.post("/api/projects/{name}/refs")
    async def upload_ref(name: str, image: UploadFile) -> dict:
        p = require_project(name)
        refs = p / "refs"
        refs.mkdir(exist_ok=True)
        fname = Path(image.filename or "ref.png").name
        with (refs / fname).open("wb") as f:
            f.write(await image.read())
        # refresh character.json refs
        write_character_json(p, None)
        log.info("uploaded reference image", project=name, stage="character", file=fname)
        return project_payload(name)

    @app.post("/api/projects/{name}/song")
    async def upload_song(name: str, mp3: UploadFile) -> dict:
        p = require_project(name)
        with (p / "song.mp3").open("wb") as f:
            f.write(await mp3.read())
        log.info("uploaded song.mp3", project=name, stage="assemble")
        return {"ok": True}

    # ── lyrics ────────────────────────────────────────────────────────────────
    @app.put("/api/projects/{name}/lyrics")
    async def save_lyrics(name: str, req: Request) -> dict:
        p = require_project(name)
        body = await req.json()
        try:
            lyrics = Lyrics(sections=[LyricSection(**s) for s in body["sections"]])
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"invalid lyrics: {exc}")
        save_json_model(lyrics, p / "lyrics.json")
        log.info("lyrics edited & saved", project=name, stage="song",
                 sections=len(lyrics.sections))
        return project_payload(name)

    # ── plan ──────────────────────────────────────────────────────────────────
    @app.post("/api/projects/{name}/plan")
    async def plan(name: str, req: Request) -> dict:
        p = require_project(name)
        body = await req.json() if await _has_body(req) else {}
        topic = (body.get("topic") or "").strip() or None
        model = body.get("model") or None
        try:
            existing = load_manifest(p / "manifest.json") if (p / "manifest.json").exists() else None
            resolved_topic = topic or (existing.project.topic if existing else None) or name
            draft_model = model or planner_mod.DEFAULT_DRAFT_MODEL
            final_model = model or planner_mod.DEFAULT_FINAL_MODEL
            await run_in_threadpool(
                planner_mod.run, p,
                topic=resolved_topic, draft_model=draft_model, final_model=final_model,
                aspect_ratio=planner_mod.DEFAULT_ASPECT_RATIO,
                resolution=planner_mod.DEFAULT_RESOLUTION, save=True,
            )
            m = load_manifest(p / "manifest.json")
            log.info("shot plan built", project=name, stage="plan", shots=len(m.shots),
                     draft_model=draft_model, final_model=final_model)
            return project_payload(name)
        except Exception as exc:  # noqa: BLE001
            log.error(f"plan failed: {exc}", project=name, stage="plan")
            raise HTTPException(500, str(exc))

    # ── cost preview ──────────────────────────────────────────────────────────
    @app.get("/api/projects/{name}/cost")
    def cost(name: str, which: str = "draft") -> dict:
        p = require_project(name)
        m = load_manifest(p / "manifest.json")
        if which == "final":
            model = m.project.final_model
            shots = [s for s in m.shots if s.status is ShotStatus.approved]
        else:
            model = m.project.draft_model
            shots = [s for s in m.shots if s.status in _DRAFT_RENDERABLE]
        breakdown = [
            {"id": s.id, "seconds": render_mod._snap_duration(model, s.duration_s),
             "est_cost": render_mod._estimate(model, s.duration_s)}
            for s in shots
        ]
        total = round(sum(b["est_cost"] for b in breakdown), 4)
        log.debug("cost preview", project=name, stage=which, total=total, shots=len(shots))
        return {"pass": which, "model": model, "total": total, "breakdown": breakdown}

    # ── shot status (approve / redo) ──────────────────────────────────────────
    @app.post("/api/projects/{name}/shots/{shot_id}/status")
    async def set_status(name: str, shot_id: str, req: Request) -> dict:
        p = require_project(name)
        body = await req.json()
        new = body.get("status")
        try:
            status = ShotStatus(new)
        except ValueError:
            raise HTTPException(400, f"invalid status '{new}'")
        m = load_manifest(p / "manifest.json")
        shot = m.shot_by_id(shot_id)
        if shot is None:
            raise HTTPException(404, f"no shot '{shot_id}'")
        old = shot.status.value
        shot.status = status
        save_manifest(m, p / "manifest.json")
        log.info(f"shot {shot_id}: {old} -> {status.value}", project=name, stage="approval",
                 shot=shot_id)
        return {"ok": True, "id": shot_id, "status": status.value}

    # ── render (background job) ───────────────────────────────────────────────
    @app.post("/api/projects/{name}/render")
    async def render(name: str, req: Request) -> dict:
        p = require_project(name)
        body = await req.json() if await _has_body(req) else {}
        which = body.get("pass", "draft")
        confirm = bool(body.get("confirm", False))

        if jobs.get(name, {}).get("status") == "running":
            raise HTTPException(409, "a job is already running for this project")
        if not confirm:
            log.warning(f"{which} render requested without confirm — cost gate held (no spend)",
                        project=name, stage=which)
            return {"started": False, "reason": "confirm required (cost gate)"}

        m = load_manifest(p / "manifest.json")
        renderable = (
            [s for s in m.shots if s.status is ShotStatus.approved] if which == "final"
            else [s for s in m.shots if s.status in _DRAFT_RENDERABLE]
        )
        if not renderable:
            log.warning(f"{which} render: nothing to render (no eligible shots)",
                        project=name, stage=which)
            return {"started": False, "reason": "nothing to render"}

        jobs[name] = {"kind": f"render:{which}", "status": "running", "total": len(renderable),
                      "done": 0, "failed": 0, "skipped": 0, "current": None, "error": None}
        log.info(f"{which} render started ({len(renderable)} shots)", project=name, stage=which)

        def progress(event: str, shot) -> None:
            job = jobs[name]
            if event == "start":
                job["current"] = shot.id
                log.info(f"render {shot.id}: start", project=name, stage=which, shot=shot.id)
            elif event == "done":
                job["done"] += 1
                log.info(f"render {shot.id}: done", project=name, stage=which, shot=shot.id)
            elif event == "failed":
                job["failed"] += 1
                log.warning(f"render {shot.id}: FAILED ({shot.error})", project=name,
                            stage=which, shot=shot.id)
            elif event == "skip":
                job["skipped"] += 1
                log.debug(f"render {shot.id}: skipped (status={shot.status.value})",
                          project=name, stage=which, shot=shot.id)

        async def _job() -> None:
            try:
                fn = render_mod.final_pass if which == "final" else render_mod.draft_pass
                summary = await fn(p, confirm=True, progress=progress)
                jobs[name]["status"] = "done"
                jobs[name]["summary"] = {"rendered": summary.rendered, "failed": summary.failed}
                level = "warning" if summary.failed else "info"
                log.log(level, f"{which} render finished: {len(summary.rendered)} ok, "
                        f"{len(summary.failed)} failed", project=name, stage=which)
            except Exception as exc:  # noqa: BLE001
                jobs[name]["status"] = "error"
                jobs[name]["error"] = str(exc)
                log.error(f"{which} render crashed: {exc}", project=name, stage=which)

        _spawn(_job)
        return {"started": True, "total": len(renderable)}

    # ── assemble (background job) ─────────────────────────────────────────────
    @app.post("/api/projects/{name}/assemble")
    async def assemble(name: str, req: Request) -> dict:
        p = require_project(name)
        body = await req.json() if await _has_body(req) else {}
        captions = bool(body.get("captions", False))
        if jobs.get(name, {}).get("status") == "running":
            raise HTTPException(409, "a job is already running for this project")

        jobs[name] = {"kind": "assemble", "status": "running", "error": None}
        log.info(f"assemble started (captions={captions})", project=name, stage="assemble")

        async def _job() -> None:
            try:
                out = await run_in_threadpool(
                    assemble_mod.assemble, p, captions=captions, dry_run=False
                )
                jobs[name]["status"] = "done"
                jobs[name]["output"] = str(out)
                log.info(f"assembled {out.name}", project=name, stage="assemble")
            except Exception as exc:  # noqa: BLE001
                jobs[name]["status"] = "error"
                jobs[name]["error"] = str(exc)
                log.error(f"assemble failed: {exc}", project=name, stage="assemble")

        _spawn(_job)
        return {"started": True}

    @app.get("/api/projects/{name}/job")
    def job_status(name: str) -> dict:
        require_project(name)
        return jobs.get(name) or {"status": "idle"}

    # ── media ─────────────────────────────────────────────────────────────────
    @app.get("/api/projects/{name}/shots/{shot_id}/video")
    def shot_video(name: str, shot_id: str) -> FileResponse:
        p = require_project(name)
        m = load_manifest(p / "manifest.json")
        shot = m.shot_by_id(shot_id)
        if shot is None or not shot.output_path or not (p / shot.output_path).exists():
            raise HTTPException(404, "clip not found")
        return FileResponse(str(p / shot.output_path), media_type="video/mp4")

    @app.get("/api/projects/{name}/final")
    def final_video(name: str) -> FileResponse:
        p = require_project(name)
        f = p / "final.mp4"
        if not f.exists():
            raise HTTPException(404, "final.mp4 not built yet")
        return FileResponse(str(f), media_type="video/mp4", filename=f"{name}.mp4")

    @app.get("/api/projects/{name}/song")
    def song_audio(name: str) -> FileResponse:
        p = require_project(name)
        f = p / "song.mp3"
        if not f.exists():
            raise HTTPException(404, "song.mp3 not present")
        return FileResponse(str(f), media_type="audio/mpeg")

    # ── activity log ──────────────────────────────────────────────────────────
    @app.get("/api/log")
    def get_log(project: Optional[str] = None, limit: int = 200, level: str = "debug") -> dict:
        return {"verbose": log.verbose, "events": log.events(project=project, limit=limit, min_level=level)}

    @app.delete("/api/log")
    def clear_log() -> dict:
        log.clear()
        log.info("activity log cleared", stage="settings")
        return {"ok": True}

    return app


# ──────────────────────────────────────────────────────────────────────────────
# small utilities
# ──────────────────────────────────────────────────────────────────────────────


async def _has_body(req: Request) -> bool:
    body = await req.body()
    # cache it back so downstream .json() can re-read
    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    req._receive = receive  # type: ignore[attr-defined]
    return bool(body.strip())


def _write_env_keys(env_path: Path, keys: dict[str, str]) -> None:
    """Update/insert KEY=value lines in .env, preserving other lines."""
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    seen = set()
    for i, line in enumerate(lines):
        for k, v in keys.items():
            if line.startswith(f"{k}="):
                lines[i] = f"{k}={v}"
                seen.add(k)
    for k, v in keys.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("NIMBO_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("NIMBO_WEB_PORT", "8000"))
    print(f"\n  Nimbo Orchestrator — open http://{host}:{port}\n")
    try:
        import webbrowser

        webbrowser.open(f"http://{host}:{port}")
    except Exception:  # noqa: BLE001
        pass
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
