"""
tts.py — ElevenLabs narration TTS

Replaces the previous Piper implementation. ElevenLabs gives us
documentary-grade voice quality (worth the $9.99/mo for Case Closed).

Design:
- Split scripts into ≤4500-char chunks at sentence boundaries (ElevenLabs
  has a per-request character limit; chunking also keeps prosody stable
  on long narrations).
- Stream each chunk to MP3 on disk.
- Concatenate MP3s losslessly with FFmpeg's concat demuxer.
- Return a single audio file path that the rest of the pipeline consumes
  the same way as the old Piper output.
"""

import os
import re
import subprocess
import logging
import requests

log = logging.getLogger(__name__)

ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

# Per-request char limit. ElevenLabs allows up to 5000 on most paid tiers,
# we stay under it to allow for some breathing room.
CHUNK_MAX_CHARS = 4500

# Default voice settings — tuned for documentary narration.
DEFAULT_VOICE_SETTINGS = {
    "stability": 0.45,         # lower = more emotional range, higher = consistent
    "similarity_boost": 0.85,  # how closely to match the reference voice
    "style": 0.35,             # 0 = flat, 1 = expressive
    "use_speaker_boost": True,
}


class TTSError(RuntimeError):
    """Raised for any TTS-related failure that should fail a job loudly."""


def _config() -> tuple[str, str, str]:
    api_key  = os.getenv("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    model    = os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5").strip()
    if not api_key:
        raise TTSError("ELEVENLABS_API_KEY is not set")
    if not voice_id:
        raise TTSError("ELEVENLABS_VOICE_ID is not set")
    return api_key, voice_id, model


def generate_voiceover(script: str, work_dir: str, speed: float = 1.0) -> str:
    """
    Generate narration MP3 from a script via ElevenLabs.

    Args:
        script:   Full narration text (any length)
        work_dir: Directory to save output and intermediate files
        speed:    Speaking rate hint (currently logged only; ElevenLabs
                  doesn't expose a speed parameter on most models)
    Returns:
        Path to the generated MP3 file
    """
    api_key, voice_id, model = _config()
    os.makedirs(work_dir, exist_ok=True)

    chunks = _split_into_chunks(script, CHUNK_MAX_CHARS)
    log.info(f"ElevenLabs TTS: {len(script)} chars → {len(chunks)} chunk(s)")

    chunk_paths: list[str] = []
    for i, chunk in enumerate(chunks):
        out = os.path.join(work_dir, f"narration_chunk_{i:02d}.mp3")
        _synthesize_chunk(chunk, out, api_key, voice_id, model)
        chunk_paths.append(out)

    final_path = os.path.join(work_dir, "narration.mp3")
    if len(chunk_paths) == 1:
        os.replace(chunk_paths[0], final_path)
    else:
        _concat_mp3s(chunk_paths, final_path, work_dir)
        for p in chunk_paths:
            try:
                os.remove(p)
            except OSError:
                pass

    size_kb = os.path.getsize(final_path) // 1024
    log.info(f"Voiceover ready: {final_path} ({size_kb} KB)")
    return final_path


# ── splitting ─────────────────────────────────────────────────────────────────

def _split_into_chunks(text: str, max_chars: int) -> list[str]:
    """
    Split at sentence boundaries to keep each chunk under max_chars.
    Falls back to splitting at any whitespace if a single sentence is huge.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    # Split into sentences (keep terminator on each).
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        if not s:
            continue
        if len(s) > max_chars:
            # A single absurdly long sentence — split on whitespace as fallback.
            if buf:
                chunks.append(buf.strip())
                buf = ""
            words = s.split()
            inner = ""
            for w in words:
                if len(inner) + len(w) + 1 > max_chars:
                    chunks.append(inner.strip())
                    inner = w
                else:
                    inner = f"{inner} {w}" if inner else w
            if inner:
                buf = inner
            continue

        if len(buf) + len(s) + 1 > max_chars:
            chunks.append(buf.strip())
            buf = s
        else:
            buf = f"{buf} {s}" if buf else s

    if buf:
        chunks.append(buf.strip())
    return chunks


# ── ElevenLabs call ───────────────────────────────────────────────────────────

def _synthesize_chunk(
    text: str,
    out_path: str,
    api_key: str,
    voice_id: str,
    model: str,
) -> None:
    """POST one chunk to ElevenLabs, stream MP3 bytes to disk."""
    url = ELEVENLABS_API_URL.format(voice_id=voice_id)
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": api_key,
    }
    body = {
        "text": text,
        "model_id": model,
        "voice_settings": DEFAULT_VOICE_SETTINGS,
    }

    log.info(f"  → POST ElevenLabs ({len(text)} chars, model={model})")
    try:
        with requests.post(url, headers=headers, json=body, stream=True, timeout=180) as r:
            if r.status_code == 401:
                raise TTSError("ElevenLabs returned 401 — ELEVENLABS_API_KEY is invalid")
            if r.status_code == 422:
                raise TTSError(f"ElevenLabs 422 (bad voice/model?): {r.text[:300]}")
            if r.status_code == 429:
                raise TTSError("ElevenLabs rate limit / quota exceeded (429)")
            if r.status_code >= 500:
                raise TTSError(f"ElevenLabs server error {r.status_code}: {r.text[:200]}")
            if r.status_code != 200:
                raise TTSError(f"ElevenLabs HTTP {r.status_code}: {r.text[:300]}")

            with open(out_path, "wb") as f:
                for piece in r.iter_content(chunk_size=64 * 1024):
                    f.write(piece)
    except requests.RequestException as e:
        raise TTSError(f"ElevenLabs network error: {e}") from e

    if os.path.getsize(out_path) < 1024:
        raise TTSError(f"ElevenLabs returned suspiciously small audio ({out_path})")

    log.info(f"  → saved {os.path.getsize(out_path) // 1024} KB → {out_path}")


# ── concat ────────────────────────────────────────────────────────────────────

def _concat_mp3s(parts: list[str], out_path: str, work_dir: str) -> None:
    """Concatenate MP3 files losslessly via FFmpeg concat demuxer."""
    manifest = os.path.join(work_dir, "narration_concat.txt")
    with open(manifest, "w") as f:
        for p in parts:
            esc = p.replace("'", r"\'")
            f.write(f"file '{esc}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", manifest,
        "-c", "copy",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        log.error(f"MP3 concat failed:\n{result.stderr[-500:]}")
        raise TTSError(f"FFmpeg concat failed: {result.stderr[-200:]}")

    try:
        os.remove(manifest)
    except OSError:
        pass
