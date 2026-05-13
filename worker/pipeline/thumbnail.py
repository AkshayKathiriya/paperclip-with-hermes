"""
thumbnail.py
Generate a YouTube thumbnail (1280x720) for a video.

Inputs from Production Manager's `thumbnail_brief`:
  prompt        — DALL-E prompt describing the background scene
  overlay_text  — short shocking text (e.g. "₹7,136 CR")
  overlay_color — hex color for the overlay text

Output:
  thumbnail.jpg in work_dir

Process:
  1. Generate base image via gpt-image-1 (landscape 1536x1024)
  2. Crop to 16:9 (1280x720)
  3. Burn overlay text with FFmpeg drawtext (bold, with stroke + shadow)
"""

import os
import subprocess
import logging

from pipeline.generate_ai_images import generate_image, AIImageError

log = logging.getLogger(__name__)

THUMB_W = 1280
THUMB_H = 720


class ThumbnailError(RuntimeError):
    pass


def generate_thumbnail(brief: dict, work_dir: str) -> str:
    """
    Build the final thumbnail.jpg. Returns its absolute path.
    """
    prompt        = (brief.get("prompt") or "").strip()
    overlay_text  = (brief.get("overlay_text") or "").strip()
    overlay_color = (brief.get("overlay_color") or "#FF3B30").strip()

    if not prompt:
        raise ThumbnailError("thumbnail_brief.prompt is required")

    os.makedirs(work_dir, exist_ok=True)
    base_image = os.path.join(work_dir, "thumbnail_base.png")
    final      = os.path.join(work_dir, "thumbnail.jpg")

    # Strengthen the prompt for thumbnail-style aesthetics
    enhanced_prompt = (
        f"YouTube thumbnail background. {prompt}. "
        "Dramatic cinematic lighting, deep contrast, dark background with one bright focal subject, "
        "photorealistic, no text, no logos, vertical 16:9 composition, eye-catching."
    )

    try:
        generate_image(enhanced_prompt, base_image, size="1536x1024", quality="medium")
    except AIImageError as e:
        raise ThumbnailError(f"AI image generation failed for thumbnail: {e}") from e

    _burn_overlay(base_image, final, overlay_text, overlay_color)
    return final


def _burn_overlay(src: str, dst: str, text: str, color: str) -> None:
    """
    Crop the base image to 1280x720 and burn overlay text.
    If text is empty, just crop+save.
    """
    # FFmpeg drawtext needs : and ' escaped
    safe_text = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", r"\'")

    crop_chain = f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=increase,crop={THUMB_W}:{THUMB_H}"

    if safe_text:
        drawtext = (
            f"drawtext="
            f"text='{safe_text}'"
            f":fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            f":fontcolor={color}"
            f":fontsize=160"
            f":borderw=8"
            f":bordercolor=black"
            f":shadowcolor=black@0.7"
            f":shadowx=4"
            f":shadowy=4"
            f":x=(w-text_w)/2"
            f":y=h-text_h-60"
        )
        vf = f"{crop_chain},{drawtext}"
    else:
        vf = crop_chain

    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-vf", vf,
        "-frames:v", "1",
        "-q:v", "2",
        dst,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        log.error(f"thumbnail overlay failed:\n{result.stderr[-500:]}")
        raise ThumbnailError(f"FFmpeg drawtext error: {result.stderr[-200:]}")

    log.info(f"thumbnail saved: {dst} ({os.path.getsize(dst) // 1024} KB)")
