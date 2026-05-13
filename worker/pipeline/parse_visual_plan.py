"""
parse_visual_plan.py
Accept Scene Director's visual plan in any of three shapes and normalize
to the canonical dict structure compose_shots expects:

  {
    "total_duration_sec": N,
    "scenes": [
      {
        "num": "01",
        "duration_sec": N,
        "shots": [
          { "id": "...", "duration_sec": N, "source": "...", "query"/"prompt"/"url": ... }
        ]
      }
    ]
  }

Accepted inputs:
  1. Already canonical dict          → returned unchanged (after validation)
  2. JSON-encoded string             → json.loads, then validate
  3. Markdown formatted as Scene Director's AGENTS.md output:
        ### Shot 01a (6s) — AI Image
        **Prompt:** ...
        **Narration:** ...
"""

import re
import json
import logging
from typing import Any

log = logging.getLogger(__name__)


SOURCE_ALIASES = {
    "ai_image": "ai_image",
    "ai image": "ai_image",
    "ai-image": "ai_image",
    "dalle": "ai_image",
    "dall-e": "ai_image",
    "wikimedia": "wikimedia",
    "wiki": "wikimedia",
    "pexels": "pexels",
    "stock": "pexels",
}


def normalize_visual_plan(raw: Any) -> dict:
    """
    Return a canonical visual_plan dict regardless of input shape.
    Raises ValueError if it can't be normalized to something with shots.
    """
    if raw is None:
        raise ValueError("visual_plan is None")

    # Try JSON first (string or dict)
    parsed = _try_json(raw)

    if isinstance(parsed, dict) and parsed.get("scenes"):
        return _validate_canonical(parsed)

    # Fall through to markdown parser
    text = raw if isinstance(raw, str) else _stringify(raw)
    parsed_md = _parse_markdown(text)
    if parsed_md.get("scenes"):
        log.info(f"parse_visual_plan: parsed Markdown into {sum(len(s['shots']) for s in parsed_md['scenes'])} shots across {len(parsed_md['scenes'])} scenes")
        return parsed_md

    raise ValueError("visual_plan could not be parsed as JSON or Markdown")


# ── JSON path ────────────────────────────────────────────────────────────────

def _try_json(val: Any) -> Any:
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("```"):
            s = s.strip("`")
            if s.startswith("json"):
                s = s[4:]
            s = s.strip()
        try:
            return json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return val  # caller will fall through to markdown
    return val


def _validate_canonical(plan: dict) -> dict:
    """Ensure every shot has a non-empty source. If even one shot is bare, return as-is and let compose_shots fall back."""
    return plan


def _stringify(obj: Any) -> str:
    try:
        return json.dumps(obj)
    except Exception:
        return str(obj)


# ── Markdown path ────────────────────────────────────────────────────────────

# Heading patterns
_SCENE_HEADING = re.compile(
    r"^##\s*(?:scene\s*)?(?P<num>\d+)[:\.\s]\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
_SHOT_HEADING = re.compile(
    r"^###\s*shot\s*(?P<id>[\w\-]+)\s*(?:\((?P<dur>[\d\.]+)\s*s\))?"
    r"(?:\s*[—–\-]\s*(?P<source>[\w\s\-]+))?\s*$",
    re.IGNORECASE,
)
# Field lines like `**Prompt:** ...`, `**URL:** ...`, `**Query:** ...`, `**Narration:** "..."`
# Handle both `**Prompt:**` (colon inside) and `**Prompt**:` (colon outside).
_FIELD_LINE = re.compile(
    r"^\s*\*\*\s*(?P<key>[A-Za-z _\-]+?)\s*:?\s*\*\*\s*[:\-]?\s*(?P<val>.*)$"
)
# Standalone duration markers `**Duration:** 70 seconds`, used at scene level
_DURATION_FIELD = re.compile(
    r"\*\*duration\*\*\s*[:\-]?\s*(?P<n>[\d\.]+)\s*(?:s|sec|seconds|secs)?",
    re.IGNORECASE,
)


def _parse_markdown(text: str) -> dict:
    """
    Parse the Markdown form of Scene Director's visual plan.
    Tolerates extra header/text outside scene blocks.
    """
    lines = text.splitlines()
    scenes: list[dict] = []
    cur_scene: dict | None = None
    cur_shot: dict | None = None
    current_field: str | None = None
    total_duration = 0.0

    def _commit_shot():
        nonlocal cur_shot, current_field
        if cur_shot and cur_scene:
            _finalize_shot(cur_shot)
            cur_scene["shots"].append(cur_shot)
        cur_shot = None
        current_field = None

    def _commit_scene():
        nonlocal cur_scene
        _commit_shot()
        if cur_scene:
            scenes.append(cur_scene)
        cur_scene = None

    for raw_line in lines:
        line = raw_line.rstrip()

        # Scene heading: `## Scene 01: The Confession` or `## 01: ...`
        m_scene = _SCENE_HEADING.match(line)
        # Shot heading: `### Shot 01a (6s) — AI Image`
        m_shot  = _SHOT_HEADING.match(line)

        if m_shot:
            _commit_shot()
            shot_id = m_shot.group("id")
            dur     = float(m_shot.group("dur") or 0) or None
            source  = _canon_source(m_shot.group("source") or "")
            cur_shot = {
                "id":           shot_id,
                "duration_sec": dur,
                "source":       source,
            }
            current_field = None
            continue

        if m_scene:
            _commit_scene()
            cur_scene = {
                "num":          m_scene.group("num").zfill(2),
                "title":        m_scene.group("title").strip(),
                "duration_sec": None,
                "shots":        [],
            }
            current_field = None
            continue

        # Field line within current shot or scene
        m_field = _FIELD_LINE.match(line)
        if m_field:
            key = m_field.group("key").strip().lower().replace(" ", "_").replace("-", "_")
            val = m_field.group("val").strip()
            target = cur_shot if cur_shot is not None else cur_scene
            if target is None:
                continue

            if key in ("prompt",):
                target["prompt"] = _strip_md_quotes(val)
                current_field = "prompt"
            elif key in ("url",):
                target["url"] = val.split()[0] if val else ""
                current_field = "url"
            elif key in ("query", "pexels_query", "search_query"):
                target["query"] = _strip_md_quotes(val)
                current_field = "query"
            elif key in ("credit", "attribution", "license"):
                target["credit"] = val
                current_field = "credit"
            elif key in ("narration", "narration_excerpt", "narration_text"):
                target["narration_excerpt"] = _strip_md_quotes(val)
                current_field = "narration_excerpt"
            elif key in ("duration", "duration_sec", "duration_seconds"):
                # Allow `**Duration:** 70 seconds`
                mdur = re.search(r"[\d\.]+", val)
                if mdur:
                    target["duration_sec"] = float(mdur.group(0))
                current_field = None
            elif key in ("section",):
                target["narration_section"] = val
                current_field = None
            else:
                target[key] = val
                current_field = key
            continue

        # Continuation of a multi-line field (Prompt: ... \n more text on next line)
        if current_field and (cur_shot or cur_scene):
            target = cur_shot if cur_shot is not None else cur_scene
            if line.strip() and not line.startswith("##") and not line.startswith("---"):
                existing = target.get(current_field, "")
                joined = (existing + " " + _strip_md_quotes(line.strip())).strip()
                target[current_field] = joined

    _commit_scene()

    for s in scenes:
        if not s.get("duration_sec"):
            s["duration_sec"] = sum((sh.get("duration_sec") or 0) for sh in s["shots"]) or 0
        total_duration += float(s["duration_sec"])

    return {
        "total_duration_sec": int(round(total_duration)),
        "scenes": scenes,
    }


def _canon_source(raw: str) -> str:
    key = re.sub(r"[^a-z_\- ]", "", raw.lower()).strip()
    return SOURCE_ALIASES.get(key, key.replace(" ", "_") if key else "")


def _strip_md_quotes(s: str) -> str:
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1]
    return s


def _finalize_shot(shot: dict) -> None:
    """Default duration to 5s if missing; pick query from narration if pexels-no-query."""
    if not shot.get("duration_sec"):
        shot["duration_sec"] = 5.0
    src = shot.get("source") or ""
    if src == "pexels" and not shot.get("query"):
        # Last-resort: use first few words of narration excerpt
        narr = shot.get("narration_excerpt") or ""
        shot["query"] = " ".join(narr.split()[:4]) or "documentary footage"
