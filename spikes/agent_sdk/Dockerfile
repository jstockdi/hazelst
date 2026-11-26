# Spike image: the Agent SDK under the production container constraints.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.13-slim
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY spike.py ./
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 HOME=/tmp/home
USER 65532:65532
ENTRYPOINT ["python", "spike.py"]
