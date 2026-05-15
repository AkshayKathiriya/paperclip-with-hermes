"""
narration_sync.py
Aligns each shot in the visual plan to the actual moment its
`narration_excerpt` is being spoken in the narration audio.

WHY THIS EXISTS
---------------
Without this module the pipeline does:

   narration: ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ (10:44)
   shots:     |sh1|sh2|sh3|...|sh30|                              (every shot
                                                                   ~20s)

Each shot is shown for `narration_duration / shot_count` seconds — visuals
have NO relationship to what's being said at that moment. The shot of Uday
Kotak might land while the narrator is still talking about August 27.

WITH this module:

   narration:  "On Aug 27, IL&FS defaulted... ICRA scrambling... Uday Kotak appointed."
   shots:      [trade floor]    [icra logo]      [kotak photo]
               ^aligned to      ^aligned to      ^aligned to
                that sentence    that phrase      that name

We use Whisper's SRT (already generated for subtitles) as the source of
truth for word timings.

HOW
---
1. Parse the SRT into (start_sec, end_sec, text) cues.
2. Concatenate cue text into a flat searchable transcript, keeping a
   character-position → time mapping.
3. For each shot's `narration_excerpt`, fuzzy-find its first 6 words in
   the transcript. The character offset gives the shot's start_time.
4. End_time = the next shot's start_time (or transcript end for the last).
5. Shots whose excerpt can't be located keep their existing duration (the
   proportional scaler will handle them).
"""

import os
import re
import logging
from dataclasses import dataclass
from difflib import SequenceMatcher

log = logging.getLogger(__name__)

# How many leading words of an excerpt we use as the search needle.
NEEDLE_WORDS = 6

# Minimum fuzzy similarity to accept a match (0..1).
MIN_SIMILARITY = 0.72

# Shots shorter than this after alignment are stretched up to this minimum.
MIN_SHOT_SEC = 2.5


@dataclass
class Cue:
    start: float
    end: float
    text: str


def align_shots_to_narration(visual_plan: dict, srt_path: str) -> dict:
    """
    Mutates visual_plan in place: every shot whose `narration_excerpt`
    matches the SRT gets its `duration_sec` replaced with the actual
    narration window. Shots that can't be matched keep their current
    duration (typically set by `_normalize_durations`).

    Returns the same visual_plan dict for convenience.
    """
    if not srt_path or not os.path.exists(srt_path):
        log.warning("narration_sync: no SRT file at %s, skipping alignment", srt_path)
        return visual_plan

    cues = _parse_srt(srt_path)
    if not cues:
        log.warning("narration_sync: SRT had no cues, skipping")
        return visual_plan

    transcript, positions = _build_transcript(cues)
    total_duration = cues[-1].end

    shots = [sh for sc in visual_plan.get("scenes") or []
                for sh in (sc.get("shots") or [])]
    if not shots:
        return visual_plan

    matched_count = 0
    # Pass 1: locate each shot's start_time using its narration_excerpt.
    for shot in shots:
        excerpt = _normalize(shot.get("narration_excerpt") or "")
        if not excerpt:
            shot["_sync_start"] = None
            continue

        words = excerpt.split()
        if len(words) < 3:
            shot["_sync_start"] = None
            continue

        needle = " ".join(words[:NEEDLE_WORDS])
        char_pos = _fuzzy_find(transcript, needle)
        if char_pos is None:
            shot["_sync_start"] = None
            continue

        shot["_sync_start"] = _char_to_time(char_pos, positions)
        matched_count += 1

    # Pass 2: enforce monotonic ordering. A later shot's start must come
    # after an earlier shot's start — drop matches that violate this.
    last_good = -1.0
    for shot in shots:
        t = shot.get("_sync_start")
        if t is None:
            continue
        if t <= last_good:
            shot["_sync_start"] = None
            matched_count -= 1
        else:
            last_good = t

    # Pass 3: derive duration_sec. Each matched shot lasts until the next
    # matched shot begins (or to the narration end for the last).
    matched_idx = [i for i, sh in enumerate(shots) if sh.get("_sync_start") is not None]
    if matched_idx:
        for k, i in enumerate(matched_idx):
            start = shots[i]["_sync_start"]
            if k + 1 < len(matched_idx):
                end = shots[matched_idx[k + 1]]["_sync_start"]
            else:
                end = total_duration
            duration = max(MIN_SHOT_SEC, end - start)
            shots[i]["duration_sec"] = round(duration, 3)

    # Pass 4: handle unmatched shots — distribute remaining time between
    # matched shots evenly across each gap.
    _fill_unmatched_durations(shots, total_duration)

    # Clean up internal field
    for shot in shots:
        shot.pop("_sync_start", None)

    log.info(
        "narration_sync: matched %d/%d shots to narration timings "
        "(narration=%.1fs, sum of shot durations=%.1fs)",
        matched_count, len(shots), total_duration,
        sum(sh.get("duration_sec", 0) for sh in shots),
    )
    return visual_plan


# ── SRT parsing ───────────────────────────────────────────────────────────────

_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _parse_srt(path: str) -> list[Cue]:
    cues: list[Cue] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        blocks = re.split(r"\n\s*\n", f.read().strip())
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        # Find timing line
        timing = next((ln for ln in lines if "-->" in ln), None)
        if not timing:
            continue
        try:
            left, right = timing.split("-->")
            start = _srt_to_sec(left.strip())
            end = _srt_to_sec(right.strip())
        except Exception:
            continue
        text_lines = [ln for ln in lines
                      if "-->" not in ln and not ln.isdigit()]
        text = " ".join(text_lines).strip()
        if text:
            cues.append(Cue(start, end, text))
    return cues


def _srt_to_sec(t: str) -> float:
    m = _SRT_TIME.match(t)
    if not m:
        raise ValueError(f"bad SRT time: {t}")
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000.0


# ── transcript building ───────────────────────────────────────────────────────

def _build_transcript(cues: list[Cue]) -> tuple[str, list[tuple[int, float]]]:
    """
    Return (transcript_text, [(start_char_idx, time_sec_of_cue_start), ...]).
    Text is lowercased + lightly normalized.
    """
    transcript_parts: list[str] = []
    positions: list[tuple[int, float]] = []
    pos = 0
    for cue in cues:
        normalized = _normalize(cue.text) + " "
        positions.append((pos, cue.start))
        transcript_parts.append(normalized)
        pos += len(normalized)
    return "".join(transcript_parts), positions


def _char_to_time(char_pos: int, positions: list[tuple[int, float]]) -> float:
    """Given a character offset in the flat transcript, return the time
    of the cue that contains it."""
    last_time = 0.0
    for start_pos, start_time in positions:
        if start_pos > char_pos:
            return last_time
        last_time = start_time
    return last_time


def _normalize(s: str) -> str:
    s = s.lower()
    # Drop punctuation that Whisper drifts on, keep word characters + spaces
    s = re.sub(r"[^\w\s'’]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ── fuzzy substring search ────────────────────────────────────────────────────

def _fuzzy_find(haystack: str, needle: str) -> int | None:
    """
    Locate `needle` (already-normalized) inside `haystack` (already-normalized).
    Returns the character index, or None if no good match.
    Tries exact first, then fuzzy over sliding windows.
    """
    if not needle or not haystack:
        return None

    exact = haystack.find(needle)
    if exact != -1:
        return exact

    # Fuzzy: slide a window of `len(needle)` characters across the haystack.
    # SequenceMatcher.ratio is O(N×M) per window, so we sample every 8 chars
    # to keep this cheap. Good enough for our typical 600s/1000-cue input.
    needle_len = len(needle)
    best_ratio = 0.0
    best_pos: int | None = None
    step = max(8, needle_len // 10)
    for i in range(0, max(1, len(haystack) - needle_len), step):
        window = haystack[i:i + needle_len]
        ratio = SequenceMatcher(None, needle, window).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_pos = i
            if ratio > 0.95:
                break

    if best_ratio >= MIN_SIMILARITY:
        return best_pos
    return None


# ── unmatched-shot filler ─────────────────────────────────────────────────────

def _fill_unmatched_durations(shots: list[dict], total_duration: float) -> None:
    """
    Walk through shots; runs of unmatched shots between matched anchors get
    their durations evenly redistributed across the gap. Unmatched shots
    before the first match share the lead-in time; unmatched after the
    last match share the trailing time.
    """
    # Build run boundaries.
    anchor_indices = [i for i, sh in enumerate(shots)
                      if sh.get("_sync_start") is not None]

    if not anchor_indices:
        # Nothing matched — leave whatever durations the proportional scaler set.
        return

    # Lead-in
    first_anchor = anchor_indices[0]
    lead_time = shots[first_anchor]["_sync_start"]
    if first_anchor > 0:
        per = max(MIN_SHOT_SEC, lead_time / first_anchor)
        for i in range(first_anchor):
            shots[i]["duration_sec"] = round(per, 3)

    # Gaps between anchors
    for a, b in zip(anchor_indices, anchor_indices[1:]):
        gap_start = shots[a]["_sync_start"]
        gap_end = shots[b]["_sync_start"]
        gap = gap_end - gap_start
        n_unmatched_between = b - a - 1
        if n_unmatched_between == 0:
            continue
        # Re-split the anchor's duration: anchor keeps some, unmatched share rest
        anchor_keep = max(MIN_SHOT_SEC, gap / (n_unmatched_between + 1))
        shots[a]["duration_sec"] = round(anchor_keep, 3)
        remaining = max(0.0, gap - anchor_keep)
        per = max(MIN_SHOT_SEC, remaining / n_unmatched_between)
        for i in range(a + 1, b):
            shots[i]["duration_sec"] = round(per, 3)

    # Trailing
    last_anchor = anchor_indices[-1]
    trail_time = total_duration - shots[last_anchor]["_sync_start"]
    n_trail = len(shots) - last_anchor - 1
    if n_trail > 0 and trail_time > 0:
        anchor_keep = max(MIN_SHOT_SEC, trail_time / (n_trail + 1))
        shots[last_anchor]["duration_sec"] = round(anchor_keep, 3)
        per = max(MIN_SHOT_SEC, (trail_time - anchor_keep) / n_trail)
        for i in range(last_anchor + 1, len(shots)):
            shots[i]["duration_sec"] = round(per, 3)
