# Style presets

Reusable ACE-Step style prompts for different song types.

| Slug | Use for |
| --- | --- |
| `skyrim_bard` | Skyrim tavern / campfire bard songs (dry solo instrument + intimate vocal) |

## Files

- `<slug>.txt` — prompt template. Use `{vocals}` and `{instrument}` where gender / instrument wording goes.
- `<slug>.json` — optional meta: `inference_steps`, `guidance_scale`, `default_bpm`, `lm_negative_prompt`, `description`

## Usage

```bash
uv run python generate_npc_song.py --list-styles
uv run python generate_npc_song.py --list-instruments
uv run python generate_npc_song.py \
  --vocalist female --instrument lute --style skyrim_bard --song the_dragonborn_comes
```

`--instrument` choices: `lute` (default), `guitar`, `flute`, `harp`, `mandolin`, `fiddle`, `lyre`.

Songs are separate from styles — see `lyrics/` / `--list-songs`.
