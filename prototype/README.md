# Prototype

Stdlib-only proof of the pipeline in `docs/`. Not the production app.

```sh
python3 prototype/fetch.py                       # live sources -> prototype/out/<date>.json
# write prototype/out/<date>.md (front matter + sections; narrative.md format)
python3 prototype/render.py prototype/out/<date>.json   # -> prototype/out/<date>.html
```

`mountains.json` is the roster; `cache/points.json` caches NWS grid lookups.
Results of the first run: `docs/experiments/2026-11-25-sample-run.md`.
