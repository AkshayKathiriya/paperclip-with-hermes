# ── Base image ────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System dependencies ───────────────────────────────────────────────────────
# ffmpeg      — video/audio assembly + concat + libass subtitle burn
# libsndfile  — audio file support for Whisper
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsndfile1 \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Upgrade pip and install setuptools (needed for openai-whisper) ───────────
RUN pip install --upgrade pip setuptools wheel

# ── Python dependencies ───────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy application code ─────────────────────────────────────────────────────
COPY . .

# ── Create directories that must exist at runtime ─────────────────────────────
# /paperclip is the Railway persistent volume mount point
# these subdirs are created at runtime by the app but we pre-create for safety
RUN mkdir -p /data/videos

# ── Environment defaults (overridden by Railway Variables) ────────────────────
ENV PORT=5000
ENV PYTHONUNBUFFERED=1
ENV OUTPUT_DIR=/data/videos
ENV WHISPER_MODEL=base

# ── Expose port ───────────────────────────────────────────────────────────────
EXPOSE 5000

# ── Start with gunicorn (production WSGI server) ──────────────────────────────
# 1 worker — video assembly is CPU-bound and memory-heavy, parallelism not needed
# timeout 1200 — video jobs can take up to 15 min (Whisper + FFmpeg passes)
CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:5000", "--workers", "1", "--timeout", "1200", "--log-level", "info"]