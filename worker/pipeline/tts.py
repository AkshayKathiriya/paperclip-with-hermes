"""
tts.py
Generates a voiceover WAV file from the narration script using Piper TTS.
Piper runs fully on CPU — no GPU needed. Fast enough for Railway.

Voice model: en_US-lessac-medium (natural sounding, free, ~65MB)
Download happens automatically on first run.
"""

import os
import subprocess
import logging
import urllib.request

log = logging.getLogger(__name__)

# Piper voice model — can be swapped to any Piper voice
VOICE_MODEL  = "en_US-lessac-medium"
MODEL_DIR    = os.getenv("PIPER_MODEL_DIR", "/paperclip/piper-models")
MODEL_URL    = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/{VOICE_MODEL}.onnx"
CONFIG_URL   = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/{VOICE_MODEL}.onnx.json"


def generate_voiceover(script: str, work_dir: str, speed: float = 0.92) -> str:
    """
    Converts the narration script to a WAV file using Piper TTS.

    Args:
        script:   Full narration text
        work_dir: Directory to save output files
        speed:    Speaking rate (0.8=slow, 1.0=normal, 1.2=fast)

    Returns:
        Path to the generated WAV file
    """
    os.makedirs(MODEL_DIR, exist_ok=True)

    model_path  = os.path.join(MODEL_DIR, f"{VOICE_MODEL}.onnx")
    config_path = os.path.join(MODEL_DIR, f"{VOICE_MODEL}.onnx.json")

    # download model if not already cached
    _ensure_model(model_path, MODEL_URL)
    _ensure_model(config_path, CONFIG_URL)

    # write script to a temp text file
    script_path = os.path.join(work_dir, "script.txt")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)

    # output audio path
    audio_path = os.path.join(work_dir, "narration.wav")

    log.info(f"Running Piper TTS on {len(script)} chars...")

    # run piper via subprocess — reads from stdin, writes WAV to file
    cmd = [
        "piper",
        "--model", model_path,
        "--config", config_path,
        "--output_file", audio_path,
        "--length_scale", str(round(1.0 / speed, 2)),   # piper uses length_scale (inverse of speed)
        "--noise_scale", "0.667",
        "--noise_w", "0.8",
    ]

    with open(script_path, "r") as stdin_file:
        result = subprocess.run(
            cmd,
            stdin=stdin_file,
            capture_output=True,
            text=True,
            timeout=900,   # 15 minute timeout — needed for 700-900 word scripts on CPU
        )

    if result.returncode != 0:
        log.error(f"Piper TTS failed:\n{result.stderr}")
        raise RuntimeError(f"Piper TTS error: {result.stderr[:500]}")

    size_kb = os.path.getsize(audio_path) // 1024
    log.info(f"Voiceover generated: {audio_path} ({size_kb} KB)")

    return audio_path


def _ensure_model(path: str, url: str) -> None:
    """Downloads the model file if it doesn't exist locally."""
    if os.path.exists(path):
        log.info(f"Model cached: {path}")
        return

    log.info(f"Downloading model: {url}")
    log.info("This only happens once — model is cached for future runs")
    try:
        urllib.request.urlretrieve(url, path)
        log.info(f"Model downloaded: {path} ({os.path.getsize(path) // 1024 // 1024} MB)")
    except Exception as e:
        log.error(f"Failed to download model from {url}: {e}")
        raise
