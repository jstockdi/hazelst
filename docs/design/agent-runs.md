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
more than 18 h old is flagged stale in the JSON (`models[].stale`) and excluded from the mean. Every job
opens a `run` row, holds the job lock (one job at a time), and is idempotent
for its date.

**Scheduler** (`scheduler.py`; on when `HAZELST_SCHEDULER=true`). Each slot
runs its steps in order in one thread; a step waits up to 20 minutes for the
job lock, and a failed step does not stop the next one.

| Slot (local) | Steps |
|--------------|-------|
| 6:45 AM | `ingest-forecast` |
| 6:50 AM | `ingest-obs` |
| 7:00 AM | `report-build` (edition 0700) |
| 8:10 AM | `ingest-forecast` (polls ECMWF `meta.json` every 5 min until the 06Z run or 8:45), `ingest-obs`, `report-refresh` (edition 0815, not before 8:15) |
| 2:30 PM | `ingest-forecast`, `ingest-obs` |
| 2:40 PM | `report-refresh` (edition 1440) |
| 9:00 PM | `forecast-verify` |
| 2:00 AM | `skill-learn` (on the day DST starts, 2:00 AM does not exist and the slot runs at 3:00 AM EDT) |
| Monday 3:00 AM | `knowledge-review` (a slot's optional `day_of_week`; DST changes fall on Sundays, so the slot always exists) |
| Sunday 4:00 AM | `backup` (weekly; DST changes at 2:00 AM, so 4:00 AM always exists) |

- **Editions**: each report version records its edition in `payload.edition`.
  A scheduled report job for an edition already published that day is skipped
  (idempotent); `hazelst run <job> --now` always publishes a new version. Without
  `--edition`, `report-build` publishes 0700 and `report-refresh` the next
  unpublished of 0815 and 1440.
- **ECMWF gate**: an 0815 edition built on an ECMWF run older than that day's
  06Z says so in the JSON `notes` ("ECMWF 06Z was not available by 8:45 AM;
  this edition uses the 00Z run.").
- **Startup catch-up**: when the container starts, the latest edition due
  today (by its publish time) is run if it has no report: ingest-forecast,
  ingest-obs, then the report job.
- A refresh carries the day's newest checked draft forward: `report-check`
  re-runs against it on the new numbers, and `report-revise` fixes it if it
  now fails. With no checked draft today, the refresh writes one
  (`report-write`). Every published edition has a checked narrative (J33,
  decisions row 33).
- `hazelst schedule` prints each slot's next run.

| Job | When | Steps | Claude roles |
|-----|------|-------|--------------|
| `ingest-forecast` | 6:45 AM, 8:10 AM, 2:30 PM | Open-Meteo (summit, base), NWS grids, alerts, discussions → `fetch` → normalize (`forecast_hourly` from 6 AM local on the ingest date) | — |
| `ingest-obs` | 6:50 AM, 8:10 AM, 2:30 PM | Resort adapters, PNS, LSR, NOHRSC → `fetch` → `observation`, `snow_report` | `conditions-extract`, only when an adapter fails |
| `report-build` | 7:00 AM | derive → digest → `report-write` → `report-check` → (`report-revise` → `report-check`, up to 3 rounds) → `report` | `report-write`, `report-revise` |
| `report-refresh` | 8:15 AM, 2:40 PM | re-derive from the latest ingest; update numbers, alerts, conditions; re-run `report-check` against the newest checked draft of the day (the `agent_call` output that passed) and revise it (up to 3 rounds) if it now fails; with no checked draft today, write one as `report-build` does | `report-revise` only if the check now fails; `report-write` only without a checked draft |
| `forecast-verify` | 9:00 PM | read the NOHRSC grid ending 12Z today (published ~7:30 PM), then score yesterday's 1d forecast from each edition (7:00, 8:15, 2:40) against observations → `verification`; re-score the date a week before that (NOHRSC re-read) | — |
| `skill-learn` | 2:00 AM nightly | rebuild `skill` from `verification` + `forecast_hourly`; refit `snow_ratio`; re-choose each mountain's method (`method_gate`) | — |
| `knowledge-review` | Monday 3:00 AM | gather the week's evidence (days D − 8 … D − 2, the days scored since the last review) → `knowledge-propose` → validate → proposed `knowledge` / `config_change` rows; skipped without skill cells or misses, or when agents are off | `knowledge-propose` |
| `backup` | Sunday 4:00 AM weekly | online copy of the database to `backups/hazelst-<date>.db`; keeps the newest 4, removing older ones after the new copy is complete | — |
| `season-backfill` | manual (`hazelst season-backfill --from D --to D`) | Open-Meteo Previous Runs (GFS, GEM, lead days 1–5), IEM LSR and PNS, NOHRSC 24 h grids into the same tables, one job per month; then `skill-learn` ([data-sources.md](data-sources.md#season-backfill-j22)) | — |

## Claude roles

All roles use `claude-opus-5-5` (the current default model), called through the
Claude Agent SDK (see [Call conventions](#call-conventions-all-roles)). Thinking
is adaptive on this model; depth is set with the SDK's `effort` option, so every
role sets it explicitly. Model and effort are per-role config
(`HAZELST_AGENT_MODEL_<ROLE>` / `HAZELST_AGENT_EFFORT_<ROLE>`, e.g.
`HAZELST_AGENT_EFFORT_REPORT_WRITE=max`), so a role can
move to a cheaper model (e.g. `claude-haiku-5-5` for `conditions-extract`)
without code changes — that is an owner decision, not a default.

| Role | Effort | Input | Output (Pydantic schema) | Guard |
|------|--------|-------|--------------------------|-------|
| **`report-write`** | `high` | Digest: windows, days with `precip_type` (summit), `base_precip_type`, `freezing_rain_at`, and the non-precip tags, discussions (Key Messages + Discussion), alerts, accepted `knowledge`, yesterday's verification | `ReportDraft {headline, best_bet{mountain, day, why} \| null, storm, mountains{key: text}, cautions}` | `report-check` |
| **`report-revise`** | `medium` | The draft + digest + the `report-check` errors of that draft | `ReportDraft` | `report-check` again; up to 3 rounds, each sent the exact errors of the draft before it. If the third still fails, the job fails (see Failure behavior) |
| **`conditions-extract`** | `low` | Text of one resort report page whose adapter failed (never for Loon, Sunday River, Sugarloaf — see data-sources.md) | `ConditionsExtract {new_24h_in, base_in, trails_open, trails_total, lifts_open, confidence}` | Stored with `source=resort_llm` (`reported_at` = fetch time); never overrides an adapter value. Skipped for a non-200 or near-empty page (a bot challenge); at most once per resort per day |
| **`knowledge-propose`** | `high` | `skill` cells with n ≥ 3 and each cell's values at the previous call, the week's 1d raw-mean MISSes with tags and the discussion the 7:00 report used, current `knowledge`, the threshold paths and values | `KnowledgeProposals {knowledge[], config_changes[]}` with evidence keys | Owner approval required; nothing proposed takes effect on its own. The job drops an item that cites no input key, names an unknown mountain, repeats a current statement, or (config) names a non-threshold path, keeps the value, or breaks the roster |

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
- The CLI needs a writable `HOME` for its config files (~32 KB per call). The
  client gives every call a fresh, empty directory under the temp dir (the
  `/tmp` tmpfs in the container; `HOME=/tmp/home` stays set for the process)
  as the CLI's `HOME` and working directory, and removes it afterwards, so
  nothing persists between calls and no user settings or credentials leak into
  a local run.
- The client (`agents/client.py`) refuses to start a call when
  `ANTHROPIC_API_KEY` is set or the token is missing. A call it does not start
  (agents disabled, cap reached, bad environment) writes no `agent_call` row;
  every started call writes one: `status` is `ok`, `error` (exception, timeout,
  `is_error`, or a non-`success` subtype), or `invalid` (schema validation
  failed; the validation errors go in `check_errors`, which `report-check`
  otherwise fills). Errors are scrubbed of the token before they are stored.
- `hazelst agents smoke` makes one live `conditions-extract` call on a
  synthetic page and prints its `agent_call` row; `hazelst agents calls` lists
  recent rows and today's count against the cap. CI uses `StubBackend`
  (`agents/stub.py`) and never calls Claude.

### `report-check` rules (code)

- Every mountain is mentioned exactly once in `mountains`.
- Every snow number in prose matches the JSON (± 0.5″ or the stated range).
- Every precip-type word (snow, ice, freezing rain, rain, mix, dry) used for a
  mountain/day matches that day's type at the elevation the phrase names
  (`precip_type` at the summit, `base_precip_type` at the base); freezing rain
  also matches where `freezing_rain_at` has it.
- A best bet, when there is one, names an open mountain (`season_open` ≤ day)
  and a day in the window. `null` means no day is worth chasing.
- A storm type is named only if `storm_type` or a discussion names it.
- Cautions mention every active alert and every `wind_hold_likely` day.

How `report_check.py` reads the prose (so a draft can be written to pass):

- Text is split into sentences, clauses (`;`, `:`, `—`, " – ", "while",
  "but", "then", "with"; not a "with" within three words after a mix word, so
  "snow mixed with rain" is one claim), and phrases. Inside a clause, a comma
  or "and" starts a new phrase when the part after it is about its own
  mountain or day (or says "otherwise") and makes its own claim (a precip word
  or snow amount) or says more than a bare name or day ("and Tuesday's gusts
  could hold the lifts"); a bare name or day joins its list. A leading claim
  with no mountain or day joins the phrase after it ("snow and rain mixed at
  Stratton"), except after a comma, or before a day when it names its
  elevation: then it keeps the clause's context ("1″ of wet snow up top, after
  rain today"). So "freezing rain to Killington, Pico, and Sugarloaf,
  with rain at Stratton and Loon" binds freezing rain to the first three and
  rain to the last two, and "rain today, a mix Saturday, and dry Sunday"
  binds each word to its own day.
- A phrase is about the mountains it names; one that names none is about the
  mountains named last in its sentence, else the entry's own mountain
  (`mountains[key]`, `best_bet.why`). It is about the days it names (weekday
  names, today / tonight / tomorrow, weekend, "through Monday" = each day up
  to Monday); one that names no day keeps the previous phrase's days, except
  an "otherwise" phrase ("dry otherwise"), which is about every day its
  section has not named and must hold on each of them. Phrases about no
  mountain (regional) are not precip-checked; their snow numbers must match
  some mountain.
- Mountain groups (J37) name their members: "the Vermont summits" (or
  mountains, resorts, peaks, bases) is every `VT` mountain, likewise Maine
  (`ME`) and New Hampshire (`NH`); "all eight" is every mountain; "the rest"
  is every mountain not yet named in that sentence ("the rest of the week" is
  not a group). Members are precip-checked one by one, and a snow amount said
  of a group must fit every member. The headline's "names a resort" rule
  counts names only, not groups.
- Elevation: each precip word is read at the elevation word nearest to it in
  its own list part (between commas and "and"s), so "snow up top, rain at the
  base" reads each at its own elevation: base (base, valley, lower elevations / slopes / mountain) reads
  `base_precip_type`; summit (summit, peak, up top, upper mountain, higher
  terrain) reads `precip_type`. With none, the word describes the summit,
  except freezing rain, which passes where `freezing_rain_at` lists any
  elevation ("Freezing rain Monday at Sugarloaf" on a summit-`rain` day with
  freezing-rain hours at the summit).
- Precip words: ice = freezing rain, freezing drizzle, ice, icy, glaze; snow =
  snow, snow showers, dusting, flurries, powder; mix = mix, wintry mix, sleet;
  rain = rain, drizzle, showers; dry = dry, drying. A `mix` day also accepts
  snow and rain words. Negated words ("no snow", "little snow" but not "a
  little snow"), partial-day "dry until …", are skipped.
- Snow numbers are amounts written with `″`, `"`, or "inches"; amounts
  followed by "of ice" or "of liquid" are not snow. A number passes when it is
  within 0.5″ of a day, multi-day sum, window mean, range end, model value,
  NWS value, or accumulation value for the phrase's mountains, or inside a
  window's range. A phrase opened by "with" that names its own mountains may
  also match the list just before it ("2.7″ at Killington with a 0.7–6.3″
  range and 0.1–4.5″ at Pico").
- Storm words: clipper; coastal low / storm, nor'easter; inland runner,
  cutter; and "upslope" (needs a `nw_upslope` tag or a discussion).
- A `null` `season_open` counts as open (date not known).

The 2026-11-25 sample run would have failed the precip-type rule three times
([sample run](../experiments/2026-11-25-sample-run.md), finding 8) — the class
of error this catches.

**Regression corpus.** `tests/fixtures/report/corpus/` holds every
`report-write` / `report-revise` call from production (`agent_call` input,
output, stored errors), with each digest re-derived on frank from the same
ingest run. `tests/unit/test_report_corpus.py` requires every stored output to
pass and pins the exact errors of each morning draft re-checked on later
numbers (genuine changes in the forecast). Add new calls to the corpus when a
check rejects a draft that reads correctly.

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

- **Numbers improve automatically** through `skill` (bias and weights) and
  `snow_ratio`, behind the held-out method gate (`method_gate`: at least
  0.05″ lower 1d MAE than the raw mean on the last complete season, or on
  20 live snow days; forecast-method.md, "Method choice").
- **Words and thresholds improve with approval**: `knowledge-propose` proposes,
  the owner reviews with `hazelst knowledge list` and accepts or rejects with
  `hazelst knowledge accept|reject <id>` and `hazelst config accept|reject <id>`
  (CLI via `docker exec`); each decision records `decided_by = 'owner'` and
  `decided_at`, and only `proposed` rows can be decided. Accepted statements
  join the `report-write` / `report-revise` system prompt; accepted config
  changes overlay `mountains.json` from the next run (`config.effective`).
- **How a skill cell moved** is measured against the previous
  `knowledge-propose` call: its `agent_call.input` holds that week's cells, so
  no separate snapshot table is kept.

## Reproducibility

Each `agent_call` stores the role, model id, effort, SDK version, the SHA of the
system prompt, the SHA and full JSON of the input, the output, token counts
(input, output, cache read, cache write), the SDK's cost estimate, the result
`subtype`, and latency. `hazelst replay <date> [--version N]` rebuilds the
digest from that date's stored report payload (newest version by default),
runs `report-write` → `report-check` → (`report-revise`) with the current
prompts and accepted knowledge, and prints the stored and regenerated
headline and best bet plus a diff of the body. It publishes nothing; its
calls are logged in `agent_call` with no `run_id`.

## Cost and limits

Calls run on the owner's Claude subscription, not a metered API key, so there is
no per-token bill and no workspace spend limit; usage counts against the
subscription's limits. A rate-limited call fails like any other failed call:
the report job fails and the published edition stays.

The in-app limit is a **daily call cap** (`HAZELST_AGENT_DAILY_CALLS`, default
10): `agents/client.py` counts today's `agent_call` rows (America/New_York)
before each call and, at the cap, skips the call; the report job fails. A
normal day uses 1–2 calls (`report-write`, an occasional `report-revise`, and
`report-revise` rounds in a refresh only when its check fails; at most 4 per
edition);
`knowledge-propose` runs weekly; `conditions-extract` only on adapter failure.

`ResultMessage.total_cost_usd` is the SDK's client-side estimate at list price.
It is logged and exported as a metric to track usage, never billed. The spike's
small structured-output call measured $0.005–0.034 per call at that price.

## Failure behavior

| Failure | Behavior |
|---------|----------|
| One source down | The JSON lists it in `sources_unavailable`; the run continues |
| Open-Meteo down | Retry 3× over 20 min; then publish yesterday's forecast marked stale |
| `report-write` / `report-revise` fail, or `report-check` still fails after 3 revisions | The job fails (`run.status = failed`, non-zero exit, the reason and last errors in `run.error` and the log); nothing is published and the previous edition stays. `/health` returns 503 and `hazelst_report_job_failed` is 1 until a report job succeeds; frank alerts on it (`HazelstReportFailed`). The `report-rebuild` slot (7:15, 7:30, … 9:00) retries the newest due edition when its job failed; it skips when agents are disabled or the daily call cap is reached. There is no page without prose: a failure is a bug to fix (decisions row 33) |
| Container down at 7:00 | `report-build` runs on startup if today has no report |
