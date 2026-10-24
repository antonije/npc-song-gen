---
name: npc-song-gen
description: >-
  Generate immersion-safe NPC / game-mod songs via the local ACE-Step 1.5
  pipeline in npc-song-gen (single instrument + vocals, Demucs purify by default).
  Starts ACE-Step automatically if the API is down. Use when the user asks to
  generate music, NPC songs, campfire/tavern audio, run npc-song-gen or music-gen,
  purify stems, or create mod audio assets with ACE-Step.
---

# npc-song-gen

Interactive runner for `/home/tonka/personal-work/npc-song-gen`. Always gather missing inputs from the user before generating. Do not invent lyrics, instrument, or vibe without asking (unless they say to reuse defaults / a named preset).

## Project root

```text
NPC_SONG_GEN=/home/tonka/personal-work/npc-song-gen
ACESTEP_DIR=$NPC_SONG_GEN/vendor/ACE-Step-1.5   # override with env ACESTEP_DIR if cloned elsewhere
```

All project commands run from `$NPC_SONG_GEN` with `uv run`.

## Intake (ask before running)

Copy this checklist. Ask only for blanks the user has not already answered in the chat. Prefer one short message with the open questions (or AskQuestion if available).

```text
Intake:
- [ ] vocalist: male | female   ← REQUIRED before any run (never assume)
- [ ] instrument: lute|guitar|flute|harp|mandolin|fiddle|lyre  ← default lute; list with --list-instruments
- [ ] style preset (default for Skyrim: skyrim_bard)  ← list with --list-styles
- [ ] song slug / lyrics (e.g. the_dragonborn_comes, ragnar_the_red, campfire_guitar)
- [ ] bpm override? (else style default_bpm or prompt tag)
- [ ] duration seconds (optional)
- [ ] seed (optional) / thinking on? (default on)
- [ ] compare A/B/C? (default no — Demucs-only mod export)
```

**Hard gate:** Do not call `generate_npc_song.py` until the user has answered **vocalist** (`male` or `female`).

For Skyrim bard NPC songs, prefer:

```bash
--vocalist female --instrument lute --style skyrim_bard --song ragnar_the_red
```

### Style presets (`style_presets/`)

Reusable structured style prompts (Genre / Vocals / Instruments / Production / Mood).

| Slug | Purpose |
| --- | --- |
| `skyrim_bard` | Dry solo instrument + intimate single vocal |

```text
style_presets/<slug>.txt    # template with {vocals} and {instrument} placeholders
style_presets/<slug>.json   # inference_steps, guidance_scale, default_bpm, lm_negative_prompt
```

**List:** `uv run python generate_npc_song.py --list-styles`  
**Instruments:** `uv run python generate_npc_song.py --list-instruments`  
**Add a style:** new `.txt` + optional `.json` (see `style_presets/README.md`).

When `--style` is set, it owns the prompt; `--song` only supplies lyrics. `{instrument}` is filled from `--instrument` (default `lute`).

### Multi-song library (`lyrics/` + optional `prompts/`)

```text
lyrics/<slug>.txt     # required — lyrics with [Verse]/[Chorus] tags
prompts/<slug>.txt    # optional legacy style (used only if --style omitted)
```

Current songs: `campfire_guitar`, `ragnar_the_red`, `the_dragonborn_comes`.

`ragnar_the_red` and `the_dragonborn_comes` lyrics are from *Skyrim* (Bethesda);
this project holds no copyright in them — see `CREDITS.md`. Do not put credit
text inside `lyrics/*.txt` (those files are sent verbatim to ACE-Step).

**Add a song:** write `lyrics/<slug>.txt`, then `--style skyrim_bard --song <slug>`.  
**List songs:** `uv run python generate_npc_song.py --list-songs`.

- Never hardcode long prompts/lyrics in the script/skill body.
- `{vocals}` in style presets is filled from `--vocalist`.
- `{instrument}` is filled from `--instrument` (prompt guidance only — Demucs does not pick the instrument).
- BPM: `--bpm` > `[N bpm]` in prompt > style `default_bpm` > 85.
- Output defaults to `<slug>.wav` unless `--name` is set.

## Preconditions

1. Ensure project deps: `cd $NPC_SONG_GEN && uv sync`
2. Demucs (default purify): `uv sync --extra purify` (once; heavy).
3. **Ensure ACE-Step API is up** (do this yourself — do not only tell the user):

```bash
cd /home/tonka/personal-work/npc-song-gen && bash scripts/ensure_acestep.sh
```

What that script does:

- If `http://127.0.0.1:8001/health` already OK → exit.
- Else clone ACE-Step 1.5 into `vendor/ACE-Step-1.5` (if missing), `uv sync`, start `acestep-api` in the background, wait until healthy.
- Logs: `vendor/acestep-api.log` · pid: `vendor/acestep-api.pid`
- **Linux:** CUDA on GPU 0 via `ACESTEP_CUDA_VISIBLE_DEVICES` (default `0`), LM backend `vllm`.
- **macOS Apple Silicon:** sets `ACESTEP_LM_BACKEND=mlx` (no CUDA). Intel Mac falls back to `pt` (slow).

Overrides: `ACESTEP_DIR`, `ACESTEP_API_HOST`, `ACESTEP_API_PORT`, `ACESTEP_API_BASE`, `ACESTEP_WAIT_SECS`, `ACESTEP_CUDA_VISIBLE_DEVICES`, `ACESTEP_LM_BACKEND`, `ACESTEP_CONFIG_PATH`, `ACESTEP_LM_MODEL_PATH`.

First boot can take a long time (clone + deps + model download). On Mac use a high shell `block_until_ms` / `ACESTEP_WAIT_SECS=600` and read the log on failure.

Do not call `generate_npc_song.py` until health succeeds.

### Mac (Apple Silicon)

Same ensure + generate flow. Prefer Demucs on Metal:

```bash
bash scripts/ensure_acestep.sh
uv run python generate_npc_song.py \
  --vocalist female --instrument lute --style skyrim_bard --song ragnar_the_red \
  --demucs-device mps
```

## Run (default = Demucs purify)

**Preferred mod export** (validated best: ACE-Step generate + Demucs):

```bash
cd /home/tonka/personal-work/npc-song-gen && uv run python generate_npc_song.py \
  --vocalist male|female \
  --instrument lute \
  --style skyrim_bard \
  --song <slug> \
  --quality default \
  --demucs-device cuda:1 \
  [--duration <seconds>] \
  [--seed <n>] \
  [--name "<filename>.wav"]
```

`--vocalist` is **required**. `--purify` is **on by default** (`--no-purify` for raw only).

### Quality presets (12GB 3080 Ti)

| `--quality` | DiT | LM | Notes |
| --- | --- | --- | --- |
| `default` | `acestep-v15-sft` | `0.6B` | **Daily default** — best reliable quality on 12GB |
| `xl` | `acestep-v15-xl-turbo` | `0.6B` | Try XL quality (may offload/OOM) |
| `xl-sft` | `acestep-v15-xl-sft` | `0.6B` | Max XL — tightest VRAM |

Script calls `POST /v1/init` before generate. Ask which quality if user cares; otherwise use `default`.

- **Devices:** Linux multi-GPU → ACE-Step on GPU 0, Demucs `--demucs-device cuda:1`. Mac → `--demucs-device mps` (or `cpu`).
- **Compare (optional):** only when the user asks — `--compare` writes A raw / B Demucs / C ACE-Step extract from the same take. Do **not** run extract by default.
- Generation can take several minutes; set a long shell `block_until_ms` and poll.

## After generate

Report:

1. Final clean path under `mod_assets/audio/` (Demucs result)
2. Raw path kept as `*_raw.wav` when purify ran
3. Offer a quick re-roll (new seed / tweak prompt `.txt`) if they dislike the take

## Do not

- Assume vocalist gender — always ask, then pass `--vocalist`.
- Leave ACE-Step down and only paste start instructions — run `scripts/ensure_acestep.sh` first.
- Default to ACE-Step extract or `--compare` — Demucs-only is the mod path.
- Call fictional endpoints like `/v1/generate` — the script uses `release_task` → `query_result` → `/v1/audio`.
- Use Demucs `--two-stems=vocals` alone for purity (keeps drums/bass in accompaniment). The script’s `--purify` does full 4-stem and remixes vocals + other only.
