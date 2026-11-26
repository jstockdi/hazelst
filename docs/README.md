# Hazel St Forecast — Documentation

A daily ski forecast for the Ikon Pass mountains in Vermont, New Hampshire, and
Maine (plus Killington): a web app on frank that ingests free weather data,
writes the report with Claude, checks it with code, and learns from every
verified day.

**Start here:** [design/technical-design.md](design/technical-design.md).

## Layout

| Folder | For | Documents |
|--------|-----|-----------|
| `product/` | What it is and what it shows | [mountains.md](product/mountains.md) · [report-page.md](product/report-page.md) · [mockups/](product/mockups/index.html) (C is selected) |
| `design/` | How it is built | [technical-design.md](design/technical-design.md) · [data-sources.md](design/data-sources.md) · [forecast-method.md](design/forecast-method.md) · [narrative.md](design/narrative.md) · [agent-runs.md](design/agent-runs.md) · [data-model.md](design/data-model.md) · [decisions.md](design/decisions.md) |
| `operations/` | How it runs | [hosting-frank.md](operations/hosting-frank.md) |
| `experiments/` | Dated runs and what they changed | [2026-11-25-sample-run.md](experiments/2026-11-25-sample-run.md) |

## Conventions

- **Forecast** = snow and weather expected from 6 AM on the report day; the
  1d / 3d / 5d windows are always forecasts. **Accumulation** = snow that has
  already fallen. Every number names its elevation (summit by default).
- Claude roles and jobs are named `<noun>-<verb>`.
- A change to behavior updates the spec in the same PR and, if it reverses a
  choice, adds a row to [decisions.md](design/decisions.md).
- Experiments are dated and never edited after the fact; follow-up changes go
  into the specs.
