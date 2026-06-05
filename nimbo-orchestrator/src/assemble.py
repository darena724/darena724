"""Assembly: ffmpeg concat + mux MP3 + optional captions -> final.mp4.

TODO(Prompt 6): implement.
  - Concat clips (re-encode to uniform codec/fps/resolution) into one silent video.
  - Mux song.mp3 as the ONLY audio; trim/pad video to song length.
  - Optional --captions burned from lyrics.json section text.
"""

from __future__ import annotations
