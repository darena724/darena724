# Nimbo Video Orchestrator — Build Playbook (v2)

A local, MCP-driven pipeline that turns a song + the Nimbo character into a finished
~3-minute kids' music video, using a model-agnostic video-generation API.

## Locked decisions (v2)

- **No lip-sync.** Nimbo acts/appears over the song; he does not mouth the lyrics. The
  pipeline is simpler and there is no separate lip-sync model pass.
- **API access confirmed.** Generation runs through a paid video API (not a consumer
  subscription). Default routing through an aggregator (fal.ai or Replicate) so the model
  is swappable; the chosen "workhorse" model can later move to its direct API for savings.
- **Budget: $5–10 per finished 3-min video.** Achievable on a clean pass with Veo-direct
  or Seedance-Fast. Protected by: draft on the cheapest tier, final-render only approved shots.
- **No Veo/model audio.** Generate silent video; the user's MP3 is the only audio, muxed at
  assembly.

<!-- Additional playbook sections will be appended as provided. -->
