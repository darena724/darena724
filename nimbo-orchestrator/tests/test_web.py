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


# ──────────────────────────────────────────────────────────────────────────────
# Model selection
# ──────────────────────────────────────────────────────────────────────────────


def test_models_endpoint_lists_tiers(client):
    ms = client.get("/api/models").json()
    ids = {m["id"] for m in ms}
    assert {"seedance-2.0-fast", "veo-3.1", "kling-v3-pro"} <= ids
    assert all("per_second_cost_estimate_usd" in m and "tier" in m for m in ms)


def _plan(client, name="mv"):
    client.post(f"/api/projects?name={name}&topic=learning the color blue")
    client.post(f"/api/projects/{name}/plan", json={})
    return name


def test_plan_accepts_separate_draft_and_final_models(client):
    client.post("/api/projects?name=mv&topic=learning the color blue")
    p = client.post("/api/projects/mv/plan",
                    json={"draft_model": "kling-v3-standard", "final_model": "kling-v3-pro"}).json()
    assert p["project"]["draft_model"] == "kling-v3-standard"
    assert p["project"]["final_model"] == "kling-v3-pro"


def test_change_final_model_only_preserves_shots(client):
    name = _plan(client)
    before = client.get(f"/api/projects/{name}").json()
    n_before = len(before["shots"])
    r = client.post(f"/api/projects/{name}/models", json={"final_model": "kling-v3-pro"}).json()
    assert r["project"]["final_model"] == "kling-v3-pro"
    assert r["project"]["draft_model"] == "seedance-2.0-fast"  # unchanged
    assert len(r["shots"]) == n_before                          # not re-planned
    assert all(s["status"] == "pending" for s in r["shots"])


def test_change_draft_model_replans(client):
    name = _plan(client)
    before = client.get(f"/api/projects/{name}").json()
    n_before = len(before["shots"])
    # Veo caps clips at 8s vs Seedance 15s -> more shots after re-tiling
    r = client.post(f"/api/projects/{name}/models", json={"draft_model": "veo-3.1-fast"}).json()
    assert r["project"]["draft_model"] == "veo-3.1-fast"
    assert len(r["shots"]) > n_before
    assert all(s["duration_s"] <= 8 + 1e-6 for s in r["shots"])


def test_change_draft_model_with_rendered_shots_needs_force(client):
    name = _plan(client)
    client.post(f"/api/projects/{name}/render", json={"pass": "draft", "confirm": True})
    _wait_idle(client, name)
    # without force -> 409 (would reset render progress)
    r = client.post(f"/api/projects/{name}/models", json={"draft_model": "veo-3.1-fast"})
    assert r.status_code == 409
    # with force -> re-plans and resets to pending
    r2 = client.post(f"/api/projects/{name}/models",
                     json={"draft_model": "veo-3.1-fast", "force": True}).json()
    assert r2["project"]["draft_model"] == "veo-3.1-fast"
    assert all(s["status"] == "pending" for s in r2["shots"])


def test_cap_mismatch_warning(client):
    name = _plan(client)  # draft seedance (15s), final seedance (15s)
    r = client.post(f"/api/projects/{name}/models", json={"final_model": "veo-3.1-fast"}).json()
    assert r["warning"] and "exceed" in r["warning"]


def test_invalid_model_rejected(client):
    name = _plan(client)
    assert client.post(f"/api/projects/{name}/models", json={"final_model": "nope"}).status_code == 400


# ──────────────────────────────────────────────────────────────────────────────
# Reference image upload + serving
# ──────────────────────────────────────────────────────────────────────────────


def _png_bytes():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (245, 240, 230)).save(buf, format="PNG")
    return buf.getvalue()


def test_ref_upload_clears_warning_and_serves(client):
    client.post("/api/projects?name=rp&topic=learning the color blue")
    # before: a missing-ref warning is present
    p = client.get("/api/projects/rp").json()
    assert any("missing" in w for w in p["ref_warnings"])

    # upload a real PNG
    r = client.post(
        "/api/projects/rp/refs",
        files={"image": ("nimbo_modelsheet.png", _png_bytes(), "image/png")},
    )
    assert r.status_code == 200

    # warning cleared, ref discovered, and the image is served back
    p2 = client.get("/api/projects/rp").json()
    assert p2["ref_warnings"] == []
    assert "refs/nimbo_modelsheet.png" in p2["character"]["ref_images"]
    img = client.get("/api/projects/rp/refs/nimbo_modelsheet.png")
    assert img.status_code == 200
    assert img.content[:4] == b"\x89PNG"


def test_ref_image_path_traversal_blocked(client):
    client.post("/api/projects?name=rp2&topic=learning the color blue")
    assert client.get("/api/projects/rp2/refs/..%2F..%2Fsecret").status_code in (400, 404)
