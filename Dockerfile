# syntax=docker/dockerfile:1

# Base image pinned by tag@digest (R4-095); Dependabot's docker ecosystem
# (.github/dependabot.yml) opens the digest-bump PRs. Do not replace with a
# moving tag.

# ─── Build stage ────────────────────────────────────────────────────────────
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from the authoritative lockfile (F-15). `uv
# export --frozen` turns uv.lock into a pinned requirements file; pip then
# installs those exact versions. `--no-deps .` installs this checkout's code
# and console script without letting pip re-resolve (and drift from) the lock.
# Bump the uv pin together with uv.lock (revision 3 checked with 0.11.7).
COPY pyproject.toml uv.lock README.md ./
COPY machina ./machina
COPY skills ./skills

RUN pip install --no-cache-dir --upgrade pip "uv==0.11.7" \
    && uv export --frozen --no-dev --no-hashes --no-emit-project -o /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir --no-deps .

# ─── Runtime stage ───────────────────────────────────────────────────────────
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS runtime

WORKDIR /app

# Create non-root user + seed the writable application home so a named
# volume mounted there (AH-AUDIT-039a) inherits correct ownership. The
# /opt/host-machina mountpoint exists so Compose can bind the host checkout
# there for read-only inspection without creating a path in a read-only rootfs
# (F-12: deliberately outside /app, i.e. off sys.path).
RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /home/appuser/.machina /opt/host-machina \
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
