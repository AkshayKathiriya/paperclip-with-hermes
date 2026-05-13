"""
generate_ai_images.py
Generates a still image from a text prompt via OpenAI's gpt-image-1.

Used for reenactment-style shots and conceptual visuals the Scene Director
flags as `ai_image` source. Output is a landscape PNG that downstream
Ken-Burns motion turns into a 4-8s video shot.

Pricing note (gpt-image-1):
  low    quality, 1024x1024  ~$0.011
  medium quality, 1536x1024  ~$0.042   ← default
  high   quality, 1536x1024  ~$0.167
With ~10 ai_image shots per video, default cost is ~$0.42/video.
"""

import os
import base64
import logging
import requests

log = logging.getLogger(__name__)

OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"

DEFAULT_MODEL   = "gpt-image-1"
DEFAULT_SIZE    = "1536x1024"   # 3:2 landscape, we crop to 16:9 later
DEFAULT_QUALITY = "medium"


class AIImageError(RuntimeError):
    """Raised for any image-generation failure that should fail a shot loudly."""


def _api_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise AIImageError("OPENAI_API_KEY is not set")
    return key


def generate_image(
    prompt: str,
    out_path: str,
    size: str = DEFAULT_SIZE,
    quality: str = DEFAULT_QUALITY,
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Generate one image from a prompt and write it to out_path (PNG).
    Returns the local path.
    """
    if not prompt or len(prompt) < 10:
        raise AIImageError(f"Prompt too short / empty: '{prompt[:40]}'")

    api_key = _api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size,
        "quality": quality,
    }

    log.info(f"  → gpt-image-1 ({quality}, {size}): {prompt[:80]}")
    try:
        r = requests.post(OPENAI_IMAGES_URL, headers=headers, json=body, timeout=120)
    except requests.RequestException as e:
        raise AIImageError(f"OpenAI network error: {e}") from e

    if r.status_code == 401:
        raise AIImageError("OpenAI 401 — OPENAI_API_KEY is invalid")
    if r.status_code == 429:
        raise AIImageError("OpenAI 429 — rate limit / quota exceeded")
    if r.status_code == 400:
        raise AIImageError(f"OpenAI 400 — bad request: {r.text[:300]}")
    if r.status_code >= 500:
        raise AIImageError(f"OpenAI server error {r.status_code}")
    if r.status_code != 200:
        raise AIImageError(f"OpenAI HTTP {r.status_code}: {r.text[:300]}")

    data = r.json()
    entries = data.get("data") or []
    if not entries:
        raise AIImageError(f"OpenAI returned no image data: {data}")

    entry = entries[0]
    # gpt-image-1 returns b64_json by default
    b64 = entry.get("b64_json")
    image_url = entry.get("url")

    if b64:
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(b64))
    elif image_url:
        # Some endpoints return a download URL instead
        with requests.get(image_url, stream=True, timeout=60) as ir:
            ir.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in ir.iter_content(chunk_size=64 * 1024):
                    f.write(chunk)
    else:
        raise AIImageError(f"OpenAI response had neither b64_json nor url: {entry}")

    size_kb = os.path.getsize(out_path) // 1024
    log.info(f"  → saved {size_kb} KB → {out_path}")
    return out_path
