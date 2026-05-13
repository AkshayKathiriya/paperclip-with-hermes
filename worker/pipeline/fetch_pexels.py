"""
fetch_pexels.py
Fetches one stock video clip per scene from the Pexels API.
Saves each clip to work_dir/clips/scene_XX.mp4
Returns list of local file paths in scene order.

Design rules (post-incident):
- NO silent fallbacks. If a search returns 0 results, log the full API
  response shape and either try a deliberate alternate query (logged) or
  raise. Never let 6 scenes silently collapse onto one generic clip.
- Track already-used video IDs so two scenes can't reuse the same clip.
- Validate PEXELS_API_KEY at call time and fail loud if missing.
"""

import os
import requests
import logging

log = logging.getLogger(__name__)

PEXELS_VIDEO_URL = "https://api.pexels.com/videos/search"

MIN_DURATION = 5     # seconds — clips shorter than this are skipped
ORIENTATION  = "landscape"
PER_PAGE     = 15    # fetch a wide pool so we can dedupe across scenes


class PexelsError(RuntimeError):
    """Raised for any Pexels-related failure that should fail a job loudly."""


def _api_key() -> str:
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key:
        raise PexelsError("PEXELS_API_KEY is not set in the worker environment")
    return key


def fetch_pexels_videos(scenes: list[dict], work_dir: str) -> list[str]:
    """
    For each scene, search Pexels using scene['pexels_search_query'],
    download the best matching clip, and return list of local paths.

    Raises PexelsError on critical failures (no key, all searches dead, etc).
    Will skip individual broken scenes (returning None for that slot) but
    will not paper over a systemic failure.
    """
    clips_dir = os.path.join(work_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    used_video_ids: set[int] = set()
    clip_paths: list[str | None] = []
    failures: list[dict] = []

    for i, scene in enumerate(scenes):
        scene_num = scene.get("num", str(i + 1).zfill(2))
        query     = scene.get("pexels_search_query", "").strip()
        duration  = scene.get("duration_seconds", 45)
        out_path  = os.path.join(clips_dir, f"scene_{scene_num}.mp4")

        if not query:
            log.error(f"[scene {scene_num}] empty pexels_search_query — skipping")
            failures.append({"scene": scene_num, "reason": "empty query"})
            clip_paths.append(None)
            continue

        log.info(f"[scene {scene_num}] searching Pexels: '{query}' (target {duration}s)")

        chosen = _find_clip(query, duration, used_video_ids)

        # Alternate queries — deliberate, logged, not silent.
        # If the primary returns zero, try ONE narrower fallback derived from
        # the longest meaningful word (avoid stopwords).
        if not chosen:
            alt = _alternate_query(query)
            if alt:
                log.warning(f"[scene {scene_num}] '{query}' returned nothing; trying alt: '{alt}'")
                chosen = _find_clip(alt, duration, used_video_ids)

        if not chosen:
            log.error(f"[scene {scene_num}] no usable Pexels result for '{query}'")
            failures.append({"scene": scene_num, "query": query, "reason": "no results after alt"})
            clip_paths.append(None)
            continue

        video_id, video_url = chosen
        used_video_ids.add(video_id)
        log.info(f"[scene {scene_num}] picked video_id={video_id} ({video_url[:70]})")

        try:
            _download_video(video_url, out_path)
            clip_paths.append(out_path)
        except Exception as e:
            log.exception(f"[scene {scene_num}] download failed: {e}")
            failures.append({"scene": scene_num, "video_id": video_id, "reason": f"download: {e}"})
            clip_paths.append(None)

    # Sanity check — refuse to ship a job where every scene fell through.
    successful = sum(1 for p in clip_paths if p)
    if successful == 0:
        raise PexelsError(
            f"All {len(scenes)} scenes failed to fetch from Pexels. "
            f"Failures: {failures}"
        )
    if len(used_video_ids) == 1 and successful > 1:
        # Should never happen given the dedupe set, but guard anyway.
        raise PexelsError(
            "All successful scenes returned the same video_id — refusing to produce a single-clip loop"
        )

    log.info(
        f"Pexels fetch summary: {successful}/{len(scenes)} scenes OK, "
        f"unique video_ids={len(used_video_ids)}, failures={len(failures)}"
    )
    return clip_paths


# ── search + selection ───────────────────────────────────────────────────────

def _find_clip(
    query: str,
    target_duration: int,
    used_video_ids: set[int],
) -> tuple[int, str] | None:
    """
    Search Pexels and return (video_id, download_url) for the best clip that
    isn't already used. Returns None if no usable result exists.
    """
    data = _pexels_search(query)
    videos = data.get("videos", [])
    log.info(f"  → API returned {len(videos)} videos for '{query}'")

    if not videos:
        return None

    # Skip already-used videos to prevent duplicate scenes.
    candidates = [v for v in videos if v.get("id") not in used_video_ids]
    if not candidates:
        log.warning(f"  → all {len(videos)} results for '{query}' already used in prior scenes")
        return None

    # Skip clips too short to be useful.
    candidates = [v for v in candidates if v.get("duration", 0) >= MIN_DURATION]
    if not candidates:
        log.warning(f"  → all results too short (< {MIN_DURATION}s)")
        return None

    # Pick video closest to target duration.
    best = min(candidates, key=lambda v: abs(v.get("duration", 999) - target_duration))

    # Pick the best file variant (prefer HD ≥ 1280px).
    files = best.get("video_files", [])
    hd_files = [f for f in files if f.get("quality") in ("hd", "sd") and f.get("width", 0) >= 1280]
    pool = hd_files or files
    if not pool:
        return None

    pool.sort(key=lambda f: f.get("width", 0), reverse=True)
    link = pool[0].get("link")
    if not link:
        return None

    return (best["id"], link)


def _pexels_search(query: str) -> dict:
    """Raw Pexels search. Raises PexelsError on auth/network errors."""
    headers = {"Authorization": _api_key()}
    try:
        response = requests.get(
            PEXELS_VIDEO_URL,
            headers=headers,
            params={
                "query": query,
                "orientation": ORIENTATION,
                "per_page": PER_PAGE,
                "size": "medium",
            },
            timeout=15,
        )
    except requests.RequestException as e:
        raise PexelsError(f"Pexels network error for '{query}': {e}") from e

    if response.status_code == 401:
        raise PexelsError("Pexels returned 401 — PEXELS_API_KEY is invalid")
    if response.status_code == 429:
        raise PexelsError("Pexels rate limit hit (429) — back off and retry later")
    if response.status_code >= 500:
        raise PexelsError(f"Pexels server error {response.status_code}")
    if response.status_code != 200:
        log.error(f"Pexels HTTP {response.status_code}: {response.text[:300]}")
        return {}

    return response.json()


def _alternate_query(query: str) -> str | None:
    """
    Produce a deliberate alternate query for one retry. Strategy:
    longest meaningful word from the original query. Returns None if
    nothing usable remains (so we don't fall back to "nature landscape"
    silently).
    """
    stop = {
        "the", "a", "an", "and", "or", "of", "to", "with", "in", "on",
        "for", "at", "by", "from", "into", "over", "under", "is", "are",
        "shot", "footage", "video", "clip", "scene", "view", "angle",
    }
    words = [w for w in query.lower().split() if w not in stop and len(w) > 3]
    if not words:
        return None
    # Longest word is usually the most distinctive subject noun.
    return max(words, key=len)


# ── download ─────────────────────────────────────────────────────────────────

def _download_video(url: str, out_path: str) -> None:
    """Stream a Pexels video file to disk. Raises on failure."""
    log.info(f"  → downloading {url[:60]}... → {out_path}")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 64):
                f.write(chunk)
    size_kb = os.path.getsize(out_path) // 1024
    log.info(f"  → downloaded {size_kb} KB")


# ── debug helper exposed via main.py ─────────────────────────────────────────

def debug_search(query: str) -> dict:
    """
    Returns a structured dump of what Pexels returns for a query.
    Used by the /debug/pexels-test endpoint.
    """
    try:
        key_set = bool(os.getenv("PEXELS_API_KEY", "").strip())
        data = _pexels_search(query)
        videos = data.get("videos", [])
        return {
            "ok": True,
            "api_key_set": key_set,
            "query": query,
            "result_count": len(videos),
            "top_results": [
                {
                    "id": v.get("id"),
                    "duration": v.get("duration"),
                    "width": v.get("width"),
                    "height": v.get("height"),
                    "user": (v.get("user") or {}).get("name"),
                    "url": v.get("url"),
                }
                for v in videos[:5]
            ],
        }
    except PexelsError as e:
        return {"ok": False, "error": str(e), "api_key_set": bool(os.getenv("PEXELS_API_KEY"))}
