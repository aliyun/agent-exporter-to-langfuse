# Langfuse Observability Hook for Pi Coding Agent

Trace every Pi agent run to [Langfuse](https://langfuse.com) — turns, generations, tool calls, TTFT, token usage and cost — with zero code changes.

Unlike a direct-to-Langfuse extension, this hook builds one OTLP JSON trace per Pi agent run and hands it to the shared three-tier delivery of `agent-exporter-to-langfuse`: local exporter buffer → Langfuse OTel endpoint → failed log. Traces survive a Langfuse outage.

## Install

```bash
git clone https://github.com/aliyun/agent-exporter-to-langfuse.git
cd agent-exporter-to-langfuse/hooks/pi
bash install.sh
```

The install script:

1. Collects Langfuse credentials (Base URL, Public Key, Secret Key) interactively, or accepts them as flags.
2. Warns when the npm `pi-langfuse` extension is already registered (see [Mutual exclusion](#mutual-exclusion-with-npm-pi-langfuse)).
3. Builds the bundle if `dist/index.mjs` is missing, then copies it plus a `package.json` declaring `pi.extensions` to `~/.pi/hooks/langfuse/`.
4. Registers that directory with Pi via `pi install <dir>` (recorded in the `packages` array of `~/.pi/agent/settings.json`).
5. Writes `~/.agent-exporter-to-langfuse/config/pi.env`.

Both scripts are idempotent: re-running `install.sh` refreshes credentials and the bundle without duplicating the `packages` entry.

Non-interactive usage:

```bash
bash install.sh \
  --base-url https://us.cloud.langfuse.com \
  --public-key pk-lf-xxx \
  --secret-key sk-lf-xxx \
  --tags team:olap \
  -y
```

`--upgrade` reuses the existing `pi.env` without prompting (used by the repo-wide installer).

Restart Pi after installing.

## Uninstall

```bash
bash uninstall.sh          # unregister + remove ~/.pi/hooks/langfuse, keep pi.env
bash uninstall.sh --purge  # also delete pi.env
```

`pi.env` is preserved by default so a reinstall can reuse the credentials.

## Configuration

Runtime configuration comes from a single source: `~/.agent-exporter-to-langfuse/config/pi.env`
(each line `export KEY="value"`, written by `install.sh`). Variables already present in the
environment always win; a missing file makes the hook degrade silently.

| Variable | Required | Description |
|----------|----------|-------------|
| `LANGFUSE_BASE_URL` | Yes | Langfuse host URL. Used by delivery Tier 2 and by the score channel. |
| `LANGFUSE_PUBLIC_KEY` | Yes | Langfuse public key. |
| `LANGFUSE_SECRET_KEY` | Yes | Langfuse secret key. |
| `LANGFUSE_USER_ID` | No | `user.id` on the trace. Defaults to the OS username. |
| `LANGFUSE_TAGS` | No | Comma-separated trace tags. The fixed tag `pi` is always included. |
| `LANGSTASH_ENABLED` | No | `true` to send to the local exporter first (Tier 1). Set by `install.sh`. |
| `LANGSTASH_URL` | No | Local exporter URL. Default `http://127.0.0.1:5288`. |
| `PI_LANGFUSE_MAX_STRING_LENGTH` | No | Max characters per captured string. Default 12000. |
| `PI_LANGFUSE_MAX_TOOL_PAYLOAD_LENGTH` | No | Max characters per tool input/output. Default 24000. |
| `PI_LANGFUSE_MAX_DEPTH` | No | Max nesting depth walked when shaping a payload. Default 6. |
| `PI_LANGFUSE_MAX_ARRAY_ITEMS` | No | Max array elements kept. Default 50. |
| `PI_LANGFUSE_MAX_OBJECT_KEYS` | No | Max object keys kept. Default 80. |
| `PI_LANGFUSE_MAX_PAYLOAD_NODES` | No | Max nodes per payload before bailing out. Default 2000. |

Set any `PI_LANGFUSE_MAX_*` variable to `0`, `off` or `unlimited` to remove that limit.

The hook registers no slash commands and never writes a config file — there is no second
configuration source.

## Trace model

- One Pi agent run → one trace named `pi-agent`; the root span carries the prompt input and the final assistant output.
- Each turn → a child span of the root.
- Each provider request → a `generation` span under its turn, with TTFT (`completion_start_time`), model, usage and cost.
- Each tool call → a `tool` span under its turn, correlated by `toolCallId`, marked `ERROR` with duration on failure.
- `session_compact` → a marker span.
- The trace is built and delivered once, at `agent_end`.

### Interruption and crash recovery

- On `session_shutdown` with an unfinished run, dangling observations are closed as `WARNING`/cancelled, the root is marked `completed: false, cancelled: true`, and the partial trace is delivered immediately.
- After every `turn_end` the run is checkpointed to `~/.agent-exporter-to-langfuse/data/pi-checkpoints/<sessionId>.json`.
- If Pi is killed, the next Pi start re-delivers the leftover checkpoint as a cancelled partial trace and deletes it. At most the last unfinished turn is lost, never the whole trace.

Checkpoints live outside `~/.pi/`, so `uninstall.sh` never deletes pending data.

## Scores are best-effort

Langfuse Scores are not an OpenTelemetry span concept, so they cannot travel through the
buffered OTLP delivery chain. The hook sends them **directly** to
`{LANGFUSE_BASE_URL}/api/public/ingestion` after the trace has been handed off:

- trace-level: `tool_call_count`, `turn_count`, `total_tool_errors`, `tool_success_rate` (NUMERIC) and `session_had_errors` (BOOLEAN)
- observation-level: `tool_is_error` (BOOLEAN) per failed tool call

This channel is best-effort: on failure the scores are logged and dropped — no retry, no
buffering. Score delivery never blocks or changes the trace delivery result.

**Degradation semantics:** if Langfuse is unreachable when the run ends, the trace still
arrives later through the buffered chain but the Scores for that run are lost. The same
aggregate values are always mirrored into the root observation's `metadata`, so they remain
visible on the trace even when the Scores panel has no entry.

## Mutual exclusion with npm `pi-langfuse`

The standalone [`pi-langfuse`](https://www.npmjs.com/package/pi-langfuse) npm extension traces
the same Pi events directly to Langfuse. Running both produces **two traces per run**. Pick one:

```bash
pi remove npm:pi-langfuse   # keep this hook
```

`install.sh` detects a registered `pi-langfuse` entry and asks for confirmation (interactive) or
warns and continues (`-y` / `--upgrade`).

## Requirements

- [Pi Coding Agent](https://github.com/earendil-works/pi) with `pi` on `PATH`
- Node.js ≥ 22 (Pi loads the bundle directly)
- `python3` (used by the install scripts to inspect `~/.pi/agent/settings.json`)

## Development

```bash
npm install
npx vitest run       # unit tests
npm run build        # produce dist/index.mjs
npx tsc --noEmit     # type check
```

The install E2E test lives at `tests/e2e/test_pi_hook_install.sh` in the repository root and
runs against a sandbox `HOME` with a stubbed `pi` CLI.
