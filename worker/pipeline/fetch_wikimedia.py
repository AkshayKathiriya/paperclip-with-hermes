"""
fetch_wikimedia.py
Downloads a still image from Wikimedia Commons.

Scene Director passes Commons URLs verbatim from the Researcher's brief.
We just need to resolve them to actual image bytes and save to disk.

Two URL shapes are accepted:
  1. https://commons.wikimedia.org/wiki/File:Foo.jpg  → resolve via API
  2. https://upload.wikimedia.org/wikipedia/commons/.../Foo.jpg  → direct
"""

import os
import re
import logging
import requests

log = logging.getLogger(__name__)

WIKI_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "CaseClosedBot/0.1 (https://github.com/AkshayKathiriya/paperclip-with-hermes)"


class WikimediaError(RuntimeError):
    """Raised for any Wikimedia fetch failure that should fail a shot loudly."""


def fetch_wikimedia_image(url: str, out_path: str) -> str:
    """
    Download a Commons image to out_path. Returns the local path.
    Accepts either upload.wikimedia.org direct URLs or commons.wikimedia.org/wiki/File: pages.
    """
    if not url or "wikimedia" not in url.lower() and "wikipedia" not in url.lower():
        raise WikimediaError(f"Not a Wikimedia URL: {url}")

    direct_url = url
    if "/wiki/File:" in url or "/wiki/Special:FilePath/" in url:
        direct_url = _resolve_file_page(url)

    _download(direct_url, out_path)
    return out_path


def _resolve_file_page(page_url: str) -> str:
    """Use the MediaWiki API to resolve a File: page to its actual image URL."""
    m = re.search(r"/wiki/(?:File:|Special:FilePath/)(.+?)(?:[?#]|$)", page_url)
    if not m:
        raise WikimediaError(f"Could not parse filename from {page_url}")

    filename = m.group(1).replace("_", " ")
    params = {
        "action": "query",
        "format": "json",
        "titles": f"File:{filename}",
        "prop": "imageinfo",
        "iiprop": "url",
        "iiurlwidth": 2000,
    }
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(WIKI_API, params=params, headers=headers, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        raise WikimediaError(f"MediaWiki API error: {e}") from e

    data = r.json()
    pages = (data.get("query") or {}).get("pages") or {}
    for _, page in pages.items():
        info = (page.get("imageinfo") or [{}])[0]
        # Prefer thumburl (scaled) when available, else fall back to url
        return info.get("thumburl") or info.get("url") or _raise(filename)
    raise WikimediaError(f"No imageinfo returned for File:{filename}")


def _raise(filename: str):
    raise WikimediaError(f"No URL in imageinfo for File:{filename}")


def _download(url: str, out_path: str) -> None:
    log.info(f"  → downloading Wikimedia image {url[:80]}")
    headers = {"User-Agent": USER_AGENT}
    try:
        with requests.get(url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    f.write(chunk)
    except requests.RequestException as e:
        raise WikimediaError(f"Image download failed for {url}: {e}") from e

    if os.path.getsize(out_path) < 1024:
        raise WikimediaError(f"Downloaded image is suspiciously small: {out_path}")

    log.info(f"  → saved {os.path.getsize(out_path) // 1024} KB → {out_path}")
