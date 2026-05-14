#!/usr/bin/env python3
"""
Gemini Search — MCP server

Wraps Gemini 2.0 Flash with `google_search` grounding so any Claude Code /
OpenCode agent can do a real Google search through a single MCP tool call.

Why this beats Chrome browsing for the Researcher:
- One JSON-RPC call returns a grounded answer PLUS the citation URLs
- ~300-800 tokens per call instead of 10k+ for Chrome scraping
- Built-in fact-grounding (Gemini cites the sources it used)

Tool exposed:
  web_search(query, num_results=8, language="en")
    → { answer, citations: [{title, url, snippet}], used_query }

Environment:
  GEMINI_API_KEY      — required
  GEMINI_MODEL        — optional, defaults to "gemini-2.0-flash"
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import urllib.request
import urllib.error

# We avoid the heavyweight `mcp` SDK and speak JSON-RPC 2.0 directly over stdio.
# The protocol is tiny — initialize, tools/list, tools/call — and writing it by
# hand keeps the container slim and the failure modes obvious.

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_URL     = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

SERVER_NAME    = "gemini-search"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"


# ── tool definition ──────────────────────────────────────────────────────────

TOOL_SCHEMA = {
    "name": "web_search",
    "description": (
        "Search the live web using Gemini's google_search grounding. "
        "Returns a grounded answer to the question plus the list of source "
        "URLs Gemini used. Prefer this over scraping pages — it's cheaper "
        "and more reliable than visiting URLs one at a time."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for. Phrase it as a question or as keywords.",
            },
            "max_citations": {
                "type": "integer",
                "description": "Maximum number of citations to return (default 8, max 20).",
                "default": 8,
            },
        },
        "required": ["query"],
    },
}


# ── Gemini call ──────────────────────────────────────────────────────────────

def gemini_web_search(query: str, max_citations: int = 8) -> dict:
    """
    POST to Gemini with google_search grounding tool enabled.
    Returns {answer, citations: [{title, url}], used_query}.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set in the environment")

    payload = {
        "contents": [{"role": "user", "parts": [{"text": query}]}],
        "tools":    [{"google_search": {}}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1500,
        },
    }
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={GEMINI_API_KEY}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Gemini HTTP {e.code}: {msg[:300]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Gemini network error: {e}") from None

    data = json.loads(body)
    candidates = data.get("candidates") or []
    if not candidates:
        return {"answer": "", "citations": [], "used_query": query, "raw_finish_reason": "no_candidates"}

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    answer = "\n".join((p.get("text") or "").strip() for p in parts if p.get("text")).strip()

    citations: list[dict] = []
    grounding = candidate.get("groundingMetadata") or {}
    chunks = grounding.get("groundingChunks") or []
    for c in chunks[:max(1, min(max_citations, 20))]:
        web = (c.get("web") or {})
        citations.append({
            "title": web.get("title") or "",
            "url":   web.get("uri")   or "",
        })

    return {
        "answer": answer,
        "citations": citations,
        "used_query": query,
        "raw_finish_reason": candidate.get("finishReason"),
    }


# ── JSON-RPC dispatch ────────────────────────────────────────────────────────

def reply(id_: Any, result: Any | None = None, error: dict | None = None) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


def handle(message: dict) -> dict | None:
    method = message.get("method")
    id_    = message.get("id")
    params = message.get("params") or {}

    # Notifications (no id) don't need a reply
    if id_ is None:
        return None

    if method == "initialize":
        return reply(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities":    {"tools": {}},
            "serverInfo":      {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "tools/list":
        return reply(id_, {"tools": [TOOL_SCHEMA]})

    if method == "tools/call":
        name = params.get("name")
        if name != "web_search":
            return reply(id_, error={"code": -32601, "message": f"Unknown tool: {name}"})
        args = params.get("arguments") or {}
        query        = (args.get("query") or "").strip()
        max_citations = int(args.get("max_citations") or 8)
        if not query:
            return reply(id_, error={"code": -32602, "message": "Missing 'query' argument"})
        try:
            result = gemini_web_search(query, max_citations=max_citations)
            content = [{
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, indent=2),
            }]
            return reply(id_, {"content": content, "isError": False})
        except Exception as e:
            return reply(id_, {
                "content": [{"type": "text", "text": f"Search failed: {e}"}],
                "isError": True,
            })

    return reply(id_, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        # Some hosts send arrays of batched requests
        if isinstance(msg, list):
            responses = [r for r in (handle(m) for m in msg) if r is not None]
            if responses:
                sys.stdout.write(json.dumps(responses) + "\n")
                sys.stdout.flush()
            continue

        response = handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
