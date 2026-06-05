"""Shot planner: song + character -> ordered shots folded into manifest.json.

TODO(Prompt 4): implement.
  - Split full duration into shots <= the locked model's clip cap, aligned to lyric
    sections, covering the whole song with no gaps/overlaps.
  - seed_from chain for continuity; status="pending"; cheapest draft model/tier.
"""

from __future__ import annotations
