# H1 stable Python SDK compatibility

Milestone H1 owns only the typed package and the `openai-codex` transport
boundary. The production adapter is `codex_flow.backends.codex_sdk`; it does
not call the Codex CLI and does not speak JSON-RPC directly. Raw SDK objects
remain inside that adapter.

## Install and deterministic checks

The package pins `openai-codex==0.147.0`, which brings the matching
`openai-codex-cli-bin` runtime. On Python 3.12:

```bash
uv sync --python 3.12
uv run --python 3.12 python -m unittest discover -s tests -v
uv run --python 3.12 python scripts/validate.py
```

Repository-level setup and gates are available through `make setup`,
`make check`, `make test`, `make test-fast`, `make format`, and `make lint`.
The checked-in `.python-version` keeps the verified Python 3.12 baseline.

The adapter tests use captured SDK-shaped fakes. They cover successful start,
pre-identity transport exceptions, post-identity terminal failures, schema
violations, explicit model/reasoning/sandbox forwarding, resume identity, and
ordered lifecycle events.

## Opt-in real sentinel

The sentinel starts the bundled local runtime only after explicit opt-in. It
creates a disposable Git repository, runs two schema-bounded turns using
`Sandbox.read_only`, resumes the exact returned thread id, and stores the
observed event sequence and before/after repository hashes. The model and
reasoning effort are mandatory arguments; omission is never treated as a
default-model success.

```bash
CODEX_FLOW_REAL_SDK=1 uv run --python 3.12 codex-flow sdk-compatibility-sentinel \
  --model gpt-5.6-luna \
  --effort medium \
  --output .codex-flow/h1-sdk-sentinel.json
```

The command returns status `passed` only when local start, real identity,
schema-bounded completion, explicit model and effort, ordered lifecycle
events, same-thread resume, and read-only repository isolation all pass. A
missing authentication or runtime capability yields retained JSON evidence
with status `blocked`; it never substitutes a CLI or direct app-server path.
The sentinel uses a persisted thread because the bundled runtime cannot resume
an ephemeral rollout; it archives only the new sentinel thread after proof.

## Capability matrix policy

The evidence JSON records every required and optional capability with one of
`proven`, `unsupported`, `not_exposed`, or `not_run`:

| Capability | H1 treatment |
| --- | --- |
| local start, thread identity, bounded turn, model, effort, events, resume, sandbox | proven only by the real sentinel |
| review | `not_exposed` unless a high-level SDK operation appears |
| structured skill input | `not_run` when the public type exists; otherwise `unsupported` |
| Desktop, idle wake, remote host, permission profile | `not_exposed` by the local SDK transport |

Thread creation alone is not evidence for Desktop, queue wake, remote host,
review delivery, skill execution, or permission-profile survival. Those checks
remain outside H1's transport contract and are not silently inferred.

H6-C retains this SDK-headless transport unchanged and adds a separate
host-mediated App-native mode. App-native prepare never imports or calls the
SDK adapter and never discovers or attaches to a private app-server socket; it
returns a typed action for the hosting Codex app. Accordingly, a successful
SDK thread remains no evidence of Desktop visibility, and the visible-App gate
requires its own real host-native pilot.

## Integrated-control exact-wheel and plugin boundary

The provider-free integrated-control pilot imported the exact retained wheel
`dist/human-terminal-ui/codex_flow-0.2.0-py3-none-any.whl` (328062 bytes,
SHA-256
`1d6eaa14c6f6c5e56f110950eb64d8e0f7513cdfb19d7243defdd058d66beea2`) from an
isolated temporary import root and launched its production
`codex_flow.cli.supervisor_run` service entrypoint. The service-supervisor-
SQLite path, live worker IPC, control acknowledgements, recovery tables, TUI,
and shutdown cleanup were all observed through one retained `Supervisor`.
The injected worker and typed provider-free fake SDK seam consumed a verified
skill input and preserved structured output without constructing a provider.
The observations therefore record `provider_calls=0`, `app_api_calls=0`,
`connector_invocations=0`, and `controller_model_tokens=0`; this is mechanism
proof, not provider compatibility proof.

The strict fixture discovered one enabled bundled `demo-plugin` with the
`demo.skill` skill and `demo-mcp` connector identity, held the typed skill
descriptor through both worker and SDK input boundaries, rejected post-bind
plugin-byte drift, rejected incomparable permission drift, and rejected a
secret-bearing manifest. Connector readiness is `proven_identity_only`; no
MCP or connector call was made. The worker's persisted raw result and
observation prove that `INTEGRATED_STEER_MARKER` changed behavior, and the
live ring remained within its entry/byte bounds. Optional App transcript
visibility is `not_observed`, and no App lifecycle operation or polling
occurred.

The current standard shared home remains unsuitable for the final real gate:
strict discovery encounters symlinked cached bundles before a selected ready
plugin can be classified. No plugin installation, enablement, trust,
authentication, cache, `CODEX_HOME`, or global App state was changed. The real
bundled-plugin/provider sentinel and final milestone status therefore remain
`external_blocked`. The smallest prerequisite is an externally supplied
standard shared home/environment containing one installed, enabled, ready
required bundled plugin that passes strict no-symlink discovery, together with
explicit authorization for exactly one bounded provider attempt. The sanitized
integrated record is retained at
`docs/reviews/evidence/integrated-control.json`.
