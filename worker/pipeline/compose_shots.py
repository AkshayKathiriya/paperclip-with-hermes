"""
compose_shots.py
Orchestrator: takes the Scene Director's `visual_plan` and produces an
ordered list of MP4 clip paths — one per shot — that the final assembler
concatenates with the narration.

Each shot's `source` field decides which fetcher to call:
  pexels     → fetch_pexels.search + download → trim/loop to duration
  wikimedia  → fetch_wikimedia.fetch → ken_burns motion → duration clip
  ai_image   → generate_ai_images.generate → ken_burns motion → duration clip

The visual_plan is a dict shaped like Scene Director's output:
{
  "total_duration_sec": 540,
  "scenes": [
    {
      "num": "01",
      "title": "...",
      "duration_sec": 60,
      "shots": [
        {"id": "01a", "duration_sec": 5, "source": "ai_image", "prompt": "..."},
        {"id": "01b", "duration_sec": 6, "source": "wikimedia", "url": "..."},
        {"id": "01c", "duration_sec": 4, "source": "pexels", "query": "..."}
      ]
    },
    ...
  ]
}

Returns a list of dicts:
  [{ "shot_id": ..., "clip_path": ..., "duration_sec": ..., "source": ... }, ...]
"""

import os
import subprocess
import logging
from typing import Any

from pipeline.fetch_pexels    import _find_clip, _download_video, PexelsError
from pipeline.fetch_wikimedia import fetch_wikimedia_image, WikimediaError
from pipeline.generate_ai_images import generate_image, AIImageError
from pipeline.ken_burns       import still_to_clip

log = logging.getLogger(__name__)

OUT_W = 1920
OUT_H = 1080
FPS   = 30


class ComposeError(RuntimeError):
    """Raised when a shot can't be produced even after fallback attempts."""


def compose_shots(
    visual_plan: dict,
    work_dir: str,
    ai_image_quality: str = "medium",
    target_duration_sec: float | None = None,
    srt_path: str | None = None,
) -> list[dict]:
    """
    Realize every shot in visual_plan as an MP4 clip on disk.
    Returns ordered metadata list (one entry per shot, in plan order).

    ai_image_quality: low | medium | high  (passed through to gpt-image-1)

    target_duration_sec: if provided, ALL shot durations are scaled
        proportionally so their total equals this value. This is how we
        keep visuals in sync with the (already-generated) narration —
        otherwise a 30-shot plan summing to 150s plays under a 600s
        narration, leaving 7+ minutes of silent end-frame.
    """
    # Priority 1: per-shot narration alignment if we have an SRT. This
    # makes visuals land on the exact moment their excerpt is being spoken.
    aligned = False
    if srt_path:
        from pipeline.narration_sync import align_shots_to_narration
        try:
            align_shots_to_narration(visual_plan, srt_path)
            aligned = True
        except Exception as e:
            log.warning(f"narration sync failed, falling back to proportional: {e}")

    # Priority 2 (fallback): proportional scaling to target duration.
    # Useful when SRT is missing or all excerpts fail to match.
    if not aligned and target_duration_sec:
        _normalize_durations(visual_plan, target_duration_sec)
    clips_dir = os.path.join(work_dir, "clips")
    images_dir = os.path.join(work_dir, "images")
    os.makedirs(clips_dir, exist_ok=True)
    os.makedirs(images_dir, exist_ok=True)

    scenes = visual_plan.get("scenes") or []
    if not scenes:
        raise ComposeError("visual_plan has no scenes")

    results: list[dict] = []
    used_pexels_ids: set[int] = set()
    still_idx = 0  # for Ken Burns variant rotation

    for scene in scenes:
        scene_num = scene.get("num", "?")
        shots = scene.get("shots") or []
        if not shots:
            log.warning(f"[scene {scene_num}] has no shots — skipping")
            continue

        for shot in shots:
            shot_id  = shot.get("id") or f"{scene_num}-{len(results)}"
            duration = float(shot.get("duration_sec") or 5)
            source   = (shot.get("source") or "").strip().lower()

            clip_path = os.path.join(clips_dir, f"shot_{shot_id}.mp4")

            try:
                if source == "pexels":
                    _render_pexels_shot(shot, clip_path, duration, used_pexels_ids)
                elif source == "wikimedia":
                    _render_wikimedia_shot(shot, clip_path, duration, images_dir, still_idx)
                    still_idx += 1
                elif source == "ai_image":
                    _render_ai_shot(shot, clip_path, duration, images_dir, still_idx, ai_image_quality)
                    still_idx += 1
                else:
                    raise ComposeError(f"unknown source '{source}' on shot {shot_id}")

                results.append({
                    "shot_id": shot_id,
                    "clip_path": clip_path,
                    "duration_sec": duration,
                    "source": source,
                })

            except (PexelsError, WikimediaError, AIImageError, ComposeError) as e:
                log.error(f"[shot {shot_id}] {source} failed: {e}")
                # Attempt graceful fallback: AI image → Pexels generic, Wikimedia → AI image, Pexels → Wikimedia
                fallback = _fallback_render(shot, clip_path, duration, images_dir, still_idx, used_pexels_ids)
                if fallback:
                    still_idx += 1
                    results.append(fallback)
                else:
                    log.error(f"[shot {shot_id}] ALL sources failed — using black placeholder")
                    _render_black_placeholder(clip_path, duration)
                    results.append({
                        "shot_id": shot_id,
                        "clip_path": clip_path,
                        "duration_sec": duration,
                        "source": "placeholder",
                    })

    if not results:
        raise ComposeError("compose_shots produced zero clips")

    # Sanity: refuse to ship a job where every shot is a placeholder.
    real = [r for r in results if r["source"] != "placeholder"]
    if not real:
        raise ComposeError("All shots fell back to placeholders — refusing to assemble")

    log.info(
        f"compose_shots: {len(results)} clips "
        f"({sum(1 for r in results if r['source']=='pexels')} pexels, "
        f"{sum(1 for r in results if r['source']=='wikimedia')} wikimedia, "
        f"{sum(1 for r in results if r['source']=='ai_image')} ai_image, "
        f"{sum(1 for r in results if r['source']=='placeholder')} placeholder)"
    )
    return results


# ── source renderers ──────────────────────────────────────────────────────────

def _normalize_durations(visual_plan: dict, target_total_sec: float) -> None:
    """
    Scale every shot's `duration_sec` so the sum equals target_total_sec.

    Strategy: simple proportional scale of all shot durations. If the plan
    summed to 150s and narration is 600s, every shot's duration is multiplied
    by 4.0 (so a 5s shot becomes 20s). Each shot is also clamped to a
    [MIN_SHOT_SEC, MAX_SHOT_SEC] band so we don't get either 0.3s flashes or
    60s glacial holds.
    """
    MIN_SHOT_SEC = 3.0
    MAX_SHOT_SEC = 18.0

    scenes = visual_plan.get("scenes") or []
    shots: list[dict] = [sh for sc in scenes for sh in (sc.get("shots") or [])]
    if not shots:
        return

    total = sum(float(sh.get("duration_sec") or 5.0) for sh in shots)
    if total <= 0:
        # Plan lacks any durations — split target_total evenly.
        per = max(MIN_SHOT_SEC, min(MAX_SHOT_SEC, target_total_sec / len(shots)))
        for sh in shots:
            sh["duration_sec"] = per
        log.info(
            f"_normalize_durations: plan had no durations; "
            f"split {target_total_sec:.1f}s evenly → {per:.1f}s per shot"
        )
        return

    scale = target_total_sec / total
    log.info(
        f"_normalize_durations: target={target_total_sec:.1f}s plan_total={total:.1f}s "
        f"scale={scale:.2f}x → applied to {len(shots)} shots"
    )

    # First pass: scale.
    for sh in shots:
        d = float(sh.get("duration_sec") or 5.0) * scale
        sh["duration_sec"] = d

    # Clamp each shot, then redistribute the leftover/deficit so total stays right.
    clamped = [max(MIN_SHOT_SEC, min(MAX_SHOT_SEC, sh["duration_sec"])) for sh in shots]
    drift = target_total_sec - sum(clamped)
    if abs(drift) > 0.5:
        # Spread drift across shots that aren't already at the clamp boundary.
        adjustable = [
            i for i, d in enumerate(clamped)
            if MIN_SHOT_SEC < d < MAX_SHOT_SEC
        ] or list(range(len(clamped)))
        per_shot_adj = drift / len(adjustable)
        for i in adjustable:
            clamped[i] = max(MIN_SHOT_SEC, min(MAX_SHOT_SEC, clamped[i] + per_shot_adj))

    for sh, d in zip(shots, clamped):
        sh["duration_sec"] = round(d, 3)

    final_total = sum(sh["duration_sec"] for sh in shots)
    log.info(f"_normalize_durations: final_total={final_total:.1f}s (target {target_total_sec:.1f}s)")


def _render_pexels_shot(shot: dict, clip_path: str, duration: float, used_ids: set[int]) -> None:
    query = (shot.get("query") or shot.get("pexels_query") or "").strip()
    if not query:
        raise ComposeError("pexels shot has no query")
    chosen = _find_clip(query, int(duration), used_ids)
    if not chosen:
        raise ComposeError(f"pexels: no usable result for '{query}'")
    video_id, video_url = chosen
    used_ids.add(video_id)
    raw_path = clip_path + ".raw.mp4"
    _download_video(video_url, raw_path)
    _trim_clip(raw_path, clip_path, duration)
    try:
        os.remove(raw_path)
    except OSError:
        pass


def _render_wikimedia_shot(shot: dict, clip_path: str, duration: float, images_dir: str, idx: int) -> None:
    url = (shot.get("url") or "").strip()
    if not url:
        raise ComposeError("wikimedia shot has no url")
    image_path = os.path.join(images_dir, f"{os.path.basename(clip_path).replace('.mp4', '')}.img")
    fetch_wikimedia_image(url, image_path)
    still_to_clip(image_path, clip_path, duration, variant_idx=idx)


def _render_ai_shot(shot: dict, clip_path: str, duration: float, images_dir: str, idx: int, quality: str = "medium") -> None:
    prompt = (shot.get("prompt") or "").strip()
    if not prompt:
        raise ComposeError("ai_image shot has no prompt")
    image_path = os.path.join(images_dir, f"{os.path.basename(clip_path).replace('.mp4', '')}.png")
    scene_context = (shot.get("narration_excerpt") or shot.get("title") or "").strip() or None
    generate_image(prompt, image_path, quality=quality, scene_context=scene_context)
    still_to_clip(image_path, clip_path, duration, variant_idx=idx)


def _fallback_render(
    shot: dict, clip_path: str, duration: float,
    images_dir: str, idx: int, used_pexels_ids: set[int],
) -> dict | None:
    """
    Try to salvage a failed shot:
      ai_image  → try Pexels with shot.fallback_query or first 3 words of prompt
      wikimedia → try AI image of shot.title or url-derived prompt
      pexels    → try AI image of the query
    Returns metadata dict if a fallback succeeded, else None.
    """
    original_source = (shot.get("source") or "").lower()
    try:
        if original_source == "ai_image":
            q = shot.get("fallback_query") or " ".join((shot.get("prompt") or "").split()[:3])
            if q:
                _render_pexels_shot({"query": q}, clip_path, duration, used_pexels_ids)
                log.info(f"  → fallback: ai_image → pexels '{q}'")
                return {"shot_id": shot.get("id"), "clip_path": clip_path, "duration_sec": duration, "source": "pexels"}
        elif original_source == "wikimedia":
            # Generate an AI image based on the shot's narration excerpt
            seed = shot.get("title") or shot.get("narration_excerpt") or "documentary archival photograph"
            prompt = f"Documentary-style archival photograph. {seed[:200]}. Cinematic lighting, photojournalistic style."
            _render_ai_shot({"prompt": prompt}, clip_path, duration, images_dir, idx)
            log.info("  → fallback: wikimedia → ai_image")
            return {"shot_id": shot.get("id"), "clip_path": clip_path, "duration_sec": duration, "source": "ai_image"}
        elif original_source == "pexels":
            # Generate an AI image from the query
            q = shot.get("query") or shot.get("pexels_query") or "cinematic establishing shot"
            prompt = f"Documentary-style B-roll footage frame. {q}. Cinematic, photorealistic, no text."
            _render_ai_shot({"prompt": prompt}, clip_path, duration, images_dir, idx)
            log.info("  → fallback: pexels → ai_image")
            return {"shot_id": shot.get("id"), "clip_path": clip_path, "duration_sec": duration, "source": "ai_image"}
    except Exception as e:
        log.warning(f"  → fallback also failed: {e}")
    return None


# ── helpers ───────────────────────────────────────────────────────────────────

def _trim_clip(in_path: str, out_path: str, duration: float) -> None:
    """Trim/loop a video clip to exactly `duration` seconds at 1920x1080@30fps, no audio."""
    vf = (
        f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},"
        f"fps={FPS}"
    )
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", in_path,
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-an",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        log.error(f"trim failed: {result.stderr[-500:]}")
        raise RuntimeError(f"trim failed: {result.stderr[-200:]}")


def _render_black_placeholder(out_path: str, duration: float) -> None:
    """Last-resort black clip so the timeline doesn't desync from audio."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=0x000000:size={OUT_W}x{OUT_H}:rate={FPS}:duration={duration:.3f}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "30",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    subprocess.run(cmd, capture_output=True, text=True, timeout=60)
