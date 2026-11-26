# Agent Runs

Everything runs inside the `hazelst` container on frank. Deterministic code does
the fetching, math, tagging, checking, verification, and rendering. Claude is
called only where judgment or language is needed: always with structured
output, always checked by code before anything is published, always logged to
`agent_call`.

Claude roles are named `<noun>-<verb>`. Code steps are named the same way so a
run reads as a sequence: `report-write` → `report-check` → `report-revise`.

## Jobs

All times America/New_York; the in-process scheduler handles DST.

### Why 7:00 AM

The schedule follows when inputs actually land (measured 2026-11-25, EST):

| Input | Morning availability | Implication |
|-------|---------------------|-------------|
| GFS 06Z on Open-Meteo | 6:17 AM | In the 6:45 ingest |
| ECMWF 06Z on Open-Meteo | 8:08 AM (00Z run, ~2 AM, is used at 6:45) | Picked up by the 8:10 ingest and 8:15 refresh |
| HRRR | hourly, ~1.5 h after init | Fresh at every ingest |
| GEM global | twice daily (00Z, 12Z) | Overnight run in the 6:45 ingest |
| NWS discussions (BTV, ALY, GYX) | morning issue 6:03–6:41 AM; overnight ~2 AM; afternoon ~2 PM | 6:45 catches the morning issue |
| Resort snow reports | usually posted 6–7:30 AM (to confirm per resort) | 6:50 catches most; 8:10 catches stragglers |

A 5 AM run would use the 7 PM model runs and the 2 AM discussions and miss the
resort reports' overnight snow. The 2:30 PM pass picks up the 12Z GFS (~12:15 PM),
12Z ECMWF (~2:10 PM), and the afternoon discussions for planning tomorrow.

**Editions.** The day has three editions of the report:

| Edition | Models | Purpose |
|---------|--------|---------|
| 7:00 AM | GFS 06Z, ECMWF 00Z, GEM 00Z, HRRR | On time for people heading out |
| 8:15 AM | GFS 06Z, **ECMWF 06Z**, GEM 00Z, HRRR | Most accurate morning numbers: first edition with a fresh run from every model |
| 2:40 PM | 12Z runs of every model, afternoon discussions | Most complete set of the day; planning tomorrow |

The 8:10 ingest checks `meta.json` for the ECMWF 06Z run. If it is not
available yet, the ingest retries every 5 minutes until 8:45 and the 8:15
refresh runs when the ingest finishes. `forecast-verify` scores each edition
separately, so the accuracy gain of each edition is measured rather than
assumed; after ~20 verified days the editions can be re-timed from that data.

Each Open-Meteo fetch also records the model's run time from
`api.open-meteo.com/data/{model}/static/meta.json`; a model whose latest run is
more than 18 h old is flagged stale in the footer and excluded from the mean. Every job
opens a `run` row, holds the job lock (one job at a time), and is idempotent
for its date.

| Job | When | Steps | Claude roles |
|-----|------|-------|--------------|
| `ingest-forecast` | 6:45 AM, 8:10 AM, 2:30 PM | Open-Meteo (summit, base), NWS grids, alerts, discussions → `fetch` → normalize | — |
| `ingest-obs` | 6:50 AM, 8:10 AM, 2:30 PM | Resort adapters, PNS, LSR, NOHRSC → `fetch` → `observation`, `snow_report` | `conditions-extract`, only when an adapter fails |
| `report-build` | 7:00 AM | derive → digest → `report-write` → `report-check` → (`report-revise` → `report-check`) → `report` | `report-write`, `report-revise` |
| `report-refresh` | 8:15 AM, 2:40 PM | re-derive from the latest ingest; update numbers, alerts, conditions; re-run `report-check` against the morning narrative | `report-revise` only if the check now fails |
| `forecast-verify` | 9:00 PM | score the 1d forecast from each edition (7:00, 8:15, 2:40) against observations → `verification` | — |
| `skill-learn` | 2:00 AM nightly | rebuild `skill` from `verification` + `forecast_hourly` | — |
| `knowledge-review` | Monday 3:00 AM | gather the week's evidence → `knowledge-propose` → proposed rows | `knowledge-propose` |
| `season-backfill` | manual | historical forecasts and reports into the same tables | — |

## Claude roles

All roles use `claude-opus-5-5` (the current default model), called through the
Claude Agent SDK (see [Call conventions](#call-conventions-all-roles)). Thinking
is adaptive on this model; depth is set with the SDK's `effort` option, so every
role sets it explicitly. Model and effort are per-role config, so a role can
move to a cheaper model (e.g. `claude-haiku-5-5` for `conditions-extract`)
without code changes — that is an owner decision, not a default.

| Role | Effort | Input | Output (Pydantic schema) | Guard |
|------|--------|-------|--------------------------|-------|
| **`report-write`** | `high` | Digest: windows, days with `precip_type` and tags, discussions (Key Messages + Discussion), alerts, accepted `knowledge`, yesterday's verification | `ReportDraft {headline, best_bet{mountain, day, why}, storm, mountains{key: text}, cautions}` | `report-check` |
| **`report-revise`** | `medium` | The draft + digest + the `report-check` error list | `ReportDraft` | `report-check` again; on a second failure publish `numbers_only` ("report delayed") and retry the build every 15 min until 9 AM |
| **`conditions-extract`** | `low` | Text of one resort report page whose adapter failed (never for Loon, Sunday River, Sugarloaf — see data-sources.md) | `ConditionsExtract {new_24h_in, base_in, trails_open, trails_total, lifts_open, confidence}` | Stored with `source=resort_llm`; never overrides an adapter value |
| **`knowledge-propose`** | `high` | `skill` changes, the week's MISSes with tags and discussions, current `knowledge` | `KnowledgeProposals {knowledge[], config_changes[]}` with evidence keys | Owner approval required; nothing proposed takes effect on its own |

### Call conventions (all roles)

Verified by the J01 spike ([spikes/agent_sdk/](../../spikes/agent_sdk/README.md)).

- SDK: `claude-agent-sdk` (Python), pinned; its wheel bundles the Claude Code
  CLI, which the SDK runs as a subprocess per call. One call is one
  `query(prompt, options)`; the app reads the final `ResultMessage`.
- Auth: `CLAUDE_CODE_OAUTH_TOKEN` (a one-year token from `claude setup-token`)
  in the environment. Usage counts against the owner's Claude subscription.
  `ANTHROPIC_API_KEY` must not be set in the container: the CLI prefers it over
  the token.
- `ClaudeAgentOptions` for every role:

  | Option | Value | Why |
  |--------|-------|-----|
  | `model` | role config (`claude-opus-5-5`) | |
  | `effort` | role config (`low` … `max`) | Per-role depth (decisions row 11) |
  | `system_prompt` | the role prompt + `SKILL.md` + accepted `knowledge`, as one string | A string replaces the Claude Code system prompt |
  | `output_format` | `{"type": "json_schema", "schema": Model.model_json_schema()}` | Structured output |
  | `tools` | `[]` | No built-in tools; only the SDK's internal `StructuredOutput` tool remains |
  | `setting_sources` | `[]` | Load no user, project, or local settings, CLAUDE.md, or hooks |
  | `skills` | `[]`; `strict_mcp_config=True` | No skills or MCP servers from the environment |
  | `permission_mode` | `"dontAsk"` | Nothing can prompt; anything not pre-approved is denied |
  | `verbatim_prompts` | `True` | Digest text (resort pages, discussions) is delivered as written: no `@path` or slash-command expansion |
  | `max_turns` | `5` | The answer arrives through a `StructuredOutput` tool turn (2 turns typical, up to 4 observed) |
  | `extra_args` | `{"no-session-persistence": None}` | No transcripts written under `HOME` |
  | `env` | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_AUTOUPDATER=1`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `CLAUDE_AGENT_SDK_CLIENT_APP=hazelst/<version>` | No telemetry, updates, or memory files |

- Output: `ResultMessage.structured_output` is validated with the role's
  Pydantic model before use. A call fails when `is_error` is true, `subtype` is
  not `success` (`error_max_turns`, `error_max_structured_output_retries`,
  `error_during_execution`, …), or validation fails. `query()` raises after an
  error result, so the client wraps the loop.
- The CLI checks the tool input against the schema and lets the model retry
  inside the same call (`MAX_STRUCTURED_OUTPUT_RETRIES`, default 5). If the
  installed SDK ever lacks `output_format`, the fallback is the schema in the
  prompt, Pydantic validation, and one retry with the validation error.
- Not exposed by the SDK, so not used: `max_tokens`, `temperature` / `top_p`,
  and the Messages API server-side refusal fallback. `fallback_model` exists
  but stays unset (one model per role; a refusal is a failed call).
- Prompt caching is automatic in the CLI (the stable system prompt is written
  to the 1-hour cache); the cache pays off between `report-write` and
  `report-revise` in the same run.
- The CLI retries 429/5xx itself (`CLAUDE_CODE_MAX_RETRIES`, default 10;
  `API_TIMEOUT_MS`, default 600000). The client sets a 10-minute wall-clock
  timeout per call; the job adds no extra retry loop beyond the 15-minute
  rebuild.
- The CLI needs a writable `HOME` for its config files (~32 KB per call). In
  the read-only container `HOME=/tmp/home` on the `/tmp` tmpfs; nothing
  persists between calls.

### `report-check` rules (code)

- Every mountain is mentioned exactly once in `mountains`.
- Every snow number in prose matches the JSON (± 0.5″ or the stated range).
- Every precip-type word (snow, ice, freezing rain, rain, mix, dry) used for a
  mountain/day matches `forecast_day.precip_type` for that day.
- Best bet names an open mountain (`season_open` ≤ day) and a day in the window.
- A storm type is named only if `storm_type` or a discussion names it.
- Cautions mention every active alert and every `wind_hold_likely` day.

The 2026-11-25 sample run would have failed the precip-type rule three times
([sample run](../experiments/2026-11-25-sample-run.md), finding 8) — the class
of error this catches.

## Compounding loop

```
 ingest-forecast ──▶ report-build (report-write → report-check) ──▶ report
        │                                                            │
        ▼                                                            ▼
 ingest-obs ──▶ observation ──▶ forecast-verify ──▶ verification ──▶ skill-learn ──▶ skill ──▶ corrected forecast
                                                                          │
                                                                          ▼
                                     knowledge-review (knowledge-propose) ──▶ knowledge / config_change (proposed)
                                                                                   │ owner approves
                                                                                   ▼
                                                             report-write prompt + thresholds (accepted)
```

- **Numbers improve automatically** through `skill` (bias and weights), behind
  the "beats raw mean on the last 20 verified days" gate.
- **Words and thresholds improve with approval**: `knowledge-propose` proposes,
  the owner accepts with `hazelst knowledge accept <id>` (CLI via
  `docker exec`), and the change is versioned in the DB.

## Reproducibility

Each `agent_call` stores the role, model id, effort, SDK version, the SHA of the
system prompt, the SHA and full JSON of the input, the output, token counts
(input, output, cache read, cache write), the SDK's cost estimate, the result
`subtype`, and latency. `hazelst replay <date>` regenerates a report with the
current prompt for comparison.

## Cost and limits

Calls run on the owner's Claude subscription, not a metered API key, so there is
no per-token bill and no workspace spend limit; usage counts against the
subscription's limits. A rate-limited call fails like any other failed call and
the page falls back to `numbers_only`.

The in-app limit is a **daily call cap** (`HAZELST_AGENT_DAILY_CALLS`, default
10): `agents/client.py` counts today's `agent_call` rows (America/New_York)
before each call and, at the cap, skips the call and falls back to
`numbers_only`. A normal day uses 1–2 calls (`report-write`, an occasional
`report-revise`, and a `report-revise` per refresh only when its check fails);
`knowledge-propose` runs weekly; `conditions-extract` only on adapter failure.

`ResultMessage.total_cost_usd` is the SDK's client-side estimate at list price.
It is logged and exported as a metric to track usage, never billed. The spike's
small structured-output call measured $0.005–0.034 per call at that price.

## Failure behavior

| Failure | Behavior |
|---------|----------|
| One source down | That section shows "source unavailable" in the footer; the run continues |
| Open-Meteo down | Retry 3× over 20 min; then publish yesterday's forecast marked stale |
| `report-write` / `report-revise` fail or `report-check` fails twice | `numbers_only` page; rebuild every 15 min until 9 AM |
| Container down at 7:00 | `report-build` runs on startup if today has no report |
