"""Tests for the local web frontend (Prompt 8).

Uses FastAPI's TestClient with render/assemble mocked (no network, no ffmpeg, no spend).
Validates: project create -> lyrics -> plan -> draft (cost gate + render) -> approve ->
final -> assemble, plus the activity log and the verbose toggle.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import assemble as assemble_mod
from src import render as render_mod
from web import app as webapp


@pytest.fixture
def client(tmp_path, monkeypatch):
    # mock the money/ffmpeg bits
    async def _fake_generate(**kwargs):
        out = Path(kwargs["output_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00FAKE")
        return {"status": "done", "output_path": str(out), "seconds": kwargs["duration_s"],
                "model": kwargs["model"], "est_cost": 0.1}

    monkeypatch.setattr(render_mod.gen, "generate_clip", _fake_generate)
    monkeypatch.setattr(render_mod, "extract_last_frame", lambda c, o: Path(o))

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(render_mod, "_sleep", _no_sleep)

    def _fake_assemble(project_dir, *, captions=False, dry_run=False, **kw):
        out = Path(project_dir) / "final.mp4"
        out.write_bytes(b"\x00FINAL")
        return out

    monkeypatch.setattr(assemble_mod, "assemble", _fake_assemble)

    app = webapp.create_app(
        projects_root=tmp_path / "projects",
        logs_root=tmp_path / "logs",
        env_path=tmp_path / ".env",
    )
    return TestClient(app)


def _wait_idle(client, name, timeout=5.0):
    """Poll the job endpoint until the background render/assemble task finishes."""
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/projects/{name}/job").json()
        if j.get("status") in (None, "idle", "done", "error"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


# ──────────────────────────────────────────────────────────────────────────────
# Health / settings / log
# ──────────────────────────────────────────────────────────────────────────────


def test_health(client):
    h = client.get("/api/health").json()
    assert "ffmpeg" in h and "keys" in h
    assert "FAL_KEY" in h["keys"]


def test_verbose_toggle_and_log(client):
    # off by default; debug events are dropped
    assert client.get("/api/settings").json()["verbose_logging"] is False
    client.post("/api/settings", json={"verbose_logging": True})
    assert client.get("/api/settings").json()["verbose_logging"] is True
    # the toggle itself is logged (info)
    events = client.get("/api/log?level=info").json()["events"]
    assert any("verbose logging ON" in e["message"] for e in events)


def test_keys_written_to_env(client, tmp_path):
    client.post("/api/settings", json={"keys": {"FAL_KEY": "fal_test123"}})
    env = (tmp_path / ".env").read_text()
    assert "FAL_KEY=fal_test123" in env
    assert client.get("/api/health").json()["keys"]["FAL_KEY"] is True


# ──────────────────────────────────────────────────────────────────────────────
# Full flow
# ──────────────────────────────────────────────────────────────────────────────


def test_create_project_from_topic_stops_at_lyrics(client):
    r = client.post("/api/projects?name=blue-song&topic=learning the color blue")
    assert r.status_code == 200
    p = r.json()
    assert p["stage"] == "lyrics"   # lyrics created -> review them, then build the plan
    assert p["lyrics"]["sections"]
    # logged
    ev = client.get("/api/log?level=info&project=blue-song").json()["events"]
    assert any("created project from topic" in e["message"] for e in ev)


def test_full_pipeline_through_assemble(client):
    # 1. create (lyrics)
    client.post("/api/projects?name=blue-song&topic=learning the color blue")
    # 2. plan -> shot-table review (all shots pending)
    p = client.post("/api/projects/blue-song/plan", json={}).json()
    assert p["stage"] == "plan"
    assert len(p["shots"]) > 0

    # 3a. cost gate: render without confirm -> nothing happens, warning logged
    r = client.post("/api/projects/blue-song/render", json={"pass": "draft", "confirm": False}).json()
    assert r["started"] is False
    warns = client.get("/api/log?level=warning&project=blue-song").json()["events"]
    assert any("cost gate held" in e["message"] for e in warns)

    # 3b. render drafts for real
    r = client.post("/api/projects/blue-song/render", json={"pass": "draft", "confirm": True}).json()
    assert r["started"] is True
    _wait_idle(client, "blue-song")

    p = client.get("/api/projects/blue-song").json()
    assert p["stage"] == "approval"
    assert all(s["status"] == "drafted" for s in p["shots"])

    # 4. approve two shots
    ids = [p["shots"][0]["id"], p["shots"][-1]["id"]]
    for sid in ids:
        client.post(f"/api/projects/blue-song/shots/{sid}/status", json={"status": "approved"})

    p = client.get("/api/projects/blue-song").json()
    assert p["stage"] == "final"

    # final render
    r = client.post("/api/projects/blue-song/render", json={"pass": "final", "confirm": True}).json()
    assert r["started"] is True
    _wait_idle(client, "blue-song")

    p = client.get("/api/projects/blue-song").json()
    # approved shots are now done; the rest still drafted -> stage assemble
    done = [s for s in p["shots"] if s["status"] == "done"]
    assert len(done) == 2
    assert p["stage"] == "assemble"

    # 5. assemble
    client.post("/api/projects/blue-song/assemble", json={"captions": True})
    _wait_idle(client, "blue-song")
    p = client.get("/api/projects/blue-song").json()
    assert p["has_final"] is True
    assert p["stage"] == "done"

    # final.mp4 is served
    assert client.get("/api/projects/blue-song/final").status_code == 200


def test_render_nothing_to_render_is_logged(client):
    client.post("/api/projects?name=p2&topic=learning the color blue")
    client.post("/api/projects/p2/plan", json={})
    # final pass with no approved shots -> nothing to render
    r = client.post("/api/projects/p2/render", json={"pass": "final", "confirm": True}).json()
    assert r["started"] is False and r["reason"] == "nothing to render"
    warns = client.get("/api/log?level=warning&project=p2").json()["events"]
    assert any("nothing to render" in e["message"] for e in warns)


def test_redo_returns_to_draft_stage(client):
    client.post("/api/projects?name=p3&topic=learning the color blue")
    client.post("/api/projects/p3/plan", json={})
    client.post("/api/projects/p3/render", json={"pass": "draft", "confirm": True})
    _wait_idle(client, "p3")
    p = client.get("/api/projects/p3").json()
    sid = p["shots"][0]["id"]
    client.post(f"/api/projects/p3/shots/{sid}/status", json={"status": "redo"})
    p = client.get("/api/projects/p3").json()
    assert p["stage"] == "draft"  # a redo shot makes the draft pass eligible again


def test_lyrics_edit_saved(client):
    client.post("/api/projects?name=p4&topic=learning the color blue")
    p = client.get("/api/projects/p4").json()
    secs = p["lyrics"]["sections"]
    secs[0]["text"] = "Brand new intro line"
    r = client.put("/api/projects/p4/lyrics", json={"sections": secs}).json()
    assert r["lyrics"]["sections"][0]["text"] == "Brand new intro line"


def test_cost_endpoint(client):
    client.post("/api/projects?name=p5&topic=learning the color blue")
    client.post("/api/projects/p5/plan", json={})
    c = client.get("/api/projects/p5/cost?which=draft").json()
    assert c["total"] > 0
    assert len(c["breakdown"]) > 0
    assert c["model"] == "seedance-2.0-fast"


def test_invalid_project_name_rejected(client):
    r = client.get("/api/projects/..%2Fetc")
    assert r.status_code in (400, 404)


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Nimbo" in r.text
