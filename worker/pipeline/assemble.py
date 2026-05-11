"""
assemble.py
Assembles the final YouTube video using pure FFmpeg subprocess calls.

Why NOT MoviePy:
  MoviePy decodes video frames into Python numpy arrays before re-encoding.
  For 6× 1080p clips with a Ken Burns filter that's ~8 100 frames × 6 MB each
  flowing through Python memory — enough to OOM-kill a Railway container.
  FFmpeg keeps all frame data inside its own C process; Python only launches it.

Pipeline (all via FFmpeg):
  1. ffprobe  — get narration audio duration
  2. ffmpeg   — per clip: loop/trim to target duration, scale+crop to 1920×1080
  3. ffmpeg   — concat all clips + mix narration audio + burn subtitles in one pass
"""

import os
import json
import shutil
import subprocess
import logging

log = logging.getLogger(__name__)

W   = 1920
H   = 1080
FPS = 30


def _has_libass() -> bool:
    """Return True if the local ffmpeg was compiled with libass (subtitles filter)."""
    r = subprocess.run(
        ["ffmpeg", "-filters"],
        capture_output=True, text=True
    )
    return "subtitles" in r.stdout or "subtitles" in r.stderr


_LIBASS_AVAILABLE = _has_libass()


# ── public entry point ────────────────────────────────────────────────────────

def assemble_video(
    video_clips: list,
    audio_path: str,
    srt_path: str,
    output_path: str,
    resolution: str = "1920x1080",
    fps: int = 30,
    subtitle_style: str = "bottom-center",
) -> str:
    """
    Assembles the final video entirely via FFmpeg — no frames in Python RAM.

    Args:
        video_clips:  List of local .mp4 paths (None entries → dark placeholder)
        audio_path:   Path to narration WAV
        srt_path:     Path to SRT subtitle file
        output_path:  Destination .mp4
    Returns:
        output_path on success
    """
    w, h = [int(x) for x in resolution.split("x")]
    work_dir = os.path.dirname(output_path)

    log.info(f"Assembling {len(video_clips)} clips → {output_path}")

    # Step 1: get narration duration via ffprobe
    total_duration = _probe_duration(audio_path)
    log.info(f"Narration duration: {total_duration:.1f}s")

    n_clips = len(video_clips)
    clip_duration = total_duration / n_clips

    # Step 2: prepare each clip (loop/trim/scale to w×h, no audio)
    prepared = []
    for i, clip_path in enumerate(video_clips):
        scene_num = str(i + 1).zfill(2)
        out_path  = os.path.join(work_dir, f"prep_{scene_num}.mp4")
        log.info(f"Preparing scene {scene_num}/{n_clips}: {clip_path or 'placeholder'}")
        _prepare_clip(clip_path, out_path, clip_duration, w, h, fps)
        prepared.append(out_path)

    # Step 3: write FFmpeg concat manifest
    concat_file = os.path.join(work_dir, "concat.txt")
    with open(concat_file, "w") as f:
        for p in prepared:
            # FFmpeg concat demuxer requires absolute or escaped paths
            escaped = p.replace("'", r"\'")
            f.write(f"file '{escaped}'\n")

    # Step 4: concat + audio mix + subtitle burn in one FFmpeg pass
    _concat_mix_burn(concat_file, audio_path, srt_path, output_path, fps)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    log.info(f"✅ Final video: {output_path} ({size_mb:.1f} MB)")
    return output_path


# ── helpers ────────────────────────────────────────────────────────────────────

def _probe_duration(path: str) -> float:
    """Use ffprobe to get media duration in seconds."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[:300]}")
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def _prepare_clip(
    clip_path: str | None,
    out_path: str,
    duration: float,
    w: int,
    h: int,
    fps: int,
) -> None:
    """
    Produce a silent clip at exactly w×h×fps for `duration` seconds.
    Uses `-stream_loop -1` to loop short clips; trims with `-t`.
    Falls back to a dark solid-color clip if clip_path is missing.
    """
    if clip_path and os.path.exists(clip_path):
        # scale-and-crop filter: fill frame without black bars
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},"
            f"fps={fps}"
        )
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",       # loop input indefinitely
            "-i", clip_path,
            "-t", str(duration),        # cut to exact duration
            "-vf", vf,
            "-an",                      # drop source audio
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            out_path,
        ]
    else:
        # dark gray placeholder — zero disk reads
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c=0x1e1e1e:size={w}x{h}:rate={fps}:duration={duration}",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            out_path,
        ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        log.error(f"Clip prep failed:\n{result.stderr[-500:]}")
        raise RuntimeError(f"FFmpeg clip prep failed: {result.stderr[-200:]}")


def _concat_mix_burn(
    concat_file: str,
    audio_path: str,
    srt_path: str,
    output_path: str,
    fps: int,
) -> None:
    """
    Single FFmpeg pass: concatenate clips → mix narration audio → burn subtitles.
    """
    # FFmpeg subtitles filter — escape backslashes, single-quotes, and colons in path.
    # Use explicit `filename=` key (required in FFmpeg 6+).
    srt_escaped = srt_path.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    subtitle_filter = (
        f"subtitles=filename='{srt_escaped}':force_style='"
        "FontName=Arial,"
        "FontSize=22,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BackColour=&H80000000,"
        "Outline=2,"
        "Shadow=1,"
        "Alignment=2,"
        "MarginV=40"
        "'"
    )

    # base command — concat + audio mix, no subtitle filter yet
    cmd_base = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_file,
        "-i", audio_path,
        "-map", "0:v:0",     # video from concatenated clips
        "-map", "1:a:0",     # audio from narration WAV
        "-shortest",         # end when shorter stream ends
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
    ]

    if _LIBASS_AVAILABLE:
        cmd = cmd_base + ["-vf", subtitle_filter, output_path]
        log.info("Running FFmpeg concat + audio mix + subtitle burn...")
    else:
        cmd = cmd_base + [output_path]
        log.warning("libass not found in this ffmpeg — skipping subtitles "
                    "(they ARE burned on Railway where apt-ffmpeg includes libass)")
        log.info("Running FFmpeg concat + audio mix (no subtitles)...")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        log.error(f"FFmpeg final pass failed:\n{result.stderr[-1000:]}")
        if _LIBASS_AVAILABLE:
            # subtitle filter caused the error — retry without it
            log.warning("Subtitle burn failed — retrying without subtitles")
            result = subprocess.run(
                cmd_base + [output_path],
                capture_output=True, text=True, timeout=600
            )
            if result.returncode != 0:
                raise RuntimeError(f"FFmpeg fallback also failed: {result.stderr[-200:]}")
        else:
            raise RuntimeError(f"FFmpeg assembly failed: {result.stderr[-200:]}")

    log.info("FFmpeg assembly complete")
