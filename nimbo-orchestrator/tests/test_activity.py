"""Tests for the activity log (Prompt 8 logging)."""

from __future__ import annotations

from src.activity import ActivityLog


def test_info_always_recorded(tmp_path):
    log = ActivityLog(tmp_path)
    log.info("hello", project="p", stage="song", n=3)
    events = log.events()
    assert len(events) == 1
    e = events[0]
    assert e["level"] == "info" and e["message"] == "hello"
    assert e["project"] == "p" and e["stage"] == "song"
    assert e["details"] == {"n": 3}


def test_debug_dropped_unless_verbose(tmp_path):
    log = ActivityLog(tmp_path)
    log.debug("noisy")
    assert log.events() == []          # dropped while not verbose
    log.set_verbose(True)
    log.debug("now visible")
    msgs = [e["message"] for e in log.events()]
    assert "now visible" in msgs
    # the toggle itself was logged
    assert any("verbose logging ON" in m for m in msgs)


def test_verbose_persists_across_instances(tmp_path):
    ActivityLog(tmp_path).set_verbose(True)
    assert ActivityLog(tmp_path).verbose is True


def test_level_filter(tmp_path):
    log = ActivityLog(tmp_path)
    log.info("i"); log.warning("w"); log.error("e")
    assert {e["message"] for e in log.events(min_level="warning")} == {"w", "e"}
    assert {e["message"] for e in log.events(min_level="error")} == {"e"}


def test_project_filter_includes_global(tmp_path):
    log = ActivityLog(tmp_path)
    log.info("global", project=None)
    log.info("for-a", project="a")
    log.info("for-b", project="b")
    msgs = {e["message"] for e in log.events(project="a")}
    assert msgs == {"global", "for-a"}   # global (project=None) shown to every project


def test_clear(tmp_path):
    log = ActivityLog(tmp_path)
    log.info("x")
    log.clear()
    assert log.events() == []


def test_limit(tmp_path):
    log = ActivityLog(tmp_path)
    for i in range(10):
        log.info(f"m{i}")
    last3 = [e["message"] for e in log.events(limit=3)]
    assert last3 == ["m7", "m8", "m9"]
