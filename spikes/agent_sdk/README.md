# Spike: Claude Agent SDK under the production container constraints

J01 spike (2026-11-26). One `claude-agent-sdk` call with a Pydantic schema, no
tools, model `claude-opus-5-5`, authenticated with a `claude setup-token` token.
The result feeds [agent-runs.md](../../docs/design/agent-runs.md#call-conventions-all-roles).

## Versions

- `claude-agent-sdk==0.2.180` (released 2026-11-09; 0.2.181+ were under two
  weeks old, so the previous release is pinned), bundled Claude Code CLI 2.1.321.
- Python 3.13, `pydantic` 2.14.

## Run

Locally, with a clean `HOME` so no user settings or credentials are picked up:

```sh
cd spikes/agent_sdk
uv sync
set -a; . ~/.config/hazelst/claude.env; set +a
HOME="$(mktemp -d)" uv run python spike.py
```

In a container with the production constraints:

```sh
docker build -t hazelst-agent-sdk-spike spikes/agent_sdk
docker run --rm --read-only --tmpfs /tmp --user 65532:65532 --cap-drop ALL \
  --security-opt no-new-privileges:true --memory 512m --cpus 1 \
  -e HOME=/tmp/home --env-file ~/.config/hazelst/claude.env \
  hazelst-agent-sdk-spike
```

`SPIKE_EFFORT` (default `low`), `SPIKE_MODEL`, and `SPIKE_MAX_TURNS` override
the defaults.

## Result

Container run, `linux/arm64`, 512 MB, 1 CPU (usage fields trimmed):

```
init: {"model": "claude-opus-5-5", "tools": ["StructuredOutput"], "apiKeySource": "none", "mcp_servers": []}
result: {"subtype": "success", "is_error": false, "stop_reason": "tool_use", "num_turns": 2, "duration_ms": 2856, "total_cost_usd": 0.005436000000000001, ...}
valid: {"headline":"Sugarbush leads with 4 inches of fresh snow","best_bet":"Sugarbush","mountains":[...]}
exit=0
```

The same image built for `linux/amd64` (frank's architecture) and run under
emulation also returned `success` with schema-valid output. `SPIKE_EFFORT=high`
returned `success` as well.

## Findings

| Question | Finding |
|----------|---------|
| Structured output | Supported: `output_format={"type": "json_schema", "schema": ...}`; the answer is in `ResultMessage.structured_output`. The CLI exposes a synthetic `StructuredOutput` tool and validates its input, so the Fallback (schema in the prompt) is not needed |
| No tools | `tools=[]` leaves only `StructuredOutput` in the session's tool list |
| No user settings | `setting_sources=[]`, `skills=[]`, `strict_mcp_config=True`. The `init` message still lists the CLI's built-in skill names, but with no tools the model cannot invoke them |
| Turns | 2 turns typical (tool call, tool result); 3–4 observed occasionally. `max_turns=5` |
| Effort / thinking | `effort` accepts `low`, `medium`, `high`, `xhigh`, `max`; `thinking` is also exposed. No `max_tokens` or `temperature` option |
| Auth | `CLAUDE_CODE_OAUTH_TOKEN` in the environment; `init.apiKeySource` is `none` (subscription token, not an API key). `ANTHROPIC_API_KEY`, if set, takes precedence, so it must not be set |
| Usage and cost | `ResultMessage.usage` (input, output, cache read/write tokens), `model_usage` per model with `costUSD`, and `total_cost_usd` — a client-side list-price estimate |
| Read-only rootfs | Works with `--read-only --tmpfs /tmp` and `HOME=/tmp/home` (no need to pre-create it). The CLI wrote ~32 KB under `HOME` (`.claude.json`, policy and remote-settings caches); with `--no-session-persistence` it writes no transcripts. It also completed with an unwritable `HOME=/`, but the spec keeps a writable `HOME` so CLI state writes do not fail |
| Image size | The SDK wheel bundles the CLI binary (~320 MB); the spike image is ~525 MB |
| Token handling | `--env-file` with the `CLAUDE_CODE_OAUTH_TOKEN=…` file works as is; nothing printed the token |
