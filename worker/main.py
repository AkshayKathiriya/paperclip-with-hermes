"""
YouTube Video Worker — Flask API
========================================
This is the single Python service that runs on Railway alongside Paperclip.
It serves 3 HTTP agent endpoints — one per Paperclip agent (HTTP adapter pattern).

Paperclip sends a POST to each endpoint on heartbeat.
The endpoint calls Claude API, does its job, returns JSON result.
Paperclip stores the result as a work product on the issue.

Endpoints:
  POST /agent/scriptwriter        ← Agent 1: writes narration script
  POST /agent/scene-director      ← Agent 2: breaks script into scenes
  POST /agent/production-manager  ← Agent 3: SEO pack + triggers assembly
  GET  /status/<job_id>           ← Paperclip polls for video progress
  GET  /health                    ← Railway health check
"""

import os
import uuid
import json
import logging
import threading
import anthropic
from flask import Flask, request, jsonify, send_from_directory

from pipeline.fetch_pexels import fetch_pexels_videos
from pipeline.tts import generate_voiceover
from pipeline.subtitles import generate_subtitles
from pipeline.assemble import assemble_video

# ── setup ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# Anthropic client — reads ANTHROPIC_API_KEY from Railway env vars automatically
claude = anthropic.Anthropic()

# In-memory job cache — backed by disk so jobs survive worker restarts
# {job_id: {status, progress, result, error}}
JOBS = {}

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/paperclip/videos")
os.makedirs(OUTPUT_DIR, exist_ok=True)

MODEL = "claude-sonnet-4-6"


# ── job state persistence (survives gunicorn worker restarts) ─────────────────

def _job_file(job_id: str) -> str:
    return os.path.join(OUTPUT_DIR, job_id, "job.json")


def _save_job(job_id: str) -> None:
    """Write current job state to disk."""
    try:
        path = _job_file(job_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(JOBS[job_id], f)
    except Exception:
        pass  # don't crash the pipeline over a persistence failure


def _load_job(job_id: str) -> dict | None:
    """Load job state from disk (used when not in memory after restart)."""
    try:
        with open(_job_file(job_id)) as f:
            return json.load(f)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# AGENT 1 — Scriptwriter
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/agent/scriptwriter", methods=["POST"])
def agent_scriptwriter():
    """
    Paperclip HTTP adapter calls this when a task is assigned to the
    Scriptwriter agent. Reads topic + style from the task, calls Claude,
    returns the full narration script as a work product.
    """
    data  = request.get_json(force=True)
    task  = data.get("task", {})
    desc  = task.get("description", "") or ""
    title = task.get("title", "") or ""

    # Prefer explicit "topic: ..." field, then title, then full description as fallback
    topic = _extract_field(desc, "topic") or title or desc.strip()
    style = _extract_field(desc, "style", default="educational")

    log.info(f"[Scriptwriter] topic='{topic[:80]}' style='{style}'")

    if not topic:
        return jsonify({"error": "No topic in task title or description"}), 400

    try:
        resp = claude.messages.create(
            model=MODEL,
            max_tokens=1500,
            system="You are an expert YouTube scriptwriter. Return ONLY valid JSON — no markdown fences.",
            messages=[{"role": "user", "content": f"""
Write a compelling {style} YouTube video script about: "{topic}"

Requirements:
- 700-900 words of narration only (no stage directions)
- Strong hook in first 2 sentences
- 5 sections marked [SECTION: Title]
- Conversational tone, sentences under 20 words
- End with a CTA

Return ONLY this JSON:
{{
  "script": "<full narration text>",
  "hook": "<first 2 sentences>",
  "title_suggestion": "<working video title>",
  "word_count": 820,
  "sections": ["Section 1", "Section 2", "Section 3", "Section 4", "Section 5"]
}}
"""}]
        )

        result = _parse_json(resp.content[0].text)
        log.info(f"[Scriptwriter] ✅ {result.get('word_count','?')} words")

        return jsonify({
            "status": "done",
            "result": result,
            "work_product": {
                "type": "script",
                "title": result.get("title_suggestion", topic),
                "data": result
            }
        })

    except Exception as e:
        log.exception(f"[Scriptwriter] ❌ {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# AGENT 2 — Scene Director
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/agent/scene-director", methods=["POST"])
def agent_scene_director():
    """
    Receives the script from Agent 1 (passed via Paperclip task context),
    breaks it into 6 timed visual scenes with Pexels search queries.
    """
    data        = request.get_json(force=True)
    task        = data.get("task", {})
    context     = data.get("context", {})
    topic       = task.get("title", "video")

    # Paperclip passes previous work products in context
    script_data = context.get("script") or task.get("script", {})
    script_text = script_data if isinstance(script_data, str) \
                  else script_data.get("script", "")

    log.info(f"[SceneDirector] topic='{topic}' script_len={len(script_text)}")

    if not script_text:
        return jsonify({"error": "No script found in task context"}), 400

    try:
        resp = claude.messages.create(
            model=MODEL,
            max_tokens=1500,
            system="You are a video scene director. Return ONLY valid JSON — no markdown fences.",
            messages=[{"role": "user", "content": f"""
Break this script into exactly 6 visual scenes for a YouTube video about "{topic}".

Script:
{script_text[:2500]}

Return ONLY this JSON:
{{
  "scenes": [
    {{
      "num": "01",
      "title": "Scene title",
      "narration_excerpt": "2-3 sentences from the script verbatim",
      "visual_direction": "Specific description of what viewer sees on screen",
      "pexels_search_query": "3-5 word search term",
      "duration_seconds": 45,
      "mood": "epic"
    }}
  ],
  "total_scenes": 6,
  "total_duration_seconds": 420
}}

Rules: exactly 6 scenes, pexels_search_query 2-5 lowercase words,
total duration 360-480s, scene 1 is hook (15-30s), scene 6 is CTA (30-45s).
"""}]
        )

        result = _parse_json(resp.content[0].text)
        log.info(f"[SceneDirector] ✅ {len(result.get('scenes', []))} scenes")

        return jsonify({
            "status": "done",
            "result": result,
            "work_product": {
                "type": "scenes",
                "title": f"Scene Breakdown — {topic}",
                "data": result
            }
        })

    except Exception as e:
        log.exception(f"[SceneDirector] ❌ {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# AGENT 3 — Production Manager
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/agent/production-manager", methods=["POST"])
def agent_production_manager():
    """
    Receives script + scenes, generates SEO pack via Claude,
    then kicks off the async video assembly pipeline.
    Returns job_id so Paperclip can poll /status/<job_id> for the .mp4.
    """
    data        = request.get_json(force=True)
    task        = data.get("task", {})
    context     = data.get("context", {})
    topic       = task.get("title", "video")

    script_data = context.get("script") or task.get("script", {})
    scenes_data = context.get("scenes") or task.get("scenes", {})

    script_text = script_data if isinstance(script_data, str) \
                  else script_data.get("script", "")
    scenes      = scenes_data if isinstance(scenes_data, list) \
                  else scenes_data.get("scenes", [])

    log.info(f"[ProductionManager] topic='{topic}' scenes={len(scenes)}")

    if not script_text or not scenes:
        return jsonify({"error": "Missing script or scenes in task context"}), 400

    try:
        # Step 1: generate SEO pack
        resp = claude.messages.create(
            model=MODEL,
            max_tokens=800,
            system="You are a YouTube SEO expert. Return ONLY valid JSON — no markdown fences.",
            messages=[{"role": "user", "content": f"""
Create a complete YouTube production pack for: "{topic}"

Return ONLY this JSON:
{{
  "youtube_title": "SEO title under 60 chars with a power word",
  "youtube_description": "First 150 chars of video description",
  "tags": ["tag1","tag2","tag3","tag4","tag5","tag6","tag7","tag8","tag9","tag10"],
  "thumbnail_concept": "Specific thumbnail description: background, text, emotion",
  "music_mood": "e.g. Epic orchestral, 85-95 BPM, builds tension",
  "editing_notes": "Key editing style instructions for the video"
}}
"""}]
        )

        seo_pack = _parse_json(resp.content[0].text)
        log.info(f"[ProductionManager] SEO pack done: '{seo_pack.get('youtube_title','?')}'")

        # Step 2: kick off async video assembly in background thread
        job_id = str(uuid.uuid4())[:8]
        JOBS[job_id] = {"status": "queued", "progress": 0, "result": None, "error": None}

        thread = threading.Thread(
            target=_run_assembly,
            args=(job_id, script_text, scenes),
            daemon=True
        )
        thread.start()
        log.info(f"[ProductionManager] assembly job {job_id} queued")

        return jsonify({
            "status": "done",
            "result": {
                "seo_pack": seo_pack,
                "assembly_job_id": job_id,
                "poll_url": f"/status/{job_id}"
            },
            "work_product": {
                "type": "production_pack",
                "title": seo_pack.get("youtube_title", topic),
                "data": {
                    "seo": seo_pack,
                    "assembly_job_id": job_id,
                    "poll_url": f"/status/{job_id}"
                }
            }
        })

    except Exception as e:
        log.exception(f"[ProductionManager] ❌ {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# DIRECT ASSEMBLY ENDPOINT  (called by Paperclip)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/assemble-video", methods=["POST"])
def assemble_video_endpoint():
    """
    Direct entry point for Paperclip to kick off video assembly.
    Accepts script + scenes, queues a background job, returns job_id immediately.

    Expected body:
    {
      "script": "<narration text>",
      "scenes": [{"num":"01","pexels_search_query":"...","duration_seconds":45}, ...]
    }

    OR Paperclip task-context style:
    {
      "task":    {"title": "..."},
      "context": {"script": {...}, "scenes": {...}}
    }
    """
    data = request.get_json(force=True)

    # support both flat and Paperclip task-context payloads
    script_data = data.get("script") or data.get("context", {}).get("script") or \
                  data.get("task", {}).get("script", {})
    scenes_data = data.get("scenes") or data.get("context", {}).get("scenes") or \
                  data.get("task", {}).get("scenes", {})

    script = script_data if isinstance(script_data, str) \
             else script_data.get("script", "") if isinstance(script_data, dict) else ""
    scenes = scenes_data if isinstance(scenes_data, list) \
             else scenes_data.get("scenes", []) if isinstance(scenes_data, dict) else []

    if not script:
        return jsonify({"error": "Missing 'script' in request body"}), 400
    if not scenes:
        return jsonify({"error": "Missing 'scenes' in request body"}), 400

    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {"status": "queued", "progress": 0, "result": None, "error": None}

    thread = threading.Thread(
        target=_run_assembly,
        args=(job_id, script, scenes),
        daemon=True
    )
    thread.start()
    log.info(f"[AssembleVideo] job {job_id} queued ({len(scenes)} scenes)")

    return jsonify({
        "status": "queued",
        "job_id": job_id,
        "poll_url": f"/status/{job_id}",
    })


# ══════════════════════════════════════════════════════════════════════════════
# ASYNC VIDEO ASSEMBLY PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def _run_assembly(job_id: str, script: str, scenes: list):
    """
    Background thread: Pexels → Piper TTS → Whisper → MoviePy+FFmpeg → .mp4
    Updates JOBS[job_id] at each step so /status can report progress.
    """
    try:
        work_dir = os.path.join(OUTPUT_DIR, job_id)
        os.makedirs(work_dir, exist_ok=True)

        _job(job_id, "fetching_videos", 10)
        video_clips = fetch_pexels_videos(scenes, work_dir)

        _job(job_id, "generating_voice", 35)
        audio_path = generate_voiceover(script, work_dir, speed=0.92)

        _job(job_id, "generating_subtitles", 55)
        srt_path = generate_subtitles(audio_path, work_dir)

        _job(job_id, "assembling_video", 75)
        out_file = f"video_{job_id}.mp4"
        out_path = os.path.join(work_dir, out_file)

        assemble_video(
            video_clips=video_clips,
            audio_path=audio_path,
            srt_path=srt_path,
            output_path=out_path,
            resolution="1920x1080",
            fps=30,
            subtitle_style="bottom-center",
        )

        base_url     = os.getenv("WORKER_PUBLIC_URL", "")
        download_url = f"{base_url}/videos/{job_id}/{out_file}"

        JOBS[job_id] = {
            "status": "done", "progress": 100,
            "result": {"download_url": download_url, "job_id": job_id, "filename": out_file},
            "error": None
        }
        _save_job(job_id)
        log.info(f"[Assembly] ✅ {job_id} → {download_url}")

    except Exception as e:
        log.exception(f"[Assembly] ❌ {job_id}: {e}")
        JOBS[job_id] = {"status": "error", "progress": 0, "result": None, "error": str(e)}
        _save_job(job_id)


def _job(job_id: str, status: str, progress: int) -> None:
    JOBS[job_id]["status"]   = status
    JOBS[job_id]["progress"] = progress
    _save_job(job_id)


# ══════════════════════════════════════════════════════════════════════════════
# STATUS + HEALTH ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/status/<job_id>", methods=["GET"])
def get_status(job_id):
    # check in-memory cache first, then fall back to disk (survives restarts)
    if job_id not in JOBS:
        saved = _load_job(job_id)
        if saved is None:
            return jsonify({"error": "Job not found"}), 404
        JOBS[job_id] = saved   # re-hydrate cache
    return jsonify({"job_id": job_id, **JOBS[job_id]})


@app.route("/videos/<path:filename>", methods=["GET"])
def serve_video(filename):
    return send_from_directory(OUTPUT_DIR, filename)


@app.route("/health", methods=["GET"])
def health():
    active = len([j for j in JOBS.values() if j["status"] not in ("done","error")])
    return jsonify({"status": "ok", "active_jobs": active, "total_jobs": len(JOBS)})


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _parse_json(text: str) -> dict:
    """Parse Claude's response safely, stripping any accidental markdown fences."""
    clean = text.strip()
    if clean.startswith("```"):
        parts = clean.split("```")
        clean = parts[1] if len(parts) > 1 else clean
        if clean.startswith("json"):
            clean = clean[4:]
    return json.loads(clean.strip())


def _extract_field(text: str, field: str, default: str = "") -> str:
    """Extract 'field: value' from a free-form task description (case-insensitive match, preserves value case)."""
    for line in (text or "").splitlines():
        if field.lower() in line.lower() and ":" in line:
            return line.split(":", 1)[1].strip()
    return default


# ══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"Starting YouTube Video Worker on :{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
