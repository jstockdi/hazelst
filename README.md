# Hazel St Forecast

A daily ski forecast for the Ikon Pass mountains in Vermont, New Hampshire, and
Maine (plus Killington). One Python service ingests free weather data into
SQLite, writes the report with Claude, checks it with code, and serves it as a
mobile web page.

Specs: [docs/README.md](docs/README.md). Build plan:
[docs/design/build-plan.md](docs/design/build-plan.md).

## Develop

```sh
uv sync
uv run hazelst --help
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```
