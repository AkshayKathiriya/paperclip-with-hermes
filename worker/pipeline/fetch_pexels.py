"""
fetch_pexels.py
Fetches one stock video clip per scene from the Pexels API.
Saves each clip to work_dir/clips/scene_XX.mp4
Returns list of local file paths in scene order.
"""

import os
import requests
import logging

log = logging.getLogger(__name__)

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
PEXELS_VIDEO_URL = "https://api.pexels.com/videos/search"

HEADERS = {"Authorization": PEXELS_API_KEY}

# Target clip settings
MIN_DURATION = 10   # seconds — clips shorter than this are skipped
ORIENTATION  = "landscape"
PER_PAGE     = 5    # fetch top 5 results and pick the best


def fetch_pexels_videos(scenes: list[dict], work_dir: str) -> list[str]:
    """
    For each scene, search Pexels using scene['pexels_search_query'],
    download the best matching clip, and return list of local paths.

    Falls back to a generic query if the specific one returns no results.
    """
    clips_dir = os.path.join(work_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    clip_paths = []

    for i, scene in enumerate(scenes):
        scene_num = scene.get("num", str(i + 1).zfill(2))
        query     = scene.get("pexels_search_query", "cinematic nature")
        duration  = scene.get("duration_seconds", 45)
        out_path  = os.path.join(clips_dir, f"scene_{scene_num}.mp4")

        log.info(f"Fetching clip for scene {scene_num}: '{query}'")

        video_url = search_pexels(query, duration)

        # fallback to simpler query if nothing found
        if not video_url:
            fallback = query.split()[0]   # just first word
            log.warning(f"No result for '{query}', trying fallback '{fallback}'")
            video_url = search_pexels(fallback, duration)

        # last resort fallback
        if not video_url:
            log.warning(f"No Pexels result at all for scene {scene_num}, using placeholder")
            video_url = search_pexels("cinematic nature landscape", duration)

        if video_url:
            download_video(video_url, out_path)
            clip_paths.append(out_path)
        else:
            log.error(f"Could not fetch any clip for scene {scene_num}")
            clip_paths.append(None)   # assembler handles None gracefully

    return clip_paths


def search_pexels(query: str, target_duration: int) -> str | None:
    """
    Searches Pexels for a video matching the query.
    Returns the download URL of the best matching clip (HD preferred).
    """
    try:
        response = requests.get(
            PEXELS_VIDEO_URL,
            headers=HEADERS,
            params={
                "query": query,
                "orientation": ORIENTATION,
                "per_page": PER_PAGE,
                "size": "medium",
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        videos = data.get("videos", [])
        if not videos:
            return None

        # pick video closest to target duration
        best = min(videos, key=lambda v: abs(v.get("duration", 999) - target_duration))

        # get HD file, fallback to SD
        files = best.get("video_files", [])
        hd_files = [f for f in files if f.get("quality") in ("hd", "sd") and f.get("width", 0) >= 1280]
        if not hd_files:
            hd_files = files  # take whatever is available

        if not hd_files:
            return None

        # sort by width descending, pick largest
        hd_files.sort(key=lambda f: f.get("width", 0), reverse=True)
        return hd_files[0].get("link")

    except Exception as e:
        log.error(f"Pexels API error for '{query}': {e}")
        return None


def download_video(url: str, out_path: str) -> None:
    """Downloads a video from url to out_path with streaming."""
    log.info(f"Downloading {url[:60]}... → {out_path}")
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 64):
                    f.write(chunk)
        log.info(f"Downloaded {os.path.getsize(out_path) // 1024} KB")
    except Exception as e:
        log.error(f"Download failed for {url}: {e}")
        raise
