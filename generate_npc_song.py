#!/usr/bin/env python3
"""Generate a single-instrument + vocal NPC song via local ACE-Step 1.5.

Style: style_presets/<slug>.txt (+ optional .json for steps/bpm).
Songs: lyrics/<slug>.txt (prompts/<slug>.txt only if --style omitted).

Workflow:
  1. POST /release_task  -> task_id
  2. POST /query_result  -> poll until status 1 (ok) or 2 (failed)
  3. GET  /v1/audio?path=... -> save WAV into mod_assets/audio/
  4. Default: Demucs 4-stem split, remix vocals + other (drop drums/bass bleed)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

ROOT = Path(__file__).resolve().parent
PROMPTS_DIR = ROOT / "prompts"
LYRICS_DIR = ROOT / "lyrics"
STYLE_PRESETS_DIR = ROOT / "style_presets"
DEFAULT_OUTPUT_DIR = ROOT / "mod_assets" / "audio"
DEFAULT_STEMS_DIR = ROOT / "separated"
DEFAULT_SONG = "campfire_guitar"
DEFAULT_STYLE = "skyrim_bard"
DEFAULT_PROMPT_FILE = PROMPTS_DIR / f"{DEFAULT_SONG}.txt"
DEFAULT_LYRICS_FILE = LYRICS_DIR / f"{DEFAULT_SONG}.txt"

# DiT + LM presets for a 12GB 3080 Ti (override with --model / --lm-model / steps).
QUALITY_PRESETS: dict[str, dict[str, Any]] = {
    "default": {
        "model": "acestep-v15-sft",
        "lm_model_path": "acestep-5Hz-lm-0.6B",
        "inference_steps": 50,
        "guidance_scale": 7.0,
        "label": "2B SFT + 0.6B LM (best practical quality on 12GB)",
    },
    "sft-1.7b": {
        "model": "acestep-v15-sft",
        "lm_model_path": "acestep-5Hz-lm-1.7B",
        "inference_steps": 50,
        "guidance_scale": 7.0,
        "label": "2B SFT + 1.7B LM (higher planning; often CPU-decodes on 12GB)",
    },
    "xl": {
        "model": "acestep-v15-xl-turbo",
        "lm_model_path": "acestep-5Hz-lm-0.6B",
        "inference_steps": 8,
        "guidance_scale": None,
        "label": "XL turbo + 0.6B LM (XL quality try; may offload/OOM)",
    },
    "xl-sft": {
        "model": "acestep-v15-xl-sft",
        "lm_model_path": "acestep-5Hz-lm-0.6B",
        "inference_steps": 50,
        "guidance_scale": 7.0,
        "label": "XL SFT + 0.6B LM (max XL; tight on 12GB)",
    },
}

# Legacy append tags when not using a structured style preset.
VOCALIST_TAGS = {
    "male": (
        "[solo male vocal], [single male singer], [one male voice only], "
        "[no female vocals], [no choir], [no chorus], [no ensemble], "
        "[no backing vocals], [no harmony vocals], [no group singing], [no overdubs]"
    ),
    "female": (
        "[solo female vocal], [single female singer], [one female voice only], "
        "[no male vocals], [no choir], [no chorus], [no ensemble], "
        "[no backing vocals], [no harmony vocals], [no group singing], [no overdubs]"
    ),
}

# Fills {vocals} inside style_presets/*.txt templates.
VOCALIST_STYLE = {
    "male": "Bone Dry Male Vocal, Close Mic, Single Singer, No Echo, No Female",
    "female": "Bone Dry Female Vocal, Close Mic, Single Singer, No Echo, No Male",
}

# Extra LM negatives keyed by requested vocalist (blocks the opposite gender).
VOCALIST_LM_NEGATIVES = {
    "male": "female vocals, woman singing, duet",
    "female": "male vocals, man singing, duet",
}

# Fills {instrument} in style presets. Keep phrases short (KV cache on 12GB).
DEFAULT_INSTRUMENT = "lute"
INSTRUMENT_STYLE = {
    "lute": "Prominent Solo Lute, Fingerpicked, Clear Plucked Notes",
    "guitar": "Prominent Solo Acoustic Guitar, Fingerpicked, Clear Plucked Notes",
    "flute": "Prominent Solo Wooden Flute, Soft Breath Tone, Melodic",
    "harp": "Prominent Solo Folk Harp, Plucked, Clear Arpeggios",
    "mandolin": "Prominent Solo Mandolin, Fingerpicked, Bright Plucked Notes",
    "fiddle": "Prominent Solo Fiddle, Soft Bowed Melody",
    "lyre": "Prominent Solo Lyre, Plucked, Clear Notes",
}

# Discourage every other catalog instrument + common bleed.
INSTRUMENT_LM_NEGATIVES = {
    key: ", ".join(
        sorted(
            {
                *(
                    name
                    for name in (
                        "lute",
                        "guitar",
                        "flute",
                        "harp",
                        "mandolin",
                        "fiddle",
                        "lyre",
                        "piano",
                        "drums",
                        "bass",
                        "synth",
                        "orchestra",
                    )
                    if name != key
                ),
            }
        )
    )
    for key in (
        "lute",
        "guitar",
        "flute",
        "harp",
        "mandolin",
        "fiddle",
        "lyre",
    )
}

# Soft negative for 5Hz LM CFG (helps when thinking=True).
DEFAULT_LM_NEGATIVE_PROMPT = (
    "choir, chorus, ensemble vocals, backing vocals, harmony vocals, "
    "group singing, multiple singers, gospel choir, layered vocals, overdubs, "
    "drums, bass, synth, orchestra, pads"
)


def list_instruments() -> list[str]:
    return sorted(INSTRUMENT_STYLE.keys())


def apply_vocalist_tags(prompt: str, vocalist: str) -> str:
    """Append required single-singer gender tags (legacy song prompt files)."""
    tags = VOCALIST_TAGS[vocalist]
    base = prompt.strip().rstrip(",")
    return f"{base}, {tags}"


def apply_instrument_tags(prompt: str, instrument: str) -> str:
    """Append solo-instrument tags when the prompt has no {instrument} slot."""
    phrase = INSTRUMENT_STYLE[instrument]
    base = prompt.strip().rstrip(",")
    return f"{base}, [Instruments: {phrase}]"


def apply_style_template(template: str, vocalist: str, instrument: str) -> str:
    """Fill {vocals} / {instrument} in a style preset; fall back to append tags."""
    out = template
    if "{vocals}" in out:
        out = out.replace("{vocals}", VOCALIST_STYLE[vocalist])
    else:
        out = apply_vocalist_tags(out, vocalist)
    if "{instrument}" in out:
        out = out.replace("{instrument}", INSTRUMENT_STYLE[instrument])
    else:
        out = apply_instrument_tags(out, instrument)
    return out.strip()


def list_songs() -> list[str]:
    """Song slugs with lyrics (prompt file optional when using --style)."""
    if not LYRICS_DIR.is_dir():
        return []
    return sorted(p.stem for p in LYRICS_DIR.glob("*.txt"))


def list_styles() -> list[str]:
    if not STYLE_PRESETS_DIR.is_dir():
        return []
    return sorted(p.stem for p in STYLE_PRESETS_DIR.glob("*.txt"))


def resolve_song_paths(slug: str, *, require_prompt: bool = True) -> tuple[Path | None, Path]:
    prompt_path = PROMPTS_DIR / f"{slug}.txt"
    lyrics_path = LYRICS_DIR / f"{slug}.txt"
    if not lyrics_path.is_file():
        available = ", ".join(list_songs()) or "(none)"
        raise SystemExit(
            f"Song '{slug}' missing lyrics at {lyrics_path}\nAvailable songs: {available}"
        )
    if require_prompt and not prompt_path.is_file():
        available = ", ".join(list_songs()) or "(none)"
        raise SystemExit(
            f"Song '{slug}' missing prompt at {prompt_path}\n"
            f"Use --style <preset> or add the prompt file. Songs: {available}"
        )
    return (prompt_path if prompt_path.is_file() else None), lyrics_path


def load_style_preset(slug: str) -> tuple[str, dict[str, Any]]:
    path = STYLE_PRESETS_DIR / f"{slug}.txt"
    if not path.is_file():
        available = ", ".join(list_styles()) or "(none)"
        raise SystemExit(f"Unknown style preset '{slug}'. Available: {available}")
    template = path.read_text(encoding="utf-8").strip()
    meta: dict[str, Any] = {}
    meta_path = STYLE_PRESETS_DIR / f"{slug}.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return template, meta


def parse_bpm_from_prompt(prompt: str) -> int | None:
    m = re.search(r"\[(\d+)\s*bpm\]", prompt, re.IGNORECASE)
    return int(m.group(1)) if m else None


def slugify_title(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", title.strip().lower()).strip("_")
    return s or "song"


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader (no extra dependency). Does not override existing env."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def api_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def check_health(base_url: str, headers: dict[str, str], timeout: float) -> None:
    url = urljoin(base_url.rstrip("/") + "/", "health")
    try:
        r = requests.get(url, headers={k: v for k, v in headers.items() if k != "Content-Type"}, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as exc:
        raise SystemExit(
            f"ACE-Step API not reachable at {base_url}\n"
            f"  Start it first:  uv run acestep-api --port 8001\n"
            f"  (from your ACE-Step-1.5 clone)\n"
            f"  Detail: {exc}"
        ) from exc


def ensure_model(
    base_url: str,
    headers: dict[str, str],
    model: str,
    lm_model_path: str | None,
    timeout: float,
) -> None:
    """Switch DiT (+ optional LM) via POST /v1/init before generating.

    Skips re-init when the requested models are already loaded — reloading the
    LM after DiT is warm can shrink KV cache to one block on 12GB cards.
    """
    auth = {k: v for k, v in headers.items() if k.lower() != "content-type"}
    try:
        health = requests.get(
            urljoin(base_url.rstrip("/") + "/", "health"),
            headers=auth,
            timeout=timeout,
        )
        health.raise_for_status()
        hdata = unwrap(health.json())
        if isinstance(hdata, dict) and hdata.get("models_initialized") and hdata.get("llm_initialized"):
            loaded = hdata.get("loaded_model")
            loaded_lm = hdata.get("loaded_lm_model")
            lm_ok = lm_model_path is None or (
                loaded_lm is not None and str(lm_model_path) in str(loaded_lm)
            )
            if loaded == model and lm_ok:
                print(f"ACE-Step already loaded: {loaded} + LM {loaded_lm} (skip /v1/init)")
                return
    except requests.RequestException:
        pass

    url = urljoin(base_url.rstrip("/") + "/", "v1/init")
    body: dict[str, Any] = {"model": model, "slot": 1}
    if lm_model_path:
        body["init_llm"] = True
        body["lm_model_path"] = lm_model_path
    print(f"Ensuring ACE-Step model: {model}" + (f" + LM {lm_model_path}" if lm_model_path else ""))
    r = requests.post(
        url,
        headers=headers,
        json=body,
        timeout=max(timeout, 300.0),
    )
    if r.status_code >= 400:
        raise RuntimeError(f"/v1/init failed ({r.status_code}): {r.text}")
    data = unwrap(r.json())
    loaded = data.get("loaded_model") if isinstance(data, dict) else None
    loaded_lm = data.get("loaded_lm_model") if isinstance(data, dict) else None
    print(f"  loaded_model={loaded}  loaded_lm={loaded_lm}")


def unwrap(payload: dict[str, Any]) -> Any:
    """ACE-Step wraps responses as {data, code, error, ...}."""
    if "code" in payload and payload.get("code") != 200:
        raise RuntimeError(f"API error code={payload.get('code')}: {payload.get('error')}")
    if "data" in payload:
        return payload["data"]
    return payload


def release_task(
    base_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    *,
    upload_src_audio: Path | None = None,
) -> str:
    """Submit a generation/extract task.

    ACE-Step rejects absolute ``src_audio_path`` outside system temp, so extract
    uploads the local WAV via multipart field ``src_audio``.
    """
    url = urljoin(base_url.rstrip("/") + "/", "release_task")
    auth = {k: v for k, v in headers.items() if k.lower() == "authorization"}

    if upload_src_audio is not None:
        form = {k: ("" if v is None else str(v) if not isinstance(v, bool) else ("true" if v else "false"))
                for k, v in payload.items() if k != "src_audio_path"}
        # bools already handled; keep numbers as strings for form fields
        with open(upload_src_audio, "rb") as audio_f:
            r = requests.post(
                url,
                headers=auth,
                data=form,
                files={"src_audio": (upload_src_audio.name, audio_f, "audio/wav")},
                timeout=timeout,
            )
    else:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)

    if r.status_code >= 400:
        raise RuntimeError(f"release_task failed ({r.status_code}): {r.text}")
    data = unwrap(r.json())
    task_id = data.get("task_id") if isinstance(data, dict) else None
    if not task_id:
        raise RuntimeError(f"No task_id in release_task response: {data}")
    return str(task_id)


def query_result(
    base_url: str,
    headers: dict[str, str],
    task_id: str,
    timeout: float,
) -> dict[str, Any]:
    url = urljoin(base_url.rstrip("/") + "/", "query_result")
    r = requests.post(
        url,
        headers=headers,
        json={"task_id_list": [task_id]},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"query_result failed ({r.status_code}): {r.text}")
    data = unwrap(r.json())
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"Unexpected query_result payload: {data}")
    return data[0]


def wait_for_task(
    base_url: str,
    headers: dict[str, str],
    task_id: str,
    poll_interval: float,
    timeout: float,
    request_timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_status: Any = None
    while time.monotonic() < deadline:
        item = query_result(base_url, headers, task_id, request_timeout)
        status = item.get("status")
        if status != last_status:
            print(f"  status={status} (0=running, 1=ok, 2=failed)")
            last_status = status
        if status == 1:
            return item
        if status == 2:
            raise RuntimeError(f"Generation failed: {item}")
        time.sleep(poll_interval)
    raise TimeoutError(f"Timed out after {timeout:.0f}s waiting for task {task_id}")


def parse_result_files(item: dict[str, Any]) -> list[str]:
    """Extract /v1/audio?... URLs (or relative paths) from a succeeded task."""
    result = item.get("result")
    if result is None:
        # Some builds expose paths directly
        for key in ("audio_paths", "first_audio_path", "file"):
            val = item.get(key)
            if isinstance(val, list) and val:
                return [str(v) for v in val]
            if isinstance(val, str) and val:
                return [val]
        raise RuntimeError(f"Succeeded task had no result field: {item}")

    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Could not parse result JSON string: {result!r}") from exc

    files: list[str] = []
    if isinstance(result, list):
        for entry in result:
            if isinstance(entry, dict) and entry.get("file"):
                files.append(str(entry["file"]))
            elif isinstance(entry, str):
                files.append(entry)
    elif isinstance(result, dict) and result.get("file"):
        files.append(str(result["file"]))

    if not files:
        raise RuntimeError(f"No audio file URLs in result: {result}")
    return files


def resolve_audio_url(base_url: str, file_ref: str) -> str:
    """Turn a result 'file' value into an absolute download URL."""
    if file_ref.startswith("http://") or file_ref.startswith("https://"):
        return file_ref
    if file_ref.startswith("/v1/audio"):
        return urljoin(base_url.rstrip("/") + "/", file_ref.lstrip("/"))
    # Bare filesystem path from server -> wrap with /v1/audio?path=
    from urllib.parse import quote

    return f"{base_url.rstrip('/')}/v1/audio?path={quote(file_ref, safe='')}"


def download_audio(
    url: str,
    dest: Path,
    headers: dict[str, str],
    timeout: float,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Authorization may be needed; Content-Type is not
    get_headers = {k: v for k, v in headers.items() if k != "Content-Type"}
    with requests.get(url, headers=get_headers, stream=True, timeout=timeout) as r:
        if r.status_code >= 400:
            raise RuntimeError(f"Download failed ({r.status_code}) for {url}: {r.text[:500]}")
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    return dest


def run_task_and_download(
    base_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    dest: Path,
    poll_interval: float,
    timeout: float,
    request_timeout: float,
    *,
    upload_src_audio: Path | None = None,
) -> Path:
    task_id = release_task(
        base_url,
        headers,
        payload,
        request_timeout,
        upload_src_audio=upload_src_audio,
    )
    print(f"  task_id={task_id}")
    item = wait_for_task(
        base_url,
        headers,
        task_id,
        poll_interval=poll_interval,
        timeout=timeout,
        request_timeout=request_timeout,
    )
    file_refs = parse_result_files(item)
    audio_url = resolve_audio_url(base_url, file_refs[0])
    print(f"  downloading → {dest}")
    return download_audio(audio_url, dest, headers, request_timeout)


def _segment_to_mono_array(seg: "AudioSegment") -> tuple[np.ndarray, int]:
    """pydub segment → float32 mono samples in [-1, 1] plus sample rate."""
    import numpy as np

    mono = seg.set_channels(1)
    samples = np.array(mono.get_array_of_samples(), dtype=np.float32)
    # pydub uses signed ints at sample_width bytes
    peak = float(1 << (8 * mono.sample_width - 1))
    return samples / peak, mono.frame_rate


def _de_echo_mono(
    samples: np.ndarray,
    sr: int,
    delays_ms: tuple[int, ...] = (40, 55, 70, 90, 110),
    max_alpha: float = 0.45,
) -> np.ndarray:
    """Cancel short slapback: y[t] -= α · y[t-delay] with α from lag correlation."""
    import numpy as np

    out = samples.astype(np.float32, copy=True)
    a = len(out) // 5
    b = min(len(out), a + sr * 20)
    for delay_ms in delays_ms:
        n = int(sr * delay_ms / 1000.0)
        if n <= 0 or a < n or b > len(out):
            continue
        seg = out[a:b]
        lagged = out[a - n : b - n]
        seg_z = seg - seg.mean()
        lag_z = lagged - lagged.mean()
        denom = float(np.dot(seg_z, seg_z) + 1e-12)
        corr = float(np.dot(seg_z, lag_z) / denom)
        alpha = min(max_alpha, max(0.0, corr) * 0.85)
        if alpha < 0.1:
            continue
        out[n:] -= alpha * out[:-n]
    peak = float(np.max(np.abs(out)) + 1e-12)
    if peak > 0.98:
        out *= 0.98 / peak
    return out


def remix_vocals_and_instrument(
    vocals: Path,
    instrument: Path,
    output_path: Path,
    *,
    instrument_gain: float = 1.0,
    vocal_gain: float = 1.0,
) -> Path:
    """Mono dry remix: collapse stereo width and shave short vocal echoes."""
    from pydub import AudioSegment
    import numpy as np

    print(
        f"Remixing {vocals.name} + {instrument.name} "
        f"(mono, de-echo, lute×{instrument_gain:.2f}) …"
    )
    v_seg = AudioSegment.from_wav(vocals)
    i_seg = AudioSegment.from_file(instrument)
    # Match rates before numpy work
    sr = min(v_seg.frame_rate, i_seg.frame_rate)
    v_seg = v_seg.set_frame_rate(sr)
    i_seg = i_seg.set_frame_rate(sr)

    v, _ = _segment_to_mono_array(v_seg)
    i, _ = _segment_to_mono_array(i_seg)
    v = _de_echo_mono(v, sr) * float(vocal_gain)
    i = i * float(instrument_gain)
    n = min(len(v), len(i))
    mixed = v[:n] + i[:n]
    peak = float(np.max(np.abs(mixed)) + 1e-12)
    if peak > 0.98:
        mixed *= 0.98 / peak

    pcm = (mixed * 32767.0).astype(np.int16)
    out = AudioSegment(
        data=pcm.tobytes(),
        sample_width=2,
        frame_rate=sr,
        channels=1,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.export(output_path, format="wav")
    return output_path


def purify_with_acestep_extract(
    base_url: str,
    headers: dict[str, str],
    src_path: Path,
    output_path: Path,
    extract_model: str,
    instrument_track: str,
    poll_interval: float,
    timeout: float,
    request_timeout: float,
    inference_steps: int,
) -> Path:
    """Isolate vocals + instrument via ACE-Step extract (base/XL model), then remix."""
    src = src_path.resolve()
    if not src.is_file():
        raise FileNotFoundError(src)

    stems_dir = output_path.parent / f"{output_path.stem}_extract_stems"
    stems_dir.mkdir(parents=True, exist_ok=True)
    vocals_path = stems_dir / "vocals.wav"
    instrument_path = stems_dir / f"{instrument_track}.wav"

    for track, dest in (("vocals", vocals_path), (instrument_track, instrument_path)):
        print(f"ACE-Step extract [{track}] via {extract_model} …")
        payload = {
            "task_type": "extract",
            "track_name": track,
            "instruction": f"Extract the {track} track from the audio:",
            "model": extract_model,
            "audio_format": "wav",
            "thinking": False,
            "inference_steps": inference_steps,
            "batch_size": 1,
            "use_cot_caption": False,
            "use_cot_language": False,
        }
        run_task_and_download(
            base_url,
            headers,
            payload,
            dest,
            poll_interval=poll_interval,
            timeout=timeout,
            request_timeout=request_timeout,
            upload_src_audio=src,
        )

    return remix_vocals_and_instrument(vocals_path, instrument_path, output_path)


def _demucs_env_and_device(device: str | None) -> tuple[dict[str, str], str | None]:
    """Map cuda:N / N onto a Demucs -d flag + subprocess env (pin secondary GPU).

    ACE-Step stays on its own GPU (e.g. CUDA_VISIBLE_DEVICES=0 for the API).
    Demucs can use another physical GPU via --demucs-device cuda:1.
    """
    env = os.environ.copy()
    if not device:
        return env, None

    d = device.strip().lower()
    if d in {"cpu", "mps"}:
        return env, d

    # cuda / cuda:0 / 0 / gpu:1
    d = d.removeprefix("gpu:")
    if d == "cuda":
        return env, "cuda"
    if d.startswith("cuda:"):
        idx = d.split(":", 1)[1]
    elif d.isdigit():
        idx = d
    else:
        return env, device

    # Hide other GPUs so Demucs -d cuda binds to the chosen physical index
    env["CUDA_VISIBLE_DEVICES"] = idx
    return env, "cuda"


def purify_with_demucs(
    input_path: Path,
    output_path: Path,
    stems_dir: Path,
    device: str | None,
) -> Path:
    """4-stem Demucs split, then remix vocals + other (drop drums/bass).

    Note: --two-stems=vocals keeps drums/bass inside no_vocals.wav, so we use
    a full 4-stem split and discard drums.wav / bass.wav.
    """
    if shutil.which("demucs") is None and shutil.which("python") is None:
        raise SystemExit("demucs not found. Install with: uv sync --extra purify")

    stems_dir.mkdir(parents=True, exist_ok=True)
    env, demucs_device = _demucs_env_and_device(device)
    cmd = [
        sys.executable,
        "-m",
        "demucs",
        "-n",
        "htdemucs",
        "--out",
        str(stems_dir),
        str(input_path),
    ]
    if demucs_device:
        cmd.extend(["-d", demucs_device])

    visible = env.get("CUDA_VISIBLE_DEVICES")
    pin_note = f" (physical GPU {visible})" if visible and demucs_device == "cuda" else ""
    print(f"Purifying with Demucs{pin_note}: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, env=env)
    except FileNotFoundError as exc:
        raise SystemExit(
            "Could not run demucs. Install the purify extra:\n"
            "  uv sync --extra purify"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Demucs failed with exit code {exc.returncode}") from exc

    track_dir = stems_dir / "htdemucs" / input_path.stem
    vocals = track_dir / "vocals.wav"
    other = track_dir / "other.wav"
    if not vocals.is_file() or not other.is_file():
        raise RuntimeError(
            f"Expected vocals.wav and other.wav under {track_dir}, found: "
            f"{list(track_dir.glob('*')) if track_dir.is_dir() else 'missing dir'}"
        )

    print("Remixing vocals + other (guitar/instrument), dropping drums & bass…")
    return remix_vocals_and_instrument(vocals, other, output_path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api-base", default=os.getenv("ACESTEP_API_BASE", "http://localhost:8001"))
    p.add_argument("--api-key", default=os.getenv("ACESTEP_API_KEY") or None)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument(
        "--song",
        default=None,
        metavar="SLUG",
        help=(
            "Song slug → lyrics/<slug>.txt (+ prompts/<slug>.txt unless --style) "
            f"(available: {', '.join(list_songs()) or 'none yet'})"
        ),
    )
    p.add_argument(
        "--style",
        default=None,
        metavar="SLUG",
        help=(
            "Style preset from style_presets/<slug>.txt "
            f"(available: {', '.join(list_styles()) or 'none yet'}; "
            f"recommended for Skyrim: {DEFAULT_STYLE})"
        ),
    )
    p.add_argument(
        "--list-songs",
        action="store_true",
        help="List available song slugs and exit",
    )
    p.add_argument(
        "--list-styles",
        action="store_true",
        help="List available style presets and exit",
    )
    p.add_argument(
        "--name",
        default=None,
        help="Output filename (default: <song-slug>.wav or npc_campfire_song.wav)",
    )
    p.add_argument(
        "--prompt-file",
        type=Path,
        default=None,
        help="Prompt text file (default from --song or campfire_guitar)",
    )
    p.add_argument(
        "--lyrics-file",
        type=Path,
        default=None,
        help="Lyrics text file (default from --song or campfire_guitar)",
    )
    p.add_argument("--prompt", default=None, help="Inline prompt (overrides --prompt-file)")
    p.add_argument("--lyrics", default=None, help="Inline lyrics (overrides --lyrics-file)")
    p.add_argument(
        "--vocalist",
        required=True,
        choices=["male", "female"],
        help="Required: solo male or solo female singer (injected into prompt tags)",
    )
    p.add_argument(
        "--instrument",
        default=DEFAULT_INSTRUMENT,
        choices=list_instruments(),
        help=(
            "Solo accompaniment instrument slotted into style {instrument} "
            f"(default: {DEFAULT_INSTRUMENT}; "
            f"available: {', '.join(list_instruments())})"
        ),
    )
    p.add_argument(
        "--list-instruments",
        action="store_true",
        help="List available --instrument choices and exit",
    )
    p.add_argument("--vocal-language", default="en")
    p.add_argument("--audio-format", default="wav", choices=["wav", "mp3", "flac", "opus", "aac", "wav32"])
    p.add_argument(
        "--bpm",
        type=int,
        default=None,
        help="Tempo (default: [N bpm] tag in prompt file, else 85)",
    )
    p.add_argument("--duration", type=float, default=None, help="Target length in seconds (10-600)")
    p.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument(
        "--quality",
        choices=sorted(QUALITY_PRESETS.keys()),
        default=os.getenv("MUSIC_GEN_QUALITY", "default"),
        help="Model preset: default=2B SFT+0.6B, sft-1.7b, xl=XL turbo, xl-sft (tight VRAM)",
    )
    p.add_argument(
        "--inference-steps",
        type=int,
        default=None,
        help="Diffusion steps (default from --quality preset)",
    )
    p.add_argument(
        "--guidance-scale",
        type=float,
        default=None,
        help="CFG scale for SFT/base models (default from --quality preset)",
    )
    p.add_argument("--batch-size", type=int, default=1)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.add_argument("--timeout", type=float, default=600.0, help="Max seconds to wait for generation")
    p.add_argument("--request-timeout", type=float, default=60.0)
    p.add_argument(
        "--purify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="After generate: Demucs 4-stem split, keep vocals+other only (default: on)",
    )
    p.add_argument(
        "--compare",
        action="store_true",
        help=(
            "A/B/C export from one take: "
            "<name>_acestep_only.wav, <name>_acestep_demucs.wav, "
            "<name>_acestep_extract.wav. Implies Demucs + ACE-Step extract."
        ),
    )
    p.add_argument(
        "--acestep-extract",
        action="store_true",
        help="After generate: ACE-Step extract vocals+instrument, then remix",
    )
    p.add_argument(
        "--extract-model",
        default=os.getenv("ACESTEP_EXTRACT_MODEL", "acestep-v15-base"),
        help="DiT model for extract (must support extract; default acestep-v15-base)",
    )
    p.add_argument(
        "--extract-instrument",
        default="guitar",
        help="Instrument track name for extract (guitar, keyboard, strings, …)",
    )
    p.add_argument(
        "--extract-steps",
        type=int,
        default=32,
        help="Inference steps for extract tasks (base models prefer 32–64)",
    )
    p.add_argument("--stems-dir", type=Path, default=DEFAULT_STEMS_DIR)
    p.add_argument(
        "--demucs-device",
        default=os.getenv("DEMUCS_DEVICE") or None,
        help="cpu | mps | cuda | cuda:N (mps=Apple Silicon; cuda:N pins Demucs to GPU N)",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Override DiT model id (default from --quality)",
    )
    p.add_argument(
        "--lm-model",
        default=None,
        help="Override LM id for /v1/init (default from --quality)",
    )
    p.add_argument(
        "--skip-model-init",
        action="store_true",
        help="Skip POST /v1/init before generate",
    )
    p.add_argument("--skip-health", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    _load_dotenv(ROOT / ".env")
    # Allow list commands without --vocalist
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if "--list-songs" in raw_argv:
        for slug in list_songs():
            print(slug)
        return 0
    if "--list-styles" in raw_argv:
        for slug in list_styles():
            print(slug)
        return 0
    if "--list-instruments" in raw_argv:
        for slug in list_instruments():
            print(slug)
        return 0

    args = build_parser().parse_args(argv)

    if args.compare:
        args.purify = True
        args.acestep_extract = True

    quality = QUALITY_PRESETS[args.quality]
    model = args.model or quality["model"]
    lm_model = args.lm_model or quality["lm_model_path"]

    style_slug = args.style
    style_meta: dict[str, Any] = {}
    if style_slug:
        style_template, style_meta = load_style_preset(style_slug)

    # Style meta overrides quality defaults (CLI still wins).
    inference_steps = args.inference_steps
    if inference_steps is None:
        inference_steps = style_meta.get("inference_steps", quality["inference_steps"])
    guidance_scale = args.guidance_scale
    if guidance_scale is None:
        guidance_scale = style_meta.get("guidance_scale", quality["guidance_scale"])
    lm_negative = style_meta.get("lm_negative_prompt", DEFAULT_LM_NEGATIVE_PROMPT)
    lm_negative = (
        f"{lm_negative}, {VOCALIST_LM_NEGATIVES[args.vocalist]}, "
        f"{INSTRUMENT_LM_NEGATIVES[args.instrument]}"
    )

    song_slug = args.song
    if song_slug:
        song_slug = slugify_title(song_slug) if " " in song_slug else song_slug
        prompt_path, lyrics_path = resolve_song_paths(
            song_slug, require_prompt=not bool(style_slug or args.prompt)
        )
        if args.prompt_file is None and prompt_path is not None:
            args.prompt_file = prompt_path
        if args.lyrics_file is None:
            args.lyrics_file = lyrics_path
    else:
        if args.lyrics_file is None:
            args.lyrics_file = DEFAULT_LYRICS_FILE
        if args.prompt_file is None and not style_slug:
            args.prompt_file = DEFAULT_PROMPT_FILE
        song_slug = (args.lyrics_file or DEFAULT_LYRICS_FILE).stem

    if args.name is None:
        args.name = f"{song_slug}.wav"

    if args.lyrics is not None:
        lyrics = args.lyrics
    else:
        if not args.lyrics_file.is_file():
            raise SystemExit(f"Lyrics file not found: {args.lyrics_file}")
        lyrics = args.lyrics_file.read_text(encoding="utf-8").strip()

    if args.prompt is not None:
        prompt = apply_instrument_tags(
            apply_vocalist_tags(args.prompt, args.vocalist),
            args.instrument,
        )
        prompt_source = "(inline)"
    elif style_slug:
        # Style preset owns the style prompt; song files only supply lyrics.
        prompt = apply_style_template(style_template, args.vocalist, args.instrument)
        prompt_source = f"style:{style_slug}"
    else:
        if args.prompt_file is None or not args.prompt_file.is_file():
            raise SystemExit(
                "No prompt source. Pass --style <preset>, --prompt-file, or --prompt."
            )
        prompt = apply_instrument_tags(
            apply_vocalist_tags(
                args.prompt_file.read_text(encoding="utf-8").strip(),
                args.vocalist,
            ),
            args.instrument,
        )
        prompt_source = str(args.prompt_file)

    if args.bpm is None:
        args.bpm = (
            parse_bpm_from_prompt(prompt)
            or style_meta.get("default_bpm")
            or 85
        )

    headers = api_headers(args.api_key)
    if not args.skip_health:
        print(f"Checking ACE-Step health at {args.api_base} …")
        check_health(args.api_base, headers, args.request_timeout)
        print("  OK")

    if not args.skip_model_init:
        ensure_model(
            args.api_base,
            headers,
            model=model,
            lm_model_path=lm_model,
            timeout=args.request_timeout,
        )

    print(f"Song: {song_slug}")
    if style_slug:
        print(f"Style: {style_slug} — {style_meta.get('description', '')}".rstrip(" —"))
    print(f"Quality: {args.quality} — {quality['label']}")
    print(f"DiT: {model}  steps={inference_steps}  guidance={guidance_scale}  bpm={args.bpm}")
    print(f"Prompt: {prompt_source}")
    print(f"Lyrics: {args.lyrics_file if args.lyrics is None else '(inline)'}")
    print(f"Vocalist: {args.vocalist}")
    print(f"Instrument: {args.instrument}")

    payload: dict[str, Any] = {
        "prompt": prompt,
        "lyrics": lyrics,
        "vocal_language": args.vocal_language,
        "audio_format": args.audio_format,
        "thinking": args.thinking,
        "inference_steps": inference_steps,
        "batch_size": args.batch_size,
        "bpm": args.bpm,
        "model": model,
        "lm_model_path": lm_model,
        "lm_negative_prompt": lm_negative,
        # Keep CoT from rewriting a carefully constrained single-instrument prompt
        "use_cot_caption": False,
        "use_cot_language": False,
    }
    if guidance_scale is not None:
        payload["guidance_scale"] = guidance_scale
    if args.duration is not None:
        payload["audio_duration"] = args.duration
    if args.seed is not None:
        payload["use_random_seed"] = False
        payload["seed"] = args.seed

    print("Requesting NPC song from ACE-Step …")
    task_id = release_task(args.api_base, headers, payload, args.request_timeout)
    print(f"  task_id={task_id}")

    item = wait_for_task(
        args.api_base,
        headers,
        task_id,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
        request_timeout=args.request_timeout,
    )

    file_refs = parse_result_files(item)
    raw_name = args.name
    # Keep extension aligned with format if user passed a bare name
    if Path(raw_name).suffix == "":
        raw_name = f"{raw_name}.{args.audio_format if args.audio_format != 'wav32' else 'wav'}"

    stem = Path(raw_name).stem
    suffix = Path(raw_name).suffix or ".wav"
    if args.compare:
        raw_path = args.output_dir / f"{stem}_acestep_only{suffix}"
        demucs_path = args.output_dir / f"{stem}_acestep_demucs.wav"
        extract_path = args.output_dir / f"{stem}_acestep_extract.wav"
    else:
        raw_path = args.output_dir / (
            raw_name if not (args.purify or args.acestep_extract) else f"{stem}_raw{suffix}"
        )
        demucs_path = args.output_dir / raw_name
        extract_path = args.output_dir / (
            raw_name if not args.purify else f"{stem}_acestep_extract.wav"
        )

    audio_url = resolve_audio_url(args.api_base, file_refs[0])
    print(f"Downloading {audio_url} → {raw_path}")
    download_audio(audio_url, raw_path, headers, args.request_timeout)

    did_cleanup = False
    if args.purify:
        purify_with_demucs(raw_path, demucs_path, args.stems_dir, args.demucs_device)
        did_cleanup = True

    if args.acestep_extract:
        purify_with_acestep_extract(
            args.api_base,
            headers,
            raw_path,
            extract_path,
            extract_model=args.extract_model,
            instrument_track=args.extract_instrument,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
            request_timeout=args.request_timeout,
            inference_steps=args.extract_steps,
        )
        did_cleanup = True

    if args.compare:
        print()
        print("Compare (same ACE-Step take):")
        print(f"  A  ACE-Step only   : {raw_path}")
        print(f"  B  ACE-Step+Demucs : {demucs_path}")
        print(f"  C  ACE-Step extract: {extract_path}")
        print("A=prompt only; B=Demucs clean; C=ACE-Step extract vocals+instrument.")
    elif did_cleanup:
        print()
        if args.purify:
            print(f"Demucs clean: {demucs_path}")
        if args.acestep_extract:
            print(f"ACE-Step extract clean: {extract_path}")
        print(f"Raw generate kept at: {raw_path}")
    else:
        print(f"Done. Mod asset saved to: {raw_path}")
        print("Tip: re-run with --compare for A/B/C (raw vs Demucs vs ACE-Step extract).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
