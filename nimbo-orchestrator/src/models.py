"""Pydantic data contracts + atomic manifest persistence.

These are the *source of truth* types for the whole pipeline (see ARCHITECTURE.md).
Everything downstream — planner, render loop, assembly — reads and writes these.

Built in Prompt 0. No feature logic here; just contracts + safe load/save.
"""

from __future__ import annotations

import json
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────


class ShotStatus(str, Enum):
    """Lifecycle of a single shot. The render loop advances these and the manifest
    is saved after every transition so a killed run can resume."""

    pending = "pending"
    drafting = "drafting"
    drafted = "drafted"
    approved = "approved"
    redo = "redo"  # user marked a draft for re-rendering (treated as renderable by the draft pass)
    rendering = "rendering"
    done = "done"
    failed = "failed"


# ──────────────────────────────────────────────────────────────────────────────
# lyrics.json
# ──────────────────────────────────────────────────────────────────────────────


class LyricSection(BaseModel):
    """One section of the song with absolute timing (seconds from start)."""

    name: str
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    text: str = ""

    @field_validator("end_s")
    @classmethod
    def _end_after_start(cls, v: float, info) -> float:
        start = info.data.get("start_s")
        if start is not None and v < start:
            raise ValueError(f"end_s ({v}) must be >= start_s ({start})")
        return v

    @property
    def duration_s(self) -> float:
        return round(self.end_s - self.start_s, 3)


class Lyrics(BaseModel):
    """Contract for ``lyrics.json``: ``{ sections: [...] }``."""

    sections: list[LyricSection] = Field(default_factory=list)

    @property
    def total_duration_s(self) -> float:
        """Total span covered by the sections (max end - min start)."""
        if not self.sections:
            return 0.0
        return round(
            max(s.end_s for s in self.sections) - min(s.start_s for s in self.sections), 3
        )


# ──────────────────────────────────────────────────────────────────────────────
# character.json
# ──────────────────────────────────────────────────────────────────────────────


class Character(BaseModel):
    """Contract for ``character.json``. Identity is fixed and supplied by the user.

    ``style_lock`` is prepended to EVERY shot prompt; per-shot prompts never redescribe
    appearance (see ARCHITECTURE.md > "Style-lock vs. appearance-free prompts").
    """

    name: str
    ref_images: list[str] = Field(default_factory=list)
    style_lock: str = ""
    negative: str = ""
    palette: list[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Shot (folded into manifest.json)
# ──────────────────────────────────────────────────────────────────────────────


class Shot(BaseModel):
    """A single ~8–15s clip. ``action``/``camera``/``scene`` vary per shot; identity
    comes from ``ref_images`` + the prepended style_lock, never from appearance words."""

    id: str
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    duration_s: float = Field(gt=0)

    # Creative direction (appearance-free for Nimbo)
    scene: str = ""
    camera: str = ""
    action: str = ""
    prompt: str = ""  # built by character.build_prompt(): style_lock + scene/camera/action

    # Identity / continuity
    ref_images: list[str] = Field(default_factory=list)
    seed_from: Optional[str] = None  # shot id whose last frame seeds this shot's first frame

    # Render state (managed by the render loop)
    status: ShotStatus = ShotStatus.pending
    model: Optional[str] = None
    tier: Optional[str] = None
    output_path: Optional[str] = None
    seed_frame_path: Optional[str] = None  # extracted last frame, fed to the NEXT shot
    est_cost: Optional[float] = None
    attempts: int = 0
    error: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────────
# manifest.json
# ──────────────────────────────────────────────────────────────────────────────


class ProjectMeta(BaseModel):
    """Project-level settings + the model/tier choices for each render pass."""

    name: str
    topic: Optional[str] = None
    source: str = "topic"  # "topic" (generate lyrics) | "mp3" (bring-your-own-audio)

    target_duration_s: float = 180.0
    aspect_ratio: str = "16:9"
    resolution: str = "720p"

    # Two-pass cost protection: draft cheap, final only-approved.
    draft_model: Optional[str] = None
    draft_tier: Optional[str] = None
    final_model: Optional[str] = None
    final_tier: Optional[str] = None

    # Key file paths (relative to the project dir)
    song_path: Optional[str] = None
    lyrics_path: str = "lyrics.json"
    character_path: str = "character.json"
    final_path: str = "final.mp4"


class Manifest(BaseModel):
    """Top-level ``manifest.json`` — the source of truth for a project."""

    version: int = 1
    project: ProjectMeta
    shots: list[Shot] = Field(default_factory=list)

    # ---- convenience ---------------------------------------------------------

    def shot_by_id(self, shot_id: str) -> Optional[Shot]:
        return next((s for s in self.shots if s.id == shot_id), None)

    def shots_with_status(self, *statuses: ShotStatus) -> list[Shot]:
        wanted = set(statuses)
        return [s for s in self.shots if s.status in wanted]

    @property
    def covered_duration_s(self) -> float:
        return round(sum(s.duration_s for s in self.shots), 3)


# ──────────────────────────────────────────────────────────────────────────────
# Atomic persistence
# ──────────────────────────────────────────────────────────────────────────────


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically: write to a temp file in the same
    directory, then ``os.replace`` (atomic on POSIX/Windows). Guards against a
    half-written manifest if the process is killed mid-render."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def save_manifest(manifest: Manifest, path: str | os.PathLike) -> None:
    """Persist a manifest atomically as pretty JSON."""
    _atomic_write_text(Path(path), manifest.model_dump_json(indent=2, exclude_none=False))


def load_manifest(path: str | os.PathLike) -> Manifest:
    """Load and validate a manifest from disk."""
    return Manifest.model_validate_json(Path(path).read_text(encoding="utf-8"))


# Generic helpers for the other JSON contracts (lyrics/character), same atomic guarantee.


def save_json_model(model: BaseModel, path: str | os.PathLike) -> None:
    _atomic_write_text(Path(path), model.model_dump_json(indent=2, exclude_none=False))


def load_json_model(model_cls: type[BaseModel], path: str | os.PathLike) -> BaseModel:
    return model_cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
