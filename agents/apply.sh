#!/usr/bin/env bash
# Apply AGENTS.md files from this directory into the running local Paperclip container.
# Edit the AGENT_IDS map below if you re-create the local Paperclip instance.

set -euo pipefail

CONTAINER="${CONTAINER:-paperclip-with-hermes-paperclip-1}"
COMPANY_ID="${COMPANY_ID:-95728844-4a80-4dd5-99e1-9da95346b6d6}"
BASE="/paperclip/instances/default/companies/$COMPANY_ID/agents"

# filename → agent_id  (local dev instance only)
declare -a PAIRS=(
  "CEO.md|13a7652c-b913-412c-b13c-a1c57cb8ca89"
  "Researcher.md|86a9d088-0709-475f-a487-2154f9310453"
  "Scriptwriter.md|29273f17-8a38-4aae-9404-853c9fc62dd1"
  "SceneDirector.md|ed43f9b8-3db4-43bb-9611-e6feb7d6bfdc"
  "ProductionManager.md|3ea42506-bc31-49e3-8464-7136c2c16a0e"
  "Publisher.md|979f1e17-160c-4029-b34c-a9d34bb73d22"
)

HERE="$(cd "$(dirname "$0")" && pwd)"

if ! docker ps --format '{{.Names}}' | grep -q "^$CONTAINER$"; then
  echo "Container $CONTAINER is not running. Start it first: docker compose up -d" >&2
  exit 1
fi

for pair in "${PAIRS[@]}"; do
  file="${pair%|*}"
  id="${pair##*|}"
  src="$HERE/$file"
  dest="$BASE/$id/instructions/AGENTS.md"

  if [ ! -f "$src" ]; then
    echo "⚠️  skip — file missing: $src"
    continue
  fi

  docker cp "$src" "$CONTAINER:$dest"
  docker exec "$CONTAINER" chown node:node "$dest"
  echo "✓ applied $file → $id"
done

echo ""
echo "Done. The next time each agent runs a heartbeat, it'll pick up the new instructions."
