"""Acceptance test for Prompt 0: the manifest helper round-trips a sample file."""

from __future__ import annotations

from pathlib import Path

from src.models import (
    Lyrics,
    LyricSection,
    Manifest,
    ProjectMeta,
    Shot,
    ShotStatus,
    load_manifest,
    save_manifest,
)


def _sample_manifest() -> Manifest:
    return Manifest(
        project=ProjectMeta(
            name="blue-song",
            topic="learning the color blue",
            source="topic",
            target_duration_s=180.0,
            draft_model="seedance-fast",
            draft_tier="draft",
            final_model="veo-direct",
            final_tier="final",
            song_path="song.mp3",
        ),
        shots=[
            Shot(
                id="shot_01",
                start_s=0.0,
                end_s=8.0,
                duration_s=8.0,
                scene="soft meadow with negative space",
                camera="slow push-in",
                action="Nimbo waves hello as the cloud glows blue",
                prompt="<style_lock> ... Nimbo waves hello ...",
                ref_images=["refs/nimbo_front.png"],
                seed_from=None,
                status=ShotStatus.pending,
            ),
            Shot(
                id="shot_02",
                start_s=8.0,
                end_s=16.0,
                duration_s=8.0,
                scene="calm sky",
                camera="static",
                action="Nimbo points up at a blue bird",
                seed_from="shot_01",
                status=ShotStatus.drafted,
                model="seedance-fast",
                tier="draft",
                output_path="shots/shot_02.mp4",
                est_cost=0.18,
            ),
        ],
    )


def test_manifest_roundtrip(tmp_path: Path) -> None:
    original = _sample_manifest()
    path = tmp_path / "manifest.json"

    save_manifest(original, path)
    assert path.exists()

    loaded = load_manifest(path)

    # Full structural equality after a save/load cycle.
    assert loaded.model_dump() == original.model_dump()
    assert loaded.covered_duration_s == 16.0
    assert loaded.shot_by_id("shot_02").status is ShotStatus.drafted
    assert [s.id for s in loaded.shots_with_status(ShotStatus.pending)] == ["shot_01"]


def test_lyrics_contract_roundtrip(tmp_path: Path) -> None:
    lyrics = Lyrics(
        sections=[
            LyricSection(name="intro", start_s=0, end_s=12, text="Blue, blue, the sky is blue"),
            LyricSection(name="verse1", start_s=12, end_s=40, text="Blueberries in my bowl"),
        ]
    )
    path = tmp_path / "lyrics.json"
    path.write_text(lyrics.model_dump_json(indent=2), encoding="utf-8")

    loaded = Lyrics.model_validate_json(path.read_text(encoding="utf-8"))
    assert loaded.total_duration_s == 40.0
    assert loaded.sections[0].duration_s == 12.0
