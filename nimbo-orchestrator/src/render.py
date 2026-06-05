"""Resumable render loop (draft + final passes) via the generation MCP.

TODO(Prompt 5): implement.
  - DRAFT: render pending shots on the cheapest tier; attach ref_images every call;
    extract prior clip's last frame for first-frame seeding.
  - Save manifest after EACH shot (resumable); skip drafted/approved/done on re-run.
  - Retry failed up to 3x; FINAL pass re-renders only "approved" shots.
  - Cost gate before any pass.
"""

from __future__ import annotations
