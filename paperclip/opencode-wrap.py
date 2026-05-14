#!/usr/bin/env python3
"""
opencode-wrap.py — shim around the `opencode` CLI for Paperclip's
`opencode_local` adapter.

WHY THIS EXISTS
---------------
OpenCode v1.14.48's `run --format json` mode is broken for non-interactive
use: it emits only a single `step_start` JSONL line to stdout and never
streams the assistant's `text` or `step_finish` events — even though the
model DID respond (the content is saved in OpenCode's session storage).

Paperclip's opencode_local adapter parses stdout JSONL looking for `text`
and `step_finish` events (see packages/adapters/opencode-local/src/server/
parse.ts). With only `step_start`, every agent run looks "successful" but
produces zero output — which sends Paperclip's continuation-recovery
watchdog into a 30s polling loop.

WHAT THIS DOES
--------------
1. Runs the real `opencode` with whatever args Paperclip passed.
2. Passes through opencode's own stdout untouched.
3. Detects whether opencode already streamed a `text` event:
   - If YES (typical for tool-using agent runs) — does nothing extra.
     OpenCode's streaming works fine in that case.
   - If NO (the pure-text-response bug) — runs `opencode export
     <sessionID>` to recover the assistant's response from session
     storage and re-emits the missing `text` / `step_finish` events as
     JSONL in the exact shape parse.ts expects (underscores, not hyphens).
4. Preserves opencode's exit code and stderr.

This way the wrapper is a no-op for healthy runs and only patches the
specific broken case — no duplicated text.

INSTALL
-------
Lives on the persistent /paperclip volume so it survives container
recreation. Point each opencode_local agent's adapter_config.command at
this file:

    adapter_config.command = "/paperclip/opencode-wrap.py"

REMOVE WHEN
-----------
OpenCode ships a fixed `run --format json`. Then set command back to
"opencode" (or drop the key) and delete this file.
"""

import json
import subprocess
import sys

OPENCODE_BIN = "/usr/local/bin/opencode"


def main() -> int:
    args = sys.argv[1:]

    # stdin may carry the prompt in some invocations — capture and forward.
    stdin_data = None
    if not sys.stdin.isatty():
        try:
            stdin_data = sys.stdin.read()
        except Exception:
            stdin_data = None

    proc = subprocess.run(
        [OPENCODE_BIN, *args],
        input=stdin_data,
        capture_output=True,
        text=True,
    )

    raw_stdout = proc.stdout or ""
    session_id = None
    opencode_streamed_text = False

    # Pass through opencode's own stdout lines. Sniff out the sessionID and
    # whether opencode already streamed a `text` event itself.
    for line in raw_stdout.splitlines():
        s = line.strip()
        if not s:
            continue
        sys.stdout.write(line + "\n")
        try:
            ev = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            continue
        sid = ev.get("sessionID")
        if isinstance(sid, str) and sid and session_id is None:
            session_id = sid
        if ev.get("type") == "text":
            part = ev.get("part") or {}
            if (part.get("text") or "").strip():
                opencode_streamed_text = True

    # Forward stderr untouched.
    if proc.stderr:
        sys.stderr.write(proc.stderr)

    # Healthy run — opencode streamed the text itself. Nothing to patch.
    if opencode_streamed_text:
        return proc.returncode

    # No session → nothing to recover. Return whatever opencode gave us.
    if not session_id:
        return proc.returncode

    # Recover the real assistant output from session storage.
    try:
        exp = subprocess.run(
            [OPENCODE_BIN, "export", session_id],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        sys.stderr.write(f"[opencode-wrap] export failed: {e}\n")
        return proc.returncode

    if exp.returncode != 0:
        sys.stderr.write(f"[opencode-wrap] export returned {exp.returncode}\n")
        return proc.returncode

    # `opencode export` prints a human header line before the JSON body.
    body = exp.stdout
    brace = body.find("{")
    if brace == -1:
        return proc.returncode
    body = body[brace:]

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError) as e:
        sys.stderr.write(f"[opencode-wrap] could not parse export JSON: {e}\n")
        return proc.returncode

    emitted_text = False
    for msg in data.get("messages", []):
        info = msg.get("info", msg)
        if info.get("role") != "assistant":
            continue

        for part in msg.get("parts", []) or []:
            ptype = part.get("type")

            if ptype == "text":
                text = (part.get("text") or "").strip()
                if text:
                    _emit({"type": "text", "sessionID": session_id,
                           "part": {"text": text}})
                    emitted_text = True

            elif ptype in ("tool", "tool-use", "tool_use"):
                # Surface tool errors so Paperclip can see failures.
                state = part.get("state") or {}
                if state.get("status") == "error":
                    _emit({"type": "tool_use", "sessionID": session_id,
                           "part": {"state": {"status": "error",
                                              "error": state.get("error", "")}}})

        # Token usage / cost lives on the assistant message `info`.
        tokens = info.get("tokens")
        if tokens:
            _emit({"type": "step_finish", "sessionID": session_id,
                   "part": {"tokens": tokens, "cost": info.get("cost", 0)}})

    if not emitted_text:
        sys.stderr.write("[opencode-wrap] warning: no assistant text recovered "
                         f"from session {session_id}\n")

    return proc.returncode


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
