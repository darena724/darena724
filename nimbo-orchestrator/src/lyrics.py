"""Song step: lyrics + section/timing map, or bring-your-own-MP3 ingestion.

TODO(Prompt 2): implement.
  A) Generate-from-scratch: topic -> lyrics.json (sections) + music_gen_prompt.txt.
  B) Bring-your-own-audio: read true duration via ffprobe, fit sections.
  Optional Suno/ElevenLabs generate_song() behind a flag; manual MP3 must always work.
"""

from __future__ import annotations
