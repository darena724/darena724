"""Model bake-off harness.

TODO(Prompt 1.5): render ONE identical Nimbo test shot across a list of models
(default: Veo 3.1, Seedance Fast, Kling 3.0) with the same refs/action/duration/aspect.
Save bakeoff/<model>.mp4 and print {model, seconds, est_cost, output_path}. Keep spend small.
"""

from __future__ import annotations
