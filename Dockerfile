# syntax=docker/dockerfile:1

# ─── Build stage ────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml README.md ./
COPY ah ./ah
COPY skills ./skills

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# ─── Runtime stage ───────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --chown=appuser:appuser ah ./ah
COPY --chown=appuser:appuser skills ./skills
COPY --chown=appuser:appuser pyproject.toml README.md ./

# Switch to non-root user
USER appuser

# Catch missing runtime dependencies during the image build.
RUN ah --help > /dev/null && python -c "from ah.api.app import create_app; create_app()"

# Default command
ENTRYPOINT ["ah"]
CMD ["--help"]
