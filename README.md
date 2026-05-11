# YouTube AI Studio — Monorepo

Self-hosted AI video production platform combining [Paperclip AI](https://github.com/paperclipai/paperclip) with a YouTube video assembly worker.

## Services

| Service | Directory | Port | Description |
|---------|-----------|------|-------------|
| `paperclip` | `paperclip/` | 3100 | AI orchestration UI + agent runtime |
| `worker` | `worker/` | 5000 | Video assembly: TTS → subtitles → mp4 |
| `db` | — | 5433 | Postgres 17 |

## Local development

```bash
cp .env.example .env
# Fill in: BETTER_AUTH_SECRET, ANTHROPIC_API_KEY, PEXELS_API_KEY

docker compose up --build
```

- Paperclip UI → http://localhost:3100  
- Worker health → http://localhost:5001/health

## Railway deployment

Each service deploys as a separate Railway service pointing at this monorepo with its own root directory:

| Railway service | Root directory | Port |
|-----------------|---------------|------|
| paperclip | `paperclip/` | 3100 |
| worker | `worker/` | 5000 |

### Environment variables (set per-service in Railway)

**paperclip service:**
| Variable | Value |
|----------|-------|
| `BETTER_AUTH_SECRET` | `openssl rand -hex 32` |
| `PAPERCLIP_PUBLIC_URL` | `https://${{RAILWAY_PUBLIC_DOMAIN}}` |
| `ANTHROPIC_API_KEY` | your key |
| `VIDEO_WORKER_URL` | `http://worker.railway.internal:5000` |

**worker service:**
| Variable | Value |
|----------|-------|
| `ANTHROPIC_API_KEY` | your key |
| `PEXELS_API_KEY` | your key |
| `WHISPER_MODEL` | `base` |

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Paperclip AI (paperclip/ — port 3100)              │
│  ├─ CEO agent        orchestrates video pipeline     │
│  ├─ Scriptwriter     writes narration via Claude     │
│  ├─ Scene Director   breaks script into scenes       │
│  └─ Production Mgr   SEO + triggers worker           │
└──────────────────┬──────────────────────────────────┘
                   │ POST /assemble-video
┌──────────────────▼──────────────────────────────────┐
│  YouTube Worker (worker/ — port 5000)               │
│  ├─ Pexels fetch     stock video per scene          │
│  ├─ Piper TTS        narration → WAV (900s timeout)  │
│  ├─ Whisper          WAV → SRT subtitles            │
│  └─ FFmpeg           assemble final .mp4            │
└─────────────────────────────────────────────────────┘
```
