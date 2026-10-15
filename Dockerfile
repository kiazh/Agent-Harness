# syntax=docker/dockerfile:1

# Base image pinned by tag@digest (R4-095); Dependabot's docker ecosystem
# (.github/dependabot.yml) opens the digest-bump PRs. Do not replace with a
# moving tag.

# ─── Build stage ────────────────────────────────────────────────────────────
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml README.md ./
COPY machina ./machina
COPY skills ./skills

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# ─── Runtime stage ───────────────────────────────────────────────────────────
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime

WORKDIR /app

# Create non-root user + seed the writable application home so a named
# volume mounted there (AH-AUDIT-039a) inherits correct ownership.
RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /home/appuser/.machina \
    && chown -R appuser:appuser /home/appuser

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --chown=appuser:appuser machina ./machina
COPY --chown=appuser:appuser skills ./skills
COPY --chown=appuser:appuser pyproject.toml README.md ./

# Switch to non-root user
USER appuser

# Catch missing runtime dependencies during the image build.
RUN machina --help > /dev/null && python -c "from machina.api.app import create_app; create_app()"

# Default command
ENTRYPOINT ["machina"]
CMD ["--help"]
