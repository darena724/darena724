"""Tests for the shot planner (Prompt 4).

Acceptance criteria, each covered:
  - shots cover the FULL duration with no gaps/overlaps
  - each shot is within the locked model's clip cap
  - prompts are style-locked (lead with style_lock) and appearance-free
  - the seed_from chain is correct (shot 1 -> ref, shot N -> shot N-1)
  - the manifest is saved
"""

from __future__ import annotations

import math
import re

import pytest

from src import character as C
from src import planner as P
from src.models import Lyrics, LyricSection, ShotStatus, load_manifest


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────


def _blue_lyrics() -> Lyrics:
    """The real blue-song section layout (8 sections, 180s)."""
    bounds = [
        ("intro", 0.0, 16.9),
        ("verse_1", 16.9, 46.1),
        ("chorus_1", 46.1, 68.6),
        ("verse_2", 68.6, 97.9),
        ("chorus_2", 97.9, 120.4),
        ("bridge", 120.4, 139.5),
        ("chorus_3", 139.5, 162.0),
        ("outro", 162.0, 180.0),
    ]
    return Lyrics(
        sections=[LyricSection(name=n, start_s=s, end_s=e, text=f"{n} text") for n, s, e in bounds]
    )


@pytest.fixture
def character():
    return C.default_character(ref_images=["refs/nimbo_modelsheet.png"])


# ──────────────────────────────────────────────────────────────────────────────
# Tiling primitive
# ──────────────────────────────────────────────────────────────────────────────


def test_tile_section_covers_exactly_no_gaps():
    slots = P._tile_section(16.9, 46.1, cap=15)
    # contiguous + exact coverage
    assert slots[0][0] == 16.9
    assert slots[-1][1] == 46.1
    for (s, e), (ns, ne) in zip(slots, slots[1:]):
        assert e == ns  # no gaps / overlaps
    # each within cap
    assert all(e - s <= 15 + 1e-6 for s, e in slots)


def test_tile_section_fewest_slots():
    # 29.2s with cap 15 -> 2 slots (not 3)
    assert len(P._tile_section(16.9, 46.1, cap=15)) == 2
    # 16.9s with cap 15 -> 2 slots
    assert len(P._tile_section(0.0, 16.9, cap=15)) == 2
    # 10s with cap 15 -> 1 slot
    assert len(P._tile_section(0.0, 10.0, cap=15)) == 1


def test_tile_section_veo_cap_8():
    slots = P._tile_section(0.0, 16.9, cap=8)
    assert len(slots) == 3  # ceil(16.9/8)
    assert all(e - s <= 8 + 1e-6 for s, e in slots)


# ──────────────────────────────────────────────────────────────────────────────
# Full coverage / clip cap
# ──────────────────────────────────────────────────────────────────────────────


def test_plan_covers_full_song_no_gaps(character):
    lyrics = _blue_lyrics()
    shots = P.plan_shots(lyrics, character, topic="learning the color blue",
                         draft_model="seedance-2.0-fast")

    assert shots[0].start_s == 0.0
    assert shots[-1].end_s == pytest.approx(180.0)
    # contiguous
    for prev, nxt in zip(shots, shots[1:]):
        assert nxt.start_s == pytest.approx(prev.end_s)
    # covered duration == song duration
    covered = sum(s.duration_s for s in shots)
    assert covered == pytest.approx(180.0, abs=0.05)


def test_every_shot_within_clip_cap(character):
    lyrics = _blue_lyrics()
    cap = P._clip_cap_seconds("seedance-2.0-fast")  # 15
    shots = P.plan_shots(lyrics, character, topic="learning the color blue",
                         draft_model="seedance-2.0-fast")
    assert all(s.duration_s <= cap + 1e-6 for s in shots)


def test_every_shot_within_veo_cap(character):
    """If the locked model is Veo (8s cap), shots must respect it."""
    lyrics = _blue_lyrics()
    shots = P.plan_shots(lyrics, character, topic="learning the color blue",
                         draft_model="veo-3.1-fast")
    assert all(s.duration_s <= 8 + 1e-6 for s in shots)
    # Veo's 8s cap -> more shots than Seedance's 15s cap
    seed_shots = P.plan_shots(lyrics, character, topic="learning the color blue",
                              draft_model="seedance-2.0-fast")
    assert len(shots) > len(seed_shots)


def test_shots_do_not_cross_section_boundaries(character):
    lyrics = _blue_lyrics()
    boundaries = {s.start_s for s in lyrics.sections} | {s.end_s for s in lyrics.sections}
    shots = P.plan_shots(lyrics, character, topic="learning the color blue",
                         draft_model="seedance-2.0-fast")
    for sh in shots:
        # each shot must sit entirely within one section
        containing = [
            sec for sec in lyrics.sections
            if sec.start_s - 1e-6 <= sh.start_s and sh.end_s <= sec.end_s + 1e-6
        ]
        assert containing, f"{sh.id} ({sh.start_s}-{sh.end_s}) crosses a section boundary"


# ──────────────────────────────────────────────────────────────────────────────
# Prompts: style-locked + appearance-free
# ──────────────────────────────────────────────────────────────────────────────


def test_every_prompt_leads_with_style_lock(character):
    lyrics = _blue_lyrics()
    shots = P.plan_shots(lyrics, character, topic="learning the color blue",
                         draft_model="seedance-2.0-fast")
    for sh in shots:
        assert sh.prompt.startswith(character.style_lock.strip())


def test_no_shot_action_or_prompt_has_appearance_descriptors(character):
    lyrics = _blue_lyrics()
    shots = P.plan_shots(lyrics, character, topic="learning the color blue",
                         draft_model="seedance-2.0-fast")

    def _has_term(text: str, term: str) -> bool:
        # whole-word match, same semantics as strip_appearance (so "rounded"/"around"
        # don't count as the appearance word "round")
        return re.search(rf"\b{re.escape(term)}\b", text, flags=re.IGNORECASE) is not None

    for sh in shots:
        # the per-shot portion (after style_lock) must be appearance-free
        suffix = sh.prompt[len(character.style_lock.strip()):]
        for term in C._APPEARANCE_TERMS:
            assert not _has_term(suffix, term), f"{sh.id}: '{term}' leaked into prompt"
        # action itself is appearance-free too
        for term in C._APPEARANCE_TERMS:
            assert not _has_term(sh.action, term), f"{sh.id}: '{term}' in action"


def test_color_song_cloud_glows_color(character):
    lyrics = _blue_lyrics()
    shots = P.plan_shots(lyrics, character, topic="learning the color blue",
                         draft_model="seedance-2.0-fast")
    # every action references the cloud glowing blue
    assert all("cloud glows a clear, gentle blue" in s.action for s in shots)


def test_animal_song_cloud_forms_animal_face(character):
    lyrics = _blue_lyrics()
    shots = P.plan_shots(lyrics, character, topic="animal song about the cat",
                         draft_model="seedance-2.0-fast")
    assert all("forms a simple, cute cat face" in s.action for s in shots)


def test_scenes_vary_across_sections_camera_varies(character):
    lyrics = _blue_lyrics()
    shots = P.plan_shots(lyrics, character, topic="learning the color blue",
                         draft_model="seedance-2.0-fast")
    # at least a few distinct scenes and cameras across the plan
    assert len({s.scene for s in shots}) >= 4
    assert len({s.camera for s in shots}) >= 3
    # first/last are hello/goodbye bookends
    assert "hello" in shots[0].action
    assert "goodbye" in shots[-1].action


# ──────────────────────────────────────────────────────────────────────────────
# seed_from chain
# ──────────────────────────────────────────────────────────────────────────────


def test_seed_from_chain_is_correct(character):
    lyrics = _blue_lyrics()
    shots = P.plan_shots(lyrics, character, topic="learning the color blue",
                         draft_model="seedance-2.0-fast")
    assert shots[0].seed_from is None  # shot 1 -> canonical ref
    for i in range(1, len(shots)):
        assert shots[i].seed_from == shots[i - 1].id


def test_all_shots_pending_with_draft_model(character):
    lyrics = _blue_lyrics()
    shots = P.plan_shots(lyrics, character, topic="learning the color blue",
                         draft_model="seedance-2.0-fast")
    assert all(s.status is ShotStatus.pending for s in shots)
    assert all(s.model == "seedance-2.0-fast" for s in shots)
    assert all(s.tier == "draft" for s in shots)
    assert all(s.ref_images == ["refs/nimbo_modelsheet.png"] for s in shots)


def test_ids_are_zero_padded_and_ordered(character):
    lyrics = _blue_lyrics()
    shots = P.plan_shots(lyrics, character, topic="learning the color blue",
                         draft_model="seedance-2.0-fast")
    ids = [s.id for s in shots]
    assert ids == sorted(ids)  # zero-padded -> lexical order == numeric order
    assert ids[0] == "shot_01"


# ──────────────────────────────────────────────────────────────────────────────
# End-to-end run + manifest save
# ──────────────────────────────────────────────────────────────────────────────


def _setup_project(tmp_path):
    pdir = tmp_path / "blue-song"
    (pdir / "refs").mkdir(parents=True)
    # clean ref so validation is happy
    from PIL import Image

    Image.new("RGB", (32, 32), (245, 240, 230)).save(pdir / "refs" / "nimbo_modelsheet.png")
    # lyrics + character
    _blue_lyrics()  # noqa
    from src.models import save_json_model

    save_json_model(_blue_lyrics(), pdir / "lyrics.json")
    C.write_character_json(pdir)
    return pdir


def test_run_saves_manifest(tmp_path):
    pdir = _setup_project(tmp_path)
    manifest = P.run(
        pdir,
        topic="learning the color blue",
        draft_model="seedance-2.0-fast",
        final_model="seedance-2.0",
        aspect_ratio="16:9",
        resolution="720p",
        save=True,
    )
    mpath = pdir / "manifest.json"
    assert mpath.exists()

    loaded = load_manifest(mpath)
    assert loaded.project.name == "blue-song"
    assert loaded.project.topic == "learning the color blue"
    assert loaded.project.draft_model == "seedance-2.0-fast"
    assert loaded.project.final_model == "seedance-2.0"
    assert loaded.project.target_duration_s == pytest.approx(180.0)
    assert len(loaded.shots) == len(manifest.shots)
    assert loaded.covered_duration_s == pytest.approx(180.0, abs=0.05)


def test_dry_run_does_not_save(tmp_path):
    pdir = _setup_project(tmp_path)
    P.run(
        pdir,
        topic="learning the color blue",
        draft_model="seedance-2.0-fast",
        final_model="seedance-2.0",
        aspect_ratio="16:9",
        resolution="720p",
        save=False,
    )
    assert not (pdir / "manifest.json").exists()


def test_run_requires_topic_when_none_stored(tmp_path):
    pdir = _setup_project(tmp_path)
    with pytest.raises(P.PlannerError, match="No topic"):
        P.run(
            pdir,
            topic=None,
            draft_model="seedance-2.0-fast",
            final_model="seedance-2.0",
            aspect_ratio="16:9",
            resolution="720p",
            save=False,
        )


def test_run_reuses_topic_from_existing_manifest(tmp_path):
    pdir = _setup_project(tmp_path)
    # first run stores the topic
    P.run(pdir, topic="learning the color blue", draft_model="seedance-2.0-fast",
          final_model="seedance-2.0", aspect_ratio="16:9", resolution="720p", save=True)
    # second run with no topic should reuse it
    m2 = P.run(pdir, topic=None, draft_model="seedance-2.0-fast",
               final_model="seedance-2.0", aspect_ratio="16:9", resolution="720p", save=True)
    assert m2.project.topic == "learning the color blue"
