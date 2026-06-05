"""Tests for the Nimbo character config + style lock (Prompt 3).

Acceptance focus:
  - character.json validates (locked strings present, palette is hex)
  - refs are checked (missing / not-an-image / clean-background heuristic)
  - build_prompt() ALWAYS leads with the style_lock
  - the per-shot portion of build_prompt() contains NO Nimbo appearance descriptors
"""

from __future__ import annotations

import pytest

from src import character as C
from src.models import Character, Shot


# ──────────────────────────────────────────────────────────────────────────────
# Locked strings / default character
# ──────────────────────────────────────────────────────────────────────────────


def test_locked_strings_match_bible():
    ch = C.default_character()
    assert ch.name == "Nimbo"
    # style_lock carries the identity anchor + art direction
    assert "soft glowing head cloud" in ch.style_lock
    assert "NO neon" in ch.style_lock
    # negative list (verbatim highlights)
    assert "belly badge" in ch.negative
    assert "star shape" in ch.negative
    assert "transforming robot" in ch.negative
    # palette is the 6 bible colors
    assert ch.palette[0] == "#F4EBD8"
    assert "#F2C84B" in ch.palette  # cloud glow
    assert len(ch.palette) == 6


def test_validate_character_clean_for_default_with_present_ref(tmp_path):
    # default ref is refs/nimbo_modelsheet.png — create a clean light PNG there
    _write_clean_png(tmp_path / "refs" / "nimbo_modelsheet.png")
    ch = C.default_character()
    warnings = C.validate_character(ch, tmp_path)
    assert warnings == [], warnings


def test_validate_character_flags_bad_palette(tmp_path):
    ch = C.default_character()
    ch.palette = ["#F4EBD8", "not-a-hex", "F2C84B"]  # one bad, one missing #
    warnings = C.validate_character(ch, tmp_path)
    assert any("hex color" in w for w in warnings)


# ──────────────────────────────────────────────────────────────────────────────
# Reference image validation
# ──────────────────────────────────────────────────────────────────────────────


def _write_clean_png(path, color=(245, 240, 230)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (64, 64), color)
    img.save(path)


def _write_busy_png(path):
    """A dark, high-contrast image whose corners are not light/uniform."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (64, 64), (10, 10, 10))
    # paint contrasting corners
    for box, col in (
        ((0, 0, 16, 16), (255, 0, 0)),
        ((48, 48, 64, 64), (0, 255, 0)),
    ):
        for x in range(box[0], box[2]):
            for y in range(box[1], box[3]):
                img.putpixel((x, y), col)
    img.save(path)


def test_validate_refs_missing_file_warns(tmp_path):
    ch = C.default_character(ref_images=["refs/missing.png"])
    warnings = C.validate_refs(ch, tmp_path)
    assert any("missing" in w for w in warnings)


def test_validate_refs_no_refs_warns_about_drift(tmp_path):
    ch = C.default_character(ref_images=[])
    ch.ref_images = []
    warnings = C.validate_refs(ch, tmp_path)
    assert any("drift" in w for w in warnings)


def test_validate_refs_clean_png_passes(tmp_path):
    _write_clean_png(tmp_path / "refs" / "nimbo.png")
    ch = C.default_character(ref_images=["refs/nimbo.png"])
    assert C.validate_refs(ch, tmp_path) == []


def test_validate_refs_busy_background_warns(tmp_path):
    _write_busy_png(tmp_path / "refs" / "busy.png")
    ch = C.default_character(ref_images=["refs/busy.png"])
    warnings = C.validate_refs(ch, tmp_path)
    assert any("background" in w for w in warnings)


def test_validate_refs_non_image_warns(tmp_path):
    bad = tmp_path / "refs" / "notimage.png"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("i am not a png")
    ch = C.default_character(ref_images=["refs/notimage.png"])
    warnings = C.validate_refs(ch, tmp_path)
    assert any("not a readable image" in w for w in warnings)


def test_discover_refs(tmp_path):
    refs = tmp_path / "refs"
    refs.mkdir()
    (refs / "b.png").write_bytes(b"x")
    (refs / "a.jpg").write_bytes(b"x")
    (refs / "notes.txt").write_text("nope")
    found = C.discover_refs(tmp_path)
    assert found == ["refs/a.jpg", "refs/b.png"]  # sorted, images only


# ──────────────────────────────────────────────────────────────────────────────
# strip_appearance
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "a cream-colored round plush creature waving hello",
        "the matte soft-touch egg-shaped Nimbo with oversized eyes and coral cheeks",
        "small cream plush-like creature with stubby little feet",
    ],
)
def test_strip_appearance_removes_all_terms(raw):
    out = C.strip_appearance(raw).lower()
    for term in C._APPEARANCE_TERMS:
        assert term not in out, f"'{term}' leaked through: {out!r}"


def test_strip_appearance_keeps_cloud_actions():
    """What the cloud DOES is core action — never stripped."""
    out = C.strip_appearance("the cloud glows blue and forms a cat face")
    assert "cloud" in out.lower()
    assert "glows blue" in out.lower()


def test_strip_appearance_tidies_punctuation():
    out = C.strip_appearance("Nimbo, a cream plush creature, waves")
    assert "  " not in out
    assert ",," not in out
    assert " ," not in out


# ──────────────────────────────────────────────────────────────────────────────
# build_prompt — leads with style_lock, appearance-free per-shot text
# ──────────────────────────────────────────────────────────────────────────────


def _shot(action, scene="a soft pastel meadow with negative space", camera="slow push-in"):
    return Shot(
        id="s", start_s=0, end_s=8, duration_s=8, scene=scene, camera=camera, action=action
    )


def test_build_prompt_leads_with_style_lock():
    ch = C.default_character()
    prompt = build = C.build_prompt(_shot("waves hello while the cloud glows blue"), ch)
    assert prompt.startswith(ch.style_lock.strip())


def test_build_prompt_per_shot_portion_has_no_appearance_descriptors():
    """THE acceptance check: the variable per-shot text (everything after the style_lock)
    must contain no Nimbo appearance descriptors."""
    ch = C.default_character()
    nasty_action = (
        "Nimbo, a cream-colored round plush creature with oversized eyes and coral cheeks, "
        "waves hello while the cloud glows blue"
    )
    prompt = C.build_prompt(_shot(nasty_action), ch)

    # Everything after the locked style is the per-shot portion.
    suffix = prompt[len(ch.style_lock.strip()):].lower()
    for term in C._APPEARANCE_TERMS:
        assert term not in suffix, f"appearance term '{term}' leaked into per-shot text: {suffix!r}"

    # The actual action survives.
    assert "waves hello" in suffix
    assert "cloud glows blue" in suffix
    # And the cloud-action / setting / camera are present.
    assert "meadow" in suffix
    assert "push-in" in suffix


def test_build_prompt_dedupes_leading_nimbo():
    ch = C.default_character()
    prompt = C.build_prompt(_shot("Nimbo waves hello"), ch)
    suffix = prompt[len(ch.style_lock.strip()):]
    assert "Nimbo Nimbo" not in suffix
    assert "Scene: Nimbo waves hello" in suffix


def test_build_prompt_handles_empty_action():
    ch = C.default_character()
    prompt = C.build_prompt(_shot("", scene="calm sky", camera=""), ch)
    assert prompt.startswith(ch.style_lock.strip())
    assert "calm sky" in prompt


# ──────────────────────────────────────────────────────────────────────────────
# IO roundtrip + CLI
# ──────────────────────────────────────────────────────────────────────────────


def test_write_and_load_character_roundtrip(tmp_path):
    _write_clean_png(tmp_path / "refs" / "nimbo_a.png")
    path = C.write_character_json(tmp_path)
    assert path.exists()
    ch = C.load_character(tmp_path)
    assert ch.name == "Nimbo"
    assert ch.ref_images == ["refs/nimbo_a.png"]  # auto-discovered
    assert ch.style_lock == C.NIMBO_STYLE_LOCK


def test_load_character_missing_raises(tmp_path):
    with pytest.raises(C.CharacterError, match="not found"):
        C.load_character(tmp_path)


def test_cli_writes_and_validates(tmp_path, capsys):
    _write_clean_png(tmp_path / "blue-song" / "refs" / "nimbo_modelsheet.png")
    code = C.main(["--project", "blue-song", "--projects-root", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "blue-song" / "character.json").exists()
    out = capsys.readouterr().out
    assert "build_prompt() demo" in out
    # The demo action contains appearance words that must be stripped in the output.
    demo_section = out.split("build_prompt() demo")[1].lower()
    assert "cream-colored" not in demo_section
    assert "plush creature" not in demo_section
    assert "waves hello" in demo_section
