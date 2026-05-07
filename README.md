# Paperclip + Hermes Agent (Docker)

Custom Docker image extending [Paperclip AI](https://github.com/paperclipai/paperclip) v2026.427.0 with [Hermes Agent](https://github.com/NousResearch/hermes-agent) and the [hermes-paperclip-adapter](https://github.com/NousResearch/hermes-paperclip-adapter) pre-installed.

## What's included

- **Paperclip AI v2026.427.0** — self-hosted AI orchestration platform
- **Hermes Agent v0.13.0 (2026.5.7)** — AI agent with 30+ tools, skills, persistent memory, MCP support
- **hermes-paperclip-adapter v0.3.0** — adapter to run Hermes as a managed employee in Paperclip

## Local development

```bash
cp .env.example .env
# Edit .env — set BETTER_AUTH_SECRET and at least one LLM API key

docker compose up --build
```

Paperclip will be available at `http://localhost:3100`.

## Deploy to Railway

### 1. Create a new Railway project

Go to [railway.com/new](https://railway.com/new) and create a new project from this GitHub repo.

### 2. Add a Postgres database

In the Railway project dashboard, click **+ New** → **Database** → **PostgreSQL**. Railway auto-injects `DATABASE_URL` into your service.

### 3. Set environment variables

In the Paperclip service settings, add these variables:

| Variable | Value |
|----------|-------|
| `BETTER_AUTH_SECRET` | A random string (use `openssl rand -hex 32`) |
| `PAPERCLIP_PUBLIC_URL` | `https://${{RAILWAY_PUBLIC_DOMAIN}}` |
| `PAPERCLIP_DEPLOYMENT_MODE` | `authenticated` |
| `SERVE_UI` | `true` |
| `PORT` | `3100` |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |

Add any other LLM API keys you want Hermes to use (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`).

### 4. Expose the service

Under the service's **Settings** → **Networking**, generate a public domain. Railway assigns a URL like `your-app.up.railway.app`.

### 5. Deploy

Railway auto-deploys on push. For manual deploys, click **Deploy** in the dashboard.

## Verifying Hermes inside the container

```bash
docker compose exec paperclip hermes --version
```

## Architecture

```
┌─────────────────────────────────────┐
│  Paperclip Server (:3100)           │
│  ├─ UI (served at /)                │
│  ├─ Adapter Registry                │
│  │   ├─ claude-local                │
│  │   ├─ codex-local                 │
│  │   └─ hermes_local  ← adapter    │
│  └─ Database (Postgres)             │
├─────────────────────────────────────┤
│  Hermes Agent (/opt/hermes)         │
│  ├─ 30+ native tools                │
│  ├─ 80+ skills                      │
│  └─ Session persistence             │
└─────────────────────────────────────┘
```
