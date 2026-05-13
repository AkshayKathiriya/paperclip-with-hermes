"""
assemble.py
Concatenate per-shot clips, mix narration MP3, and burn subtitles.

`compose_shots` already produced each shot as a 1920x1080 30fps MP4
matching its planned duration, so this module just:
  1. Writes a concat manifest of all shot clips
  2. Runs one FFmpeg pass to concat + mix audio + burn subtitles

Why we cwd into work_dir before invoking FFmpeg:
  libass' `subtitles=` filter has fragile path escaping on absolute paths
  with colons. By staging `subs.srt` next to the concat manifest and
  invoking FFmpeg with cwd=work_dir, we use bare relative filenames and
  sidestep escape issues entirely.
"""

import os
import shutil
import subprocess
import logging

log = logging.getLogger(__name__)


def _has_libass() -> bool:
    r = subprocess.run(["ffmpeg", "-filters"], capture_output=True, text=True)
    return "subtitles" in r.stdout or "subtitles" in r.stderr


_LIBASS_AVAILABLE = _has_libass()


def assemble_video(
    shot_clips: list[str],
    audio_path: str,
    srt_path: str | None,
    output_path: str,
    fps: int = 30,
) -> str:
    """
    Concatenate shot_clips → mix audio → burn subtitles.

    Args:
        shot_clips:  Ordered list of MP4 paths (output of compose_shots)
        audio_path:  Narration audio (MP3 from ElevenLabs)
        srt_path:    SRT subtitle file (or None)
        output_path: Final .mp4 path
    """
    if not shot_clips:
        raise RuntimeError("assemble_video called with zero shot_clips")

    work_dir = os.path.dirname(output_path) or "."
    os.makedirs(work_dir, exist_ok=True)

    # Stage SRT next to the concat manifest so we can use a relative filename.
    staged_srt = None
    if srt_path and os.path.exists(srt_path) and _LIBASS_AVAILABLE:
        staged_srt = os.path.join(work_dir, "subs.srt")
        if os.path.abspath(srt_path) != os.path.abspath(staged_srt):
            shutil.copyfile(srt_path, staged_srt)

    # Stage audio similarly so we can reference by basename in cwd=work_dir.
    audio_basename = os.path.basename(audio_path)
    staged_audio = os.path.join(work_dir, audio_basename)
    if os.path.abspath(audio_path) != os.path.abspath(staged_audio):
        if not os.path.exists(staged_audio):
            shutil.copyfile(audio_path, staged_audio)

    # Write concat manifest with paths relative to work_dir
    concat_file = os.path.join(work_dir, "concat.txt")
    with open(concat_file, "w") as f:
        for p in shot_clips:
            rel = os.path.relpath(p, work_dir)
            esc = rel.replace("'", r"\'")
            f.write(f"file '{esc}'\n")

    log.info(f"Assembling {len(shot_clips)} shots → {output_path}")

    output_basename = os.path.basename(output_path)
    subtitle_filter = (
        "subtitles=subs.srt:force_style='"
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

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", "concat.txt",
        "-i", audio_basename,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
    ]

    if _LIBASS_AVAILABLE and staged_srt:
        cmd += ["-vf", subtitle_filter]
        log.info("  → concat + mix + subtitle burn")
    else:
        if not _LIBASS_AVAILABLE:
            log.warning("  → libass not available — subtitles will NOT be burned")
        elif not staged_srt:
            log.warning("  → no SRT provided — subtitles will NOT be burned")

    cmd.append(output_basename)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=work_dir)
    if result.returncode != 0:
        log.error(f"FFmpeg assembly failed:\n{result.stderr[-1500:]}")
        raise RuntimeError(
            f"FFmpeg assembly failed: {result.stderr.strip().splitlines()[-1] if result.stderr else 'unknown'}"
        )

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    log.info(f"✅ Final video: {output_path} ({size_mb:.1f} MB)")
    return output_path
