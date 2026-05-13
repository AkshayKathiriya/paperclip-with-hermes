"""
ken_burns.py
Convert a still image into a short video clip with subtle Ken Burns motion
(slow zoom + pan) so it doesn't look like a static slideshow.

We use FFmpeg's zoompan filter — no frames pass through Python memory.

Three motion variants are cycled per shot so adjacent stills don't all
zoom in identically:
  zoom_in   — slow zoom toward center
  zoom_out  — slow zoom away from center
  pan_right — gentle pan from left to right while holding zoom
"""

import os
import subprocess
import logging

log = logging.getLogger(__name__)

OUT_W = 1920
OUT_H = 1080
FPS   = 30

# We pre-upscale to give zoompan enough resolution for smooth motion.
WORK_W = 3840
WORK_H = 2160

# Motion variants — keyed by index for round-robin cycling.
_VARIANTS = ("zoom_in", "zoom_out", "pan_right", "pan_left", "zoom_in_topleft")


def still_to_clip(image_path: str, out_path: str, duration_sec: float, variant_idx: int = 0) -> str:
    """
    Render a still image as a duration_sec MP4 clip with Ken Burns motion.
    Returns out_path on success.
    """
    if duration_sec < 0.5:
        raise ValueError(f"duration_sec must be >= 0.5, got {duration_sec}")

    frames = max(int(round(duration_sec * FPS)), 1)
    variant = _VARIANTS[variant_idx % len(_VARIANTS)]
    vf = _build_filter(variant, frames)

    # NOTE: zoompan's d= is frames-per-input-frame. With `-loop 1` we have
    # infinite input frames, so we MUST cap the output explicitly with
    # `-frames:v` — otherwise we get duration_sec × FPS × FPS frames.
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", image_path,
        "-vf", vf,
        "-frames:v", str(frames),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-an",
        out_path,
    ]

    log.info(f"  → ken_burns variant={variant} dur={duration_sec:.1f}s frames={frames}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        log.error(f"FFmpeg ken_burns failed:\n{result.stderr[-700:]}")
        raise RuntimeError(f"ken_burns FFmpeg error: {result.stderr[-300:]}")

    return out_path


def _build_filter(variant: str, frames: int) -> str:
    """
    Build the FFmpeg filter chain for the chosen Ken Burns variant.
    All variants:
      1. scale to a pre-zoompan working resolution
      2. zoompan with motion expressions
      3. fps lock
    """
    scale = f"scale={WORK_W}:{WORK_H}:force_original_aspect_ratio=increase,crop={WORK_W}:{WORK_H}"

    # zoompan parameters
    # z = zoom factor (1.0 = no zoom). Range typically 1.0 → 1.15
    # x, y = pan position (0..(iw-iw/zoom), 0..(ih-ih/zoom))
    if variant == "zoom_in":
        zp = (
            f"zoompan=z='min(zoom+0.0006,1.18)'"
            f":d={frames}"
            f":x='iw/2-(iw/zoom/2)'"
            f":y='ih/2-(ih/zoom/2)'"
            f":s={OUT_W}x{OUT_H}:fps={FPS}"
        )
    elif variant == "zoom_out":
        zp = (
            f"zoompan=z='if(eq(on,0),1.18,max(zoom-0.0006,1.0))'"
            f":d={frames}"
            f":x='iw/2-(iw/zoom/2)'"
            f":y='ih/2-(ih/zoom/2)'"
            f":s={OUT_W}x{OUT_H}:fps={FPS}"
        )
    elif variant == "pan_right":
        zp = (
            f"zoompan=z='1.12'"
            f":d={frames}"
            f":x='(iw-iw/zoom)*on/{frames}'"
            f":y='ih/2-(ih/zoom/2)'"
            f":s={OUT_W}x{OUT_H}:fps={FPS}"
        )
    elif variant == "pan_left":
        zp = (
            f"zoompan=z='1.12'"
            f":d={frames}"
            f":x='(iw-iw/zoom)*(1-on/{frames})'"
            f":y='ih/2-(ih/zoom/2)'"
            f":s={OUT_W}x{OUT_H}:fps={FPS}"
        )
    else:  # zoom_in_topleft
        zp = (
            f"zoompan=z='min(zoom+0.0006,1.15)'"
            f":d={frames}"
            f":x='0'"
            f":y='0'"
            f":s={OUT_W}x{OUT_H}:fps={FPS}"
        )

    return f"{scale},{zp}"
