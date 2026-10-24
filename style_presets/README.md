# Style presets

Reusable ACE-Step style prompts for different song types.

| Slug | Use for |
| --- | --- |
| `skyrim_bard` | Tavern / storytelling bard (dry solo instrument, ~80 BPM) |
| `skyrim_campfire` | Unplugged campfire folk (intimate raw mix, ~85 BPM; pair with `--instrument guitar`) |

## Files

- `<slug>.txt` — prompt template. Use `{vocals}` and `{instrument}` where gender / instrument wording goes.
- `<slug>.json` — optional meta: `inference_steps`, `guidance_scale`, `default_bpm`, `lm_negative_prompt`, `description`

## Usage

```bash
uv run python generate_npc_song.py --list-styles
uv run python generate_npc_song.py --list-instruments
uv run python generate_npc_song.py \
  --vocalist female --instrument guitar --style skyrim_campfire --song campfire_guitar
```

`--instrument` choices: `lute` (default), `guitar`, `flute`, `harp`, `mandolin`, `fiddle`, `lyre`.

Songs are separate from styles — see `lyrics/` / `--list-songs`.
