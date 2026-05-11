# YouTube Agent Worker — HTTP API (single reference)

Paste-friendly doc for orchestrators / agent tools. All `POST` bodies are **JSON** with header `Content-Type: application/json`.

**Base URL:** set once (example: local after avoiding macOS port 5000):

```bash
export BASE="http://localhost:5001"
```

**Env (worker side):**

- Required: `ANTHROPIC_API_KEY`, `PEXELS_API_KEY` (assembly / stock video).
- Recommended when not on port 5000: `WORKER_PUBLIC_URL` must match how clients reach this server (used in job `download_url`).
- Optional: `PORT`, `OUTPUT_DIR`, `PIPER_MODEL_DIR`, `WHISPER_MODEL`.

---

## Endpoints overview

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness |
| `POST` | `/agent/scriptwriter` | Narration script (Claude) |
| `POST` | `/agent/scene-director` | 6 scenes + Pexels queries (Claude) |
| `POST` | `/agent/production-manager` | SEO pack + queue full assembly |
| `POST` | `/assemble-video` | Queue assembly only |
| `GET` | `/status/<job_id>` | Poll async job |
| `GET` | `/videos/<path>` | Download rendered file |

---

## `GET /health`

```bash
curl -sS "${BASE}/health"
```

Example success shape: `{ "status": "ok", "active_jobs": 0, "total_jobs": 0 }`

---

## `POST /agent/scriptwriter`

Topic from `task.title`:

```bash
curl -sS -X POST "${BASE}/agent/scriptwriter" \
  -H "Content-Type: application/json" \
  -d '{
    "task": {
      "title": "Why black holes evaporate",
      "description": ""
    }
  }'
```

Topic + style from free-form description (`topic:` / `style:` lines):

```bash
curl -sS -X POST "${BASE}/agent/scriptwriter" \
  -H "Content-Type: application/json" \
  -d '{
    "task": {
      "title": "",
      "description": "topic: Quantum tunneling in CPUs\nstyle: educational"
    }
  }'
```

---

## `POST /agent/scene-director`

Script inside `context` as object (Paperclip-style):

```bash
curl -sS -X POST "${BASE}/agent/scene-director" \
  -H "Content-Type: application/json" \
  -d '{
    "task": { "title": "Deep sea creatures" },
    "context": {
      "script": {
        "script": "YOUR_FULL_NARRATION_SCRIPT_TEXT_HERE"
      }
    }
  }'
```

Script as plain string:

```bash
curl -sS -X POST "${BASE}/agent/scene-director" \
  -H "Content-Type: application/json" \
  -d '{
    "task": { "title": "Deep sea" },
    "context": {
      "script": "YOUR_FULL_NARRATION_SCRIPT_TEXT_HERE"
    }
  }'
```

---

## `POST /agent/production-manager`

Expects narration + scenes. **Queues real assembly** (Pexels, Piper TTS, Whisper, FFmpeg on the worker).

```bash
curl -sS -X POST "${BASE}/agent/production-manager" \
  -H "Content-Type: application/json" \
  -d '{
    "task": { "title": "Deep sea creatures" },
    "context": {
      "script": { "script": "YOUR_FULL_NARRATION_TEXT" },
      "scenes": {
        "scenes": [
          { "num": "01", "pexels_search_query": "ocean deep blue", "duration_seconds": 30 },
          { "num": "02", "pexels_search_query": "bioluminescent jellyfish", "duration_seconds": 45 },
          { "num": "03", "pexels_search_query": "underwater robot camera", "duration_seconds": 45 },
          { "num": "04", "pexels_search_query": "marine research ship", "duration_seconds": 45 },
          { "num": "05", "pexels_search_query": "deep sea darkness", "duration_seconds": 45 },
          { "num": "06", "pexels_search_query": "ocean horizon sunset", "duration_seconds": 35 }
        ]
      }
    }
  }'
```

Response includes `assembly_job_id` (short id) and `poll_url` (relative, e.g. `/status/ab12cd34`).

---

## `POST /assemble-video`

Flat payload:

```bash
curl -sS -X POST "${BASE}/assemble-video" \
  -H "Content-Type: application/json" \
  -d '{
    "script": "YOUR_FULL_NARRATION_TEXT",
    "scenes": [
      { "num": "01", "pexels_search_query": "ocean waves sunset", "duration_seconds": 30 },
      { "num": "02", "pexels_search_query": "forest aerial drone", "duration_seconds": 45 }
    ]
  }'
```

Nested `task` + `context` (same idea as production-manager) is also accepted.

---

## `GET /status/<job_id>`

```bash
JOB_ID="YOUR_JOB_ID"
curl -sS "${BASE}/status/${JOB_ID}"
```

Poll until `status` is `"done"` or `"error"`. On success, `result` may include `download_url` built from **`WORKER_PUBLIC_URL`** plus `/videos/...`.

---

## `GET /videos/<path>`

Prefer the **`download_url`** from the finished job status. Typical pattern:

```text
GET ${BASE}/videos/<job_id>/video_<job_id>.mp4
```

Download with curl:

```bash
JOB_ID="YOUR_JOB_ID"
curl -sS -f -O -J "${BASE}/videos/${JOB_ID}/video_${JOB_ID}.mp4"
```

If `download_url` is wrong while testing locally, set `WORKER_PUBLIC_URL` equal to `${BASE}` (same scheme, host, and port).

---

## Minimal agent “tool” list (for prompting)

1. **health** → `GET /health`
2. **write_script** → `POST /agent/scriptwriter`
3. **direct_scenes** → `POST /agent/scene-director`
4. **produce_video** → `POST /agent/production-manager` (SEO + enqueue)
5. **assemble_video_direct** → `POST /assemble-video`
6. **job_status** → `GET /status/<job_id>`
7. **fetch_video_file** → `GET /videos/<path>`

---

## Copy-paste block (bash, all curls)

```bash
export BASE="http://localhost:5001"

curl -sS "${BASE}/health"

curl -sS -X POST "${BASE}/agent/scriptwriter" -H "Content-Type: application/json" \
  -d '{"task":{"title":"Demo topic for script","description":""}}'

curl -sS -X POST "${BASE}/agent/scene-director" -H "Content-Type: application/json" \
  -d '{"task":{"title":"Demo"},"context":{"script":{"script":"Paragraph one.\nParagraph two."}}}'

curl -sS -X POST "${BASE}/assemble-video" -H "Content-Type: application/json" \
  -d '{"script":"Hello world narration.","scenes":[{"num":"01","pexels_search_query":"nature landscape","duration_seconds":30},{"num":"02","pexels_search_query":"city skyline","duration_seconds":30}]}'
```

Replace `BASE` and extend scene lists for production flows; capture `job_id` from `/assemble-video` or production-manager response for `/status/` polling.
