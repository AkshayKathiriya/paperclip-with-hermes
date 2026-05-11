"""
subtitles.py
Uses faster-whisper to transcribe the generated narration WAV and produce
a well-timed SRT subtitle file.

faster-whisper uses CTranslate2 (not torch+numba), so it has no numpy
version conflicts and runs 4x faster on CPU than openai-whisper.
"""

import os
import logging
from faster_whisper import WhisperModel

log = logging.getLogger(__name__)

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")

_model = None   # module-level cache so model loads only once per container


def generate_subtitles(audio_path: str, work_dir: str) -> str:
    """
    Transcribes audio and writes an SRT subtitle file.

    Args:
        audio_path: Path to the narration WAV file
        work_dir:   Directory to save the SRT file

    Returns:
        Path to the generated .srt file
    """
    global _model

    srt_path = os.path.join(work_dir, "subtitles.srt")

    if _model is None:
        log.info(f"Loading faster-whisper '{WHISPER_MODEL}' model...")
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        log.info("Whisper model loaded")

    log.info(f"Transcribing: {audio_path}")
    segments_iter, _ = _model.transcribe(
        audio_path,
        language="en",
        word_timestamps=True,
        vad_filter=True,    # skip silent gaps — cleaner subtitles
    )
    segments = list(segments_iter)   # exhaust the generator

    srt_content = _build_srt(segments)

    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(srt_content)

    log.info(f"Subtitles written: {srt_path} ({len(segments)} segments)")
    return srt_path


def _build_srt(segments: list) -> str:
    """
    Converts faster-whisper segments into SRT format.
    Keeps subtitle lines under 60 chars for readability.
    """
    lines = []
    idx = 1

    for seg in segments:
        start = _format_time(seg.start)
        end   = _format_time(seg.end)
        text  = seg.text.strip()

        if not text:
            continue

        if len(text) > 60:
            words = text.split()
            mid   = len(words) // 2
            text  = " ".join(words[:mid]) + "\n" + " ".join(words[mid:])

        lines.append(f"{idx}\n{start} --> {end}\n{text}\n")
        idx += 1

    return "\n".join(lines)


def _format_time(seconds: float) -> str:
    """Converts float seconds to SRT timestamp format HH:MM:SS,mmm"""
    hours   = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs    = int(seconds % 60)
    millis  = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
