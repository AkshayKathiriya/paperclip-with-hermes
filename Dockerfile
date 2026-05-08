# syntax=docker/dockerfile:1.20
# ---------------------------------------------------------------------------
# Stage 1: Clone Paperclip at the pinned release tag
# ---------------------------------------------------------------------------
FROM node:lts-trixie-slim AS base
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
       ca-certificates gosu curl gh git wget ripgrep python3 \
  && rm -rf /var/lib/apt/lists/* \
  && corepack enable

ARG USER_UID=1000
ARG USER_GID=1000
RUN usermod -u $USER_UID --non-unique node \
  && groupmod -g $USER_GID --non-unique node \
  && usermod -g $USER_GID -d /paperclip node

# ---------------------------------------------------------------------------
# Stage 2: Install Paperclip dependencies
# ---------------------------------------------------------------------------
FROM base AS source
ARG PAPERCLIP_VERSION=v2026.427.0
WORKDIR /app
RUN git clone --depth 1 --branch "$PAPERCLIP_VERSION" \
      https://github.com/paperclipai/paperclip.git .

FROM base AS deps
WORKDIR /app
COPY --from=source /app/package.json /app/pnpm-workspace.yaml /app/pnpm-lock.yaml /app/.npmrc ./
COPY --from=source /app/cli/package.json cli/
COPY --from=source /app/server/package.json server/
COPY --from=source /app/ui/package.json ui/
COPY --from=source /app/packages/shared/package.json packages/shared/
COPY --from=source /app/packages/db/package.json packages/db/
COPY --from=source /app/packages/adapter-utils/package.json packages/adapter-utils/
COPY --from=source /app/packages/mcp-server/package.json packages/mcp-server/
COPY --from=source /app/packages/adapters/ packages/adapters/
COPY --from=source /app/packages/plugins/ packages/plugins/
COPY --from=source /app/patches/ patches/
RUN pnpm install --frozen-lockfile

# ---------------------------------------------------------------------------
# Stage 3: Build Paperclip
# ---------------------------------------------------------------------------
FROM base AS build
WORKDIR /app
COPY --from=deps /app /app
COPY --from=source /app .
RUN pnpm --filter @paperclipai/ui build \
  && pnpm --filter @paperclipai/plugin-sdk build \
  && pnpm --filter @paperclipai/server build \
  && test -f server/dist/index.js

# ---------------------------------------------------------------------------
# Stage 4: Production image with Hermes Agent
# ---------------------------------------------------------------------------
FROM base AS production
ARG USER_UID=1000
ARG USER_GID=1000
WORKDIR /app
COPY --chown=node:node --from=build /app /app

RUN npm install --global --omit=dev @anthropic-ai/claude-code@latest @openai/codex@latest opencode-ai \
  && apt-get update \
  && apt-get install -y --no-install-recommends \
       openssh-client jq python3-pip python3-venv python3-dev build-essential \
  && rm -rf /var/lib/apt/lists/* \
  && mkdir -p /paperclip \
  && chown node:node /paperclip

# Install Hermes Agent from source (not published to PyPI)
ENV HERMES_VENV=/opt/hermes
ARG HERMES_VERSION=v2026.5.7
RUN python3 -m venv "$HERMES_VENV" \
  && "$HERMES_VENV/bin/pip" install --no-cache-dir \
       "hermes-agent @ git+https://github.com/NousResearch/hermes-agent.git@${HERMES_VERSION}" \
  && ln -s "$HERMES_VENV/bin/hermes" /usr/local/bin/hermes

# Install the Paperclip adapter for Hermes into the project
RUN cd /app && pnpm add -w hermes-paperclip-adapter@0.3.0

# Hermes data directories
RUN mkdir -p /paperclip/.hermes/skills /paperclip/.hermes/sessions \
  && chown -R node:node /paperclip/.hermes

COPY --from=source /app/scripts/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV NODE_ENV=production \
  HOME=/paperclip \
  HOST=0.0.0.0 \
  PORT=3100 \
  SERVE_UI=true \
  PAPERCLIP_HOME=/paperclip \
  PAPERCLIP_INSTANCE_ID=default \
  PAPERCLIP_CONFIG=/paperclip/instances/default/config.json \
  PAPERCLIP_DEPLOYMENT_MODE=authenticated \
  PAPERCLIP_DEPLOYMENT_EXPOSURE=private \
  OPENCODE_ALLOW_ALL_MODELS=true \
  HERMES_HOME=/paperclip/.hermes

# Note: Do not use VOLUME here — Railway requires volumes to be
# configured via its dashboard (Settings → Volumes), not in the Dockerfile.
EXPOSE 3100

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["node", "--import", "./server/node_modules/tsx/dist/loader.mjs", "server/dist/index.js"]
