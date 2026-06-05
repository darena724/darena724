"""Activity log (for the web UI's logging toggle).

A tiny structured logger that records what the app DID and — just as importantly — what it
DIDN'T do ("non-actions": cost gate held, nothing to render, validation warnings, skipped
shots). Events are appended as JSON lines so they're easy to tail, filter, and diff when
something goes wrong.

Levels: debug < info < warning < error. DEBUG events are only recorded when verbose logging
is toggled on; everything info+ is always recorded. The verbose flag is persisted so it
survives a server restart.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}


class ActivityLog:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "activity.jsonl"
        self.settings_file = self.root / "settings.json"
        self._lock = threading.Lock()
        self.verbose = self._load_verbose()

    # ── settings ────────────────────────────────────────────────────────────────
    def _load_verbose(self) -> bool:
        try:
            return bool(json.loads(self.settings_file.read_text()).get("verbose_logging", False))
        except (FileNotFoundError, json.JSONDecodeError):
            return False

    def set_verbose(self, value: bool) -> None:
        with self._lock:
            self.verbose = bool(value)
            data = {}
            try:
                data = json.loads(self.settings_file.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            data["verbose_logging"] = self.verbose
            self.settings_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # record the toggle itself so the timeline explains a change in verbosity
        self.log("info", f"verbose logging {'ON' if value else 'OFF'}", stage="settings")

    # ── writing ─────────────────────────────────────────────────────────────────
    def log(
        self,
        level: str,
        message: str,
        *,
        project: Optional[str] = None,
        stage: Optional[str] = None,
        **details: Any,
    ) -> Optional[dict]:
        level = level.lower()
        if level not in _LEVELS:
            level = "info"
        if level == "debug" and not self.verbose:
            return None
        rec = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "level": level,
            "project": project,
            "stage": stage,
            "message": message,
            "details": details or {},
        }
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            with self.file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        return rec

    def debug(self, message: str, **kw: Any) -> Optional[dict]:
        return self.log("debug", message, **kw)

    def info(self, message: str, **kw: Any) -> Optional[dict]:
        return self.log("info", message, **kw)

    def warning(self, message: str, **kw: Any) -> Optional[dict]:
        return self.log("warning", message, **kw)

    def error(self, message: str, **kw: Any) -> Optional[dict]:
        return self.log("error", message, **kw)

    # ── reading ─────────────────────────────────────────────────────────────────
    def events(
        self,
        *,
        project: Optional[str] = None,
        limit: int = 200,
        min_level: str = "debug",
    ) -> list[dict]:
        """Return the most recent events (newest last), filtered by project + min level."""
        threshold = _LEVELS.get(min_level.lower(), 10)
        if not self.file.exists():
            return []
        with self._lock:
            lines = self.file.read_text(encoding="utf-8").splitlines()
        out: list[dict] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if project is not None and rec.get("project") not in (project, None):
                continue
            if _LEVELS.get(rec.get("level", "info"), 20) < threshold:
                continue
            out.append(rec)
        return out[-limit:]

    def clear(self) -> None:
        with self._lock:
            if self.file.exists():
                self.file.unlink()
