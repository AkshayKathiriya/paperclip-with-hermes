# Agent instructions (AGENTS.md)

Each `.md` file in this directory is the persistent instruction bundle for one
agent in Paperclip. They define what each agent does, the constraints it
operates under, and how it hands work off to the next step in the pipeline.

## Pipeline order

```
CEO → Researcher → Scriptwriter → Scene Director → Production Manager → [Worker] → Publisher
```

## Files

| File | Agent role | Phase |
|------|-----------|-------|
| `CEO.md`               | Orchestrator — fans the request into the pipeline       | upstream |
| `Researcher.md`        | Gathers facts, citations, real subject imagery URLs     | step 1   |
| `Scriptwriter.md`      | 1000–1500 word documentary script with hook + 5 sections | step 2   |
| `SceneDirector.md`     | Per-shot plan (25–45 shots, mixed sources)              | step 3   |
| `ProductionManager.md` | SEO metadata + thumbnail brief + worker call            | step 4   |
| `Publisher.md`         | YouTube Data API upload (deferred to a later phase)     | step 5   |

## Applying changes to a running Paperclip instance

Agent IDs are stable inside a given Paperclip installation. The mapping from
filename → agent ID is below for the *local development* instance only —
production / Railway instances will have different IDs.

| File                  | Local agent ID                                |
|-----------------------|-----------------------------------------------|
| CEO.md                | `13a7652c-b913-412c-b13c-a1c57cb8ca89`         |
| Researcher.md         | `86a9d088-0709-475f-a487-2154f9310453`         |
| Scriptwriter.md       | `29273f17-8a38-4aae-9404-853c9fc62dd1`         |
| SceneDirector.md      | `ed43f9b8-3db4-43bb-9611-e6feb7d6bfdc`         |
| ProductionManager.md  | `3ea42506-bc31-49e3-8464-7136c2c16a0e`         |
| Publisher.md          | `979f1e17-160c-4029-b34c-a9d34bb73d22`         |

To re-apply these files into the running local Paperclip container after edits:

```bash
./agents/apply.sh
```

## Editing rules

When you edit a `.md` file:

1. Keep the standard sections: `Role`, `Working rules`, `Domain lenses`,
   `Output bar`, `Collaboration`, `Safety`, `Done`.
2. Each agent's instructions should be 3–6 KB. More than 8 KB usually means
   you're putting code-level detail in the wrong place.
3. Be specific about hand-offs ("link the `script` document to Production
   Manager") — vague hand-offs cause stuck pipelines.
4. The Production Manager file is special: it includes the exact request
   body the worker expects, including the rule to forward `script` and
   `visual_plan` as raw document body strings (do NOT re-parse).

## Adapter configuration gotchas

These are real failure modes we've already hit. Read before changing
`adapter_type` or `adapter_config` in the database.

### Adapter type names use **underscores**, not hyphens

Paperclip registers adapters under `snake_case` IDs:

| ✅ correct       | ❌ wrong          |
|------------------|-------------------|
| `claude_local`   | `claude-local`    |
| `opencode_local` | `opencode-local`  |
| `codex_local`    | `codex-local`     |
| `gemini_local`   | `gemini-local`    |

If you set `adapter_type = 'opencode-local'` (hyphen), Paperclip silently
falls back to the **generic process adapter**, which then fails every
heartbeat with `"Process adapter missing command"`. The `stranded_issue_recovery`
watchdog then creates a new "Recover stalled" sub-issue every ~16 seconds —
we've seen this cascade reach **177 issues in 94 seconds** before being paused.

The directory names under `packages/adapters/` use hyphens (`opencode-local/`)
which is what causes the confusion. The directory name and the registered
adapter ID are NOT the same.

### Verify adapter type when changing it

After an `UPDATE agents SET adapter_type = ...`, sanity-check by running:

```sql
SELECT name, adapter_type FROM agents;
```

If any value contains a hyphen, fix it immediately — do not unpause the
affected agent until the type is correct.

### Required adapter_config fields per adapter

| adapter_type   | required keys              | optional keys                                |
|----------------|----------------------------|----------------------------------------------|
| `claude_local` | (none — `model` defaults)  | `model`, `extraArgs`, `chrome`, `dangerouslySkipPermissions` |
| `opencode_local` | `model` (e.g. `google/gemini-2.5-flash` or `openrouter/deepseek/deepseek-v3.2`) | `command` (defaults to `opencode`) |

Provider auth is read from process env. **Variable names matter exactly:**

| Provider | Env var OpenCode actually reads |
|----------|--------------------------------|
| Anthropic | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Google Gemini | **`GOOGLE_GENERATIVE_AI_API_KEY`** (NOT `GEMINI_API_KEY`!) |
| OpenRouter | `OPENROUTER_API_KEY` |

Note: `opencode providers list` *shows* "Google [GEMINI_API_KEY]" in its UI,
but the underlying `@ai-sdk/google` package only reads
`GOOGLE_GENERATIVE_AI_API_KEY`. If you only set `GEMINI_API_KEY`, OpenCode
will spawn, run for ~2.5 seconds, then exit silently with empty stdout —
which Paperclip records as `succeeded` with `exit_code=0`, triggering the
`issue.continuation_recovery` watchdog into a polling loop every 30s. The
agent appears to "run" but produces no work.

Both `GEMINI_API_KEY` and `GOOGLE_GENERATIVE_AI_API_KEY` are set in our
docker-compose; the Gemini MCP server reads the former, OpenCode reads the
latter.

## Deferred — Phase 4 backlog

### Per-scene mood modifier for AI-image style

Today every AI-generated shot uses one fixed `STYLE_GUIDE` (see
`worker/pipeline/generate_ai_images.py`). That guarantees brand cohesion
but means an "envelope on a desk" shot and a "Hyderabad cityscape at
golden hour" shot get the same lighting prescription.

When ready, layer a per-scene **mood modifier** between the raw prompt
and the global style guide:

| Mood tag      | Hint inserted into prompt                                   |
|---------------|-------------------------------------------------------------|
| `warm_rise`   | warm golden lighting, optimistic atmosphere, bright accents |
| `tense`       | high contrast, harsh shadows, cold blue undertones          |
| `grim_fall`   | desaturated, overcast, muted browns and greys               |
| `reckoning`   | sterile institutional lighting, blue-grey, gravitas         |
| `neutral`     | balanced, no mood bias                                      |

Implementation cost (~30 min):
- Add `mood` field to Scene Director's per-scene output (AGENTS.md)
- Update `parse_visual_plan.py` to capture `mood`
- Update `_wrap_prompt()` in `generate_ai_images.py` to insert the
  matching modifier line above STYLE_GUIDE

Why deferred: we want to ship working videos first with one consistent
style. Adding per-scene mood is a polish step that only pays off once
the rest of the pipeline is reliable.

---

### Known issue: `opencode_local` adapter swallows model output

Even with the correct env vars set, the Paperclip `opencode_local` adapter
does not currently surface the assistant's response back to the issue
thread. The session completes (we've verified Gemini receives the request
and OpenCode logs `exiting loop`), but the `result_json.stdout` field on
the heartbeat run is always empty.

**Current workaround:** keep all agents on `claude_local` until this is
resolved. The architectural choice (per-agent models) is preserved — when
the adapter is fixed, only the `adapter_type` and `model` fields in the DB
need to change.
