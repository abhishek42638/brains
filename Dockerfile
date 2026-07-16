# Multi-stage: build deps in one image, ship only what runs.
#
# Stage 1 resolves and installs dependencies with uv. Stage 2 copies the built
# virtualenv and the source, and carries none of the build tooling — uv, the
# lockfile machinery and the build cache never reach the runtime image.

# ---- builder ---------------------------------------------------------------
FROM python:3.12-slim AS builder

# uv from its official distroless image: pinned, no curl|sh, no apt.
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer keyed on the lockfile: source edits
# (the common case) then rebuild in seconds instead of re-resolving everything.
# --frozen fails rather than silently re-resolving if uv.lock is stale, so the
# image cannot drift from the lockfile that was reviewed.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY . .

# Install the project itself into the same venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime ---------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Non-root. A container that spends Anthropic credits and holds DB creds should
# not also be able to rewrite its own code: the app owns nothing it runs.
RUN useradd --create-home --uid 10001 brains

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# --chown on COPY, not a later chown -R: a recursive chown would duplicate every
# file into a new layer and double the image size.
COPY --from=builder --chown=brains:brains /app /app

USER brains

EXPOSE 8000

# Cloud Run injects $PORT and it is not always 8000. Default it so the same
# image runs locally with `docker run -p 8000:8000` and unmodified in Cloud Run.
ENV PORT=8000

# No .env in the image (see .dockerignore) — config arrives as real environment
# variables. config.py's load_dotenv no-ops when the file is absent, which is
# exactly this case, and never overrides a real env var.
#
# `sh -c "exec ..."` rather than bare shell form: we need the shell to expand
# $PORT, but `exec` then REPLACES the shell with uvicorn so uvicorn is PID 1 and
# receives signals directly. Without exec, sh stays PID 1, does not forward
# SIGTERM, and Cloud Run's shutdown (or `docker stop`) would kill the container
# out from under a running loop instead of letting it unwind — which is exactly
# the case decisions.process() catches to file a needs_review row. A signal that
# never arrives cannot be handled.
#
# Single worker: the loop is I/O-bound on the model, and Cloud Run scales by
# container, not by in-container workers.
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
