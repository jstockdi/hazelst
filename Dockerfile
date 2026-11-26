# syntax=docker/dockerfile:1

# --- build: resolve the locked environment into /app/.venv ---------------------
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY .claude/skills/hazel-st/SKILL.md ./.claude/skills/hazel-st/SKILL.md
RUN uv sync --locked --no-dev --no-editable

# --- runtime: venv and config only; no compilers, no uv ----------------------------
FROM python:3.13-slim
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.source="https://github.com/jstockdi/hazelst" \
      org.opencontainers.image.revision="${GIT_SHA}"
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY config /app/config
RUN mkdir -p /data && chown 65532:65532 /data && chmod 0750 /data
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/New_York \
    HOME=/tmp/home \
    HAZELST_DB=/data/hazelst.db \
    HAZELST_MOUNTAINS=/app/config/mountains.json \
    HAZELST_CODE_VERSION=${GIT_SHA}
USER 65532:65532
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["hazelst", "healthcheck"]
ENTRYPOINT ["hazelst"]
CMD ["serve"]
