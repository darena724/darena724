"""Nimbo character config + style lock.

TODO(Prompt 3): implement.
  - Define/validate character.json (ref_images, style_lock, negative, palette).
  - build_prompt(shot) -> style_lock + scene/camera/action, always passing ref_images,
    never redescribing Nimbo's appearance.
  Source of truth for the style_lock / negative prompt: ../docs/CHARACTER_BIBLE.md.
"""

from __future__ import annotations
