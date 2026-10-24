# npc-song-gen — NPC song pipeline (ACE-Step 1.5)

Two-step pipeline for immersion-safe game-mod audio:

1. **Generate** a single-instrument + vocal track via your local [ACE-Step 1.5](https://github.com/ACE-Step/ACE-Step-1.5) API.
2. **Purify** (optional) with Demucs: keep only `vocals` + `other` (the instrument), drop drums/bass bleed.

> The other agent’s `/v1/generate` helper does **not** match ACE-Step 1.5. This project uses the real async API: `POST /release_task` → poll `POST /query_result` → `GET /v1/audio`.

## Prerequisites

1. **ACE-Step 1.5** — easiest via the ensure script (Linux CUDA or macOS Apple Silicon / MLX):

   ```bash
   bash scripts/ensure_acestep.sh
   # Mac first boot may need: ACESTEP_WAIT_SECS=600 bash scripts/ensure_acestep.sh
   ```

   Or start manually from `vendor/ACE-Step-1.5` (`ACESTEP_LM_BACKEND=mlx` on Apple Silicon).

2. **This folder** — Python deps via `uv` (already on your machine):

   ```bash
   cd /home/tonka/personal-work/npc-song-gen
   uv sync
   # Optional stem purification (pulls PyTorch + Demucs — large):
   uv sync --extra purify
   ```

3. Copy env defaults if you want:

   ```bash
   cp .env.example .env
   ```

## Songs

Lyrics live in `lyrics/<slug>.txt`. List with `--list-songs`.

| Slug | Title | Source |
| --- | --- | --- |
| `campfire_guitar` | Campfire guitar | Project lyric sheet |
| `ragnar_the_red` | Ragnar the Red | *Skyrim* (Bethesda) — see [CREDITS.md](CREDITS.md) |
| `the_dragonborn_comes` | The Dragonborn Comes | *Skyrim* (Bethesda) — see [CREDITS.md](CREDITS.md) |

**Copyright:** *Ragnar the Red* and *The Dragonborn Comes* lyrics are from
*The Elder Scrolls V: Skyrim*. This project **holds no copyright** in them; all
rights remain with Bethesda Softworks / ZeniMax. See [CREDITS.md](CREDITS.md).

## Generate

With ACE-Step already running on port 8001:

```bash
cd /home/tonka/personal-work/npc-song-gen
uv run python generate_npc_song.py \
  --vocalist female --instrument lute --style skyrim_bard --song the_dragonborn_comes
```

Output lands in `mod_assets/audio/`.

### Generate + purify (recommended for mods)

```bash
# Demucs device: cuda:1 (Linux), mps (Apple Silicon), or cpu
uv run python generate_npc_song.py \
  --vocalist female --instrument lute --style skyrim_bard --song the_dragonborn_comes \
  --demucs-device mps
```

This writes:

- `mod_assets/audio/<slug>_raw.wav` — as generated
- `mod_assets/audio/<slug>.wav` — vocals + instrument only
- `separated/htdemucs/...` — intermediate stems

## Customize for another NPC

```bash
uv run python generate_npc_song.py \
  --vocalist female \
  --instrument lute \
  --style skyrim_bard \
  --song the_dragonborn_comes \
  --name npc_dragonborn_bard.wav \
  --bpm 80 \
  --duration 75
```

Useful flags:

| Flag | Meaning |
| --- | --- |
| `--vocalist` | Required: `male` or `female` |
| `--instrument` | `lute` (default), `guitar`, `flute`, `harp`, `mandolin`, `fiddle`, `lyre` |
| `--style` / `--song` | Style preset + lyrics slug |
| `--bpm` / `--duration` | Tempo / length (seconds) |
| `--thinking` / `--no-thinking` | LM code planning (default on) |
| `--demucs-device` | `mps` (Mac), `cuda:N`, or `cpu` |

Structural section tags in lyrics (`[Verse 1]`, `[Chorus]`) help keep arrangements simple.

## Project layout

```
npc-song-gen/
├── generate_npc_song.py   # main pipeline
├── style_presets/         # reusable style prompts ({vocals}, {instrument})
├── prompts/               # legacy prompt snippets
├── lyrics/                # lyric sheets
├── CREDITS.md             # Skyrim lyric attribution / no-copyright notice
├── scripts/ensure_acestep.sh
├── mod_assets/audio/      # game-ready exports (gitignored)
├── separated/             # Demucs stems (gitignored)
├── pyproject.toml
└── .env.example
```
