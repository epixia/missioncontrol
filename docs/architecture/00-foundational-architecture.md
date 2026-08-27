# Mission Control — Foundational Architecture (v0.1)

Status: draft, first architecture deliverable
Date: 2026-08-26

## 1. Purpose

Mission Control is a control plane that supervises AI-agent runtimes (Hermes Agent,
OpenClaw, Claude Code, Codex) and a local/cloud model-serving layer (MLX-LM on Mac
Studio, plus cloud APIs) through stable, owned interfaces. It does not reimplement
agent loops, tool systems, or model servers — it orchestrates them.

Guiding constraint (non-negotiable, from project instructions): **do not build a
Frankenstein**. External projects stay replaceable behind adapters. Mission Control
owns the database, mission model, policies, scheduler, Git coordination,
observability, approvals, and orchestration logic.

```
                  MISSION CONTROL
                       │
                 Stable Interfaces
                       │
       ┌───────────────┼────────────────┐
       │               │                │
   Hermes Adapter  OpenClaw Adapter  Coding Adapter
       │               │                │
     Hermes          OpenClaw      Claude Code / Codex

                       │
                  Model Gateway
                       │
             ┌─────────┴─────────┐
             │                   │
            MLX-LM             Cloud APIs
             │
       Qwen (quantized)
```

## 2. Core Owned Subsystems

These are never delegated to an external project, regardless of how good that
project's implementation looks in research:

- **Mission model** — missions/tasks/issues, decomposition, dependency graph
- **Policies** — approval policy, budget policy, sandbox/permission policy
- **Scheduler** — cron-like and event-driven wake-ups across all runtimes
- **Git coordination** — worktree/branch allocation, "no runtime pushes to
  remote directly" invariant
- **Observability** — activity log, heartbeats (scheduled wake cycles, not
  liveness pings), cost/usage events
- **Approvals/governance** — human-in-the-loop gates, independent of which
  runtime raised the request
- **Model Gateway** — the only thing agents/runtimes are allowed to call for
  inference; never MLX or a cloud SDK directly

## 3. Runtime Adapter Contract (Anti-Corruption Layer)

Reference implementations investigated:

- `paperclipai/paperclip` — `packages/adapter-utils/src/types.ts`
  (`AdapterExecutionContext`, `AdapterExecutionResult`, `AdapterRuntimeEvent`,
  `AdapterRuntimeMcpAccess`) and three execution-strategy implementations:
  `packages/adapter-utils/src/{command-managed-runtime,remote-managed-runtime,
  sandbox-managed-runtime}.ts`.

Decision: Mission Control will define its own `RuntimeAdapter` interface,
**modeled on** Paperclip's contract (it is more battle-tested than a from-scratch
sketch — it already carries usage/billing fields, structured error families, and
event/approval callbacks) but not vendored, since Mission Control's mission/policy
model differs from Paperclip's issue-centric one.

Reason: keeps the interface shape proven-in-production while keeping ownership of
the concepts (missions, budgets, approvals) inside Mission Control rather than
inheriting Paperclip's schema wholesale.

```python
class RuntimeAdapter:
    async def install(self, spec: RuntimeSpec) -> None: ...
    async def configure(self, config: RuntimeConfig) -> None: ...
    async def deploy(self, workspace: Workspace) -> DeployResult: ...
    async def start(self, session: SessionRequest) -> SessionHandle: ...
    async def stop(self, handle: SessionHandle) -> None: ...
    async def restart(self, handle: SessionHandle) -> SessionHandle: ...
    async def health(self, handle: SessionHandle) -> HealthReport: ...
    async def send_task(self, handle: SessionHandle, task: Task) -> TaskAck: ...
    async def stream_events(self, handle: SessionHandle) -> AsyncIterator[RuntimeEvent]: ...
    async def get_logs(self, handle: SessionHandle) -> LogStream: ...
    async def destroy(self, handle: SessionHandle) -> None: ...
```

`RuntimeEvent` and `TaskAck` should carry, at minimum, the fields Paperclip's
`AdapterExecutionResult`/`AdapterRuntimeEvent` proved necessary in production:
usage/cost accounting, a structured error family (not a bare string), and an
approval/question callback hook — because governance and budgets are cross-cutting
concerns every adapter must surface, not something each adapter reinvents.

Runtime-specific code lives only under:

```
runtime_adapters/
  hermes/
  openclaw/
  claude_code/
  codex/
```

## 4. Runtime Adapters

### 4.1 Hermes Adapter

Reference implementations investigated:

- `NousResearch/hermes-agent` — CLI (`hermes_cli/main.py`,
  `hermes_cli/subcommands/`), config (`hermes_cli/config.py`,
  `~/.hermes/config.yaml`), MCP server (`mcp_serve.py`,
  `agent/transports/hermes_tools_mcp_server.py`), approvals
  (`hermes_cli/approval_mode.py`, `approval_transport.py`), container isolation
  (`docker/`, `nix/sandbox.nix`), cron (`cron/scheduler.py`), subagents
  (`agent/subagent_lifecycle.py`).

Control surface: documented CLI + YAML config file (mutated via
`hermes config set <section.key> <value>`).

**Correction, post-implementation:** the original plan here was to drive task
execution through `mcp_serve.py` as an MCP client. That was revised after
investigating `NousResearch/hermes-paperclip-adapter` (MIT, npm
`hermes-paperclip-adapter`, commit `937ea71a34f5efcaa3834b11fdd08cfc1c99cb2c`)
— a real, actively-used Hermes adapter for Paperclip. It does **not** use MCP
at all: it spawns `hermes chat -q "<prompt>" -Q --source tool` (quiet,
single-query mode) as a child process and parses stdout for a `session_id:`
line, token usage, and a cost figure — the same subprocess-and-parse shape
already used for the Claude Code and Codex adapters below.
`HermesRuntimeAdapter.send_task`/`stream_events`
(`src/mission_control/adapters/hermes/adapter.py`) follows this CLI-subprocess
design, not an MCP client.

One deliberate deviation from that reference: `hermes-paperclip-adapter`
always passes `--yolo` to bypass Hermes's dangerous-command approval prompts
("agents have no TTY"). Mission Control does not inherit that default —
approval bypass is gated behind an explicit
`RuntimeConfig.extra["bypass_approvals"]` opt-in, never implicit, consistent
with this project owning governance itself (§2).

Decision:

- Treat the `hermes` CLI + config file as `configure`/`start`/`stop`.
- Treat `hermes chat -q ... -Q --resume <id> --source tool` (subprocess +
  stdout parsing) as the `send_task`/`stream_events` transport — corrected
  from the MCP-based plan originally sketched here.
- Wrap Hermes's approval transport with our own governance model rather than
  adopting its UI (`approval_transport.py` → adapter-level translation), and
  never default to `--yolo`.
- Reuse the *idea* of container-per-runtime state isolation (`docker/`,
  `nix/sandbox.nix`), but Mission Control owns its own state-directory layout —
  do not depend on Hermes's Docker layout directly.
- Build our own cron/scheduler; Hermes's `cron/scheduler.py` is agent-scoped and
  Mission Control's scheduler must be cross-runtime.
- Out of scope for v0.1: Hermes's messaging-platform gateway
  (`gateway/platforms/` — Telegram/Discord/Slack/WhatsApp/Signal) and its skill
  self-improvement loop — orchestration-level, not something Mission Control's
  control plane needs to reproduce.
- Also present but out of scope: `apps/desktop/` (Electron app) and
  `tui_gateway/` — Mission Control adapts only the headless CLI/MCP surface.

Reason: Hermes exposes a genuinely documented control surface (CLI, YAML config,
MCP server), so "control through supported interfaces" (per project instructions)
is achievable without touching internals.

License: MIT — no restriction on referencing or vendoring small pieces with
attribution.

### 4.2 OpenClaw Adapter

Reference implementations investigated:

- `openclaw/openclaw` — Gateway (`src/gateway/`), ACP implementation
  (`src/acp/`), ACP packages (`packages/acp-core`, `packages/gateway-client`,
  `packages/gateway-protocol`), multi-gateway isolation
  (`docs/gateway/multiple-gateways.md`), remote gateway
  (`docs/gateway/remote-gateway-readme.md`, `docs/gateway/remote.md`),
  health/status (`docs/gateway/health.md`), ACP bridge (`docs/cli/acp.md`),
  ACP harness runner (`docs/tools/acp-agents.md`, `extensions/acpx/`), auth
  (`docs/gateway/authentication.md`, `docs/gateway/operator-scopes.md`).

Confirmed capability: OpenClaw supports independently isolated Gateway instances
via `OPENCLAW_CONFIG_PATH`, `OPENCLAW_STATE_DIR`, `agents.defaults.workspace`,
and `gateway.port` (selectable via `--profile <name>`), with startup-time
enforcement of unique state-dir ownership.

Confirmed capability: ACP support is bidirectional —
1. `openclaw acp` — OpenClaw as an ACP **server** (IDEs/Codex/Claude Code connect
   to a Gateway session over stdio→WebSocket).
2. `/acp spawn` + the `acpx` plugin — OpenClaw as an ACP **client**, launching
   Claude Code, Codex, Gemini CLI, Cursor as external harnesses.

Decision:

- Reuse OpenClaw's env-var isolation model (config/state/workspace/port) as the
  pattern for Mission Control's own per-runtime state-directory scheme — it maps
  cleanly onto the same problem (this is a "reuse the idea" case, not
  "integrate directly", since Mission Control's isolation directory must span
  all four runtime types, not just OpenClaw).
- Integrate directly against `openclaw acp` as the transport when Mission
  Control needs to route a coding session through OpenClaw's Gateway.
- Integrate directly against `openclaw health --json` / `openclaw status --deep`
  for the adapter's `health()` method — documented, stable CLI output.
- Wrap the `acpx` harness runner with our adapter, calling through the
  documented CLI, not OpenClaw's internal ACP translator classes.

License: MIT (OpenClaw Foundation, 2026) per the `LICENSE` file. Note: GitHub's
license-detector API reports `"other"/NOASSERTION` for this repo despite the
MIT `LICENSE` file — verify the file directly before relying on the GitHub
badge; third-party notices are tracked separately in
`THIRD_PARTY_NOTICES.md`.

### 4.3 Claude Code Adapter

Reference implementations investigated:

- `anthropics/claude-code` — the GitHub repo contains no CLI source (only
  `.github/`, `.claude/`, `.devcontainer/`, `examples/`, `plugins/`, `scripts/`,
  `CHANGELOG.md`). The actual binary ships via npm as
  `@anthropic-ai/claude-code`. License is proprietary
  (`LICENSE.md`: Anthropic Commercial Terms of Service) — there is no source to
  vendor or fork; only the public CLI/SDK surface is usable.

Documented, stable control surface:

- Non-interactive invocation: `claude -p "<prompt>"`, `--bare` for a hermetic
  run (skips hooks/MCP/skills/CLAUDE.md — recommended for scripted use).
- Output: `--output-format {text|json|stream-json}` (+
  `--verbose --include-partial-messages` for token streaming),
  `--json-schema` for structured output.
- Session lifecycle: `--continue`, `--resume <id>`, `session_id` in JSON output;
  SIGINT ends the turn cleanly, SIGTERM exits 143 leaving the turn resumable.
- Permissions: `--allowedTools`, `--permission-mode {auto|dontAsk|acceptEdits}`,
  `--settings`/`--mcp-config`/`--agents` overrides.
- Hooks (`.claude/settings.json`): `PreToolUse`, `PostToolUse`,
  `SessionStart/End`, `Stop`, `SubagentStart/Stop`, with `command`/`http`/
  `mcp_tool`/`prompt`/`agent` hook types and
  `permissionDecision: allow|deny|escalate` — the natural hook point for
  Mission Control's approval/governance layer.
- Full programmatic control: the **Agent SDK** (Python/TypeScript), same agent
  loop as the CLI — prefer this over shelling out to `-p` for anything beyond
  simple scripting.

Decision: treat the Agent SDK + `-p`/`--output-format stream-json` + hooks in
`settings.json` as the only integration surface. Never depend on repo internals
— there are none to depend on.

Risk: hook payload/event shape has drifted across npm point releases (e.g. a
`capabilities` array was added at 2.1.205; stream ordering changed between
2.1.169 and 2.1.204). Gate adapter behavior on the presence of the
`capabilities` array, not on a version-string comparison.

### 4.4 Codex Adapter

Reference implementations investigated:

- `openai/codex` — genuinely open source, Apache-2.0. Rust workspace
  (`codex-rs/`): `core`, `exec`, `mcp-server`, `sandboxing`, `linux-sandbox`,
  `windows-sandbox-rs`, `app-server-protocol`, `hooks`, `execpolicy`,
  `rollout` (session state), plus `codex-cli/` (npm wrapper) and `sdk/`.

Documented, stable control surface:

- Headless mode: `codex exec "<prompt>"` (progress → stderr, final message →
  stdout); `--json` for a JSON-Lines event stream
  (thread/turn/item/error events); `--output-schema <path>` enforces a JSON
  Schema on the final response; `-o <path>` writes the final message to file.
- Sandbox: `--sandbox {read-only|workspace-write|danger-full-access}` (default
  read-only), platform-enforced via Seatbelt (macOS) / Landlock (Linux).
- Approval policy: `{untrusted|on-failure|on-request|never}`, via CLI flag or
  `config.toml`.
- Session resume: `codex exec resume --last "<instruction>"` /
  `codex exec resume <SESSION_ID>`; `--ephemeral` for no persistence.

Decision: because Codex is Apache-2.0 and ships an actual
`app-server-protocol` crate, deeper integration is licensing-safe if ever
needed — but the **stable integration surface remains `codex exec --json` /
`--output-schema` + `config.toml` sandbox/approval settings**. The internal
Rust crates move fast (alpha releases ship daily) and are not a committed API;
pin to a tagged `rust-vX.Y.Z` release, never `main`.

## 5. Model Gateway / Model Router

Reference implementations investigated:

- `ml-explore/mlx` — Apple's array/autograd framework for Apple Silicon
  (unified memory, Metal + CPU backends). No LLM-specific concepts; it is the
  tensor substrate mlx-lm is built on.
- `ml-explore/mlx-lm` — `mlx_lm/server.py`, `mlx_lm/generate.py`,
  `mlx_lm/models/` (per-architecture files, including `qwen.py`, `qwen2.py`,
  `qwen2_moe.py`, `qwen3.py`, `qwen3_moe.py`, `qwen3_vl.py`),
  `mlx_lm/models/cache.py`, `mlx_lm/utils.py`, plus `mlx_lm/SERVER.md`,
  `mlx_lm/MANAGE.md`, `mlx_lm/manage.py`, `mlx_lm/cli.py`, `mlx_lm/lora.py`.

Confirmed: `mlx_lm/server.py` exposes an OpenAI-Chat-API-compatible HTTP
surface — `POST /v1/chat/completions` (messages, stream, max_tokens,
temperature, top_p/top_k/min_p, repetition/presence/frequency penalty,
logit_bias, logprobs, stop, plus mlx-lm-specific `model`, `adapters`,
`draft_model`, `num_draft_tokens`) and `GET /v1/models`. Response shape mirrors
OpenAI (`chat.completion`/`.chunk`, `usage.*_tokens`, `system_fingerprint`).

Confirmed limitations that make an internal gateway non-optional, not a
nice-to-have:

1. **No auth mechanism in the code** — `SERVER.md` explicitly states the
   server "is not recommended for production" and "only implements basic
   security checks."
2. **Single generation worker per process** (`ResponseGenerator._generate` on
   one background thread pulling a `Queue`) with hot-swap-on-request model
   loading (`ModelProvider`) — no real multi-tenant concurrency across models
   in one process.
3. No rate limiting, retries, or circuit-breaking.

`mlx_lm/models/cache.py` implements `KVCache`, `RotatingKVCache`,
`QuantizedKVCache`, `ChunkedKVCache`, `BatchKVCache`/`BatchRotatingKVCache`, and
a server-level `LRUPromptCache` for prefix reuse across requests, plus
`save_prompt_cache`/`load_prompt_cache` for persistence — useful context for
understanding what "warm" means for a given model process, even though Mission
Control never touches this layer directly.

Decision: Mission Control's Model Gateway sits in front of one-or-more
`mlx-lm` server processes (and cloud APIs) and is the **only** thing agents or
runtime adapters may call for inference.

- Because the wire format is already OpenAI-shaped, the gateway's adapter code
  to mlx-lm is thin: translate Mission Control's internal model-call contract
  to `/v1/chat/completions` and back.
- The gateway, not mlx-lm, owns: authn/authz, per-tenant/per-mission
  concurrency and queueing across one-or-more mlx-lm processes, rate limiting,
  retries, circuit-breaking, and routing between local (MLX/Qwen) and cloud
  models.
- Qwen and other models load via `mlx_lm.utils.load()` by HF repo id or local
  path; quantized weights typically come from `mlx-community/*` HF repos.
  `mlx_lm.manage` (documented in `MANAGE.md`) handles local HF-cache
  management — the gateway can shell out to this for model lifecycle, but
  should not embed mlx-lm's Python internals.

## 6. Organization / Mission / Budget / Governance Model

Reference implementation investigated:

- `paperclipai/paperclip` — Postgres/Drizzle schema under `packages/db/src/schema/`:
  org hierarchy (`companies.ts`, `company_memberships.ts`, `projects.ts`,
  `project_memberships.ts`, `agent_memberships.ts`), goals (`goals.ts`,
  `project_goals.ts`), task orchestration (`issues.ts`, `issue_relations.ts`,
  `issue_plan_decompositions.ts`, `pipelines.ts`, `pipeline_cases.ts`,
  `routines.ts`), budgets (`budget_policies.ts`, `budget_incidents.ts`,
  `cost_events.ts`, `finance_events.ts`), governance (`approvals.ts`,
  `issue_approvals.ts`, `decisions.ts`, `decision_queues.ts`,
  `principal_permission_grants.ts`), activity/heartbeats (`activity_log.ts`,
  `heartbeat_runs.ts`, `heartbeat_run_events.ts`,
  `heartbeat_run_watchdog_decisions.ts` — "heartbeat" there means a *scheduled
  agent wake cycle*, not a liveness ping), and execution-workspace isolation
  (`execution_workspaces.ts`, `execution_workspace_runtime_leases.ts`,
  `environments.ts`, `environment_leases.ts`). React dashboard mirrors this
  boundary at `ui/src/adapters/`.

Also notable: `packages/adapters/AUTHORING.md` codifies a "no-remote-git"
cross-run persistence contract enforced by CI
(`scripts/check-no-git-push.mjs`) — i.e., runtime adapters are not permitted
to push to remote git themselves; that responsibility stays with the platform.

Decision:

- **Reference for schema design, build our own tables.** Do not take a
  dependency on `packages/db` (Postgres+Drizzle, coupled to Paperclip's own
  migration/backup tooling), but the table shapes for
  org/goals/issues/budgets/approvals/heartbeats are a strong starting point
  for Mission Control's own schema.
- **Adopt the "no-remote-git" invariant** as Mission Control's own
  CI-enforced policy for the Git Coordination subsystem — runtime adapters
  never push directly; only Mission Control's Git Coordination layer does.
- **Do not integrate the app itself.** Paperclip is a complete competing
  product (server, CLI, embedded Postgres supervisor), not a library;
  pulling it in would violate the no-Frankenstein principle. Reference only.
- License: MIT (Copyright 2025 Paperclip AI) — permits direct copying of
  small pieces (e.g., the `AdapterExecutionContext`/`AdapterExecutionResult`
  type shapes in §3) with attribution retained, even though the overall
  decision is "reference, don't vendor" for the app as a whole.

Scope note: `packages/plugins`, `evals/`, and `skills-catalog/` in Paperclip
were not investigated — flag if a future architecture pass should cover them.

## 7. Reference Repository Analysis (Summary)

| Repository | What it actually is | License | Integration posture |
|---|---|---|---|
| `paperclipai/paperclip` | Full agent-orchestration control plane (TS, Postgres, React) — closer to Mission Control's shape than a loose reference | MIT | Reference architecture/schema; adopt adapter-contract shape and no-remote-git invariant; do not vendor the app |
| `NousResearch/hermes-agent` | Self-improving autonomous agent runtime (Python/TS), messaging-platform gateway, cron, container sandboxing | MIT | Integrate via CLI + YAML config + `hermes chat -q ... -Q` subprocess (not MCP — see §4.1 correction); wrap approvals; do not adopt its scheduler or messaging gateway |
| `NousResearch/hermes-paperclip-adapter` | Real, actively-used Hermes↔Paperclip adapter (MIT, npm `hermes-paperclip-adapter`) — validated our `RuntimeAdapter` design and corrected the Hermes integration transport | MIT | Reference/port small pieces (stdout-parsing regexes for session id/usage/cost) with attribution; do **not** inherit its default `--yolo` approval bypass |
| `openclaw/openclaw` | Personal AI-assistant Gateway platform with multi-instance isolation and bidirectional ACP | MIT (verify via `LICENSE` file, not GitHub's badge) | Integrate via `openclaw acp`, `openclaw health --json`/`status --deep`, `acpx`; reuse isolation-directory pattern |
| `anthropics/claude-code` | CLI/SDK product; GitHub repo has no source, only docs/examples/plugins | Proprietary (Commercial Terms of Service) | Integrate only via Agent SDK + `-p`/`stream-json` + `settings.json` hooks; gate on `capabilities`, not version |
| `openai/codex` | Genuinely open-source coding agent (Rust) | Apache-2.0 | Integrate via `codex exec --json`/`--output-schema` + `config.toml`; pin to tagged `rust-vX.Y.Z`, not `main` |
| `ml-explore/mlx` | Apple Silicon array/autograd framework | MIT | Sits underneath mlx-lm; Mission Control never touches it directly |
| `ml-explore/mlx-lm` | LLM serving/generation/quantization package on top of mlx | MIT | mlx-lm's server is a swappable backend behind Mission Control's own Model Gateway (no auth/multi-tenancy in mlx-lm itself) |

## 8. Capabilities to Reuse/Integrate vs. Build

**Reuse the idea / adapt the pattern (build our own implementation, informed by theirs):**
- Runtime adapter contract shape (Paperclip `AdapterExecutionContext`/`Result`)
- Per-runtime state/config/workspace isolation via env vars (OpenClaw)
- No-remote-git cross-run invariant, CI-enforced (Paperclip)
- Org/goal/issue/budget/approval/heartbeat schema shapes (Paperclip, referenced not vendored)

**Integrate directly against a documented interface:**
- Hermes CLI + YAML config + `hermes chat -q ... -Q` subprocess
- OpenClaw `acp`, `health --json`/`status --deep`, `acpx`
- Claude Code Agent SDK / `-p --output-format stream-json` / `settings.json` hooks
- Codex `exec --json`/`--output-schema` / `config.toml`
- mlx-lm `/v1/chat/completions` (OpenAI-compatible) as a Model Gateway backend

**Build ourselves, no external substitute:**
- Mission model, dependency graph, task decomposition
- Cross-runtime scheduler (cron + event-driven)
- Budget/approval policy engine and enforcement
- Git coordination (worktree/branch allocation, no-remote-git enforcement)
- Observability (activity log, heartbeats, cost events)
- Model Gateway (auth, multi-tenant queueing, routing, rate limiting)

## 9. External Integration Boundaries

Mission Control never imports or subclasses code from an external runtime.
Every boundary crossing goes through one of:
- a CLI invocation (subprocess, structured stdout/stderr)
- a documented HTTP/JSON or MCP interface
- a config file written/read in a documented format
- an SDK package used as a black box (Claude Agent SDK)

No adapter is permitted to reach into another project's source tree, database,
or undocumented internal modules.

## 10. Version-Pinning Strategy

Each adapter's `runtime.yaml` (or equivalent) records:

```yaml
runtime:
  type: hermes
  source:
    repository: https://github.com/NousResearch/hermes-agent
    version: "v2026.8.19"          # release name: Hermes Agent v0.20.5
    commit: "791e2ae3257e211d14ca77e654dfe10ee1976a1c"
  adapter_version: 1
  date_validated: 2026-08-26
```

Values captured during this research pass (all `date_validated: 2026-08-26`):

| Runtime | Pin to | Validated version | Validated commit |
|---|---|---|---|
| Hermes Agent | tagged release | `v2026.8.19` | `791e2ae3257e211d14ca77e654dfe10ee1976a1c` |
| OpenClaw | tagged release | `v2026.7.1-2` | `b7d9be02093061e7879bea571b5ef6fd2605c909` (main, for reference only) |
| Claude Code | npm version range | `2.1.247` | n/a (proprietary, no public source) |
| Codex | tagged release | `0.150.1` (stable) | pin to `rust-vX.Y.Z` tag, not `main` |
| mlx | pip/tag | `v0.32.2` | n/a |
| mlx-lm | pip/tag | `v0.31.3` | n/a |

**Distribution-channel discrepancy, found during adapter verification
(2026-08-27), not the initial research pass above:** the versions actually
installable via each runtime's normal package manager lag or diverge from
the GitHub release tags researched above — the pins in the table are still
the reviewed target, not what got installed for testing:

| Runtime | GitHub release tag (pinned above) | Installed via package manager | Gap |
|---|---|---|---|
| Hermes Agent | `v2026.8.19` (release name `v0.20.5`) | `hermes-agent==0.15.2` on PyPI → CLI reports `v0.19.0 (2026.7.20)` | PyPI trails GitHub releases by several versions |
| OpenClaw | `v2026.7.1-2` | `openclaw@2026.6.34` on npm (`npm view openclaw version` reports `2026.7.1-2` as latest, but `npm install -g` resolved `2026.6.34` — registry dist-tag inconsistency, not investigated further) | one release behind, plus an unexplained resolution mismatch worth re-checking before relying on `npm view` alone |
| Codex | `0.150.1` (latest stable at research time) | `codex-cli 0.122.0` (already installed on this host prior to this project) | 28 versions behind; Codex ships frequently |

None of these were bumped in `configs/runtimes/*.yaml` — per the mandatory
Detect → Review → Test → Approve → Deploy workflow, a version gap is logged
here (Detect) but not auto-applied.
| Paperclip (reference only, not a runtime dependency) | n/a | `v2026.824.1` | `9c57c0f11900faef6e5498c3b11c066539a1b6b4` |

Upgrade workflow (mandatory, no exceptions):

```
Detect → Review → Test → Approve → Deploy
```

Mission Control must never auto-upgrade a production runtime. A future
"upstream version available" indicator can compare the pinned commit/version
against the latest tagged release per repository, using the same version
strings captured above as the baseline.

## 11. Potential Upstream Compatibility Risks

- **Claude Code**: hook payload/event shape has already drifted across npm
  point releases (capabilities array added 2.1.205; stream ordering changed
  2.1.169→2.1.204). No source repo to diff against — risk must be managed by
  gating on feature-detection (`capabilities` array) rather than semver.
- **Codex**: alpha releases ship daily; internal crates (`core`, `rollout`,
  `execpolicy`) are not a committed API even though the repo is open source —
  only `exec --json`/`config.toml` should be treated as stable.
- **OpenClaw**: GitHub's automated license detector reports `NOASSERTION`
  despite a clear MIT `LICENSE` file — a process risk (compliance tooling that
  trusts the GitHub API badge could misflag this dependency), not a technical
  one. Verify the license file directly, not just the repo metadata.
- **Hermes Agent**: very high issue/PR velocity (5k+ open) suggests the CLI
  surface is more stable than the internal Python modules — reinforces the
  decision to integrate only via CLI/config/MCP, never internal imports.
- **mlx-lm**: explicitly disclaimed as not production-ready by its own
  maintainers (no auth, basic security only) — this is a design input (why
  Mission Control's gateway exists), not a risk to mitigate away; do not wait
  for upstream to add auth before shipping the gateway.
- **Paperclip**: reference-only; no runtime dependency risk, but its schema
  and adapter-contract shape may evolve weekly (frequent releases) — re-check
  before treating any specific field name as a long-term contract to mirror.

## 12. Implementation Verification (v0.1 scaffold)

The `RuntimeAdapter` contract (§3) and all four adapters were implemented
under `src/mission_control/adapters/` and verified, not just written:

- All four adapters (`HermesRuntimeAdapter`, `OpenClawRuntimeAdapter`,
  `ClaudeCodeRuntimeAdapter`, `CodexRuntimeAdapter`) instantiate and satisfy
  the `RuntimeAdapter` ABC (`tests/test_adapter_contract.py`, 12 passing
  offline tests) — a real compile-time/runtime guard against the anti-corruption
  boundary drifting.
- `ClaudeCodeRuntimeAdapter` was exercised genuinely end-to-end against the
  CLI installed on the dev host (`claude` 2.1.247) via an opt-in live smoke
  test (`tests/test_live_smoke.py`, gated behind `MC_LIVE_TESTS=1` since it
  makes real, cost-incurring API calls) — a full round trip through
  `install → deploy → start → send_task → stream_events → destroy`, with a
  successful (`is_error: false`) terminal result.
- **Correction:** `CodexRuntimeAdapter` was initially reported as
  round-tripping successfully too, based on the live smoke test asserting
  only `events` was non-empty. That assertion was wrong — it also passes on
  total failure, since `error`/`turn.failed` events are events too. Rerunning
  with real event content printed showed the account behind the installed
  `codex-cli` 0.122.0 has **no API credits**
  (`stream disconnected before completion: You have no credits remaining`),
  so no task has actually completed successfully through this adapter yet.
  The test now inspects event types and fails on `error`/`turn.failed`
  (skipping specifically for the known no-credits case, so an account
  limitation doesn't read as a hard CI failure). Codex's real `--json` event
  schema is now confirmed for the failure path
  (`thread.started{thread_id}` → `turn.started` → zero or more
  `error{message}` → `turn.failed{error.message}`); the **success-path**
  event shape (presumably `item.*`/`turn.completed`) remains unverified
  pending API credits.
- `HermesRuntimeAdapter` and `OpenClawRuntimeAdapter` could not be
  live-tested (neither CLI is installed in this environment); their
  lifecycle methods (`install`/`health`) fail cleanly with
  `ErrorFamily.NOT_FOUND` when the binary is missing, and `send_task` for
  OpenClaw is `NotImplementedError` pending a real install to verify the ACP
  bridge against.

Two real defects were found and fixed during this pass, not anticipated by
research alone:

1. **Windows npm-shim subprocess bug.** `claude`/`codex` install as `.cmd`
   shims on Windows. `asyncio.create_subprocess_exec` calls Win32
   `CreateProcess` directly, which cannot launch a script file — it fails
   with `FileNotFoundError: [WinError 2]` even though `shutil.which` finds
   the shim on PATH. Fixed by routing all adapter subprocess launches
   through `mission_control/adapters/_proc.py::create_subprocess`, which
   wraps with `cmd /c` on `win32` only. This affects any Mission Control
   deployment on Windows, not just this dev host.
2. **Claude Code's `--output-format stream-json` requires `--verbose`** when
   combined with `-p`/`--print`, or the CLI exits immediately with
   `Error: When using --print, --output-format=stream-json requires
   --verbose` — not called out in the flag research pass; only surfaced by
   actually running the command. `ClaudeCodeRuntimeAdapter.send_task` now
   always passes `--verbose`.

One schema correction from live output: Claude Code's authoritative
`total_cost_usd` appears only once, on the final `type: result` event —
per-`assistant`-event `usage` blocks carry token counts but not cost.
`ClaudeCodeRuntimeAdapter._extract_cost` was updated to read `total_cost_usd`
off that terminal event rather than assuming every event carries cost.

A third defect, found the same way, affected all three CLI-subprocess
adapters identically: **`health()` reported a session as still "running"
indefinitely after the underlying process had already exited.**
`asyncio.subprocess.Process.returncode` is only populated once the process
is reaped via `await process.wait()` — draining stdout to EOF via
`async for line in process.stdout` does not reap it. Fixed by adding
`await state.process.wait()` at the end of `_pump_events` in all three
adapters (`claude_code`, `codex`, `hermes`), immediately before the sentinel
`None` is pushed to the event queue. Confirmed fixed by re-running Hermes
against a deliberately-failing invocation (no provider credentials
configured) end-to-end: `health()` now correctly reports
`unhealthy - exited 1` instead of `healthy - task running`. This recurring
bug across three near-identical adapter implementations is itself a signal
that `mission_control/adapters/{claude_code,codex,hermes}` should eventually
be refactored onto one shared `CliSubprocessAdapter` base (lifecycle,
`_pump_events`, `_require` are ~80% identical across the three) — tracked as
an open item in §14 rather than done in this pass, to avoid scope creep on
top of the version-0.1 scaffold.

**Hermes and OpenClaw were both actually installed** to extend verification
beyond Claude Code/Codex (`uv tool install hermes-agent` → Hermes Agent
v0.19.0 (2026.7.20); `npm install -g openclaw` → OpenClaw 2026.6.34, `5c38f99`
— note this is older than the `2026.7.1-2` release tag `npm view` reports as
latest, and older than the `v2026.7.1-2` pinned in §10; re-validate before
treating either pin as current). `hermes chat --help` confirmed every flag
`HermesRuntimeAdapter` depends on (`-q`/`-Q`/`--resume`/`--source`/`--yolo`)
exists exactly as `hermes-paperclip-adapter` uses them.

**OpenClaw's `health`/`status` commands require a running, authenticated
Gateway** — `openclaw health --json` failed with "gateway health requires
credentials before opening a websocket" (exit 1, message on stderr) because
no Gateway was configured (`gateway.mode` unset, no auth token) or running.
This is not a bug in the adapter: `OpenClawRuntimeAdapter.health()` handles
it correctly today, surfacing `HealthStatus.UNHEALTHY` with the real error
text rather than crashing — confirmed live. What it does mean is that
`health()` is only meaningful once Mission Control has actually stood up a
local/remote Gateway instance (via `openclaw setup`/`configure` + `gateway
start`, generating and storing an auth token) — this was deliberately not
done in this pass, since generating credentials and starting a persistent
background service is a bigger action than installing a CLI, and was left
for when there's a concrete OpenClaw-backed mission to run. `send_task` for
OpenClaw remains `NotImplementedError` pending that setup.

## 13. v0 Platform (API + Dashboard)

The adapter layer (§3–§5, §12) has no server, database, or UI in front of
it — nothing a person could "access." A first, real slice of that now
exists under `src/mission_control/server/`:

- **`models.py`** — the first piece of the owned "mission model" (§2):
  `Mission` → `MissionTask` → `TaskEvent`, persisted via SQLModel to SQLite
  at `~/.mission-control/mission-control.db`.
- **`runtime_registry.py`** — one long-lived adapter instance per
  `RuntimeType`, loading the pinned `RuntimeSpec` from `configs/runtimes/`.
- **`app.py`** — a FastAPI app: `POST /api/missions`, `POST
  /api/missions/{id}/tasks` (kicks off `install → configure → deploy →
  start → send_task` as a background `asyncio` task), `GET
  /api/tasks/{id}/events` (Server-Sent Events, tailing `TaskEvent` rows so a
  reconnecting client replays history then continues live), `GET
  /api/health` (per-runtime binary presence).
- **`static/dashboard.html`** — a single self-contained page served at `/`:
  create a mission, submit a task against any of the four runtimes, watch
  its event stream live.

Verified live, not just written — run via `uv run python -m
mission_control.server` (binds `127.0.0.1:8420`) and driven with real
`curl`/browser requests:

- A full mission → task → `claude_code` round trip actually ran end-to-end
  through the API and streamed real Claude Code events over SSE to
  completion (`event: done` / `succeeded`).
- Two more real bugs surfaced this way, not caught by writing the code:
  1. **`create_task` was a sync route.** FastAPI runs `def` routes in a
     worker thread; `asyncio.create_task(...)` inside one throws
     `RuntimeError: no running event loop`. Fixed by making the route
     `async def` so it runs on the actual event loop.
  2. **Double-counted cost/tokens.** `ClaudeCodeRuntimeAdapter` was emitting
     a `CostEvent` for both the mid-stream `assistant` event's partial
     `message.usage` *and* the terminal `result` event's authoritative
     `usage`/`total_cost_usd` — the server summed every event's cost, so a
     single $0.0095 turn was recorded as ~$0.019 with doubled token counts.
     Fixed by only extracting cost from the terminal `type: result` event;
     `assistant`-event usage is a snapshot of the same turn, not additional
     usage.
- Job-control caveat hit while iterating: killing a backgrounded server
  with `kill %1` silently no-ops across separate tool invocations (each one
  can be a new shell, so `%1` doesn't necessarily refer to the process
  actually listening on the port) — the reliable way to restart the server
  during development was finding the real PID via
  `Get-NetTCPConnection -LocalPort <port>` and `Stop-Process -Id`.

What this v0 platform is **not**: there is no budget enforcement, no
approval gate, no auth, and no scheduler — a task runs the moment it's
submitted, with nothing checking cost limits or requiring human sign-off.
Those are the governance/budget/scheduler subsystems from §2 and remain
open work (§14).

## 14. Open Questions / Next Steps

1. Confirm whether Mission Control needs Hermes's messaging-platform gateway
   (Telegram/Discord/Slack/WhatsApp/Signal) for any near-term use case, or
   whether the headless CLI surface is sufficient indefinitely.
2. Decide whether OpenClaw's `acpx` harness runner (OpenClaw launching Claude
   Code/Codex itself) should be used instead of Mission Control's own
   `ClaudeCodeRuntimeAdapter`/`CodexRuntimeAdapter` in scenarios where OpenClaw
   is already the active runtime — avoid double-routing the same coding agent
   through two adapters.
3. ~~Design the concrete `RuntimeSpec`/`RuntimeConfig`/`SessionRequest`/
   `HealthReport` schemas referenced in §3.~~ Done —
   `src/mission_control/adapters/types.py`.
4. Design the Model Gateway's routing policy between local MLX/Qwen and cloud
   APIs (cost, latency, capability-based routing).
5. Revisit `packages/plugins`, `evals/`, and `skills-catalog/` in Paperclip if
   Mission Control later needs a plugin/skill-packaging system of its own.
6. Refactor `mission_control/adapters/{claude_code,codex,hermes}` onto a
   shared `CliSubprocessAdapter` base — flagged in §12 after the same
   `process.wait()` bug had to be fixed identically in all three files.
7. When there is a concrete OpenClaw-backed mission to run: perform
   `openclaw setup`/`configure`, generate and store a Gateway auth token, and
   start a local Gateway (`openclaw gateway start`) so
   `OpenClawRuntimeAdapter.health()`/`send_task()` can be verified live —
   deliberately deferred in this pass (see §12) since it stores credentials
   and starts a persistent background service, which the user asked to skip
   until there's a real need.
