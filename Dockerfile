# Image for the API (optional: you can also run the API directly with uv).
#
# Build:  docker compose --profile api build
# Contains only the app and its runtime dependencies (no dev tools, no tests).

FROM python:3.12-slim

# uv installs Python dependencies quickly and exactly as pinned in uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /bin/uv

WORKDIR /srv

# Install dependencies BEFORE copying the code. Docker caches each step, so editing code
# does not trigger a full reinstall; only changing pyproject.toml/uv.lock does.
#   --frozen               fail if uv.lock is out of date instead of silently updating it
#   --no-dev               skip test tools
#   --no-install-project   dependencies only; the app code is added next
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY scripts ./scripts

# Use the virtual environment uv created, and let "import app" work from /srv.
ENV PATH="/srv/.venv/bin:$PATH" \
    PYTHONPATH=/srv \
    PYTHONUNBUFFERED=1

# Do not run as root inside the container: if the app were compromised, the attacker would
# have as few rights as possible. (docker-compose overrides this with your own user id so
# files in ./data belong to you.)
USER nobody

EXPOSE 8000
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
