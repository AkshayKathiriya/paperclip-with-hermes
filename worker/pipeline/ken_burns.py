"""
ken_burns.py
Render a still image as a documentary-style short clip:

  • Foreground = full image, **fit inside 16:9 with no crop** (subject never
    loses their head)
  • Background = same image, scaled-fill + heavy blur + slight darken, behind
    the foreground in the letterbox bars
  • Slow Ken Burns motion (zoom / pan) applied to the foreground

This is the YouTube documentary standard (Veritasium / Vsauce / Polymatter
all use it). Looks premium and is robust to any source aspect ratio.

We use FFmpeg's `filter_complex` — no frames pass through Python memory.
"""

import os
import subprocess
import logging

log = logging.getLogger(__name__)

OUT_W = 1920
OUT_H = 1080
FPS   = 30

# Foreground (un-cropped image) takes ~88% of canvas height. Leaves a soft
# blurred frame around the subject.
FG_MAX_W = 1700
FG_MAX_H = 950

# Background is downscaled FIRST (much smaller, fast), then upscaled — the
# bilinear interpolation gives a smooth fake-blur look. Far cheaper than
# `boxblur` at full 1080p (which OOMs on small containers).
BG_TINY_W = 480
BG_TINY_H = 270

# Motion variants — keyed by index for round-robin cycling so adjacent
# stills don't all zoom identically.
_VARIANTS = ("zoom_in", "zoom_out", "pan_right", "pan_left", "zoom_in_subtle")


def still_to_clip(image_path: str, out_path: str, duration_sec: float, variant_idx: int = 0) -> str:
    """
    Render a still image as a duration_sec MP4 clip with the fit-in-blurred-frame
    documentary look and a Ken Burns motion variant.
    """
    if duration_sec < 0.5:
        raise ValueError(f"duration_sec must be >= 0.5, got {duration_sec}")

    frames = max(int(round(duration_sec * FPS)), 1)
    variant = _VARIANTS[variant_idx % len(_VARIANTS)]
    fcx = _build_filter_complex(variant, frames)

    # filter_complex passed inline gets confused by commas inside expressions
    # like min(zoom+0.0006,1.12) (FFmpeg treats them as filter separators).
    # `-filter_complex_script <file>` reads from a file with no escaping
    # required — cleaner and parses any expression correctly.
    work_dir = os.path.dirname(out_path) or "."
    script_path = os.path.join(work_dir, f".{os.path.basename(out_path)}.filter.txt")
    with open(script_path, "w") as f:
        f.write(fcx)

    # zoompan's d= is frames-per-input-frame. With `-loop 1` we have
    # infinite input frames, so we cap output explicitly via `-frames:v`.
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", image_path,
        "-/filter_complex", script_path,
        "-map", "[out]",
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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    try:
        os.remove(script_path)
    except OSError:
        pass
    if result.returncode != 0:
        log.error(f"FFmpeg ken_burns failed:\n{result.stderr[-700:]}")
        raise RuntimeError(f"ken_burns FFmpeg error: {result.stderr[-300:]}")

    return out_path


def _build_filter_complex(variant: str, frames: int) -> str:
    """
    Build the FFmpeg filter_complex chain:

      [0:v] split into bg_in + fg_in
      [bg_in] scale-fill the canvas, crop to canvas, blur heavily, darken
      [fg_in] fit inside FG_MAX without crop, optional Ken Burns motion
      overlay foreground centered on background

    Returns a single string suitable for `-filter_complex`.
    """
    # Background: downscale to tiny, crop, then upscale back. The cheap
    # bilinear interpolation gives a smooth-blur look without the RAM cost
    # of `boxblur` at 1080p (which can OOM small containers).
    bg = (
        f"[bg_in]"
        f"scale={BG_TINY_W}:{BG_TINY_H}:force_original_aspect_ratio=increase,"
        f"crop={BG_TINY_W}:{BG_TINY_H},"
        f"scale={OUT_W}:{OUT_H}:flags=fast_bilinear,"
        f"eq=brightness=-0.18:saturation=0.85,"
        f"setsar=1"
        f"[bg]"
    )

    # Foreground: scale to working size with motion-friendly resolution,
    # apply zoompan, output at FG_MAX. Image is fit (no crop) inside FG_MAX.
    # zoompan's first scale ensures source is bigger than output frame so
    # `zoom` >1.0 has room to crop into.
    fg = _foreground_chain(variant, frames)

    # Overlay foreground centered on background
    overlay = f"[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto[out]"

    return f"[0:v]split=2[bg_in][fg_in];{bg};{fg};{overlay}"


def _foreground_chain(variant: str, frames: int) -> str:
    """Foreground filter chain ending in [fg]."""

    # Pre-scale: fit (no crop) within FG_MAX dimensions so the head/subject
    # is fully preserved. Pad to FG_MAX exactly so zoompan has a fixed input
    # rect to work with. The padded border (if any) is transparent and lands
    # over the blurred background after overlay.
    base = (
        f"[fg_in]"
        f"scale={FG_MAX_W}:{FG_MAX_H}:force_original_aspect_ratio=decrease,"
        f"pad={FG_MAX_W}:{FG_MAX_H}:(ow-iw)/2:(oh-ih)/2:color=black@0,"
        f"setsar=1"
    )

    # Output size for zoompan (= foreground display size)
    s = f"{FG_MAX_W}x{FG_MAX_H}"

    if variant == "zoom_in":
        zp = (
            f"zoompan=z='min(zoom+0.0006,1.12)'"
            f":d={frames}"
            f":x='iw/2-(iw/zoom/2)'"
            f":y='ih/2-(ih/zoom/2)'"
            f":s={s}:fps={FPS}"
        )
    elif variant == "zoom_out":
        zp = (
            f"zoompan=z='if(eq(on,0),1.12,max(zoom-0.0006,1.0))'"
            f":d={frames}"
            f":x='iw/2-(iw/zoom/2)'"
            f":y='ih/2-(ih/zoom/2)'"
            f":s={s}:fps={FPS}"
        )
    elif variant == "pan_right":
        zp = (
            f"zoompan=z='1.10'"
            f":d={frames}"
            f":x='(iw-iw/zoom)*on/{frames}'"
            f":y='ih/2-(ih/zoom/2)'"
            f":s={s}:fps={FPS}"
        )
    elif variant == "pan_left":
        zp = (
            f"zoompan=z='1.10'"
            f":d={frames}"
            f":x='(iw-iw/zoom)*(1-on/{frames})'"
            f":y='ih/2-(ih/zoom/2)'"
            f":s={s}:fps={FPS}"
        )
    else:  # zoom_in_subtle — very slow zoom centered, less motion
        zp = (
            f"zoompan=z='min(zoom+0.0003,1.06)'"
            f":d={frames}"
            f":x='iw/2-(iw/zoom/2)'"
            f":y='ih/2-(ih/zoom/2)'"
            f":s={s}:fps={FPS}"
        )

    return f"{base},{zp}[fg]"
