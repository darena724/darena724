# Nimbo — Character Bible

> Canonical source for `character.json`, `style_lock`, negative prompt, and the model sheet.
> Store these verbatim when implementing `src/character.py` (Prompt 3).

## Nimbo

A small, soft, invented creature who sings short songs about colors, animals, and early
concepts.

Each episode is one concept = one 2–3 minute song. Nimbo lives in the calm, premium lane:
gentle pacing (7+ seconds per scene), a soothing muted palette, and one warm pop of glow —
deliberately the opposite of neon, fast-cut "content slop."

## The signature — Nimbo's Cloud

Nimbo's one unforgettable feature is the soft translucent cloud on top of the head. It behaves
like a gentle glowing badge and does exactly two things, depending on the song:

- **Fills with one solid color** — for color songs, the cloud glows the exact color being
  taught (red song → red cloud).
- **Forms a simple animal face** — for animal songs, the cloud morphs into a minimal, cute
  cat / duck / bunny face made of soft rounded shapes.

The body never changes shape — only the cloud. This keeps Nimbo instantly recognizable in
thumbnails and clearly distinct from "transforming" characters.

## Anatomy & Proportions

- Round, egg-shaped body — soft and plush, wider at the base.
- Oversized gentle eyes, set close together (baby-like = endearing).
- Small soft coral cheeks; a tiny, simple smile.
- Short rounded arms, stubby little feet, no fingers/toes detail.
- Cloud = ~⅓ of head height, centered on top.
- Matte, soft-touch surface — reads like a designer plush toy.

## Voice & Format

- **Tone:** warm, calm, curious — never frantic or shouty.
- **Music:** gentle, melodic, acoustic-leaning; soft tempo.
- **Pacing:** 7+ seconds per scene; minimal hard cuts.
- **Length:** 2–3 minutes, one concept per video.
- **Repetition:** repeat the key word/phrase often (memory aid).
- **Titles:** lead with the searchable concept ("The Red Song").

## Color Palette

| Name       | Hex       |
| ---------- | --------- |
| Body Cream | `#F4EBD8` |
| Belly Light| `#FBF4E6` |
| Edge / Line| `#E0D2B5` |
| Cheek Coral| `#F1B49E` |
| Eyes Ink   | `#322B22` |
| Cloud Glow | `#F2C84B` |

## Attributes — Always

- Keep the same proportions in every render.
- Muted, soothing palette + one glow accent only.
- Soft even lighting; uncluttered backgrounds with negative space.
- Use the locked model sheet as the reference image every time.
- Lock a seed / use "character reference" if your tool supports it.

## Attributes — Never

- No star or raindrop silhouette (keeps clear of Nintendo's "Luma").
- No belly badge (Care Bears) — the glow lives on the cloud only.
- No neon / oversaturation; no rapid hard cuts.
- No human toddler family, fox, shark, or chick mascot cues.
- No full-body transformation — cloud morphs, body stays.

## Model Sheet (image-gen prompt)

> Character design model sheet for an original children's animated mascot named Nimbo, on a
> plain off-white studio background. Nimbo is a small, round, plush-toy-like invented creature
> — not a real animal, not human: a soft egg-shaped body in warm matte cream/oatmeal, short
> rounded arms, stubby little feet, oversized gentle dark eyes set close together with soft
> highlights, small soft coral cheeks, and a tiny friendly smile. Calm, sweet, approachable
> expression.
>
> Signature feature: a soft, translucent rounded cloud on top of the head that works like a
> gentle glowing badge. It can either fill with a single solid color, OR form a simple, cute,
> minimal animal face (cat, duck, bunny) made of soft rounded shapes. The cloud glows softly;
> the body never changes shape.
>
> Show a clean model sheet: front view, 3/4 view, and side view (consistent proportions); then
> a row of four small headshots with the cloud glowing red, yellow, green, and blue; then a row
> of three small headshots with the cloud forming a simple cat face, duck face, and bunny face.
>
> Style: clean modern soft 3D render, smooth matte soft-touch surfaces, simple rounded shapes,
> gentle even studio lighting, soft ambient shadows. Muted, soothing, premium pastel palette
> with one warm glow accent — NOT neon, NOT high-saturation. Designer-toy / gentle
> Pixar-adjacent aesthetic. White background, no text, no logos.  --ar 16:9

## Episode template (per-shot style_lock + slots)

> The same character Nimbo (small round cream plush-like creature, oversized gentle close-set
> eyes, soft coral cheeks, soft glowing head cloud), consistent design and proportions.
>
> Scene: Nimbo [ACTION] in [SETTING, soft and simple with negative space]. The cloud
> [glows COLOR / forms a simple ANIMAL face] for the song about [TOPIC].
>
> Style: clean soft 3D render, matte soft-touch surfaces, simple rounded shapes, gentle even
> lighting, soothing muted pastel palette with one accent, NO neon, calm uncluttered
> composition. Designer-toy / gentle Pixar-adjacent look. No text, no logos.  --ar 16:9

## Negative Prompt

> neon colors, oversaturated, busy cluttered background, harsh lighting, scary, sharp teeth,
> star shape, raindrop shape, human toddler, oversized-head toddler family, fox mascot, shark
> family, yellow chick, belly badge, transforming robot, text, watermark, logo, brand names
