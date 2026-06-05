"""End-to-end pipeline CLI with mandatory review stops + guardrails.

TODO(Prompt 7): implement.
  make-video --project blue-song [--topic "color blue" | --mp3 path] [--model <locked>] [--captions]
  Flow: lyrics -> STOP -> shot plan -> STOP -> draft render -> STOP -> final -> assemble.
  Guardrails: print est. cost before steps 3 & 4 (require --yes); --dry-run = zero API calls.
"""

from __future__ import annotations


def main() -> int:
    raise SystemExit("pipeline CLI is implemented in Prompt 7 (not yet built).")


if __name__ == "__main__":
    main()
