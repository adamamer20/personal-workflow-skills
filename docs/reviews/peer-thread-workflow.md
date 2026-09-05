# Plan: Deterministic Codex workflow controller

## Goal

Replace model-owned peer-thread transport and callback bookkeeping with a small,
controller-first Python harness. Sol remains responsible for planning and
material decisions, Luna executes decision-ready milestones, and independent
review gates promotion. The controller owns lifecycle state, idempotency,
worktrees, routing, recovery, budgets, and durable results so that messages are
notifications rather than the workflow source of truth. SDK-headless execution
must start, recover and complete without depending on Codex App availability.
The App may be open or closed; physical closure is an optional environment
state, not a runtime or promotion prerequisite. When open, the App is only an
optional projection for visible native workers and one terminal wake-up
notification; it is never lifecycle, liveness or result authority.

## Current context

- The prior peer-thread program delivered authorized native routing and merged
  plugin lifecycle hooks through `bd626a4` / plugin
  `0.1.4+codex.20260820180447` on `origin/main`.
- The seven-day transport audit supplied by the user found that the hook's exact
  response-envelope assumptions do not match current successful
  `create_thread` and `list_threads` results. Queued creation and reconciliation
  can therefore be misclassified even when a task exists.
- The same audit showed that task messages are not a reliable completion ledger
  and that long-lived planner control loops consume excessive context and
  credits.
- The current primary checkout is intentionally protected: branch
  `agent/thread-handoff-hooks` is six commits behind `origin/main` and contains
  older uncommitted hook work. This plan and its execution start from clean
  branch `agent/python-sdk-controller` at `bd626a4` in an isolated worktree.
- Local Codex CLI is `0.147.0`; `openai_codex` is not installed in the base
  Python environment. Python 3.12 and `uv` are available.
- Official OpenAI documentation describes `openai-codex` as a stable Python
  SDK for Python 3.10+, controlling the local Codex app-server over JSON-RPC and
  shipping a pinned Codex runtime. It documents synchronous/asynchronous thread
  start and run plus per-thread sandbox changes. The exact installed SDK's
  resume, event, structured-output, review, skill-input, and model-effort APIs
  must be proven by a real compatibility sentinel before orchestration depends
  on them.
- The user selected the Python SDK as the primary v1 transport. CLI commands may
  be used as acceptance or diagnostic oracles, but there will not be a second
  CLI-backed production controller.
- H1 is integrated on the canonical branch as `1c04bf7`. The retained real
  sentinel proves stable `openai-codex==0.147.0` local start, addressable thread
  identity, explicit model and reasoning effort, two schema-bounded turns, 45
  ordered lifecycle events, same-thread resume, and read-only sandbox isolation.
  Review, Desktop, idle wake, remote host, and permission profile are truthfully
  `not_exposed`; structured skill input is `not_run`.

## Proposed design

Add a typed Python package and `codex-flow` CLI to this repository:

```text
AGENTS.md                  # controller-specific engineering instructions
Makefile                   # canonical setup/check/test/format/lint entrypoints
.python-version
.pre-commit-config.yaml
pyproject.toml
src/codex_flow/
  cli.py                 # thin human-facing commands
  config.py              # typed workflow.toml contract
  domain.py              # ids, states, reasons, result contracts
  ledger.py              # SQLite transactions, leases, migrations
  artifacts.py           # atomic readable JSON/JSONL projections
  worktrees.py           # git worktree creation and ownership checks
  controller.py          # deterministic state transitions
  backends/
    codex_sdk.py         # only production Codex transport adapter
tests/
  controller/
workflow.toml            # one routing and limit table
```

## Immediate recovery selection after provider schema preflight failure

The first real execution of the planned refresh-policy successor reached the
official SDK and created a durable worker thread, but the provider rejected the
controller-owned output schema before model work began.  Both attempt 1 and the
single authorized recovery attempt returned `400 invalid_json_schema`: the
root object declared `blocker` in `properties` without including it in
`required`.  The durable dispatch
`model-89956fe146023c2e937d3dfcfb2c4a6d/milestone-57b78291c4f14ac0433031b2706ea0c9/executor/1`
is `human_attention_required` with no result and no active turn.  It remains
frozen: another retry before runtime repair is forbidden.

This is a bounded provider-boundary prerequisite, not evidence against the
refresh policy.  The canonical `ModelFacingResult` contract intentionally
accepts absent `blocker` for local compatibility and serializes it as nullable;
that public/persisted acceptance contract remains unchanged.  Only the
provider projection must obey the documented Structured Outputs object rule:
every declared property is required, and semantic optionality is represented
by an already-nullable child schema.  A canonical optional property that is not
nullable cannot be represented truthfully and must fail closed before an SDK
call rather than being silently forced or widened.

The immediate serial DAG is:

    provider-object-required-closure [ready; one recovery owner]
      -> install verified wheel and safely refresh only the target harness
      -> one CAS recovery of the existing human-attention dispatch
      -> refresh-replacement-policy-matrix-closure
      -> independent Luna XHigh correctness causal review
      -> independent Sol Medium architecture causal review
      -> integration-owner promotion/install/refresh/provider-free activation step 5
      -> one already-authorized real streaming sentinel
      -> live-plan-dag-revision
      -> module-responsibility-decomposition

Every edge is an acceptance dependency.  The blocked worker is never replaced
or retried through another transport.  The recovery reuses the same durable
dispatch only after source, package and installed-runtime parity prove the
provider schema fix is active.

### Provider object projection invariant

`src/codex_flow/backends/codex_sdk.py` remains the sole official SDK boundary.
Its existing positive recursive projection must, for every closed object with
declared `properties`, emit `required` equal to the complete ordered property
set.  Existing canonical required properties remain unchanged.  Each canonical
optional property must already admit JSON null through its type, enum, const,
or canonical `oneOf`; otherwise projection raises
`TerminalFailureAfterIdentity` before `Thread.turn`.  The projector does not
mutate its input, invent defaults, relax `additionalProperties`, change the
canonical schema/digest/parser, or turn local optional values into nullable
values.

Null admission is effective-schema evaluation, not an independent-key
shortcut. A declared type must admit null; a present `const` must be null; a
present `enum` must contain null; and canonical `oneOf` admits null only when
exactly one branch admits it. Thus nullable type plus a non-null const or an
enum excluding null is not nullable, and overlapping null branches are not a
truthful optional-value representation.

Focused tests in `tests/test_codex_sdk_adapter.py` must prove:

- the real `model_facing_result_schema()` projects root `required` exactly equal
  to root `properties`, including nullable `blocker`;
- the projected nullable `blocker` remains object-or-null and the complete
  canonical local validator accepts both serialized states;
- the rule applies recursively to a nested optional nullable property;
- an optional non-nullable property, including nullable type contradicted by a
  non-null const, enum exclusion or overlapping `oneOf`, fails before the fake
  SDK thread receives a turn call; and
- projection is deterministic and leaves the canonical input byte-for-byte
  equivalent.

No provider call is part of this implementation gate.  After the focused
adapter and worker/recovery partitions, the owner stages only the two source
paths, inspects the staged diff, runs `git diff --cached --check`, commits once,
runs full `make check`, and builds a fresh external wheel with complete packaged
Python source parity.  Independent Luna XHigh correctness and Sol Medium
architecture reviews must return P0=0/P1=0 before installation.  The
integration owner then uses the single canonical installer, proves installed
adapter byte parity, safely refreshes only
`codex-flow-937220b34ee45f7f.service` while proving
`codex-flow-71308abd5aeed226.service` unchanged, and authorizes exactly one CAS
recovery of the existing dispatch.  No new evidence file, module, schema,
migration, command, service, transport or compatibility alias is allowed.

The semantic delta is one completed boundary invariant, not new vocabulary.
Mutable implementation surfaces are exactly
`src/codex_flow/backends/codex_sdk.py` and
`tests/test_codex_sdk_adapter.py`.  Protect the canonical result schema and
digest in `src/codex_flow/contracts.py`, all ledger/worker/harness/service/TUI
code and tests, current refresh evidence, retained wheels, AGENTS.md, services,
provider/App/network/global state, remotes and unrelated bytes.

## Next execution — provider-object-required-closure

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make every closed object in the official SDK Structured Outputs projection provider-valid without changing the canonical local result contract: all provider properties are required, and canonical optionality is representable only through an already-nullable child."
    ),
    decomposition=(
        "Complete the existing recursive positive projector so each closed object emits required equal to its complete ordered properties.",
        "Evaluate canonical null admission conjunctively across type, const and enum and exclusively across oneOf, then fail before Thread.turn for an optional property that cannot be represented truthfully.",
        "Prove the real nullable blocker, recursive nullable fields, non-nullable rejection, input immutability and fake-SDK no-call boundary.",
        "Commit the two owned paths, run focused and full gates, build a fresh external wheel with complete source parity, then obtain independent correctness and architecture reviews before activation."
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "The provider projection of model_facing_result_schema has root required exactly equal to root properties and includes blocker while preserving blocker as object-or-null.",
        "Every nested closed object follows the same complete-required rule; canonical optional properties project only when their effective existing schema admits null after type, const, enum and exclusive oneOf constraints.",
        "An optional non-nullable property raises TerminalFailureAfterIdentity before any SDK turn call, and projection never mutates or widens the canonical schema.",
        "Canonical ModelFacingResult schema, digest, parser and local validation behavior remain unchanged.",
        "Focused adapter and affected worker/recovery partitions plus make check pass; a fresh external wheel matches every packaged Python source path.",
        "Only the two owned paths form one clean implementation commit and independent Luna XHigh correctness plus Sol Medium architecture reviews return P0=0/P1=0 before install or dispatch recovery."
    ),
    mutable_surfaces=(
        "src/codex_flow/backends/codex_sdk.py",
        "tests/test_codex_sdk_adapter.py"
    ),
    protected_surfaces=(
        "src/codex_flow/contracts.py and every canonical/public/persisted schema, digest and parser",
        "ledger, worker, harness, service, TUI and all other production/test/evidence paths",
        "docs/reviews/peer-thread-workflow.md after this recovery plan commit, AGENTS.md, retained wheels and current refresh evidence",
        "both named services, provider/App/network/global state, remotes, push, rebase, history rewrite, discard, cleanup and unrelated bytes"
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer"))
    ),
    prompt=(
        "Use execute-milestone as the single bounded recovery owner for provider-object-required-closure in the existing integration checkout. Modify only codex_sdk.py and test_codex_sdk_adapter.py. Make the positive recursive provider projection require every declared property at every closed object; permit a canonical optional property only when its effective schema admits null after conjunctive type/const/enum and exclusive oneOf evaluation, otherwise fail before Thread.turn. Preserve the canonical ModelFacingResult schema/digest/parser and every other surface. Add real-result, recursive, constraint-conflict, input-immutability and fake-SDK no-call regressions. Commit a successor repair, run focused and affected gates plus make check, build a fresh external wheel with complete source parity, and return exact identities with independent correctness and architecture re-reviews pending. Do not install, refresh services, retry the blocked dispatch, invoke a provider/App/network action, create another task or add artifacts/modules/schemas/commands."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=8_000
)
```

## Paused successor after exhaustive refresh-policy causal reviews

The fixed clean integration parent is
`70a1065d188ec94ac3b734743177206a1e2e7249` on
`agent/python-sdk-controller`. Its source/test commit is
`c4f270d889582ba73483e43897d9a3b572f8d45a`. The reviewed external wheel at
`/tmp/codex-flow-wheel-refresh.o9aH5N/codex_flow-0.2.0-py3-none-any.whl`,
SHA-256 `2c68304a43c00c8b1fcce730e5053f67b2ee312fd408c5defbc59cdeef580f4b`,
457834 bytes, matched 33/33 packaged Python paths; the evidence JSON SHA-256
was `12d54409ba2778c066879171d591d08e425eb9203deb5320f1fd7ef7b2e021c7`,
5646 bytes. These are immutable inputs, not successor closure proof.

The correctness review left `CI-MATRIX-001` open because the public matrix did
not discriminate delayed acquisition or the required fence, stop, inactivity,
birth-death and revalidation failures. The architecture causal review also
found that nonzero or raised `systemctl start` could still enter the absence
restore branch, an already fenced inactive/dead replacement still received a
redundant stop, and `make check` had not closed on the evidence tip. All are
promotion-blocking P1 findings. The disconnected TUI behavior, authenticated
`for_state_root` proof and wheel parity were accepted and are now protected.

The remaining serial DAG is:

    refresh-replacement-policy-matrix-closure [blocked by provider-object install/refresh/CAS recovery]
      -> independent Luna XHigh correctness causal review
      -> independent Sol Medium architecture causal review
      -> integration-owner promotion/install/refresh/provider-free activation step 5
      -> one already-authorized real streaming sentinel
      -> live-plan-dag-revision
      -> module-responsibility-decomposition

Every edge is an acceptance dependency. Production policy, its exhaustive
public matrix, wheel identity and evidence bind one serial candidate.

## Refresh replacement policy-matrix architecture

### Outcome, non-goals and authorities

`refresh_with_credential` restores legacy harness bytes only after a successful
start followed by complete stable-absence proof or exact fenced-replacement
shutdown and death proof. Every incomplete, uncertain, live, drifted, failed,
missing, malformed or ambiguous proof retains current harness bytes. A delayed
replacement transitions into replacement handling or fails closed; it is never
reduced to a false absence result.

This is not a service, ledger, systemd, TUI, IPC, schema, packaging or recovery
redesign. It adds no production module, public or persisted type, entrypoint,
table, migration, service, socket, transport, timer or polling owner. It does
not install or refresh `codex-flow`, touch
`codex-flow-937220b34ee45f7f.service` or
`codex-flow-71308abd5aeed226.service`, invoke a provider sentinel, use App or
network state, or alter accepted disconnected TUI behavior.

The ledger remains durable row/fence authority, systemd remains unit authority,
PID plus birth identity remains process-liveness authority, and installed-unit
validation remains byte/configuration authority. Reuse the existing deadline,
clock, sleeper, runner, liveness seam, rollback mechanics and public entrypoint.

### Compact table-driven policy

Keep one private policy in `src/codex_flow/service.py`. Descriptive private
functions or a private declarative table are allowed, but no new public/domain
type. Each complete observation combines exact authority identity and shutdown
bit, unit activity, PID/birth liveness, socket presence where relevant, and
installed-unit validity:

| State | Required action | Restore eligibility |
| --- | --- | --- |
| `start` returns nonzero or raises | Preserve current bytes and raise; do not observe for absence or restore | Permanently ineligible because start is uncertain |
| Zero start; exact predecessor shutdown 1, inactive unit, dead predecessor PID/birth, no current `harness.sock` | Run the stable-predecessor protocol below | Eligible only after three observations and immediate final revalidation |
| Higher-epoch distinct valid replacement, shutdown 0 | Arm `Ledger.arm_harness_refresh_fence` exactly once and require the returned unchanged identity with shutdown 1 | Eligible only after complete shutdown, death and revalidation proof |
| Higher-epoch replacement, shutdown 1, active unit or live PID/birth | Never refence; stop exact unit once and prove inactivity/death | Eligible only after complete revalidation |
| Higher-epoch replacement, shutdown 1, already inactive and dead | Never refence and skip redundant stop | Eligible only after exact row and installed-unit revalidation |
| Unit stays active, PID/birth stays live, fence/stop fails or deadline expires | Preserve current bytes and raise | Ineligible |
| Post-fence or post-stop row is missing, malformed, ambiguous, unfenced or identity-drifted | Preserve current bytes and raise; issue no direct SQL | Ineligible |
| Installed-unit revalidation fails | Preserve current bytes and raise | Ineligible |
| Unexpected epoch, repository/state/version mismatch, invalid PID/birth, malformed/missing shutdown, socket/liveness uncertainty or any unclassifiable state | Preserve current bytes and raise | Permanently ineligible |

Shutdown 0 fences exactly once before shutdown handling. After the returned
fence is validated, an already inactive/dead replacement needs no stop;
otherwise stop occurs exactly once. Shutdown 1 never refences, and its explicit
already-inactive/dead branch skips stop.

Stable-predecessor absence proof starts only after `start` returns zero. It
requires three complete observations of the exact unchanged predecessor
epoch/PID/birth row with shutdown 1, unit inactivity, birth-bound death and no
current `harness.sock`. Observation 1 to 2 and 2 to 3 are separated by the
existing sleeper by at least 50 milliseconds within the existing deadline.
Immediately before restore, all facts are re-read and must still match. A
higher-epoch replacement acquired before observation 1, 2 or 3, or during final
revalidation, enters replacement handling in the same invocation; if safe
completion is impossible, current bytes remain and the invocation fails.
Missing, malformed, ambiguous, live, drifted or expired observations never
reset the count or become absence.

Restoration is exact in bytes and mode. The existing later retry must acquire
the next epoch and succeed through the public entrypoint.

### Exhaustive public test matrix

`tests/test_service_lifecycle.py` owns one parameterized matrix through public
`refresh_with_credential`. Every row records start result, ordered authority
observations, unit-activity and PID/birth-liveness sequences, socket facts,
expected fence/manager calls, installed bytes/mode, exception or success, and
retry outcome. Distinct rows cover:

- start success, nonzero and raised exception; both uncertain-start rows use an
  exact inactive/dead/socket-free predecessor and prove no observation restore;
- predecessor observations 1, 2 and 3, spacing of at least 50 milliseconds and
  immediate final revalidation;
- higher-epoch acquisition on observation 1, 2 and 3, each transitioning into
  replacement handling or closed failure;
- replacement shutdown 0 and 1 with active/live and already inactive/dead
  states; shutdown 0 fences once, shutdown 1 never refences, and inactive/dead
  shutdown 1 never stops;
- fence failure, post-fence identity drift, stop failure, unit remaining active
  and PID/birth remaining live;
- post-stop authority drift, missing, malformed and ambiguous row;
- installed-unit revalidation failure;
- malformed/missing shutdown, repository/state/version/epoch/PID/birth drift,
  socket or liveness uncertainty, and deadline exhaustion; and
- exact restore of bytes/mode plus accepted higher-epoch retry success.

Every unsafe row asserts current harness bytes remain and no forbidden fence,
stop, restore or direct ledger mutation occurs. Test-local table helpers may
compress setup/assertions but cannot become a production API or fixture file.

### Architecture map, ownership and budgets

One Luna XHigh implementation owner has exactly three mutable paths:

- **Modify** `src/codex_flow/service.py`: complete the private policy,
  delayed-acquisition transition and exact restore/retry behavior through the
  existing public entrypoint and authorities.
- **Modify** `tests/test_service_lifecycle.py`: add the exhaustive public table
  and exact call, liveness, byte/mode and retry assertions.
- **Modify**
  `docs/reviews/evidence/safe-refresh-and-control-list-paging.json`: only after
  source/test commit and fresh wheel parity, record successor lineage and gates
  without a self-hash.

**Preserve** `src/codex_flow/tui_client.py`,
`tests/test_live_worker_control.py`, every other production/test/evidence path,
`AGENTS.md`, this plan after its planning commit, retained wheels, installer,
ledger, harness, control clients, TUI models/UI/CLI, schemas, migrations,
contracts, entrypoints, services, sockets, provider/App/network/global state,
remotes and unrelated bytes. A concrete defect requiring a protected path
returns to planning rather than widening the capsule.

Create and remove nothing. New durable-artifact, production-module,
public-boundary, schema, entrypoint and dependency-edge budgets are zero. The
semantic delta is one completed existing invariant: a total fail-closed refresh
replacement policy. Descriptive private helpers and a private test-row
representation may compress repeated decisions; no new class, protocol, enum,
model, registry, runner, service or compatibility alias is justified. Timing
and parity numbers support but cannot weaken the safety proof.

### Artifact sequence, validation and promotion

The single owner must:

1. run the focused public matrix and affected service semantic partition;
2. stage only `src/codex_flow/service.py` and
   `tests/test_service_lifecycle.py`, inspect the staged diff, run
   `git diff --cached --check`, and create one source/test commit whose exact
   parent is this planning commit;
3. build one fresh external wheel from that commit in a new external temporary
   directory and independently compare all 33 packaged Python paths, including
   `codex_flow/service.py`;
4. after 33/33 parity only, update and stage only the existing evidence JSON,
   inspect it, run `git diff --cached --check`, create one evidence-only commit,
   and compute its final hash/size externally; and
5. run full `make check` on the final evidence tip, verify clean status and
   exact two-commit ancestry containing only the three mutable paths.

The prior wheel/evidence identities are superseded, never reused or
overwritten. Evidence records exact planning parent, source/test commit,
evidence tip binding, external wheel path/hash/size, 33/33 enumeration, focused
and final-tip gates, and no self-hash.

Promotion requires independent Luna XHigh correctness and Sol Medium
architecture causal reviews. Each binds the exact final tip, fresh wheel,
external evidence hash/size and every commit in
`ba05399d57da5ca65250f64727e36270f786c213..FINAL_TIP`, and returns P0=0/P1=0.
No visual review is requested. Finding severity remains distinct from
`promotion_blocking`; every deferred finding names owner and `defer_to`. These
are provider-free gates only; installation, service refresh, activation,
protected-service checks and the real streaming sentinel remain outside.

## Paused successor capsule — refresh-replacement-policy-matrix-closure

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make refresh_with_credential a total fail-closed replacement policy: uncertain start never restores, delayed acquisition is handled causally, and legacy bytes return only after complete stable-absence or fenced-replacement death proof."
    ),
    decomposition=(
        "Express one private table-driven classification over start outcome, exact authority identity, unit activity, birth-bound liveness, socket presence and installed-unit validity.",
        "After successful start only, require three complete predecessor observations at least 50ms apart plus immediate final revalidation, routing acquisition on observation 1, 2 or 3 into replacement handling.",
        "For shutdown 0 fence exactly once; for shutdown 1 never refence; stop only a live/active replacement and skip redundant stop for an already inactive/dead fenced replacement.",
        "Drive every success, drift, malformed, uncertain and failed transition through a parameterized public refresh_with_credential matrix with exact calls, liveness and installed-byte assertions.",
        "Commit source/tests, prove a fresh external wheel at 33/33, commit evidence alone, run make check on the final tip, then obtain both independent causal reviews."
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Nonzero or raised systemctl start is permanently restore-ineligible and retains current harness bytes even with an exact inactive/dead/socket-free predecessor.",
        "A zero start with the exact unchanged predecessor restores only after three complete observations separated by at least 50ms and immediate final revalidation; acquisition on observation 1, 2 or 3 enters replacement handling or fails closed.",
        "Shutdown 0 fences exactly once; shutdown 1 never refences; active/live replacement shutdown is stopped and death-proven, while already inactive/dead shutdown 1 skips redundant stop.",
        "Fence failure, post-fence drift, stop failure, active unit, live PID/birth, post-stop missing/malformed/ambiguous/drifted authority and installed-unit revalidation failure all retain current bytes.",
        "Every missing, malformed, ambiguous, unexpected, uncertain, live, drifted, failed or incomplete proof retains current harness bytes and issues no inferred direct ledger repair.",
        "Eligible proofs restore exact legacy bytes/mode, and the accepted later higher-epoch retry succeeds.",
        "The exhaustive public matrix covers every frozen row with ordered inputs and exact manager, fence, byte/mode and outcome assertions.",
        "Only service.py and test_service_lifecycle.py form the source/test commit; a fresh wheel matches 33/33 Python paths; only the existing JSON forms the evidence commit; make check passes on that final tip and status is clean.",
        "Independent Luna XHigh correctness and Sol Medium architecture reviews bind the full candidate and return P0=0/P1=0; no visual or runtime/provider action occurs."
    ),
    mutable_surfaces=(
        "src/codex_flow/service.py",
        "tests/test_service_lifecycle.py",
        "docs/reviews/evidence/safe-refresh-and-control-list-paging.json"
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md and AGENTS.md after this planning commit",
        "src/codex_flow/tui_client.py, tests/test_live_worker_control.py and all other production/test/evidence paths",
        "retained wheels, installer, ledger, harness, control clients, TUI model/UI/CLI, schemas, migrations, contracts, entrypoints, services and sockets",
        "provider/App/network/global state, both named real services, remotes, push, rebase, history rewrite, discard, cleanup and unrelated bytes"
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer"))
    ),
    prompt=(
        "Use execute-milestone as the one Luna XHigh mutable owner for only refresh-replacement-policy-matrix-closure in the existing integration checkout. Modify exactly service.py, test_service_lifecycle.py and, after wheel parity, the existing evidence JSON; create nothing. Implement the frozen table through public refresh_with_credential: nonzero/raised start never observes for absence or restores; zero start needs three exact predecessor observations >=50ms apart plus final revalidation; delayed acquisition on observation 1/2/3 enters replacement handling or fails closed; shutdown 0 fences once, shutdown 1 never refences, and already inactive/dead shutdown 1 skips stop. Exhaustively test fence/stop/inactivity/death/row/installed-unit failures and exact restore/retry success; every unsafe case retains current bytes. Preserve accepted TUI code/tests and all protected surfaces. Commit source/tests, build a fresh external wheel and prove 33/33, commit evidence alone, run make check on the final evidence tip, verify clean ancestry, and return exact identities with both independent causal reviews pending. Do not install or refresh codex-flow, touch either named service, run a provider sentinel, create a task, or perform App/network/global-state actions."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000
)
```

## Superseded selection after replacement-state correctness review

The clean integration tip is
`17e6d2f0f1786c1fd7c35931877e595cbde70f87`: source/test commit
`3794c7ff2cde6abfad2949ac72b4fee23d2595e0` followed by the evidence-only
commit. Architecture/provenance review accepted the existing ledger, service,
systemd and authenticated TUI-client authority boundaries, and the external
wheel at `/tmp/codex-flow-wheel.FJ76dl/codex_flow-0.2.0-py3-none-any.whl`,
SHA-256 `8c59a09260462724dbc7fc50b34f42f7b348e4919fcdd446ae739acc43a68075`,
456829 bytes, was independently compared at 33/33 packaged Python paths.
The evidence input is the existing JSON at tip `17e6d2f`, whose externally
computed SHA-256 is
`21ff83e84ab51a593818a7ec2236313e997e0915cc7e7a151f3133d646c486bc` and
size is 5220 bytes. These wheel and evidence identities are accepted inputs
only: successor source changes require a fresh external wheel and new external
evidence identity and must not inherit a final parity or promotion claim.

The correctness review left four promotion-blocking findings over reachable
production paths:

- `CI-003-001`: a matching higher-epoch replacement already carrying
  `requested_shutdown=1` is rejected before the exact unit is stopped, so the
  accepted fenced-replacement rollback transition never runs;
- `CI-003-002`: a successful start followed only by the unchanged fenced dead
  predecessor sleeps until timeout instead of positively proving replacement
  absence and restoring the legacy unit bytes;
- `CI-004-001`: `load_more_sessions()` clears its maps, tokens and snapshot
  identities after stale/error, but `_snapshot_from_statuses()` infers both
  completion flags as true from the cleared `None` tokens; and
- `CI-004-TEST`: the new stale/error coverage uses fake clients and therefore
  does not prove the production `for_state_root -> authenticated harness.sock`
  boundary or reconnection behavior.

The prior final-closure capsule is superseded. The remaining serial DAG is:

    refresh-replacement-state-and-disconnected-control-proof [ready; one mutable owner]
      -> independent Luna XHigh correctness causal re-review
      -> independent Sol Medium architecture causal re-review
      -> integration-owner promotion/install/refresh/provider-free activation step 5
      -> one already-authorized real streaming sentinel
      -> live-plan-dag-revision
      -> module-responsibility-decomposition

Every edge is an acceptance dependency. There is no fan-out: the two service
classifications, centralized disconnected TUI transition, production-boundary
tests and wheel-bound evidence must close as one exact candidate. The frozen
DAG base for this serial successor is this planning commit; the full causal
review base remains `ba05399d57da5ca65250f64727e36270f786c213`.

## Exhaustive replacement-state and disconnected-control replan

### Outcome, scope and non-goals

The successor closes the remaining `CI-003` state machine so every matching
fenced replacement is stopped and death-proven before rollback, while a
genuinely absent replacement is recognized through a bounded positive
observation sequence rather than a timeout. It closes `CI-004` through one
central disconnected-control snapshot construction and real authenticated IPC
tests for refresh, load-more and endpoint loss/recovery.

This is not a lifecycle, ledger, IPC, paging or TUI redesign. It does not add a
module, public or persisted type, API, table, schema, migration, transport,
service, socket, polling loop, compatibility alias, provider path, App path,
fixture file or runner. It does not change conversation history, token framing,
ordering, visibility or page semantics. No real service, provider, App,
network, install, wheel reuse/overwrite or global configuration action is
authorized during implementation. Promotion and all later integration-owner
actions remain outside the successor.

### Exhaustive service classification and observation protocol

`refresh_with_credential` remains the only production entrypoint. Reuse its
captured predecessor identity, monotonic post-start guard, existing deadline,
ledger fence, exact installed-unit checks, systemd manager seam, PID/birth
liveness seam and rollback helper. The post-start classification is a single
table-driven policy expressed within `src/codex_flow/service.py`; no second
authority or public result is introduced.

| Post-start observation | Required production action | Restore eligibility |
| --- | --- | --- |
| Matching higher-epoch replacement, distinct valid PID/birth identity, `requested_shutdown=0` | Arm `Ledger.arm_harness_refresh_fence` exactly once; validate the returned same identity with shutdown 1; stop the exact unit; prove unit inactivity and PID/birth death; revalidate the same fenced row and installed harness bytes | Restore only after the complete fence/stop/death/revalidation proof |
| Matching higher-epoch replacement, distinct valid PID/birth identity, `requested_shutdown=1`, unit active or PID/birth live | Skip `Ledger.arm_harness_refresh_fence`; stop the exact unit immediately; prove unit inactivity and PID/birth death; revalidate the unchanged same fenced row and installed harness bytes | Restore only after the complete stop/death/revalidation proof |
| Matching higher-epoch replacement with shutdown 1 already inactive and dead | Skip refence and redundant stop only when both inactivity and birth-bound death are already true; revalidate the unchanged fenced row and installed harness bytes | Restore after the same death/revalidation proof |
| Exact captured predecessor row with `requested_shutdown=1`, unchanged epoch/PID/birth, inactive unit, dead predecessor PID/birth and absent current `harness.sock` entry | Run the bounded consecutive-observation protocol below | Restore only after that positive absence proof |
| Exact predecessor but unit active, predecessor PID/birth live, current socket present, or any liveness fact uncertain | Continue observing only while the state remains safely classifiable and the deadline remains; otherwise fail closed | Ineligible without the full positive absence proof |
| Matching predecessor or replacement with malformed/missing `requested_shutdown`, including `2` | Leave current harness bytes installed and raise | Permanently ineligible for this invocation |
| Missing, malformed or ambiguous authority; repository/state/version mismatch; unexpected epoch; invalid PID/birth; predecessor/replacement identity drift | Leave current harness bytes installed and raise; issue no direct SQL | Permanently ineligible for this invocation |
| Start returns nonzero/raises before a positive absence proof, or start outcome is otherwise uncertain/live | Leave current harness bytes installed and raise | Ineligible |
| Fence, stop, unit-inactivity, PID/birth-death, same-row revalidation or installed-unit validation fails | Leave current harness bytes installed and raise | Ineligible |

The positive no-replacement proof is concrete and bounded. After start returns
success, count three consecutive complete observations of the exact unchanged
captured predecessor row with `requested_shutdown=1`, the exact unit inactive,
the predecessor PID/birth dead and no filesystem entry at the current
`harness.sock`. Separate observation 1 from 2 and 2 from 3 with the existing
sleeper by at least 50 milliseconds; all reads and waits consume the existing
refresh deadline. Any replacement identity, malformed/ambiguous row, active or
uncertain process/unit, socket entry or deadline exhaustion aborts this proof
and follows the table. Immediately before restoration, re-read all four facts
and require the same predecessor epoch/PID/birth and fence again. This final
revalidation plus the stable observation window distinguishes a delayed or
uncertain acquisition from positive absence without creating a new timer,
thread or polling authority. A test clock may advance the same sleeper seam;
production retains the existing 30-second outer deadline.

The already-fenced replacement branch is not an error and never calls
`arm_harness_refresh_fence`. It still owns the exact same stop, inactivity,
birth-death, row revalidation, installed-byte validation and legacy restoration
sequence as a replacement that the refresh invocation fenced itself. The
ordinary shutdown-0 branch continues to fence exactly once. Both branches
leave the higher-epoch row fenced after rollback so the accepted later retry
can acquire the next epoch.

### Central disconnected-control snapshot construction

`TerminalUiClient` gains one private, centralized disconnected-control snapshot
construction inside `src/codex_flow/tui_client.py`. Every disconnected exit
from `refresh()`, `load_more_sessions()` and the existing offline transition
must call it after closing/clearing the relevant live state. The construction
clears `_worker_statuses`, `_decision_statuses`, both next-page tokens and both
snapshot ids, then returns a `TerminalUiSnapshot` with empty worker and decision
rows and explicit `workers_complete=False` and `decisions_complete=False`.
It must not route through token-derived completeness or retain previous control
rows. `_snapshot_from_statuses()` remains the connected accumulated-page
projection and derives completeness only for a valid connected snapshot.

The next `refresh()` after any disconnected snapshot sends `page_token=None`
to both production clients, accepts only a fresh pair of first pages, atomically
replaces the empty accumulation and derives completeness from those fresh
pages. Conversation history, selected-subject reconciliation, live keyframes,
visibility and `L`/`M`/`F` behavior remain unchanged.

### Frozen implementation architecture map and ownership

One Luna XHigh implementation owner has exactly five mutable paths:

- **Modify** `src/codex_flow/service.py`: complete the table-driven
  post-start classification, already-fenced replacement transition and bounded
  positive no-replacement observations using only existing authorities and
  private mechanics.
- **Modify** `src/codex_flow/tui_client.py`: centralize the private
  disconnected-control snapshot construction and route refresh, load-more and
  offline exits through it without changing public types or APIs.
- **Modify** `tests/test_service_lifecycle.py`: add public
  `refresh_with_credential` table rows for every frozen classification and
  exact call/liveness/byte assertions.
- **Modify** `tests/test_live_worker_control.py`: replace or extend fake-only
  assertions with production-boundary `TerminalUiClient.for_state_root` tests
  over the existing authenticated disposable `harness.sock`.
- **Modify** the existing
  `docs/reviews/evidence/safe-refresh-and-control-list-paging.json` only after
  the fresh wheel parity gate, recording exact successor facts without an
  embedded self-hash.

**Preserve** `src/codex_flow/ledger.py`, `src/codex_flow/harness.py`,
`src/codex_flow/control_client.py`, `src/codex_flow/tui_models.py`,
`src/codex_flow/tui.py`, `src/codex_flow/cli.py`,
`scripts/install_personal_workflow_skills.py`,
`tests/test_plugin_installation.py`, all other production/test/evidence paths,
test partitions, schemas, migrations, contracts, entrypoints, services,
sockets, provider/App/global state, remotes and unrelated worktree bytes. A
discriminating failure that requires any protected path or boundary returns to
planning rather than widening the capsule.

Create and remove nothing. The new durable-artifact budget is zero. The
semantic delta is completion of two existing invariants: one exhaustive
replacement-state transition and one centralized disconnected-control
projection. The only new identifier permitted is a descriptive private helper
inside `TerminalUiClient` if needed to make that projection single-sourced; it
is not a public/domain concept or compatibility surface. No new class,
protocol, enum, model, registry, runner or public method is justified.

### Production-boundary test contract

The service suite must use a parameterized state table through the public
`refresh_with_credential` entrypoint. Each row records the authority sequence,
unit active sequence, PID/birth liveness sequence, socket presence, manager
calls, fence count, final installed bytes and restore eligibility. It must
cover: malformed, missing and ambiguous authority; repository/state/version or
identity drift; shutdown-0 replacement fenced exactly once; shutdown-1
replacement skipping refence but following stop/death/revalidation; already
inactive/dead shutdown-1 replacement; matching predecessor completing the
three-observation plus final-revalidation absence proof; predecessor becoming a
replacement during that window; uncertain/live or failed start; and every
fence, stop, inactivity, death and revalidation failure. Unsafe cases retain
current harness bytes; only the two proven rollback classifications restore
legacy bytes. The accepted higher-epoch retry remains green.

The TUI suite must instantiate `TerminalUiClient.for_state_root` against the
existing authenticated socket of a disposable `WorkflowHarness`, first obtain
rows plus non-null page tokens/snapshot ids, and then exercise both failures:

1. mutate a disposable worker or decision source fingerprint between first
   page and `load_more_sessions()` so the production harness returns `stale`;
2. close or deterministically fault that disposable harness endpoint so a
   production client request reaches the bounded IPC/client error and offline
   transition.

For each failure, assert the public snapshot has empty rows and false
completeness and the internal worker/decision maps, tokens and snapshot ids are
all cleared. Then use the same state root to start/reacquire the disposable
harness endpoint, reconnect with `refresh()`, prove both outgoing continuation
tokens are `None`, and prove only fresh first-page rows and completeness are
published. Fake clients may remain for narrow model tests but cannot satisfy
`CI-004-TEST`. No real service or provider is involved.

### Artifact sequence, validation and promotion

The artifact sequence is fixed:

1. stage only the four source/test paths, inspect the staged diff, run
   `git diff --cached --check`, and create one coherent source/test commit on
   this planning commit;
2. afterward build one fresh wheel from that exact commit into a fresh external
   temporary directory and independently enumerate and compare all 33 packaged
   Python paths against source; the accepted 17e6d2f wheel is an input, never
   the successor wheel;
3. only after exact 33/33 parity, update the existing evidence JSON with the
   source/test commit, external wheel path/SHA-256/size, enumerated parity and
   gate results, explicitly superseding the 17e6d2f wheel/evidence identities
   without embedding the evidence file's own hash; and
4. stage only that JSON, inspect it, run `git diff --cached --check`, create one
   evidence-only commit, then compute its final hash/size externally.

Closure requires the focused exhaustive service matrix and production-boundary
TUI stale/error/recovery tests; accepted IPC, bootstrap, token, 120/120 paging,
visibility and higher-epoch retry regressions; every affected service and
live-control/TUI semantic partition; full `make check`; exact external 33/33
wheel parity; and a clean two-commit implementation ancestry containing only
the five mutable paths. Independent Luna XHigh correctness and Sol Medium
architecture causal re-reviews must bind the final evidence-only tip, fresh
wheel identity, external evidence identity and every commit in
`ba05399d57da5ca65250f64727e36270f786c213..FINAL_TIP`, with P0=0/P1=0.
Finding severity remains distinct from `promotion_blocking`; every deferred
finding names its owner and `defer_to`.

These are provider-free closure gates. Installation, service refresh,
activation step 5, protected-service verification, provider execution and App
actions remain outside this capsule. A hard deadline or artifact budget cannot
weaken a safety/observable gate: use an in-scope proof-preserving repair or
return a bounded replan; do not restore on uncertainty or add an artifact.

## Next execution — refresh-replacement-state-and-disconnected-control-proof

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Close CI-003-001/002 with exhaustive post-start replacement classification and bounded positive absence proof, and close CI-004-001/TEST with one truthful disconnected-control projection proven through authenticated production clients."
    ),
    decomposition=(
        "Treat a matching higher-epoch shutdown-1 replacement as already fenced: skip refence, stop its exact unit, prove inactivity and birth-bound death, revalidate the same row and only then restore.",
        "Recognize positive replacement absence only after three deadline-bound consecutive unchanged-predecessor/inactive/dead/no-current-socket observations plus immediate final revalidation.",
        "Centralize every refresh, load-more and offline disconnected-control snapshot so rows, maps, tokens and snapshot ids are empty and both completeness flags are explicitly false.",
        "Drive the exhaustive service matrix through refresh_with_credential and stale/error/reconnect TUI tests through for_state_root and the authenticated disposable harness.sock.",
        "Create the source/test commit, build and independently prove a fresh external wheel at 33/33, then create the evidence-only commit and obtain two causal independent reviews."
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "The public refresh_with_credential state table covers malformed, missing and ambiguous authority, identity drift, shutdown-0 and shutdown-1 replacements, exact predecessor absence, delayed acquisition, uncertain/live or failed start, and fence/stop/death/revalidation failures with exact calls, bytes and liveness outcomes.",
        "A matching epoch+1 replacement with requested_shutdown=1 never calls arm_harness_refresh_fence; it stops the exact unit, proves inactivity and PID/birth death, revalidates the unchanged fenced row and restores legacy bytes only after that proof.",
        "A successful start with only the exact fenced predecessor restores only after three complete observations separated by at least 50ms within the existing deadline and an immediate final revalidation; delayed/uncertain replacement acquisition or deadline exhaustion leaves current harness bytes installed.",
        "Malformed/missing/ambiguous authority, identity drift and uncertain/live start never restore; shutdown-0 is fenced once; fence/stop/death/revalidation failure leaves current bytes; the accepted higher-epoch retry remains green.",
        "Every disconnected exit from refresh, load_more_sessions or offline transition uses one private construction and exposes empty worker/decision rows, cleared internal maps/tokens/snapshot ids and workers_complete=False plus decisions_complete=False.",
        "Production-boundary tests use TerminalUiClient.for_state_root and authenticated disposable harness.sock, force stale by changing the source fingerprint and force client error by closing/faulting the endpoint, then reacquire/reconnect and prove tokenless fresh first-page recovery.",
        "Conversation history and accepted IPC/bootstrap/token/120-worker/120-decision/M/F paging behavior remain unchanged and green; no protected boundary or new artifact is introduced.",
        "The four source/test paths form one commit; a fresh later external wheel matches all 33 packaged Python paths; the existing JSON alone forms the evidence commit and contains exact wheel facts but no self-hash.",
        "Focused matrices, affected partitions, full make check, staged-diff hygiene, clean two-commit ancestry and independent Luna XHigh correctness plus Sol Medium architecture reviews over ba05399d57da5ca65250f64727e36270f786c213..FINAL_TIP close at P0=0/P1=0."
    ),
    mutable_surfaces=(
        "src/codex_flow/service.py",
        "src/codex_flow/tui_client.py",
        "tests/test_service_lifecycle.py",
        "tests/test_live_worker_control.py",
        "docs/reviews/evidence/safe-refresh-and-control-list-paging.json"
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md and AGENTS.md after this planning commit",
        "src/codex_flow/ledger.py, src/codex_flow/harness.py, src/codex_flow/control_client.py, TUI models/UI/CLI, installer and all other production/test/evidence paths",
        "schemas, migrations, contracts, entrypoints, tables, services, sockets, test partitions, prior evidence and retained input wheel bytes",
        "real service/provider/App/network/global Codex state, remotes, push, rebase, history rewrite, discard, cleanup and unrelated worktree bytes"
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer"))
    ),
    prompt=(
        "Use execute-milestone as the single Luna XHigh mutable owner for only refresh-replacement-state-and-disconnected-control-proof in the existing integration worktree. Modify exactly service.py, tui_client.py, the two named tests and the existing evidence JSON; create no path. Implement the frozen table: shutdown-0 replacement fences once; shutdown-1 replacement skips refence but stops, proves inactivity/death, revalidates and only then restores; exact predecessor absence requires three complete observations at least 50ms apart within the existing deadline plus final revalidation; all malformed, ambiguous, drift, delayed/uncertain/live and failed proof states retain harness bytes. Centralize disconnected snapshots for refresh, load-more and offline with empty state and explicit false completeness. Test the public service entrypoint exhaustively and for_state_root through authenticated disposable harness.sock, including source-fingerprint stale, endpoint failure and fresh reconnect. Keep protected regressions green. Commit source/tests first, build a fresh external wheel afterward and independently compare 33/33, then update and commit only the existing evidence JSON without self-hash. Run focused, affected, make check, diff, ancestry and parity gates. Touch no real service, provider, App, network or global state. Return one typed terminal result with exact two-commit lineage, wheel/evidence identities and both causal reviews pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000
)
```

## Recovery design — supervisor-refresh-and-runtime-compatibility-recovery

This bounded recovery supersedes the inactive `local-image-worker-input`
dispatch after its first worker was terminated by a shared-tool supervisor
refresh and its second attempt failed before SDK identity because the durable
native compatibility snapshot no longer matched the intentionally updated
plugin source.  The first attempt's implementation and green deterministic
checks are preserved in the same dirty worktree.  The superseded dispatch must
close resultlessly as cancelled before this owner starts; it is never retried
or given an invented result.

The recovery prevents a worker from updating or restarting the runtime that is
currently supervising it.  Shared activation becomes controller-owned after
the implementation worker has returned a terminal result.  A supervisor
refresh first acquires a durable shutdown fence, refuses while a worker or
controller child is active, stops through the existing authenticated IPC,
waits for the old process birth identity and service state to disappear, and
only then installs/reloads/starts and verifies the replacement.  Controller
enqueue fails closed while the fence is set, closing the preflight-to-shutdown
race without a new table, migration, daemon, transport or lifecycle owner.

Native compatibility drift before SDK thread creation is a typed profile or
configuration drift, never malformed model input.  It remains fail-closed and
requires a controller-owned superseding dispatch bound to the current verified
profile; immutable queue authority is not silently rewritten.  The current
recovery dispatch is that authorized replacement.

If the exact prior attempt reached human attention after its worker exit but
left an unconsumed result capability, the same identity-rebind transaction may
revoke only that exact generation/attempt capability after verifying durable
exited liveness and the absence of an active worker/controller owner.  A
consumed capability, missing exit proof or active lease still rejects the
rebind.  This closes the rebind-before-retry deadlock without accepting an old
result, fabricating completion or weakening capability identity.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Complete the local-image worker candidate and make shared codex-flow refresh safe, "
        "non-self-terminating and recoverable under intentional runtime compatibility changes."
    ),
    decomposition=(
        "Preserve and reconcile the existing image, CLI, diagnostics and parallel-START candidate without reimplementing already-green work.",
        "Add one durable supervisor shutdown fence that rejects refresh while worker or controller children are active and blocks new enqueue during handoff.",
        "Replace immediate systemctl restart with authenticated shutdown, bounded old-owner/service disappearance, install/reload/start and replacement health verification.",
        "Classify pre-thread native compatibility mismatch as profile/configuration drift and require a newly bound superseding dispatch rather than malformed-input retry.",
        "Prove the refresh race, active-worker deferral, enqueue fence, lease handoff, classification and resultless supersession with focused deterministic tests.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "The blocked local-image dispatch is cancelled exactly once without result, restart, successor or fabricated terminal model output before the recovery owner starts.",
        "An active worker or controller child makes shared bootstrap/refresh defer or fail before tool/plugin installation or supervisor termination; its SDK turn and durable identity remain intact.",
        "With no active child, refresh sets one durable fence, prevents enqueue, requests authenticated shutdown, waits for the old PID birth identity and unit inactivity, then installs, reloads, starts and health-checks exactly one replacement supervisor.",
        "Timeout, IPC failure, service failure or replacement-health failure is explicit and leaves no false ready claim; a subsequent bounded refresh can reconcile safely.",
        "Native compatibility mismatch before SDK identity is recorded as profile/configuration drift with human attention and is never labelled malformed_input or automatically retried against rewritten authority.",
        "The existing schema-v3 local-image input, cleaned CLI, conversation/tool summaries and same-turn disjoint START semantics remain intact; focused affected partitions and one full make check pass.",
        "The worker does not install or refresh the shared tool during its own run; after its terminal result the controller may activate once and run the authorized disposable real image and Chromium/Playwright smoke.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/service.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/control_client.py",
        "scripts/install_personal_workflow_skills.py",
        "tests/test_controller_execution.py",
        "tests/test_live_worker_control.py",
        "tests/test_service_lifecycle.py",
        "tests/test_supervisor_recovery.py",
        "tests/test_plugin_installation.py",
        "tests/test_plan_compilation.py",
        "tests/test_production_pilots.py",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md after this capsule is frozen and AGENTS.md",
        "schemas and persisted database schema or migrations",
        "src/codex_flow/backends/codex_sdk.py image and provider projection except for verification",
        "src/codex_flow/contracts.py and src/codex_flow/projection.py schema-v3 image contracts except for verification",
        "src/codex_flow/ipc.py framing and 64 KiB ceiling",
        "TUI modules, retained evidence and wheels, plugin manifests and unrelated plugin skills",
        "Codex authentication, global configuration, App state, remotes, Git history and unrelated dirty bytes",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use recover-milestone and execute-milestone as one Luna XHigh mutable owner for only "
        "supervisor-refresh-and-runtime-compatibility-recovery in the existing dirty python-sdk-controller "
        "worktree. Preserve the first local-image worker's candidate and green checks. Implement the frozen "
        "shutdown-fence architecture without a new table, migration, daemon, transport or lifecycle owner. "
        "Never install or refresh the shared tool while this worker is active; use fake/hermetic systemd and "
        "process-identity discriminators, affected partitions and one full make check. Return one raw typed "
        "terminal result to the controller, which alone owns later activation and real provider/browser smoke."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

Per-repository run state is stored under `.codex-flow/` and excluded from Git:

```text
.codex-flow/
  workflow.db
  runs/<run-id>/
    plan.json
    events.jsonl
    milestones/<milestone-id>/
      capsule.json
      result.json
      review.json
      decision-request.json
      decision.json
```

SQLite is authoritative. JSON and JSONL are atomic, readable projections for
humans, model inputs, and recovery; they never determine state transitions by
themselves.

Execution workspaces are program/lane resources rather than task resources. For
repository `<parent>/<repo>`, a managed workspace lives at
`<parent>/<repo>.worktrees/<program-slug>` or
`<parent>/<repo>.worktrees/<program-slug>-<lane-slug>` with matching
`agent/<slug>` branch. Sequential milestones, context rollover, review, repair,
recovery, and model changes reuse it while mutable ownership remains singular.

The initial state machine is deliberately small:

```text
PLANNED -> STARTING -> RUNNING
RUNNING -> NEEDS_DECISION -> RUNNING
RUNNING -> COMPLETED -> REVIEWING -> ACCEPTED
REVIEWING -> REPAIR_REQUIRED -> RUNNING
any non-terminal state -> BLOCKED | FAILED | CANCELLED
```

`BLOCKED` is the durable H2 spelling for an external prerequisite failure only;
the model/controller result contract exposes it as `EXTERNAL_BLOCKED`.
`NEEDS_DECISION` is reserved for genuinely underdetermined user intent or
missing user authority. `FAILED` means the accepted goal is not reasonably
achievable under its constraints. `CONTINUE_WITH_REPLAN` is a causal internal
event that returns to `RUNNING`, never a terminal state.

Every dispatch has logical identity
`<run-id>/<milestone-id>/<role>/<generation>`. A Codex thread id, title, host,
client queue handle, worktree path, or process id is metadata and never workflow
identity.

The SDK adapter owns the headless bundled-runtime connection boundary and
normalizes installed SDK objects/events into controller-owned types without a
running Codex App. The App-native boundary does not attach to a private or
undocumented Desktop socket: when the App is open it may emit one typed visible
task action and bind the returned thread/host identity into the same claimed
dispatch. Every worker closes directly through the harness-owned result IPC;
the App cannot close or recover a dispatch. No controller component parses raw
SDK or app-server envelopes outside these boundaries. Direct app-server
JSON-RPC remains out of scope.

Every milestone declares one or more acceptance modes: `objective`, `visual`,
and `architecture`. The controller derives implementation and review authorities
per mode rather than from filenames or one generic reviewer role. Normal
objective execution uses one executor plus an independent code reviewer. Visual
quality uses Sol for render-aware implementation and a separate Sol High
qualitative reviewer; a mixed objective/visual milestone must pass both. An
architecture gate uses Sol High. Passing one authority never implies passing
another.

Repair normally resumes the same executor. Demonstrated non-convergence creates
a diagnostic continuation for a recovery authority, which finishes locally,
changes implementation strategy, or replans and continues. It is not an
automatic terminal or user escalation. A fresh owner is also valid for proven
unavailability or explicit context rollover.

## Contracts and invariants

- The controller is the only mutable owner of workflow state, thread dispatch,
  worktree leases, routing, and recovery.
- One SQLite transaction claims a dispatch identity before any external start;
  a uniqueness constraint prevents duplicate ownership.
- `thread_started` is recorded only from a real SDK thread identity. A transport
  exception before durable identity leaves a recoverable transport state and is
  never reclassified as implementation failure.
- A task's terminal structured result is persisted before any optional
  notification. Notification failure cannot erase or change the result.
- Every state transition validates its allowed predecessor and appends a
  causally ordered event in the same transaction.
- Worktrees are created and leased by the controller from an explicit repository
  root and base commit only when isolation is required. Models never choose
  project ids, branches, or worktree paths. A fresh model thread does not create
  a fresh worktree.
- One milestone has one mutable executor lease. Review is read-only. Repairs
  resume the same executor and lease unless an explicit rollover transition is
  recorded.
- Every new or renamed durable path and code/contract identifier uses a stable,
  descriptive capability, domain, responsibility or observable-behavior name.
  This includes files, modules, classes, functions, methods, variables,
  constants, tests, fixtures, CLI commands, public exports, evidence records and
  generated artifacts. Temporary planning/control labels such as `h6_*`, `s1_*`
  and `milestone-*` never become durable names. Runtime milestone identities
  remain typed protocol data, not naming input.
- A numbered historical path or persisted schema/protocol identifier is retained
  only through an explicit exception recording the exact identifier, required
  compatibility/provenance, immutable or versioned status, and a compatibility
  check. Renames must update symbols, imports, packaging, commands, links,
  fixtures and evidence references atomically; mechanical bulk renames are
  prohibited.
- Every substantial executable milestone freezes an implementation architecture
  map before handoff. It lists exact production paths as
  `create`/`modify`/`preserve`/`remove`, module responsibilities and dependency
  direction, primary classes/protocols/public or persisted types/entrypoints,
  state/error/serialization boundaries, and a justified new-artifact budget.
  The executor may add private helpers within an owned module but may not create
  an unplanned production module, public class, registry, runner, schema,
  entrypoint or dependency edge without a bounded architecture replan.
- After the architecture map is frozen, planning must attempt to factor the
  program into independently closable vertical milestones with disjoint mutable
  surfaces. The plan records a milestone dependency DAG and current readiness,
  freezes shared schemas, public or persisted contracts, state authority and
  production entrypoints before dependent fan-out, and gives every retained
  serial edge one concrete reason: shared schema, state authority, entrypoint,
  migration order or acceptance dependency. Milestone count is not an
  optimization target; the planner minimizes the safe critical path without
  inventing fake boundaries.
- Every mutable milestone remains single-owner and normally single-context.
  Parallelism is between ready milestones, not a required nested swarm or a
  second controller inside a milestone. Read-only scouts and genuinely distinct
  review authorities may run concurrently when they do not mutate shared
  surfaces or duplicate a review lens.
- Model and reasoning mappings live only in `workflow.toml`; the controller
  validates them against the runtime before dispatch and fails closed without
  silent substitution.
- Planner, executor, code-reviewer, visual-reviewer, architecture-reviewer,
  recovery, and decision outputs use versioned typed contracts and JSON Schema
  at the model boundary. Free-form final prose is supplementary, not state
  authority.
- Acceptance modes, implementation authority, and each required promotion
  authority are durable milestone inputs. The controller may require multiple
  independent reviews for distinct modes without treating them as repeated
  review waves.
- The base reason codes are `decision_required`, `acceptance_ambiguity`,
  `contract_change`, `environment_blocked`, `review_rejected`,
  `context_rollover`, and `transport_failure`. H4 adds typed
  `continue_with_replan` and `architecture_replan` recovery outcomes without
  turning them into terminal failure categories.
- `transport_failure` is controller-owned. A surviving finding or disproven plan
  triggers diagnosis/replan; only underdetermined intent or missing user
  authority becomes `NEEDS_DECISION`.
- No polling loop is introduced. The detached supervisor blocks on local IPC
  and explicit deadlines, takes one recovery snapshot at startup, and receives
  raw terminal results directly from workers. Human inspection uses bounded
  status snapshots or explicit commands; normal execution never calls
  `wait_threads`, `read_thread` or a source-controller model.
- Existing plugin hooks and handoff skills remain available as a migration
  bridge until the SDK controller passes a real medium milestone. They are not
  extended in parallel and are not deleted based only on unit tests.
- The controller never rewrites user Git history, pushes, merges, installs a
  plugin, trusts hooks, or deletes a legacy path without separate authorization
  and the named promotion gate.
- Reuse SprintAct's general Python standards in repository-owned form: `uv`
  dependency/lock management, Ruff formatting and linting, strict root-level
  pytest, Typer for the persistent CLI, `pathlib`, precise PEP 484/695 types,
  typed boundary models, small explicit public interfaces, parameterized
  logging, no indirect access to expected interfaces, and one canonical
  execution document.
- Adapt rather than copy SprintAct policy. Legal-data, frontend, Compose,
  service, PostgreSQL-only, `httpx2`, benchmark, and product-agent rules do not
  apply here. SQLite remains the intentional controller ledger, and the Python
  baseline is selected for this repository and the stable SDK rather than
  inherited mechanically from SprintAct.

### Proportional validation and stable test partitions

Validation is change-sensitive by default. During implementation, run the
smallest discriminating tests that cover the changed behavior and its direct
consumers. At milestone closure, run every affected semantic partition plus
the named integration, packaging, service or sentinel gates required by that
capsule. Do not rerun the whole repository merely because a previous unrelated
gate exists.

The full `make check` gate is required when the capsule explicitly names it or
when a change touches shared schemas/contracts, registries/runners, a
production CLI/entrypoint, packaging/dependencies/lockfiles, test collection or
configuration, broad cross-cutting behavior, or any surface whose impact cannot
be bounded confidently. Documentation- or instruction-only work uses its
applicable formatting, link, schema, skill-metadata and executable-example
validators rather than full pytest. After a narrow repair, rerun only the gates
made stale by that repair; broader evidence may be carried forward only with an
exact changed-surface and candidate-digest justification. `git diff --check`
and complete self-review of the owned diff remain universal. An explicit
capsule gate always overrides these defaults.

The workflow-skill reconciliation milestone introduces stable repository-root
test partitions for `contracts`, `controller`, `workers`, `integrations` and
`workflow-assets`, with semantic Make targets and a validator-owned partition
manifest. Every collected repository test must belong to exactly one primary
partition, stale paths must fail validation, and `make test` remains the full
strict suite. These capability names, not milestone labels, are the durable
selection interface.

## Program scope and non-goals

In scope: the Python package and CLI, stable SDK adapter, optional App-native
projection boundary, detached repository supervisor, durable SQLite queue,
capability-bound local worker-result IPC, readable artifacts, worktree leases,
role/routing configuration, structured task contracts, deterministic tests,
prompt-input fixtures, native compatibility sentinels, packaging/service
management, documentation, and a bounded migration of the three workflow
skills.

Non-goals for v1: a general agent framework; a custom web UI; remote fleet
scheduling; automatic Git push/merge; provider-agnostic backends; direct
app-server protocol maintenance or private Desktop socket discovery; recursive
planners; subagent orchestration; periodic polling; automatic model
substitution; and deletion or disabling of the existing handoff path before
production parity. The native Codex task list/sidebar is the App-native UI;
building a second dashboard is not required for v1.

## Milestone H1 — Prove the stable Python SDK contract

### Outcome

A minimal typed package installs reproducibly and a real, read-only local
sentinel proves which stable `openai-codex` lifecycle capabilities are available
through its pinned runtime. The program either has a supported SDK contract for
the controller or stops with an exact capability gap before workflow logic is
built.

### Mutable ownership

- root `AGENTS.md`, `Makefile`, `.python-version`, `.pre-commit-config.yaml`,
  `pyproject.toml`, `uv.lock`, and `.gitignore` additions for the controller;
- `src/codex_flow/backends/codex_sdk.py` and minimal supporting domain/CLI files;
- focused SDK adapter unit tests and one opt-in real sentinel;
- controller installation and compatibility documentation;
- this canonical plan for terminal evidence only.

### Protected surfaces

- existing plugin hooks, workflow skills, manifests, validators, and audit
  skills;
- the dirty primary checkout and all unrelated worktrees/branches;
- global Codex configuration/authentication, saved tasks, hook trust, remotes,
  and downstream repositories;
- controller state machine, worktree creation, and skill-policy migration,
  which belong to later milestones.

### Implementation boundary

1. Add the smallest distributable Python 3.10+ package using `uv` and pin the
   stable `openai-codex` dependency resolved for this environment. Adapt the
   reusable SprintAct tooling into root-owned `setup`, `check`, `test`,
   `test-fast`, `format`, and `lint` Make targets; Ruff/pre-commit/pytest
   configuration; and a repository Python-version file. Do not copy monorepo,
   frontend, Compose, database, service, or legal-data tooling.
2. Add a concise root `AGENTS.md` that makes the canonical plan and controller
   ownership explicit and carries the reusable SprintAct Python rules: clarity,
   stable precise types, `pathlib`, Typer, typed boundary models, no global or
   implicit side effects, no `getattr`/`setattr`/`hasattr` for expected SDK
   interfaces, parameterized logging, parameterized SQL, secrets via
   environment only, root tooling, strict tests, and no weakened gates. State
   explicitly that SQLite is the designed local workflow ledger.
3. Inspect the installed SDK, then expose only verified operations behind a
   typed adapter: start, run/turn, thread identity, resume, event consumption,
   sandbox, model, reasoning effort, structured output, review, and structured
   skill input. Unsupported optional capabilities must be explicit typed
   capability results, not guessed methods or CLI fallbacks.
4. Add hermetic adapter tests with captured SDK-facing fakes. Keep raw SDK
   objects inside the adapter.
5. Add an opt-in sentinel that starts one read-only local thread in a disposable
   Git repository, records its real id and runtime versions, completes a
   schema-bounded response, resumes the same thread, and captures lifecycle
   events without editing this repository.
6. Exercise a second read-only review or detached-review path only if the stable
   SDK advertises it. Record unsupported status truthfully.
7. Add a manual acceptance capsule for `codex agents`, `codex resume <id>`,
   Desktop `/app`, idle wake/queue behavior, and permission-profile survival.
   These checks are compatibility evidence, not controller transport.

### Acceptance criteria

- `uv sync` installs the stable SDK and its pinned runtime reproducibly on
  Python 3.12 without modifying the user's base Python installation.
- `make check` is the canonical aggregate gate and covers Ruff format/lint,
  strict pytest, the existing plugin validator, Python compilation, and
  pre-commit without weakening the pre-existing repository tests.
- Root `AGENTS.md` contains only applicable controller/repository rules and
  explicitly rejects SprintAct-specific PostgreSQL, service, frontend, Compose,
  legal-data, and HTTP-client policy.
- Adapter unit tests cover success, SDK exception before identity, terminal
  failure after identity, unsupported capability, resume, and event ordering.
- The real sentinel records package/runtime versions, an addressable thread id,
  a successful schema-bounded turn, a successful same-thread resume, and the
  observed event sequence.
- The sentinel uses a disposable repository and read-only sandbox and makes no
  change to this repository, global config, hooks, remotes, or existing tasks.
- Model and reasoning effort are either proven through the installed SDK or
  reported as an H1 blocker. No default-model substitution is called success.
- Desktop-openability, idle wake, remote-host behavior, review delivery, skill
  input, and permission-profile survival are each labelled `proven`,
  `unsupported`, `not_exposed`, or `not_run`; none is inferred from thread
  creation alone.

### Validation and promotion gate

- `make check`
- `uv run pytest`
- `uv run python scripts/validate.py`
- opt-in real SDK sentinel with retained JSON evidence
- `git diff --check` and complete diff self-review

Promote only when required local start, schema-bounded completion, same-thread
resume, event capture, explicit model/reasoning control, and sandbox isolation
are proven against the stable SDK. A missing required capability returns
`EXTERNAL_BLOCKED` with the smallest actionable prerequisite; it does not
silently introduce CLI or direct app-server transport.

### Review requirement and successor

Self-review the adapter boundary and sentinel side effects. H2 follows only
after H1 promotion.

### Completion evidence

Status: complete and integrated as `1c04bf7`.

Evidence: `make check` passed with 29 tests, existing 10-skill validation,
Ruff, compilation, pre-commit, and clean diff checks. Real evidence is retained
at `docs/reviews/evidence/h1-sdk-sentinel.json`; the disposable repository and
controller worktree were unchanged by both read-only turns, and the sentinel's
owned thread was archived after proof.

## Milestone H2 — Implement the durable ledger and state machine

### Outcome

The controller has one typed, versioned SQLite ledger and deterministic state
machine that can claim logical ownership, record causally ordered transitions,
survive process restart, and rebuild readable artifacts without starting Codex,
creating a worktree, or depending on messages.

### Mutable ownership

- `src/codex_flow/domain.py` for H2 ids, enums, and typed records;
- new `src/codex_flow/ledger.py` and `src/codex_flow/artifacts.py`;
- a narrow internal schema/migration module only if it keeps SQL ownership
  clearer than embedding versioned SQL in `ledger.py`;
- focused H2 tests under `tests/`;
- compatibility documentation only for H2 ledger/storage contracts when needed.

### Protected surfaces

- the H1 SDK adapter, sentinel, retained evidence, CLI behavior, dependency
  versions, root tooling, and repository instructions;
- existing plugin hooks, workflow/audit skills, manifests, validators, and
  marketplace behavior;
- worktree creation, SDK execution orchestration, routing configuration,
  decisions/review/repair, notifications, and model-facing skill migration;
- the canonical plan, dirty primary checkout, global Codex state, remotes,
  downstream repositories, and unrelated worktrees.

### Dependencies

- H1 commit `1c04bf7` and its real sentinel are integrated and green.
- Use Python's standard `sqlite3`; H2 has no need for an ORM, SQLModel,
  PostgreSQL, network dependency, or SDK call.

### Implementation boundary

1. Define validated value types for run, milestone, role, generation, dispatch,
   event, and schema version. Empty or malformed ids fail before SQL.
2. Implement the exact state set `PLANNED`, `STARTING`, `RUNNING`,
   `NEEDS_DECISION`, `COMPLETED`, `REVIEWING`, `REPAIR_REQUIRED`, `ACCEPTED`,
   `BLOCKED`, `FAILED`, and `CANCELLED`, with one explicit allowed-transition
   table. Terminal states have no outgoing transition.
3. Create one versioned SQLite schema owning runs, milestones, dispatch claims,
   current state, and append-only events. Use foreign keys, uniqueness and check
   constraints, parameterized SQL, UTC timestamps, explicit transactions,
   bounded busy timeout, and a migration marker. Reject newer/unknown schemas;
   migrate older owned schemas transactionally.
4. Claim dispatch identity
   `<run-id>/<milestone-id>/<role>/<generation>` in the same transaction that
   records its initial event. Repeating the same logical claim is idempotent and
   returns the established record; conflicting ownership fails without mutation.
5. Perform every state change and its next monotonically ordered event in one
   transaction. Validate expected predecessor so stale writers cannot advance a
   milestone. Roll back both current state and event on any failure.
6. Represent pre-identity transport failure, post-identity execution failure,
   review rejection, and terminal outcomes as distinct typed reasons; none may
   silently consume or increment another category's attempt.
7. Write atomic JSON/JSONL projections under `.codex-flow/runs/<run-id>/` from a
   committed ledger snapshot. Projections are rebuildable and non-authoritative;
   partial replacement or projection failure cannot change SQLite state.
8. Add explicit open/close/reopen behavior and recovery queries for non-terminal
   dispatches. H2 must not decide whether to retry, resume, replace, or contact a
   model; it exposes facts for H3.

### Acceptance criteria

- The complete allowed and forbidden transition matrix is exhaustively tested,
  including terminal immutability and stale expected-state rejection.
- Multiple processes or connections racing for the same dispatch identity
  establish exactly one record; the loser receives the existing idempotent
  result or a typed conflict, never a second owner.
- Fault injection proves transaction rollback leaves neither a state-only nor
  event-only write and preserves monotonically ordered event sequence.
- Close/reopen tests recover identical current state, dispatch identity,
  ordered history, and non-terminal recovery facts.
- Schema tests cover first creation, same-version reopen, transactional supported
  migration, corrupt metadata, foreign-key enforcement, and newer/unsupported
  version rejection.
- Artifact tests rebuild byte-stable canonical JSON/JSONL from the ledger,
  replace files atomically, and prove write/projection failure cannot mutate the
  ledger or become workflow authority.
- Security/safety tests cover parameterized values containing SQL metacharacters,
  repository-local path validation, symlink/path-escape rejection, and no secret,
  prompt body, or raw SDK response persistence in generic ledger fields.
- H2 tests make no SDK call, create no Codex task or Git worktree, and require no
  network, credentials, global config, plugin hook, or external service.
- The local filesystem threat model treats processes running as the controller's
  Unix UID as one trusted authority. Path integrity rejects symlinks, path
  substitution, pre-existing/mid-boundary hardlinks, changed inode identity, and
  non-cooperative state observed at every owned transaction boundary. It does
  not claim atomic isolation from a malicious same-UID process that can invoke
  `link(2)` or inspect/open the controller's files between kernel syscalls; POSIX
  regular files provide no such boundary. Cross-user access remains governed by
  directory/file permissions. This is a local workflow-integrity contract, not
  a same-account hostile-process sandbox.

### Validation

- `make check`
- focused H2 tests with a temporary filesystem and multiple SQLite connections
- `git diff --check` and complete staged-diff self-review
- protected-path diff proving H1 adapter/sentinel/evidence and plugin surfaces
  are unchanged

### Promotion gate and review requirement

The implementation candidate passes every acceptance check with zero self-review
P0/P1, then receives one fresh independent read-only integrity review of the
exact commit. H2 promotes only with zero open P0/P1 on ledger correctness,
concurrency, transactions, migration safety, path safety, and scope adherence.
A rejected candidate is diagnosed and repaired against concrete findings; model
or context changes improve the approach but never form an attempt-count terminal
gate. User intervention is required only for a genuine material decision or new
authority, not because a repair is difficult.

### Current candidate

Rejected implementation candidate: `6db6146` (parent `fab4cb6`, the executor's
cherry-picked equivalent of planning commit `256233a`). The candidate changes
only `domain.py`, new `ledger.py`, new `artifacts.py`, package exports, and
focused H2 tests. Executor and planning-owner reruns of `make check` are green
with 39 tests. Independent review verdict is `REPAIR_REQUIRED`, P0=0, P1=5,
P2=1; this commit is evidence only and must not be integrated or promoted.

Required repair decisions:

- `H2-INT-001`: remove every runless milestone shorthand. Ledger operations
  require explicit `run_id` plus `milestone_id`; no lexicographic first-match or
  cross-run event aggregation is permitted.
- `H2-SEC-001`: do not attempt semantic secret detection in arbitrary strings.
  Remove arbitrary metadata/event-data and free-text reason persistence from H2,
  or replace it with explicit allowlisted typed fields and identifier-like
  diagnostic codes that cannot carry prompts, raw SDK responses, credentials,
  or general text. H1 exception messages never become durable automatically.
- `H2-SCHEMA-001`: validate the owned schema, not only table/column names. Check
  primary keys, uniqueness, checks, foreign keys, indexes, migration marker and
  version-specific schema identity; reject counterfeit or weakened v2 schemas
  and orphan/duplicate rows before use.
- `H2-SEC-002`: validate the ledger file and every relevant ancestor on every
  open/reopen, rejecting symlink or repository escape before connecting. Tests
  replace the database path after close and must prove reopen fails closed.
- `H2-SEC-003`: artifact creation and replacement use anchored directory file
  descriptors with no-follow semantics so an ancestor swap after validation
  cannot redirect any write outside the repository.
- `H2-API-001`: remove speculative aliases, compatibility names, duplicate
  wrappers, and broad exports. Retain one canonical name per H2 concept and only
  the public surface required by H1 plus the H2 plan.

Required regression expansion: exercise all 121 state pairs through the real
Ledger API; use a genuine constrained v1 fixture and migration rollback test;
cover fault injection after event insertion; cover same-identity and
conflicting-generation process races; cover alternate sensitive keys/values and
H1 exception details; cover counterfeit constraintless schemas, reopen symlink
swaps, and ancestor-swap projection attacks.

Rejected repair candidate: `d32e190` on parent `6db6146`; full H2 review range
is `fab4cb6..d32e190`. Executor, planning-owner, and independent-review reruns
of `make check` are green with 46 tests, but the second independent review found
five P1 integrity escapes and two P2 boundary defects. This commit is evidence
only and must not be integrated or promoted.

Stable closure from the second review: `H2-INT-001`, `H2-SEC-001`, and
`H2-SEC-003` are closed on the tested Linux path; the speculative aliases and
wrappers from `H2-API-001` are removed. `H2-SCHEMA-001` and `H2-SEC-002` remain
open, and the review added `H2-INT-002`, `H2-INT-003`, `H2-API-002`,
`H2-API-003`, and `H2-API-004`.

Required continuation decisions:

- Make the transition contract immutable to callers and keep Ledger behavior
  bound to that canonical immutable representation. No exported mutable mapping
  may change terminal immutability or any allowed edge at runtime.
- Reject non-string identifiers before SQL. Constructors validate the supplied
  type and value; they never stringify arbitrary objects.
- Validate event-kind/dispatch relationships before mutation. A dispatch-claim
  event requires its matching durable dispatch, and normal state-transition
  events cannot carry a dispatch id. Every successful public write must leave a
  ledger that immediately closes and reopens successfully.
- Remove public mutable access to the raw SQLite connection. The connection is
  private; tests and diagnostics use only narrow typed/read-only queries.
- Verify the canonical owned schema structurally from exact SQLite metadata and
  PRAGMAs or a recomputed canonical fingerprint, not SQL substring matching.
  Fresh and migrated v2 databases must converge on the same owned DDL identity.
- Replay every milestone history on open: sequences are contiguous, every
  `from_state` equals the replay state, every edge is allowed, dispatch-claim
  events correspond exactly to dispatch rows, and the final replay state equals
  `milestones.current_state`. Reject orphaned, duplicate, discontinuous, or
  otherwise non-causal authority before use.
- Pin the database inode before SQLite connects and anchor its parent directory
  with no-follow file descriptors. Validate file identity with `fstat` and use
  the pinned descriptor path for SQLite so a hardlink substitution between path
  validation and connect cannot redirect writes. Exercise journal, locking,
  close, failed-open cleanup, and reopen behavior on the supported Linux runtime.
- Use direct `os.O_DIRECTORY` access on the supported Linux runtime; do not use
  an indirect fallback for this security-critical interface.

Repeated Luna non-convergence on the same schema/path-integrity classes produced
a diagnostic continuation for a fresh Sol Medium recovery owner, without
weakening H2 acceptance or opening H3.

Rejected Sol continuation candidate: `a01596d` on parent `d32e190`; full H2
review range is `fab4cb6..a01596d`. It changes only `domain.py`, `ledger.py`,
`artifacts.py`, and focused H2 tests. The executor and planning owner both ran
`make check` successfully with 53 tests; focused H2 has 24 passing tests, diff
checks are clean, and the executor worktree is clean. Independent Sol High
review denied promotion with P0=0/P1=3/P2=2. This commit remains evidence only
and must not be integrated or promoted.

Stable closure from the Sol High review: `H2-INT-001`, `H2-SEC-001`,
`H2-SEC-003`, `H2-INT-003`, `H2-API-002`, `H2-API-003`, and `H2-API-004` are
closed. `H2-SCHEMA-001`, `H2-SEC-002`, and `H2-INT-002` remain P1;
`H2-API-001` remains partially open at P2 and new `H2-SEC-004` is P2.

Required Sol repair-cycle-2 decisions:

- Canonical schema identity covers the complete non-internal `sqlite_master`
  object inventory, including object type, name, owning table, and exact owned
  SQL where present. Reject every extra non-`sqlite_*` table, index, view, or
  trigger before authority; continue validating internal autoindexes through
  the exact constraint and PRAGMA checks. Fresh and migrated v2 inventories
  must remain identical.
- Audit every public mutator so a reported-successful create, claim, or
  transition re-reads and validates its exact durable row/state/event inside the
  transaction before commit. A trigger or other noncanonical object cannot
  erase or rewrite a successful result silently, even if introduced after open.
- Treat one successful first open as binding the Ledger instance to that exact
  database device/inode. Preserve the expected identity across `close()` and
  require it on `open()`/`reopen()`; never silently adopt another valid v2 file.
  Reject `st_nlink != 1` at pinning and around every write transaction, and
  revalidate the descriptor, directory entry, expected identity, and link count
  before commit so a hardlink cannot export writes outside the repository.
- Distinguish opening an existing file from atomically creating a missing file.
  Track ownership of a newly created inode. If first open fails, remove only
  that still-matching, single-link, empty/uncommitted owned inode through the
  pinned parent descriptor; never delete a substituted path or a pre-existing
  file. Preserve concurrent-first-opener behavior and close all descriptors.
- Make the transition predicate itself the single canonical policy in code,
  with no mutable/rebindable backing mapping or class attribute. Ledger calls
  that predicate directly. Any exported transition mapping is a derived
  read-only diagnostic whose mutation or rebinding cannot affect validation;
  remove `StateMachine` if it provides no distinct required contract.
- Restore root `codex_flow.__init__` exports to the exact H1 public baseline.
  H2 remains available through explicit `codex_flow.domain`,
  `codex_flow.ledger`, and `codex_flow.artifacts` modules; do not re-export H2
  records, diagnostics, policy constants, projector internals, or exception
  taxonomy from the package root without a real caller.

Ordinary bounded repair resumes the same Sol Medium execution task and worktree.
The next independent review either promotes H2 or returns concrete evidence for
diagnosis and continued repair/replan. A failed check does not itself terminate
the milestone, choose a model ladder, or require user intervention.

### Successor milestone

H3 — add controller-owned worktrees and SDK execution.

## Milestone H3 — Add controller-owned worktrees and execution

### Outcome and acceptance modes

`codex-flow plan`, `start`, `resume`, `status`, and `cancel` form the first
agent-usable vertical slice. The controller validates and leases the selected
current checkout or semantic managed Git worktree from an explicit
repository/base commit, starts or resumes exactly one SDK executor under
the effective native Codex permission/profile, runs explicit validation, and
persists a typed terminal result before any projection or notification.

Acceptance modes: `objective`, `architecture`.

### Mutable ownership

- `src/codex_flow/worktrees.py`, `controller.py`, and narrow typed additions to
  `domain.py`;
- H3 schema migration and typed ledger methods in `ledger.py` for repository,
  worktree lease, SDK thread identity, turn identity, capsule, validation, and
  terminal result facts;
- `backends/codex_sdk.py` and a narrow typed native-profile projection to
  support permission/profile inheritance and fresh-process resume through the
  proven stable high-level SDK;
- `cli.py`, artifact projections, focused tests, `workflow.toml` only if H3
  requires a minimal single-route default, and retained H3 sentinel evidence.

### Protected surfaces and non-goals

- H1 transport remains the only Codex backend; no CLI/app-server fallback;
- H2 history and schema migrations are forward-only and canonical;
- plugin/skill migration, multi-authority review, decisions, repair policy,
  automatic notifications, remote hosts, push/merge, and legacy retirement are
  H4+;
- models never select repository roots, base refs, worktree paths, mutable or
  protected paths, validation commands, model ids, or reasoning effort. Native
  Codex owns the default permission authority; a capsule may only narrow it.

### Contracts and failure behavior

1. A versioned `ExecutionCapsule` contains run/milestone identity, workspace
   mode (`current_checkout`, `existing_worktree`, or `managed_worktree`),
   absolute repository/workspace roots, semantic program/lane slug, branch,
   resolved full base SHA, normalized mutable/protected path sets, explicit
   validation argv/timeout, executor model/effort, prompt input, and a strict
   structured-output schema, and permission mode (`inherit_native` by default,
   or explicit `read_only`). Prompt bodies remain in owned capsule artifacts,
   not generic ledger metadata/events.
2. `WorktreeManager` uses argument-vector Git subprocesses, validates the
   repository and ancestors without symlinks, resolves the supplied base before
   mutation, and leases the selected workspace. When a new managed worktree is
   explicitly required, it creates only sibling
   `<repo-parent>/<repo-name>.worktrees/<program-or-lane-slug>` with matching
   `agent/<slug>` branch. Same-contract acquisition is idempotent; sequential
   milestones, rollover, review, repair, recovery, and model changes reuse it;
   conflicts cover changed repository/base/path/branch/lane/owner facts.
3. `plan` validates and durably records the capsule without external side
   effects. `start` claims the dispatch, creates/records the worktree, starts one
   SDK thread, persists its real identity immediately, executes the turn, runs
   validation, and persists the terminal result in that causal order.
4. A fresh controller process can `resume` only from durable facts. When thread
   identity exists it re-resolves native configuration and atomically persists
   the meet of prior effective permission authority and the current native
   authority before adapter creation or SDK `thread_resume`. Native tightening
   is inherited, native broadening retains the prior restriction, and changed
   provider/routing/catalog/discovery compatibility fails closed under a typed
   reason distinct from permission change. It then uses the same worktree/route.
   `start` or `resume` never creates a second dispatch, lease, worktree, or thread.
5. The unavoidable crash window after an external SDK start but before durable
   identity fails closed as an explicit uncertain pre-identity transport fact;
   automatic recovery never starts another thread. The promotion sentinel
   injects its resumable crash only after identity is committed.
6. Validation runs from explicit argv with bounded timeout in the leased
   worktree. Result status and validation observation commit before artifact
   projection. Projection/notification failure cannot change the authoritative
   result.
7. `cancel` is idempotent, terminal, and never deletes a worktree, archives a
   task, or discards Git changes automatically. `status` is read-only and emits
   stable JSON plus concise human output.
8. The SDK child uses the normal bundled app-server launch path and a typed,
   fail-closed projection of the active native Codex configuration. Provider,
   model catalog, MCP, skill, plugin, memory, project, hook, shell-environment,
   and permission semantics are preserved; model/effort/cwd remain capsule
   inputs. Mutable sessions/databases/logs use a private `CODEX_HOME`. Provider
   authentication remains environment-reference based. Literal MCP headers and
   every stdio environment entry are converted to process-only references; no
   raw value is projected or persisted.
9. `inherit_native` supplies no SDK approval or sandbox override while current
   native authority equals the execution's durable authority. A `read_only`
   capsule supplies only the stricter read-only sandbox override. Schema v8
   retains schema v7's causal baseline and schema v6's immutable native
   compatibility identity separately from sanitized effective permission facts
   and digest, and adds accepted terminal workspace authority for successor
   preflight. Before resume, the permission meet is committed atomically; an
   SDK override is supplied only when required to retain a prior stricter
   sandbox/approval authority. The lattice has no broadening value and fails
   closed if monotonic restriction cannot be established.
10. Worktree selection, mutable/protected path checks, Git-authority snapshots,
    and validation are ownership and evidence controls, not an OS containment
    boundary. H3 does not claim to contain hostile same-UID native code beyond
    the effective native Codex permissions. Repository-external effects are
    governed by that native authority and are not unconditionally reclassified
    as controller failure.

### Acceptance and validation

- exhaustive hermetic tests cover capsule/path/semantic-slug validation,
  current-checkout selection, sibling-root managed-worktree ownership, workspace
  reuse, same-contract idempotency, conflicting parallel lanes, dirty/protected path checks,
  subprocess failure, stale writers, duplicate `start`, result-before-
  projection ordering, cancel idempotency, and every crash injection boundary;
- fresh-process adapter tests prove resume initializes a new SDK client and
  rejects identity change without a CLI/direct-RPC fallback;
- deterministic parity tests prove a blank private home selects built-in
  `openai`, the projected profile selects exact `codex-lb` provider facts,
  unrestricted and restricted native profiles are inherited on the next
  execution, no broadening mode exists, global config bytes are unchanged, and
  explicit read-only mode is the only SDK permission override;
- crash/concurrency regressions prove `danger-full-access` to `read-only`
  resumes exactly once under read-only after durable rebind,
  `read-only` to `danger-full-access` remains read-only, incompatible native
  compatibility changes make no adapter/external call, and capsule read-only
  remains monotonic;
- no-follow runtime-home regressions cover existing and ancestor symlinks,
  non-directory/device substitution, clean private creation, and unchanged
  external target content/metadata; cancelled/terminal executions reject every
  public turn/checkpoint mutator after close/reopen;
- recursive discovery regressions reject root, parent, nested, multi-node, and
  dangling cycles on all three native surfaces before adapter construction;
- managed and existing worktree regressions cover empty `.codex-flow` and
  out-of-scope directory creation/removal/rename, while mutable-root empty
  topology remains usable under deterministic scan bounds;
- close/reopen successor regressions reject predecessor output content,
  deletion, type, symlink, hardlink, empty-directory, and Git-authority drift
  before baseline/lease/adapter authority;
- lexical, relative, trailing-component, and symlink workspace aliases share
  one physical lease and thread identity; noncanonical legacy keys fail closed;
- structural config regressions scan alternate header and secret values across
  private runtime files, ledger/artifacts, exceptions, and repository bytes,
  while the active native profile still loads through the pinned runtime;
- a disposable real Git repository sentinel plans and starts a bounded editing
  milestone, injects a controller stop after durable SDK identity, resumes in a
  fresh process, runs validation, and reaches one terminal structured result;
- retained evidence records one dispatch, one worktree lease/path, one SDK
  thread identity, a semantic program/lane workspace reused across resume,
  ordered turns/events, before/after protected-path hashes,
  validation output digest, final Git diff/commit facts, sanitized effective
  provider/profile/permission facts and digest, unchanged global native config,
  and no duplicate owner;
- `make check`, focused H3 tests, `git diff --check`, complete diff self-review,
  and zero open P0/P1.

### Promotion gate and successor

Promote only when the real disposable sentinel survives the injected post-
identity crash and finishes through `codex-flow resume` with no duplicate thread
or worktree and a durable terminal result. Deterministic boundary fault injection,
not the retained real sentinel alone, proves result-before-projection ordering.
H4 follows.

Successor: H4.

### Completion evidence

Status: H3 implementation, deterministic validation, and the corrected real
sentinel are complete. On 2026-08-25 the user accepted exact candidate
`544c1c8` as pilot-ready and explicitly unblocked H4 rather than continuing an
open-ended adversarial review/repair loop. This is a successor-safety decision,
not a claim that H3 is finally production-promoted or defect-free: concrete
findings reached by H4 remain classified by severity and promotion impact, and
the integrated H6 system gate retains cross-cutting hardening authority.
The user superseded the mandatory Bubblewrap containment contract: H3 now
inherits the same native Codex permission authority and automatically follows
later native restrictions. The bounded Git-authority snapshot remains required
for HEAD/branch, refs, reflogs, index, repository/worktree configuration, and
stable operation metadata, but it is evidence/contract enforcement rather than
an OS sandbox claim. Schema v8 retains schema v7 per-milestone baselines and
causal turn-start authority, and adds accepted terminal HEAD/content facts for
predecessor verification while preserving schema v6 monotonic permission and
Git-authority evidence. The single corrected real SDK run reached
the first SDK turn and injected post-turn crash boundary, then fresh-process
resume failed because the native runtime had legitimately added private config
state and reprojection treated that private change as source drift. The
launcher now atomically restores the validated projection for each fresh SDK
process, with a regression covering runtime-added private config. The user then
authorized one new run for exact commit `04e0f889678770bfecd7da3c0a8cbd03344f123f`.
That single Python-SDK/bundled-app-server sentinel passed through `codex-lb`
with inherited native `danger-full-access`/`never` authority, one dispatch and
thread identity, injected post-turn crash, fresh-process resume, an allowed
workspace edit, equal Git-authority digests, and unchanged source Codex config
bytes. Retained evidence records the sanitized provider/profile/permission
facts and digests without secrets. External-write denial is not an H3 gate.
The objective review of `3014c568824e3f5d1474766f6b17f5b1dddddf49`
returned P0=0/P1=2/P2=2. The repair makes native permission changes monotonic
across resume, moves runtime-home setup to descriptor-anchored no-follow
creation, preserves terminal cancellation immutability, and updates the schema
and compatibility truth. The retained real sentinel was not rerun: the repair
does not change its proven SDK/provider/model route, while permission tightening
and unsafe-path rejection are deterministic gates and global native config is
protected.

## Milestone H4 — Add decisions, review, repair, and limits

Outcome: each milestone carries explicit objective/visual/architecture
acceptance modes; routing produces separate executor, code-reviewer,
visual-reviewer, architecture-reviewer, and recovery authorities as required.
Material decision requests wake a bounded planner turn; repair resumes the
original executor when useful; stable finding identities and causal classes
drive diagnosis rather than attempt counts; limits and budgets live in
`workflow.toml`.

H4 is delivered in two independently useful slices. H4-A is the early walking
skeleton: one objective milestone is executed, independently reviewed,
rejected, repaired by the same durable owner, and accepted through the
production SDK controller without callback authority. H4-B then adds distinct
visual and architecture authorities, bounded planner decisions, and the
complete H4 promotion scenario. H4-A must change observable repository behavior
before H4-B deepens the policy surface.

Acceptance: completed, continue-with-replan, needs-decision, repair-required,
external-blocked, context-rollover, and transport-failure scenarios have typed
deterministic integration tests. Replanning continues automatically when intent,
public/persisted contracts, security/privacy boundary, material cost,
destructive behavior, and scope are unchanged. Review is read-only; mixed
acceptance modes require every distinct authority; new reviewer scope cannot
masquerade as a surviving finding; thread and compaction limits fail closed.
Every finding records severity separately from `promotion_blocking`, a concrete
reason, and `defer_to` when non-blocking. A gate blocks only dependent successor
work; isolated non-propagating findings remain visible for the system gate.

Promotion gate: one end-to-end disposable milestone exercises objective and
visual review, review rejection, automatic architecture replan, repair, and
acceptance with durable evidence and no callback dependency. Separate scenarios
prove `NEEDS_DECISION`, `EXTERNAL_BLOCKED`, and `FAILED` are mutually distinct
and that implementation difficulty alone produces none of them.

Mutable ownership: `workflow.toml`, `config.py`, H4 domain/result schemas,
controller/ledger/artifact extensions, role prompt templates, focused tests, and
retained H4 evidence. H3 worktree and SDK transports are extended, never
duplicated. Plugin skills remain protected until H5.

Implementation boundary:

- `workflow.toml` owns model/effort mappings for planner, executor,
  code-reviewer, visual-reviewer, architecture-reviewer, and recovery roles plus
  turn, repair, compaction, validation, and wall-clock limits. Runtime validation
  rejects unavailable routes without substitution.
- Capsules declare required acceptance modes. Objective review uses a fresh
  read-only Luna reviewer; visual-quality review uses a fresh read-only Sol High
  reviewer over fixed rendered evidence; architecture review and recovery
  diagnosis use Sol High. Multiple modes require all distinct authorities.
- Typed reviewer output carries stable finding id, causal class, severity,
  promotion impact and reason, evidence, acceptance criterion, optional
  `defer_to`, and whether it survives the exact prior repair. New scope cannot
  impersonate a surviving finding.
- A valid rejection resumes the executor/lease when useful. Non-convergence
  creates a diagnostic continuation. Recovery returns finish-local,
  change-strategy, `CONTINUE_WITH_REPLAN`, `NEEDS_DECISION`,
  `EXTERNAL_BLOCKED`, or `FAILED`; only the last three are terminal/user-visible
  outcomes, with the semantics frozen above.
- A bounded planner turn answers `NEEDS_DECISION` only when existing intent can
  resolve it; genuinely underdetermined product/contract/authority decisions are
  persisted for the user. Notification remains non-authoritative.
- Limits fail closed before another external turn, preserve the last durable
  checkpoint, and never silently change model, effort, sandbox, or acceptance.

H4-A implementation boundary and gate:

- add canonical role/limit configuration plus typed objective-review finding,
  repair, recovery, decision, budget, and lifecycle contracts to the existing
  controller/ledger/artifact path; do not duplicate H3 transport, workspace, or
  state authority;
- implement objective execution -> read-only review -> rejection -> same-owner
  repair -> fresh read-only re-review -> acceptance, with stable finding
  identity, explicit promotion impact, causal event history, and durable result
  before projections;
- exercise `CONTINUE_WITH_REPLAN`, `NEEDS_DECISION`, `EXTERNAL_BLOCKED`,
  `FAILED`, context rollover, transport failure, stale review, and exhausted
  limits hermetically, keeping their semantics mutually distinct;
- run one bounded real disposable objective pilot through the Python SDK and
  `codex-lb`, producing an observable repository edit, one deliberate review
  rejection, one repair, final acceptance, and retained sanitized evidence;
- close H4-A with focused/full gates, self-review, and zero findings that are
  promotion-blocking for H4-B. Do not require another broad H3 review or label
  isolated hardening as blocking merely because it is severe.

H4-B implementation boundary and gate:

- derive distinct executor, objective/code-review, visual-review,
  architecture-review, recovery, and decision authorities from capsule
  acceptance modes plus `workflow.toml`; reject unavailable or aliased required
  authorities without model, effort, permission, provider, or transport
  substitution;
- keep objective review on the accepted H4-A lifecycle and add fixed rendered
  evidence plus an independent visual authority, an independent architecture
  authority, and bounded recovery/planner decisions on the same controller,
  SQLite authority, lease, checkpoint, finding, and projection path;
- require every declared mode to accept. Preserve stable finding and repair
  lineage across authority changes, distinguish new scope from surviving
  findings, and retain a non-blocking finding without blocking an unrelated
  successor gate;
- make a rejected architecture finding produce a durable bounded
  `CONTINUE_WITH_REPLAN`, then execute the accepted revised strategy without
  changing user intent, public/persisted contracts, security/privacy boundary,
  material cost, destructive authority, or milestone scope. Only genuinely
  underdetermined or externally blocked cases may leave the autonomous path;
- run one bounded disposable real multi-authority pilot through the Python SDK
  and `codex-lb`: objective execution, independent objective and visual review,
  an architecture rejection, bounded recovery/replan, same-workspace repair,
  fresh distinct-authority re-review, and final acceptance over an observable
  repository edit and fixed rendered evidence. Retain sanitized causal facts;
- close H4 with focused/full gates, clean worktree, truthful compatibility and
  retained evidence, and zero findings that are promotion-blocking for H5.
  H4-B does not package plugins, simplify skills, run H6 medium/large pilots,
  retire legacy behavior, or reopen a broad H3/H4-A audit.

Validation includes hermetic scenario tests for every role/mode/result,
surviving-versus-new findings, recovery replans, budget exhaustion, stale
reviewers, notification failure, and result ordering, plus one disposable real
SDK milestone that is rejected, repaired, separately code/visual reviewed, and
accepted with durable evidence.

### Completion evidence

Status: H4 is complete and accepted at implementation commit `662d006`.
H4-A established the objective rejection/repair/re-review walking skeleton;
H4-B derives distinct executor, objective/code, visual, architecture, recovery,
and decision authorities from capsule modes and `workflow.toml`, requires every
declared authority to accept, and fails closed on missing or aliased roles.

The retained real multi-authority pilot at
`docs/reviews/evidence/h4-b-multi-authority-pilot.json` used the Python SDK and
`codex-lb` on one reused thread for two turns. It made an observable authorized
edit, fixed rendered evidence by digest, passed independent objective and
visual review, received an architecture rejection, durably recorded a
boundary-preserving `CONTINUE_WITH_REPLAN`, repaired in the same workspace, and
reached `ACCEPTED` after fresh distinct-authority re-review. Evidence contains
only sanitized hashes, counts, route, lifecycle kinds, and terminal status.
Planning independently reran the 11 focused H4 tests and full `make check` with
286 tests plus formatting, lint, cross-skill validation, compileall, and
pre-commit; the exact worktree is clean and H4-A evidence is unchanged. No
finding is known to block H5. This closes H4 without claiming H5 packaging or
H6 integrated-pilot/retirement work.

Successor: H5.

## Milestone H5 — Reduce workflow instructions to cognitive roles

Outcome: `plan-work` writes plans/capsules, `execute-milestone` executes one
capsule and writes one result, and a small `workflow-control` skill invokes the
controller. Routing, recovery, callbacks, and state-machine prose are removed
from model-visible skills and owned once by code/configuration.

Acceptance: `codex debug prompt-input` fixtures show one routing policy, explicit
acceptance modes, distinct code/visual/architecture authorities, completion-
biased recovery, the intended skill only, protected surfaces and acceptance
retained, no obsolete handoff protocol in executor prompts, and an enforced
prompt-size budget.

Promotion gate: existing direct workflow remains reachable under an explicit
legacy command while the controller path passes all repository validators and
one medium real milestone.

Mutable ownership: the three workflow skills, a new minimal `workflow-control`
skill, plugin manifest/version/marketplace metadata as required, prompt-input
fixtures, CLI help/docs, controller model-facing schemas, and H5 tests/evidence.

Implementation boundary:

- `plan-work` owns intent, decomposition, acceptance modes, and decision-ready
  capsules; `execute-milestone` owns only implementation judgment inside one
  capsule; `workflow-control` invokes the controller and reports durable status.
- Remove native task creation, callback, recovery, model-routing, attempt-loop,
  and ledger prose from model-visible skills only after the controller path is
  reachable and proven. Keep one explicit legacy command during H5/H6; no hidden
  dual routing.
- Typed Python is the model-facing authoring language for capsules/results;
  JSON/JSONL remains controller serialization. Prompt fixtures must contain the
  intended skill once, acceptance/protected surfaces, and no obsolete peer-
  transport policy.
- Enforce a measured prompt-size budget and compare before/after prompt inputs.
  Validator success alone is not behavioral proof.

Validation: repository/plugin validators, focused prompt fixture assertions,
CLI help and package install, legacy reachability, `make check`, diff checks,
and a real medium milestone initiated by a Codex agent through
`workflow-control`/`codex-flow` rather than native peer tools.

### Completion evidence

Status: H5 is complete and accepted at implementation commit `b57a353`.
`plan-work` and `execute-milestone` now expose only their bounded cognitive
contracts, the new `workflow-control` skill owns controller invocation and
durable status, and `$codex-thread-handoff` remains an explicit separately
selectable legacy route that cannot be mixed into the controller path. Plugin
source metadata advances to `0.1.5+codex.20260825000000`; no installed plugin or
global Codex state was mutated.

Prompt fixtures measured `plan-work` at 3,063 bytes versus 14,193 before,
`execute-milestone` at 3,043 versus 14,310, and `workflow-control` at 1,716
under its 4,000-byte cap. The retained real medium pilot at
`docs/reviews/evidence/h5-workflow-control-medium.json` was initiated through
`workflow-control`/`codex-flow`, used Luna/medium via the canonical controller,
made only its authorized repository edit, preserved the protected file, and
reached `result_durable`/`completed`. Planning independently verified the six
focused H5 tests, CLI help, isolated wheel build, exact evidence, diff hygiene,
full `make check` with 292 tests and 11 validated skills, and a clean worktree.
No finding is known to block H6.

Successor: H6.

## Milestone H6 — Production pilot and legacy retirement decision

### H6-R prerequisite — make the model-facing control boundary reachable

Before the production pilots, repair the proven H5 reachability gap: `codex-flow
schema` publishes `ModelFacingCapsule`, while `codex-flow control` currently
loads only the repository-bound `ExecutionCapsule` projection. The bootstrap
repair is implemented directly in this existing worktree because the broken
boundary cannot execute itself. Its promotion proof must then invoke the fixed
`codex-flow control` entrypoint with a serialized `ModelFacingCapsule`; no native
peer task or `$codex-thread-handoff` is part of this route.

Outcome: `control` accepts the exact closed model-facing schema, validates its
declared acceptance authorities against `workflow.toml`, and deterministically
projects it into the single existing controller execution path. The projection
owns stable run/milestone identity, the selected checkout's physical Git facts,
the executor route, inherited native permission, default bounded validation,
and the model-facing result schema. Existing repository-bound execution
capsules remain an explicit internal/controller compatibility input for fixed
H3/H5/H6 pilots; malformed or ambiguous shapes fail before durable state.

Mutable ownership: one new projection boundary module, the narrow `control`
CLI integration, focused H5/H6 reachability tests, compatibility documentation,
and this canonical plan. Preserve the H6 CLI/ledger/test changes already in the
worktree and integrate without discarding or rewriting them.

Protected surfaces: controller/ledger state semantics, worktree manager, SDK
adapter and native-profile projection, workflow routes and limits, plugin
installation/cache/trust, global Codex state, primary checkout, remotes,
downstream repositories, and the explicit legacy handoff source.

Acceptance modes: `objective` and `architecture`, with distinct
`code-reviewer` and `architecture-reviewer` authorities. A model-facing capsule
round-trips through `control` into one durable execution; identical input has
stable identity and cannot duplicate work; route and authority drift fail
closed; invalid input creates no controller state; protected and pre-existing
worktree changes remain unchanged; focused tests and full `make check` pass;
and the installed wheel is refreshed and proves the same model-facing command
against a disposable repository before H6 pilots begin.

Non-goals: redesigning H4 orchestration, changing model routes or limits,
inventing a second transport, weakening mutation/protected-surface checks,
installing or trusting plugin hooks, deleting legacy behavior, or modifying the
user's dirty primary checkout.

### H6-C prerequisite — Git-native workspace policy and App-native dispatch

The R6A production pilot proved the SDK/controller path can complete a real
milestone and independently promote its repository outcome, but the controller
misclassified Git-ignored frontend build output as protected or out-of-scope
source mutation. The same pilot also proved that a thread created by the
controller's private SDK app-server is not automatically a visible task in the
active Codex desktop app. H6-C closes both product gaps before replacement
pilots continue.

Outcome: workspace integrity follows Git authority, and a second App-native
hosting mode makes controller-owned workers visible as ordinary Codex app
conversations without weakening the durable harness. Tracked files are checked
for protected integrity; tracked changes and non-ignored untracked files are
checked for mutable-scope ownership; Git-ignored files and directories are
excluded from ordinary source-mutation and directory-topology deltas. Explicit
sensitive runtime/configuration roots remain protected by their existing
dedicated integrity mechanisms rather than by accidental inclusion of all
ignored build output.

The App-native boundary is host-mediated, not a second scheduler or an
undocumented socket client. The controller transactionally claims a logical
dispatch and emits one closed action containing route, selected existing
workspace, bounded prompt, and result contract. The hosting Codex app performs
the native non-blocking task creation and returns its thread/host identity; a
closed bind operation records that identity against the exact outstanding
claim. Repeated prepare/bind calls are idempotent, conflicting or invented
identities fail closed, and status/recovery continue to use SQLite. SDK-headless
execution remains supported and cannot be silently substituted for an
App-native request.

Mutable ownership: `src/codex_flow/controller.py`, the narrow App-native
boundary and typed contracts, required ledger/schema and CLI integration,
`src/codex_flow/projection.py`, the source `workflow-control` skill, focused
tests and compatibility documentation. H6-C owns integration and validation of
the existing uncommitted H6-R candidate; it must preserve all unrelated user
changes and retained evidence.

Protected surfaces: this canonical plan and repository instructions; native
Codex configuration/authentication/permissions; installed plugin caches and
trust; the primary checkout, remotes, downstream repositories and legacy
handoff source; existing accepted H1-H5 evidence; SDK provider/config/discovery
projection; and all paths outside the declared mutable set. No worker may push,
merge, rebase, stash, discard, install/trust a plugin, or discover/attach to a
private Desktop app-server endpoint.

Acceptance modes: `objective` and `architecture`, with distinct
`code-reviewer` and `architecture-reviewer` authorities. Objective acceptance
requires regression proof that `.next`, `node_modules` symlinks, caches and
other ignored build output neither change protected digests nor appear in
ordinary mutation/topology scope, while tracked protected edits and
non-ignored untracked out-of-scope files still fail. It also requires closed
prepare/bind/status contracts, stable logical identity, exact route/workspace
projection, no duplicate ownership, and headless compatibility. Architecture
acceptance requires one SQLite authority, one implementation owner, no direct
Desktop socket/app-server protocol dependency, fail-closed host acknowledgments
and explicit separation between App-native and SDK-headless dispatch.

Promotion gates: focused adversarial tests, full `make check`, Ruff and
`git diff --check`, complete diff self-review with zero P0/P1, wheel/package
verification, and one bounded real App-native pilot launched from a visible
Codex app thread. The pilot must bind a real native thread id, remain visible in
the app, use the selected existing worktree without creating a duplicate, make
only its authorized change, and persist a terminal typed result. If the current
host does not expose a required native task action, report that exact capability
as `EXTERNAL_BLOCKED`; do not fall back to the private SDK server and claim UI
visibility.

Non-goals: a custom run dashboard, automatic sidebar manipulation, remote fleet
scheduling, autonomous multi-milestone polling, SDK removal, legacy deletion,
plugin installation/trust, or weakening explicit sensitive-path protection.
H6-C's historical successor was H6-D. The definitive App-independence
requirement supersedes that route; H6 resumes with fresh medium and large pilots
only after H6-E promotes under the detached-supervisor acceptance contract.

### Milestone H6-D — superseded donor: App ref and raw-envelope normalization

The bounded visible H6-C pilot proved the host-mediated task path, real visible
thread binding, selected-worktree reuse and the requested one-line repository
edit. It also proved two App-native promotion defects. First, Codex App writes
host-owned checkpoint and capture refs below `refs/codex/turn-diffs/**` in the
repository's shared common Git directory during an ordinary turn; the v1 Git
authority hashes that volatile namespace and therefore converted the otherwise
valid pilot into `integrity_failure`. Second, the native `create_thread` action
does not accept `output_schema`: the worker returned usable JSON only because
the host improvised the contract and terminal ingestion used a hand-constructed
projection. `wait_threads` is a deliberately compact progress/summary surface,
not the lossless terminal message authority.

This milestone was planned at commit `678105a` but was superseded before
execution by the definitive App-independence requirement. Its uncommitted donor
diff is not a separate implementation owner and must not be discarded or
rewritten before the H6-E executor captures its exact bytes. H6-E absorbs the
following useful, still-required pieces:

- `src/codex_flow/controller.py`: exact semantic normalization of only
  `refs/codex/turn-diffs/**`, including shared-common-directory ref/reflog
  treatment, plus the raw-result completion seam;
- `src/codex_flow/contracts.py`: bounded strict parsing of one complete raw
  `ModelFacingResult`, the canonical schema digest and the in-band result
  envelope formatter;
- `src/codex_flow/app_native.py`: readable version-1 actions and a version-2
  action that binds the result-contract digest without claiming native
  `create_thread` accepts `output_schema`.

The donor's working-tree SHA-256 values at planning time are
`controller.py=38a591b1e5c4712013a9b3acd470b40501900a35c38f74623998f1298c5a0c1d`,
`app_native.py=4eeefe47032ceb5ae920f5ae16981065f47a359b8216dfbd1a8f1b139dfc7d27`
and
`contracts.py=1b4c278d66010dc0105d21612d2b77acfc8ae0b98595c8acc85abe4d7d5cb3ea`.
The planning task preserves all three byte-for-byte. The H6-E implementation
owner may integrate and repair them inside its declared mutable surfaces after
capturing the baseline.

The remainder of the old design is rejected: App-host `read_thread`,
`wait_threads`, a host-authored result object, a source-controller callback, or
an open App cannot be lifecycle, liveness, recovery or terminal-result
authority. The detailed H6-D acceptance text below is retained only as donor
history; H6-E is the sole executable milestone and its stricter gates win.

Historical H6-D outcome: fix exactly those two defects without weakening any other controller
boundary. Git authority semantically normalizes only the exact App-owned
`refs/codex/turn-diffs/` namespace, whether refs are loose or packed and whether
their reflogs live in the shared common Git directory. `HEAD`, the checked-out
branch, index, config, ordinary refs and reflogs, history/operation state,
protected paths, tracked mutations and non-ignored out-of-scope mutations remain
fail-closed. App-native action preparation no longer represents
`output_schema` as an argument accepted by native task creation. Instead, one
controller-owned formatter appends a compact canonical `ModelFacingResult`
schema-version-1 JSON envelope/template and the instruction to emit that JSON
object alone, without prose or a Markdown fence, to the native prompt.
Terminal ingestion accepts the full raw terminal `agentMessage` text for the
exact bound thread, applies the existing bounded strict JSON decoder to the
entire text, constructs `ModelFacingResult`, and only then enters the existing
capability-, identity-, validation- and integrity-checked durable completion
path. A `wait_threads` summary, excerpt, commentary item, inferred object or
host-rewritten dictionary is never terminal-result authority.

Design and ownership:

- `git_authority_snapshot()` keeps its bounded `HEAD`, current branch, Git-dir,
  index, config and operation-state facts. Replace raw all-ref hashing with one
  canonical, bounded ordinary-ref projection derived from Git's ref inventory:
  parse complete ref records strictly, discard a ref only when its full name is
  exactly below `refs/codex/turn-diffs/`, sort and hash every remaining ref name,
  object id and symbolic target. Apply the same exact-subtree exclusion to
  loose-ref and ref-log metadata in the common directory; do not use prefix,
  substring or general `refs/codex/**` exclusions. Packed ordinary refs remain
  protected through the semantic inventory. `HEAD` and ordinary reflogs retain
  transient-history detection, including commit-then-reset.
- The canonical result-envelope formatter lives beside
  `ModelFacingResult`/`model_facing_result_schema()` and is the sole source used
  by App-native prompt construction and its tests. New App-native actions use a
  versioned host contract that binds the result-contract digest but does not
  instruct the host to pass `output_schema` to `create_thread`. Existing
  persisted version-1 action/status rows remain readable and recoverable; no
  second result type, transport, scheduler or ledger authority is introduced.
- The `codex-flow app-result --agent-message PATH` CLI/controller terminal
  boundary reads one bounded raw UTF-8 `agentMessage` payload; the former
  host-authored `--result` JSON-file input is not an alternate authority. It
  rejects BOMs, invalid UTF-8, leading/trailing prose,
  Markdown fences, concatenated JSON, non-object roots, missing/extra keys,
  invalid enums/types and over-limit content, and then delegates the typed
  result to the unchanged `complete_app_native()` authority. Host orchestration
  must obtain the final full message through native `read_thread` for the bound
  `thread_id` and copy its complete raw `agentMessage` text without rewriting;
  `wait_threads` may wait for lifecycle state but its summary text must never be
  ingested.

Mutable ownership:

- `src/codex_flow/controller.py` only for the exact Git-authority normalization
  and raw-result completion entrypoint;
- `src/codex_flow/app_native.py`, `src/codex_flow/contracts.py`,
  `src/codex_flow/projection.py` and `src/codex_flow/cli.py` only for the
  versioned native action, canonical prompt envelope and raw `agentMessage`
  ingestion boundary;
- `tests/test_h3_controller.py`, `tests/test_h5_workflow_control.py`,
  `tests/test_h6_app_native.py` and new focused fixtures required by the two
  defects;
- `docs/reviews/evidence/h6-d-app-native-pilot.json` as the one sanitized,
  retained visible-pilot record.

Protected surfaces:

- `docs/reviews/peer-thread-workflow.md`, `AGENTS.md`, `workflow.toml`, the
  existing uncommitted line in
  `docs/reviews/codex-controller-compatibility.md`, and every other pre-existing
  user change;
- ledger schema/state semantics and `src/codex_flow/ledger.py`; worktree lease,
  mutation-scope and protected-digest behavior outside the owned
  `controller.py` functions; `src/codex_flow/worktrees.py`,
  `src/codex_flow/domain.py`, `src/codex_flow/config.py`,
  `src/codex_flow/native_profile.py`, `src/codex_flow/backends/codex_sdk.py`,
  `src/codex_flow/h6_pilot.py`, all plugin sources, accepted H1-H5 evidence and
  unrelated H6-C behavior;
- installed plugin caches/trust, global Codex configuration/hooks/state, the
  primary checkout, remotes, downstream repositories, legacy handoff source,
  and every path outside the mutable set. No push, merge, rebase, stash,
  discard, plugin installation/trust, legacy disablement/deletion or private
  Desktop socket/app-server discovery is authorized.

Non-goals: broad Git-ignore policy changes; excluding all `refs/codex/**`, all
unknown host refs, ordinary ref logs or packed refs; weakening current-branch,
index, config, history, protected-path or mutation-scope checks; changing
`ModelFacingResult` fields/status semantics; adding native structured-output
support that the host action does not expose; parsing task summaries; changing
routes, budgets, SQLite schema or SDK-headless structured-output behavior; and
running the medium/large H6 parity pilots or making the legacy-retirement
decision.

Acceptance modes: `objective` and `architecture`. The objective authority is
the independent `code-reviewer` (Luna XHigh); the architecture authority is the
independent `architecture-reviewer` (Sol Medium). Both must review the complete
H6-D candidate once, with at most one bounded repair for concrete blockers.
Promotion requires P0=0/P1=0 from both authorities; one authority cannot waive
the other.

Objective acceptance and regression validation:

- In a repository with at least two worktrees sharing one common Git directory,
  create, update and delete loose and packed refs plus reflogs strictly below
  `refs/codex/turn-diffs/**` between baseline and completion. The normalized Git
  authority and a valid App-native completion remain stable. Prove exact-name
  discrimination: `refs/codex/turn-diffs-evil/**`, `refs/codex/other/**`,
  heads, tags and remotes still change the digest and fail completion.
- Retain or extend adversarial regressions proving changes to `HEAD`, current
  branch, index, local/worktree config, ordinary reflogs, merge/rebase/sequencer
  state and commit-then-hard-reset history fail closed. Tracked protected edits,
  tracked out-of-scope edits, non-ignored untracked out-of-scope files and
  terminal-capture races still fail; Git-ignored build-output behavior remains
  unchanged.
- Assert that a newly prepared App-native host action maps only supported
  native creation inputs and carries no `output_schema` argument. Its prompt
  contains exactly one deterministic canonical envelope/template whose digest
  is bound by the closed action/receipt contract; SDK-headless turns continue
  to receive `output_schema` exactly as before. Persisted version-1 App-native
  actions remain readable without being silently reinterpreted as version 2.
- Feed terminal ingestion the full raw valid `agentMessage` and prove one typed,
  idempotent durable result for the exact bound host/thread/capability. Reject a
  truncated or summarized `wait_threads` projection and every malformed form
  listed above before ledger mutation; compare database bytes/state before and
  after each rejection. Reject a correct message for the wrong host/thread or
  stale/cancelled dispatch through the existing closed checks.
- Run the focused H3/H5/H6-D tests, Ruff on every changed Python path,
  `git diff --check`, the complete `make check`, exact dirty-baseline/status and
  protected-surface comparisons, and a full candidate self-review. Preserve the
  existing compatibility-document insertion byte-for-byte.

Packaging and install acceptance: after source gates pass, build one wheel into
a fresh temporary directory; run `uvx --from <exact-wheel> codex-flow --help`,
schema output and the focused App-native prepare/raw-ingest checks against a
disposable Git repository. Then install that exact wheel into fresh temporary
`UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` roots and repeat the same CLI checks from the
installed executable. The isolated install must not mutate the user's normal
tool directory, plugin cache/trust, global Codex state or the live H6 ledger,
and source-tree imports must be unavailable during the wheel/install checks.

Architecture acceptance: one SQLite/controller authority and one
implementation owner remain; the exclusion is exact, semantic and bounded
rather than a general App trust bypass; shared-common-dir/worktree behavior is
explicit; v1 action recovery is preserved; the prompt schema/template and
strict parser have one source of truth; raw response ownership is the exact
bound thread's full terminal `agentMessage`; summaries and host-created result
objects are non-authoritative; SDK-headless behavior remains separate; and no
private Desktop/app-server protocol, second transport or schema migration is
introduced.

Visible promotion pilot: run exactly one bounded App-native task from the
installed isolated wheel against a disposable Git repository/worktree. Bind its
real visible native thread, make one declared sentinel-file edit, wait only for
lifecycle completion, read the full raw terminal `agentMessage` from that exact
thread, and ingest that raw text. Retain sanitized evidence of native identity,
supported create inputs, raw-message digest/length (not lossy summary text),
the exact `refs/codex/turn-diffs/**` before/after delta, unchanged normalized Git
authority, successful protected/mutation/validation checks and durable
`COMPLETED`. Confirm the selected worktree gained no duplicate and the source
worktree's pre-existing compatibility-document change and all other dirty bytes
are unchanged. Failure to expose the full raw terminal message is
`EXTERNAL_BLOCKED`; do not substitute `wait_threads`, inferred JSON or the SDK
server. This historical promotion route is superseded; H6 medium/large parity
pilots resume only after H6-E promotion.

### Milestone H6-E — detached supervisor and App-independent completion

#### Outcome and acceptance modes

`codex-flow control` durably queues a workflow and returns after the harness has
accepted ownership. A harness-owned detached supervisor, running as the same OS
user but independently of the invoking terminal and Codex App, executes the
queue, accepts worker results, closes milestones and schedules already-authorized
successors. SQLite remains the only lifecycle and result authority. The source
controller model spends zero tokens and makes zero lifecycle tool calls while a
worker runs.

The milestone declares `objective` and `architecture` acceptance. Luna XHigh is
the independent objective/code authority; Sol Medium is the independent
architecture, lifecycle and security-boundary authority. Both review the exact
candidate once; at most one bounded repair addresses concrete P0/P1 blockers.
Promotion requires P0=0/P1=0 from both. No App-visible or notification success
can waive a durable harness gate.

#### Fixed process and authority model

- The invoking `codex-flow` CLI is a short-lived producer/status client. It
  validates and projects the capsule, commits a queue item, wakes the supervisor
  through local IPC and exits. It never owns the worker after enqueue.
- One repository-bound supervisor owns queue claims and state transitions for
  that repository's `.codex-flow/workflow.db`. It is a deterministic Python
  process, not a model turn. A durable epoch/lease row plus verified PID/process
  birth identity prevents two live supervisors; a stale lease may be stolen
  only after expiry and a liveness check. PID files and socket existence are
  diagnostics, never authority.
- Each model worker runs outside the supervisor process. The SDK-headless runner
  uses only the stable `openai-codex` adapter and pinned runtime. An App-native
  runner may ask the open App to create/display a visible native worker, but the
  worker receives the same result-submission capability and must close through
  the same harness IPC boundary.
- The worker submits its complete raw, bounded `ModelFacingResult` directly with
  `codex-flow worker-submit` over the controller-owned local socket. Neither the
  App, the source controller thread, `read_thread`, `wait_threads`, prose, a
  summary nor a host-created dictionary may translate or submit the result.
- The supervisor strictly decodes, validates and binds the raw bytes to the
  dispatch/generation/capability, rechecks validation and workspace/Git
  integrity, records the terminal result and transition in one SQLite
  transaction, and only then projects artifacts or considers a successor.
- Successors are finite edges already authorized by the durable capsule and H4
  state machine: required review, bounded repair/recovery, or the next declared
  milestone action. Scheduling is a deterministic transaction, not a recursive
  planner or controller-model turn. A material contract/scope/security decision
  still becomes the existing typed decision state.

#### Daemon lifecycle, packaging and service management

The installed wheel exposes the existing `codex-flow` command and an internal
supervisor entrypoint from the same exact distribution. Production Linux uses a
generated per-repository user-systemd unit with `Restart=on-failure`, an exact
absolute installed executable, canonical repository/state-root arguments, a
private runtime directory and no App dependency. `codex-flow supervisor
install|start|status|stop|uninstall` validates the canonical repository identity
and exact wheel version. Install/uninstall are explicit user operations; normal
`control` may start an already-installed unit but may not silently modify user
service configuration. Tests generate and exercise units under temporary
`XDG_CONFIG_HOME`/runtime roots; this milestone does not install a real user
service or mutate the user's normal tool/plugin configuration.

For development and hermetic tests, `codex-flow supervisor run --foreground`
uses the identical supervisor loop. A bounded detached-spawn fallback is
allowed only when configuration explicitly selects it: `Popen(start_new_session=True,
close_fds=True)` plus a one-shot readiness pipe, exact executable/version and
canonical repository identity. It must not pretend to provide boot-time restart.
If neither an installed user service nor the explicitly selected detached mode
can provide the requested recovery contract, enqueue fails `EXTERNAL_BLOCKED`
before a worker call. Supervisor startup performs one recovery scan, then blocks
on its local socket/timers; it never periodically polls SQLite or Codex tasks.

#### Durable schema, queue and idempotency

Advance the SQLite schema from v9 in one serialized, crash-atomic migration.
The exact names may follow repository conventions, but the following facts are
mandatory and closed-schema validated:

- one supervisor authority row: repository/state-root identity, epoch, random
  owner nonce hash, PID and process-birth identity, acquired/renewed/expiry
  times, executable/version digest and requested shutdown state;
- one dispatch queue row per logical dispatch/generation: backend
  (`sdk_headless` or `app_native`), immutable capsule/action/route/workspace and
  result-contract digests, state (`queued`, `claimed`, `starting`, `running`,
  `result_submitted`, `finalizing`, terminal), availability/deadline, claim
  epoch/nonce, attempt number and bound SDK/App thread identity when known;
- one capability row per attempt: dispatch/generation, allowed operation
  `submit_result`, schema/workspace/backend binding, issued/expiry/consumed
  facts, random-token hash and accepted raw-result digest; plaintext capability
  bytes never enter SQLite, logs, artifacts or prompts;
- one successor/outbox fact that makes terminal-result commit and successor
  enqueue atomic, and one optional terminal-notification fact with source task,
  payload digest and outcome (`not_applicable`, `unavailable`, `attempted_ok`,
  `attempted_failed`). A database constraint permits at most one notification
  attempt per terminal dispatch.

Enqueue, claim, bind, submit, finalize and successor scheduling are idempotent
only for byte-identical immutable facts. Conflicts fail closed. A repeated raw
submission with the same capability/result digest returns the recorded terminal
fact without another transition; any different result, consumed token reuse,
wrong dispatch/generation/backend/workspace/schema, stale epoch or terminal
mutation is rejected with exact database bytes unchanged. Queue selection is
FIFO by durable sequence among eligible items, with explicit route/workspace
lease constraints and bounded retry/recovery counts; no wall-clock ordering is
used as identity.

#### Local IPC, authentication and security boundaries

The supervisor listens on a Unix-domain socket below a controller-owned runtime
directory created descriptor-first with no-follow checks, directory mode 0700
and socket mode 0600. Every request is length-prefixed/canonically encoded,
bounded before allocation, versioned and closed. Linux peer credentials must
match the controller OS uid. The same-uid process boundary remains the product
trust boundary, but same uid alone grants no workflow mutation: a 256-bit
single-purpose capability is also required.

The plaintext capability is delivered to the worker in a descriptor-anchored
0400 capability file below the private runtime root (or an inherited read-only
file descriptor for the SDK child), never as an environment variable, argv,
model prompt, SQLite value or artifact. The worker CLI reads it, connects to the
bound socket and submits one raw UTF-8 payload of at most 65,536 bytes. The
capability binds dispatch id, generation, backend, workspace identity and
`ModelFacingResult` schema digest, expires, is consumed transactionally and is
removed best-effort after durable closure. Crash recovery can reissue a new
attempt capability only after invalidating the old attempt and proving the old
worker dead or incapable of submission; ambiguous live work is never duplicated.
Socket substitution, symlink/hardlink/special-file paths, oversized frames,
partial writes, invalid UTF-8/BOM, unknown keys, replay, cross-repository use and
concurrent conflicting submissions are rejected before workflow mutation.

Workers have no direct SQLite write authority and controller state remains
outside their mutable surfaces. Native Codex sandbox/approval/provider/profile
inheritance remains unchanged. Secrets, capability bytes, raw prompts and raw
results are excluded from logs and sanitized retained evidence. No network
listener, privileged daemon, private Desktop socket, direct app-server JSON-RPC,
global Codex config change or App authentication scraping is introduced.

#### Recovery and terminal protocol

On clean start or crash restart the supervisor takes one transactional snapshot
and reconciles each nonterminal item:

- `queued` work is claimable once; a claim committed without spawn is returned
  to eligible state after its expired supervisor epoch;
- a known-live SDK runner remains owned and is allowed to submit; a dead runner
  before thread identity is retried within the typed transport budget; a dead
  runner after durable SDK identity resumes that exact SDK thread through the
  existing checkpoint contract, never starts a duplicate;
- a bound App-native runner is never completed from App status. If it survives
  App closure it submits normally. If it is proven dead before submission, the
  queue records a recoverable App-worker loss and waits for the configured
  App-native recovery action; it is never silently converted to SDK-headless.
  Failure to resume/recreate the visible native worker without the App is an
  App-native capability limitation, not loss of the durable workflow fact;
- `result_submitted`/`finalizing` rows are replayed from the stored bounded raw
  result digest/body to exactly one terminal transaction. Terminal rows are
  immutable. Projection, notification or process death after commit cannot
  reopen them.

The worker terminal protocol is: finish owned work; write one complete schema-v1
`ModelFacingResult`; invoke `codex-flow worker-submit --capability-file <path>
--result-file <path>`; receive a small acknowledgement containing only dispatch,
terminal status and result digest; then exit. The result file and capability
path must be controller-provided private paths, and the raw result is retained
only in the bounded controller record/projection required by the existing
contract.

After terminal commit, an already-open App adapter may make exactly one
best-effort native `send_message` notification to the recorded source task with
`COMPLETION`, run/milestone id and durable status. The attempt is outbox-backed,
non-authoritative and never retried: unavailable App records `unavailable`, an
API error records `attempted_failed`, and success records `attempted_ok`.
Notification happens after successor enqueue and cannot affect completion,
review scheduling or recovery. The source controller is never required to call
`wait_threads` or poll; notification is only a wake-up hint.

#### Adapter boundaries and App-independent truth table

- SDK-headless: start, execute, result submission, review/recovery successors,
  terminal commit and status work without any App API/task dependency. The App
  may be open, never started or closed during execution; its physical state is
  recorded truthfully but is not a production or promotion gate. The mandatory
  sentinel proves zero App lifecycle authority and zero App API/task calls.
- App-native: the App is permitted only to create/display a visible native
  worker and to attempt the terminal notification. Binding identity does not
  grant it ledger authority. The worker submits directly to harness IPC. If the
  App/runtime keeps the worker alive after the window closes, the workflow must
  durably complete; if closing the App kills the worker and no documented native
  resume exists, H6-E must retain a recoverable nonterminal fact and report the
  exact App-native capability gap. It may not invent completion or substitute
  SDK-headless.
- When the App is unavailable, codex-flow cannot create a new App UI task,
  add/update sidebar or thread-visible content, wake a visible source task, or
  deliver a native notification. These are optional UI projections, never
  SDK-headless lifecycle or result requirements.

#### Mutable ownership

- `src/codex_flow/ledger.py`, `src/codex_flow/controller.py`,
  `src/codex_flow/cli.py` and `src/codex_flow/projection.py` for the v10 queue,
  supervisor-owned transitions, result closure and command boundary;
- `src/codex_flow/supervisor.py`, `src/codex_flow/worker.py`,
  `src/codex_flow/ipc.py` and `src/codex_flow/service.py` as the sole new process,
  worker, local-protocol and service-management implementations;
- `src/codex_flow/backends/codex_sdk.py`, `src/codex_flow/app_native.py` and
  `src/codex_flow/contracts.py` only for runner integration, App projection and
  the retained H6-D normalization/raw-envelope donor;
- `pyproject.toml` and `uv.lock` only for exact entrypoints/package data; focused
  controller/ledger/App/SDK/IPC/service tests, fixtures and temporary service
  templates; `docs/reviews/codex-controller-compatibility.md` and one sanitized
  `docs/reviews/evidence/h6-e-detached-supervisor.json` record.

The H6-E executor is the one mutable owner of all these surfaces, including the
three-file donor. Shared ledger/schema, public CLI/contracts and production
entrypoints have no parallel owner.

#### Protected surfaces and non-goals

Protected: this canonical plan and `AGENTS.md`; `workflow.toml` routes/limits;
`src/codex_flow/domain.py`, `src/codex_flow/config.py`,
`src/codex_flow/native_profile.py`, `src/codex_flow/worktrees.py` and existing
H4 acceptance semantics except the named integration seams; `src/codex_flow/h6_pilot.py`;
all plugin/skill sources, manifests, validators, accepted H1-H5 evidence and
legacy handoff code; installed plugins/trust/hooks, normal user service/config
roots, global Codex authentication/state, the primary checkout, other
worktrees, remotes and downstream repositories; every pre-existing change
outside the three-file donor and every path not expressly mutable.

Non-goals: a general distributed scheduler, remote/network IPC, multi-user or
root service, custom UI/dashboard, App sidebar automation, undocumented App
attachment, provider/backend abstraction, route/model/effort changes, recursive
planning, periodic `wait_threads`/task/status/SQLite polling, controller-model
keepalives, automatic Git push/merge/rebase/stash/discard, real user-service or
plugin installation, legacy retirement, medium/large H6 parity pilots, or
claiming App-native UI operations work while the App is closed.

#### Objective acceptance and adversarial tests

1. Install/build: `make check`, focused tests, Ruff, `git diff --check` and full
   diff self-review pass. Build one wheel in a fresh directory; run help/schema,
   v9-to-v10 migration, foreground supervisor, detached/service-template,
   enqueue/status and worker-submit checks via `uvx --from <exact-wheel>` with
   source imports unavailable. Install the same wheel into temporary
   `UV_TOOL_DIR`, `UV_TOOL_BIN_DIR`, `XDG_CONFIG_HOME` and runtime roots and
   prove generated units use the exact installed executable/version. Do not
   install/start a real user unit.
2. Queue/idempotency: cover empty/one/500-item FIFO queues, 501/beyond configured
   limits, concurrent producers, two supervisor contenders, lease expiry,
   claim-before-spawn crash, spawn-before-observation crash, duplicate and
   conflicting raw submissions, stale generations, expired/revoked tokens,
   result-commit/projection/notification boundaries and atomic successor enqueue.
3. IPC/security: cover fragmented and coalesced frames, zero/maximum/oversized
   payloads, invalid version/type/UTF-8/BOM/JSON/schema, wrong uid where
   injectable, token guessing/replay/cross-dispatch/cross-repository use,
   socket/capability path substitution, symlink/hardlink/special files,
   concurrent same/different submissions and log/artifact secret scans.
4. Recovery: inject process death at every durable boundary. Restart with one
   fresh supervisor and prove no duplicate model turn, worker, result,
   transition, successor or notification. Resume an identified SDK thread;
   safely retry a pre-identity transport loss; retain rather than guess an
   ambiguous live worker; finalize a submitted result exactly once.
5. Zero-poll/token proof: instrument all controller adapters and native task
   tools during a deliberately blocked worker. From enqueue until direct result
   submission, assert zero `wait_threads`, `read_thread`, list/status polling,
   source-task messages, controller model/API calls and controller-model token
   usage. The supervisor blocks on socket/timer readiness; no status-read count
   scales with elapsed wall time. Only the documented singleton-lease renewal
   write may run at its bounded cadence.
6. App independence: with the App process proven absent, run a real disposable
   SDK-headless worker through direct IPC, observable file change, validation,
   required successor scheduling and durable terminal completion. Open then
   close the App during a second SDK-headless run and prove identical authority.
   For one bounded App-native sentinel, close the App after visible bind and
   before result submission: accept durable completion only from the worker's
   direct IPC; otherwise retain and report the exact recoverable capability gap.
7. Notification: terminal commit and successor scheduling precede notification.
   Test App absent, success, injected failure and supervisor crash around the
   outbox. Each terminal dispatch has zero attempts when no App is available or
   exactly one attempted outcome when available; no path retries or changes the
   terminal bytes/status.
8. Donor/protection: retain exact `refs/codex/turn-diffs/**` normalization and
   adversarial near-prefix/ordinary-ref/history checks; retain strict raw-result
   envelope/parser and v1 action recovery. Capture the initial three donor
   hashes above and prove all unrelated starting bytes/status, protected paths,
   global state and selected worktree topology remain unchanged.

#### Architecture promotion gate and successor

The architecture reviewer must confirm one SQLite authority, one supervisor and
one implementation owner; event-driven no-poll operation; capability-confined
same-uid IPC; crash-atomic migration and transitions; explicit process and App
failure semantics; no secret/token leakage; immutable terminal results; direct
worker-to-harness raw result ownership; exact package/service identity; and
truthful App-independent limitations. The objective reviewer independently
confirms runtime behavior and regressions. Retained sanitized evidence must include
process/service identity digests, queue/epoch transitions, worker/capability and
raw-result digests (never secrets/raw sensitive text), truthful App-presence
state, zero App API/task calls, zero-poll/token counters, crash points,
notification outcome, Git/workspace integrity and exact wheel identity. App
evidence never relabels an App-present run as physically absent.

After H6-E promotion, H6 resumes with one medium and one large parity pilot and
the typed legacy-retirement decision. H6-E itself neither runs those pilots nor
retires anything.

Outcome: run one medium and one large real milestone through the controller,
compare lifecycle correctness and usage against the legacy path, verify local
and remote/Desktop compatibility gates, and make an evidence-backed decision on
disabling hooks and replacing `codex-thread-handoff`.

Acceptance: no duplicate owners; durable terminal evidence survives notification
failure; planner compaction and thread budgets hold; observable repository
outcomes and independent reviews pass; every unrun external gate stays open.

Promotion gate: only proven reachability and required behavioral parity permit
legacy disablement. Deletion is a separate authorized cleanup milestone after
installed-plugin and downstream-pin migration.

Acceptance modes: `objective`, `architecture`, plus `visual` for any pilot whose
outcome includes rendered quality.

Implementation boundary and evidence:

- run one medium and one large real repository milestone through the canonical
  controller entrypoint with fixed capsules, explicit routes, real worktrees,
  structured execution/review/repair, and durable retained evidence;
- compare duplicate ownership, terminal-result durability, recovery behavior,
  planner prompt/compaction use, model turns, wall time, and notification
  independence against the retained legacy baseline without inventing cost or
  quality claims;
- exercise local SDK compatibility directly. Record Desktop, idle wake, remote
  host, permission-profile, and native-review capabilities as proven,
  unsupported, not-exposed, or not-run; an unavailable optional capability does
  not falsify the local controller pilot;
- make a typed retirement decision: `retain_legacy`, `disable_hooks_keep_manual`,
  or `ready_for_separate_cleanup`. Default to retention unless production
  reachability and required behavioral parity are both proven.

Promotion requires both pilots to deliver observable repository outcomes,
independent required-mode reviews with zero P0/P1, one recovery/notification-
failure scenario, green repository gates, retained evidence, and an explicit
legacy decision. H6 never deletes installed plugins, global hooks, downstream
pins, or legacy code; any cleanup remains a separately authorized follow-up.

## Assumptions

- The user selected an SDK-first controller and authorized implementation of
  the program, but not push, PR, merge, plugin installation/trust, global config
  mutation, downstream updates, or legacy deletion.
- The repository's routing instructions authorize H1 as a substantial,
  objectively verifiable Luna XHigh milestone.
- The stable SDK's pinned runtime may differ from the system `codex` CLI; H1
  records both and treats that separation as intentional unless evidence shows
  an interoperability failure.

## Open findings

- Review, Desktop automatic sidebar visibility, idle wake, remote-host support,
  and permission-profile survival are not exposed by the H1 stable SDK surface;
  structured skill input exists but remains unexercised. These are optional or
  later compatibility gates, not inferred capabilities.
- Desktop visibility is an optional H6 App-native projection. It is never
  inferred from SDK thread creation and never required for SDK-headless
  execution, durable completion or recovery. No undocumented Desktop-owned
  app-server attachment is permitted.
- The visible H6-C pilot proved App-native visibility and native identity
  binding, but also exposed false Git-integrity failure from
  `refs/codex/turn-diffs/**` and the lack of native `output_schema`. The partial
  H6-D donor addresses exact ref normalization and strict raw envelopes. H6-E
  absorbs those changes while replacing App/source-thread lifecycle ownership
  with the detached supervisor and direct worker submission contract.
- SDK-headless workflows remain fully functional without App lifecycle
  authority. Creating/updating visible App UI threads and native notifications
  is optional and unavailable whenever the App cannot serve those actions.
  Whether a bound App-native worker survives an App close is a diagnostic fact,
  not a prerequisite for headless correctness or authority to infer a result.
- The supplied audit's underlying archive is not stored in this repository;
  its findings motivate the design but do not substitute for H1 captured
  evidence.
- Existing merged hook build `0.1.4+codex.20260820180447` has not demonstrated
  compatibility with the newer native envelopes described in the audit.

## Current review log

- 2026-08-18 through 2026-08-20: prior program delivered authorized routing,
  retry-free peer recovery instructions, and lifecycle hook build
  `0.1.4+codex.20260820180447`; commits are retained in Git and the merged source
  remains the migration baseline.
- 2026-08-24: user supplied a seven-day transport/lifecycle audit and selected a
  Python-SDK harness. Planning moved to clean branch
  `agent/python-sdk-controller` at `bd626a4`; the stale dirty primary checkout is
  protected. Chosen design is one SDK production backend, SQLite authority,
  controller-owned worktrees, structured results before notifications, and
  capability-gated legacy migration.
- 2026-08-24: user requested bringing SprintAct's Python tooling and
  `AGENTS.md` standards into this repository. Selected an adapted import of the
  reusable root toolchain and Python engineering rules, with explicit exclusion
  of SprintAct product/service/database policy and preservation of SQLite as the
  controller ledger.
- 2026-08-24: H1 commit `7f41eaa` returned green and was integrated as
  `1c04bf7`. Verified retained evidence for two real schema-bounded turns on one
  resumed thread, explicit Luna/medium routing, 45 ordered events, unchanged
  read-only repositories, truthful optional-capability labels, and a clean
  executor worktree. Selected H2 as the next executable milestone.
- 2026-08-24: H2 implementation candidate `6db6146` returned with a clean
  worktree and zero self-review P0/P1. Planning verification confirmed the
  five-path scope and reran `make check` successfully with 39 tests. Candidate
  promotion is withheld pending the required independent review of transition
  completeness, concurrency/idempotency, transactional rollback and migration,
  metadata/path safety, and projection authority.
- 2026-08-24: H2R independently rejected `6db6146` with P0=0/P1=5/P2=1.
  Reproduced defects are ambiguous cross-run milestone mutation, alternate-key
  sensitive-text persistence, counterfeit constraintless v2 acceptance,
  symlink bypass on ledger reopen, and ancestor-swap artifact escape; speculative
  aliases are P2. The reviewer independently confirmed the pure 121-pair table,
  single-owner races, stale-writer exclusion, rollback behavior, genuine-v1
  migration feasibility, and absence of external side effects. Repair cycle 1
  returns to the same executor under the decisions above.
- 2026-08-24: H2-R1 repair `d32e190` returned on top of immutable candidate
  `6db6146`, changing the same five H2 paths. It claims all six stable findings
  closed and adds real-ledger 121-pair coverage, constrained-v1 migration and
  rollback, post-event rollback, conflicting-generation races, sensitive-text
  exclusion, counterfeit-schema rejection, reopen symlink rejection, and an
  ancestor-swap projection regression. Planning reran `make check` successfully
  with 46 tests. Promotion remains withheld for fresh full-range re-review.
- 2026-08-24: the second independent Luna review rejected `d32e190` with
  P0=0/P1=5/P2=2 despite a green `make check`. It confirmed closure of explicit
  run/milestone identity, arbitrary durable text exclusion, anchored artifact
  writes on Linux, and speculative API aliases. It reproduced counterfeit
  schema and non-causal-history acceptance, a pre-connect hardlink substitution
  that redirects SQLite writes, caller mutation of the exported transition
  table, invalid event/dispatch combinations that make committed databases
  unreopenable, and public raw-connection mutation; it also found indirect
  `O_DIRECTORY` access and non-string identifier coercion. H2 remained
  unintegrated and H3 blocked; the evidence was handed to a fresh Sol Medium
  recovery owner for bounded diagnosis and repair.
- 2026-08-24: Sol Medium continuation `a01596d` returned on exact parent
  `d32e190`, changing four authorized H2 paths. It reports immutable transition
  policy, strict identifier types, pre-mutation event/dispatch validation,
  private SQLite access, canonical DDL identity, full causal replay, and a
  parent-anchored pinned-inode Linux connection through `/proc/self/fd`.
  Planning verified the parent and path scope and reran `make check`
  successfully with 53 tests in the clean executor worktree. H2 remains
  unintegrated and H3 remains blocked pending one fresh independent Sol High
  review of the exact repair and full H2 range.
- 2026-08-24: independent Sol High review denied `a01596d` with
  P0=0/P1=3/P2=2 despite a green `make check`. It confirmed seven prior finding
  classes closed, but reproduced an accepted extra trigger/view/index that
  deletes a run while `create_run` reports success, writes escaping through a
  pre-existing hardlink, silent adoption of a different valid ledger across
  close/reopen, mutation of the transition policy's private backing dictionary,
  broad unused root exports, and a zero-byte residue after failed first open.
  H2 remains unintegrated and H3 remains blocked. The same executor receives one
  bounded repair against the concrete findings above.
- 2026-08-24: the user replaced attempt-count routing ladders with typed
  acceptance and completion-biased recovery. Future milestones declare
  objective, visual, and architecture modes and receive distinct authorities.
  Non-convergence triggers diagnosis, strategy change, or architecture replan
  and continued execution. Only underdetermined intent or new user authority is
  `NEEDS_DECISION`; missing external prerequisites are `EXTERNAL_BLOCKED`; only
  proven infeasibility is `FAILED`. The already-dispatched H2 repair remains the
  sole owner and will receive one final independent integrity verification
  before promotion; no automatic model ladder or user interruption follows from
  a failed check.
- 2026-08-24: H2 repair `269a75b` closed the remaining schema-inventory,
  durable-write, inode-continuity, transition-policy, root-export, and failed-
  first-open findings on top of `a01596d`. Planning verification did not promote
  it: the focused suite reproduced a concurrent-open authority read that could
  observe a dispatch row and its causal event from different SQLite snapshots,
  producing `CorruptSchemaError` during a four-process idempotent claim race.
- 2026-08-24: follow-up `fc6d286` makes authority validation snapshot-consistent
  and adds an explicit concurrent-open regression. Planning verification passed
  the focused H2 suite ten consecutive times and full `make check` with 62
  tests. H2 remains unintegrated pending one fresh independent Sol High review
  of exact target `fc6d286` and full range `fab4cb6..fc6d286`.
- 2026-08-24: independent Sol High review denied `fc6d286` with
  P0=0/P1=3/P2=0. It closed the prior main-schema, inode/hardlink, transition-
  mutation, root-export, failed-open, and concurrent-open classes, but
  reproduced same-connection TEMP schema/metadata mutation that returns success
  then fails reopen, an authority-free `PLANNED` through `ACCEPTED` history, and
  a public transition without an expected predecessor token. The same H2
  workspace/executor owns the bounded repair; H3 remains blocked.
- 2026-08-24: the user made workspace identity program/lane-owned. Fresh
  threads, model changes, reviews, repairs, recovery, rollovers, and sequential
  milestones reuse the same workspace while mutable ownership is singular.
  Managed worktrees move to semantic sibling `<repo>.worktrees/<program[-lane]>`
  paths; H3 and the workflow skills must enforce this before controller use.
- 2026-08-24: repairs `3186662` and `124107b` closed TEMP/schema-metadata
  mutation, authority-free execution history, optional stale-writer tokens,
  mixed-snapshot recovery facts, and the deterministic late-link commit
  boundary. Planning reran focused H2 and full `make check` successfully with 68
  tests; the full seven-commit H2 chain is integrated as `51e1f30..a858d6f`.
- 2026-08-24: a final reviewer verified the exact target/scope and green
  aggregate gates, then was twice stopped by the platform security filter while
  evaluating a continuously hostile same-UID hardlink race. Planning resolved
  the architecture rather than retrying: same-UID processes are explicitly one
  trust boundary, while all repository-path, substitution, cooperative
  concurrency, and deterministic transaction-boundary guarantees remain
  required and green. Under that product threat model H2 has zero open P0/P1 and
  is promoted; H3 is unblocked.
- 2026-08-24: H3 implements the first agent-usable SDK controller vertical
  slice: versioned capsules, SQLite v3 execution facts, program/lane-owned
  current/existing/managed worktree leases, workspace-write SDK execution,
  durable external-call and identity checkpoints, fresh-client resume,
  explicit validation, with result-before-projection ordering proved by the
  deterministic boundary fault injection rather than inferred from the real
  sentinel, and
  `plan/start/resume/status/cancel`. The real disposable sentinel passed with
  one dispatch, one semantic managed worktree, one thread id, 43 ordered SDK
  events, an injected post-turn process boundary, successful fresh-client
  `thread_resume`, unchanged protected paths, and a durable terminal result.
  Hermetic tests and repository gates are green; H3 promotion remains pending
  independent objective and architecture review of the exact candidate.
- 2026-08-24: the second Luna review cycle reopened two P1 mutation-containment
  classes: repository-external writes were invisible to Git-only audits, and
  commit-then-reset plus Git-dir configuration mutations could erase their
  final-tree evidence. A fresh Sol Medium continuation sealed the production
  SDK child with a bounded OS mount policy, added durable schema-v4 sandbox and
  Git-authority facts, and passed 86 focused tests plus the 109-test aggregate
  gate. The first live repair attempt exposed an unintended writable synthetic
  tmp root and the second exposed a DNS break caused by hiding `/run`; both
  launcher defects were repaired and regression-covered. The final actual-SDK
  attempt reached the provider but returned `usage_limit_exceeded` before the
  executor turn. H3 promotion and H4 remain blocked on one unchanged-model live
  sentinel rerun after credit/reset; no denied-write evidence is inferred from
  the deterministic probe or the absence of an external file change.
- 2026-08-24: the user corrected the H3 permission contract. Harness agents
  must inherit native Codex permissions rather than add mandatory Bubblewrap
  containment. The continuation removes hard-coded workspace-write/deny-all
  production overrides, projects the active native provider/profile into
  private mutable runtime state, adds monotonic optional read-only restriction,
  and keeps worktree/Git controls as ownership and evidence boundaries only.
  The prior usage-limit diagnosis is superseded by the missing `codex-lb`
  provider projection; H4 remains blocked pending corrected deterministic and
  real-sentinel evidence plus independent promotion review.
- 2026-08-24: the one authorized corrected real sentinel run reached the SDK
  turn and injected post-turn crash boundary, but resume rejected native
  runtime-added private config as a projection conflict. Deterministic repair
  now restores the validated projection atomically between SDK processes and
  keeps mutable sessions private. The run was not repeated; corrected terminal
  provider/profile, Git-parity, and global-config evidence remain open.
- 2026-08-24: the user authorized exactly one new corrected sentinel for exact
  commit `04e0f889678770bfecd7da3c0a8cbd03344f123f`. It passed on the actual
  Python SDK/bundled app-server route using `gpt-5.6-luna`/`medium` and the
  active `codex-lb` profile. Evidence records inherited native
  `danger-full-access`/`never`, one dispatch and identity reused across the
  injected fresh-process resume, the validated workspace edit, unchanged Git
  authority, unchanged global Codex config bytes, and sanitized profile and
  provider digests. H3 has zero self-reviewed P0/P1; H4 remains blocked pending
  independent objective and architecture promotion review.
- 2026-08-24: objective promotion review of `3014c568` returned
  P0=0/P1=2/P2=2: resume rejected native tightening instead of monotonically
  rebinding it, runtime-home mkdir/chmod could follow a symlink before lstat,
  cancelled executions accepted a late turn observation, and compatibility
  documentation was stale. The existing Sol Medium owner repaired these in
  schema v6 with an atomic permission meet before adapter creation, distinct
  compatibility failure, descriptor-anchored no-follow runtime preparation,
  terminal mutator guards, and deterministic crash/concurrency/path regressions.
  Focused H2/H3/SDK validation passed 100 tests, the tightening concurrency
  regression passed five additional consecutive runs, and `make check` passed
  all 123 tests plus formatting, lint, validator, compileall, and pre-commit.
  Full H3 self-review found zero open P0/P1.
  The prior passing real sentinel remains the production-route evidence; no new
  live run was consumed because transport/provider/model behavior did not
  change. H4 remains blocked pending fresh objective and architecture reviews.
- 2026-08-24: objective re-review of `e580d291` returned P0=0/P1=1/P2=0:
  terminal ledger calls could still acknowledge identical thread-identity and
  terminal-result payloads without changing durable state. The bounded repair
  now applies one terminal-execution guard before every equality/idempotency
  branch across all 12 public H3 execution, checkpoint, lease, and integrity
  mutators. Controller cancellation retains command-level idempotency through
  a read-only terminal precheck. A table-driven close/reopen regression covers
  COMPLETED, FAILED, and CANCELLED against every mutator and proves exact
  database bytes, typed rows, lifecycle events, and workflow events remain
  unchanged after each rejected stale call; a complementary regression retains
  contractual nonterminal idempotency. Focused H2/H3/SDK tests passed 103 tests
  and `make check` passed all 126 tests plus formatting, lint, validator,
  compileall, and pre-commit; `git diff --check` is clean. Full H3 self-review
  found zero open P0/P1. The passing real sentinel was not rerun because this
  repair changes only hermetic ledger acknowledgement semantics, not the proven
  provider/profile/permission route. H4 remains blocked pending fresh objective
  and architecture promotion reviews.
- 2026-08-24: architecture review of `f61c12de` returned P0=0/P1=3/P2=1.
  Repair `02160dc` restricts the `.codex-flow` mutation exemption to the actual
  repository-bound state tree, requires a persistent causal turn-start fact and
  exact durable-fact matching for turn idempotency, and adds schema-v7
  per-milestone workspace baselines while preserving stable program/lane lease
  identity and its original base SHA. Managed and existing worktree regressions
  reject non-authoritative `.codex-flow` writes; close/reopen tests reject
  skipped turn order and retain matching idempotency; two disjoint sequential
  milestones reuse one managed workspace and recover from both a rejected
  preflight mutation and an injected post-baseline/pre-external stop without
  remaining in `STARTING`. Evidence wording now attributes result-before-
  projection ordering to the deterministic boundary fault injection; the
  retained real sentinel proves only its observable production-route facts.
  Focused H2/H3/SDK validation passed 108 tests, `make check` passed all 131
  tests plus formatting, lint, validators, compileall, and pre-commit, and both
  repair and full-range diff checks are clean. Full H3 self-review found zero
  open P0/P1. The real sentinel was not rerun because the SDK/provider/model,
  native permission, and resume transport route did not change. H4 remains
  blocked pending fresh independent objective and architecture promotion review
  of the exact repaired candidate.
- 2026-08-24: architecture promotion review of `02160dc` returned the sole
  P1 H3-ARCH-004: native discovery compatibility used only shallow root
  metadata. Repair `84b3065` replaces it with deterministic descriptor-anchored
  recursive identities for `skills`, `plugins`, and `memories`. Sorted relative
  paths, types, ownership/identity metadata, file-content digests, internal
  symlink targets, roots, and absolute ancestors are covered; external,
  dangling, or cyclic links, hardlinks, special files, substitutions, unsafe
  scan races, and explicit entry/depth/path/per-file/total-byte limit overflow
  fail closed without truncation. `verify_sources()` now compares the original
  captured snapshots before adapter construction and again during private-home
  preparation. Same-length nested drift on all three surfaces and structural,
  symlink, hardlink, special-file, limit, ancestor, and post-turn fresh-resume
  attacks are regression-covered; compatibility failure remains typed and
  distinct from monotonic permission rebinding with zero adapter/resume calls.
  Native-profile sanitized facts advance to v2, but SQLite remains schema v7:
  its existing opaque digest field safely represents the stronger identity,
  and a prior-algorithm in-flight reopen fails typed and closed. Focused
  H2/H3/SDK validation passed 129 tests and `make check` passed all 152 tests
  plus formatting, lint, validators, compileall, and pre-commit. Repair and
  full-H3 diff checks are clean; full-H3 self-review found zero open P0/P1.
  The retained real sentinel was not rerun because the SDK/provider/model,
  permission, and resume transport path did not change. H4 remains blocked
  pending fresh independent objective and architecture promotion review of
  this exact repair.
- 2026-08-24: final architecture review confirmed five promotion blockers.
  Implementation `06a332e` closes H3-ARCH-004A by rejecting discovery-root,
  active-ancestor, multi-node, nested, and dangling link cycles on all native
  discovery surfaces before adapter construction. It closes H3-ARCH-003A with
  schema-v8 accepted terminal HEAD/mutable-root/Git-authority facts verified
  before any successor baseline, lease, adapter, or external call; migrated v7
  predecessors without reconstructable terminal facts require explicit
  reconciliation. It closes H3-ARCH-001A with bounded no-follow directory-
  topology deltas that expose empty-directory creation/removal/rename while
  exempting only the actual repository-bound controller state tree. It closes
  H3-ARCH-005 by canonicalizing physical repository/workspace paths before
  capsule serialization and every execution/lease key or lookup, with legacy
  noncanonical rows failing typed and closed. It closes H3-ARCH-006 through a
  structural native-config projection: literal MCP HTTP headers and sensitive
  stdio environment values become process-only environment references, while
  raw authorization, proxy-authorization, cookie, arbitrary header, API key,
  token, password, and client-secret bytes never enter durable private state.
  The pinned runtime accepts the active projected `codex-lb` profile with zero
  ephemeral secret references; its documented `env_http_headers` surface was
  exercised directly without a provider turn. Focused H2/H3/SDK validation
  passed 177 tests and `make check` passed all 200 tests plus formatting, lint,
  validators, compileall, and pre-commit. The retained real sentinel was not
  rerun because the active SDK/provider/model/permission route and projected
  config are unchanged. Full-H3 self-review has zero open P0/P1; H4 remains
  blocked pending fresh independent objective and architecture promotion
  review of the exact repaired candidate.
- 2026-08-24: the latest final review confirmed four remaining propagation
  blockers. Implementation `eb25987` closes H3-ARCH-003B with one canonical,
  double-captured pre-external authorization operation used before adapter
  construction and every SDK start/resume/turn boundary. The durable
  per-milestone baseline now binds its exact Git-authority digest at the same
  checkpoint; restart and fresh resume reverify HEAD, content, empty-directory
  topology, protected paths, config/index/refs/history, physical lease facts,
  and every accepted predecessor before external work. Authorization races
  fail closed and restore the last safe durable checkpoint when no call was
  made. H3-ARCH-005B is closed by transaction-ordering physical lease
  selection before contender checks: the insertion winner reacquires
  idempotently, unrelated capsule-only PLANNED rows are not active owners, and
  losing lexical/symlink aliases receive `WorkspaceLeaseConflict`. Eight
  threaded and four process interleavings plus identity-crash recovery prove
  one runnable owner, adapter, thread, and retained lease. H3-ARCH-006B is
  closed by a field-typed MCP server schema that rejects every unknown,
  noncanonical, nested, and colliding header shape before projection while
  retaining canonical native MCP fields and process-only secret references.
  H3-ARCH-007 is closed by the shared recursive schema predicate: all objects
  are closed and fully required, arrays have explicit items, scalar/schema
  keywords are closed, and byte/depth/property/item limits are enforced again
  after SDK decoding. Focused adversarial validation passed 50 tests; the full
  H3 suite passed 181 tests, the combined H2/SDK suite passed 50 tests, and
  `make check` passed all 254 tests plus formatting, lint, validators,
  compileall, and pre-commit. The active native profile still projects its
  three configured MCP servers with zero ephemeral secret references. The
  retained real sentinel was not rerun because SDK/provider/model/permission,
  bundled app-server transport, and the canonical sentinel schema are
  unchanged. Full-H3 self-review found zero open P0/P1. H4 remains blocked
  pending fresh independent objective and architecture promotion review of
  this exact repaired candidate.
- 2026-08-25: implementation `ca151fe` repairs the H3-ARCH-006C, H3-ARCH-007A,
  and H3-ARCH-007B candidate gaps on top of repair parent `3efa338`. Literal
  stdio `CODEX_HOME` is now a deterministic process-only collision alias with
  a fixed argv shim that restores the source-bound name only in the MCP child;
  the SDK/app-server keeps its private runtime home and provider/cross-source
  conflicts remain typed and fail closed. The strict decoder explicitly
  decodes byte inputs as UTF-8 without BOM before parsing, and all capsule,
  result, event, projection, and adapter JSON paths use that canonical loader.
  Capsules detach each caller Mapping/Sequence once into owned immutable data
  before validation, and `plan` independently canonicalizes before durable
  writes. Adversarial environment, pinned-runtime parser, child-delivery,
  decoder, durability, mutation, digest, and close/reopen coverage is included;
  the retained real sentinel was not rerun because the SDK/provider/model,
  permission, and resume transport route remain unchanged. H3 remains blocked
  pending fresh independent objective and architecture promotion review; H4
  remains blocked.
- 2026-08-25: after the subsequent independent review was interrupted by the
  platform security gate, the user explicitly ended the open-ended H3
  review/repair loop and accepted exact branch head `544c1c8` as pilot-ready so
  H4 can begin. This decision does not relabel H3 as defect-free or finally
  production-promoted. H4 must classify any concrete inherited defect it
  reaches by both severity and successor promotion impact, while H6 retains the
  final integrated hardening gate. H4-A is selected as the next executable
  slice; another broad H3 review is not one of its prerequisites or promotion
  gates.
- 2026-08-25: H4-A implementation `fafa29b` adds the typed `workflow.toml`
  role/limit authority and durable review, repair, recovery, decision, budget,
  lifecycle, and projection facts on the existing controller and SQLite
  connection. Planning independently verified exact path scope, the retained
  sanitized pilot, seven focused tests, full `make check` with 282 tests, and a
  clean worktree. The single real disposable Python-SDK/`codex-lb` pilot used
  Luna XHigh for two turns on one thread, made an observable authorized edit,
  received one deliberate promotion-blocking P1 rejection, repaired through
  the same durable owner, passed a fresh read-only re-review, and reached
  `ACCEPTED`. H4-A is promoted as the walking skeleton; H4-B is now executable
  without another H3 or H4-A review cycle.
- 2026-08-25: H4-B implementation `662d006` completes the multi-authority
  workflow. Capsules now derive fail-closed distinct executor, objective/code,
  visual, architecture, recovery, and decision routes; immutable rendered
  evidence, finding survival/new-scope lineage, non-blocking deferral,
  boundary-preserving architecture replans, planner decisions, and result-before-
  projection artifacts share the accepted H4/SQLite authority. Planning
  verified the sanitized real pilot, 11 focused tests, full `make check` with
  286 tests, exact diff hygiene, H4-A evidence preservation, and a clean
  worktree. The pilot reused one SDK thread for two `codex-lb` turns and reached
  `ACCEPTED` after objective/visual review, architecture rejection,
  `CONTINUE_WITH_REPLAN`, same-workspace repair, and fresh all-authority
  re-review. H4 is complete; H5 is the next program milestone but was not
  authorized for dispatch by the H4-only continuation request.
- 2026-08-25: the user explicitly authorized continuing through the end of the
  program. H5 and, after its verified terminal callback, H6 may now dispatch in
  fresh sequential execution contexts from this planning owner without another
  routine authorization prompt. This does not authorize plugin installation or
  trust, global/remote mutation, legacy deletion, or bypass of either
  milestone's evidence and promotion gates.
- 2026-08-25: H5 implementation `b57a353` packages the controller-backed
  workflow source and reduces model-visible planning/execution instructions to
  cognitive contracts. Planning verified prompt-input reductions, the 1,716-
  byte `workflow-control` skill, explicit non-mixable legacy reachability,
  plugin metadata `0.1.5+codex.20260825000000`, CLI/schema help, an isolated
  wheel build, six focused tests, full `make check` with 292 tests and 11
  validated skills, and the sanitized real medium pilot. That pilot entered
  through `workflow-control`/`codex-flow`, made only its authorized edit, and
  reached durable completion. H5 is promoted; H6 is the now-executable final
  integrated promotion and legacy-decision milestone.
- 2026-08-25: H6-R implemented the deterministic `ModelFacingCapsule` projection
  and made `codex-flow control` reachable through the packaged SDK controller.
  Focused tests, Ruff, diff checks, isolated-wheel packaging and a prior full
  307-test gate were reported green; later draft integration reached 310 tests
  before the user intentionally interrupted the aggregate run. A real R6A pilot
  nevertheless completed and independently returned `PROMOTE` with zero P0/P1,
  6,715 backend tests, 42 expected skips and 397 frontend tests, but controller
  closure failed because ignored `.next` and `node_modules` output was treated
  as source mutation. Its private SDK thread also did not appear in the active
  Codex app. The user selected Git-native ignored-artifact semantics and an
  App-native visible-worker mode while retaining SDK-headless execution; H6-C
  is now the next executable prerequisite and owns both fixes.
- 2026-08-25: the user raised the milestone wall-clock budget from 15 minutes
  to four hours (`14400` seconds) after the H6-C implementation demonstrated
  that substantial controller work plus the full repository gate can exceed
  the former nominal budget. Per-command and validation timeouts remain bounded
  independently; the longer milestone budget does not authorize polling,
  duplicate workers, weaker gates, or silent recovery.
- 2026-08-25: the user selected architecture-first routing for future
  substantial work. Sol Medium first writes a detailed decision-ready design in
  the canonical plan; only then does Luna XHigh implement it as the single
  mutable owner. Luna XHigh remains the objective/code reviewer and Sol Medium
  checks architecture conformance when declared. Sol High is reserved for
  critical security, irreversible/system-wide decisions or explicit
  escalation. One review per authority and at most one bounded repair for
  concrete blockers replaces repeated broad review waves.
- 2026-08-26: H6-C implementation commit `6feb927` supplied Git-native ignored-
  artifact semantics and the host-mediated App-native prepare/bind/result path.
  The visible pilot bound real task `01a03d56-1658-7e02-a082-890b26251106`,
  reused the selected worktree and produced only the requested existing
  compatibility-document insertion plus a complete result envelope. Durable
  closure nevertheless failed with `integrity_failure`: Codex App created or
  updated host-owned `refs/codex/turn-diffs/**` in the shared common Git
  directory after the baseline. The pilot also proved native `create_thread`
  cannot receive `output_schema`; terminal JSON was reconstructed outside a
  canonical raw-message boundary. H6-C is not promoted. H6-D is the sole next
  milestone and fixes exactly those two defects while preserving the existing
  uncommitted compatibility-document line and all other user changes.
- 2026-08-26: the user made App independence definitive before H6-D execution.
  Commit `678105a` contains the now-superseded H6-D plan; the uncommitted
  three-file donor is retained byte-for-byte by planning and classified above.
  H6-E replaces host callback/read/poll closure with a harness-owned detached
  supervisor, durable SQLite queue, capability-bound direct worker result
  submission, event-driven restart/recovery and post-terminal at-most-once App
  notification. SDK-headless execution is mandatory with the App absent;
  App-native UI creation/update remains truthfully unavailable while closed.
  H6-E is the sole next executable milestone.

## Next execution

Milestone: H6-E — Detached supervisor and App-independent completion.

Next executable capsule (authoritative typed authoring form):

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make codex-flow complete SDK-headless workflows with the Codex App closed by moving queue, "
        "worker lifecycle, direct raw-result closure, recovery and successor scheduling into one "
        "harness-owned detached supervisor and durable SQLite authority."
    ),
    decomposition=(
        "Integrate the three-file H6-D donor and add the crash-atomic v10 queue, supervisor lease, attempt capability, successor and notification-outbox facts.",
        "Implement packaged service/detached lifecycle, event-driven local IPC and capability-bound direct raw ModelFacingResult submission for SDK-headless and App-native workers.",
        "Implement exact restart recovery, immutable terminal closure, optional at-most-once App notification and truthful App-closed limitations without wait_threads or controller-model polling.",
        "Run adversarial unit/integration/migration/package/service gates, App-closed and App-close sentinels, independent objective and architecture reviews, and retain sanitized evidence.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "With the App absent, a real SDK-headless worker completes through direct capability-bound IPC, durable terminal commit and successor scheduling; controller-model token and lifecycle-tool usage while it runs are zero.",
        "One crash-atomic v9-to-v10 SQLite migration and event-driven supervisor provide singleton process ownership, FIFO bounded queueing, exact idempotency and restart recovery without duplicate worker, turn, result, transition, successor or notification.",
        "The worker submits one complete raw bounded ModelFacingResult through the controller CLI/socket; strict dispatch, generation, backend, workspace, schema, peer and single-use capability binding rejects malformed, replayed or conflicting submissions before mutation.",
        "The App is only an optional visible-worker and terminal-notification projection: closure never reads wait_threads/read_thread or awaits a source callback, and notification is post-terminal, best-effort and attempted at most once.",
        "The retained donor continues to normalize only refs/codex/turn-diffs/** and supplies the canonical raw-result envelope/parser while ordinary Git authority, v1 recovery and SDK structured behavior remain fail-closed.",
        "Focused adversarial tests, make check, Ruff, diff hygiene, exact-wheel/temporary-install/service checks, App-closed and App-close sentinels, and exact dirty-baseline protection pass with sanitized retained evidence.",
        "Independent objective and architecture reviews of the exact candidate both report P0=0/P1=0.",
    ),
    mutable_surfaces=(
        "src/codex_flow/ledger.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/projection.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/service.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/app_native.py",
        "src/codex_flow/contracts.py",
        "pyproject.toml",
        "uv.lock",
        "tests/test_h2_ledger.py",
        "tests/test_h3_controller.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_h5_workflow_control.py",
        "tests/test_h6_model_facing_projection.py",
        "tests/test_h6_app_native.py",
        "tests/test_h6_supervisor.py",
        "tests/test_h6_service.py",
        "docs/reviews/codex-controller-compatibility.md",
        "docs/reviews/evidence/h6-e-detached-supervisor.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "workflow.toml",
        "src/codex_flow/domain.py",
        "src/codex_flow/config.py",
        "src/codex_flow/native_profile.py",
        "src/codex_flow/worktrees.py",
        "src/codex_flow/h6_pilot.py",
        "plugins",
        "skills",
        "docs/reviews/evidence/h1-sdk-sentinel.json",
        "docs/reviews/evidence/h4-a-objective-pilot.json",
        "docs/reviews/evidence/h4-b-multi-authority-pilot.json",
        "docs/reviews/evidence/h5-workflow-control-medium.json",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Implement only H6-E from the canonical plan in the selected existing worktree. "
        "Capture and preserve the complete dirty baseline, then integrate rather than discard the exact three-file H6-D donor. "
        "Use one mutable owner for schema, queue, supervisor, IPC, adapters, CLI and packaging. "
        "Prove SDK-headless completion with the App absent, direct raw worker submission, crash/restart idempotency, zero wait_threads/read_thread/controller-model polling, and post-terminal at-most-once optional notification. "
        "Test exact wheel and temporary service management without installing a real user service or mutating global Codex/plugin state. "
        "Obtain the declared independent reviews, repair at most once for concrete blockers, retain sanitized evidence, and return one schema-valid ModelFacingResult. "
        "Do not run medium/large parity pilots, retire legacy paths, change routes, or claim App UI operations work while the App is closed."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```
## Queued TUI successor — active-session-filter-and-conversation-loading

Readiness: `blocked_on=local-image-worker-input terminal reconciliation`.
The dependency is serial because the current owner already changed the shared
`domain.py` and `cli.py` contracts.  After that candidate closes, this is the
sole next executable TUI milestone.  It requires no visual-quality review: the
accepted behavior is objective and architecture-bounded, with headless Textual
interaction proof at wide and 80x24 sizes.

Architecture map and ownership:

- modify `domain.py` only to add one bounded, redacted, non-secret
  `task_summary` field to the existing ephemeral `LiveWorkerStatus` projection;
- modify `supervisor.py` only to derive that summary from the already-durable
  execution prompt in the queue capsule, without a ledger/schema write;
- modify `control_client.py` only for the closed status decoder;
- modify `tui_client.py` only for selection-bound newest-page loading, stale
  discard and active-session filtering state;
- modify `tui_models.py` only to render the typed task summary and exclude
  empty diagnostic deltas from conversation fallback;
- modify `tui.py` and the existing `tui` command in `cli.py` for automatic
  selection loading, explicit loading/error states, `F` filtering and
  `--active-only` startup behavior;
- preserve `ledger.py`, `ipc.py`, the SDK adapter, worker execution, history
  broker/source, schemas, packaging and every transcript persistence boundary.

Dependency direction remains TUI -> typed control client -> authenticated
supervisor -> existing queue/history authorities.  New production modules,
public entrypoints, schemas, migrations, tables and retained artifacts: zero.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make the codex-flow TUI immediately show meaningful active worker sessions and their "
        "real persisted conversations instead of opaque task labels and empty diagnostic deltas."
    ),
    decomposition=(
        "Project one bounded redacted task summary from the existing durable execution capsule through the closed live-status API without new persistence.",
        "Load the newest stable conversation page whenever worker or controller selection changes, with explicit loading, unavailable and stale states.",
        "Add an Active only filter toggled by F and an optional tui --active-only startup flag while keeping attention decisions visible.",
        "Use diagnostic activity only as a truthful fallback and never render empty streaming deltas as conversation messages.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Selecting any worker with an eligible SDK thread automatically renders its newest complete user/agent history without requiring L; L is offered only when an older token exists.",
        "Rapid selection changes cannot display a late page for the wrong worker, reconnect clears ephemeral pages, and unavailable, stale or incomplete history is explicit rather than replaced by diagnostics.",
        "Worker rows and headers show a bounded human task summary from the existing execution capsule instead of Current task, while technical identities remain secondary.",
        "F toggles All sessions and Active only, tui --active-only starts filtered, completed/cancelled/attention workers are hidden in active mode, and the Needs your attention decision list remains visible.",
        "Empty agent-message and command-output deltas never appear as chat entries; bounded redacted non-empty tool summaries may remain in the diagnostic fallback only.",
        "Focused status-decoder, supervisor, TUI model, keyboard, selection-race, reconnect and exact 80x24 headless tests pass, followed by affected partitions, full make check, install/refresh and one live TUI smoke.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/control_client.py",
        "src/codex_flow/tui_client.py",
        "src/codex_flow/tui_models.py",
        "src/codex_flow/tui.py",
        "src/codex_flow/cli.py",
        "README.md",
        "tests/test_live_worker_control.py",
        "tests/test_workflow_control.py",
        "tests/test_local_ipc.py",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md after this capsule is frozen",
        "AGENTS.md and templates/AGENTS.workflow.md",
        "src/codex_flow/ledger.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/contracts.py and src/codex_flow/projection.py",
        "schemas, pyproject.toml, uv.lock and packaging",
        "docs/reviews/evidence and retained wheels",
        "plugin manifests, provider/App/global/auth/Git state and unrelated dirty bytes",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only active-session-filter-and-conversation-loading after the "
        "current local-image-worker-input owner is terminally reconciled. Preserve the dirty "
        "candidate and every protected surface. Add one ephemeral typed task summary, selection-bound "
        "automatic newest-history loading, explicit loading/error states, Active only filtering and "
        "truthful non-empty diagnostic fallback through the existing supervisor/control/TUI path. "
        "Do not add transcript persistence, polling, a second history source, a migration/schema, a "
        "new production module or visual-review evidence. Run focused headless interaction tests, "
        "affected partitions, full make check, reinstall/refresh and one live TUI smoke, then return "
        "one raw ModelFacingResult to the controller."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

## Next execution — complete-conversation-history

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Render the complete persisted user/agent conversation available from the official shared "
        "SDK for each eligible controller or worker thread, with safe loading and no alternate state authority."
    ),
    decomposition=(
        "Project one stable complete-message snapshot from official SDK thread/read, distinguishing it from lossy non-message provider activity and the bounded diagnostic ring.",
        "Carry exact controller/worker/thread/turn/message identity through bounded authenticated IPC pages, active-worker same-client reads and inactive read-only adapter reads.",
        "Add explicit newest-page and load-older behavior with stable snapshot tokens, bounded memory/frames, fail-closed privacy and truthful unavailable/incomplete states.",
        "Preserve the conversation-first hierarchy and safe actions while rendering a scrollable complete history at wide and exact 80x24 sentinels.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.VISUAL, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Every persisted user and agent message returned with full SDK turn items is reconstructible once, in SDK order, across bounded pages; diagnostic activity and lossy tool/provider events are never substituted.",
        "Worker pages bind dispatch, generation, attempt and thread; controller pages bind decision revision, generation and controller thread; stale, replacement or changed-snapshot tokens fail before display.",
        "Raw transcript text is never persisted or logged, known secrets are marked redacted before IPC, paths and URLs become typed presence markers, and unavailable, deleted, permission, incomplete, oversized and malformed sources remain explicit.",
        "The TUI offers explicit load-older and scrolling without a timer or polling loop, retains safe steer/interrupt/decision confirmations, and reconnect discards ephemeral pages before a fresh stable load.",
        "Wide light/dark and exact 80x24 light/no-color renders show controller above workers, multi-turn conversation, loading/scrolling and actions with no important zero-height text panel; independent visual review approves the exact candidate.",
        "A real existing or separately authorized disposable shared-SDK multi-turn thread proves the production read boundary without App APIs; absence of that prerequisite is reported as the single external gate rather than replaced by a fixture claim.",
        "Focused tests, affected semantic partitions, full make check, exact-wheel temporary-service proof, schema/format/compile/pre-commit/diff gates and objective/visual/architecture reviews close with zero open P0/P1.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py only for non-persisted conversation-history types and non-truncating secret redaction",
        "src/codex_flow/backends/codex_sdk.py only for official thread/read conversation projection",
        "src/codex_flow/supervisor.py only for exact subject validation, bounded ephemeral read broker and history IPC operation",
        "src/codex_flow/worker.py only for active-turn same-client conversation reads through the existing control loop",
        "src/codex_flow/control_client.py",
        "src/codex_flow/tui_client.py",
        "src/codex_flow/tui_models.py",
        "src/codex_flow/tui.py",
        "tests/test_codex_sdk_adapter.py, tests/test_live_worker_control.py, tests/test_local_ipc.py, tests/test_workflow_control.py, tests/test_production_pilots.py and tests/test_plan_compilation.py",
        "docs/reviews/evidence/complete-conversation-history.json and the existing conversation-first visual contract plus four conversation render sentinels",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md after this capsule is frozen and AGENTS.md",
        "ledger persistence/schema, controller lifecycle/recovery, service and CLI entrypoints, projection/plan compilation, native profile and plugin capability authority",
        "SDK start/resume/run/control/result/recovery behavior outside the precise read-only projection",
        "IPC framing and 64 KiB ceiling, contracts.py, __init__.py exports, schemas, configuration, packaging, Makefile and lockfile",
        "installer/plugin/skill/template surfaces, retained wheels, accepted unrelated evidence, provider sentinels, App/global/auth state and unrelated dirty bytes",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.VISUAL, RoleId("visual-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone as the single Luna XHigh implementation owner for only complete-conversation-history in the existing dirty python-sdk-controller worktree. Preserve the completed conversation-first TUI and every unrelated byte. Implement the frozen zero-new-artifact architecture over official shared-SDK thread/read, bounded ephemeral supervisor/worker coordination and the existing typed IPC/control clients. Do not add transcript persistence, parse rollout files, scrape or poll App state, replay history for recovery, change lifecycle/ledger/dispatch authority, enlarge IPC, create a module/schema/CLI/transport, call a provider without separate authorization, or mutate global/Git/plugin state. Run focused checks, real full-size renders and the named closure gates; retain only sanitized evidence, self-review the exact owned diff and return one terminal result to the planning controller. Do not start installation or another milestone."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

Planning validation (2026-08-26): the exact capsule above instantiated and
projected successfully against this checkout as
`run_id=model-36d442572b67827298bb7dd891c780c3`,
`milestone_id=milestone-8e374b232309f1f4cecfb4b6fa6c6010`,
`workspace_mode=existing_worktree`, route `gpt-5.6-luna/xhigh`. Its 23 mutable
and 14 protected surfaces are canonical repository-relative paths with zero
overlap; the execution prompt is 919 bytes under its 12,000-byte cap. The
independent routes remain `code-reviewer = Luna XHigh` and


## Active recovery — ordinary-chat installation integration

The user-selected recovery owner integrates the frozen complete-conversation-
history candidate under `docs/reviews/evidence/complete-conversation-history.json`
and `dist/complete-conversation-history/codex_flow-0.2.0-py3-none-any.whl`,
projects canonical output schemas onto the documented
provider Structured Outputs subset while retaining full local validation, and
closes the single plugin/controller bootstrap. Historical retained evidence is
never rewritten to bless successor bytes. The exact reconciliation wheel at
`dist/structured-output-runtime/codex_flow-0.2.0-py3-none-any.whl` remains its
historical authority. Exact H6-I evidence `2e6cf6d2c4804979...` and wheel
`1d6eaa14c6f6c5e5...` were recovered from retained local closure artifacts and
restored unchanged at their original `human-terminal-ui` paths; successor bytes
never reuse those immutable authorities. The recovery also validates raw
installer marketplace/plugin topology before path resolution, preserves empty
SDK user messages as typed zero-length text fragments, and requires the native
light/dark plus exact 80x24 render set to expose an always-visible scroll cue
and unclipped Load older, Open and Details actions.

Provider calls and the real two-turn SDK transcript sentinel remain outside
this recovery. After provider-free repository, reproducible-wheel and isolated
installer gates pass, the authorized shared-user activation installs
`codex-flow 0.2.0` and `personal-workflow-skills
0.1.7+codex.20260831000000` through the explicit bootstrap. The next actions
after closure are independent objective, visual and architecture reviews of the
integrated candidate plus the separately authorized real-provider transcript
gate.

Planning validation (2026-08-26): the exact capsule above instantiated and
projected successfully against this checkout as
`run_id=model-36d442572b67827298bb7dd891c780c3`,
`milestone_id=milestone-8e374b232309f1f4cecfb4b6fa6c6010`,
`workspace_mode=existing_worktree`, route `gpt-5.6-luna/xhigh`. Its 23 mutable
and 14 protected surfaces are canonical repository-relative paths with zero
overlap; the execution prompt is 919 bytes under its 12,000-byte cap. The
independent routes remain `code-reviewer = Luna XHigh` and
`architecture-reviewer = Sol Medium` from `workflow.toml` and repository
instructions. This planning-only task does not dispatch any executor or
reviewer.

Planning owner: source task `01a038ae-62ee-7910-ae89-6c13c2e0112c`.

Plan path:
`/home/adam/personal-workflow-skills.worktrees/python-sdk-controller/docs/reviews/peer-thread-workflow.md`.

Execution workspace:

- mode: `existing_worktree`
- repository: `/home/adam/personal-workflow-skills`
- path: `/home/adam/personal-workflow-skills.worktrees/python-sdk-controller`
- branch: `agent/python-sdk-controller`
- base SHA: `678105a0e55f89525e8adc0ff90d4ac7c7e75c34`
- lane: `python-sdk-controller`

Starting dirty state is exactly the three H6-D donor files listed and hashed in
the milestone plus this canonical planning change. The executor captures status,
diff and hashes before edits, reuses this worktree and branch, and creates only
disposable repositories/runtime/service roots for tests and sentinels. No other
worktree, repository, installed service, plugin, global configuration or task is
mutable.

The executor owns only the capsule's mutable surfaces, one bounded repair,
validation, the two independent reviews and one durable terminal result. The
planner retains scope, ordering, this plan and program closure. Medium/large
parity pilots and the legacy decision are successors only after H6-E promotion.

Escalate only for genuinely underdetermined user intent or a material public,
persisted, security, destructive-behavior, cost or scope decision. Missing
systemd/App/runtime capability is a typed external/capability fact with the
safe SDK-headless or foreground route retained when its acceptance still holds;
implementation difficulty or App UI absence never authorizes polling, invented
completion, backend substitution, global mutation or weakened gates.

### H6-E terminal audit and bounded repair decision — 2026-08-26

H6-E returned one corrected raw `ModelFacingResult` that parses as the complete
schema-v1 agent message (`1665` bytes, SHA-256
`841def5184f141094138942fc2aad0825ad28dc6351c994ac1257c6dba870562`).
Controller-owned revalidation passed `make check` with 333 tests, protected
surface equality and `git diff --check`, but durable closure correctly recorded
`failed` / `integrity_failure`. The result is therefore implementation evidence,
not promotion authority.

The failure has two reproduced causes. Git authority still changes when the
Codex host updates exact `refs/codex/snapshots/**` refs, although H6-D normalized
only `refs/codex/turn-diffs/**`. Separately, the App controller-state digest
includes `.codex-flow/runs/**`, even though those files are derived lifecycle
projections expected to change at bind and terminal projection. History,
mutable-surface confinement and protected bytes all passed. The candidate also
lacks dedicated supervisor/service/IPC test modules and retained evidence does
not contain the exact wheel, App-absent, crash-point and service identities
required by the H6-E gate. Independent review must not start from this candidate.

The repair boundary is exact:

- normalize only the two proven host-owned volatile namespaces
  `refs/codex/turn-diffs/**` and `refs/codex/snapshots/**`, including matching
  packed-ref and reflog treatment, while rejecting near-prefixes and preserving
  every branch, tag, remote, user ref, HEAD, index, config and ordinary history
  authority;
- make the controller-state digest cover immutable controller inputs and
  unknown state, while excluding only the ledger/lock/runtime and derived
  `.codex-flow/runs/**` projections that the controller itself must mutate;
  capsule bytes remain included and tampering still fails closed;
- add direct migration/queue/supervisor/IPC/worker/service tests and the named
  adversarial bounds, then retain exact sanitized wheel/service/App-absent,
  zero-poll, recovery and integrity evidence rather than a summary claim; and
- replay the failed App-native integrity sentinel and one App-absent real
  SDK-headless direct-submission sentinel. No App-native leaf guarantee may be
  invented; that path remains explicitly unsupported/fail-closed.

This is one bounded repair milestone because the authority fixes, regression
tests and retained evidence share the same controller/ledger integration owner.
Splitting them would create two owners for the same persisted and production
boundaries. The objective and architecture reviews remain two parallel,
first-class, read-only successors after the repaired candidate is frozen.

## Next execution — H6-E-R

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Repair H6-E's reproduced App-owned Git/controller-projection integrity "
        "failures and supply the missing direct supervisor/service/IPC and exact "
        "retained evidence gates without changing the accepted authority model."
    ),
    decomposition=(
        "Correct exact host-ref and derived controller-projection normalization with adversarial near-prefix and tamper tests.",
        "Add direct v10 queue, supervisor, IPC, worker, service, recovery and package tests for the existing implementation.",
        "Run the App-native regression and App-absent SDK-headless direct-submission sentinels and retain sanitized exact evidence.",
        "Freeze the repaired candidate for controller-owned objective and architecture review successors.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "The reproduced H6-E dispatch-integrity scenario closes without normalizing any ref or controller path outside the two exact host-ref prefixes and the named derived/runtime state.",
        "Capsule, ordinary Git authority, near-prefix refs, unknown controller files, protected surfaces and the starting dirty baseline remain fail-closed and byte-preserved.",
        "Dedicated tests directly exercise v10 migration/queue, singleton supervisor and recovery, bounded authenticated IPC, leaf worker submission and exact service identity, including the plan's adversarial bounds.",
        "One exact-wheel App-absent SDK-headless sentinel completes through direct IPC with zero controller-model polling/tokens; the App-native limitation remains truthful and recoverable.",
        "Retained sanitized evidence records exact candidate, wheel and executable digests, test counts, crash/recovery facts, App absence, zero-poll counters and integrity results without secrets or raw prompts/results.",
        "make check, focused tests, Ruff, diff hygiene and self-review pass; the terminal response is one raw schema-v1 JSON object and leaves both independent reviews pending.",
    ),
    mutable_surfaces=(
        "src/codex_flow/ledger.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/projection.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/service.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/app_native.py",
        "src/codex_flow/contracts.py",
        "pyproject.toml",
        "uv.lock",
        "tests/test_h2_ledger.py",
        "tests/test_h3_controller.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_h5_workflow_control.py",
        "tests/test_h6_model_facing_projection.py",
        "tests/test_h6_app_native.py",
        "tests/test_h6_supervisor.py",
        "tests/test_h6_service.py",
        "tests/test_h6_ipc.py",
        "docs/reviews/codex-controller-compatibility.md",
        "docs/reviews/evidence/h6-e-detached-supervisor.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "workflow.toml",
        "src/codex_flow/domain.py",
        "src/codex_flow/config.py",
        "src/codex_flow/native_profile.py",
        "src/codex_flow/worktrees.py",
        "src/codex_flow/h6_pilot.py",
        "plugins",
        "skills",
        "docs/reviews/evidence/h1-sdk-sentinel.json",
        "docs/reviews/evidence/h4-a-objective-pilot.json",
        "docs/reviews/evidence/h4-b-multi-authority-pilot.json",
        "docs/reviews/evidence/h5-workflow-control-medium.json",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Implement only H6-E-R from the canonical plan in the existing python-sdk-controller worktree. "
        "Preserve the complete dirty H6-E candidate and fix the two reproduced integrity boundaries without broad ignore rules. "
        "Add direct tests and exact sanitized evidence for every named supervisor/service/IPC/App-absent gate; repair implementation defects those tests expose within owned surfaces. "
        "Remain a leaf worker: do not create subagents, peer tasks, reviewers or successors, and do not poll Codex tasks. "
        "Do not modify the canonical plan, routes, protected surfaces, global configuration, installed services/plugins or other worktrees. "
        "Do not run medium/large parity pilots or retire legacy code. Return exactly one raw ModelFacingResult JSON object with independent reviews pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

H6-E-R is the sole executable implementation milestone. After its exact
candidate freezes, the controller may dispatch the objective review (Luna
XHigh) and architecture review (Sol Medium) in parallel on isolated read-only
review workspaces. Medium/large parity pilots remain blocked until both report
zero promotion-blocking P0/P1 findings.

### H6-E-V architecture correction — shared App-visible SDK authority

The user selected one definitive execution architecture after a live local
sentinel disproved the earlier visibility assumption. A normal persisted
`codex exec` thread created inside the saved project was immediately readable
and navigable by the Codex App as task
`01a03dce-3c69-7a10-8eda-48f465c4f3c9`; a projectless `/tmp` CLI thread was
also readable and navigable by exact id. The official Python SDK controls the
local Codex app-server and creates/resumes the same class of local Codex thread.
SDK execution is therefore not inherently invisible to the App.

The harness made its H6-E SDK thread invisible by forcing a private
`CODEX_HOME`. That same isolation also produced a real `401 Missing bearer or
basic authentication` worker failure because the private home did not own the
user's normal Codex authentication. The H6-E-R supervisor and child were stopped
before any new worktree delta. Its durable queue row remains recoverably
`running`: the current cancellation transition is itself defective because row
validation requires an immutable result for `cancelled`, although cancellation
must be terminal without an invented model result. H6-E-R is superseded and
must never be restarted or treated as implementation evidence.

#### Selected process, session and UI model

- The only production execution owner is the harness SDK worker. It uses the
  official local Codex app-server and the canonical user Codex session store;
  it does not set a private `CODEX_HOME`, copy authentication, scrape App state,
  or create a second App-native worker.
- Authentication, model access and normal Codex session persistence remain
  owned by the standard Codex runtime. Harness-specific queue, capability,
  result and successor authority remains repository-bound in SQLite. No secret,
  auth material or full Codex home is copied into the repository or ledger.
- The harness applies leaf and milestone policy through supported process/thread
  configuration overlays (`agents.enabled=false`,
  `features.multi_agent=false`, sandbox/approval/model/effort and bounded
  prompt/result settings) without rewriting the user's global config. Unsupported
  overrides fail before thread creation.
- The App is an optional UI over the same persisted SDK thread. While open it
  can display and let the user inspect that task; if closed before or during
  execution, the SDK/app-server and detached supervisor continue. Reopening the
  App must make the persisted task readable/navigable in its saved project.
  App presence, sidebar refresh and notification never own completion.
- `app_native` is retained only as explicitly labelled legacy compatibility
  during migration. It is not the production worker route, is never paired with
  an SDK worker for the same dispatch, and cannot waive SDK/ledger gates.
- The harness does not keep a controller model alive. SQLite and the supervisor
  deterministically close results, recover work and schedule already-authorized
  successors. An open App may surface the worker transcript and a best-effort
  source notification, but no model waits, polls or translates results.

This preserves what the harness adds beyond native App task management: one
durable lifecycle/result authority independent of UI; crash/restart idempotency;
FIFO queueing and bounded successor scheduling; typed capsule/result contracts;
model/effort/budget and leaf policy; worktree and mutable/protected ownership;
dependency-aware parallel lanes; first-class independent reviews; and retained
auditable evidence. It deliberately does not replace Codex inference,
authentication, transcripts, project registration or the App UI.

#### Recovery correction

The superseded H6-E-R dispatch
`model-74c74cfb35fe3e5195028c974f1af111/milestone-6fd650278d755dd490a5e9b6853c401b/executor/1`
must close exactly once as `cancelled`/superseded without a fabricated
`ModelFacingResult`, successor, notification or restart. Queue validation must
distinguish completed/failed result terminals from resultless cancellation, and
startup recovery must not claim a queue whose owning execution is already
cancelled. Migration/validation must accept only the internally consistent
cancelled shape and reject partial or conflicting terminal facts.

## Next execution — H6-E-V

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make the detached SDK route use the standard App-visible Codex thread "
        "and authentication authority while keeping all orchestration, result, "
        "recovery and ownership authority in codex-flow."
    ),
    decomposition=(
        "Replace private-CODEX_HOME execution with supported shared-session SDK/app-server configuration and immutable per-thread policy overlays.",
        "Repair resultless cancellation and recover the superseded H6-E-R queue without restart, duplicate work or invented result facts.",
        "Prove App-closed continuation plus later App readability/navigation of the same SDK thread in a saved project.",
        "Complete the missing supervisor/IPC/service adversarial tests and exact sanitized evidence before parallel independent reviews.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "A real SDK worker uses normal Codex authentication without copying secrets, persists one standard Codex thread, and is readable/navigable in the App under the selected saved project.",
        "Closing the App before or during the run does not stop queue, SDK/app-server, direct result submission, terminal commit or authorized successor scheduling; reopening surfaces the same thread id.",
        "Leaf, model, effort, sandbox, approval and bounded result policy are applied through supported per-process/thread configuration without modifying global Codex config.",
        "No production dispatch creates both SDK and App-native workers; App-native remains explicitly non-authoritative legacy compatibility.",
        "The superseded H6-E-R queue closes resultlessly as cancelled exactly once and can never be reclaimed, resumed, notified or given a fabricated result/successor.",
        "Dedicated queue/supervisor/IPC/worker/service tests, exact-wheel temporary install, App visibility/closure sentinels, make check, Ruff, diff hygiene and sanitized retained evidence pass.",
        "The implementation worker remains leaf, modifies only owned surfaces, sends no progress callbacks and emits exactly one raw schema-v1 ModelFacingResult with both independent reviews pending.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/config.py",
        "src/codex_flow/native_profile.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/projection.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/service.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/app_native.py",
        "src/codex_flow/contracts.py",
        "pyproject.toml",
        "uv.lock",
        "tests/test_h2_ledger.py",
        "tests/test_h3_controller.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_h5_workflow_control.py",
        "tests/test_h6_model_facing_projection.py",
        "tests/test_h6_app_native.py",
        "tests/test_h6_supervisor.py",
        "tests/test_h6_service.py",
        "tests/test_h6_ipc.py",
        "tests/test_h6_visible_sdk.py",
        "docs/reviews/codex-controller-compatibility.md",
        "docs/reviews/evidence/h6-e-detached-supervisor.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "workflow.toml",
        "src/codex_flow/worktrees.py",
        "src/codex_flow/h6_pilot.py",
        "plugins",
        "skills",
        "docs/reviews/evidence/h1-sdk-sentinel.json",
        "docs/reviews/evidence/h4-a-objective-pilot.json",
        "docs/reviews/evidence/h4-b-multi-authority-pilot.json",
        "docs/reviews/evidence/h5-workflow-control-medium.json",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone and implement only H6-E-V from the canonical plan in the existing python-sdk-controller worktree. "
        "Preserve the complete H6-E dirty candidate and the committed plan. Replace the private CODEX_HOME route with the official shared Codex session/auth path and supported per-thread leaf policy; never copy or expose auth. "
        "Repair the superseded queue as a resultless cancellation and prove it cannot restart. Add the direct tests, exact-wheel evidence, App-closed continuation and later App visibility sentinel specified by the plan. "
        "You are a leaf worker: create no subagents, peer tasks, reviewers or successors and do not poll Codex tasks. Do not modify protected/global state, install a real service, run parity pilots or retire legacy code. "
        "Return exactly one raw ModelFacingResult JSON object; objective and architecture reviews remain controller-owned and pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

H6-E-V is the sole executable implementation milestone. Its implementation
candidate must be frozen before the controller dispatches the Luna XHigh
objective review and Sol Medium architecture review in parallel. The old H6-E-R
capsule and queue are recovery inputs only, never parallel work.

## H6-E-W-R1 closure repair — credential-safe service start and bounded persisted-thread recovery

The complete dirty H6-E-W/H6-E-V candidate at base
`14106f3ee1e376f7c105d9e309292e9a742f0482` is retained as the donor and must
not be discarded or reconstructed. Its SDK validator now correctly accepts the
closed JSON-schema `type` union used by `ModelFacingResult.next_action`
(`string|null`) and has a regression test; `make check` currently passes 365
tests. `ModelFacingResult` is therefore not the unresolved boundary and this
repair preserves that patch.

The exact installed v6 user service is active, but dispatch
`model-32bb32979a14c09afc0209304413a34a/milestone-c2550c4b687d019b026361a2b7563669/executor/1`
is stale at `state=starting`, `attempt=93`, SDK thread
`01a03e38-9530-7db2-b0cc-a9e6a1d52c85`: its worker exited and no
`terminal_status` or `raw_result_sha256` was committed. The user manager lacked
the profile-selected provider environment key. A temporary manager injection
proved the next boundary and was removed. Once the key was available,
`thread_resume` raised `openai_codex.InvalidRequestError` code `-32600` with the
exact message `the thread already has an active writer`. These are retained
failure facts, not permission to resume blindly, create a concurrent writer or
rerun repository work.

### Outcome, ownership and non-goals

Outcome: an exact-wheel user service starts with an explicit volatile credential
handoff, and every worker exit leaves the dispatch in one durable actionable
state. Recovery inspects the persisted SDK thread before any writer is created,
ingests an already-terminal result when present, waits without model tokens for
a genuine live writer, or runs one bounded continuation only after proving the
thread idle. Ambiguity and exhausted budget close once as
`human_attention_required`.

The single implementation owner is `gpt-5.6-luna` with `xhigh` thinking in the
saved existing checkout `/home/adam/personal-workflow-skills.worktrees/python-sdk-controller`
on branch `agent/python-sdk-controller` at the stated base. Mutable production
surfaces are only `native_profile.py`, `service.py`, `ledger.py`,
`supervisor.py`, `worker.py`, `backends/codex_sdk.py`, and the minimal CLI seam;
their focused tests, compatibility note and H6-E evidence are owned with them.
All other dirty candidate files are retained donors, not cleanup targets.

Non-goals: H6-F live steer/interrupt/event/TUI control; Pydantic migration;
periodic App/task polling; generic prose classification; unbounded retries;
blind resume; concurrent writers; replay of repository implementation; a new
transport or private `CODEX_HOME`; secret copies; and any new worktree, commit,
push, merge, rebase, stash, discard, plugin/global-config mutation or later
milestone implementation.

### Secure service-start credential contract

- The validated native profile remains the sole source of the selected provider
  environment key *name*. Persist that name and its profile digest only; reject
  malformed, conflicting or drifted names. Never persist, render or hash the
  value in the unit, SQLite, JSON evidence, command arguments or logs.
- The explicit service-start command verifies that the named value exists in
  its invoking environment, transfers it to the user manager with
  `systemctl --user import-environment <NAME>` (the subprocess arguments contain
  the name only), and then starts the exact repository unit whose
  `PassEnvironment=<NAME>` entry also contains only the name. Any failure before
  confirmed start clears the imported manager entry where possible and returns
  typed `credential_unavailable` or `service_start_failed` evidence without
  echoing the value.
- The manager copy is volatile, not durable. It survives service restarts only
  while that user-manager instance retains it; manager restart/logout clears it,
  and the authenticated explicit start command must be run again. Automatic
  login/start without a newly available credential fails closed rather than
  reading a secret file or copying authentication. Tests restore the manager's
  pre-test environment state and never assert the value.

### Persisted-thread inspection and recovery state machine

The pinned SDK raw client exposes `thread_read(thread_id, include_turns=True)`
without `thread_resume`; the high-level `Thread` object exists only after a
resume. Add one narrow raw-client adapter boundary that performs exactly one
read-only persisted-thread inspection per recovery decision and converts a
closed, bounded snapshot into controller-owned facts. It must not create a turn,
stream events indefinitely or expose transcript prose beyond the minimum typed
terminal-result envelope.

Only `openai_codex.InvalidRequestError` with structured code `-32600` and exact
message `the thread already has an active writer` establishes `active_writer`.
Arbitrary message substrings and unrelated `-32600` errors remain ambiguous.
The transition model is:

```text
starting|running --worker exit--> recovery_inspection_pending
recovery_inspection_pending --terminal raw result--> completed|failed
recovery_inspection_pending --active writer--> recovery_retry_wait
recovery_retry_wait --eligible--> recovery_inspection_pending
recovery_inspection_pending --idle, dead owner, no result--> recovery_continuation_pending
recovery_continuation_pending --bounded continuation--> completed|failed|recovery_inspection_pending
any recovery state --ambiguous or budget exhausted--> human_attention_required
```

Worker-exit classification, recovery-budget consumption, next eligibility and
the transition out of `starting`/`running` commit in one SQLite transaction.
Each inspection outcome and its retry/continuation decision is likewise one
transaction. Service restart may claim only the already-persisted eligible
action; restart never resets `attempt`, inspection count or continuation budget.
No dispatch can remain `starting` or `running` after its bound process exit is
committed.

If inspection finds one terminal raw agent result, validate the complete payload
strictly as `ModelFacingResult` and ingest it through the existing capability,
terminal-commit and successor authority; summaries or partial transcript state
cannot substitute. If a writer is truly active, persist a bounded harness retry
deadline without controller-model tokens. If the prior process is dead and the
thread is idle with no usable result, the only resume prompt is a bounded
envelope/recovery continuation that begins with inspect-before-mutate facts,
forbids repeating already-observed repository work and asks only to recover or
emit the missing typed terminal envelope.

A fresh SDK thread is disabled by default. It is permitted only by an explicit
typed `fresh_thread_after_idle` policy, after the same inspection proves no live
writer and no usable result, within the shared durable budget. Its mandatory
inspect-before-mutate preamble identifies the existing workspace and instructs
the worker to examine the current diff/evidence before any mutation. Ambiguous
ownership, malformed history, an unclassifiable SDK error, conflicting results
or exhausted inspection/continuation budget transitions exactly once to
`human_attention_required` with bounded sanitized actionable facts.

### Persistence and migration decision

The dirty candidate already owns crash-atomic schema v11 and a live v11 ledger,
so H6-E-W-R1 adds one serialized v11-to-v12 migration. V12 stores the provider
environment key name/profile digest, structured worker-exit classification,
recovery state, inspection/continuation/fresh-thread policy and consumed budget,
next eligibility, last inspected thread/turn facts and the terminal
`human_attention_required` reason. It never stores provider values or unbounded
thread content. Migration fault points prove rollback and idempotent reopen;
shape validation rejects v11/v12 hybrids, a live queue with an exited bound
process, budget regression, duplicate recovery ownership and terminal mutation.

The stale attempt-93 dispatch is a required migration/recovery fixture. The
implementation must inspect its persisted thread before any continuation. It
may ingest a real terminal result, wait for the active writer to clear, or run
the bounded continuation according to the facts above; it may not manufacture a
result or reset the attempt. Any unavoidable destructive live-pilot prerequisite
is returned as a typed checkpoint rather than inferred.

### Acceptance, adversarial validation and promotion

Objective acceptance requires all of the following:

- focused tests cover missing credential, wrong key name, profile drift,
  import/start failure, redaction, manager restart/login semantics and cleanup;
- raw read distinguishes terminal result, exact structured active-writer error,
  idle/no-result, malformed/ambiguous history and unrelated errors without
  calling resume during inspection;
- crash points at worker exit and every inspection decision prove atomic state,
  monotonic budget, bounded eligibility, restart idempotency and no lingering
  `starting`/`running` rows;
- active writer never creates a concurrent writer; terminal result ingests once;
  dead-idle recovery uses only the bounded continuation; fresh-thread policy is
  off by default and its enabled path carries the inspect-before-mutate preamble;
- ambiguity and exhaustion create exactly one sanitized
  `human_attention_required` terminal fact and no successor/result invention;
- `make check`, Ruff, `git diff --check`, exact dirty-baseline/protected-surface
  checks and focused self-review pass with zero open P0/P1.

Architecture acceptance additionally requires an exact built wheel installed
into a disposable service root, with the user service started through the
credential handoff while the App is closed. The pilot must prove from the v12
ledger that a controlled worker exit reaches the correct recovery state, one
read-only persisted-thread inspection precedes any recovery writer, and the run
either ingests its existing result or closes through the bounded continuation
without duplicate repository work, result, successor or writer. Evidence must
record wheel/service/unit digests, key *name* and profile digest, dispatch/thread
identity, transitions, budgets and terminal digest, but no credential value or
raw transcript. Promotion then requires independent Luna XHigh objective and
Sol Medium architecture reviews of the exact frozen candidate with P0=0/P1=0;
those reviews are successors, not work for this executor.

## Next execution — H6-E-W-R1

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Close H6-E-W by adding credential-safe exact-service startup and bounded persisted-thread "
        "recovery so worker exit can never strand a dispatch or create a concurrent writer."
    ),
    decomposition=(
        "Implement the explicit volatile provider-credential handoff from the validated caller environment to the exact user service while persisting only the key name and profile digest.",
        "Add the crash-atomic v12 recovery state and atomic worker-exit decision boundary with bounded inspection, continuation and fresh-thread policy budgets.",
        "Inspect the persisted SDK thread once through raw thread_read before choosing terminal ingestion, active-writer wait, dead-idle bounded continuation or human attention.",
        "Recover the stale attempt-93 fixture without concurrent writers, blind rerun, invented results or reset budgets, preserving the existing ModelFacingResult union fix.",
        "Run adversarial migration/service/recovery tests and an exact-wheel App-closed service pilot, then freeze evidence for independent objective and architecture reviews.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Service start validates the profile-selected environment key, transfers only its value through volatile user-manager state, persists/logs only the name, and fails closed with typed actionable status when unavailable.",
        "Restart/login semantics are proven: the credential survives only its user-manager lifetime and authenticated explicit start is required after manager restart without secret files or CODEX_HOME copies.",
        "A worker exit atomically records classification, consumes bounded recovery budget, sets next eligibility and leaves starting/running; restart cannot reset or duplicate that decision.",
        "One raw thread_read inspection precedes every recovery writer and strictly distinguishes terminal result, exact InvalidRequestError -32600 active-writer facts, idle/no-result and ambiguity without prose matching.",
        "A terminal raw ModelFacingResult is strictly validated and ingested once; an active writer receives only bounded harness retry; a dead idle thread receives only the recovery continuation.",
        "Fresh thread creation is disabled by default and, when explicitly enabled after proving no live writer, carries the inspect-before-mutate recovery preamble and shares the durable budget.",
        "Ambiguous ownership/status or exhausted budget transitions exactly once to human_attention_required with sanitized actionable facts and no result, successor or writer invention.",
        "The v11-to-v12 migration is crash-atomic and the stale attempt-93 dispatch is recovered according to persisted facts without resetting attempt or blindly rerunning repository work.",
        "Focused adversarial tests, make check, Ruff, diff hygiene, exact wheel/service pilot, protected hashes and secret-negative evidence pass while the complete dirty donor remains intact.",
        "Independent Luna XHigh objective and Sol Medium architecture reviews of the exact combined candidate both report P0=0/P1=0 after at most one bounded concrete repair.",
    ),
    mutable_surfaces=(
        "src/codex_flow/ledger.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/service.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/native_profile.py",
        "tests/test_h2_ledger.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_h6_supervisor.py",
        "tests/test_h6_service.py",
        "tests/test_h6_visible_sdk.py",
        "tests/test_h6_plan_capsule.py",
        "docs/reviews/codex-controller-compatibility.md",
        "docs/reviews/evidence/h6-e-detached-supervisor.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "workflow.toml",
        "src/codex_flow/domain.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/projection.py",
        "src/codex_flow/worktrees.py",
        "src/codex_flow/app_native.py",
        "src/codex_flow/h6_pilot.py",
        "src/codex_flow/contracts.py",
        "src/codex_flow/config.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/plan_capsule.py",
        "pyproject.toml",
        "uv.lock",
        "plugins/personal-workflow-skills/skills/plan-work",
        "plugins/personal-workflow-skills/skills/workflow-control",
        "plugins/personal-workflow-skills/skills/codex-thread-handoff",
        "skills",
        "docs/reviews/evidence/h1-sdk-sentinel.json",
        "docs/reviews/evidence/h4-a-objective-pilot.json",
        "docs/reviews/evidence/h4-b-multi-authority-pilot.json",
        "docs/reviews/evidence/h5-workflow-control-medium.json",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone and implement only H6-E-W-R1 from the canonical plan in this exact existing checkout. Preserve the entire dirty H6-E-W/H6-E-V candidate, especially the ModelFacingResult union-validator fix and regression. "
        "Implement the v12 atomic recovery state machine and secure explicit systemd user-service credential handoff exactly as designed. Inspect persisted threads read-only before any writer; never classify arbitrary prose, create a concurrent writer, blindly repeat repository work or reset attempt/budget. "
        "Recover the stale attempt-93 fixture only according to persisted facts. Keep fresh-thread policy disabled unless the typed policy explicitly permits it after proving no live writer, and include the inspect-before-mutate preamble. "
        "Do not modify protected surfaces, H6-F control/TUI work, Pydantic, global/plugin state or Git topology/history. Create no subagents, peers, reviews or progress callbacks. "
        "Run the adversarial tests and exact-wheel App-closed service pilot; retain sanitized terminal evidence with no credential value. "
        "Return exactly one raw schema-v1 ModelFacingResult; objective and architecture reviews remain controller-owned and pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

H6-E-W-R1 is retained implementation evidence. H6-E-W/H6-E-V is its
same-worktree donor, not a parallel owner.

## Next execution — H6-E-W-R2

The exact H6-E-W-R1 candidate digest
`3387eff1e4ecb418768d06f5e14978f0a786c4b163190b7a8974c8138a820b6a`
and wheel digest
`b1f226c74aca64831c169e989a9c77213bbc166854515fe4742f121c57808ef6`
passed the focused objective recheck and the real temporary user-service
terminal-result route. The initial architecture pilot then reproduced one P1
on the required idle/no-result route: `begin_recovery_continuation` consumes
the continuation budget and leaves the dispatch claimed, but `_spawn_one`
reuses generation/attempt `g1-a1`. Its fresh token conflicts with the immutable
attempt capability and its capability/capsule/result files collide with the
existing `O_EXCL` paths. The failed spawn leaves `state=claimed`,
`recovery_state=none`, no live worker and no remaining continuation authority.
H6-E-W-R1 is therefore not promoted; all later H6 milestones remain protected.

The repair outcome is one crash-atomic continuation spawn identity that cannot
reuse immutable capability or file authority and cannot strand a claimed queue
row when process creation fails. This is an implementation-detail repair: it
does not change public/model-facing contracts, persisted result authority,
recovery budgets, successor semantics or the H6-F architecture.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make the bounded idle/no-result continuation executable exactly once with a unique "
        "crash-atomic spawn identity and no claimed-without-worker failure window."
    ),
    decomposition=(
        "Introduce the minimal monotonic continuation process/attempt identity needed by the existing v12 queue and capability authority.",
        "Bind capability, capsule, result and liveness paths to that identity without weakening immutable-token or O_EXCL guarantees.",
        "Make every failure before durable worker ownership restore one actionable bounded recovery state or terminal human attention without refunding consumed budget.",
        "Exercise the real _spawn_one continuation path, restart/fault windows and an exact-wheel App-absent service pilot through terminal closure.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "idle_no_result followed by begin_recovery_continuation and the production _spawn_one path starts exactly one continuation with a new immutable capability and non-colliding files.",
        "The continuation retains the same logical dispatch/thread recovery authority and consumed budget while its process attempt identity advances monotonically and survives restart.",
        "Failure before or during spawn cannot leave claimed/recovery_state=none without a live bound worker; retry/restart cannot duplicate a writer, result, successor or capability.",
        "Focused adversarial tests call the real spawn boundary and prove capability/token immutability, O_EXCL identity, crash atomicity, exhaustion and terminal immutability.",
        "An exact installed wheel runs a temporary real user service with the Codex App physically absent through worker exit, idle_no_result inspection, bounded continuation and terminal result, retaining secret-negative and deduplication evidence.",
        "make check, Ruff, git diff --check, protected-surface hashes and self-review pass with zero open P0/P1; independent objective and architecture rechecks of the exact candidate both return P0=0/P1=0.",
    ),
    mutable_surfaces=(
        "src/codex_flow/ledger.py",
        "src/codex_flow/supervisor.py",
        "tests/test_h6_supervisor.py",
        "docs/reviews/evidence/h6-e-detached-supervisor.json",
        "docs/reviews/codex-controller-compatibility.md",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "workflow.toml",
        "all other src/codex_flow production modules",
        "all other tests and accepted evidence",
        "plugins and global Codex state",
        "H6-F through H6-J surfaces",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone and implement only H6-E-W-R2 in the existing dirty python-sdk-controller worktree. Preserve every donor and concurrent change. Repair the reproduced continuation identity and spawn-failure atomicity defect without changing public contracts or later H6 surfaces. Add production-shaped tests that execute _spawn_one, then run the exact-wheel real user-service idle-continuation pilot with the Codex App physically absent. Do not create peers, callbacks, commits, worktrees or global mutations. Return one terminal schema-v1 ModelFacingResult."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=8_000,
)
```

H6-E-W-R2 is retained implementation evidence but is not promoted. Its exact
candidate passed the architecture code boundary except for the physically
App-closed pilot, while objective review reproduced a same-class spawn-failure
atomicity defect: `_close_failed_spawn` swallows a failed terminal cleanup
commit, leaving `starting/recovery_state=none`, no liveness, and a same-epoch
queue row that `recover_once` skips. The continuation identity repair itself
remains accepted input to the bounded Sol Medium continuation below.

## Next execution — H6-E-W-R3

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make failed-spawn terminalization durably recoverable even when its first ledger commit "
        "fails, so no same-epoch or restarted supervisor can strand a queue row."
    ),
    decomposition=(
        "Remove the swallowed LedgerError boundary and preserve one explicit detectable recovery obligation when failed-spawn terminalization cannot commit.",
        "Make same-epoch and restart reconciliation reclaim or terminalize a starting/claimed row with no live process or liveness without launching duplicate work.",
        "Keep invocation-owned artifact cleanup ordered behind durable authority so a commit fault cannot erase the facts needed for recovery.",
        "Inject commit, artifact-cleanup, process-launch and restart faults at the real _spawn_one boundary and retain exact evidence.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "A failed-spawn cleanup commit error is never swallowed or reported as successful terminalization.",
        "After any injected cleanup-commit failure the queue is either durably human_attention_required or remains in an explicitly reclaimable state that the same supervisor epoch and a restarted supervisor both reconcile exactly once.",
        "No fault window can leave starting/claimed plus recovery_state=none and no live process/liveness permanently skipped by recover_once.",
        "Recovery never creates a duplicate SDK writer, capability, result, successor or notification and never refunds continuation budget or reuses an immutable attempt identity.",
        "Production-shaped focused tests execute _spawn_one and same-epoch/restart recovery across commit and cleanup faults; make check, Ruff, diff hygiene and protected hashes pass.",
        "Independent objective and architecture rechecks of the exact repaired candidate return P0=0/P1=0 before the unchanged App-closed exact-wheel continuation pilot is attempted.",
    ),
    mutable_surfaces=(
        "src/codex_flow/ledger.py",
        "src/codex_flow/supervisor.py",
        "tests/test_h6_supervisor.py",
        "docs/reviews/evidence/h6-e-detached-supervisor.json",
        "docs/reviews/codex-controller-compatibility.md",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "workflow.toml",
        "all other src/codex_flow production modules",
        "all other tests and accepted evidence",
        "plugins, templates and global Codex state",
        "descriptive naming gate and H6-F through H6-J surfaces",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone and implement only H6-E-W-R3 in the existing dirty python-sdk-controller worktree as the Sol Medium same-class recovery authority. Preserve the accepted R2 monotonic attempt/capability repair and every concurrent naming/plan change. Repair the swallowed cleanup-commit fault and same-epoch recovery skip without changing public or persisted contracts, budgets or later surfaces. Add production-shaped real _spawn_one and restart fault tests. Do not run the App-closed pilot until code review passes, and do not create peers, callbacks, commits, worktrees or global mutations. Return one terminal schema-v1 ModelFacingResult."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=8_000,
)
```

H6-E-W-R3 is the sole executable milestone. One fresh Sol Medium implementation
owner receives the current diff, R2 validations and both open findings. It may
change implementation strategy inside the listed surfaces but may not change a
public/persisted contract, recovery budget, scope or acceptance gate. After it
freezes a new candidate, one focused Luna XHigh objective recheck and one Sol
Medium architecture recheck decide code promotion. The later user-owned gate
change below removes physical App closure as an external prerequisite.

H6-E-W-R3 is promoted at exact dirty candidate
`6e51b3eee12ff7cbf49c1dca7192dbce49e5af008cb298558017fcd67a73c448`.
Its Luna XHigh objective and Sol Medium architecture reviews both returned
`APPROVED`, P0=0/P1=0/P2=0, and all 19 protected hashes matched. The exact
wheel `a26ea6dbaf416917531f32004144a90e7c6ac15c3abe41ffff8dcf1065862bb6`
(206374 bytes) completed the detached idle-continuation service pilot with
attempt 1 to 2, one continuation, no duplicate writer/result/successor or
notification, no App API/task call, and complete service, credential and
temporary-artifact cleanup.

On 2026-08-27 the user explicitly removed physical App closure from this
promotion gate after two coordinator-only exit-sentinel failures. The retained
runtime proof is therefore App-present and App-independent at the API/task
boundary; it does not claim that the App was physically absent. The harness
architecture remains App-optional, and later integrated work may exercise
App-closed behavior when it is naturally available, but physical App closure
is no longer a blocking prerequisite. The retained evidence records this
limitation and user-owned gate change without relabelling `app_closed` as true.

## Descriptive naming cutover gate

This gate does not interrupt or expand H6-E-W-R3. It starts only after R3
promotion and closes before live-control implementation takes ownership of the
shared CLI, ledger and supervisor surfaces. Its outcome is descriptive stable
names for every still-mutable durable path and code/contract identifier. Public
source names use a forward-only atomic cutover: migrate all repository callers,
prove zero replacement reachability, then delete the old modules, identifiers
and commands without aliases or shims. Persisted protocol and immutable
provenance keep only the exact exceptions below. New artifacts in all later
milestones already use semantic names.

The work is grouped by compatibility boundary rather than by blind pattern:

1. A bounded private/test-path pass renames milestone-coupled test modules to
   capability names, including ledger integrity, controller worktrees, review
   lifecycle, multi-authority review, workflow control, App-native dispatch,
   local IPC, model-facing projection, plan compilation, production pilots,
   service lifecycle, supervisor recovery and shared SDK visibility. It also
   renames private schema-detection locals such as `has_h3`, `has_h6e`,
   `has_h6w` and `has_h6r` to the capabilities they detect. Pytest discovery,
   plan/evidence links, changed-path inventories and direct-suite labels update
   atomically; full pytest, `make check`, packaging and link checks must pass.
2. A public forward-only cutover introduces semantic canonical names for the
   review lifecycle and production-pilot APIs: `review_workflow`,
   `ReviewLifecycleResult`, `write_review_artifact`, lifecycle record methods,
   `PilotError`, `build_visible_worker_capsule`, `run_production_pilots`,
   `write_production_evidence`, `run_review_pilot`,
   `run_multi_authority_review_pilot` and `run_workflow_control_pilot`. Semantic
   CLI commands replace the numbered commands, and old public imports, modules,
   identifiers and commands are deleted after current callers migrate and a
   zero-reachability test passes. README, compatibility docs, exports, fixtures,
   `--help` and wheel behavior update together. No persisted value is rewritten
   in place and no deprecated alias or compatibility shim remains.
3. Add a new-name validator covering paths and Python/public identifiers. It
   rejects temporary milestone/task prefixes for additions while using an
   explicit allowlist for the protocol/provenance exceptions below. The
   validator evaluates the changed-name set, so it does not force a mechanical
   rewrite of immutable history.

The first pass is bounded/mechanical Luna High work. The second changes public
surfaces with a forward-only cutover and uses Luna XHigh with objective and architecture
acceptance. They run sequentially because both update shared references and
tests. Neither runs in parallel with an implementation owner touching the same
files. Historical evidence inventories keep their original filenames as
immutable provenance; link and naming validation distinguish those records
from current canonical names rather than rewriting accepted evidence.

## Next execution — private-semantic-naming

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Replace still-mutable milestone-coupled private and test identifiers with stable "
        "capability and behavior names, and enforce the descriptive-name rule for additions."
    ),
    decomposition=(
        "Rename the current test modules to ledger, controller, review, workflow-control, App dispatch, IPC, projection, plan compilation, production-pilot, service, supervisor and SDK-visibility capability names.",
        "Rename private ledger schema-detection locals to the table or recovery capability they detect.",
        "Add a changed-name validator for paths and Python identifiers with explicit protocol/provenance exceptions.",
        "Update current pytest, plan, compatibility and validation references while leaving retained historical evidence bytes unchanged.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE,),
    acceptance_criteria=(
        "No newly canonical test path, private identifier or validator-owned addition uses a milestone, task, thread, model or sequence label.",
        "Every renamed test is collected exactly once and preserves its prior behavioral coverage; imports, direct-suite commands, package checks and current documentation links resolve.",
        "The validator rejects representative h-number, s-number, milestone, task, thread and model coupling in files and Python identifiers while accepting documented persisted-protocol and immutable-provenance exceptions.",
        "Public pilot modules, imports, classes, functions, CLI commands, persisted values and retained evidence remain unchanged for the separate forward-only cutover pass.",
        "Focused validator/collection tests, full pytest, make check, packaging, git diff --check and self-review pass; one Luna XHigh objective review of the exact candidate returns P0=0/P1=0.",
    ),
    mutable_surfaces=(
        "still-mutable tests/test_h*_*.py files and their current non-historical references",
        "private capability-detection locals in src/codex_flow/ledger.py",
        "one semantically named descriptive-name validator and its focused tests",
        "Makefile, pyproject.toml or .pre-commit-config.yaml only where required to run that validator",
        "current compatibility documentation references owned by this pass",
    ),
    protected_surfaces=(
        "all public production modules, imports, exports, classes, functions and CLI commands",
        "persisted ledger/schema/event/projection identities and runtime layouts",
        "docs/reviews/evidence including the promoted detached-supervisor evidence",
        "canonical plan and AGENTS.md except for planning-controller updates",
        "global Codex/plugin state, Git topology/history and unrelated worktrees",
        "H6-F through H6-J production surfaces",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only the private semantic naming pass in the existing dirty python-sdk-controller worktree. Preserve every donor and concurrent change. Perform reference-aware renames, not blind substitution; keep public and persisted compatibility surfaces plus retained evidence byte-stable. Add the validator and tests, run the full gates, self-review the exact diff, and return one terminal schema-v1 ModelFacingResult."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=8_000,
)
```

This private semantic naming capsule is now the sole executable milestone. Its
Luna High implementation owner owns the listed mutable surfaces; after the
exact result is frozen, one Luna XHigh objective review decides promotion.

The private semantic naming pass is promoted at exact dirty candidate
`dc35d203596d3eebbd5759f69351a20cdf169230a7ebe4c23335d9241617b4c2`.
It renamed all 13 still-mutable test modules, private ledger capability probes
and the changed-name validation surface. The single objective review found one
P1 regression in real wheel-content coverage; the one bounded repair restored
the temporary wheel build and exact packaged `workflow.toml` byte comparison.
The final Luna XHigh recheck returned `APPROVED`, P0=0/P1=0/P2=0, with 405
tests, packaging, validation, Ruff, compile, pre-commit and diff hygiene green.

## Next execution — public-api-semantic-cutover

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Replace milestone-coupled or generic public module, class, API and CLI names with stable "
        "semantic names through one forward-only atomic cutover."
    ),
    decomposition=(
        "Move review, workflow-control, production-pilot and SDK/controller sentinel implementations into semantically named modules, migrate every current caller and delete the old module paths.",
        "Replace public review lifecycle classes, controller and ledger methods, artifact writers and production-pilot functions with semantic names; delete the old identifiers after zero-reachability proof.",
        "Replace numbered or superseded CLI commands with semantic commands and remove the old command registrations after current documentation and callers migrate.",
        "Replace mutable pilot-internal run, scenario, fixture, path and private type names with capability or observable-behavior names while retaining persisted schema/event values and historical evidence unchanged.",
        "Update exports, current callers, README, compatibility documentation, tests, wheel contents and descriptive-name validation together.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Production imports use review_pilots, workflow_control_pilot, production_pilots, sdk_compatibility_sentinel and controller_recovery_sentinel; the old module paths are absent from source and wheel.",
        "Canonical public identifiers include ReviewWorkflow, review_workflow, ReviewLifecycleResult, write_review_artifact, semantic lifecycle ledger methods, PilotError, build_visible_worker_capsule, run_review_pilot, run_multi_authority_review_pilot, run_workflow_control_pilot, run_production_pilots and write_production_evidence.",
        "Semantic CLI commands are documented and primary; old numbered commands and old public imports fail as removed surfaces and have no alias, shim or duplicate registration.",
        "No new or canonical module, class, function, method, variable, constant, test, fixture, command, export, scenario id or generated path couples to a milestone/task/thread/model/sequence label.",
        "Persisted ledger schema identities, event kinds, envelopes, model/milestone ids, runtime layouts and immutable H1-H6 evidence bytes are unchanged; migration tests prove old persisted values remain readable without preserving removed source names.",
        "Focused cutover/reachability/validator tests, full pytest, make check, exact wheel/import/help checks, Ruff and diff hygiene pass; independent Luna XHigh objective and Sol Medium architecture reviews of the exact candidate both return P0=0/P1=0.",
    ),
    mutable_surfaces=(
        "src/codex_flow/review_pilots.py and deletion of src/codex_flow/h4_pilot.py",
        "src/codex_flow/workflow_control_pilot.py and deletion of src/codex_flow/h5_pilot.py",
        "src/codex_flow/production_pilots.py and deletion of src/codex_flow/h6_pilot.py",
        "src/codex_flow/sdk_compatibility_sentinel.py and deletion of src/codex_flow/sentinel.py",
        "src/codex_flow/controller_recovery_sentinel.py and deletion of src/codex_flow/controller_sentinel.py",
        "src/codex_flow/controller.py, domain.py, artifacts.py, ledger.py and cli.py only for semantic replacement APIs and removal of old source names",
        "public exports, focused current tests, README.md and docs/reviews/codex-controller-compatibility.md",
        "src/codex_flow/descriptive_naming.py and scripts/validate.py only for canonical public-identifier enforcement and explicit compatibility exceptions",
    ),
    protected_surfaces=(
        "persisted schema identities, migrations, event values, envelopes, dispatch identities and runtime layouts",
        "detached supervisor recovery behavior, SDK transport semantics, IPC, service and worker lifecycle",
        "docs/reviews/evidence and all immutable historical evidence bytes",
        "canonical plan and AGENTS.md except for planning-controller updates",
        "global Codex/plugin state, Git topology/history and unrelated worktrees",
        "H6-F through H6-J production surfaces beyond compatibility-only references",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only the public API semantic cutover in the existing dirty python-sdk-controller worktree. Preserve every donor and concurrent change. Migrate all current callers to one semantic implementation per capability, prove zero reachability, delete the old public modules, identifiers and commands, and create no alias or shim. Never rewrite persisted protocol or retained evidence. Update callers/docs/exports/validator atomically, run the full objective and packaging gates, self-review, and return one terminal schema-v1 ModelFacingResult. Do not start live-control or later milestones."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=8_000,
)
```

This public semantic cutover capsule is now the sole executable milestone. One
Luna XHigh owner controls every shared module, export, CLI and cutover surface
in the listed surfaces. No parallel writer may touch those references;
after implementation freezes, objective and architecture reviews may run in
parallel because both are read-only.

Successor routing decision: after this cutover is promoted, the planning
controller starts the live-worker-control implementation through exactly one
`$codex-thread-handoff` native `START`, not a child/subagent. The handoff reuses
the existing worktree `/home/adam/personal-workflow-skills.worktrees/python-sdk-controller`
on branch `agent/python-sdk-controller` at the then-current saved checkout,
requests the AGENTS-authorized exact pair `model=gpt-5.6-luna` and
`thinking=xhigh`, and captures the native thread/host identity plus the exact
controller callback route. It performs no create retry, fallback transport,
new worktree or routine polling. Dispatch is not completion; later progression
requires the peer's one terminal callback and the planning controller's gate
reconciliation.

Explicit exceptions:

- Generated `model-*`/`milestone-*` identities, `--milestone-id` and the
  `.codex-flow/.../milestones/<id>` layout are persisted controller protocol;
  projection, ledger foreign-key and compatibility tests retain them.
- Ledger schema identities through v12, the `__h4`/`codex-flow/h4/v1` envelope,
  H4 sidecar namespace and existing H6 event kinds are versioned persisted
  protocol. A future schema migration may introduce a semantic successor
  version, but never rewrites or aliases the old values.
- Published H1/H3/H4/H5 evidence, schema strings and sentinel identifiers are
  immutable historical provenance. The current H6-E evidence path, keys and
  schema are the same kind of frozen exception after R3 promotion; current test
  paths have already completed the private naming cutover.
- Existing numbered public pilot commands and imports are forward-only removal
  inputs. The public cutover migrates current callers and documentation, proves
  zero reachability and deletes them without aliases or shims.

Future retained evidence paths are already reserved as
`live-worker-control.json`, `controller-turn-recovery.json`,
`plugin-capability-parity.json`, `human-terminal-ui.json` and
`integrated-control.json`; no later capsule may reintroduce milestone-coupled
filenames, symbols, commands or evidence keys.

## Post-detached-supervisor control plane — live workers, recoverable controller and human UI

The work below starts only after H6-E-W-R3 and the descriptive naming gate reach
truthful terminal results, their exact candidates are frozen and their required
promotion gates close. App-native is not a production fallback: production
workers and controller turns use the shared standard SDK session, while the App
is an optional transcript projection.

The next program sequence is:

```text
H6-E-W-R3 failed-spawn recovery and wake-up
                  |
                  v
descriptive naming compatibility
                  |
                  v
H6-F live worker observation, control and bounded recovery
                  |
                  v
workflow skill and validation contract reconciliation
                  |
                  v
H6-G transactional controller-turn recovery
                  |
                  +-------------------+
                  v                   v
H6-H plugin capability parity     H6-I human TUI
                  +-------------------+
                              |
                              v
                  H6-J integrated promotion
```

H6-F, workflow-skill reconciliation and H6-G are deliberately sequential. H6-F
and H6-G both own the durable ledger/supervisor/SDK control boundary, while the
intermediate reconciliation owns shared instructions, controller-facing
contracts and test selection that H6-G must consume. After H6-G freezes that
boundary, H6-H and H6-I may execute in parallel: plugin parity owns native
discovery and capsule capability binding, while the TUI consumes the
already-frozen control client without modifying the supervisor, ledger or SDK
adapter. H6-J freezes the combined candidate and runs one parallel
objective/architecture promotion wave; it does not reopen broad implementation
unless a concrete P0/P1 finding requires one bounded repair.

### Shared outcome and invariants

The harness, not an App task or prompt, owns worker lifecycle, recent activity,
control commands, terminal results, controller decisions and recovery. A human
can inspect and steer work without spending model tokens. A controller model is
an episodic decision authority that can disappear and be restarted; it is never
the durable queue or callback receiver.

- `codex-flowd` plus SQLite remain the only lifecycle and result authority.
- Worker event streams are diagnostic evidence, never completion authority.
  Retain a bounded redacted ring rather than copying full Codex transcripts.
- Reading, steering and interruption are capability-bound to one dispatch,
  generation, attempt, SDK thread and active turn. A command can never be
  replayed against a replacement turn merely because text looks equivalent.
- App closure, TUI closure and controller-turn failure do not stop workers,
  erase results, suppress pre-authorized successors or mutate terminal facts.
- The App remains optional and may show SDK-created worker/controller threads
  after reopening. No production path uses App-native creation, App polling,
  private Desktop sockets or App-owned callback prose.
- The standard shared `CODEX_HOME` is used without copying authentication.
  Plugin availability is proven and bound explicitly; catalog presence or App
  installation alone is not treated as runtime capability.
- The controller and TUI use the same typed control API. TUI actions perform no
  model call unless the human explicitly requests a controller-model decision.
- No milestone installs/trusts/uninstalls plugins, changes global Codex config,
  broadens permissions, pushes, rebases, merges, stashes or discards user work.

### H6-F — Live worker observation, control and bounded recovery

Outcome: while an SDK worker is alive, the supervisor and authorized clients
can read bounded recent activity and submit exactly ordered steer or interrupt
commands without App dependency, task polling or controller-model keepalive;
when a worker or turn fails, the harness applies a durable error-specific retry
policy rather than stopping after every recoverable defect or looping without a
budget.

Architecture and persistence:

- Extend the SDK boundary to retain the live `TurnHandle`, consume its event
  stream and expose the installed SDK's `Thread.read`, `TurnHandle.steer` and
  `TurnHandle.interrupt` capabilities through typed adapter methods. Persisted
  thread reads are recovery diagnostics; the live SDK stream is the normal
  activity source.
- Add one crash-atomic schema migration after H6-E-W. Store a bounded per-
  dispatch diagnostic ring with monotonic sequence, event kind, timestamp,
  optional bounded redacted agent text and payload digest. Default bounds are
  128 entries, 64 KiB total and 8 KiB per text item; oldest diagnostic entries
  are evicted transactionally without touching authoritative lifecycle facts.
- In the same migration add immutable retry-policy facts and mutable recovery
  state per dispatch: policy version, bounded budgets by failure class,
  consumed count, last typed failure, recovery strategy, next eligible time,
  prior thread/turn identity and terminal `human_attention_required` reason.
  `attempt` remains process-attempt identity and is never interpreted as a
  retry budget. Every failure and its retry decision commit atomically before
  another process or SDK turn may start, so supervisor restart cannot reset a
  budget or duplicate a recovery.
- Classify failures at their owning boundary without matching arbitrary prose.
  Pre-identity transient transport/runtime failures use bounded exponential
  backoff with jitter; the default budget is five. One structured
  `Invalid previous_response_id` may retire the unusable continuation and
  start one fresh SDK thread in the same workspace. A schema-invalid terminal
  result may request at most two correction turns on the same thread, carrying
  only the validator-owned field/type violations and the unchanged output
  schema. Worker process loss after a bound identity may resume once only when
  persisted SDK evidence makes continuation safe. Authentication, permission,
  native-profile, capability, workspace-integrity, malformed-input and replay
  failures are non-retryable and fail closed.
- Recovery never reruns completed repository work blindly. Same-thread schema
  repair asks only for a corrected terminal envelope; fresh-thread recovery
  receives a harness-authored bounded recovery preamble identifying the
  existing workspace and requiring inspection before mutation. Successful
  result commit clears pending retry eligibility atomically. Exhausted budgets,
  repeated same-class failure, conflicting identity or an ambiguous external
  side effect transition the dispatch to `human_attention_required`, enqueue
  one durable controller/human decision and stop automatic execution.
- Expose retry class, budget/consumption, last failure, next retry time and
  recovery strategy through the typed control client and CLI/TUI-facing status.
  Permit an authorized controller or human to retry, cancel or change a budget
  only through a compare-and-swap action with an explicit reason; never by
  editing SQLite, restarting the service or resetting `attempt`.
- Add a durable control-command outbox with immutable command id, dispatch,
  generation, attempt, thread/turn identity, kind (`steer` or `interrupt`),
  bounded payload digest, state and timestamps. Steer text is at most 8 KiB.
- Upgrade authenticated local IPC to a bidirectional live-worker control
  channel. The supervisor pushes commands; the worker sends lifecycle/activity
  events and command acknowledgements. IPC loss never fabricates command
  application or terminal status.
- A worker restart may resume its bound SDK thread only under the durable
  typed recovery policy. An unacknowledged steer from a lost turn becomes
  `unresolved` and is never applied to a new turn. Interrupt is confirmed only
  by SDK terminal evidence; process termination alone is reported separately.
- Expose a stable typed control client plus noninteractive CLI operations for
  status, recent activity, steer and interrupt. The controller and later TUI
  consume that client rather than reading SQLite tables or App tasks directly.

Non-goals: no unbounded or service-restart-reset retry, generic retry of
integrity/auth/permission failures, TUI, plugin policy, controller-decision
retry, full transcript archive, recurring status poller, App-native worker or
remote-network control plane.

Mutable owner:

- `src/codex_flow/domain.py`, `src/codex_flow/contracts.py`,
  `src/codex_flow/ledger.py`, `src/codex_flow/supervisor.py`,
  `src/codex_flow/worker.py`, `src/codex_flow/ipc.py`,
  `src/codex_flow/service.py`, `src/codex_flow/backends/codex_sdk.py`,
  `src/codex_flow/control_client.py`, `src/codex_flow/cli.py`;
- focused adapter, ledger, IPC, worker, supervisor, service and CLI tests; and
- `docs/reviews/evidence/live-worker-control.json`.

Protected surfaces: the canonical plan and `AGENTS.md`; H6-E-W retained
evidence; `src/codex_flow/native_profile.py`, plan/capsule projection,
App-native compatibility, plugin sources/caches/config, future TUI modules,
accepted H1-H5 evidence, global Codex state and unrelated worktrees.

Acceptance modes: `objective` and `architecture`.

Acceptance and validation:

- A real delayed SDK worker emits activity visible through the control client
  without any App API/task dependency, and one steer changes its subsequent
  observable response through the same live turn. The App may be open or
  closed and its physical state is recorded truthfully rather than gated.
- One interrupt reaches the exact live turn and closes with truthful SDK
  terminal evidence; replay, stale generation, wrong attempt/turn, oversized
  text, malformed IPC and post-terminal commands fail before mutation.
- Supervisor/worker restart and IPC failure injection produce no duplicate
  event sequence, steer, interrupt, worker, result, successor or callback.
- Failure-matrix tests prove exact durable budgets and strategies: five
  backoff-gated pre-identity retries, one invalid-chain fresh-thread rollover,
  two same-thread schema-envelope corrections and zero automatic retries for
  auth, permission, capability, profile or integrity failures. Restart between
  failure and retry neither resets nor double-consumes a budget; success on any
  allowed retry produces one terminal result and suppresses all later work.
- Exhaustion and ambiguous post-identity cases produce one observable
  `human_attention_required` decision containing only bounded sanitized facts.
  Historical H6-E attempts with inflated process counts migrate without being
  mistaken for consumed typed retry budget.
- The diagnostic ring proves byte/count eviction, redaction and separation from
  terminal authority at 0/1/128/129 entries and boundary payload sizes.
- Focused adversarial tests, `make check`, Ruff, diff hygiene, exact wheel and
  temporary service checks pass. Self-review reports zero open P0/P1; integrated
  independent promotion is deferred to H6-J.

Residual boundary: after loss of the process that owns an active SDK
`TurnHandle`, the harness may read persisted history but cannot claim it can
steer that exact in-flight handle. It reports the command unresolved and uses
the existing fail-closed recovery policy.

### Workflow skill and validation contract reconciliation

Outcome: reconcile the previously exported workflow instruction/schema package
with the newer typed controller so repository-owned skills, templates, routing
examples and test selection describe one forward-only production workflow. The
dirty checkout `/home/adam/personal-workflow-skills` is read-only source input,
not an authority to copy blindly. Its exported behavior is compared file by
file with this worktree; current typed-capsule, naming and controller ownership
improvements win wherever the export is older.

The reconciliation delivers these semantic capability surfaces:

- retain and merge, rather than replace, the current `plan-work` and
  `execute-milestone` skills;
- add `review-work` and `recover-milestone` for independent findings-first
  review and completion-biased bounded recovery;
- add `collect-evidence`, `run-discovery-spike` and
  `define-visual-contract` with their matching skill metadata;
- add the Draft 2020-12 capsule, evidence, finding, recovery, result, review
  and visual-contract schemas under `schemas/`;
- add `templates/AGENTS.workflow.md`, integrate it through
  `templates/AGENTS.md`, and add `config/workflow.toml.example` without
  introducing a second active plan, router or runtime entrypoint;
- update root `AGENTS.md`, the general template and every applicable workflow
  skill with the descriptive durable-naming invariant and proportional
  validation policy plus the decision-ready implementation architecture-map
  and bounded new-artifact-budget invariant; and
- add semantic `test-contracts`, `test-controller`, `test-workers`,
  `test-integrations` and `test-workflow-assets` Make targets backed by
  `config/test-partitions.toml`, while preserving `make test` as the complete
  strict suite and `make check` as the explicit broad gate.

The skills remain cognitive roles around the controller. They may define typed
intent, acceptance, evidence or findings, but may not own durable dispatch,
callbacks, successor scheduling, retry loops, worktree invention or direct
ledger mutation. `workflow-control` and the closed capsule compiler remain the
only normal model-to-controller entrypoint. No deprecated alias, compatibility
shim or milestone-labelled duplicate is introduced.

Implementation architecture map:

- `modify AGENTS.md`, `modify templates/AGENTS.md`, `modify
  plugins/personal-workflow-skills/skills/{plan-work,execute-milestone}/SKILL.md`
  and their existing `agents/openai.yaml`: merge controller ownership,
  descriptive naming, proportional validation and the frozen architecture-map
  contract without losing stronger current wording.
- `create plugins/personal-workflow-skills/skills/{review-work,recover-milestone,
  collect-evidence,run-discovery-spike,define-visual-contract}/{SKILL.md,
  agents/openai.yaml}`: five independently discoverable cognitive roles. They
  depend only on canonical instructions and typed schemas, never on native task
  transport, the ledger or one another.
- `create schemas/{capsule,evidence,finding,recovery,result,review,
  visual-contract}.schema.json`: seven closed Draft 2020-12 contracts with
  stable semantic `$id` and title values. The typed Python contracts remain the
  serialization authority; schemas validate projections and never generate a
  second runtime model.
- `create templates/AGENTS.workflow.md`, `create
  config/workflow.toml.example`, and `create config/test-partitions.toml`:
  declarative includes, role-class routing example and the single primary test
  membership registry. These assets have no side effects and no model ids leak
  into skills.
- `modify scripts/validate.py`: remain the one repository workflow-asset
  validator and partition selector. It validates schemas through Draft 2020-12,
  skill/front-matter metadata, Markdown fences, template/config resolution,
  naming, architecture-map fixtures and exact one-partition membership; invalid
  or stale input fails before pytest dispatch.
- `modify Makefile`: expose only the five semantic test targets and delegate
  membership resolution to `scripts/validate.py`; keep `test` and `check`
  unchanged as the full gates. No second test manifest or shell-maintained file
  list is allowed.
- `modify pyproject.toml`, `modify uv.lock`, `modify
  plugins/personal-workflow-skills/.codex-plugin/plugin.json`, and `modify
  .agents/plugins/marketplace.json`: add `jsonschema` as a dev-only validator
  dependency, package the schemas plus workflow template/config example into
  the wheel's read-only `codex_flow/workflow_assets/` tree, and advance matching
  plugin metadata exactly once. No runtime dependency or installed plugin state
  changes.
- `create tests/test_workflow_assets.py` and `modify
  tests/fixtures/prompt-input/plan-work.json`: one parameterized semantic test
  module owns positive/negative schema fixtures,
  skill/template/config/package checks, architecture-map rejection and
  partition-union adversaries, while the existing prompt fixture records the
  new planning contract. Do not create a fixture file per example.
- `create docs/reviews/evidence/workflow-skill-contracts.json`: sanitized
  source/destination digest inventory, adopted/merged/superseded decisions,
  partition counts and exact-wheel asset proof. It contains no prompt bodies or
  secrets.
- `preserve src/codex_flow/**` except the wheel force-included read-only assets;
  `preserve` all existing audit skills, `codex-thread-handoff`,
  `workflow-control`, hooks and runtime entrypoints; `remove` nothing in this
  milestone.

Primary durable identifiers are the seven schema `$id` values, the seven
workflow skill names, the five partition names and Make targets, and evidence
schema `codex-flow/workflow-skill-contracts/v1`. Dependency direction is
schemas/templates/config -> validator/tests -> Make targets; plugin cognitive
skills may reference schemas/instructions, while controller/runtime production
modules never import plugin or test assets. Error handling is fail-closed at
schema, naming, metadata, include, packaging and partition boundaries. The
new-artifact budget is 22 files: ten files for five skill packages, seven
schemas, three declarative template/config files, one semantic test module and
one evidence record. The executor must report the exact final count and request
a bounded replan before exceeding 22 total new files.

Mutable owner:

- `AGENTS.md`, `templates/AGENTS.md`, `templates/AGENTS.workflow.md` and
  `config/workflow.toml.example`;
- `plugins/personal-workflow-skills/skills/{plan-work,execute-milestone,review-work,recover-milestone,collect-evidence,run-discovery-spike,define-visual-contract}`
  including their `agents/openai.yaml` metadata;
- the seven semantic JSON schemas under `schemas/`, plugin packaging/validation
  metadata required to ship them, `Makefile`, `config/test-partitions.toml` and
  the repository validator/tests for these assets; and
- `docs/reviews/evidence/workflow-skill-contracts.json`.

Protected surfaces: the frozen live-worker ledger, SDK, supervisor, IPC,
service, worker, control-client and CLI behavior; controller-turn recovery;
plugin capability discovery; TUI implementation; App-native compatibility;
installed plugin/cache/global Codex state; accepted immutable evidence and
unrelated worktrees. The source export checkout is read-only and receives no
mutation.

Acceptance modes: `objective` and `architecture`.

Acceptance and validation:

- A source/destination digest inventory accounts for every exported capability
  and records whether it was adopted, merged or superseded with a concrete
  reason; no current improvement in `plan-work` or `execute-milestone` is lost.
- All seven schemas validate against Draft 2020-12, their positive/negative
  fixtures exercise required fields and closed boundaries, and typed controller
  projections agree with the applicable schema without creating a second
  serialization authority.
- Every new skill has valid front matter and metadata, balanced Markdown fences,
  bounded examples and explicit non-ownership of callbacks, routing, retries,
  successor scheduling and ledger mutation. Template includes and TOML examples
  resolve from a packaged wheel as well as from the repository.
- The naming validator accepts the new semantic artifacts and rejects
  milestone/task/thread/model/sequence coupling in durable paths and code or
  contract identifiers, including classes, functions, fixtures, schema titles,
  evidence keys and example-generated filenames. There are no aliases or shims.
- Planning skill fixtures prove that a substantial capsule names its expected
  production paths, module responsibilities/dependencies, primary durable
  identifiers and bounded new-artifact budget; a capsule that delegates module,
  file or public-class topology to the executor fails workflow-asset validation.
- Every collected `tests/test_*.py` file belongs to exactly one primary semantic
  partition; duplicate entries, missing tests and stale manifest paths fail
  validation. Each partition runs independently from the repository root and
  their union equals the full collected suite.
- Skill/schema/template-only edit loops use `test-workflow-assets` plus the
  relevant validators. Because this milestone changes shared contracts, test
  collection and packaging, closure runs all five partitions, full `make check`,
  exact-wheel asset inspection, `git diff --check` and complete self-review.
- One independent Luna XHigh objective review and one Sol Medium architecture
  review of the exact candidate both return P0=0/P1=0 before H6-G starts.

This reconciliation becomes the next executable milestone only after
live-worker-control freezes and passes its declared objective and architecture
promotion. It is sequential with controller-turn recovery because both the
recovered controller role and later plugin/TUI work consume these instruction,
schema and validation contracts.

## Next execution — workflow-skill-and-validation-contract-reconciliation

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Reconcile the exported workflow skills and schemas into one forward-only controller-owned "
        "instruction, packaging and proportional-test contract without changing runtime control behavior."
    ),
    decomposition=(
        "Inventory every donor skill/schema/template/config capability and record adopted, merged or superseded status by digest.",
        "Implement the fixed 22-file architecture map: five semantic skill packages, seven closed schemas, three declarative assets, one semantic test module and one sanitized evidence record.",
        "Merge current plan/execute improvements and enforce descriptive naming, implementation architecture maps and proportional validation across instructions and fixtures.",
        "Make scripts/validate.py the sole schema/skill/naming/partition selector and expose five semantic Make test targets from one TOML membership registry.",
        "Package read-only workflow schemas/template/config assets into the exact wheel, update local plugin metadata once and prove repository/wheel parity without installation."
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Every donor capability has source/destination digests and a concrete adopted, merged or superseded reason; no stronger current plan-work or execute-milestone behavior is lost.",
        "Seven Draft 2020-12 schemas and positive/negative projections are closed, while typed Python contracts remain the only serialization authority.",
        "Seven cognitive workflow skills have valid metadata and own no dispatch, callback, retry, successor, worktree or ledger behavior.",
        "Planning rejects executor-invented module/file/public-class topology and requires the frozen architecture map plus bounded artifact budget; all durable names are semantic and no alias exists.",
        "Every collected test has exactly one contracts/controller/workers/integrations/workflow-assets membership; each target runs independently and their union equals full collection.",
        "All five partitions, one full make check, exact-wheel asset parity, plugin validation, git diff hygiene and complete self-review pass with zero open P0/P1."
    ),
    mutable_surfaces=(
        "the exact create/modify paths in the workflow-skill implementation architecture map",
        "pyproject.toml and uv.lock only for dev-only Draft 2020-12 validation and wheel force-includes",
        "plugins/personal-workflow-skills/.codex-plugin/plugin.json and .agents/plugins/marketplace.json for one matching local package version",
        "tests/fixtures/prompt-input/plan-work.json",
        "docs/reviews/evidence/workflow-skill-contracts.json"
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "src/codex_flow production Python modules and every live-worker/controller runtime behavior",
        "existing audit skills, codex-thread-handoff, workflow-control and plugin hooks",
        "controller recovery, plugin parity, TUI and integrated-promotion successor surfaces",
        "source export checkout /home/adam/personal-workflow-skills, installed plugin/cache/global Codex state and unrelated worktrees"
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer"))
    ),
    prompt=(
        "Use execute-milestone for only workflow-skill-and-validation-contract-reconciliation in the existing dirty python-sdk-controller worktree. Follow the exact architecture map and 22-file budget; request a bounded replan before adding any unplanned module, file, public class, schema, runner, registry or entrypoint. Treat /home/adam/personal-workflow-skills as read-only donor input and merge rather than overwrite stronger current plan/execute contracts. Use semantic forward-only names and no aliases. Run test-workflow-assets and validators during implementation, then the five partitions, one full make check and exact-wheel/plugin/package gates once at closure. Preserve all runtime code and accepted evidence, create no peers/subagents/worktrees/commits, perform no install/global/App mutation, and send one terminal callback to the planning controller. Do not start controller recovery, plugin parity, TUI or integrated promotion."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

Candidate `9efce3f4040a70ad4052846b9afa8ebaedd37e2aa5475e629cc00003157db625`
and exact wheel
`578a9464ccbe248642650dae159b72bdb6b8b90f015c83283e7a7cde39e9251c`
passed the independent objective review with P0/P1/P2 zero. The architecture
review rejected promotion with one P1 and one P2. The P1 found that
`templates/AGENTS.md` and its validator made `$codex-thread-handoff` the normal
execution route, contradicting the controller-only boundary and the explicit
legacy-only handoff contract. The P2 found that schema tests used hand-authored
capsule/result dictionaries rather than durable regressions over the real typed
`to_json()` projections, although a targeted read-only comparison showed the
current projections already validate.

## Next execution — controller-entrypoint-and-schema-projection-closure

The first bounded four-path repair completed as candidate
`0086815cb3fcd72c73f720832cbd03a183deee2a009328540bd787268a9abaf0`:
normal execution now selects `$workflow-control`, the legacy handoff wording is
rejected, and real typed capsule/result projections validate their schemas.
Before promotion, the user added one planning-contract requirement: maximize
safe milestone-level parallelism after architecture is frozen, without adding a
nested swarm/controller layer. The same repair owner therefore continues under
this bounded replan; no second writer is created.

The expanded repair stays inside the existing workflow-asset architecture and
changes no new file, runtime production module, schema, dependency, runner,
registry, entrypoint or public class:

- `modify AGENTS.md`: require the planner to derive a dependency DAG/readiness
  map after freezing architecture; freeze shared schemas/contracts/state
  authority/entrypoints before fan-out; require a concrete reason for every
  serial edge; keep each milestone single-owner and reject nested orchestration
  as the default.
- `modify templates/AGENTS.md`: normal execution invokes `$workflow-control`
  with the canonical plan path and exact milestone id. The planner does not
  create a peer or serialize a sidecar capsule. `$codex-thread-handoff` remains
  named only as an explicit legacy compatibility or deliberate comparison
  route and is never silently selected or combined with workflow-control.
- `modify templates/AGENTS.workflow.md`: add the reusable compact form of the
  same architecture-first DAG, readiness, serial-edge and single-owner policy.
- `modify plugins/personal-workflow-skills/skills/plan-work/SKILL.md`: after the
  architecture map is fixed, require decomposition into independently closable
  vertical milestones, explicit dependency/readiness facts, shared-authority
  freeze before fan-out, and critical-path minimization without fake splits or
  nested milestone controllers.
- `modify plugins/personal-workflow-skills/skills/plan-work/agents/openai.yaml`:
  make the default planning prompt request the architecture map and dependency
  DAG/readiness projection, without adding routing or transport ownership.
- `modify scripts/validate.py`: replace the conflicting normal fresh-peer
  requirements with fail-closed requirements for the packaged workflow-control
  entrypoint, canonical plan path, exact milestone id and explicit legacy-only
  handoff distinction; validate the architecture-first DAG and single-owner
  parallelism contract in the skill and instruction templates. A template that
  restores direct handoff as the ordinary execution path or recommends a nested
  swarm/controller as the default must fail validation.
- `modify tests/fixtures/prompt-input/plan-work.json`: extend the semantic plan
  fixture with a dependency DAG/readiness example whose independent lanes have
  disjoint ownership and whose serial edge names a permitted concrete reason.
- `modify tests/test_workflow_assets.py`: retain the schema fixture tests and
  add durable assertions that real `ModelFacingCapsule.to_json()` and
  `ModelFacingResult.to_json()` objects validate their respective Draft
  2020-12 schemas, including closed unknown-field rejection at the projection
  boundary; add focused positive and negative coverage for DAG/readiness,
  serial-edge reasons, disjoint mutable ownership and the no-nested-controller
  invariant.
- `modify docs/reviews/evidence/workflow-skill-contracts.json`: recompute the
  exact owned-path candidate digest and record the focused repair gates. The
  new-file count remains exactly 22. Because packaged templates and the
  `plan-work` skill now change, rebuild and verify the exact wheel instead of
  carrying forward the previous artifact.

Mutable ownership is limited to those nine paths. All schemas, other skills,
config, `Makefile`, `pyproject.toml`, `uv.lock`, plugin metadata, runtime
production code and successor milestones are protected. The repair has one
owner because its validator, templates, skill contract, fixture and evidence are
one shared policy boundary; splitting writers would create cross-file drift.

The planning contract models program work as a DAG rather than a two-level
swarm. A shared-authority foundation node precedes dependent vertical lanes;
ready lanes with disjoint mutable surfaces may then execute in parallel. Each
lane closes independently under one owner. Read-only scouts and distinct
promotion authorities may overlap, but no milestone worker becomes a lifecycle
controller and no nested multi-agent topology is required. The planner records
why any lane remains serial using exactly the relevant dependency class instead
of defaulting all work to a sequence or maximizing milestone count.

Acceptance requires the workflow asset validator to reject the old conflicting
entrypoint template and policy variants that omit the DAG/readiness facts,
permit overlapping mutable lanes, leave serial edges unexplained, or prescribe
nested controllers as the normal route. The real typed projections must still
validate the schemas. Run `test-workflow-assets`, the direct validator, the
package/workflow-asset wheel parity checks made stale by the packaged changes,
and `git diff --check`; self-review must report P0/P1 zero. Carry forward the
unaffected contracts/controller/workers/integrations partitions and the prior
488-test broad gate; do not rerun the whole suite merely for instruction and
validator changes.

The new requirement expands the accepted candidate beyond the four-path repair,
so neither prior review carries forward. After the repaired candidate freezes,
run one Luna XHigh objective review and one Sol Medium architecture review of
that exact digest; promotion requires P0=0/P1=0 from both.

Candidate `6d837cb3a2fc64c97b6d7b15e5ab33b9a44fa2b95d97a6ebd1eaab6fb0c73c7d`
and exact wheel
`08dcdeb133eec47f175a99940e311b8f8eb5f9705e00ccd97f76428b76d181bc`
passed the focused 38-test workflow-asset gate, direct validator, exact ten-
asset wheel parity and diff hygiene. The independent Luna XHigh objective
review returned `DO_NOT_PROMOTE`, P0=0/P1=1/P2=0; the Sol Medium architecture
review returned `DO_NOT_PROMOTE`, P0=0/P1=2/P2=0. The three unique P1 findings
are one shared workflow-contract boundary:

- `capsule.schema.json` and `result.schema.json` accept alternate cognitive
  shapes that their authoritative `ModelFacingCapsule.from_json()` and
  `ModelFacingResult.from_json()` parsers reject, creating a second
  serialization contract despite the controller-only boundary;
- the plugin manifest default prompt still offers direct peer handoff as an
  ordinary workflow choice rather than an explicitly requested legacy or
  comparison route; and
- the top-level descriptive-name gate validates changed paths but does not
  validate architecture identifiers or identifier-bearing content. Milestone-
  coupled fixture ids and primary identifiers therefore pass despite the
  forward-only naming invariant.

## Next execution — workflow-contract-authority-closure

Outcome: close all three exact-candidate P1 findings with one forward-only
controller-facing schema, one normal workflow-control entrypoint and one
promotion-reachable descriptive-name gate. No compatibility alias, alternate
schema branch or second runtime/cognitive serialization model is retained.

Implementation architecture map:

- `modify schemas/capsule.schema.json`: make it the exact closed JSON Schema
  projection of `ModelFacingCapsule`. Accept exactly the keys and value domains
  parsed by `ModelFacingCapsule.from_json()`; remove the alternate
  program/milestone/workspace/routing/review-policy branch and its unused
  properties. `schema_version` is integer `1`, not a string compatibility form.
- `modify schemas/result.schema.json`: make it the exact closed projection of
  `ModelFacingResult`. Accept exactly its status, summary, changed surfaces,
  typed validations, durable status and nullable-string next action; remove the
  milestone/validation/findings branch, unused finding definition and every
  field the parser discards or rejects.
- `modify plugins/personal-workflow-skills/.codex-plugin/plugin.json`: the
  default prompt selects planning or normal `$workflow-control` execution;
  direct peer handoff is named only as an explicitly requested legacy
  compatibility or deliberate comparison route. Metadata remains semantic and
  version/source identity does not change in this repair.
- `modify scripts/validate.py`: validate the manifest entrypoint distinction;
  prove both authoritative typed projections validate their schemas and that
  representative schema-valid values round-trip through the exact `from_json`
  parsers; reject any schema-valid value outside the typed boundary. Integrate
  descriptive identifier validation into the top-level gate for architecture
  primary identifiers, milestone ids, owners and identifier-bearing Python,
  fixture, schema, evidence-key and generated-filename fields. Use explicit
  persisted-protocol/immutable-provenance exceptions only; never scan arbitrary
  prose or silently exempt a mutable evidence key.
- `modify tests/test_workflow_assets.py`: replace hand-authored alternate
  capsule/result positives with real typed projections, add bidirectional
  schema/parser closure negatives, manifest normal-route adversaries and
  top-level naming-gate bypass regressions.
- `modify tests/test_descriptive_naming.py`: add focused class, function, test,
  fixture id, schema title, evidence key and generated-filename adversaries plus
  explicit protocol/provenance positives. Reuse the existing semantic naming
  module; do not add another validator or broaden its public API unless a
  compile-time dependency proves unavoidable.
- `modify docs/reviews/evidence/workflow-skill-contracts.json`: record the
  repaired exact 34-path digest, focused gates, updated workflow-asset count and
  rebuilt exact wheel identity. This evidence record remains excluded from its
  own candidate digest.

Dependency direction is fixed:
`src/codex_flow/contracts.py` and `src/codex_flow/descriptive_naming.py`
authorities (preserved) -> exact schemas/manifest -> validator -> focused tests
-> rebuilt packaged assets/evidence. No reverse import from runtime production
code into plugin metadata is introduced. New durable artifact budget is zero;
new production modules, classes, public functions, schemas, runners,
registries, entrypoints, aliases and shims are prohibited.

Mutable ownership is limited to those seven paths under the same repair owner.
`src/codex_flow/contracts.py`, `src/codex_flow/descriptive_naming.py`, all other
schemas/skills/templates/config, plan fixture, partition manifest, marketplace
metadata, `AGENTS.md`, runtime controller/ledger/supervisor/SDK/CLI code,
`Makefile`, `pyproject.toml`, `uv.lock`, accepted evidence and H6-G onward are
protected. A material need to change a typed runtime contract, add a schema or
retain an alternate cognitive shape returns to planning before mutation.

Acceptance modes are `objective` and `architecture`. Acceptance requires:

- every value accepted by the capsule/result schemas parses through the exact
  authoritative `from_json` boundary for the exercised combinatorial domains,
  every typed `to_json` projection validates, extra/alternate/wrongly typed
  values fail both boundaries, and no `anyOf` compatibility branch remains;
- the packaged manifest cannot silently select direct peer handoff for normal
  execution, while explicit legacy/comparison reachability remains truthful;
- the repository top-level validator rejects representative milestone/task/
  thread/model/sequence coupling in changed paths, Python class/function/test/
  fixture identifiers, architecture graph ids/owners/primary identifiers,
  schema titles/ids, mutable evidence keys and generated filenames, while the
  documented persisted-protocol and immutable-provenance exceptions still
  pass;
- during implementation run only the smallest discriminating workflow-asset
  tests and direct validator. At closure run `make test-workflow-assets`, exact
  rebuilt-wheel ten-asset parity, JSON/plugin metadata validation,
  `git diff --check` and complete seven-path self-review. Carry forward the
  unaffected contracts/controller/workers/integrations partitions and prior
  488-test broad gate; do not rerun the full suite or provider sentinel; and
- self-review and one subsequent Luna XHigh objective recheck plus one Sol
  Medium architecture recheck of the same repaired digest each report
  P0=0/P1=0 before H6-G becomes ready.

This repair is the sole mutable milestone. The pending successor dependency is
an `acceptance dependency`: H6-G consumes the promoted controller-facing
schema, routing and naming contracts. Read-only reviewer contexts may run in
parallel only after the repaired digest freezes.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Close the exact workflow schema authority, plugin entrypoint and descriptive-name "
        "gate findings without adding a parallel contract, alias or runtime surface."
    ),
    decomposition=(
        "Make capsule and result schemas exact closed projections of their authoritative typed parsers.",
        "Make the plugin manifest select workflow-control normally and handoff only when explicitly requested for legacy comparison.",
        "Connect semantic identifier and content-name checks to the promotion validator with focused adversarial proof.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Schema acceptance and typed parser acceptance are bidirectionally aligned with no alternate branch.",
        "The plugin default prompt exposes one normal controller entrypoint and an explicit-only legacy handoff route.",
        "The top-level gate rejects prohibited durable identifiers and preserves only documented protocol/provenance exceptions.",
        "Focused workflow assets, direct validation, rebuilt exact-wheel parity, diff hygiene and self-review pass with P0/P1 zero.",
    ),
    mutable_surfaces=(
        "schemas/capsule.schema.json and schemas/result.schema.json",
        "plugins/personal-workflow-skills/.codex-plugin/plugin.json",
        "scripts/validate.py",
        "tests/test_workflow_assets.py and tests/test_descriptive_naming.py",
        "docs/reviews/evidence/workflow-skill-contracts.json",
    ),
    protected_surfaces=(
        "typed runtime contracts and descriptive naming implementation",
        "all other schemas, skills, templates, fixtures, configuration and metadata",
        "runtime controller, ledger, supervisor, SDK, worker, IPC, service and CLI",
        "canonical plan and AGENTS instructions",
        "H6-G and later milestones, global Codex/App state and unrelated dirty changes",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only workflow-contract-authority-closure in the existing dirty "
        "python-sdk-controller worktree. Preserve unrelated changes. Implement the fixed seven-path "
        "forward-only architecture and close the three exact-candidate P1 findings. Use no aliases, "
        "alternate schema branches, new artifacts, peers/subagents/worktrees/commits, App/global mutation "
        "or provider sentinel. Run proportional focused and exact-wheel gates, retain sanitized evidence, "
        "self-review the complete owned diff and send one terminal callback to the planning controller."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

Candidate `69f8f10b0558c400bb8ff524f21c32000df20be811a79862ec83b27dd52e68d9`
and wheel `a1b954b5ecce5a1d1988daa5a0388ab62c46197bb9dcd9247af1d7a5c181fe13`
closed the alternate top-level branches, current manifest wording and first
naming bypasses, but both exact-candidate rechecks returned `DO_NOT_PROMOTE`.
Objective found two P1 and architecture found four P1. The unique surviving
classes are: non-expressible cross-field schema/parser differences plus local
string/count mismatches; a contradictory manifest prompt that defeats substring
checks; hard-coded/incomplete Python, graph and evidence naming coverage; and a
candidate digest omitting `tests/test_descriptive_naming.py`.

This is same-class non-convergence after the Luna implementation and repair
cycle. The bounded continuation routes to one fresh Sol Medium recovery owner
and changes implementation shape without weakening ownership, routing, naming,
privacy or acceptance outcomes.

## Retained execution — typed-workflow-boundary-convergence

Outcome: replace the cross-field JSON projection with one locally enforceable,
forward-only shape so static schemas, Python parsers, generated SDK schemas and
controller projection accept exactly the same values. No deprecated key,
old-shape alias, compatibility branch or second cognitive schema survives.

The recovery owner reproduced one protected-boundary conflict before creating
controller state: the existing `validate_output_schema` vocabulary rejects the
bounded Draft constraints required by the exact canonical generated result
schema. A metadata/constraint-stripping SDK projection would recreate the
forbidden second schema authority. This bounded replan therefore adds the
existing domain validator and its focused `ExecutionCapsule` tests to the same
mutable owner; it does not change the frozen serialization shape or authorize a
general JSON Schema engine.

Frozen serialized capsule shape (shown as an indented data example so the
active plan section retains exactly one Python capsule fence):

    {
      "schema_version": 1,
      "objective": "...",
      "decomposition": ["..."],
      "acceptance_criteria": ["..."],
      "surfaces": {"src/example.py": "mutable", "src/stable.py": "protected"},
      "acceptance": {"objective": "code-reviewer", "architecture": "architecture-reviewer"},
      "prompt": "...",
      "recovery_policy": "completion_biased"
    }

Architecture decisions:

- `surfaces` is one closed object of bounded semantic surface keys with values
  `mutable` or `protected`; object-key uniqueness makes overlap
  unrepresentable. The total map is bounded to 128. The Python type may retain
  ergonomic internal mutable/protected tuples, but JSON uses only this map.
- `acceptance` is one closed object keyed by the three supported modes. Each
  property has its canonical configured role (`code-reviewer`,
  `visual-reviewer`, `architecture-reviewer`). Separate serialized
  `acceptance_modes` and `authorities` disappear, making missing coverage and
  duplicate roles unrepresentable. The internal type rejects noncanonical role
  mappings before projection.
- `prompt_budget_bytes` is removed from model-authored JSON. The plan/compiler
  still bounds measured prompt bytes, while runtime/transport budgets remain
  controller-owned. The schema applies one fixed prompt-length bound and the
  raw decoder retains its byte ceiling, so no schema-valid value is rejected
  because of a model-selected cross-field budget.
- Every serialized text and text array uses one identical non-empty,
  non-whitespace, NUL-free and maximum-length predicate in Python and schema.
  Arrays use the same uniqueness and 128-item maximum. Result validations use
  that same maximum. Regexes cannot use the permissive `$` anchor.
- Static `schemas/{capsule,result}.schema.json` and generated
  `model_facing_{capsule,result}_schema()` are structurally equal after JSON
  decoding. CLI/SDK output schema, packaged assets and parsers therefore expose
  one contract without a new generated file.
- `src/codex_flow/domain.py::validate_output_schema` remains the single
  execution-boundary validator and accepts the exact bounded Draft 2020-12
  vocabulary used by those canonical schemas. The supported vocabulary is
  limited to schema metadata (`$schema`, `$id`, `title`, and `description` when
  emitted by the canonical generator), `type`, `properties`, `required`,
  `additionalProperties`, `propertyNames`, `items`, `const`, `enum`,
  `minLength`, `maxLength`, `pattern`, `minItems`, `maxItems`, `uniqueItems`,
  `minProperties`, and `maxProperties`. It is not a general permissive schema
  engine: unknown keywords and composition/reference keywords fail closed.
- The validator recursively checks each keyword's JSON type, bounds,
  applicability and child schema. Explicit record objects still require every
  declared property and set `additionalProperties` to false. Bounded semantic
  maps may instead combine a validated `propertyNames` schema with a validated
  schema-valued `additionalProperties` and explicit property-count bounds;
  `additionalProperties: true` and otherwise open object shapes remain
  invalid. Arrays require an item schema and enforce their declared bounded
  count/uniqueness constraints. Scalar `const`, `enum`, string bounds/patterns
  and nullable unions must remain compatible with the declared type.
- `ExecutionCapsule` accepts the exact generated result schema through that
  validator before any controller state is created. There is no metadata- or
  constraint-stripping transport projection: the same schema bytes/structure
  reach the SDK/controller output-schema boundary.

Implementation architecture map:

- `modify src/codex_flow/contracts.py`: exact forward-only JSON projection,
  canonical acceptance mapping, aligned string/item bounds and validation count.
- `modify src/codex_flow/projection.py`: accept only the new keys, preserve
  controller-owned budgets and consume canonical typed authorities without a
  second role mapping contract.
- `modify src/codex_flow/domain.py`: extend the one closed recursive
  `validate_output_schema` authority only with the bounded canonical Draft
  vocabulary above; preserve depth/property/byte limits and fail-closed
  `ExecutionCapsule` construction before state mutation.
- `modify schemas/capsule.schema.json` and `schemas/result.schema.json`: encode
  exactly the shape and bounds above with stable semantic schema ids.
- `modify plugins/personal-workflow-skills/.codex-plugin/plugin.json`: retain
  correct route intent using one validator-owned canonical default prompt.
- `modify scripts/validate.py`: compare manifest prompt by exact canonical
  equality; compare static/generated schemas structurally; exercise a bounded
  per-field adversarial round-trip matrix; AST-scan every changed repository
  Python file; validate every graph identity projection (`id`, `owner`,
  dependencies, `current_readiness` keys, serial-edge endpoints, parallel
  groups, `critical_path`, `fan_out_after`, primary identifiers and path
  owners); scan every changed evidence JSON unless it is in an explicit
  immutable accepted-evidence allowlist; and validate mutable evidence paths,
  keys and generated filenames without arbitrary prose scanning or a second
  naming implementation.
- `modify tests/test_model_facing_projection.py` and
  `tests/test_workflow_control.py`: migrate callers to the forward-only JSON
  shape and prove low-level controller/CLI rejection is mutation-free.
- `modify tests/test_controller_execution.py`: prove `ExecutionCapsule`
  accepts the exact generated canonical result schema, rejects malformed
  keyword types, unsupported keywords and open objects before state creation,
  and preserves recursive metadata, property-name, enum/const, string,
  object-bound and array-bound constraints without a reduced projection.
- `modify tests/test_workflow_assets.py` and
  `tests/test_descriptive_naming.py`: cover exact bidirectional constraints,
  contradictory manifest text, every graph identity position, arbitrary
  changed Python, mutable evidence/provenance and generated names.
- `modify docs/reviews/evidence/workflow-skill-contracts.json`: update exact
  candidate scope, gates and wheel identity; remain self-excluded.

`RECONCILIATION_CANDIDATE_PATHS` expands from 34 to exactly 41 paths by adding
`src/codex_flow/contracts.py`, `src/codex_flow/projection.py`,
`tests/test_model_facing_projection.py`, `tests/test_workflow_control.py` and
`tests/test_descriptive_naming.py`, plus `src/codex_flow/domain.py` and
`tests/test_controller_execution.py`. Every mutable implementation/test path
is therefore frozen by the same sorted path-NUL-bytes digest.

Mutable ownership is limited to those thirteen paths under the existing Sol
Medium recovery owner. All other runtime modules, schemas,
skills/templates/config, partition manifest, plan fixture, marketplace
metadata, canonical plan/`AGENTS.md`, accepted evidence, H6-G onward, global
Codex/App state and unrelated changes are protected. New durable artifact
budget is zero. `ExecutionCapsule` construction may change only through the
bounded validator extension above; changing its persisted/public shape,
ledger/controller persistence, public CLI commands or schema ids, or adding an
alias/module/class/runner/registry/entrypoint requires bounded replanning.

Acceptance modes are `objective` and `architecture`. Acceptance requires:

- a bounded adversarial matrix proves both directions for every capsule/result
  field: every schema-valid value parses/re-projects identically, typed
  projections validate, and wrong types/bounds, NUL/whitespace, extra/old keys,
  noncanonical acceptance mappings and invalid statuses fail;
- overlap, duplicate authority roles and prompt-budget disagreement are
  unrepresentable in JSON while controller ownership and byte budgets remain;
- exact manifest equality prevents contradictory direct-handoff defaults while
  the dedicated legacy/comparison route remains truthful;
- the top-level validator rejects coupled names in any changed Python/test,
  every graph identity position, every new/mutable evidence path/key/generated
  filename and schema metadata, with only explicit immutable/protocol exceptions;
- canonical generated capsule/result schemas pass the bounded output-schema
  validator unchanged, including recursive metadata, `propertyNames`,
  enum/const, bounds, patterns and array constraints; malformed keyword types,
  unsupported keywords and open objects fail before any controller state;
- changing any implementation or regression proof changes the 41-path digest;
- implementation uses focused tests, then closure runs affected `contracts`,
  `controller` and `workflow-assets` partitions and one full `make check`
  because shared contracts/schemas/CLI schema/packaging change. Rebuild/install
  the exact wheel in fresh Python 3.12, prove generated/static schema and ten-
  asset parity, validate plugin/JSON, run `git diff --check`, and self-review the
  thirteen-path diff with P0/P1 zero. The provider sentinel is not rerun; and
- subsequent Luna XHigh objective and Sol Medium architecture rechecks of the
  same digest both return P0=0/P1=0 before H6-G becomes ready.

The dependency to H6-G is an `acceptance dependency`. This is the sole mutable
milestone; the two later reviews may run in parallel read-only after freeze.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Converge the typed, schema, routing and naming boundary with one locally "
        "enforceable forward-only projection and complete candidate identity."
    ),
    decomposition=(
        "Replace cross-field capsule JSON with canonical acceptance and surface maps while retaining controller-owned budgets.",
        "Make static schemas, generated SDK schemas and typed parsers bidirectionally exact.",
        "Extend the single execution output-schema validator with only the bounded canonical Draft vocabulary and prove unchanged SDK reachability.",
        "Make manifest and naming gates canonical and complete the candidate digest.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "The adversarial matrix proves schema/parser equality and the old JSON shape is unreachable without aliases.",
        "ExecutionCapsule accepts the exact generated result schema while unsupported or open schemas fail before state creation.",
        "One canonical manifest prompt and one naming authority close every reproduced bypass.",
        "The 41-path digest includes every implementation and test surface except self-excluded evidence.",
        "Affected partitions, full make check, exact-wheel gates and self-review pass with P0/P1 zero.",
    ),
    mutable_surfaces=(
        "src/codex_flow/contracts.py, src/codex_flow/projection.py and src/codex_flow/domain.py",
        "schemas/capsule.schema.json and schemas/result.schema.json",
        "plugins/personal-workflow-skills/.codex-plugin/plugin.json and scripts/validate.py",
        "tests/test_model_facing_projection.py, tests/test_workflow_control.py, tests/test_controller_execution.py, tests/test_workflow_assets.py and tests/test_descriptive_naming.py",
        "docs/reviews/evidence/workflow-skill-contracts.json",
    ),
    protected_surfaces=(
        "all other runtime, schema, skill, template, config, fixture and metadata paths",
        "ExecutionCapsule persisted/public shape, ledger/controller persistence and public CLI commands",
        "canonical plan and AGENTS instructions",
        "accepted evidence, H6-G onward, global Codex/App state and unrelated changes",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only typed-workflow-boundary-convergence in the existing dirty "
        "python-sdk-controller worktree as the existing Sol Medium recovery owner. Implement the fixed "
        "thirteen-path forward-only redesign including the bounded canonical output-schema validator, "
        "preserve unrelated changes, add no aliases or artifacts, "
        "run proportional then required shared-contract gates, retain sanitized evidence and send one callback."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

Candidate `63af585f760b29b0a53f079101a61d11d5282b136d096e9ac9533c7f8b0fb267`
and exact wheel
`cd8d747638f7c8f849622596619990f09063a94c5bc70f9829a5f394b1620512`
passed 46 contract tests, 347 controller tests, 56 workflow-asset tests and one
525-test `make check`. The independent Luna XHigh objective review nevertheless
returned `DO_NOT_PROMOTE`, P0=0/P1=4/P2=0. It proved that bounded property
counts could make named record properties optional; the SDK decoded only a
reduced subset of the accepted schema vocabulary and crashed on semantic maps;
the top-level naming gate scanned a fixed candidate list and blanket-exempted
mutable evidence; and the active plan section contained a non-Python fence that
made normal canonical-plan compilation unreachable. The non-Python fence is a
controller-owned plan defect and is already removed. This candidate remains
retained evidence and is not promotable.

## Retained execution — structured-output-runtime-and-naming-closure

Outcome: one bounded schema authority validates both the accepted schema and
the decoded JSON instance at the production SDK boundary, every changed durable
identifier is promotion-gate reachable, and the exact canonical plan capsule is
compilable through normal `workflow-control`. No reduced decoder, alternate
schema, compatibility branch or milestone-coupled artifact survives.

Current promotion state: previous candidate
`ecd7bcd4a75dcc4694c14c371d28ba0c87b73b28b545f1b0c25ea4ec116a1119`
was `DO_NOT_PROMOTE` because of nullable-scalar constraint bypass,
serialized-text equality and an unavailable exact wheel. Those findings are
repaired in candidate
`278cea0530ca43acba00e3cc7ba1b232f2f749f89706e3e632e45d7ed0d0933e`,
whose objective recheck then found one large-integer `math.isfinite()` P1. The
one Sol High escalation has closed that finding in candidate
`fd9331c855d8352febc84ad56df142aec1bc7864f07fcd07d0b582321860996d`.
Its exact nonsymlinked 259769-byte wheel is retained at
`dist/structured-output-runtime/codex_flow-0.2.0-py3-none-any.whl` with SHA-256
`fea94483f8c0a834ba139612603a223868a536a1837bdb03a02e20319780815a`.
Promotion state: `PROMOTED` for exactly candidate
`fd9331c855d8352febc84ad56df142aec1bc7864f07fcd07d0b582321860996d`
and retained wheel
`fea94483f8c0a834ba139612603a223868a536a1837bdb03a02e20319780815a`.
The final independent Luna XHigh objective and Sol Medium architecture reviews
both returned `APPROVED` with P0=0/P1=0/P2=0. This closes the structured-output,
runtime-schema, naming and workflow-contract authority prerequisite and unlocks
only `outcome-evidence-priority-contract`; H6-G remains behind that dependency.

Architecture map and dependency direction:

- `modify src/codex_flow/domain.py`: `validate_output_schema` remains the only
  bounded Draft schema-vocabulary authority and named records always require
  exactly every declared property, irrespective of property-count bounds. Add
  one private `_json_schema_instance_equal(left, right)` predicate shared by
  schema enum-uniqueness and instance `const`/`enum`/`uniqueItems` checks. It
  follows Draft 2020-12 instance equality recursively: booleans remain distinct
  from numbers, mathematically equal finite integers/floats such as `1` and
  `1.0` compare equal, arrays compare positionally and objects compare by the
  same string keys and recursively equal values independent of member order;
  serialized-text fingerprints are forbidden. Keep the semantic
  `validate_structured_output(value, schema)` function as the single instance
  authority: it first validates the schema and then recursively enforces the
  same `type`, `const`, `enum`, string pattern/bounds, object bounds,
  explicit-record,
  `propertyNames`, schema-valued `additionalProperties`, array bounds and
  `uniqueItems` rules against decoded JSON. For scalar unions it selects the
  matching declared type and continues through that type's applicable
  constraints; `null` short-circuits only when the actual value is null, so a
  nullable string still enforces `minLength`, `maxLength` and `pattern` for its
  string instances. Preserve existing depth, property, item and byte ceilings.
  This module imports no SDK, `jsonschema` runtime dependency or model-facing
  contract.
- `modify src/codex_flow/backends/codex_sdk.py`: remove the private reduced
  `_type_matches`/`_validate_decoded_schema` interpretation and call
  `validate_structured_output` after strict bounded JSON decoding. The adapter
  remains the raw SDK conversion boundary and returns the unchanged decoded
  object; it does not define schema semantics or a second result model.
- `modify src/codex_flow/descriptive_naming.py`: remove the directory-wide
  evidence exemption. `validate_path_name` and `validate_changed_names` accept
  only caller-supplied exact immutable/protocol exceptions; ordinary mutable
  evidence paths use the same semantic naming rules as every other durable
  artifact.
- `modify src/codex_flow/contracts.py`: delete the noncanonical public
  `ModelCapsule` and `ModelResult` assignments and exports. The only public
  model-facing serialization types are `ModelFacingCapsule` and
  `ModelFacingResult`; add no alias, shim or compatibility branch.
- `modify scripts/validate.py`: keep the exact immutable accepted-evidence
  allowlist at the top-level repository policy boundary, pass it explicitly to
  the naming authority and feed the complete `changed_paths_from_git()` result
  to structured-content validation. `RECONCILIATION_CANDIDATE_PATHS` identifies
  candidate bytes only; it is never a scan allowlist.
- `modify tests/test_controller_execution.py`: reject named optional records
  before any controller state, retain valid bounded records/maps, reject
  numeric-equivalent duplicate enum members and prove boolean/number values
  remain distinct under the domain equality predicate.
- `modify tests/test_codex_sdk_adapter.py`: exercise every supported canonical
  keyword against decoded values, including invalid result const/enum/text and
  duplicate arrays, plus positive and negative bounded semantic maps. Add a
  focused table checked against the development-only `Draft202012Validator`
  covering nullable string bounds/pattern, numeric `const`, numeric enum
  membership, `uniqueItems`, boolean-versus-number and nested array/object
  equality. Prove the removed private reduced decoder has no remaining call
  path; production code does not import the oracle library.
- `modify tests/test_descriptive_naming.py`: drive the top-level gate with
  arbitrary changed Python and mutable evidence paths; prove only exact
  immutable/protocol exceptions survive and arbitrary prose is still ignored.
- `modify tests/test_plan_compilation.py`: compile this exact active milestone
  from the real canonical plan and reject an injected non-Python fence.
- `modify tests/test_model_facing_projection.py`: prove the removed aliases are
  absent from module attributes and `__all__`, cannot be imported and are not
  present in the exact installed wheel, while canonical model-facing imports
  remain usable.
- `modify docs/reviews/evidence/workflow-skill-contracts.json`: retain sanitized
  exact candidate, focused/broad gates and wheel facts; remain self-excluded.
  Record one repository-local ignored retention path
  `dist/structured-output-runtime/codex_flow-0.2.0-py3-none-any.whl`, its exact
  SHA-256 and byte size. The retained artifact must remain present and
  byte-identical through both independent reviews; a temporary build/install
  path or another wheel with the same filename is not acceptable evidence.

Preserve `projection.py`, both canonical static schemas, `plan_capsule.py`,
controller/ledger/supervisor/worker/IPC/service/CLI,
plugin/skill/template/config surfaces, accepted evidence, H6-G onward and all
unrelated dirty changes. Dependency direction is
`domain schema/value authority -> SDK adapter -> typed result consumer`; the
naming authority flows into the repository validator, never back into runtime.
No production module, public canonical class, schema, registry, runner or
entrypoint is created or removed. The two noncanonical aliases are removed
forward-only. New tracked durable artifact budget is zero. The one ignored
wheel retained below `dist/structured-output-runtime/` is review evidence, not
a new production path or serialization authority. Existing stale wheels are
protected: do not delete, overwrite, install, inspect as a substitute or report
them as candidate artifacts.

The candidate digest expands from 41 to exactly 45 sorted path-NUL-bytes paths
by adding `src/codex_flow/backends/codex_sdk.py`,
`src/codex_flow/descriptive_naming.py`, `tests/test_codex_sdk_adapter.py` and
`tests/test_plan_compilation.py`. `src/codex_flow/contracts.py` and
`tests/test_model_facing_projection.py` were already members of the original
41-path boundary, so the digest remains exactly 45 paths. Every mutable
implementation/test path is therefore frozen; the evidence record remains the
only self-exclusion.

Current Sol High escalation ownership overrides the historical eleven-path
implementation map for this final repair. The first focused implementation
proved that the protected plan-compilation test still asserted the historical
eleven-path ownership, so the controller authorizes that one necessary contract
update. Mutable ownership is exactly four paths:

- `modify src/codex_flow/domain.py`: add the private semantic predicate
  `_is_finite_json_number(value)`. It rejects booleans and non-numbers, accepts
  every Python `int` without converting it to float, and calls
  `math.isfinite()` only for `float`. Reuse it in
  `_json_schema_instance_equal`, schema `value_matches_type` and instance
  `type_matches`; no direct `math.isfinite()` call remains on an `int | float`
  union. Do not invent a numeric magnitude limit: the existing strict JSON,
  schema depth/cardinality and one-megabyte structured-output bounds remain the
  resource authority.
- `modify tests/test_codex_sdk_adapter.py`: add a 400-digit integer under a
  bounded `type: number` record and prove `Draft202012Validator`,
  `validate_structured_output` and `_decode_schema_output` all accept the exact
  value without coercion. Add the same magnitude to `const`/`enum` equality and
  retain boolean/number separation; no provider call is permitted.
- `modify tests/test_plan_compilation.py`: update only
  `test_active_structured_output_closure_capsule_compiles_from_canonical_plan`
  so it asserts the authoritative four-path escalation capsule and its exact
  ordered surfaces instead of the obsolete length `11`. Preserve every other
  plan-compiler adversary and do not weaken the one-active-capsule contract.
- `modify docs/reviews/evidence/workflow-skill-contracts.json`: update the exact
  candidate, proportional gates and retained-wheel path/hash/size; remain the
  sole self-exclusion.

The other seven formerly mutable implementation/test paths are now protected
frozen candidate bytes. The retained wheel path is milestone-owned and may be
atomically replaced only by the fresh wheel for the repaired candidate after
its bytes pass verification; the protected stale root wheel/sdist remain
untouched. New tracked artifact budget remains zero and the candidate identity
still frames the same 45 paths.

Acceptance modes are `objective` and `architecture`. Acceptance requires:

- named record `required` keys equal declared `properties` exactly even when
  object bounds are present; bounded semantic maps remain independently valid;
- every schema accepted by `validate_output_schema` has all supported
  constraints enforced against decoded SDK JSON by the same domain authority,
  and invalid values fail terminally before typed result ingestion or durable
  result mutation;
- nullable scalar unions enforce every applicable constraint on the selected
  non-null type, and Draft instance equality—not JSON text identity—governs
  schema enum uniqueness plus instance `const`, `enum` and `uniqueItems`;
- arbitrary JSON integers accepted within the existing byte bound satisfy
  `integer` and `number` without float conversion or `OverflowError`, retain
  their exact value through the SDK boundary and participate in Draft equality;
- canonical capsule/result schemas and normal SDK structured output pass
  unchanged, without stripping metadata/constraints or introducing another
  schema/value decoder;
- `ModelCapsule` and `ModelResult` are absent from module attributes, `__all__`,
  imports and the exact installed wheel; no deprecated alias or compatibility
  shim replaces them;
- arbitrary changed Python identifiers, mutable evidence paths/keys/generated
  filenames and all graph/schema identities fail the naming gate when coupled;
  only exact documented immutable/protocol exceptions remain;
- `compile_canonical_plan(...,
  "structured-output-runtime-and-naming-closure")` succeeds on this plan and
  the section contains exactly one Python capsule fence;
- one fresh Python 3.12 wheel is built directly into and retained at
  `dist/structured-output-runtime/codex_flow-0.2.0-py3-none-any.whl`; evidence
  records its exact path/hash/size, an install uses that exact byte artifact,
  canonical imports/assets/schemas match, removed aliases remain absent, and
  both independent reviewers can hash and inspect the same retained file;
- implementation uses only focused discriminating tests while repairing. At
  closure run the focused large-integer/Draft discriminator, the contracts
  partition and the controller output-schema selection only. Carry the prior
  538-test broad gate and unaffected partitions forward because this repair
  changes one private number predicate and one focused test; rerun a broad gate
  only if the actual diff escapes those four owned paths or a focused failure
  proves wider impact. Build/install the retained exact fresh Python 3.12 wheel,
  verify the packaged schemas/assets and SDK import path, run `git diff --check`, and
  self-review the four current mutable paths plus the complete eleven-path
  milestone boundary with P0/P1 zero. Do not rerun the
  consumed provider sentinel; and
- one subsequent Luna XHigh objective and one independent Sol Medium
  architecture recheck of the same 45-path digest both return P0=0/P1=0 before
  H6-G becomes ready.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Close the structured-output runtime and durable-naming boundary so one bounded "
        "authority governs schema acceptance, decoded values and exact promotion reachability."
    ),
    decomposition=(
        "Use one domain schema-instance authority and remove the SDK's reduced decoder.",
        "Require exact named records while preserving bounded semantic-map behavior.",
        "Apply nullable scalar constraints to the selected type and use exact Draft JSON instance equality.",
        "Accept bounded arbitrary-size JSON integers without float conversion or numeric coercion.",
        "Make descriptive-name enforcement follow every real changed durable surface with explicit exceptions only.",
        "Remove the noncanonical model-facing aliases without a compatibility shim.",
        "Prove the exact canonical plan compiles through the normal controller entrypoint.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Every accepted canonical schema constraint is enforced on decoded SDK JSON before typed or durable ingestion.",
        "Named records cannot become optional through bounds, and semantic maps no longer crash or bypass validation.",
        "Nullable strings and numeric-equivalent JSON instances match Draft 2020-12 behavior at schema and SDK boundaries.",
        "A 400-digit integer remains an exact valid number through schema validation, SDK decoding, const and enum checks.",
        "Every real changed Python and mutable evidence identifier reaches the naming gate with exact exceptions only.",
        "Only ModelFacingCapsule and ModelFacingResult remain as public model-facing serialization types.",
        "The exact retained wheel remains available at its semantic evidence path for both independent reviews.",
        "The active capsule compiles, the 45-path digest is exact, required gates pass and self-review has P0/P1 zero.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_plan_compilation.py",
        "docs/reviews/evidence/workflow-skill-contracts.json",
    ),
    protected_surfaces=(
        "the other seven frozen milestone implementation and test paths",
        "SDK adapter, contracts, projection, schemas, naming authority, validator and plan compiler",
        "ledger, controller, supervisor, worker, IPC, service, CLI and public entrypoints",
        "plugins, skills, templates, configuration and accepted evidence outside the owned record",
        "canonical plan outside this controller-owned repair section, H6-G onward and unrelated dirty changes",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only structured-output-runtime-and-naming-closure in the existing dirty "
        "python-sdk-controller worktree. This is the one Sol High non-convergence escalation. Modify exactly domain.py, "
        "test_codex_sdk_adapter.py, the one stale plan-compilation assertion and the reconciliation evidence. Add "
        "_is_finite_json_number so ints never pass through "
        "math.isfinite, floats remain finite-only and Draft equality/type checks preserve a 400-digit integer exactly. Add "
        "the frozen provider-free oracle regression. Run focused discriminators, the contracts partition and controller "
        "output-schema selection; carry broad/unaffected gates unless the actual diff proves wider impact. Atomically replace "
        "only the retained semantic wheel with the verified repaired wheel and leave stale root artifacts untouched. Preserve "
        "every other dirty byte, add no artifact or alias, do not rerun the provider sentinel or start the follow-up/H6-G. "
        "Retain sanitized evidence, self-review the four-path delta and complete milestone boundary with P0/P1 zero, then "
        "send one terminal callback to the planning controller."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

## Retained execution — outcome-evidence-priority-contract

Outcome: planning, execution and review skills preserve the accepted behavior,
safety/integrity guarantees and observable proof when optimizing secondary
metrics. A line, file, duration, token, coverage, complexity, score or inventory
target remains subordinate to the outcome it is intended to support.

This follow-up is an `acceptance dependency` after
`structured-output-runtime-and-naming-closure`: the exact candidate digest under
review includes the skill/template paths, so changing them earlier would
invalidate its promotion evidence. It precedes H6-G so later executors consume
the corrected decision rule. It is a separate requirement added after the
first green workflow-contract gate and is not folded into the runtime repair.
Its `Next queued execution` heading is intentionally non-compilable by the
single-active-capsule compiler. After the outstanding objective approval, the
controller changes the promoted closure heading to `Retained execution` and
this heading to `Next execution` before handoff; there is never more than one
compiler-active capsule.

Frozen decision hierarchy:

1. accepted observable outcome and user intent;
2. safety, integrity, lineage, isolation, recovery and production-proof
   guarantees required for that outcome;
3. explicit external hard constraints such as a legal, protocol,
   compatibility or deployment ceiling; and
4. optimization targets and proxy metrics such as line/file counts, duration,
   tokens, coverage percentages, scores or inventory counts.

A lower-priority item never authorizes weakening a higher-priority item. A
numeric target is an optimization target unless the user or accepted plan
explicitly marks it as a hard external constraint and records the resolution
policy for conflict. Even a hard constraint does not authorize silent deletion
of required validation or evidence: when the two cannot coexist, stop that
implementation approach and return a bounded replan or genuine decision
request. Do not report success merely because the proxy target is exact.

Implementation architecture map:

- `modify plugins/personal-workflow-skills/skills/plan-work/SKILL.md`: require
  every plan to distinguish primary outcome/guarantees, hard external
  constraints and secondary optimization metrics; record which verification
  gates are non-negotiable and the conflict policy for every hard cap.
- `modify plugins/personal-workflow-skills/skills/execute-milestone/SKILL.md`:
  forbid deleting or weakening required production, persisted-artifact,
  lineage, isolation, recovery or independent-proof checks to meet a proxy;
  require proof-preserving replacement before removing a check and replan when
  a hard target conflicts with acceptance.
- `modify plugins/personal-workflow-skills/skills/review-work/SKILL.md`: treat
  proxy-target success accompanied by lost semantic proof as a
  promotion-blocking outcome regression, and challenge disproportionate proxy
  constraints without weakening the primary guarantee.
- `modify templates/AGENTS.md`: publish the same compact outcome/evidence
  priority invariant for repositories consuming the workflow template.
- `modify tests/test_workflow_assets.py`: verify the three packaged skills and
  template expose one consistent priority contract. Add the descriptive tests
  `test_outcome_evidence_priority_contract_is_consistent` and
  `test_hard_line_cap_requires_proof_preservation_or_replan`; do not create a
  wording-only second policy implementation or pretend these non-wheel plugin
  assets are part of the Python wheel.
- `modify scripts/validate.py`: make the reconciliation record an explicitly
  historical promoted-evidence contract. A `promoted` record validates its
  closed shape, digest syntax, inventory structure and repository-relative
  required paths, recorded review facts, and the retained wheel's current
  path/type/hash/size/asset proof, but never compares its historical candidate
  or inventory digests to successor source bytes. Reject unsupported statuses,
  open keys, malformed digests, unsafe/missing inventory paths and stale or
  missing wheel evidence. Do not create a second evidence schema, compatibility
  alias, mutable digest snapshot or general migration framework.
- `modify docs/reviews/evidence/workflow-skill-contracts.json`: change only the
  lifecycle status from `passed` to `promoted`. Preserve the promoted candidate
  digest, digest scope, inventory hashes, partition counts, review result and
  retained-wheel facts exactly; this record must not be rewritten to claim the
  successor skill bytes were part of the already reviewed runtime candidate.

All production runtime, schemas, controller state, runners, registries,
entrypoints, other skills, fixtures, other evidence and retained artifacts are
protected. New artifact budget is zero. The initial five-path implementation is
retained, but its validator failure exposed an architecture-significant
evidence-lifecycle defect: promoted historical bytes were incorrectly required
to equal all future source bytes. The bounded repair therefore has one fresh
Sol Medium owner and exactly seven mutable paths, with no second evidence
authority. Use focused workflow-asset and promoted-evidence adversaries during
the repair, then the affected workflow-assets partition, direct validator and
`git diff --check`. These seven paths do not change Python runtime, packaging
metadata or declared wheel assets, so carry the exact-wheel and broad repository
gates forward without rebuilding/rerunning them unless the actual diff proves
that assumption false. One independent objective forward-test receives a
realistic hard line-cap scenario with mandatory persisted-artifact,
role-binding and typed-case checks and must preserve those guarantees or return
a truthful replan instead of optimizing the cap. One independent architecture
review must also confirm that promoted evidence remains historical, current
source validation remains separate, and no mutable parallel digest authority
was introduced. Promotion requires both authorities to return P0=0/P1=0.

First implementation state: `DO_NOT_PROMOTE`. The frozen candidate SHA-256
`07f291c0fbea2cad995f7bac9233f25784942ebc97083807e388fcae9cbd9e49`
over exactly the seven sorted repository-relative mutable paths above, framed
as path UTF-8 + NUL + raw bytes. The historical reconciliation record is part
of this seven-path candidate; its embedded
`fd9331c855d8352febc84ad56df142aec1bc7864f07fcd07d0b582321860996d`
digest remains the distinct promoted runtime candidate and must not be updated
to the successor digest. Focused implementation evidence is 17 adversaries,
77 workflow-assets tests, direct validator, focused Ruff and diff hygiene all
passing. Its fresh Luna XHigh objective review returned P0=0/P1=2: the
9,120-versus-8,900 test computed a local arithmetic answer without exercising
an enforceable workflow decision boundary, and promoted evidence accepted a
truncated or valid-digest-substituted inventory because it had no immutable
inventory commitment. The architecture review was deliberately not dispatched;
it cannot override objective blockers. H6-G remains locked.

One bounded repair retains the same seven-path ownership and changes only the
smallest necessary validator/test bytes unless the proof requires another owned
asset adjustment:

- `scripts/validate.py` owns one private structured outcome/evidence decision
  boundary used by the top-level repository gate. It consumes a typed hard-cap
  case with ordered named proof obligations and returns only proof-preserving
  continuation, bounded replan, or promotion-blocking loss. Success is
  impossible when any required obligation is absent, even when the numeric cap
  is met; a 9,120-line case under a hard 8,900-line cap without a verified
  proof-preserving replacement returns bounded replan. The validator reads the
  canonical priority block from planning, execution, review and repository
  assets and requires the same ordered policy, so the executable decision is a
  promotion gate for those instructions rather than a disconnected test-only
  arithmetic branch or second runtime model.
- The same validator freezes the promoted 49-entry inventory with canonical
  JSON commitment SHA-256
  `18ab3d341f5920691d42c2f6d6a2ae6571cd09aed9017599a918774d439594c3`.
  It verifies both exact cardinality and commitment after closed-shape, digest
  and safe-path checks. This detects deletion, reordering or valid-digest
  substitution without comparing any historical source/destination hash to
  successor file bytes. The evidence record itself stays unchanged.
- `tests/test_workflow_assets.py` drives the actual validator decision boundary
  with the named persisted-DOCX reopening/recomputation, rendered-page
  artifact-role binding and typed/validated `HarnessCase`
  corpus/prompt/region obligations. It proves 9,120/8,900 yields bounded replan,
  an explicit proof-preserving alternative may continue, and a missing-proof
  negative can never return success. Separate inventory adversaries delete an
  entry and substitute a syntactically valid digest; both must fail the exact
  immutable commitment while legitimate successor source edits still pass.

After repair, freeze a new seven-path digest and rerun one fresh Luna XHigh
objective review. Only an objective approval permits the independent Sol Medium
architecture review of those identical bytes. Any byte change invalidates the
review identity.

Bounded repair state: `COMPLETED`, pending fresh objective review. The new exact
seven-path path-UTF-8 + NUL + raw-bytes candidate is SHA-256
`b0c431c383bd8a3978957adf21c0e3f5bbeef3109f6f83a8077b921eb7cfd5a9`.
Only `scripts/validate.py` and `tests/test_workflow_assets.py` changed relative
to the rejected candidate; the other five owned files, historical evidence and
retained wheel remain byte-identical. Focused repair evidence is 19 adversaries,
79 workflow-assets tests, direct validator, focused Ruff/format and diff hygiene
all passing with self-review P0=0/P1=0. This evidence authorizes review, not
promotion. The fresh objective authority must independently discriminate both
former P1s on these exact bytes before architecture review may begin.

Objective promotion state: `APPROVED` for exactly candidate
`b0c431c383bd8a3978957adf21c0e3f5bbeef3109f6f83a8077b921eb7cfd5a9`.
The fresh Luna XHigh review returned P0=0/P1=0/P2=0 and independently closed
both the structured hard-cap decision and immutable 49-entry inventory
commitment findings. The only remaining promotion gate is one independent Sol
Medium architecture review of these identical seven-path bytes. This objective
approval alone does not promote the milestone or unlock H6-G.

Architecture promotion state: `DO_NOT_PROMOTE` for candidate
`b0c431c383bd8a3978957adf21c0e3f5bbeef3109f6f83a8077b921eb7cfd5a9`.
The independent Sol Medium review returned P0=0/P1=2: the validator anchored
the 49-entry inventory but accepted substitution of the enclosing promoted
`candidate_sha256`, and proof-preserving continuation accepted a replacement
line count without replacement-bound proof obligations. This invalidates the
objective approval for any repaired bytes. H6-G remains locked.

This is the second unsuccessful Sol Medium repair cycle on the same evidence
immutability/proof-preservation boundary, so one fresh Sol High recovery owner
now performs the final bounded strategy correction on the same seven owned
paths:

- Add one exact immutable promoted candidate constant
  `fd9331c855d8352febc84ad56df142aec1bc7864f07fcd07d0b582321860996d`
  and one exact SHA-256 commitment
  `03c422680ae42c37b1e5367a1d3d53a41f2d5dd535d2b39d5c4a9ee811b1cb9c`
  for the frozen 576-byte UTF-8 `candidate_digest_scope`. Validation compares
  both fields to those anchors after syntax/shape checks. Syntactically valid
  substitution of either field fails, while historical inventory hashes remain
  independent of successor source bytes. The evidence file stays unchanged.
- Split the typed hard-cap case into current-artifact proof obligations and
  optional replacement-bound proof obligations. A replacement line count alone
  never authorizes continuation. `proof_preserving_continuation` requires an
  at-or-below-cap replacement plus its own exact ordered three obligations all
  verified; missing, reordered or false replacement proofs return
  `promotion_blocking_loss`. An over-cap current artifact with no replacement
  still returns `bounded_replan` when its current proofs are intact.
- Add direct top-level adversaries for valid promoted-candidate and frozen-scope
  substitution, plus a replacement count carrying only current-artifact proofs
  and replacement proofs that are missing, reordered or false. Preserve the
  already green no-replacement, missing-current-proof and fully verified
  replacement cases.

No new public type, schema, artifact, evidence version, compatibility path or
runtime policy is permitted. After this escalation, freeze a new seven-path
digest and require fresh Luna XHigh objective plus Sol Medium architecture
reviews. If Sol High cannot close these exact facts, return a truthful terminal
failure rather than weakening or repeating the approach.

Sol High repair state: `COMPLETED`, pending fresh promotion reviews. The new
exact seven-path path-UTF-8 + NUL + raw-bytes candidate is SHA-256
`eb9a28add5c2d0f9a961c54042c1891fc06cf2b57a94f095a8c0fb4d85553e88`.
Only `scripts/validate.py` and `tests/test_workflow_assets.py` changed relative
to the rejected architecture candidate; the other five owned paths, promoted
historical evidence and retained wheel remain byte-identical. Focused evidence
is 9 new adversaries, 87 workflow-assets tests, direct validator, focused
Ruff/format and diff hygiene all passing with self-review P0=0/P1=0. Freeze
these bytes for one fresh Luna XHigh objective review followed, only on
approval, by one independent Sol Medium architecture review. Any byte change
invalidates both authorities; H6-G remains locked meanwhile.

Final objective promotion state: `APPROVED` for exactly candidate
`eb9a28add5c2d0f9a961c54042c1891fc06cf2b57a94f095a8c0fb4d85553e88`.
The fresh Luna XHigh authority returned P0=0/P1=0/P2=0 and independently
reproduced complete historical identity pinning plus replacement-bound proof
decisions. The only remaining promotion gate is one fresh independent Sol
Medium architecture review of these identical seven-path bytes. H6-G remains
locked until that authority also returns P0=0/P1=0.

Final architecture promotion state: `APPROVED` for exactly the same candidate
`eb9a28add5c2d0f9a961c54042c1891fc06cf2b57a94f095a8c0fb4d85553e88`.
The fresh independent Sol Medium authority returned P0=0/P1=0/P2=0. The
outcome/evidence priority contract is therefore `PROMOTED`; any byte change to
its seven paths invalidates that promotion identity. This unlocks only the
controller-turn-recovery architecture phase. No successor code has started.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make outcome, safety and observable proof explicitly outrank proxy optimization targets "
        "across planning, execution, review and repository workflow instructions."
    ),
    decomposition=(
        "Classify accepted outcomes, non-negotiable guarantees, hard external constraints and secondary metrics.",
        "Require proof-preserving replacement before removing any validation or evidence gate.",
        "Return a bounded replan when a hard cap conflicts with required behavior or proof.",
        "Prove the contract with a realistic line-cap regression and consistent packaged workflow wording.",
        "Exercise one structured promotion-gate decision boundary rather than a vacuous local arithmetic assertion.",
        "Freeze the promoted historical inventory with an immutable cardinality and canonical commitment.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "No line, file, duration, token, coverage, complexity, score or inventory target can silently weaken a higher-priority outcome or guarantee.",
        "A hard external cap records its conflict policy and produces replan rather than false success when required proof cannot fit.",
        "The 8900-line scenario preserves persisted-artifact reopening, artifact-role binding and the typed case contract, or returns a truthful replan.",
        "A missing required proof obligation cannot produce success even when a proxy cap is met, while a verified proof-preserving replacement may continue.",
        "Replacement continuation is authorized only by replacement-bound ordered proof obligations, never by a replacement metric plus current-artifact proof.",
        "Promoted reconciliation evidence remains bound to its reviewed historical candidate while legitimate successor source edits pass current validation.",
        "The exact promoted candidate digest and frozen digest-scope commitment reject syntactically valid identity substitution.",
        "Malformed or open evidence, truncated or substituted inventory, unsafe or missing paths, unsupported status and stale retained-wheel facts fail closed.",
        "Exactly seven owned paths change, zero artifacts are added, focused gates pass and independent objective plus architecture reviews have P0/P1 zero.",
    ),
    mutable_surfaces=(
        "plugins/personal-workflow-skills/skills/plan-work/SKILL.md",
        "plugins/personal-workflow-skills/skills/execute-milestone/SKILL.md",
        "plugins/personal-workflow-skills/skills/review-work/SKILL.md",
        "templates/AGENTS.md",
        "tests/test_workflow_assets.py",
        "scripts/validate.py",
        "docs/reviews/evidence/workflow-skill-contracts.json",
    ),
    protected_surfaces=(
        "all Python runtime, schemas, controller state, runners, registries and entrypoints",
        "all other skills, templates, fixtures, validators, packaging metadata and evidence",
        "the exact promoted structured-output wheel and every unrelated dirty change",
        "H6-G and every later milestone",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only outcome-evidence-priority-contract in the existing dirty "
        "python-sdk-controller worktree. This is the one Sol High escalation after two unsuccessful Sol Medium cycles. "
        "Repair candidate b0c431c3... on exactly the same seven frozen paths and add no artifact. Preserve the existing "
        "skill contract and promoted evidence bytes. Keep one consistent "
        "priority hierarchy: accepted outcome, required safety/integrity/proof, explicit hard external constraints, then "
        "secondary metrics. Replace the vacuous line-cap arithmetic test with one private typed validator decision boundary "
        "used by the top-level gate. Drive 9120/8900 through it with the three named proof obligations; require bounded "
        "replan without a proof-preserving replacement and forbid success when any proof is missing. Freeze the historical "
        "49-entry inventory using canonical JSON commitment 18ab3d341f5920691d42c2f6d6a2ae6571cd09aed9017599a918774d439594c3; "
        "reject deletion and valid-digest substitution without comparing historical hashes to successor source bytes. "
        "Anchor promoted candidate fd9331c855d8352febc84ad56df142aec1bc7864f07fcd07d0b582321860996d and the "
        "576-byte scope commitment 03c422680ae42c37b1e5367a1d3d53a41f2d5dd535d2b39d5c4a9ee811b1cb9c. "
        "Reject valid identity/scope substitution without current-source comparisons. Split current and replacement-bound "
        "proof obligations: a replacement count alone or missing/reordered/false replacement proof must never continue; "
        "only an at-cap verified replacement may. Retain existing evidence/wheel/path adversaries. Run only the focused workflow-assets partition, direct "
        "validator and diff hygiene unless actual impact invalidates carried gates. Do not rebuild the wheel, rerun the "
        "provider sentinel or start H6-G. Self-review with P0/P1 zero and send one terminal callback to the planning controller."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=8_000,
)
```

## Program successors

## Retained execution — controller-turn-recovery

Legacy plan label: H6-G. This section retains the accepted implementation
architecture and original capsule; implementation, bounded correctness and
native-profile closure are complete and the exact promotion record appears
below. The accepted outcome is unchanged: every controller callback is a
durable decision whose effective actions and final acknowledgement survive a
failed, interrupted, unavailable or replaced controller turn without losing
work, duplicating effects or making the App lifecycle authority.

The outcome/evidence priority rule is binding. Observable recovery correctness
and the required integrity, idempotency, lineage, isolation and retained proof
outrank file counts, test counts, duration or other proxies. The separate real
SDK delayed-steer sentinel already consumed by live-worker-control remains
`EXTERNAL_BLOCKED`; this milestone neither retries it nor treats deterministic
controller recovery as evidence that the provider gate passed.

The exact accepted inputs are frozen: outcome-evidence-priority-contract
candidate `eb9a28add5c2d0f9a961c54042c1891fc06cf2b57a94f095a8c0fb4d85553e88`
is `PROMOTED` with objective and architecture P0=0/P1=0/P2=0; deterministic
live-worker/control candidate
`b49e8da8debe577c4b3bb661bd0c1aa8ac6cab46a5161cabdd5d8b2195c15bb5`
and exact wheel
`44dbeaa82538c8b08e6a2210cd7e51c20591c3e94685656724f311e3aeb63603`
have objective and architecture P0=0/P1=0/P2=0. Any change to those promoted
surfaces is outside this capsule. Plugin parity, the human TUI and integrated
promotion remain protected successors.

### Frozen boundary decisions

- SQLite remains the only durable workflow and decision authority. Wake
  delivery, controller generation state, exclusive claim, action commit and
  acknowledgement are distinct facts. A successful SDK call or CLI response
  never implies a later fact.
- The source controller and any replacement use the official shared-session
  SDK. They never use App task APIs, App state, a private `CODEX_HOME`, copied
  authentication, task polling or transcript prose as authority.
- The controller model chooses intent only through one closed typed action
  bundle. The harness claims, snapshots, validates, commits and acknowledges;
  the model cannot write SQLite, invent a successor, create a worktree or own a
  callback.
- Existing terminal result closure already releases every pre-authorized
  successor atomically in `Ledger._finalize_queue_result_in_transaction`.
  Controller recovery preserves that invariant. It validates the immutable
  authorized/released successor snapshot and rejects any bundle that names a
  new, unreleased or conflicting successor; it does not add a second successor
  scheduler or delay terminal closure behind a model turn.
- One decision permits at most two model generations by default: the initial
  source-controller generation and one bounded recovery/replacement generation.
  A human claim consumes no model generation. Budget exhaustion, unverifiable
  ownership or source-controller ambiguity closes once as
  `human_attention_required` and cannot become an automatic approval.
- Claims use a random capability returned only over authenticated same-UID
  local IPC. SQLite stores its SHA-256, never the token. Model and human claims
  use the same CAS boundary; exactly one unexpired claimant wins.

### Production architecture map and artifact budget

One implementation owner owns every `modify` and `create` path below. Allowed
dependency direction is
`domain/contracts -> ledger -> control client -> controller recovery runner ->
supervisor/CLI`; the SDK adapter is an outer transport boundary used only by
the runner/supervisor. Neither contracts nor the ledger import SDK, service,
CLI or process code. The later TUI imports only the typed control client and
domain models.

- `modify src/codex_flow/domain.py`: own durable semantic identifiers, enums
  and immutable service models. Add `ControllerDecisionId`,
  `ControllerDecisionState`, `ControllerClaimantKind`,
  `ControllerGenerationState`, `ControllerActionKind`,
  `ControllerDecisionSummary`, `ControllerDecisionClaim`,
  `ControllerDecisionStatus`, `ControllerGenerationStatus` and
  `ControllerActionReceipt`. These types validate bounded identifiers,
  revisions, generations, deadlines, digests and claimant state; they contain
  no raw IPC dictionaries or transcript text.
- `modify src/codex_flow/contracts.py`: own the model serialization boundary.
  Add the closed schema-v1 `ModelFacingControllerAction` discriminated union,
  `ModelFacingControllerActionBundle`, strict JSON parser/projector and
  `model_facing_controller_action_schema()`. The bundle fields are exactly
  `schema_version`, `decision_id`, `generation`, `action_id`,
  `expected_revision`, `expected_successor_dispatch_ids`, `actions` and
  `rationale`; it contains one to eight closed actions and is bounded to 32
  KiB. No free-form fallback or second schema file is created.
- `modify src/codex_flow/ledger.py`: remain the only SQLite/migration/state
  writer. Add the forward-only v14-to-v15 migration, exact v15 DDL/shape and
  row validation, decision creation/claim/lease/commit/ack/recovery methods and
  a single transaction that validates and applies an action bundle. The ledger
  imports no SDK/process code and performs no external side effect.
- `modify src/codex_flow/backends/codex_sdk.py`: remain the sole raw SDK
  envelope boundary. Add `ControllerThreadInspectionKind`,
  `ControllerThreadInspection` and one bounded `inspect_controller_thread`
  read. It reads the authoritative thread exactly once per persisted recovery
  decision, returns only typed turn identity/status plus an optional strictly
  decoded action bundle, and never returns raw transcript prose. Add the
  supported shared-session start/resume turn seam used by the runner; exact SDK
  not-found/failed/interrupted facts are typed, and every unknown envelope or
  RPC remains ambiguous.
- `create src/codex_flow/controller_recovery.py`: one responsibility only:
  execute one bounded controller generation around the typed control client and
  shared SDK. `ControllerGenerationRunner` claims the exact decision, reads the
  closed durable status/activity/successor snapshot, builds a bounded prompt,
  runs one schema-bound turn, submits its bundle and acknowledges the committed
  receipt. `ControllerGenerationRecovery` performs one persisted-thread
  inspection and returns one deterministic next step; it owns no SQLite access,
  App API, polling loop, worker lifecycle or successor scheduling.
- `modify src/codex_flow/control_client.py`: generalize the existing local
  client without exposing wire/SQLite shapes. Keep `LiveWorkerControlClient`
  compatible and add `ControllerDecisionClient` with typed `pending`, `status`,
  `claim`, `renew_claim`, `submit_actions`, `acknowledge` and
  `request_recovery` operations. The same API is the only later TUI mutation
  boundary.
- `modify src/codex_flow/supervisor.py`: remain the single service-time owner.
  Convert each newly created checkpoint/terminal wake into one decision, drive
  decision deadlines from the existing select/lease loop, spawn at most one
  `controller-generation` subprocess per persisted generation, reap it without
  inferring completion, expose the closed IPC operations and invoke one
  recovery inspection only when a persisted deadline/startup fact is due.
  Worker child/control ownership remains byte-for-byte semantically unchanged.
- `modify src/codex_flow/cli.py`: add the semantic `controller-decision`
  command group. Human-facing `list`, `status`, `claim`, `submit`, `acknowledge`
  and `recover` commands call only `ControllerDecisionClient`; the private
  `controller-generation` service entrypoint calls the runner. JSON output is
  the typed public projection. No command accepts a ledger path or raw SQL.
- `modify config/test-partitions.toml`: place the one new semantic recovery
  test module in the `controller` partition and preserve exact one-partition
  ownership.
- `modify tests/test_ledger_integrity.py`,
  `tests/test_codex_sdk_adapter.py`, `tests/test_supervisor_recovery.py`,
  `tests/test_live_worker_control.py`, `tests/test_workflow_control.py` and
  `tests/test_public_api_semantic_cutover.py`: extend existing owners only for
  v15 migration, SDK inspection, supervisor/IPC/client/CLI integration and
  semantic-name/public-reachability regression coverage.
- `modify tests/test_plan_compilation.py`: replace its stale active-capsule
  assertion with exact compilation of `controller-turn-recovery`, including
  objective/architecture authorities, surfaces and the one-Python-fence rule.
- `create tests/test_controller_turn_recovery.py`: one production-shaped
  provider-free state-machine/fault/restart matrix. Do not split each fault
  into a new fixture module.
- `create docs/reviews/evidence/controller-turn-recovery.json`: one sanitized
  retained record for exact candidate/wheel/service identity, migration and
  recovery matrices, idempotency/lineage/isolation proof, affected/full gate
  results, provider-gate separation and self-review.
- `preserve src/codex_flow/controller.py`, `src/codex_flow/service.py`,
  `src/codex_flow/worker.py`, `src/codex_flow/ipc.py`,
  `src/codex_flow/native_profile.py`, `src/codex_flow/plan_capsule.py`,
  `src/codex_flow/projection.py`, `src/codex_flow/config.py`,
  `src/codex_flow/app_native.py`, every plugin/skill/schema/template,
  `pyproject.toml`, `uv.lock`, `Makefile`, `AGENTS.md`, all accepted evidence
  and every unrelated dirty byte. Existing IPC framing and the systemd unit
  already host the supervisor and require no new production path or dependency.
- `remove` nothing.

The new-artifact budget is exactly three files: one production module, one test
module and one evidence record. This is a secondary cap, not permission to
collapse a required type or proof boundary. Exceeding it requires a bounded
architecture replan with the missing guarantee identified. No new runtime
dependency, public registry, runner, schema file, service unit or entrypoint
outside the paths above is authorized.

### SQLite v15 persistence and compatibility

V15 is a serialized, crash-atomic, forward-only migration from exact v14. It
creates `controller_decisions`, `controller_decision_generations` and
`controller_action_outbox`, then rebuilds `wake_outbox` with one non-null unique
`decision_id` foreign key. It updates schema version/identity and migration
marker only after all rows and indexes are complete. Migration fault injection
before/after every create, copy, drop and metadata update must roll back to an
exact reopenable v14 database; reopening an exact v15 database is idempotent.
Downgrade is unsupported: pre-v15 binaries fail on the newer schema marker and
no down migration rewrites durable authority.

`controller_decisions` is the current decision/CAS authority:

- `decision_id TEXT PRIMARY KEY`, deterministically
  `decision/<wake delivery id>`; `dispatch_id` foreign key; `kind` is
  `checkpoint` or `terminal`; `(dispatch_id, kind)` and `decision_id` are
  unique, preserving the existing one-wake-per-kind rule;
- canonical bounded `summary_json` plus `summary_sha256`; source thread id;
  state in `pending_delivery`, `awaiting_claim`, `claimed`,
  `action_committed`, `acknowledged`, `superseded`,
  `human_attention_required` or `legacy_closed`; monotonic `revision >= 0`;
  `current_generation > 0`;
  `generation_budget` in 1..2 and `generation_used` within that budget;
- `claimant_kind` (`model` or `human`), claimant id, claim-token SHA-256 and
  claim timestamp are all-null before first claim or all-present and immutable
  afterwards for audit. Claim lease expiry is present only while the claim is
  live and is cleared by a committed/acknowledged/superseded/attention
  transition. Expiry alone never steals an active or ambiguous model
  generation; it only makes a terminally inspected generation eligible for the
  recovery decision below;
- committed `action_id`, canonical action bundle JSON/SHA-256 and commit time
  are all-null or all-present; acknowledgment time exists only after commit;
  `superseded_at` is present only when a terminal result makes an outstanding
  checkpoint decision obsolete; `deadline`, bounded human-attention reason and
  created/updated timestamps are mandatory where their state requires them.

`controller_decision_generations` preserves lineage rather than overwriting it:

- primary key `(decision_id, generation)`, generation 1..2, stable
  `lineage_id`, optional predecessor generation and source kind
  (`source_controller` or `replacement_controller`);
- state in `prepared`, `delivery_starting`, `active`, `completed`, `failed`,
  `interrupted`, `ambiguous`, `unavailable` or `superseded`; canonical prompt
  digest; controller thread/turn ids all-null before identity and immutable
  afterwards; unique non-null `(controller_thread_id, controller_turn_id)`;
- one `inspection_started_at`/`inspection_completed_at` pair and typed
  inspection outcome. A uniqueness/check constraint makes a second read for
  the same recovery decision impossible. Start, terminal and replacement
  timestamps must agree with the state.

`controller_action_outbox` is the immutable effect/ack audit:

- `action_id` primary key, `decision_id` unique foreign key, generation,
  expected decision revision, claimant kind/id, canonical bundle JSON and
  SHA-256, exact effect receipt JSON/SHA-256, state `committed` or
  `acknowledged`, committed/acknowledged timestamps and a unique
  `(decision_id, bundle_sha256)` identity;
- the row is inserted only in the same transaction as every effective ledger
  mutation and the decision transition to `action_committed`. A crash cannot
  persist an effect without its digest/receipt or an outbox row without its
  effect. Acknowledgment is a later CAS transaction.

The rebuilt `wake_outbox` retains every v14 delivery field and delivery-state
meaning, adds `decision_id`, and adds one `suppressed` state for an undelivered
wake pre-empted by an exact human claim. A suppressed wake has no source turn,
is never deliverable and retains its source route only as audit. Delivery
attempt state never doubles as a decision claim or acknowledgement. Migration
creates a decision for every existing wake without SDK activity: pending
delivery remains `pending_delivery`; an exhausted `failed` delivery becomes
`human_attention_required`; delivered/not-applicable delivery becomes
`awaiting_claim`; starting/ambiguous delivery becomes
`human_attention_required` because the external identity window cannot be
reconstructed; an already terminal, fully closed historical dispatch may use
`legacy_closed`. Migration never fabricates an action, claim, controller turn
or successor. Existing v14 ledgers without wakes receive no decision row.

Required indexes are: decisions by `(state, deadline, decision_id)` and
`(dispatch_id, kind)`; generations by `(state, decision_id, generation)` plus
the unique non-null controller thread/turn identity; action outbox by
`(state, committed_at, action_id)`; and the existing wake
`UNIQUE(dispatch_id, kind)` plus new `UNIQUE(decision_id)`. All three new tables
foreign-key to their owning dispatch/decision with `ON DELETE CASCADE`; no row
may point outside the queue or lineage.

### Transactional state machine, CAS and idempotency

    pending_delivery --wake identity committed--> awaiting_claim
    awaiting_claim --claim(decision revision)--> claimed
    claimed --validated action bundle + all effects--> action_committed
    action_committed --ack(action id + digest + revision)--> acknowledged

    pending_delivery --typed pre-identity failure within delivery budget--> pending_delivery
    delivery_starting --identity lost/ambiguous--> human_attention_required
    awaiting_claim|claimed --one terminal inspection: completed bundle--> action_committed
    awaiting_claim|claimed --one terminal failed/interrupted inspection + budget--> next generation
    awaiting_claim|claimed --active inspection--> unchanged and not replaceable
    awaiting_claim|claimed --ambiguous/malformed/exhausted inspection--> human_attention_required
    any non-acknowledged state --one valid human CAS claim--> claimed
    pending_delivery --human CAS claim / suppress wake--> claimed
    outstanding checkpoint decision --dispatch terminal commit--> superseded

- Decision creation is part of the same transaction that creates/rearms a wake.
  Terminal finalization still commits result, authorized successor release,
  wake and decision together. Checkpoint claiming still disarms the checkpoint
  and creates its wake/decision together.
- Claim requires decision id, generation, claimant kind/id, expected revision,
  unexpired decision deadline and no committed action. A first claim increments
  revision and returns the secret capability once. Repeating the exact claimant
  request with the token is idempotent; a different claimant/token, stale
  revision, expired claim or terminal decision is rejected without mutation.
  A human claim may win directly from `pending_delivery`; the same transaction
  marks the still-undelivered wake `suppressed`. Wake delivery may start only
  while the decision remains `pending_delivery`, so model delivery and human
  claim cannot both win.
- Claim renewal is bounded by the original decision deadline, requires the
  exact token and revision, increments revision and cannot revive a terminally
  inspected, committed or acknowledged decision.
- An expired human claim with no committed action is released back to
  `awaiting_claim` by one supervisor CAS lease-reap; the expired token cannot be
  replayed. An expired model claim is never released by the clock alone: its
  persisted generation must first receive the one authoritative inspection.
- Bundle submission requires the current decision/generation, unexpired exact
  claim token, expected decision revision and action id. The canonical digest
  binds every field including the sorted exact
  `expected_successor_dispatch_ids`. Same action id plus same digest returns the
  prior receipt; same id or decision with a different digest is a conflict.
- Allowed action variants are exactly `acknowledge_only`,
  `rearm_checkpoint`, `retry_dispatch`, `cancel_dispatch`,
  `change_retry_budget` and `require_human_attention`. Retry/cancel/budget
  actions also carry their existing recovery action id and expected
  retry-policy revision. `acknowledge_only` is the sole action in its bundle;
  re-arm cannot mix with cancel/human-attention; duplicate or contradictory
  actions fail. No action may mutate a different dispatch.
- `expected_successor_dispatch_ids` must equal the ledger's exact sorted
  authorized/released successor set at commit. A requested new successor,
  missing released successor or changed set makes the whole bundle stale. This
  binds approval context without transferring successor authority to the
  controller.
- The action transaction revalidates queue terminal state, decision deadline,
  claim, decision revision, retry revision, checkpoint arm and successor facts
  before any write. It either applies all actions, inserts the immutable outbox
  receipt and increments the decision revision, or applies nothing.
- Acknowledgment requires action id, bundle digest, committed revision and
  claim token. It changes both outbox and decision to acknowledged in one
  transaction. Exact replay returns the acknowledged receipt; any other digest,
  claimant, stale revision or later action fails. Acknowledgment suppresses all
  future generation/recovery work.
- Terminal queue mutation after summary/claim but before commit makes the
  bundle stale unless it is the exact terminal `acknowledge_only` snapshot.
  Post-terminal retry, cancel, budget change or checkpoint re-arm is rejected.
  Terminal result closure atomically marks any outstanding checkpoint decision
  `superseded` before removing its checkpoint wake; no orphan decision, claim,
  generation or action outbox remains claimable.
  Existing live-worker control command replay remains governed by the frozen
  H6-F command id/submission-sequence contract and is not reimplemented here.

Crash expectations are exact: before turn identity a typed SDK failure may
reuse the same prepared generation only within the two-attempt wake-delivery
budget; after identity it is persisted before any model output is accepted;
before claim no claimant exists; after claim lease/token facts survive; before
commit no action/outbox/effect exists; after commit replay returns the one
receipt; after acknowledgment every later attempt is a no-op receipt. A crash
in the uncommitted identity window is ambiguous and requires human attention,
never speculative redelivery.

### Shared-SDK controller generation recovery

`ControllerGenerationRecovery` performs no routine status polling. The
supervisor invokes it once at startup for a due nonterminal generation, once at
a persisted decision/claim deadline, or on an explicit authenticated recovery
request. The ledger CAS-reserves that inspection before the SDK read, so a
restart cannot perform it twice.

- The adapter reads exactly one authoritative thread snapshot for the persisted
  controller thread/generation. It considers only the latest turn, validates
  bounded thread/turn identity and status, and decodes a completed output only
  as `ModelFacingControllerActionBundle` with the exact decision id,
  generation, action id and revision. Older bundles and summaries are never
  authority.
- A valid completed unacknowledged bundle is submitted through the same CAS
  path and then acknowledged. Completed output without one exact valid bundle,
  conflicting output or malformed history closes as human attention; it is not
  regenerated from prose.
- Exact terminal `failed` or `interrupted` evidence with no committed action
  and an expired/unclaimed claim may atomically supersede the generation and
  create the next generation with the same decision id, incremented generation
  and revision. At most one such rollover exists.
- Exact structured active-writer proof leaves the generation active and creates
  no replacement. A latest-turn active status without that proof is ambiguous:
  it is left alone and is not replaceable. Other ambiguous ownership,
  identity, multiple-candidate or unknown RPC facts fail closed to human
  attention and create no writer.
- Source-controller unavailability is recognized only by the adapter's exact
  typed not-found/unavailable SDK fact. Within the remaining generation budget,
  the next generation starts one shared-session replacement thread in the same
  lineage and selected repository cwd with the same-or-narrower effective
  native authority. It receives only decision id, canonical bounded summary,
  decision revision, action schema and exact callback/service route. No raw
  transcript, credential, worker prompt/result body or App state is copied.
- If source unavailability is not proven, the generation budget is exhausted,
  replacement start is ambiguous, the source route lacks required permission,
  or the controller process disappears again, the decision becomes
  `human_attention_required`. The existing terminal wake delivers that status
  once; no open model turn, repeated checkpoint or recursive controller is
  created.

Worker prompts remain capability-bound leaves. They submit only one raw typed
result to the harness and never receive decision ids, claim tokens, callback
instructions, scheduling authority or controller recovery responsibilities.

### Security, privacy, permissions and non-goals

- Persist only canonical bounded summaries, type/status identifiers, digests,
  revisions and sanitized reasons. Never persist claim tokens, provider values,
  auth material, raw prompts, raw worker results, transcript prose, SDK raw
  envelopes or environment values. Diagnostic/activity input remains redacted
  and count/byte bounded under the promoted H6-F policy.
- Same-UID private UNIX IPC, bounded frames/deadlines and the existing
  supervisor lease are mandatory for every mutation. CLI JSON files are parsed
  with no symlink following, bounded bytes and exact closed schemas. No shell
  interpolation, `eval`, `exec` or direct database option is introduced.
- Controller generations inherit the source controller's effective native
  sandbox/approval authority and may only narrow it. Permission/profile drift
  fails before thread creation. `danger-full-access` is preserved when it is
  the validated native grant; `workspace-write` is never hardcoded as a
  detached default.
- Non-goals are a persistent controller-model process, App task polling,
  background TUI polling, plugin capability work, a second transport, automatic
  planning, new successor authorization, repair-capsule invention, unbounded
  retry, replay of prose, remote/multi-user administration, direct SQLite/TUI
  coupling, provider sentinel retry, App/native global mutation, service
  installation, worktree creation or Git history mutation.

### Provider-free validation, evidence and promotion

The deterministic implementation matrix must cover:

- exact v14 creation, every v14-to-v15 migration fault point, v15 reopen,
  legacy pending/delivered/not-applicable/starting/ambiguous/terminal wake
  classification, foreign keys/indexes/checks, hybrid/corrupt rows and
  forward-only old-binary rejection;
- wake/decision atomicity, two concurrent model/human claimants, claim renewal
  and expiry, stale revision/token/generation, action-id replay/conflict,
  bundle digest/canonicalization, conflict matrix, retry-policy CAS, successor
  snapshot drift, terminal mutation, commit/ack replay and cleanup;
- process/SDK faults before identity, after identity, before/after claim,
  before/after action commit and before/after acknowledgment, plus supervisor
  crash/restart at every persisted state. Every schedule proves at most one
  effective action, one outbox receipt and one acknowledgment;
- one-read active, completed-valid, completed-malformed, failed, interrupted,
  unavailable, missing, multiple-turn and ambiguous SDK snapshots; exact
  initial-plus-one replacement lineage; source permission/profile drift;
  human closure with zero model invocation; and no App API/task or polling;
- typed client/CLI round trips and negatives with no raw dict/SQLite leakage;
  unchanged live-worker steer/interrupt/cancel/retry/budget behavior; worker
  leaf prompts; bounded privacy/redaction; 0/1/many workers and decision
  cleanup.

During implementation run the smallest discriminating tests. Closure requires
the affected `contracts`, `controller` and `workers` semantic partitions, the
integration service-lifecycle regression, direct validator/compile checks and
one full `make check` because the SQLite schema, public contracts and CLI are
shared. Then run Ruff, `git diff --check`, exact-wheel build/install/import/help
checks and a provider-free temporary foreground service using the installed
wheel, fake SDK and disposable repository. It must recover one failed
controller generation and one human-claimed decision across service restart
without App/provider calls or persistent service/global mutation. Do not run
the real SDK delayed-steer sentinel.

The retained evidence record is schema
`codex-flow/controller-turn-recovery/v1` and includes exact candidate digest and
sorted digest scope, wheel SHA-256/size, Python/SDK/package versions, v14/v15
schema identities, migration/restart/fault matrix counts, decision/action ids
and digests, generation/claim/lease/revision/ack facts, replacement lineage,
successor and worker-isolation results, affected/full gate commands/counts,
exact-wheel/service cleanup, protected-surface hashes, secret-negative scan and
self-review counts. The prior real SDK delayed-steer gate is recorded only as
`external_blocked` with its retained evidence identity and
`attempt_reused=false`; no new provider outcome is claimed.

Acceptance modes are `objective` and `architecture`. Implementation self-review
must report P0=0/P1=0. Freeze one exact candidate and evidence record, then run
one independent Luna XHigh objective review and one independent Sol Medium
architecture-conformance review, both read-only against the same digest.
Promotion requires both P0=0/P1=0; tests, wheel/service facts and inventory
counts cannot override an observable recovery or integrity finding. H6-J later
owns integrated independent promotion, but this milestone must first close its
own objective and architecture gates.

### Dependency DAG and ownership

    PROMOTED outcome-evidence-priority-contract
                  +
    PROMOTED deterministic live-worker/control boundary
                  |
                  v  acceptance dependency
    controller-turn-recovery implementation + objective/architecture promotion
                  |
                  +--------------------+
                  |                    |
                  v                    v
    plugin-capability-parity      human-terminal-ui
                  \                    /
                   +------------------+
                             |
                             v
                  integrated-control promotion

No safe internal implementation fan-out is retained. The persisted v15 schema,
decision state authority, typed public control API and supervisor entrypoint
form one vertical observable recovery path; splitting them would create serial
scaffolding and multiple owners for shared schema, state authority and
entrypoint surfaces. Read-only objective and architecture reviews may run in
parallel only after the candidate freezes. The outgoing edges to plugin parity
and the TUI are `acceptance dependency` edges: both consume the promoted public
contracts, while their mutable production surfaces are disjoint. H6-I may not
consume or reimplement this API before controller-turn-recovery promotion.
H6-J remains serial by `acceptance dependency` on all promoted component
candidates.

Unresolved decisions: none. Ordinary implementation choices inside one owned
module are Luna-owned; any change to the persisted/public contract, ownership,
security/privacy/permission boundary, provider authority, destructive behavior,
successor semantics, artifact budget or acceptance gate requires a bounded Sol
plan update.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make every controller callback a durable exclusively claimed decision whose bounded "
        "actions and acknowledgement recover exactly once across controller failure or replacement."
    ),
    decomposition=(
        "Add the forward-only SQLite v15 decision, generation and action-outbox authority while keeping wake delivery a separate fact.",
        "Implement closed model-facing controller actions and one typed local decision client reusable by the later human TUI.",
        "Run one shared-SDK controller generation and one-read bounded recovery path without App lifecycle authority, polling or transcript replay.",
        "Commit action bundles and acknowledgements with revision-bound CAS, idempotency, lineage and existing successor/worker isolation.",
        "Prove migration, crash/restart, model/human claim, replacement, privacy and exact-wheel service behavior with sanitized retained evidence.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "V14 migrates crash-atomically and forward-only to exact v15 decision/generation/action-outbox and decision-bound wake schemas; reopen, corruption and legacy-wake classifications are deterministic.",
        "Wake delivery, claim, action commit and acknowledgement remain distinct; every injected failure window produces at most one effective action, immutable receipt and acknowledgement.",
        "Model and human claimants share one typed CAS boundary with bounded leases, secretless persisted tokens, stale/conflicting/replayed/terminal rejection and no raw IPC or SQLite coupling.",
        "A completed valid controller bundle is recovered once; active turns are left alone; ambiguous facts fail closed; only failed/interrupted unacknowledged generations or exact source unavailability permit the single bounded rollover lineage.",
        "Controller actions cannot invent or release successors outside the existing atomic authorization, mutate another dispatch, bypass retry revisions, rearm terminal work or weaken leaf-worker ownership.",
        "The App, TUI and source controller process may be absent; one human claim closes without a model and one shared-SDK replacement recovers without polling, private CODEX_HOME, copied auth or widened permission.",
        "Affected contracts/controller/workers partitions, service regression, full make check, Ruff, validator/compile, diff hygiene and exact-wheel provider-free service gates pass with sanitized evidence and protected hashes.",
        "The consumed real SDK delayed-steer sentinel is not retried and remains separately external_blocked; self-review and independent objective plus architecture reviews of one exact candidate report P0=0/P1=0.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/contracts.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/controller_recovery.py",
        "src/codex_flow/control_client.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/cli.py",
        "config/test-partitions.toml",
        "tests/test_ledger_integrity.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_supervisor_recovery.py",
        "tests/test_live_worker_control.py",
        "tests/test_workflow_control.py",
        "tests/test_public_api_semantic_cutover.py",
        "tests/test_plan_compilation.py",
        "tests/test_controller_turn_recovery.py",
        "docs/reviews/evidence/controller-turn-recovery.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md and AGENTS.md",
        "src/codex_flow/controller.py, service.py, worker.py, ipc.py, native_profile.py, plan_capsule.py, projection.py, config.py and app_native.py",
        "pyproject.toml, uv.lock, Makefile and every plugin, skill, schema and template",
        "promoted live-worker control behavior and its separately external-blocked consumed provider sentinel",
        "accepted evidence except the new controller-turn-recovery record",
        "H6-H plugin parity, H6-I TUI, H6-J integration, global Codex/App/plugin state, Git topology/history and unrelated dirty bytes",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only controller-turn-recovery in the existing deliberately dirty "
        "python-sdk-controller worktree. Implement the frozen v15 schema, typed controller decision API, "
        "shared-SDK generation runner and one-read recovery state machine exactly as designed. Preserve "
        "terminal successor release, live-worker control and leaf-worker ownership; create no second scheduler, "
        "raw SQLite/TUI boundary or App authority. Run focused tests while iterating, then every named affected/full, "
        "exact-wheel and provider-free temporary-service gate. Do not retry the consumed real SDK sentinel, run a "
        "provider, install a service/plugin, create peers/subagents/worktrees/commits, mutate Git/global state or start "
        "plugin parity, TUI or integration. Retain one sanitized semantic evidence record, self-review the complete "
        "owned diff and return exactly one raw schema-v1 ModelFacingResult with independent reviews pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

This was the sole implementation capsule. Its architecture and ownership are
retained for provenance; it is no longer executable.

## Retained execution — controller-turn-recovery-correctness-closure

The exact `controller-turn-recovery` candidate
`884c1edde3bb5d32cff662bbeb60e6457c783d0311a4f86d1376da76fac7f41d`
is retained rejected evidence. Its independent Luna XHigh objective review
returned `DO_NOT_PROMOTE`, P0=0/P1=10/P2=0. Architecture review did not start.
One bounded correctness closure owns all ten findings because they share the
same unpromoted v15 decision/generation/action authority and supervisor/SDK
recovery path. No accepted outcome, public intent, worker authority, successor
semantics, provider gate or later milestone changes.

The rejected v15 schema has no compatibility authority. Amend its exact
v14-to-v15 migration and fresh definition in place; do not introduce v16 or a
compatibility path for a database created only by the rejected candidate. Add
one private persisted inspection receipt to each generation: a once-returned
claim capability stored only by digest, a bounded lease expiry, and optional
canonical inspected-bundle JSON plus SHA-256. Add the semantic typed
`ControllerRecoveryInspectionClaim` returned only by the reserve operation.
Completion requires its exact live token. A completed outcome requires and
atomically stores one exact validated bundle; every other outcome requires no
bundle. Recovered submission accepts the decision identity and loads the
persisted bundle itself, so caller-supplied or reconstructed actions can never
cross the boundary. A restart after completed inspection commits and
acknowledges that persisted bundle without another SDK read. A crash after
reservation but before durable completion never rereads: while the lease is
live it remains owned, and after expiry one CAS records ambiguous human
attention and clears the lost claim.

Make the generation state and inspection outcome identical whenever inspection
is complete. Bind decision action JSON/digest/identity exactly to its sole
outbox row and receipt. Reject terminal or human-attention claim replay before
any token-idempotency branch. Bind every pre-identity prepare/reset to the exact
decision revision, claimant identity and claim capability that owned the
launch. A synchronous `Popen` failure performs that exact reset; an orphaned
`delivery_starting` fact reached only after restart is ambiguous and becomes
human attention rather than spawning another process or clearing a newer human
claim.

`require_human_attention` is an effective action with a committed/acknowledged
outbox receipt while the decision remains `human_attention_required`; its
reason, action fields and acknowledgement are valid together, and the decision
cannot be reclaimed. Other actions retain the ordinary
`action_committed`-then-`acknowledged` transition. The SDK inspection accepts
exactly one closed controller `agentMessage` item for a completed action turn;
missing, unknown, extra or malformed item shapes fail closed.

Checkpoint re-arm creates a new immutable decision/wake cycle instead of
rewriting or returning the prior audit row. Correct v15 adds a positive bounded
cycle sequence to decision and wake identities, uses uniqueness on
`(dispatch_id, kind, sequence)`, preserves terminal wake single-shot behavior,
and caps checkpoint cycles at 64 per dispatch. V14 migration assigns sequence
one to every retained wake/decision. Re-arm rejects an active latest cycle;
after an inactive or acknowledged cycle it arms the binding, and the next due
claim atomically creates the next sequence with a distinct delivery/decision
identity. Prior decision, generation, action and acknowledgement rows remain
immutable.

No new file, module, registry, runner, public entrypoint or durable artifact is
allowed. Existing production modules keep their frozen responsibilities:
`ledger.py` is the only schema/CAS/effect authority; `controller_recovery.py`
owns generation and one-read recovery orchestration; `supervisor.py` owns child
launch/reap; `codex_sdk.py` owns closed SDK conversion; `control_client.py`
owns typed IPC projection; domain/contracts own only the named semantic types.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Close every exact objective-review defect in controller-turn recovery so inspected "
        "actions, claims, launch/reset, human attention and checkpoint re-arm remain durable and exactly once."
    ),
    decomposition=(
        "Amend unpromoted v15 with lease/token-bound inspection receipts, persisted exact bundles and sequenced immutable decision/wake cycles.",
        "Make claim, inspection, action/outbox, human-attention and pre-identity reset transitions exact cross-field CAS authorities.",
        "Recover completed inspections from their durable bundle, expire lost reservations fail-closed and contain every supervisor launch orphan.",
        "Close SDK item conversion and prove all ten predecessor discriminators plus migration, restart, concurrency, re-arm and unchanged-worker behavior.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Recovered commit can apply only the exact canonical bundle persisted by the completed one-read inspection; invented or substituted actions fail before mutation.",
        "Inspection reservation has a once-returned token and bounded lease: live ownership is untouched, expired incomplete ownership becomes durable human attention without a second SDK read, and completed persisted output resumes after restart.",
        "Human-attention actions commit and acknowledge one receipt without corrupting or reclaiming the attention decision; every other terminal, stale-token and replay claim fails before idempotency.",
        "Pre-identity reset requires the exact launch revision/claimant/capability; Popen failure resets safely, restart or ownership drift fails closed, and no newer human claim is cleared.",
        "Generation inspection/state and decision/outbox JSON, digest, action and acknowledgement facts are cross-bound and reopen rejects every hybrid or substituted row.",
        "Completed SDK inspection rejects missing, unknown, extra or malformed items and accepts exactly one matching closed agentMessage action bundle.",
        "Explicit checkpoint re-arm produces a distinct bounded immutable decision/wake sequence, never returns or rewrites the old wake, and remains single-shot while a cycle is active.",
        "The ten independent predecessor probes fail at the rejected candidate and pass at closure; affected partitions, full make check, exact wheel/service, Ruff, validator, compile and diff gates pass with self-review P0=0/P1=0.",
        "The consumed real SDK sentinel remains external_blocked and is not retried; one fresh Luna XHigh objective review and then one Sol Medium architecture review of the same repaired digest both report P0=0/P1=0 before promotion or successors."
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/contracts.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/controller_recovery.py",
        "src/codex_flow/control_client.py",
        "src/codex_flow/supervisor.py",
        "tests/test_ledger_integrity.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_supervisor_recovery.py",
        "tests/test_live_worker_control.py",
        "tests/test_workflow_control.py",
        "tests/test_plan_compilation.py",
        "tests/test_controller_turn_recovery.py",
        "tests/test_controller_execution.py only for corrected v15 migration and exact plan/capsule assertions",
        "docs/reviews/evidence/controller-turn-recovery.json",
    ),
    protected_surfaces=(
        "AGENTS.md and the canonical plan outside this planning-owned closure section",
        "src/codex_flow/controller.py, service.py, worker.py, ipc.py, native_profile.py, plan_capsule.py, projection.py, config.py, app_native.py and cli.py",
        "pyproject.toml, uv.lock, Makefile, plugins, skills, schemas, templates and accepted evidence",
        "promoted live-worker behavior and its consumed external-blocked provider sentinel",
        "H6-H plugin parity, H6-I TUI, H6-J integration, App/global/plugin state, Git topology/history and unrelated dirty bytes",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only controller-turn-recovery-correctness-closure in the existing dirty "
        "python-sdk-controller worktree. Implement the fixed corrected-v15 design and close all ten exact P1s; "
        "do not invent v16, a rejected-v15 compatibility path, another module/class beyond the named inspection "
        "claim, or another authority. Run predecessor discriminators during repair, then affected semantic "
        "partitions, full make check and exact-wheel/provider-free restart gates. Do not run providers, App APIs, "
        "reviews or successors, create tasks/worktrees/commits, or mutate global/Git state. Refresh the single "
        "sanitized evidence record, self-review P0/P1 and return one terminal callback to planning."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

This repair had one mutable owner and no internal fan-out. It is retained
rejected-candidate and repair provenance and is no longer executable.

## Retained execution — native-profile-runtime-compatibility-closure

The repaired `controller-turn-recovery` implementation remains unpromoted
because its sole full-gate failure is the active read-only native-profile
projection. The pinned Codex runtime accepts the current native config without
empty `agents`, `hooks` or `shell_environment_policy` tables, while
`NativeProfileProjection` currently requires all three. The same valid config
also contains `service_tier`, an execution-selection field rather than durable
provider, permission or discovery authority. This bounded compatibility
closure precedes both H6-G reviews and changes no recovery semantics.

Keep one fail-closed projection authority in `native_profile.py`. Split its
preserved top-level allowlist into required and optional-preserved surfaces.
Provider selection and definitions, `model_catalog_json`, `approval_policy`,
`sandbox_mode`, `approvals_reviewer`, `mcp_servers`, and every other previously
required preserved surface remain required. Only `agents`, `hooks` and
`shell_environment_policy` may be absent; when present each must be a table and
must pass the existing bounded clone, secret rejection and TOML rendering
rules. Preserve an optional table only when present and never synthesize an
empty/default value, so omission and explicit configuration have distinct
digests. Recognize `service_tier` alongside model/reasoning selections as
controller-owned execution policy and exclude it from the child projection.
No other new top-level field is allowed.

The existing provider shape, model-catalog file identity, permission enums,
complete MCP inventory and secret-to-ephemeral-environment conversion remain
unchanged. Missing or malformed required authority fails before projection;
unknown top-level fields, unknown fields inside the existing closed provider/MCP
shapes, and literal secret-bearing fields still fail closed. The active source
config is read-only: a temporary parser-proof home must load the projection
through the bundled pinned `codex doctor --json`, expose every active MCP server
and leave the source bytes unchanged. This does not change the shared-session
production topology. Temporary fixtures must prove
absent optional tables succeed, present optional tables are preserved, each
required provider/catalog/permission/MCP surface cannot be omitted, and
unknown or secret-bearing substitutions are rejected without mutation.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Accept the pinned runtime's valid omission of empty native policy tables while preserving "
        "all provider, catalog, permission and MCP authority fail-closed."
    ),
    decomposition=(
        "Separate required preserved native surfaces from the three optional policy tables and omit absent optionals without defaults.",
        "Classify service_tier as controller-owned execution selection while retaining closed rejection of every unknown or secret-bearing surface.",
        "Prove the active read-only profile and adversarial temporary profiles through the pinned parser, then close the full H6-G gate and evidence.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "The active native config projects every MCP server and passes bundled codex doctor --json in a temporary parser-proof home without changing source bytes or production topology.",
        "Absent agents, hooks and shell_environment_policy tables are valid and omitted from projected TOML; present tables are type-checked, sanitized and preserved without invented defaults.",
        "Provider/model-catalog/approval/sandbox/MCP and all other required preserved surfaces remain mandatory, exact under the existing secret-safe projection, and digest-bound.",
        "service_tier is recognized only as non-projected controller-owned execution selection; every other unknown top-level field, closed-shape provider/MCP field and literal secret-bearing field fails before mutation.",
        "Focused active-profile and temporary adversaries, the affected controller/native-profile tests, full make check, validator, Ruff, compile and diff hygiene pass; recovery semantics and the consumed provider sentinel remain unchanged.",
        "One new exact candidate and evidence record are self-reviewed P0=0/P1=0, then fresh Luna XHigh objective and Sol Medium architecture reviews both approve the same bytes before H6-G promotion or successors.",
    ),
    mutable_surfaces=(
        "src/codex_flow/native_profile.py",
        "tests/test_controller_execution.py",
        "tests/test_plan_compilation.py",
        "docs/reviews/evidence/controller-turn-recovery.json",
    ),
    protected_surfaces=(
        "AGENTS.md and docs/reviews/peer-thread-workflow.md outside this planning-owned section",
        "all controller-turn-recovery production and test surfaces except native_profile.py and the two explicitly owned focused test files",
        "the active ~/.codex config, authentication, plugins, MCP processes, model catalog, discovery directories and all App/global state",
        "packaging, schemas, workflow assets, accepted evidence, Git topology/history and unrelated dirty bytes",
        "the consumed external-blocked provider sentinel and H6-H, H6-I and H6-J successor work",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only native-profile-runtime-compatibility-closure in the existing dirty "
        "python-sdk-controller worktree. Preserve absent optional tables by omission, classify service_tier "
        "as controller-owned and keep required authority plus unknown/secret rejection fail-closed. Touch only "
        "the four mutable surfaces; do not run providers or mutate source config, App/global/Git state, recovery "
        "semantics or successors. Run focused adversaries, the pinned doctor proof and full make check, refresh "
        "the single evidence record, self-review P0/P1 and return one terminal callback to planning."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

This was the final bounded closure before H6-G review. It added no module,
public type, schema, registry, runner, entrypoint or durable artifact. Its exact
bytes and evidence were frozen for the objective and architecture reviews
recorded below; it is no longer executable.

Controller-turn recovery is promoted at exact candidate
`58a67173a2f4139692976c1ad64d65967b6dec27885140685d1ebe02daccd59b`.
Its exact temporary candidate wheel was
`518f2567002a01bce9aba0cfa0bf5a0a5b12f1092e0f81ed33f64200e33397b1`
(303694 bytes); the protected historical retained wheel remains
`fea94483f8c0a834ba139612603a223868a536a1837bdb03a02e20319780815a`
(259769 bytes). One fresh Luna XHigh objective/code review and one fresh Sol
Medium architecture-conformance review independently returned `APPROVED`,
P0=0/P1=0/P2=0 on the exact candidate. The 613-test full gate and exact-wheel
provider-free restart proof remain green. The consumed delayed-steer provider
sentinel remains separately `EXTERNAL_BLOCKED` and is not retried by either
successor.

H6-G therefore closes the shared schema/state/API acceptance dependency. The
two sections below are simultaneously `ready`: their exact mutable path sets
are disjoint, neither may amend the frozen H6-G ledger/control API, and H6-J
remains blocked until both independently promote. `src/codex_flow/cli.py` is
owned only by the TUI lane. Plugin parity enters through the existing
plan-driven production controller and has no CLI registration surface.

## Next execution — plugin-capability-parity

Legacy plan label: H6-H. Outcome: SDK workers and controller turns can use an
explicitly required installed Codex plugin capability when the shared-session
runtime genuinely supports it, with no App dependency and no ambient, guessed
or secret-bearing authority.

The implementation architecture is frozen:

- `contracts.py` owns the closed typed `PluginRequirement` and
  `PluginCapabilitySnapshot` serialization boundary. The model-facing capsule
  gains one compatible next version with explicit required-plugin facts;
  schema-v1 capsules remain readable and cannot imply plugin requirements.
- `schemas/capsule.schema.json`, `scripts/validate.py` and the existing
  workflow-asset tests remain the one static/generated parity gate for that
  compatible contract; no plugin-specific schema file or validator is added.
- `plugin_capabilities.py` is the only new production module. It performs
  bounded no-follow discovery from the inherited standard Codex home, binds
  canonical plugin id, version/source, enabled state, bundle digest, bundled
  skill ids, declared MCP/connectors and readiness (`ready`,
  `setup_required`, `unsupported` or `unknown`), and emits only sanitized
  immutable facts. Tokens, OAuth material, headers and secret environment
  values are never persisted or echoed.
- `config.py`, `native_profile.py`, `plan_capsule.py`, `projection.py` and
  `controller.py` carry the single requirement from typed authoring through
  enqueue and revalidate the exact capability snapshot immediately before SDK
  thread creation. Missing, disabled, changed, setup-required or incompatible
  requirements fail before model/tool mutation. Optional ambient plugins never
  satisfy a required one.
- `codex_sdk.py` remains the sole transport boundary and exposes only the
  installed SDK's verified bundled-skill invocation surface. It does not
  interpret plugin catalogs, install anything or add a connector transport.
  `cli.py` is preserved: the existing plan-driven controller entrypoint is the
  only production caller.

Dependency direction is `contracts/config -> plugin capability authority and
native profile -> plan projection/controller -> SDK adapter`. No plugin module
imports controller, CLI, ledger, supervisor or TUI code. SQLite v15, decision
recovery, worker control, process ownership and the human UI remain frozen.
New-artifact budget is exactly two files: the semantic production module and
one sanitized evidence record. Existing focused test modules and the canonical
capsule schema absorb all coverage; no new test module, plugin-specific schema
file, registry, runner, command or public entrypoint is permitted.

Non-goals are installing, trusting, enabling, disabling or authenticating a
plugin; copying App credentials; changing global Codex configuration or plugin
bytes; treating marketplace/catalog presence as readiness; automatically
granting ambient tools; claiming App-only UI headlessly; or invoking a
connector/MCP that lacks separately proven read-only readiness and explicit
authorization.

Acceptance modes are `objective` and `architecture`. Luna XHigh at the app's
configured fast speed is the implementation and objective/code-review route;
Sol Medium is the independent architecture-conformance route. One real
shared-session bundled-skill sentinel is authorized only after deterministic
and exact-wheel gates: it uses an already installed, enabled, ready capability,
one bounded provider attempt and a disposable read-only repository, makes zero
App API/task calls, performs no install/auth/trust/config mutation and records
only sanitized identities/digests. If no installed capability is genuinely
ready, the result is truthful `EXTERNAL_BLOCKED`; no substitute or setup action
is allowed. No connector/MCP provider sentinel is authorized by this capsule.

Promotion requires exact v1 compatibility and v2 requirement projection;
enqueue/start drift rejection; disabled/missing/setup-required/App-only and
secret-negative adversaries; the observable bundled-skill result through the
existing production entrypoint; affected contracts/controller/integration
partitions; full `make check` because shared contracts and projection change;
exact-wheel install/import/runtime proof; validator, Ruff, compile and diff
hygiene; protected H6-G/TUI hashes; one sanitized evidence record; complete
self-review P0=0/P1=0; and fresh independent Luna XHigh objective plus Sol
Medium architecture reviews of one exact candidate with P0=0/P1=0.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Bind explicitly required installed plugin capabilities to the shared-session controller "
        "and prove one real bundled-skill outcome without App, ambient or secret-bearing authority."
    ),
    decomposition=(
        "Add one typed plugin requirement/snapshot contract and one bounded secretless capability discovery authority.",
        "Carry compatible capsule requirements through plan projection and revalidate exact capability immediately before SDK start.",
        "Expose only the verified bundled-skill SDK seam and keep the existing plan-driven controller as the sole production entrypoint.",
        "Run closed drift, compatibility, exact-wheel and one authorized read-only bundled-skill sentinel before independent promotion reviews.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Schema-v1 capsules remain readable and imply no plugin authority; the next compatible capsule version binds explicit canonical requirements and sanitized capability snapshots.",
        "Missing, disabled, changed, setup-required, unsupported, App-only or secret-bearing requirements fail before model/tool mutation and without changing plugin or global state.",
        "The same exact capability digest is bound at enqueue and revalidated before SDK thread creation; optional ambient plugins cannot satisfy a required capability.",
        "One already-ready installed bundled skill changes the observable result through the exact-wheel production controller in a disposable read-only repository with zero App calls and no install, trust, auth or config mutation.",
        "Connector and MCP readiness remain separately classified and no connector/provider sentinel runs without distinct authorization.",
        "Affected partitions, full make check, exact-wheel, validator, Ruff, compile, diff and protected-hash gates pass; sanitized evidence and self-review report P0/P1 zero.",
        "Fresh independent Luna XHigh objective and Sol Medium architecture reviews approve the same exact candidate with P0/P1 zero before H6-J can consume it.",
    ),
    mutable_surfaces=(
        "src/codex_flow/contracts.py",
        "src/codex_flow/config.py",
        "src/codex_flow/native_profile.py",
        "src/codex_flow/plan_capsule.py",
        "src/codex_flow/projection.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/plugin_capabilities.py",
        "src/codex_flow/backends/codex_sdk.py",
        "schemas/capsule.schema.json",
        "scripts/validate.py",
        "tests/test_model_facing_projection.py",
        "tests/test_controller_execution.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_plan_compilation.py",
        "tests/test_shared_sdk_visibility.py",
        "tests/test_workflow_assets.py",
        "docs/reviews/evidence/plugin-capability-parity.json",
    ),
    protected_surfaces=(
        "AGENTS.md and docs/reviews/peer-thread-workflow.md",
        "src/codex_flow/domain.py, ledger.py, supervisor.py, worker.py, ipc.py, service.py, control_client.py, controller_recovery.py, app_native.py and cli.py",
        "src/codex_flow/tui.py, src/codex_flow/tui_client.py and src/codex_flow/tui_models.py",
        "pyproject.toml, uv.lock, tests/test_live_worker_control.py and tests/test_workflow_control.py",
        "docs/reviews/evidence/human-terminal-ui and docs/reviews/evidence/human-terminal-ui.json",
        "plugin bundles, caches, trust/auth state, global Codex configuration, accepted evidence and unrelated dirty bytes",
        "the consumed external-blocked delayed-steer sentinel and H6-J integrated promotion",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only plugin-capability-parity in the existing dirty python-sdk-controller worktree. "
        "Run as the single Luna XHigh mutable owner using the app-configured fast Luna speed. Implement the frozen two-artifact architecture without touching CLI, TUI, ledger, supervisor, controller recovery or plugin/global state. Preserve v1 capsules, add one compatible explicit requirement path, bind and revalidate secretless capability facts, and use the existing production controller for exactly one already-ready bundled-skill sentinel after deterministic and wheel gates. Do not install, enable, trust or authenticate plugins; do not run a connector/MCP sentinel or retry the consumed delayed-steer sentinel. Run the named gates, retain sanitized evidence, self-review P0/P1 and return one terminal callback with independent reviews pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

This capsule is ready and independent of `human-terminal-ui`. It owns no TUI,
dependency-lock or CLI-registration byte.

The implementation candidate produced from that capsule is rejected and is no
longer executable. Its exact 50-path identity was
`538271925eaf5c31a210d087fd185a48e4b2d3ca71c03d7d579801d305f921ac`
and its sanitized evidence record was
`e7ede7921bc4365fabc64a1df42e053cd9d3c32b5be6af7b3eae2708b0fde581`.
The independent objective review approved those bytes, but the independent Sol
Medium architecture review returned `DO_NOT_PROMOTE`, P0=0/P1=4: prompt magic
could still create SDK skill authority, the detached supervisor/worker did not
revalidate the durable plugin binding immediately before SDK creation,
discovery lost marketplace identity and lacked complete no-follow bounds, and
the static schema rejected a closed historical v1 capsule. No prior approval
or carried green gate overrides those blockers.

## Next execution — plugin-capability-authority-closure

Legacy plan label: H6-H bounded repair. Outcome: close the rejected plugin
capability authority end to end so only a typed schema-v2 requirement can
select one verified bundled skill, while every schema-v1 prompt remains plain
text and the detached production worker fails closed on any identity, byte,
readiness or filesystem drift before creating an SDK thread.

This repair changes no accepted outcome or public controller API. It replaces
the disproven implementation detail with one production path and freezes the
following architecture map:

- **Create:** nothing. The cumulative milestone artifact budget remains exactly
  two already-present paths, `src/codex_flow/plugin_capabilities.py` and
  `docs/reviews/evidence/plugin-capability-parity.json`; this repair may not add
  another module, test file, schema, registry, runner, command or evidence
  record.
- **Modify — schema authority:** `contracts.py` emits and parses two closed
  versions. V1 has exactly the historical base keys and omits
  `plugin_requirements`; v2 requires a non-empty `plugin_requirements` array.
  `model_facing_capsule_schema()` expresses those two closed records with one
  bounded `oneOf`. `domain.py` adds only the corresponding bounded Draft
  `oneOf` schema/instance validation: a finite branch count, recursive existing
  depth/cardinality ceilings and exactly one matching branch. It adds no second
  schema model or reduced decoder.
- **Modify — projection and direct controller:** `projection.py` deletes prompt
  marker emission and leaves prompt bytes unchanged for both versions.
  `controller.py` keeps requirements/snapshots as typed, canonical route facts,
  rejects every App-native plugin route, and for its direct SDK seam resolves
  exactly one verified bundled-skill binding into `SkillInput`; it never
  prepends or parses control prose. V2 execution supports exactly one plugin
  requirement with exactly one bundled skill and no MCP/connector invocation;
  zero, multiple, connector-only or multi-skill execution requirements fail
  before SDK mutation rather than selecting an ambient default.
- **Modify — discovery authority:** `plugin_capabilities.py` is the sole reader
  of plugin layout, manifests, config, bundles and skill paths. Flat
  `plugins/<id>` remains `id` with source `bundled`; both
  `plugins/cache/<marketplace>/<name>/<version>` and
  `local-marketplaces/<marketplace>/plugins/<name>` become canonical
  `<name>@<marketplace>` with source `<marketplace>`. One resolution examines
  at most 4,096 directory entries and 128 candidate bundles; each bundle is at
  most depth 32, 8,192 regular single-link files, 16 MiB per file and 256 MiB
  total, and the whole resolution reads at most 256 MiB. Config and manifest
  limits remain 1 MiB each. Exceeding any bound is a typed fail-closed
  capability error.
- **Modify — filesystem and typed SDK boundary:** every authoritative directory
  and file traversal uses descriptor-relative `os.open` with `O_NOFOLLOW` (and
  `O_DIRECTORY` for directories), `fstat` identity/type/link/size checks before
  and after bounded reads, and descriptor-based enumeration. No authoritative
  `Path.resolve`, `exists`, `is_dir`, `iterdir` or `read_bytes` result may bridge
  validation and use. The verified bundled-skill resolver retains the opened
  skill-directory descriptor for the SDK input lifetime and exposes only a
  private context-managed binding whose public transport value is the existing
  typed `SkillInput`; the SDK receives the held descriptor path, never an
  unchecked plugin pathname.
- **Modify — detached boundary:** `supervisor.py` parses the queued capsule and
  route into existing `PluginRequirement` and `PluginCapabilitySnapshot`
  records, rereads the standard inherited Codex home, and requires exact ordered
  requirement, snapshot and capability-digest equality before it writes the
  private worker capability or calls `Popen`. It serializes those same
  secretless closed facts into the attempt capability; no token, manifest body
  or environment value is added. `worker.py` cross-checks the private capability
  against the capsule, reloads the same native profile/home, rereads the exact
  plugin bundle through the descriptor authority immediately before
  `CodexSdkAdapter.start_thread`, and keeps the verified skill binding alive
  through the bounded turn. Any mismatch exits through the existing capability
  or integrity failure path before SDK identity. Recovery continuations repeat
  the complete check; they cannot reuse a prior descriptor or snapshot.
- **Modify — SDK adapter:** `codex_sdk.py` accepts only ordinary `str` or typed
  `SkillInput`. `_wire_input` never recognizes JSON, a prefix or any other magic
  inside a string, removes the legacy bundled-skill marker branch, and forwards
  the typed skill name/held descriptor path only when the installed SDK exposes
  `SkillInput`.
- **Preserve:** `config.py`, `native_profile.py`, `plan_capsule.py`, the SQLite
  v15 ledger, live-control and controller-recovery public/persisted contracts,
  IPC/service/control clients, CLI and every TUI module/dependency/rendered
  artifact remain byte-protected. H6-I source and rendered outcomes are not
  repaired or regenerated here. Plugin bundles, caches, auth/trust/config,
  global Codex/App state, accepted evidence and unrelated dirty bytes remain
  read-only.

Dependency direction is `domain bounded schema -> contracts -> plugin
capability authority -> projection/controller and supervisor/worker -> SDK
adapter`. `supervisor.py` and `worker.py` may import the contracts and plugin
authority; the plugin module may import only domain/contracts and standard
library code. It may not import controller, supervisor, worker, ledger, IPC,
service, CLI or TUI. Neither the SDK adapter nor prompt projection discovers
plugins. SQLite remains the sole dispatch authority; the private attempt file
is a capability-bound serialization copy, not a new state authority.

State and error boundaries are closed. Enqueue binds typed requirement and
snapshot facts. Supervisor launch authorizes those facts against a fresh
descriptor read. Worker start authorizes them again and owns the live
descriptor. Only then may adapter thread creation occur. Missing, disabled,
setup-required, unsupported, ambiguous, oversized, symlinked, hardlinked,
renamed, reordered or byte-drifted inputs terminate before SDK identity through
the existing sanitized capability/integrity classification. Provider or SDK
failures after identity remain the existing worker lifecycle authority; plugin
code never rewrites them. V1 always follows the plain-string path and cannot
acquire capability from marker-shaped user text.

Acceptance modes are `objective` and `architecture`. The implementation and
objective/code review route is Luna XHigh using the app-configured fast speed;
the independent architecture-conformance route is Sol Medium. The repair is
provider-free: it must not run or retry the consumed delayed-steer sentinel,
the rejected bundled-skill provider attempt or any connector/MCP/provider
sentinel.

Promotion requires all of the following on one exact candidate:

- a Draft 2020-12 oracle plus the production parser prove a closed v1 payload
  without `plugin_requirements` is valid, v1 with that field is rejected, v2
  without it or with an empty array is rejected, and a closed non-empty v2 is
  valid; static/generated schema bytes remain equal and the bounded production
  schema validator accepts the exact two-branch schema;
- marker-shaped v1 and v2 prompt strings remain byte-identical ordinary SDK
  strings; the removed marker helpers/exports and adapter parser are absent,
  while a valid v2 path reaches the fake SDK as an actual `SkillInput`;
- direct-controller and detached-supervisor/worker adversaries mutate each of
  requirement, snapshot, capability digest, marketplace/source identity,
  manifest, bundle byte and skill directory before SDK creation and observe
  zero `start_thread`/`run_turn` calls; recovery continuation repeats the same
  rejection;
- real-layout provider-free fixtures cover flat, standard-cache and local
  marketplace discovery plus missing/disabled/setup/App-only, ambiguity,
  secret-negative, total-entry, bundle-count, depth, file-count, single-file,
  total-byte, symlink, hardlink, FIFO and descriptor rename/swap cases;
- the exact frozen H6-G API/state behavior and all H6-I production/rendered
  bytes are unchanged; no App, plugin, auth, trust, config or global mutation
  occurs;
- focused schema/projection/discovery/adapter/controller/supervisor-worker
  discriminators, contracts/controller/workers/integrations/workflow-assets
  partitions, full `make check`, exact-wheel build/install/import and a
  provider-free temporary-service restart proof pass; validator, Ruff,
  compileall, pre-commit and `git diff --check` pass;
- the one sanitized evidence record is refreshed with exact candidate, plan,
  capsule and wheel identities, negative SDK-call counts and protected H6-G/H6-I
  hashes; complete self-review reports P0=0/P1=0; and one fresh independent Luna
  XHigh objective review and one fresh Sol Medium architecture review both
  return P0=0/P1=0 before promotion.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Close plugin capability authority so schema-v1 text can never select an SDK skill and "
        "one explicit schema-v2 bundled-skill requirement is revalidated by the detached worker "
        "through a held no-follow descriptor before SDK identity."
    ),
    decomposition=(
        "Make the generated/static capsule schema a bounded closed v1-or-v2 contract and remove every prompt marker authority.",
        "Replace path-based plugin discovery with canonical marketplace identity, complete traversal bounds and descriptor-held bundle/skill reads.",
        "Carry exact typed requirement and snapshot facts through queue, supervisor capability and worker revalidation into one typed SDK SkillInput.",
        "Prove direct and detached pre-SDK drift rejection, v1 compatibility, exact-wheel behavior and preserved H6-G/H6-I bytes before independent reviews.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Closed historical schema-v1 capsules omit plugin_requirements and always execute their exact prompt as plain text; closed schema-v2 capsules require one explicit non-empty typed requirement array.",
        "Only one requirement with exactly one bundled skill can become a typed SkillInput; prompt markers, ambient plugins, multiple skills and connector or MCP requirements cannot create SDK authority.",
        "Standard-cache and local-marketplace bundles retain exact name@marketplace identity and source under finite total, depth, bundle, file and byte bounds.",
        "Supervisor and worker independently cross-check the exact queued requirement, capability snapshot and bundle immediately before SDK thread creation using identity-checked O_NOFOLLOW descriptors held through input consumption.",
        "Every drift, race, unsafe file type, missing readiness or closed-shape violation fails before SDK identity without plugin, App, auth, trust, config, controller-state or global mutation.",
        "Focused adversaries, affected partitions, full make check, exact-wheel and provider-free restart gates pass with preserved H6-G/H6-I bytes, sanitized evidence and self-review P0/P1 zero.",
        "Fresh independent Luna XHigh objective and Sol Medium architecture reviews approve the same exact candidate with P0/P1 zero before H6-H promotion or H6-J execution.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/contracts.py",
        "src/codex_flow/projection.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/plugin_capabilities.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "schemas/capsule.schema.json",
        "scripts/validate.py",
        "tests/test_model_facing_projection.py",
        "tests/test_controller_execution.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_plan_compilation.py",
        "tests/test_shared_sdk_visibility.py",
        "tests/test_supervisor_recovery.py",
        "tests/test_workflow_assets.py",
        "docs/reviews/evidence/plugin-capability-parity.json",
    ),
    protected_surfaces=(
        "AGENTS.md and docs/reviews/peer-thread-workflow.md",
        "src/codex_flow/config.py, native_profile.py and plan_capsule.py",
        "src/codex_flow/ledger.py, ipc.py, service.py, control_client.py, controller_recovery.py and app_native.py",
        "src/codex_flow/cli.py, tui.py, tui_client.py and tui_models.py",
        "pyproject.toml, uv.lock, tests/test_live_worker_control.py and tests/test_workflow_control.py",
        "docs/reviews/evidence/human-terminal-ui and docs/reviews/evidence/human-terminal-ui.json",
        "all other tests, schemas, plugins, skills, templates, packaging and accepted evidence",
        "plugin bundles, caches, trust/auth state, global Codex/App state, the consumed provider sentinel, H6-J and unrelated dirty bytes",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only plugin-capability-authority-closure in the existing dirty "
        "python-sdk-controller worktree as the single Luna XHigh mutable owner at the app-configured "
        "fast speed. Modify only the exact eighteen surfaces. Do not add artifacts or touch the frozen "
        "H6-G API, H6-I source/renders, CLI, packaging, plugin/global/App state or H6-J. Implement the "
        "closed v1/v2 schema, remove all prompt marker authority, replace discovery with the frozen "
        "bounded descriptor design, and make supervisor plus worker revalidate exact typed facts before "
        "one held-descriptor SkillInput reaches the SDK. Run every named provider-free, partition, full, "
        "wheel and protected-byte gate; refresh only the sanitized parity evidence, self-review P0/P1 and "
        "return one terminal callback with independent reviews pending. Do not run any provider sentinel."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

Plugin-capability authority closure is promoted at exact plan SHA-256
`b80bbc29615c3d317d6c4ce4c30b41061c5528541378aed42c6b64c5e2d90355`,
capsule source-block SHA-256
`f84681a55520d45aeefe38ef15a8d170e81ed7eae67d9f3772fe85b55b970818`,
17-path non-evidence candidate SHA-256
`1ebba0ef7379b49c532ecac543839a9588df652c63d84e94cd7a2da48137f375`,
evidence SHA-256
`70cdb7f615b8907166a5b37b10cdcad94b5a2493eb1721a5838e4a34beb65ef3`
and reproducible exact-candidate wheel SHA-256
`1127163f04fd8544f3cc0fd9a5a4a76c8ea8b42323daa128694501b694fb036f`
(325341 bytes). The one bounded repair moved supervisor verification before
all attempt/state/file derivation and made worker startup/result submission use
one exact closed capability parser. Fresh independent Luna XHigh objective and
Sol Medium architecture reviews both returned `APPROVED`, P0=0/P1=0; 654 tests,
exact-wheel isolated install and provider-free restart evidence are green. No
provider or App call was part of promotion.

The objective reviewer recorded one non-blocking P2 for a future bounded
`plugin-requirement-schema-parity` follow-up: the static requirement schema is
looser than the typed parser for canonical-id syntax/length and empty bundled
skill arrays. Runtime parsing and execution fail closed, so this does not block
H6-J and is not authority to change H6-H bytes during the H6-I repair.

Appending the next bounded repair changes the current canonical plan identity.
The promoted H6-H record above remains a closed historical identity. The
plan-bound candidate/evidence identities previously recorded for H6-I are not
promotable; its source and four rendered sentinels may be carried only if the
final independent visual authority rechecks their exact bytes with the final
post-repair candidate. H6-J remains locked until H6-I promotes.

## Next execution — human-terminal-ui

Legacy plan label: H6-I. Outcome: a human can observe runs and workers and issue
bounded control or controller-decision actions from a terminal without keeping
the Codex App or any model turn open.

The implementation architecture is frozen:

- `tui_client.py` is a thin async façade over the already frozen
  `LiveWorkerControlClient` and `ControllerDecisionClient`. It owns connection,
  bounded refresh-on-explicit-event, last-snapshot offline fallback and typed
  command results; it never imports or reads SQLite and does not poll after UI
  exit.
- `tui_models.py` owns immutable presentation-only view models derived from
  typed control API models: queue/dependency state, dispatch/generation/attempt,
  SDK thread/turn, route, elapsed/lease/activity, bounded recent diagnostics,
  retry state, pending decisions and terminal evidence. It creates no workflow
  or plugin capability authority.
- `tui.py` owns the Textual application, screens, widgets, keyboard bindings,
  command palette, confirmation dialogs and deterministic light/dark/no-color
  plus 80x24/120x40 rendering. Framework state stays behind those view models;
  closing or crashing the TUI has no lifecycle side effect.
- `cli.py` owns only registration of `codex-flow tui` and safe exact-argument
  launch/print handling for `codex resume <thread-id>`. The selected id is
  validated and passed as an argv element without shell interpolation. Resume
  is a transcript handoff, never authority over another live `TurnHandle`.
- `pyproject.toml` and `uv.lock` pin Textual as the sole new runtime dependency.
  Existing workflow-control and live-worker test modules own deterministic
  headless-driver, action-binding, offline, keyboard and packaging coverage.

Dependency direction is `domain/control_client -> tui_client -> tui_models ->
tui -> cli registration`; Textual never flows into ledger, supervisor, SDK,
controller recovery or plugin capability code. New-artifact budget is exactly
eight files: three semantic TUI modules, four fixed rendered SVG sentinels and
one sanitized evidence record. Existing test modules absorb coverage; no new
test module, schema, service, runner, registry or alternate entrypoint is
allowed.

Non-goals are embedding a model; cloning the Codex composer or transcript;
replacing App transcripts; owning or launching workers; direct database writes;
background polling after exit; remote/multi-user administration; displaying or
mutating plugin readiness; or changing any H6-G control contract. Offline mode
is read-only and truthful; every mutation requires an authenticated live
supervisor connection.

Acceptance modes are `objective` and `visual`. Sol Medium is the render-aware
implementation owner. Luna XHigh at the app's configured fast speed is the
independent objective/code authority. Because the fixed full-size sentinel
inspection is this milestone's final qualitative promotion review, the
repository default selects one independent Sol High visual authority. No
provider call is authorized: the delayed-worker/service exercise is
production-shaped and provider-free, and the consumed delayed-steer sentinel
is not retried.

Promotion requires a real detached provider-free worker/service that remains
observable and steerable through the TUI while App calls remain zero; exact
identity binding and confirmation for steer, interrupt, checkpoint re-arm,
human claim and acknowledge; TUI close/reopen and crash with no lifecycle or
duplicate effect; authenticated-live versus offline behavior; safe exact SDK
resume argv; deterministic keyboard-only navigation, labels and feedback;
full-size independent inspection of the four fixed SVG sentinels; focused
headless-driver tests; affected workers/integrations partitions; full
`make check` because CLI/packaging/dependencies change; exact-wheel entrypoint,
validator, Ruff, compile, diff and protected hashes; one sanitized evidence
record; self-review P0=0/P1=0; and fresh Luna XHigh objective plus Sol High
visual approvals of one exact candidate with P0=0/P1=0.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Deliver a deterministic accessible terminal UI over the frozen worker-control and "
        "controller-decision APIs without becoming lifecycle, transcript or database authority."
    ),
    decomposition=(
        "Add typed presentation models and one async TUI client over the existing authenticated control clients.",
        "Build the Textual status, activity, control and human-decision surface with explicit confirmation and safe SDK transcript handoff.",
        "Prove offline and close/crash isolation, exact action identity, keyboard accessibility and deterministic bounded rendering.",
        "Freeze four full-size visual sentinels, exact-wheel behavior and provider-free detached-service evidence for independent promotion reviews.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.VISUAL),
    acceptance_criteria=(
        "Every displayed fact derives from the typed control clients and every mutation binds the exact visible dispatch, generation, attempt, turn or decision revision with truthful confirmation feedback.",
        "A provider-free detached worker remains observable and steerable with zero App calls; TUI close, crash and reopen neither interrupt, duplicate nor own work.",
        "Offline mode is a truthful read-only last snapshot and rejects mutation until an authenticated live supervisor connection returns.",
        "The SDK transcript handoff validates one selected thread id and executes or prints exact argv without shell interpolation or claiming live-turn authority.",
        "Keyboard-only navigation and deterministic light, dark, no-color, 80x24 and 120x40 states remain legible, labelled and bounded in the four fixed rendered sentinels.",
        "Focused headless-driver tests, affected partitions, full make check, exact-wheel entrypoint, validator, Ruff, compile, diff and protected-hash gates pass; sanitized evidence and self-review report P0/P1 zero.",
        "Fresh independent Luna XHigh objective and Sol High visual reviews approve the same exact candidate with P0/P1 zero before H6-J can consume it.",
    ),
    mutable_surfaces=(
        "src/codex_flow/tui.py",
        "src/codex_flow/tui_client.py",
        "src/codex_flow/tui_models.py",
        "src/codex_flow/cli.py",
        "pyproject.toml",
        "uv.lock",
        "tests/test_live_worker_control.py",
        "tests/test_workflow_control.py",
        "docs/reviews/evidence/human-terminal-ui/light-wide.svg",
        "docs/reviews/evidence/human-terminal-ui/dark-wide.svg",
        "docs/reviews/evidence/human-terminal-ui/light-narrow.svg",
        "docs/reviews/evidence/human-terminal-ui/no-color-narrow.svg",
        "docs/reviews/evidence/human-terminal-ui.json",
    ),
    protected_surfaces=(
        "AGENTS.md and docs/reviews/peer-thread-workflow.md",
        "src/codex_flow/domain.py, contracts.py, config.py, native_profile.py, plan_capsule.py, projection.py, controller.py and backends/codex_sdk.py",
        "src/codex_flow/ledger.py, supervisor.py, worker.py, ipc.py, service.py, control_client.py, controller_recovery.py and app_native.py",
        "src/codex_flow/plugin_capabilities.py and docs/reviews/evidence/plugin-capability-parity.json",
        "schemas/capsule.schema.json, scripts/validate.py and tests/test_workflow_assets.py",
        "tests/test_model_facing_projection.py, tests/test_controller_execution.py, tests/test_codex_sdk_adapter.py, tests/test_plan_compilation.py and tests/test_shared_sdk_visibility.py",
        "plugin bundles, caches, trust/auth state, global Codex configuration, accepted evidence and unrelated dirty bytes",
        "the consumed external-blocked delayed-steer sentinel and H6-J integrated promotion",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.VISUAL, RoleId("visual-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only human-terminal-ui in the existing dirty python-sdk-controller worktree as the single Sol Medium render-aware mutable owner. Implement the frozen eight-artifact Textual architecture over the existing typed control clients. Own only TUI modules, CLI registration, Textual dependency/lock, two existing test modules, four rendered sentinels and the evidence record; do not touch contracts, config, native profile, controller, SDK, ledger, supervisor, plugin parity or global/App state. Run provider-free detached-service and headless-driver gates, inspect deterministic renders during iteration, retain sanitized evidence and return one terminal callback with independent Luna objective and Sol High visual reviews pending. Do not retry the consumed provider sentinel."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

This capsule is ready and independent of `plugin-capability-parity`. It owns
`cli.py`, packaging/dependency locks and TUI artifacts exclusively; it consumes
only frozen H6-G typed control APIs and does not consume H6-H plugin facts.

The implementation candidate from that capsule is rejected and no longer
executable. Its 12-path non-evidence identity was
`df3e0c16c7b93c3ff8b4483906fa1c4df7e9247cada42a7c5c7da8d37aaa6e32`,
its evidence record currently hashes
`6c4df36da4e34caac6c15198453d505332fb16a9ff1bd060dc27e5f48edd92e8`,
and its retained wheel is
`dist/human-terminal-ui/codex_flow-0.2.0-py3-none-any.whl`, SHA-256
`eeb1d2355448a6a5270508206b885c112c3f3181164578ff8f76e609490cc342`
(322793 bytes). The fresh independent Luna XHigh objective review returned
`DO_NOT_PROMOTE`, P0=0/P1=2: decision-selected resume could execute without a
typed inactive-turn proof, and ambiguous steer/interrupt transport failures
were labelled rejected while a retry could allocate a new command id after the
supervisor had already committed the first. The fixed SVGs remain usable visual
evidence, not objective correctness proof.

## Next execution — human-terminal-control-correctness-closure

Legacy plan label: H6-I bounded repair. Outcome: preserve the accepted terminal
UI and rendered result while making transcript open and live control truthful:
a decision source thread opens only with a current typed inactive-turn proof,
and every steer/interrupt owns one caller-retained idempotent command identity
that is durably reconciled before any retry.

The repair architecture is frozen and adds no artifact:

- **Create:** nothing. The cumulative H6-I artifact budget remains the existing
  three TUI modules, four fixed SVG sentinels and one evidence record. No new
  module, schema, test file, database table, durable record, service, runner,
  registry, command or entrypoint is permitted.
- **Modify — resume proof:** `tui_client.py` owns one private lookup over the
  latest connected typed snapshot. For a selected worker, the existing
  `LiveWorkerStatus.thread_id` plus `active_turn_id is None` is the sole inactive
  proof. For a selected decision, `ControllerDecisionSummary.source_thread_id`
  must match exactly one current `LiveWorkerStatus.thread_id` whose
  `active_turn_id` is `None`; zero matches, multiple matches, an active match,
  an offline/stale snapshot or a changed decision revision fails closed.
  `tui.py` asks this client boundary before `action_open_resume` calls the
  existing exact-argv resume handler. It never infers inactivity from decision
  state, missing data or App visibility. Copying an inert command string remains
  non-executing; only Open crosses the inactive-turn gate.
- **Modify — canonical command API:** `ControlCommand` and the existing ledger
  row remain the only durable command model and state authority. `supervisor.py`
  adds one closed read-only `control_status` IPC operation taking exactly one
  bounded `command_id` and returning either the existing canonical command or
  explicit `None`; it performs no mutation and exposes no list/poll loop.
  `control_client.py` adds the matching typed `command_status(command_id)`
  decoder and two error classes that distinguish an explicit supervisor
  rejection from an ambiguous post-send transport failure. No ledger/domain
  schema or command creation semantics change.
- **Modify — retained identity and reconciliation:** `tui_client.py` generates
  and retains one command id before the first steer/interrupt send, keyed by the
  exact dispatch, generation, attempt, thread, turn, kind and payload digest,
  then always supplies it to `LiveWorkerControlClient`. An explicit negative
  response becomes `rejected`. A transport exception becomes
  `post_send_uncertain`, never rejected: before any resend the client queries
  `control_status` with the same retained id. An exact matching durable command
  returns its canonical state without resending; a different command under the
  id is an integrity failure; explicit absence permits at most one resend with
  the same id; an unavailable or ambiguous reconciliation returns
  `post_send_uncertain` and preserves the id for the next explicit user action.
  No path allocates a new id while that semantic request is unresolved.
- **Modify — truthful UI result:** `TerminalUiCommandResult` gains one closed
  presentation outcome (`accepted`, `rejected` or `post_send_uncertain`) without
  becoming a second command state model. `tui.py` renders those outcomes
  distinctly and never labels uncertainty rejected or reports success before
  the canonical command is observed. Refresh remains explicit/event-driven;
  reconciliation is a single command-id read, not polling.
- **Modify — tests/evidence:** the existing live-control and workflow-control
  modules own all new adversaries. `test_plan_compilation.py` updates only the
  active plan identity/capsule ownership assertion made stale by this planning
  change. `human-terminal-ui.json` is the sole refreshed record. The four SVGs,
  `tui_models.py`, CLI, dependency files and packaging remain byte-preserved.

Dependency direction is unchanged except for the already accepted control API
extension: `domain ControlCommand/LiveWorkerStatus -> ledger/supervisor
read-only status -> control_client -> tui_client -> tui`. TUI code never imports
ledger, IPC internals or SDK/App code. The supervisor does not retain UI state,
and `control_status` never creates, sends, acknowledges, rejects or retries a
command. Resume proof uses only current typed control-client facts; no App poll,
direct SQLite read, worker handle or lifecycle ownership is introduced.

State and error transitions are exact. Before send, the caller owns `command_id`
and exact semantic facts. A decoded positive response is accepted. A decoded
negative response is rejected. A missing/closed transport response is
post-send uncertain until `control_status(command_id)` proves exact presence or
explicit absence. Exact presence returns the durable `pending`, `sent`,
`acknowledged`, `rejected` or `unresolved` state without resend. Explicit
absence authorizes one same-id resend. Any status mismatch or second ambiguous
transport remains fail-closed and cannot create another command. Existing
ledger uniqueness, exact dispatch/generation/attempt/thread/turn binding,
supervisor commit-before-reply behavior and worker acknowledgement authority
remain unchanged.

Acceptance modes remain `objective` and `visual`. Sol Medium is the repair
implementation owner because it modifies terminal feedback behavior. Luna
XHigh at the app-configured fast speed is the independent objective/code
authority. One independent Sol High visual authority must recheck the exact
final TUI bytes and the four full-size sentinels. If the repair changes a
baseline rendered state, implementation must stop for a bounded plan update
before regenerating any SVG; no silent visual rebaseline is allowed.

Promotion requires all of the following on one exact candidate:

- decision-selected Open is rejected for offline/stale data, no matching source
  thread, multiple matches and an active match, while one exact connected typed
  inactive match passes the unchanged validated argv element to the resume
  handler; no App, database or SDK read occurs;
- steer and interrupt generate their id before send and preserve it across a
  reply-loss-after-commit discriminator; exact durable reconciliation observes
  one command and zero duplicate rows/worker applications, while command-id
  substitution or semantic mismatch fails closed;
- an explicit supervisor rejection renders `rejected`; a lost response plus
  failed reconciliation renders `post_send_uncertain`; explicit durable absence
  permits one resend with the same id and never a new id; repeated ambiguity
  creates no second command;
- the real provider-free Supervisor UNIX-socket path proves commit-before-lost-
  reply reconciliation, same-id replay and terminal acknowledgement through the
  canonical `ControlCommand`; close/crash/offline tests retain zero lifecycle or
  duplicate effect and provider/App call counts remain zero;
- focused TUI/control API/IPC/headless-driver tests, workers and integrations
  partitions, full `make check`, exact-wheel build/install/CLI and provider-free
  supervisor proof, validator, Ruff, compileall, pre-commit and diff hygiene
  pass; the H6-G/H6-H production authority and all protected H6-I hashes match;
- the four SVGs remain byte-identical and receive a fresh full-size Sol High
  visual recheck unless a prior bounded plan update explicitly authorizes a
  deterministic rerender; the one sanitized evidence record and complete
  self-review report P0/P1=0; and fresh independent Luna XHigh objective plus
  Sol High visual reviews approve the same exact candidate before H6-J starts.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Close terminal control correctness so transcript open requires typed inactive-turn proof "
        "and every steer or interrupt retains one command identity through durable reconciliation."
    ),
    decomposition=(
        "Gate decision-selected transcript open on an exact connected LiveWorkerStatus match with no active turn.",
        "Expose one read-only command-id status query over the canonical supervisor and ControlCommand authority.",
        "Retain one caller-generated command id across send ambiguity, exact reconciliation and any single safe same-id resend.",
        "Prove truthful rejected-versus-uncertain feedback, zero duplicates, unchanged renders and exact-wheel provider-free behavior before independent reviews.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.VISUAL),
    acceptance_criteria=(
        "Decision-selected resume Open fails closed without exactly one current typed source-thread match proving active_turn_id is absent; it uses no App poll, direct database read or lifecycle authority.",
        "Steer and interrupt allocate one caller-retained id before send and never allocate another while the exact semantic request is unresolved.",
        "Explicit rejection, exact durable command state and post-send uncertainty are distinct; ambiguous transport is reconciled by the retained id before any same-id retry.",
        "Lost replies, command substitution, repeated ambiguity, stale turn identity and close/crash adversaries produce at most one durable command and one worker application.",
        "The existing ControlCommand, ledger and supervisor remain sole command authority; H6-G/H6-H behavior, TUI lifecycle isolation and provider/App call counts remain unchanged.",
        "Focused tests, affected partitions, full make check, exact-wheel provider-free supervisor, validator/Ruff/compile/diff and protected-hash gates pass with sanitized evidence and self-review P0/P1 zero.",
        "The unchanged four full-size SVGs receive a fresh Sol High visual recheck, and independent Luna XHigh objective plus Sol High visual reviews approve the same exact candidate before H6-J starts.",
    ),
    mutable_surfaces=(
        "src/codex_flow/control_client.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/tui_client.py",
        "src/codex_flow/tui.py",
        "tests/test_live_worker_control.py",
        "tests/test_workflow_control.py",
        "tests/test_plan_compilation.py",
        "docs/reviews/evidence/human-terminal-ui.json",
    ),
    protected_surfaces=(
        "AGENTS.md and docs/reviews/peer-thread-workflow.md",
        "src/codex_flow/domain.py, contracts.py, ledger.py, worker.py, ipc.py, service.py and controller_recovery.py",
        "src/codex_flow/config.py, native_profile.py, plan_capsule.py, projection.py, controller.py and backends/codex_sdk.py",
        "src/codex_flow/plugin_capabilities.py and docs/reviews/evidence/plugin-capability-parity.json",
        "src/codex_flow/tui_models.py, cli.py, pyproject.toml and uv.lock",
        "docs/reviews/evidence/human-terminal-ui/light-wide.svg, dark-wide.svg, light-narrow.svg and no-color-narrow.svg",
        "all other tests, schemas, plugins, skills, templates, packaging, accepted evidence and unrelated dirty bytes",
        "plugin/auth/global Codex/App state, the consumed provider sentinel, deferred plugin schema parity and H6-J",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.VISUAL, RoleId("visual-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only human-terminal-control-correctness-closure in the existing "
        "dirty python-sdk-controller worktree as the single Sol Medium mutable owner. Modify only the "
        "exact eight surfaces. Add no artifact and do not touch domain, ledger, worker, IPC, CLI, "
        "packaging, plugin authority, SVGs, App/global state or H6-J. Implement one read-only canonical "
        "command-id status query, caller-retained same-id reconciliation with truthful rejected versus "
        "post-send-uncertain outcomes, and typed inactive-turn proof for decision-selected Open. Run all "
        "named provider-free, partition, full, wheel and protected-byte gates; refresh only the H6-I "
        "evidence, self-review P0/P1 and return one terminal callback with independent objective and "
        "visual reviews pending. Do not run any provider sentinel or silently rerender visual evidence."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

This repair is the sole ready execution milestone. H6-J remains locked. The
deferred plugin schema-parity P2 is a later bounded follow-up and does not enter
this owner.

### Human-terminal-ui promotion

H6-I is promoted on exact plan
`ee92d605dbc453cdf46d0062bea38c59e3626b4348d08e508c87b329b3bf3875`,
capsule `65fb555193f21df60bbace155f945605e7767b2e8ba7e0c4a499773f8a519578`,
candidate `7da3f1e6491470da73ea5ff05870d52ce8fe458a88b817e0d3a9bcd84f52f7e0`
and evidence
`2e6cf6d2c480497919f9a6852666a6bcb9309a5a97aee7a2fa2c2cd4e748efcb`.
The retained wheel is
`dist/human-terminal-ui/codex_flow-0.2.0-py3-none-any.whl`, 328062 bytes,
SHA-256 `1d6eaa14c6f6c5e56f110950eb64d8e0f7513cdfb19d7243defdd058d66beea2`.
Independent Luna XHigh objective review and the internal Sol High visual
fallback both approved the exact candidate with P0=0/P1=0/P2=0. The unchanged
four fixed SVGs are the accepted visual evidence. Native visual task
`01a05307-7c3c-7352-becb-053c96cfda6d` remains suspended on an unnecessary
approval and is neither an outstanding authority nor retry authorization; it
must not be approved, retried, duplicated or mutated. H6-J may now consume the
promoted H6-G, H6-H and H6-I authorities.

## Next execution — integrated-control

Legacy plan label: H6-J. Outcome: prove through one exact retained-wheel and
temporary-service candidate that detached execution, live control, controller
recovery, required plugin capability and the human terminal UI operate together
without App lifecycle authority. The provider-free integrated path is ready;
final promotion also requires one real bundled-plugin/provider sentinel and is
therefore `EXTERNAL_BLOCKED` until its external prerequisite exists.

### Frozen architecture and ownership

One integrated verification owner controls exactly five mutable surfaces:

- **Modify `tests/test_production_pilots.py`:** own the sole provider-free
  integrated temporary-service harness, disposable repositories, strict plugin
  fixture, typed fake SDK seam, fault injection, cleanup assertions and fixed
  H6-I visual hash assertions. This test module may consume production APIs but
  may not add a second controller, ledger, transport, plugin-discovery authority
  or UI lifecycle path.
- **Modify `tests/test_plan_compilation.py`:** assert this exact active capsule,
  ordered ownership and frozen plan revision. It owns no behavior policy.
- **Modify `docs/reviews/codex-controller-compatibility.md`:** record the
  integrated controller/service/recovery observations, negative controller-token
  facts, optional App transcript status and cleanup boundary.
- **Modify `docs/reviews/codex-sdk-compatibility.md`:** record exact-wheel SDK,
  plugin-discovery, connector-readiness and real-sentinel prerequisite facts
  without claiming a provider call that did not occur.
- **Create `docs/reviews/evidence/integrated-control.json`:** the one sanitized,
  closed evidence record for candidate, wheel, protected hashes, provider-free
  gates, real-sentinel status and final promotion status. This is the only new
  artifact; new-artifact budget is exactly one.

All production modules are **preserve**: `domain.py`, `contracts.py`,
`ledger.py`, `controller.py`, `controller_recovery.py`, `control_client.py`,
`supervisor.py`, `service.py`, `worker.py`, `ipc.py`, `tui.py`, `tui_client.py`,
`tui_models.py`, `plugin_capabilities.py`, `cli.py`, `native_profile.py`,
`plan_capsule.py`, `projection.py`, `config.py` and `backends/codex_sdk.py`.
Packaging, schemas, plugin/skill/template sources and production entrypoints are
also preserve. **Create** applies only to the evidence record; **remove** is
empty. Any observed production defect stops this owner and returns a separate
bounded repair capsule rather than changing a production or promoted focused-
test surface.

Dependency direction is retained wheel/service entrypoint -> canonical
controller, supervisor and typed control client -> SQLite ledger/recovery and
shared SDK adapter, with the TUI and strict plugin discovery as typed consumers.
The test harness observes these authorities through public or existing focused
test seams only. SQLite remains sole durable state authority; the supervisor is
sole detached lifecycle owner; `CodexSdkAdapter` is sole provider transport;
`plugin_capabilities.py` is sole plugin-discovery authority; the TUI owns no
lifecycle, ledger or SDK reads. Serialization remains in the existing typed
contracts and the evidence record is observation, not runtime state.

The integrated state sequence is: create disposable roots -> start exact
retained-wheel service -> dispatch dependency-aware workers -> observe bounded
activity -> apply one exact behavior-changing steer and one exact interrupt ->
inject controller failure before decision commit and after commit/before
acknowledgement -> restart -> recover or human-claim exactly one action ->
verify zero duplicate successor -> stop service -> prove cleanup and immutable
protected bytes. Replay, stale identity, permission drift, plugin drift,
ambiguous recovery or cleanup residue fails closed. The activity ring is
diagnostic and bounded; it never becomes state authority. Optional App
transcript visibility is recorded as `not_observed` when not externally
supplied and never causes App polling.

The provider-free strict plugin fixture and typed fake SDK prove mechanism only.
They cannot satisfy the real bundled-plugin sentinel. Current standard-home
discovery is blocked because the shared cache contains symlinked bundles and
strict discovery fails before a selected ready plugin can be classified; no
provider call is authorized during planning. The smallest prerequisite is an
externally supplied standard shared Codex home/environment with one already
installed, enabled and ready required bundled plugin that passes strict
no-symlink discovery, plus explicit authorization for exactly one bounded real
provider attempt. The program must not mutate plugin installation, enablement,
trust, authentication, caches or global Codex/App state, and must not substitute
a private `CODEX_HOME`, fake plugin or catalog presence.

### Milestone DAG, gates and closure

The serial edge from H6-G/H6-H/H6-I to this milestone is an acceptance
dependency on their frozen state, plugin and human-control authorities. Inside
H6-J, provider-free integration is ready and independently closable as evidence;
the real sentinel follows only after the external prerequisite. Objective and
architecture reviews follow one exact candidate only after both integration
branches pass. There is no mutable fan-out because the pilot, compatibility
notes and evidence share one candidate identity. This is the final program
milestone; no later milestone may consume a promoted result until it closes.

Acceptance modes are `objective` and `architecture`. Promotion requires:

- exact retained-wheel/service execution with zero App API/task dependency,
  at least two dependency-aware workers and the 0/1/many-worker matrix;
- a bounded activity ring, one behavior-changing exact-turn steer, one exact
  interrupt and zero controller-model tokens while workers merely run;
- failure before decision commit and after commit/before acknowledgement,
  followed after restart by exactly one recovered or human-claimed action and
  no duplicate action, successor, notification or worker application;
- provider-free strict-plugin and typed-SDK mechanism proof, truthful connector
  readiness without invoking MCP/connectors, plus one later real bounded
  bundled-plugin/provider sentinel using the external prerequisite;
- replay, stale identity, ring count/byte bounds, permission/plugin drift,
  secret-negative, 0/1/many-worker, service restart and cleanup adversaries;
- stopped service, no child process/socket/lease, no temporary plugin/worktree/
  controller state outside disposable roots, and byte-identical retained wheel,
  promoted evidence, H6-I SVGs and protected checkout surfaces;
- focused pilot and plan tests, affected partitions, full `make check`, exact
  retained-wheel/service proof, validator, Ruff, compileall, pre-commit and diff
  hygiene, with complete self-review P0/P1=0;
- a closed `integrated-control.json` whose provider-free status is
  `passed`/`failed`, real sentinel status is `external_blocked`/`passed`, and
  final status remains `external_blocked` until the real sentinel passes; and
- fresh independent Luna XHigh objective and Sol Medium architecture reviews
  approve the same exact final candidate with P0=0/P1=0.

No line, file, duration or test-count proxy may weaken the observable outcome,
isolation, recovery, exact-wheel or independent-proof requirements. If a hard
external constraint conflicts with them, use proof-preserving replacement or
return bounded replan. Residual risk is explicit: the provider-free branch can
complete now, but H6-J and the program cannot promote while the real sentinel
is `external_blocked`.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Prove one exact retained-wheel service integrates detached execution, live control, "
        "controller recovery, required plugin capability and the human TUI without App lifecycle authority."
    ),
    decomposition=(
        "Run one disposable provider-free retained-wheel service across dependency-aware 0/1/many-worker cases, bounded activity, exact steer and interrupt.",
        "Inject controller failures on both sides of decision commit and prove restart yields exactly one recovered or human-claimed action with no duplicate successor.",
        "Exercise strict plugin and typed SDK mechanisms provider-free, classify connector and optional App visibility truthfully, and preserve promoted visual and runtime bytes.",
        "Close sanitized evidence and all deterministic gates, while retaining external_blocked until one authorized real bundled-plugin sentinel can pass.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "The exact retained-wheel service proves detached dependency-aware execution, bounded live activity, one behavior-changing exact steer, one exact interrupt and zero idle controller-model tokens without App lifecycle calls.",
        "Before-commit and after-commit-before-ack failures recover or human-claim exactly one action after restart and create no duplicate action, successor, notification or worker application.",
        "Provider-free strict-plugin and typed-SDK fixtures prove mechanisms only; connector readiness and optional App visibility are truthful and cause no connector or App polling.",
        "Replay, stale identity, ring bounds, permission/plugin drift, secret-negative, worker-count and cleanup adversaries fail closed with no state outside disposable roots.",
        "The retained wheel, promoted H6-G/H6-H/H6-I evidence, four fixed SVGs and every protected production surface remain byte-identical.",
        "Focused and affected tests, full make check, exact-wheel/service, validator, Ruff, compile, pre-commit and diff gates pass with sanitized evidence and self-review P0/P1 zero.",
        "Final status remains external_blocked until an externally ready strict bundled plugin and authorization permit one real provider attempt that passes.",
        "Independent Luna XHigh objective and Sol Medium architecture reviews approve the same final candidate with P0/P1 zero before program promotion.",
    ),
    mutable_surfaces=(
        "tests/test_production_pilots.py",
        "tests/test_plan_compilation.py",
        "docs/reviews/codex-controller-compatibility.md",
        "docs/reviews/codex-sdk-compatibility.md",
        "docs/reviews/evidence/integrated-control.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md, AGENTS.md and every production module under src/codex_flow",
        "all promoted H6-G/H6-H/H6-I evidence and docs/reviews/evidence/human-terminal-ui/*.svg",
        "all focused production tests other than tests/test_production_pilots.py and tests/test_plan_compilation.py",
        "schemas, packaging, plugins, skills, templates, Makefile, pyproject.toml and uv.lock",
        "the retained wheels, canonical entrypoints and unrelated dirty or untracked bytes",
        "plugin installation/cache/trust/auth state, global Codex/App state, Git history and provider execution without explicit authorization",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only integrated-control in the existing dirty python-sdk-controller "
        "worktree as one Luna XHigh mutable owner at app-configured fast speed. Modify only the exact "
        "five surfaces and create only integrated-control.json. Add no production repair, alternate "
        "authority or plugin/global/App/Git mutation. Build the single provider-free retained-wheel "
        "temporary-service pilot with dependency-aware workers, bounded activity, exact steer/interrupt, "
        "two controller failure windows, strict plugin and typed fake SDK mechanisms, truthful connector "
        "and optional App status, adversarial restart/replay/drift/cleanup and protected-byte proof. Run "
        "all named gates and return one terminal result. If the standard shared home still fails strict "
        "plugin discovery or no one-attempt provider authorization exists, record the real sentinel and "
        "final milestone as external_blocked; do not mutate or substitute the environment. Any production "
        "defect requires a separate bounded repair capsule. Independent reviews remain pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

H6-E-W-R3 is closed under the explicitly recorded App-present runtime gate, the
private semantic naming pass is promoted, and the forward-only public semantic
cutover is promoted at exact candidate
`f7e86be6d6ae1d20cb9d431cbc173b526690af1172595c6266d10b4124b9fee8`.
The cutover removed old modules, identifiers and commands without aliases or
shims. Its single P1 was a stale active `AGENTS.md` command; the bounded repair
changed it to `sdk-compatibility-sentinel`. Final Luna XHigh objective and Sol
Medium architecture rechecks both returned `APPROVED`, P0=0/P1=0/P2=0, with
408 tests and exact wheel/import/help/absence gates green.

## Live-worker-control implementation candidate

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Expose bounded live worker activity, exact-turn steer and interrupt, and durable "
        "error-specific recovery through the harness control plane without App authority."
    ),
    decomposition=(
        "Extend the typed SDK adapter and worker boundary to retain live turn identity, stream bounded redacted activity and apply exact-turn steer or interrupt commands.",
        "Add one crash-atomic ledger migration for the diagnostic ring, typed retry policy and capability-bound control-command outbox.",
        "Upgrade authenticated local IPC and the supervisor so events, commands, acknowledgements, restart and loss windows remain exactly-once and fail closed.",
        "Expose one typed control client plus semantic noninteractive CLI operations for status, recent activity, steer, interrupt and bounded retry decisions.",
        "Exercise real delayed SDK behavior, fault/replay/budget matrices, exact wheel/service behavior and secret-negative cleanup evidence.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "A real delayed SDK worker emits bounded recent activity through the production control client, and one exact-turn steer changes its subsequent observable response without App API/task calls.",
        "One interrupt reaches only the bound live turn and closes with truthful SDK terminal evidence; stale generation, wrong attempt/turn, replay, oversized payload, malformed IPC and post-terminal commands fail before mutation.",
        "Durable retry policy proves five backoff-gated pre-identity retries, one invalid-chain fresh-thread rollover, two same-thread schema-envelope corrections and zero automatic retries for auth, permission, capability, profile or integrity failures.",
        "Restart between failure and retry neither resets nor double-consumes budget; exhaustion or ambiguous post-identity state produces one sanitized human_attention_required decision and no duplicate writer/result/successor/notification.",
        "The diagnostic ring enforces count and byte eviction plus redaction while remaining non-authoritative; control commands bind dispatch, generation, attempt, thread and turn identity with exactly-once acknowledgement semantics.",
        "Focused adversarial migration/adapter/IPC/worker/supervisor/client/CLI tests, make check, Ruff, exact wheel and temporary service checks pass with zero self-review P0/P1; independent objective and architecture promotion is deferred until the exact implementation candidate freezes.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/contracts.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/service.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/control_client.py",
        "src/codex_flow/cli.py",
        "focused semantic adapter, ledger, IPC, worker, supervisor, service, client and CLI tests",
        "docs/reviews/evidence/live-worker-control.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md and AGENTS.md",
        "promoted detached-supervisor and naming evidence/diff semantics",
        "src/codex_flow/native_profile.py, plan compilation/projection and App-native compatibility",
        "plugin sources, caches, configuration and future TUI modules",
        "accepted historical evidence, global Codex state and unrelated worktrees",
        "controller-turn recovery, plugin parity, TUI and integrated-promotion successor surfaces",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only live-worker-control in the existing dirty python-sdk-controller worktree. Preserve every donor and promoted cutover change. Implement the decision-ready H6-F architecture already specified in this plan with one owner for ledger, supervisor, SDK, IPC, service, control client and CLI. Keep App optional, workers leaf-bound, credentials secretless and every command/retry capability- and budget-bound. Create no subagents, callbacks, worktrees, commits or global mutations. Run adversarial and exact-wheel/service gates, self-review, retain sanitized live-worker-control evidence, and send exactly one terminal callback to the planning controller. Do not start controller recovery, plugin parity, TUI or integration."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=8_000,
)
```

This capsule produced implementation candidate
`78aae2667fc08d1bfcab86cab83606f2286acc7075db86df88995123633ec743`
and exact wheel
`b1150bf92db515e6927a40d72bff61022926f5bc662299182a6cfaa92489bf42`.
The implementation owner reported 416 tests, full `make check`, a 61-test
focused post-fix suite, exact-wheel install/help/import/service checks and clean
diff hygiene. Independent review rejected promotion, so this capsule is retained
implementation evidence and is no longer executable.

### Live-worker-control promotion findings and repair architecture

The exact candidate received `DO_NOT_PROMOTE` from both required authorities.
The Sol Medium architecture review found four P1s: worker exit and retry
classification were separate commits; command order depended on timestamp and
client id; the exported client returned unchecked dictionaries; and reasoned
CAS recovery exposed retry but not cancel or budget change. The Luna XHigh
objective review found five additional P1s and one P2: broad SDK exception
wrapping made non-transient failures retryable; `UnsupportedCapability` escaped
to an unclassified exit that became post-identity retry; one lost diagnostic
event permanently blocked later activity; non-string command ids were coerced;
the real delayed SDK/control-client steer gate was unproven; and a partial-frame
same-uid IPC client could block the supervisor event loop.

One bounded repair owns all ten findings because they converge on the same
ledger/SDK/worker/IPC/control-client contract. It does not begin controller-turn
recovery or change accepted retry ceilings, terminal-result authority, App
policy, permissions, plugin behavior or later TUI scope.

The repair design is fixed:

- Add one forward migration after schema v13. The control outbox receives a
  server-allocated per-dispatch monotonic submission sequence and every claim,
  list and delivery orders by that sequence, never wall clock or client id.
  Retry policy receives a monotonic revision. A semantic recovery-control audit
  record binds action id, dispatch, expected revision, action kind, bounded
  reason, requested budget fact and applied revision. Existing v13 rows migrate
  deterministically in current creation order and remain readable; no alias or
  parallel schema path is retained.
- Replace separate worker-exit and retry writes with one idempotent ledger
  transaction that validates the live process identity, records exit
  classification and consumes or rejects the typed retry decision together.
  Any ledger failure retains in-memory child ownership for retry in the same
  supervisor epoch. No failure is swallowed and no child is forgotten before
  the durable transaction commits.
- At the SDK boundary, allow automatic transient retry only for the pinned
  SDK's typed `TransportClosedError` or `is_retryable_error` result. Typed
  capability, authentication, permission, profile, integrity, malformed-input
  and all unknown SDK/RPC failures are explicit non-retryable outcomes; message
  text never selects retry. Worker exit codes carry these closed classes, and an
  unknown exit fails closed rather than becoming post-identity continuation.
- Make diagnostic delivery idempotent across a lost acknowledgement. The
  supervisor returns the next durable stream position when a turn binds; the
  worker advances only after acknowledgement and retries the same bounded event
  identity before later events. Exact duplicate delivery returns the prior
  record, conflicting replay fails closed, and a new process/turn resumes from
  the durable position. Diagnostic loss never becomes terminal authority or
  prevents later activity.
- Replace public `dict[str, object]` client responses with closed semantic
  status, activity, control-command, retry-policy and recovery-action models.
  Each decoder rejects missing, extra or wrongly typed fields. Keep raw request
  decoding private to the IPC adapter so H6-G and the TUI cannot depend on
  SQLite/wire shapes.
- Expose authorized retry, cancel and budget-change through those typed models.
  Every mutation requires an action id, expected policy revision and bounded
  reason. Budget changes remain within the accepted absolute ceilings, cannot
  fall below consumed counts or reset them, and atomically advance revision;
  stale, replayed, conflicting or terminal actions fail before mutation.
- Validate command id as a bounded non-empty string before conversion or
  persistence. Never coerce booleans, integers, containers, whitespace/control
  text or other malformed IPC values into an identifier.
- Apply a bounded server-side deadline immediately after accepting a UNIX
  connection and before reading its header/body. Timeout, fragmented, oversized
  or stalled frames close only that client and return control to lease renewal,
  recovery and other workers.
- Add a semantic opt-in `live-control-sentinel` that runs the exact installed
  wheel and production supervisor/worker/control client against one disposable
  Git repository with SDK `Sandbox.read_only`. It waits for a real bound delayed
  turn, submits one exact-turn steer through the control client, proves the
  steered marker in the terminal response and retained activity, compares
  before/after repository bytes, records zero App API/task calls and truthfully
  records App presence. It uses one bounded provider attempt, cleans every
  temporary service/runtime artifact, archives only its own SDK thread after
  proof and updates `live-worker-control.json` without secrets or raw prompts.

## Next execution — live-worker-control-conformance-repair

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Close every objective and architecture promotion finding on the frozen live-worker "
        "control boundary, including the real read-only steer outcome, without expanding later milestones."
    ),
    decomposition=(
        "Make worker-exit classification and retry policy one idempotent crash-atomic transaction, and allow automatic retry only for typed transient SDK failures.",
        "Migrate command ordering, diagnostic acknowledgement recovery and reasoned retry/cancel/budget controls to server-owned sequences and revision-bound CAS records.",
        "Replace raw control-client dictionaries with closed semantic models and reject malformed identifiers or wire shapes before mutation.",
        "Bound accepted IPC connections with a server-side frame deadline so a partial same-uid peer cannot stall supervisor liveness.",
        "Run one opt-in exact-wheel real SDK delayed-turn steer sentinel in a disposable read-only repository and retain sanitized outcome evidence.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Injected failure between exit observation and retry decision cannot persist either fact alone; ledger error retains child ownership and restart consumes each typed budget at most once.",
        "Only pinned-SDK typed transient transport/overload errors are retryable; capability, authentication, permission, profile, integrity, malformed and unknown failures consume zero automatic retry budget and produce one truthful fail-closed decision.",
        "Concurrent same-timestamp control submissions receive unique monotonic server sequence and are delivered exactly in submission order; stale/replayed/conflicting commands and non-string or malformed ids fail before mutation.",
        "Lost diagnostic request or acknowledgement can be retried idempotently and later activity remains visible after reconnect/restart without duplicate durable sequence or terminal implication.",
        "The public control client returns only closed status/activity/command/retry/recovery models, and reasoned retry, cancel and bounded budget change require action id plus expected revision with stale and terminal CAS rejection.",
        "A partial-frame same-uid IPC client exceeds the bounded server deadline without delaying lease renewal, recovery or a second valid client.",
        "One real exact-wheel SDK worker in a disposable read-only Git repository is observed and steered through the production control client; the final marker changes observably, repository bytes remain identical, App calls are zero and sanitized evidence records actual App presence.",
        "Focused fault/migration/adapter/worker/IPC/supervisor/client/CLI/sentinel tests, full make check, exact-wheel install/service/sentinel gates, diff hygiene and self-review pass with zero open P0/P1.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/contracts.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/control_client.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/live_control_sentinel.py",
        "src/codex_flow/__init__.py only for closed semantic client model exports",
        "focused semantic migration, adapter, worker, IPC, supervisor, client, CLI and sentinel tests",
        "docs/reviews/evidence/live-worker-control.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md and AGENTS.md",
        "promoted detached-supervisor and naming/cutover behavior outside direct regression repair",
        "src/codex_flow/native_profile.py, plan compilation/projection and App-native compatibility",
        "plugin sources, templates, schemas, configuration and future TUI modules",
        "controller-turn recovery, plugin parity and integrated-promotion successor surfaces",
        "accepted historical evidence, global Codex state and unrelated worktrees",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only live-worker-control-conformance-repair in the existing dirty python-sdk-controller worktree. Preserve all unrelated donor and concurrent plan changes. Implement the fixed repair architecture and close all nine P1 plus the IPC P2 from the two exact-candidate reviews. Use descriptive forward-only names and no aliases. Run focused gates during development, then the required shared-contract full gate, exact wheel/service checks and exactly one opt-in real SDK read-only steer sentinel; physical App closure is not required and App APIs/tasks are forbidden. Retain sanitized evidence, self-review the full repair diff, create no subagents/worktrees/commits/global mutations, and send one terminal callback to the planning controller. Do not start skill reconciliation, controller recovery, plugin parity, TUI or integration."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

This repair capsule produced frozen candidate
`3457b99128d2af84f93e69f343215a2f7e6a94d4013e607121cc93ef4c6d3ff9`
and exact wheel
`7a3ea21543f511c863013b9f193b8361fdd5c53bfcf0e0a4dc333dfb5eb397b7`.
The implementation owner reported 417 tests, full `make check`, exact-wheel and
temporary-service checks. Its one authorized real SDK sentinel attempt failed
after thread identity with an unknown non-transient SDK failure, so that
observable production gate remains `EXTERNAL_BLOCKED` and may not be retried
without fresh authority. Independent objective and architecture review matched
both frozen identities but rejected code promotion. This capsule is retained
implementation evidence and is no longer executable.

### Live-worker ownership and protocol-closure continuation

The exact repaired candidate received `DO_NOT_PROMOTE` with seven unique P1s
and one P2. The architecture review found that reasoned cancellation could
terminalize SQLite state and delete capability/liveness while the owned worker
continued running, and that the public command decoder discarded malformed
digest or acknowledgement facts. The objective review additionally found a
transport-before-identity exit path that deletes liveness before
`mark_worker_exit` reads it; retry strategy transitions that do not advance CAS
revision; cancellation unavailable from `human_attention_required`; a
post-rejection `BrokenPipeError` that can escape the foreground supervisor;
worker turn binding that accepts control/whitespace ids and boolean counters;
invalid-chain outcomes that incorrectly become human attention instead of the
single authorized fresh-thread rollover; and a P2 open top-level response
envelope.

These findings share one durable ledger/supervisor/public-control ownership
boundary. A cancellation repair and the retry/CAS repairs must therefore have
one writer. Because the cancellation/recovery class reopened after the Luna
implementation and repair cycle, the bounded continuation routes once to a
fresh Sol Medium recovery owner. It does not change accepted retry ceilings,
public intent, App policy, permissions, sentinel authority or any successor
milestone.

The continuation design is fixed:

- Make live cancellation a supervisor-owned stop protocol. Record a reasoned,
  revision-bound cancellation request without terminalizing the queue or
  deleting capability/liveness, interrupt or terminate the exact owned child
  under a bounded deadline, retain process and durable liveness ownership until
  exit acknowledgement, then atomically terminalize cancellation. A crash or
  failure before acknowledgement leaves enough durable ownership for recovery;
  no cancelled worker may continue repository work behind a terminal row.
- Permit the same reasoned cancellation protocol from
  `human_attention_required`. Retry, cancel and budget actions remain
  idempotent CAS operations, and every inspection, human-attention,
  continuation-budget or fresh-thread-budget strategy transition increments
  the monotonic retry-policy revision in the same transaction.
- Repair transport-before-identity exit processing so the one atomic
  exit/retry transaction never deletes a liveness row before its return value
  is built. The supervisor contains every ledger failure, retains child
  ownership until a durable decision commits and never raises an untyped
  `AttributeError` from the reap loop.
- Preserve the single typed invalid-chain fresh-thread budget for empty-history
  and latest-failed-turn provider outcomes. Persisted inspection distinguishes
  those normal invalid-chain facts from truly ambiguous identity/integrity
  evidence; no text heuristic or additional retry class is introduced.
- Validate the complete worker identity before mutating `_active_turns`:
  generation and attempt reject booleans, and turn ids reject empty,
  whitespace, control, oversized or otherwise malformed text. A failed
  rejection response, including `BrokenPipeError`, closes only that peer and
  cannot escape the supervisor loop.
- Close the public response and command envelopes. Reject all extra top-level
  fields, require a valid lower-case 64-hex payload digest, and accept only the
  exact bounded acknowledgement object shape before semantic projection. No
  malformed wire fact may be silently discarded.

## Next execution — live-worker-ownership-and-protocol-closure

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Close the exact live-worker ownership, retry/CAS and typed protocol findings on the "
        "frozen repaired boundary without retrying the externally blocked SDK sentinel or starting successors."
    ),
    decomposition=(
        "Make reasoned cancellation a bounded supervisor-owned stop-and-ack protocol that retains durable and process ownership until terminalization.",
        "Repair exit/retry atomicity, revision advancement, human-attention cancellation and invalid-chain fresh-thread classification.",
        "Reject malformed worker identities and contain failed rejection writes before they can affect supervisor liveness.",
        "Close public response, command digest and acknowledgement envelopes before semantic projection.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "A running cancellation cannot produce a terminal cancelled row until the exact child exits and durable acknowledgement commits; crash/failure retains recoverable ownership and the worker cannot continue behind terminal authority.",
        "Reasoned cancel works from human-attention state, every retry-strategy mutation advances revision, and stale retry/cancel/budget actions fail CAS before mutation.",
        "Transport-before-identity exit and retry commit atomically without missing-row exceptions or forgotten child ownership; each typed budget is consumed at most once.",
        "Empty-history and latest-failed-turn invalid-chain outcomes use at most the one authorized fresh-thread rollover, while ambiguous integrity evidence remains fail-closed.",
        "Malformed turn ids, boolean counters, broken rejection peers, extra response keys, invalid command digests and non-exact acknowledgement objects are rejected before durable or in-memory mutation.",
        "Focused ledger/supervisor/IPC/client/SDK tests pass during repair; because shared ledger and public control contracts change, closure runs affected semantic partitions and one full make check, exact-wheel/service checks, diff hygiene and complete self-review.",
        "The consumed real SDK sentinel is not rerun; evidence continues to report its production gate separately as external_blocked.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py and contracts.py only when the stop protocol needs an existing semantic type extension",
        "src/codex_flow/ledger.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/backends/codex_sdk.py only for typed invalid-chain classification",
        "src/codex_flow/control_client.py",
        "focused live-worker ledger, supervisor, IPC, control-client and SDK tests",
        "docs/reviews/evidence/live-worker-control.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md and AGENTS.md",
        "worker, IPC, service, CLI and sentinel behavior except a direct regression repair proven necessary by a finding",
        "promoted detached-supervisor, naming and public-cutover behavior outside the listed findings",
        "workflow skills, schemas, templates, test partitions, controller recovery, plugin parity, TUI and integrated promotion",
        "the consumed provider attempt, accepted historical evidence, global Codex/App state and unrelated worktrees",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only live-worker-ownership-and-protocol-closure in the existing dirty python-sdk-controller worktree. Preserve all unrelated and concurrent changes. Implement the fixed Sol Medium continuation design and close the seven unique P1 plus one P2 from the exact-candidate reviews. Use descriptive forward-only identifiers and no aliases. During implementation run only focused discriminating tests; at closure run the affected partitions and one required full make check plus exact-wheel/service and diff-hygiene gates. Do not retry the provider sentinel, call App/task APIs, create peers/subagents/worktrees/commits, mutate global state or start later milestones. Retain sanitized evidence and send one terminal callback to the planning controller."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

This continuation is the sole executable milestone. One fresh Sol Medium owner
implements, tests and self-reviews it in the existing worktree. After a frozen
candidate is returned, one Luna XHigh objective recheck and one independent Sol
Medium architecture recheck of that exact digest decide code promotion. Only
the still-blocked production sentinel may remain separate; no broader review
wave or second speculative repair is authorized before the controller
reevaluates the milestone and routing policy.

The continuation produced candidate
`8ea819175f6525234901b1bc22956d0873fe42555c7694d45412f282a20b3a48`
and exact wheel
`cc3e44b6bb0362e84c5bda971bded92b5161ea1229f716937299ba16ab9c42a7`.
Its architecture recheck approved with P0/P1/P2 zero, but the objective recheck
reproduced two P1 ownership races. First, worker authentication renewed durable
liveness before strict counter/turn validation, and the event path accepted
`generation=True` as generation one. Second, a worker result could terminalize
the queue after a cancellation request but before stop acknowledgement, leaving
the pending cancellation action, exited child and liveness permanently owned by
the reaper. This candidate is retained evidence and is not promoted.

## Retained execution — strict-worker-identity-and-cancellation-race-closure

One final bounded repair in the current Sol Medium recovery cycle owns only
these two findings:

- Split non-mutating capability lookup from liveness renewal. Validate the
  complete worker generation, attempt, thread and turn identity first for every
  worker event, bind, poll and acknowledgement operation; reject booleans,
  numeric lookalikes and malformed turn ids before any durable or in-memory
  write. Renew liveness only after the exact operation envelope and identity
  are closed.
- Give a pending cancellation precedence over result terminalization. A result
  racing after the cancellation CAS is either rejected before terminal
  mutation or retained as bounded non-authoritative evidence until the exact
  stop/exit acknowledgement atomically resolves cancellation. Reaping an
  already-exited child is idempotent across restart and can never strand a
  pending action, child ownership or liveness row behind a completed queue.

Mutable surfaces are limited to `src/codex_flow/ledger.py`,
`src/codex_flow/supervisor.py`, the smallest necessary semantic domain/contract
extension if unavoidable, focused live-worker/supervisor tests and
`docs/reviews/evidence/live-worker-control.json`. The canonical plan,
instructions, SDK adapter, worker, IPC, service, client, CLI, sentinel and all
successor milestones are protected unless a direct compile/test repair proves
an exact dependency. No alias or milestone-coupled identifier is allowed.

Acceptance requires adversarial proof that malformed bool/lookalike identities
leave liveness, activity and `_active_turns` byte-for-byte unchanged; and that
both cancel-before-result and result-before-cancel schedules reach one truthful
terminal owner without a live/unreaped child, stranded liveness or pending
action. During implementation run only the smallest discriminating tests. At
closure run the affected live-worker/supervisor semantic tests, then one broad
gate only if the shared contract impact cannot be bounded; rebuild and inspect
the exact wheel only for gates made stale by the repair, run `git diff --check`
and self-review the complete owned diff. The consumed provider sentinel is not
rerun and remains a separate external gate.

After the repaired digest freezes, one Luna XHigh objective recheck is the only
stale promotion authority. The prior exact-candidate architecture approval may
be carried forward only if the repair stays inside this fixed design and the
reviewer confirms no architecture surface changed; otherwise run one fresh Sol
Medium architecture recheck. A surviving same-class finding after this repair
triggers the repository's Sol High escalation rather than another Sol Medium
cycle.

The final repair produced candidate
`b49e8da8debe577c4b3bb661bd0c1aa8ac6cab46a5161cabdd5d8b2195c15bb5`
and wheel
`44dbeaa82538c8b08e6a2210cd7e51c20591c3e94685656724f311e3aeb63603`.
The authorized Luna XHigh recheck returned `APPROVED`, P0=0/P1=0/P2=0 after
52 focused identity/race tests and confirmed the exact architecture approval
carried forward. Live-worker deterministic code and architecture promotion is
therefore complete. The consumed real SDK delayed-steer sentinel remains the
only separate `EXTERNAL_BLOCKED` production gate and is not retried by any
successor milestone.

## Plugin and controller installation architecture

The installed Codex plugin intentionally contains skills and trusted lifecycle
hooks, while the Python `codex-flow` executable is an independently installed
tool. The accepted user outcome is nevertheless one explicit repository command
that installs or upgrades both from the same checkout and proves that a new task
can call the matching controller and TUI outside this worktree. Codex does not
provide a plugin `OnInstall` event for arbitrary binary installation, so no
session hook may mutate `~/.local`, install Python, or silently bootstrap the
controller.

One bounded repository bootstrap owns the operation. It uses only the Python
standard library for orchestration and invokes the supported `uv tool install`,
`codex plugin marketplace add`, and `codex plugin add` commands with argument
vectors, never a shell. It resolves the repository root from its own path,
requires the standard inherited Codex home, installs the current checkout with
CPython 3.12, registers that checkout as `adam-workflows`, installs and enables
`personal-workflow-skills`, then verifies the installed CLI, `tui` command,
manifest/version, regular single-link bundle files, and byte parity. A repeated
run converges safely. Failure reports the completed phase and exits nonzero; it
does not invent transactional rollback across two external installers.

Architecture map and ownership:

- `scripts/install_personal_workflow_skills.py` — create; sole bootstrap
  orchestrator and verification owner. It owns no provider, controller, ledger,
  App, authentication, configuration format, or plugin-discovery semantics.
- `Makefile` and `README.md` — modify; expose and document one canonical
  `make install-personal-workflow-skills` entrypoint plus its explicit global
  effects and new-task requirement.
- `plugins/personal-workflow-skills/skills/workflow-control/SKILL.md` — modify;
  perform a non-mutating CLI/TUI preflight and point a missing or incompatible
  installation to the canonical bootstrap instead of attempting installation.
- `.agents/plugins/marketplace.json` and
  `plugins/personal-workflow-skills/.codex-plugin/plugin.json` — modify together
  only for one forward plugin version increment; their version remains a single
  exact authority pair.
- `tests/test_plugin_installation.py` — create; fake-command and filesystem
  integration tests for argument safety, ordering, idempotence, parity,
  incompatible CLI, symlink rejection and truthful partial failure without
  touching the real shared home.
- `tests/test_plan_compilation.py` — modify only to bind this canonical-plan
  revision and compile the exact new capsule.
- `docs/reviews/evidence/plugin-and-controller-installation.json` — create;
  sanitized closure evidence for candidate, tests and the final explicit shared-
  home installation performed only after code promotion.

Dependency direction is bootstrap -> external `uv`/`codex` CLIs -> installed
tool/plugin facts. Plugin skills may diagnose the bootstrap result but never
invoke or own it. Production `src/codex_flow/**`, hooks, schemas, controller
state, retained wheels, accepted evidence and provider sentinels remain
protected. New durable-artifact budget is three: installer, focused test module,
and evidence record. There is one vertical milestone and no useful internal
fan-out because version authority, command ordering and end-to-end verification
share one ownership boundary. No unresolved design decision remains.

## Next execution — plugin-and-controller-installation-schema-compatible

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Provide one explicit, idempotent repository command that installs or upgrades both "
        "personal-workflow-skills and the matching codex-flow CLI into the standard shared user environment."
    ),
    decomposition=(
        "Create one standard-library bootstrap that validates prerequisites and invokes uv and Codex plugin installation with closed argument vectors.",
        "Verify the installed Python 3.12 tool exposes the current controller and TUI and that the enabled plugin bundle is regular, single-link and byte-identical to source.",
        "Expose the bootstrap through Makefile and README, add a non-mutating workflow-control preflight, and advance the plugin manifest/marketplace version together.",
        "Prove success, safe repeat, fail-closed partial failure and symlink or version drift rejection without mutating the real shared home during tests.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "From any clean checkout, one documented make target installs the checkout as the CPython 3.12 codex-flow uv tool and installs/enables personal-workflow-skills from the same local adam-workflows marketplace.",
        "A successful run proves codex-flow is on PATH, reports version 0.2.0, exposes the tui command, and the installed plugin manifest/version and every bundle byte match the source with no symlink, special file or multi-link regular file.",
        "Running the command again converges without duplicate marketplace/plugin authority; a failed external phase exits nonzero with truthful completed-phase context and never reports success or mutates AGENTS.md.",
        "The bootstrap rejects private CODEX_HOME, incompatible Python/tool output, mismatched manifest/marketplace versions, path substitution and unsafe installed bundle topology before declaring readiness.",
        "The workflow-control skill diagnoses a missing or stale CLI and names the canonical bootstrap but performs no installation or global mutation itself.",
        "Focused installer tests, workflow asset validation, affected plan/plugin partitions, one full make check, Ruff, compileall, diff hygiene and complete self-review pass with zero open P0/P1; no provider, App, connector or real SDK sentinel runs.",
    ),
    mutable_surfaces=(
        "scripts/install_personal_workflow_skills.py",
        "Makefile",
        "README.md",
        "plugins/personal-workflow-skills/skills/workflow-control/SKILL.md",
        "plugins/personal-workflow-skills/.codex-plugin/plugin.json",
        ".agents/plugins/marketplace.json",
        "tests/test_plugin_installation.py",
        "tests/test_plan_compilation.py",
        "docs/reviews/evidence/plugin-and-controller-installation.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md after this capsule is frozen and repository/global AGENTS.md",
        "src/codex_flow production modules, schemas, hooks and every other plugin skill",
        "retained wheels and previously accepted evidence or visual artifacts",
        "provider sentinels, Codex authentication, App state, remotes, Git history and unrelated dirty worktree bytes",
        "the real shared Codex home during implementation tests; final activation belongs to the planning owner after promotion",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Schema-compatible recovery continuation: use execute-milestone for only plugin-and-controller-installation in the existing dirty python-sdk-controller worktree after the superseded dispatch reached the real SDK but its provider wire schema was rejected. The installed adapter now projects the provider-supported schema while preserving complete local validation. Implement the frozen bounded bootstrap architecture without changing other production codex_flow modules, hooks, schemas, retained wheels, accepted evidence, provider state or unrelated bytes. Use the exact mutable surfaces and three-artifact budget; do not add an automatic install hook, alternate package manager, rollback framework, second installer or new public runtime abstraction. Tests use fake commands and an isolated standard-home fixture only; do not mutate the real shared home or call providers/App/connectors. Run focused checks while implementing, then the named affected partitions and one full make check because plugin contracts, Makefile and plan compilation change. Retain sanitized evidence, self-review the exact diff with P0/P1 zero, and return one terminal result to the controller. Do not start another milestone."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

## Next execution — local-image-worker-input

This user-authorized post-install follow-up adds one typed, repository-local
image-input path to the existing controller and SDK worker.  It does not add a
transport, persistence authority, browser runtime, TUI bitmap renderer, remote
URL fetcher or alternate lifecycle owner.  Model-facing capsule schema v3
extends the established prompt with a bounded non-empty tuple of
repository-relative image paths and may carry the existing plugin requirements;
schema v1 and v2 remain byte- and behavior-compatible.  The internal execution
capsule carries the same paths, and the worker validates each path as a bounded
single-link regular image below the selected workspace immediately before the
external turn.  The adapter then projects one official SDK `TextInput` or
`SkillInput` followed by official `LocalImageInput` values.  Image bytes never
enter SQLite, IPC, evidence, logs or the TUI transcript projection.

The same bounded owner also closes two directly observed usability defects.
First, `supervisor install` and `supervisor start` can bind different native
profile snapshots when the App updates its configuration between processes.
One explicit `supervisor refresh` operation must load one profile once, install
the exact unit, reload the user manager, and restart the repository-scoped
supervisor with that same credential/profile authority.  The repository
bootstrap may invoke this operation after installing the current tool, so one
documented command leaves both the plugin/controller and an already-installed
repository supervisor current.  It never installs a supervisor implicitly for
a repository that has no existing unit.

Second, the legacy native handoff wording is clarified: "one bounded handoff"
is a per-peer/per-milestone idempotency rule, not a global per-turn concurrency
cap.  A planning/controller turn may issue multiple START operations in the
same turn when every milestone is ready, each has one owner, mutable surfaces
are disjoint, and each peer is started exactly once.  Serial dependencies and
shared mutable ownership still prohibit parallel STARTs.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Let a codex-flow SDK worker receive bounded local screenshots and other image files "
        "as official typed Codex turn inputs while preserving the existing controller authority."
    ),
    decomposition=(
        "Version the model-facing and execution capsule contracts with repository-relative local image paths while preserving v1/v2.",
        "Validate image topology, type, size and workspace containment before the provider boundary.",
        "Project prompt or verified skill input plus official LocalImageInput values through the existing SDK adapter and detached worker.",
        "Add one convergent supervisor refresh path and let the explicit repository bootstrap refresh an already-installed matching unit.",
        "Clarify that native handoff singularity is per peer and milestone, while ready disjoint milestones may start concurrently in one planning turn.",
        "Prove the installed controller can read an image and use Chromium/Playwright to inspect a page and screenshot in one disposable real run.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "A schema-v3 capsule with one or more repository-relative local images reaches the same SDK worker as ordered TextInput/LocalImageInput values and returns one typed result.",
        "Schema v1/v2 behavior is unchanged; malformed, missing, external, symlinked, special, multi-link, oversized or unsupported image paths fail before an SDK turn starts.",
        "Image bytes are not copied into controller persistence, IPC, logs, evidence or conversation projections, and no second transport or lifecycle owner is introduced.",
        "One supervisor refresh command uses one native profile snapshot to install, daemon-reload and restart the exact repository unit; repeated refresh converges and the bootstrap refreshes only a pre-existing matching unit.",
        "Instructions and the legacy handoff skill explicitly allow multiple same-turn STARTs for ready disjoint milestones while preserving one start and one owner per milestone.",
        "Focused contract, projection, adapter, controller, worker and installer tests plus the full repository gate pass before reinstalling the shared tool.",
        "One disposable real provider smoke proves image understanding, browser-driven UI inspection, screenshot capture and controller-owned terminal result projection without claiming universal connector availability.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/contracts.py",
        "src/codex_flow/projection.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/service.py",
        "src/codex_flow/cli.py",
        "schemas/capsule.schema.json",
        "scripts/install_personal_workflow_skills.py",
        "Makefile",
        "README.md",
        "AGENTS.md",
        "templates/AGENTS.md",
        "templates/AGENTS.workflow.md",
        "plugins/personal-workflow-skills/skills/codex-thread-handoff/SKILL.md",
        "plugins/personal-workflow-skills/skills/plan-work/SKILL.md",
        "plugins/personal-workflow-skills/skills/workflow-control/SKILL.md",
        "tests/test_model_facing_projection.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_controller_execution.py",
        "tests/test_live_worker_control.py",
        "tests/test_service_lifecycle.py",
        "tests/test_plugin_installation.py",
        "tests/test_workflow_assets.py",
        "tests/test_plan_compilation.py",
        "tests/test_production_pilots.py",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "src/codex_flow/ledger.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/tui.py",
        "src/codex_flow/tui_client.py",
        "src/codex_flow/tui_models.py",
        "pyproject.toml",
        "uv.lock",
        "plugins/personal-workflow-skills/.codex-plugin/plugin.json",
        ".agents/plugins/marketplace.json",
        "docs/reviews/evidence",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Implement only local-image-worker-input in the existing dirty python-sdk-controller worktree. "
        "Preserve schema-v1/v2 callers and the single supervisor/SDK worker authority. Accept only bounded "
        "repository-local regular image files, pass them with the official SDK input types, retain no image "
        "bytes, add no browser installation or TUI bitmap renderer. Add the frozen one-snapshot supervisor "
        "refresh and clarify same-turn parallel START semantics without weakening per-milestone idempotency. "
        "Run one authorized disposable real image plus Chromium/Playwright smoke after deterministic gates "
        "and shared-tool installation pass."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

## Next execution — supervisor-refresh-and-runtime-compatibility-recovery

This recovery supersedes the inactive `local-image-worker-input` dispatch. Its
first implementation and green deterministic checks remain in the same dirty
worktree. The old dispatch is already closed resultlessly as cancelled. Shared
activation is controller-owned only after the recovery worker returns.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Complete the local-image worker candidate and make shared codex-flow refresh safe, "
        "non-self-terminating and recoverable under intentional runtime compatibility changes."
    ),
    decomposition=(
        "Preserve and reconcile the existing image, CLI, diagnostics and parallel-START candidate without reimplementing already-green work.",
        "Add one durable supervisor shutdown fence that rejects refresh while worker or controller children are active and blocks new enqueue during handoff.",
        "Replace immediate systemctl restart with authenticated shutdown, bounded old-owner/service disappearance, install/reload/start and replacement health verification.",
        "Classify pre-thread native compatibility mismatch as profile/configuration drift and require a newly bound superseding dispatch rather than malformed-input retry.",
        "Contain a durably closed worker-spawn failure to its dispatch so stale private attempt artifacts cannot terminate the repository supervisor.",
        "Run the bootstrap inside the locked checkout environment and permit only a well-formed profile-digest update during an already fenced refresh.",
        "Prove the refresh race, active-worker deferral, enqueue fence, lease handoff, classification and resultless supersession with focused deterministic tests.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "The blocked local-image dispatch is cancelled exactly once without result, restart, successor or fabricated terminal model output before the recovery owner starts.",
        "An active worker or controller child makes shared bootstrap/refresh defer or fail before tool/plugin installation or supervisor termination; its SDK turn and durable identity remain intact.",
        "With no active child, refresh sets one durable fence, prevents enqueue, requests authenticated shutdown, waits for the old PID birth identity and unit inactivity, then installs, reloads, starts and health-checks exactly one replacement supervisor.",
        "Timeout, IPC failure, service failure or replacement-health failure is explicit and leaves no false ready claim; a subsequent bounded refresh can reconcile safely.",
        "Native compatibility mismatch before SDK identity is recorded as profile/configuration drift with human attention and is never labelled malformed_input or automatically retried against rewritten authority.",
        "A colliding immutable attempt descriptor leaves the dispatch in human attention while the supervisor continues serving unrelated durable work; failed terminalization still fails the service closed.",
        "The canonical make bootstrap loads checkout code through uv, then uses uv tool install for shared activation; refresh accepts no installed-unit drift except one syntactically valid prior profile digest.",
        "The existing schema-v3 local-image input, cleaned CLI, conversation/tool summaries and same-turn disjoint START semantics remain intact; focused affected partitions and one full make check pass.",
        "The worker does not install or refresh the shared tool during its own run; after its terminal result the controller may activate once and run the authorized disposable real image and Chromium/Playwright smoke.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/service.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/control_client.py",
        "scripts/install_personal_workflow_skills.py",
        "Makefile",
        "tests/test_controller_execution.py",
        "tests/test_live_worker_control.py",
        "tests/test_service_lifecycle.py",
        "tests/test_supervisor_recovery.py",
        "tests/test_plugin_installation.py",
        "tests/test_plan_compilation.py",
        "tests/test_production_pilots.py",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md after this capsule is frozen and AGENTS.md",
        "schemas and persisted database schema or migrations",
        "src/codex_flow/backends/codex_sdk.py image and provider projection except for verification",
        "src/codex_flow/contracts.py and src/codex_flow/projection.py schema-v3 image contracts except for verification",
        "src/codex_flow/ipc.py framing and 64 KiB ceiling",
        "TUI modules, retained evidence and wheels, plugin manifests and unrelated plugin skills",
        "Codex authentication, global configuration, App state, remotes, Git history and unrelated dirty bytes",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use recover-milestone and execute-milestone as one Luna XHigh mutable owner for only "
        "supervisor-refresh-and-runtime-compatibility-recovery in the existing dirty python-sdk-controller "
        "worktree. Preserve the first local-image worker's candidate and green checks. Implement the frozen "
        "shutdown-fence architecture without a new table, migration, daemon, transport or lifecycle owner. "
        "Contain already-terminalized spawn failures without hiding uncertain terminalization, and preserve exact unit authority while allowing only the fenced profile-digest rotation proven by recovery. "
        "Never install or refresh the shared tool while this worker is active; use fake/hermetic systemd and "
        "process-identity discriminators, affected partitions and one full make check. Return one raw typed "
        "terminal result to the controller, which alone owns later activation and real provider/browser smoke."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

## Post-smoke successor — live-coding-agent-terminal-ui

This successor starts only after one real codex-flow dispatch closes with a
durable result and controller acknowledgement on the repaired supervisor.  A
provider-free liveness gate is necessary but not sufficient: if the smoke does
not complete, recovery remains the sole executable predecessor and this
milestone stays blocked by an acceptance dependency rather than starting an
alternate worker or UI path.

The user selected a coding-agent experience for the existing terminal UI.  An
active worker's assistant response must appear progressively in the
conversation pane, and tool calls must appear inline with human labels and
pending, running, succeeded, failed or interrupted state.  This is distinct
from both the complete persisted conversation and the bounded diagnostic ring.
Persisted SDK `Thread.read(include_turns=True)` remains the stable conversation
authority; live deltas are ephemeral projections that may be coalesced or lost
without changing lifecycle or completion truth.

Existing agent TUIs are references, not dependencies.  Codex CLI, OpenCode,
Aider and similar tools are complete applications coupled to their own runtime,
session model, input loop and state projection; embedding one would introduce a
second control plane or require replacing codex-flow's SDK, supervisor and
Textual ownership.  The current Textual application already owns layout,
keyboard actions, narrow rendering and conversation paging, so the milestone
reuses it and adds no UI framework or terminal dependency.

The reference implementation was checked against the open-source Codex CLI at
upstream commit `6a479e1813fc44c1ea3c85b7b4023ee3d4c21b8f`.  Its reusable pattern is a
committed transcript plus one mutable in-flight `active_cell`, a monotonic
revision that invalidates cached layout whenever that cell changes, typed
thread/turn/item notifications for start, delta and completion, and final item
completion as the reconciliation authority when a saturated transport loses a
delta.  Codex-flow adopts those semantic invariants, including authoritative
completion and mutable in-place tool cells, but not the CLI's Rust/Ratatui
widgets, app-server connection, terminal scrollback ownership or replay store.
The pinned `openai-codex==0.147.0` Python SDK already exposes the corresponding
typed `AgentMessageDeltaNotification`, `ItemStartedNotification`,
`ItemCompletedNotification`, `CommandExecutionOutputDeltaNotification` and
`McpToolCallProgressNotification` payloads; the adapter must consume those
official SDK events rather than inventing a parser or depending on private App
state.

### Frozen live projection architecture

The allowed dependency direction is:

    official SDK event stream
      -> codex_sdk typed event projection
      -> worker in-memory message/tool accumulator
      -> supervisor ephemeral live-view broker
      -> authenticated IPC subscription
      -> TerminalUiClient selected-subject subscription
      -> Textual conversation and inline tool rendering

The worker keeps one bounded accumulator per active turn.  It projects only
typed assistant-text and tool lifecycle facts from official SDK events.  It
coalesces rapid fragments into cumulative redacted keyframes no faster than 20
frames per second, with an 8 KiB fragment ceiling, 64 KiB per assistant message
or tool excerpt and 256 KiB total active-turn memory ceiling.  Intermediate
frames are lossy under pressure, while each retained keyframe contains the
complete bounded projection so far; terminal item state, interrupt state,
worker identity and the ModelFacingResult are never downgraded to lossy deltas.
An exceeded live-view bound becomes an explicit truncated-presence marker in
the ephemeral UI only and cannot truncate or alter the official persisted
conversation read or result contract.

The supervisor broker is memory-only and identity-bound to dispatch,
generation, attempt, SDK thread, active turn and monotonically increasing live
revision.  It holds at most four subscribers and one latest keyframe per active
subject.  Slow or disconnected subscribers lose intermediate frames and are
closed without blocking worker heartbeats, controls, lease renewal, diagnostics
or result ingress.  The existing 64 KiB framed same-UID IPC remains the only
transport; one closed subscription operation extends that framing without a
socket, port, daemon, timer, polling loop or alternate lifecycle owner.

The TUI opens one subscription only for the selected active controller or
worker and cancels it on selection change, reconnect or exit.  Assistant text
updates in place instead of creating one row per token.  A tool call renders as
one inline block whose state and bounded redacted summary update in place.
The selected subject owns one mutable active view with a monotonic render
revision; committed conversation rows remain immutable.  SDK `item/completed`
replaces the matching active item with its authoritative final projection, so
loss of an intermediate delta can affect animation smoothness but never the
final displayed item or the stable conversation rebuilt after the turn.
Paths, URLs, secrets and image bytes follow the existing typed presence-marker
and redaction rules.  At terminal state or reconnect, the client discards all
ephemeral keyframes, reads a fresh stable conversation snapshot and replaces
the live projection; it never merges diagnostics or missing deltas to invent a
persisted message.  Existing steer, interrupt, decision confirmation, load
older, scrolling, controller-over-workers hierarchy and explicit unavailable,
stale, incomplete and oversized states remain unchanged.

### Architecture map, ownership and artifact budget

- Modify `src/codex_flow/domain.py` only for non-persisted typed live-message,
  tool-state, keyframe and subscription status values.
- Modify `src/codex_flow/backends/codex_sdk.py` only to project documented SDK
  assistant-text and tool lifecycle events into those values.
- Modify `src/codex_flow/worker.py` only for the bounded active-turn accumulator,
  coalescing and priority-aware publication through the existing control path.
- Modify `src/codex_flow/supervisor.py` only for the bounded ephemeral broker,
  exact identity validation and one subscription operation.
- Modify `src/codex_flow/ipc.py` only for bounded multi-frame subscription
  helpers over the existing framing and timeout rules.
- Modify `src/codex_flow/control_client.py`, `src/codex_flow/tui_client.py`,
  `src/codex_flow/tui_models.py` and `src/codex_flow/tui.py` for subscription
  lifecycle, stale-frame discard and conversation-first rendering.
- Modify only the existing focused adapter, IPC, supervisor, live-control,
  workflow-control, production-pilot and plan-compilation tests, plus the
  existing human-terminal-ui evidence, visual contract and four render
  sentinels when observable UI proof changes.
- Preserve ledger tables/schema/migrations, retry and result authority,
  controller scheduling, service lifecycle, CLI production entrypoints,
  capsule/result schemas, plugin authority, SDK create/resume/run semantics,
  App/global/auth state, packaging and unrelated dirty bytes.

The new durable-artifact budget is zero: no production module, table, migration,
schema, evidence path, transport, registry, runner, daemon or dependency may be
created.  Executor-private helpers remain within the named owner modules.  Any
need for a new durable/public surface returns to planning before implementation.

### Milestone DAG and promotion

`supervisor-liveness real smoke -> event-driven-program-controller ->
live-coding-agent-terminal-ui -> objective and architecture reviews -> ordinary
codex-flow use`.  The first serial edge is shared schema, state-authority and
production-entrypoint work: the program controller generalizes the durable
controller action boundary before the TUI may consume it.  The second edge is
an acceptance dependency: live UI traffic cannot be promoted before the same
production supervisor proves terminal result ingress and program-level event
routing.  Each implementation has one mutable owner.  Read-only objective and
architecture reviews may overlap only after an exact candidate freezes; the
user-cancelled visual-promotion review is not reinstated.

### Latest real-smoke status

The authorized repaired-supervisor smoke reached attempt 13 on SDK thread
`01a05e1c-a8d3-70f1-b786-1f4ef7cfcc19`.  The supervisor remained active with
zero restarts, the worker executed commands, and official assistant-message and
tool lifecycle events crossed the production adapter.  A read-only official
`Thread.read(include_turns=True)` inspection proved one full persisted failed
turn and an authoritative failed `turn/completed`: codex-lb returned HTTP 503
because its session bridge was cooling down after repeated upstream timeouts.
The live diagnostic ring did not retain that terminal event, but the adapter
classified it correctly as `transient-after-identity`; no terminal model result
or attempt-13 result file exists.

Durable state is `human_attention_required` with `post_identity_loss`.  The
reused historical dispatch entered attempt 13 with `inspection_used=4` and
`inspection_budget=4`, so the exit path stopped before the ordinary one-read
same-thread continuation.  Those counters are cumulative dispatch recovery
authority, not a queue deadline.  Reusing this thirteen-attempt dispatch as a
new smoke therefore polluted the acceptance probe.  The completed intermediate
JSON-like assistant item was phase `commentary`, not a terminal result; the
agent continued tool work and it was never submitted to the result contract.

This proves worker/provider/tool/event reachability but not terminal result
ingress.  Attempt 14 is forbidden.  The next smoke uses a fresh dispatch, and
provider-transient recovery is repaired first so a typed 429/5xx can wait and
continue the exact persisted thread without replaying completed side effects.

### Program integration trunk and parallel lane policy

The existing `python-sdk-controller` program worktree is the sole local
integration trunk and stays active until this plan closes.  Its branch must
progressively contain every promoted milestone.  Before the first mutable
parallel fan-out, the integration owner must establish one coherent verified
local commit containing only authorized surfaces and record the exact SHA as
the frozen DAG base.  A dirty baseline that cannot be separated from unrelated
user bytes blocks fan-out until this plan records a safe separation; a digest
or test result does not substitute for the commit.

A serial milestone with one mutable owner may work and commit directly in this
program worktree. Its coherent local commit is both the physical workspace
candidate and the input to logical promotion; integrating it never invokes a
same-checkout merge, cherry-pick or reset. The ledger records the last promoted
trunk SHA separately from the current physical workspace/candidate HEAD. Only
a promoted serial commit unlocks its successors. A rejected or blocked serial
commit remains in forward-only local history so repair can continue on top;
abandoning it requires an explicit controller-authorized revert or replan,
never implicit cleanup.

Parallel mutable milestones use semantic child lanes in physical sibling Git
worktrees at
`<repo-parent>/<repo-name>.worktrees/<program-slug>-<lane-slug>`, each created
by the controller from the frozen integration SHA or one exact integrated
predecessor, with branch `agent/<program-slug>-<lane-slug>`.  No lane worker
modifies or integrates the trunk.  Every mutable lane creates at least one
coherent local commit before `COMPLETION`, stages only owned surfaces, inspects
the staged diff, runs `git diff --cached --check`, and proves its named outcome
gates.  Read-only planning/review/evidence work and an explicit no-commit
capsule are the only exceptions.

Independent review and promotion bind to an exact lane tip or commit range.  A
repair adds a successor commit; amending an already reviewed commit invalidates
that review.  Only the controller/integration owner may integrate promoted
commits after promotion-blocking P0/P1 findings reach zero.  A merge commit is
the default for true fan-out; cherry-pick or fast-forward requires a recorded
reason.  Integration verifies ancestry and absence of unrelated commits,
treats conflict resolution as new integration work, and reruns proportional
integration gates.  Readiness advances only after successful integration;
successors start from the new trunk tip.  Lane worktrees and branches remain
until commit, review, integration and recovery evidence are durable.

This plan authorizes its required local commits and local integration only.  It
does not authorize push, rebase, history rewrite, discard, remote mutation or
implicit cleanup.  The present dirty baseline is not safely separable into the
required checkpoint because the policy sources overlap the existing
uncommitted program candidate and several skill files are untracked.  No
checkpoint SHA is fabricated here; mutable fan-out remains not ready.  This
policy becomes operational at the first later coherent verified integration
commit, which must record its SHA before any lane START.

At this replan checkpoint the existing index already contains five staged
semantic renames outside the policy surfaces, while the instruction/skill/test
sources above also contain pre-existing unstaged or untracked program work.
They cannot truthfully be folded into a policy-only commit.  The policy
validator, five skill validators, Ruff and the focused workflow/plan suite are
green (`74 passed`).  The full repository run reached `768 passed` and one
pre-existing controller lease-contention timeout; the exact failed round passed
alone, while the eight-round discriminator reproduced the timeout on a
different round.  Because the policy change does not own controller lease code,
that failure is retained as an integration-checkpoint blocker rather than
repaired or hidden here.

### Integration checkpoint activation

The planning-time dirty-baseline blocker above is now closed. Read-only
provenance audit classified the complete accumulated program candidate as 109
authorized logical changes with no unrelated nonignored user surface. The
lease discriminator was not an authority failure: its per-future 15-second
wall-clock bound was shorter than the intentionally serialized full-validation
path under load. The test now waits for both contenders under one bounded
60-second deadline and still requires exactly one durable winner; 48 focused
rounds and the full repository gate passed.

Local integration checkpoint commit
`093eb64accd51c0516680c2c7779bcba0fe4641a` is the frozen DAG base. It
contains the complete authorized codex-flow program candidate, the narrow
retained-SVG whitespace exception required to preserve exact historical
evidence bytes, and no `.codex-flow`, retained-wheel, cache, environment or
remote state. `make check` passed with Ruff, 769 tests, 16 skill and 7 schema
validations, compileall and every pre-commit hook; `git diff --cached --check`
also passed before commit. The active provider-transient milestone is now
ready for one managed semantic child lane based on this exact checkpoint; the
later metadata commit that records this SHA does not change its implementation
or acceptance boundary.

## Completed — provider-transient-backoff-and-fresh-e2e-smoke

The next owner repairs only typed transient provider recovery and then runs one
fresh disposable E2E smoke after installation.  A pre-identity transient may
retry without thread recovery.  After thread identity, a 429 or retryable 5xx
never replays the original prompt blindly: the worker exits, the controller
waits, reads the persisted thread once, proves the prior turn is failed and no
writer is active, and issues a continuation on the same thread.  Completed
tool/file side effects remain in the worktree and the continuation prompt
explicitly forbids repeating them.

New dispatches receive three post-identity transient recoveries.  Backoff is
durable and deterministic, increasing across the three failures and respecting
an explicit provider reset time when present.  Existing dispatch counters are
never silently reset.  A typed authorized recovery action may increase the
remaining ceiling by one bounded grant while retaining prior usage in the
action receipt; repeated implicit grants are impossible.  After the grant is
exhausted the dispatch requires human attention.  Inspection and provider-loss
budgets must remain mutually sufficient, and an explicitly new smoke uses a
new dispatch rather than inheriting historical attempts.

The persisted schema changes only through a forward v16-to-v17 migration of the
existing retry-policy authority; no table, registry, scheduler or alternate
result path is added.  `domain.py` owns the bounded retry types,
`codex_sdk.py` owns typed 429/5xx classification only, `ledger.py` owns counters,
grant receipts and eligible-at state, and `supervisor.py` owns the one-read
continuation sequence.  Worker/result submission remains unchanged.  Failed
`turn/completed` is terminal-priority diagnostic input and must be delivered or
reconstructed from the official read before the continuation; it never becomes
conversation authority.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Recover typed transient codex-lb/provider failures with bounded durable backoff and prove "
        "one fresh codex-flow worker-result-controller end-to-end smoke."
    ),
    decomposition=(
        "Extend the existing retry authority and forward migration for three bounded post-identity transient continuations without resetting historical use.",
        "Make failed-turn inspection schedule deterministic backoff and resume the exact persisted SDK thread without replaying the original prompt or completed side effects.",
        "Preserve failed turn/completed priority across the worker-supervisor diagnostic boundary while keeping Thread.read as recovery truth.",
        "Run provider-free migration, retry, restart, idempotence and no-duplicate-side-effect discriminators, then install through the canonical local installer.",
        "Create one fresh disposable smoke dispatch that edits one sentinel file and returns one valid ModelFacingResult durably acknowledged by the controller."
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "A typed 429/500/502/503/504 after thread identity schedules at most three increasing backoff continuations on the same thread after one full failed-turn inspection; it never blindly resubmits the original prompt.",
        "Recovery preserves completed tool/file effects, rejects active or ambiguous writers, retains cumulative usage, and requires one exact authorized grant rather than silently resetting an exhausted dispatch.",
        "Schema v16 migrates forward without losing dispatch, result, capability, action, retry or recovery facts; no second table, scheduler, transport or lifecycle authority is introduced.",
        "Failed turn/completed remains observable to recovery even when lossy diagnostics are saturated, while raw transcript/tool output remains outside ledger, logs and evidence.",
        "One new disposable real dispatch starts with fresh budgets, modifies exactly its sentinel file, commits one valid raw result through worker IPC, and reaches controller acknowledgement without supervisor restart or residue.",
        "Focused retry/migration/SDK/supervisor tests, affected semantic partitions, full make check, canonical install parity and the exact real smoke close with P0=0/P1=0."
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py only for terminal lifecycle diagnostic priority",
        "src/codex_flow/control_client.py and src/codex_flow/cli.py only for the existing typed budget grant surface",
        "tests/test_codex_sdk_adapter.py, tests/test_supervisor_recovery.py, tests/test_ledger_integrity.py, tests/test_live_worker_control.py and tests/test_production_pilots.py",
        "docs/reviews/peer-thread-workflow.md only for final exact evidence/status reconciliation"
    ),
    protected_surfaces=(
        "capsule/result schemas and controller action semantics",
        "TUI live-streaming modules and visual evidence",
        "plugin registry, native authentication, App/global state and alternate transports",
        "unrelated dirty bytes, remotes and Git history outside the required local milestone commit"
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only provider-transient-backoff-and-fresh-e2e-smoke. Preserve the "
        "single SQLite/supervisor/official-SDK authority. Implement bounded same-thread continuation for "
        "typed 429/5xx with durable increasing backoff and no blind prompt replay; never reset historical "
        "counters silently. Run provider-free gates first, create the required coherent local milestone "
        "commit, then let the integration owner install and authorize exactly one fresh disposable real "
        "smoke. Do not retry the historical attempt-13 dispatch or start the TUI successor."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

### Provider-transient implementation and fresh-smoke checkpoint

The accepted implementation is frozen at program-trunk commit
`ac438a544d9deb7c2173953c99233de23714cb27`.  It comprises the transient-recovery
owner commit `6e8d822fb83fe42af08eb06197ed9704ef7f3a02`, the exact delayed-result recovery
commit `4631ed2008dd40dc64ec1f3b0e99ee6e838216c5`, the terminal-controller
constraint commit `c6186f0b762b74734d8c5527b4726806f07f926d`, and the bounded grant repairs
`aa5810b`, `dba1b05` and `ac438a5`.  The recovered historical worker result
closed without another provider attempt, and its terminal controller generation
acknowledged the exact result without scheduling a retry.

After canonical installation, one newly created disposable real workflow pilot
ran with `gpt-5.6-luna` at medium effort.  It reached the packaged
`codex-flow control` entrypoint, started exactly one worker attempt, changed
only the authorized `workflow_result.txt`, returned a schema-valid completed
result, and received durable `controller_acknowledged`.  The protected sentinel
and Git HEAD remained unchanged; the temporary supervisor shut down cleanly
and the temporary repository was removed.  The sanitized dispatch identity is
SHA-256 `eca3178e08d270cf2ba4ebc852ce78901bd0536ebf3e72433168c5a5e2f57ec9` and
the exact final sentinel SHA-256 is
`be9e35885d26bab9689ab7a97cac19b9eea7deeb4ba071ab394057229ef9caf7`.

The repair synchronizes provider and continuation budgets in one CAS, binds the
grant to the current typed provider failure and exact thread, clears stale
inspection/failure context at ownership transitions, consumes exactly the one
newly authorized continuation and rejects stale non-provider facts.  The fresh
production-shaped regression proves initial attempt, three ordinary
continuations, fourth typed failure, one grant, one final continuation and exact
exhaustion.  The affected partition passed 230 tests and the final full gate
passed 787 tests plus Ruff, validator, compileall and pre-commit.

Independent Luna XHigh objective/correctness and Sol Medium architecture
reviews accepted exact tip `ac438a544d9deb7c2173953c99233de23714cb27` with
P0=0/P1=0.  No visual review was requested.  Because the accepted repair was
committed directly on the program trunk, no separate lane integration action is
required.  The `event-driven-program-controller` successor is ready.

## Next execution — event-driven-program-controller

This milestone is the self-hosting cutover from a manually operated sequence
of single-milestone dispatches to one codex-flow-owned program lifecycle.  Its
`provider-transient-backoff-and-fresh-e2e-smoke` prerequisite is accepted and
integrated at exact trunk tip `ac438a544d9deb7c2173953c99233de23714cb27`.
The dependency was serial because both
milestones own the ledger schema, supervisor event loop, controller action
contract and production CLI.  No second worker may start against these shared
surfaces.

### Current boundary and selected responsibility split

The existing `codex-flow control --plan-path ... --milestone-id ...` compiles,
plans and queues one exact milestone.  The detached supervisor already owns
queueing, leases, worker heartbeats, terminal result ingress, bounded recovery
and source-controller wake-ups.  Its ephemeral controller generations are
event-driven, but their closed action contract is dispatch-local: acknowledge,
checkpoint re-arm, retry, cancellation, retry-budget change or human attention.
The existing `ReviewWorkflow` proves review/repair policy through in-process
callbacks, and `authorized_successors` can release only dispatches created and
authorized in advance.  No production path currently compiles a complete DAG,
starts independent ready nodes, dispatches detached reviewers, promotes an
exact reviewed commit, integrates a promoted lane or derives the next ready
milestones.  Those actions are still performed by the external planning
controller.

The selected architecture keeps the supervisor non-model and deterministic.
It owns SQLite, CAS revisions, outboxes, process identity, leases, readiness
calculation and application of validated effects.  It never judges review
quality, chooses an implementation strategy, resolves a merge conflict or
edits the canonical plan.  A short-lived Sol Medium program-controller model is
started only for a coalesced durable event: implementation terminal result,
review terminal result, integration completion/conflict, controller-attention
condition or one explicitly armed checkpoint.  It receives one bounded,
secretless program summary, commits one closed action bundle and exits.  There
is no long-lived model, timer-driven model polling, model-owned callback,
alternate scheduler or App lifecycle dependency.

Implementation and review workers remain capability-bound leaves.  Reviewers
receive an immutable candidate commit/range and read-only workspace authority
and return one typed `ReviewResult`.  The controller decides whether to request
a same-owner repair, replan, promote or require human authority.  Promotion is
valid only when every declared acceptance authority reviewed the same exact
candidate and no promotion-blocking P0/P1 remains.  A promoted integration is a
typed deterministic effect: the supervisor verifies expected trunk HEAD,
ancestry, lane ownership, clean topology and a conflict-free Git preflight,
then applies the plan-selected merge strategy through the existing worktree
authority.  A conflict or validation failure creates a new controller event;
it is never silently resolved, reverted or blessed.

The canonical Markdown plan remains the static authority for intent, capsules,
dependencies, ownership and acceptance.  A closed `ProgramGraph` compiler
projects that plan without executing it and binds its digest plus every capsule
source digest.  SQLite remains the sole dynamic authority for node state,
candidate revision, reviews, promotion, integration and controller decisions.
Ordinary status changes never rewrite the Markdown plan.  A material change to
intent, public/persisted contract, security/privacy, destructive behavior,
scope or cost emits a typed replan requirement for the planning authority.

### Program state, events and actions

One registered program progresses through the existing workflow states
`planned`, `starting`, `running`, `completed`, `reviewing`, `repair_required`
and `accepted`; integration readiness is an exact revision fact, not a parallel
lifecycle vocabulary.  The forward migration generalizes the existing
controller decision subject from one dispatch to either a dispatch or a
program revision while preserving all prior rows and dispatch-local behavior.
It adds only the dependency edges and integration outbox needed by the missing
runtime behavior.  Existing run, milestone, event, execution, review,
controller-generation and action-receipt authorities are reused rather than
duplicated.

`ProgramControllerActionKind` is closed to: start one or more currently ready
milestones, start the missing declared reviews for one exact candidate, request
a same-owner repair, promote one fully accepted candidate, authorize one exact
integration, require a bounded replan, require human attention or acknowledge
without effects.  `ModelFacingProgramControllerActionBundle` binds program id,
plan digest, decision generation, expected program revision, expected trunk
HEAD, exact candidate/review/integration identities and all affected milestone
revisions.  The ledger rejects stale, cross-program, non-ready, overlapping,
unreviewed, unpromoted or authority-changing bundles before any process or Git
side effect.

After an integration receipt commits and its proportional checks pass, the
supervisor deterministically marks the exact node integrated, recomputes ready
nodes from durable dependency edges and emits at most one coalesced program
decision.  One controller activation may start multiple ready milestones when
their mutable surfaces and workspaces are disjoint; each milestone still has
one owner and one START.  Human-attention worker states first become controller
attention.  User attention is requested only when intent/authority is genuinely
underdetermined or an external/destructive prerequisite requires it.

### Architecture map and artifact budget

- **Modify `src/codex_flow/domain.py`:** own typed program graph/state/event,
  exact candidate/integration identities and `ProgramControllerActionKind`.
- **Modify `src/codex_flow/contracts.py`:** own the closed model-facing program
  action/bundle contract and review-result serialization without changing
  existing capsule/result compatibility.
- **Modify `src/codex_flow/plan_capsule.py`:** add the non-executing
  `CompiledProgramGraph` compiler and exact dependency/ownership validation;
  retain the current single-milestone compiler.
- **Modify `src/codex_flow/ledger.py`:** own one forward schema migration,
  program revision CAS, dependency readiness, review/promotion facts and one
  integration outbox.  Reuse existing lifecycle and controller-generation
  tables wherever their authority already fits.
- **Modify `src/codex_flow/controller.py`:** register one compiled program and
  reuse the existing review policy/types; do not keep an in-process scheduler.
- **Create `src/codex_flow/program_controller.py`:** own only the ephemeral
  schema-bound program-controller generation runner.  It accesses durable state
  and effects solely through the typed client, exactly like dispatch recovery.
- **Modify `src/codex_flow/controller_recovery.py`:** preserve dispatch recovery
  and share only its bounded SDK generation mechanics with the new runner.
- **Modify `src/codex_flow/supervisor.py`:** coalesce program events, spawn at
  most one generation per program revision, launch ready worker/reviewer roles,
  drain the integration outbox and acknowledge exact receipts.
- **Modify `src/codex_flow/worker.py` and
  `src/codex_flow/backends/codex_sdk.py`:** add the read-only reviewer job/result
  variant without granting reviewers mutation or lifecycle authority.
- **Modify `src/codex_flow/worktrees.py`:** verify exact commits, lane ancestry,
  clean trunk and conflict-free integration before the supervisor applies the
  authorized strategy.
- **Modify `src/codex_flow/control_client.py` and `src/codex_flow/cli.py`:** add
  typed program status/control IPC and one `codex-flow program` command group
  for register, start, status and bounded human decisions.
- **Modify the existing workflow-control skill/template, focused controller,
  ledger, plan, supervisor, worker, worktree, IPC and production-pilot tests,
  plus one new focused `tests/test_program_controller.py` and retained
  `docs/reviews/evidence/event-driven-program-controller.json` proof.
- **Preserve** the App transport, native authentication/profile ownership,
  TUI modules, live-conversation protocol, service unit, IPC framing ceiling,
  plugin registry, unrelated evidence, remotes and global state.

The durable-artifact budget is three paths: one production module for the
ephemeral runner, one focused test module and one retained evidence record.
The schema migration may add at most two tables (`milestone_dependencies` and
`integration_outbox`) and must not add a service, daemon, socket, transport,
database, registry, scheduler or parallel lifecycle authority.  Generated JSON
schema remains derived from typed contracts rather than a second handwritten
authority.

### Bootstrap, acceptance and cutover

The external planning controller remains authoritative through this milestone:
it accepts and integrates the active provider-transient predecessor, invokes
this capsule once, dispatches its independent objective and architecture
reviews, repairs at most one concrete blocking set and integrates the accepted
candidate.  Only then does the explicit `codex-flow program start` cutover
register the remaining graph and allow codex-flow's event-driven controller to
start and close `live-coding-agent-terminal-ui`.  Self-hosting evidence must
show the controller process absent while workers merely run and exactly one
controller generation per coalesced terminal event.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make codex-flow the event-driven controller for a complete milestone DAG, including "
        "review, promotion, exact Git integration and successor scheduling without a long-lived model."
    ),
    decomposition=(
        "Compile and durably register one closed canonical program graph while preserving single-milestone callers.",
        "Generalize the existing controller decision CAS to bounded program events and closed program action bundles.",
        "Dispatch ready implementation and read-only review workers, promote only exact fully accepted commits and request bounded repair when blocked.",
        "Apply controller-authorized conflict-free integration through the existing worktree authority and derive readiness only after a verified receipt.",
        "Prove event-triggered controller generations, parallel starts for disjoint ready nodes, restart recovery and a real self-hosted successor cutover.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "The supervisor remains a deterministic non-model process; while work is merely running it consumes zero controller-model turns and never polls a model.",
        "Implementation completion, review completion, integration outcome, controller-attention and one-shot checkpoint events each produce at most one CAS-bound program decision generation, action receipt and acknowledgement across restart.",
        "One controller bundle may start every currently ready milestone with disjoint mutable ownership, while shared surfaces, serial dependencies, stale plan revisions and duplicate STARTs fail before thread creation.",
        "Every declared acceptance authority reviews the same exact candidate commit read-only; promotion rejects stale reviews or any promotion-blocking P0/P1 and routes concrete blockers to one same-owner repair.",
        "Integration verifies expected trunk HEAD, ancestry, lane ownership, clean topology and conflict-free preflight, applies only the authorized strategy and advances DAG readiness only after exact receipt and proportional validation.",
        "Human-attention worker states first receive a bounded controller recovery decision; only underdetermined intent, new authority or an external prerequisite is escalated to the user.",
        "The canonical plan is static intent authority and SQLite is sole dynamic authority; no second scheduler, transport, daemon, database, App lifecycle dependency or model-owned callback is introduced.",
        "Focused migration/contract/DAG/review/integration/restart tests, affected semantic partitions, full make check, exact-wheel installation and one real self-hosted program smoke close with P0=0/P1=0.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/contracts.py",
        "src/codex_flow/plan_capsule.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/program_controller.py",
        "src/codex_flow/controller_recovery.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/worktrees.py",
        "src/codex_flow/control_client.py",
        "src/codex_flow/cli.py",
        "plugins/personal-workflow-skills/skills/workflow-control/SKILL.md",
        "templates/AGENTS.workflow.md",
        "tests/test_program_controller.py and existing focused contract, plan, ledger, controller, supervisor, worker, worktree, IPC and production-pilot tests",
        "docs/reviews/evidence/event-driven-program-controller.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md after this capsule is frozen and AGENTS.md",
        "TUI modules and live-conversation protocol owned by the blocked successor",
        "service unit, IPC framing and 64 KiB ceiling, native profile/authentication and App transport",
        "plugin registry/manifests, unrelated skills/evidence, retained wheels and packaging dependencies",
        "global Codex/App state, remotes, pushes, rebases, history rewrites, implicit cleanup and unrelated dirty bytes",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only event-driven-program-controller after the provider-transient "
        "predecessor is accepted and integrated. Implement the frozen deterministic-supervisor plus "
        "ephemeral-controller architecture in the existing program trunk. Reuse the single SQLite, SDK, "
        "review, worktree and supervisor authorities; add no second scheduler, transport, service or App "
        "dependency. Create the exact bounded artifacts, commit only owned surfaces, run provider-free gates "
        "before the one authorized self-hosted program smoke, and return one typed terminal result. Do not "
        "start or modify the live-coding-agent-terminal-ui successor."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

## Next execution — event-driven-program-controller-review-repair

The fixed implementation commit
`43901ab2f9e42e95e483a2eea5a954706cc873a8` passed the independent
architecture/provenance review with P0=0/P1=0, but its independent
objective/correctness review found four promotion-blocking lifecycle and
concurrency defects.  This is one bounded same-owner repair before the
self-hosted cutover.  It does not reopen the accepted program-controller
architecture, start the terminal-UI successor, or add another scheduler,
transport, database, service, public command group or durable artifact.

The supervisor must remain event driven when recovery inspection observes an
active SDK writer: that durable `active` outcome suppresses further recovery
children until a terminal callback or explicitly armed checkpoint creates a
new event.  An external program action effect may finish as applied or failed;
both are terminal effect facts.  Once every effect is terminal, the original
committed action is acknowledged exactly once, while one coalesced typed
controller-attention event owns any recovery from failed effects.  Restart
must neither repeat applied/failed effects nor strand the original outbox.

Expected worktree, controller or integration failures are typed control-plane
outcomes.  They must be redacted at the authenticated IPC boundary and may
never escape the request handler or terminate the foreground supervisor loop.
Git integration remains fail closed under concurrency: serialize codex-flow
integration attempts through the existing controller mutation authority,
revalidate the exact predecessor immediately before mutation, and verify the
strategy-specific resulting HEAD, parents and tree before committing a success
receipt.  A non-cooperating external Git mutation is detected as conflict and
is never accepted, reverted, or rewritten by codex-flow.

The implementation architecture map is:

- **Modify `src/codex_flow/supervisor.py`:** gate program recovery scheduling
  on terminal inspection state; terminalize/reconcile effect application; and
  close the program IPC exception boundary without weakening typed errors.
- **Modify `src/codex_flow/ledger.py`:** make all-terminal applied/failed
  effect sets acknowledgeable exactly once and preserve one coalesced recovery
  attention fact using the existing decision, outbox and lifecycle tables.
- **Modify `src/codex_flow/worktrees.py`:** bind integration mutation and its
  receipt to the exact expected predecessor and strategy-specific post-state,
  without reverting an unknown concurrent advancement.
- **Modify focused existing tests only:**
  `tests/test_program_controller.py`, `tests/test_supervisor_recovery.py` and,
  only if needed for the existing mutation-lock boundary,
  `tests/test_controller_execution.py`.
- **Modify** the existing retained
  `docs/reviews/evidence/event-driven-program-controller.json` after all gates
  pass.  Create no new production module, schema table, evidence path, lock
  authority, CLI entrypoint or package dependency.
- **Preserve** the canonical plan after this capsule is frozen, AGENTS.md,
  program graph/action public contracts, TUI/live-conversation modules,
  service/IPC framing, native profile/authentication, plugin authority,
  unrelated evidence, remotes and global App/Codex state.

The semantic delta is limited to three existing invariants: an inspected active
writer is deferred rather than polled; an effect is terminal when applied or
failed; and an integration receipt proves the actual strategy-specific Git
post-state.  No new public class, protocol, enum, state vocabulary or synonym
result family is authorized.  If the existing types cannot express one of
those facts without ambiguity, return a bounded replan before adding
vocabulary.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Close the event-driven program controller's active-recovery, failed-effect, IPC and Git "
        "concurrency defects without changing its accepted authority model."
    ),
    decomposition=(
        "Defer an inspected active controller writer until a new durable event instead of respawning recovery children.",
        "Terminalize applied and failed external effects exactly once, acknowledge the original action and emit one recovery attention event.",
        "Contain expected program-effect failures at the authenticated IPC boundary while keeping the supervisor available.",
        "Bind integration mutation and success receipts to the exact expected predecessor and strategy-specific post-state under concurrency.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "An active SDK controller writer produces at most one recovery inspection and no repeated child spawn until a terminal callback or explicitly armed checkpoint changes durable state.",
        "Applied and failed effect facts are replay-idempotent; once every effect is terminal the original outbox closes exactly once and one coalesced controller-attention decision owns failed-effect recovery.",
        "Expected controller, worktree and integration effect failures return one closed redacted IPC rejection, leave durable recovery facts and do not exit or starve the supervisor foreground loop.",
        "Each merge, fast-forward or cherry-pick success receipt proves the authorized predecessor plus exact resulting HEAD, parents and tree; concurrent unknown advancement is a conflict and is never accepted or reverted.",
        "Focused active-writer, restart, partial-effect, live-IPC and concurrent-Git adversaries pass together with affected partitions, full make check, exact-wheel parity and P0=0/P1=0 objective and architecture reviews.",
        "The worker creates one coherent local commit over only owned surfaces, performs no provider-independent successor dispatch and returns one raw schema-v1 ModelFacingResult."
    ),
    mutable_surfaces=(
        "src/codex_flow/supervisor.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/worktrees.py",
        "tests/test_program_controller.py",
        "tests/test_supervisor_recovery.py",
        "tests/test_controller_execution.py only if required by the existing mutation-lock boundary",
        "docs/reviews/evidence/event-driven-program-controller.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md after this capsule is frozen and AGENTS.md",
        "domain and model-facing public contracts, plan compiler, controller and worker modules",
        "TUI/live-conversation modules, service and IPC framing, native profile/authentication and plugin authority",
        "schemas, migrations, new production modules or entrypoints, dependencies and unrelated evidence",
        "global Codex/App state, remotes, pushes, rebases, history rewrites, implicit cleanup and unrelated dirty bytes",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only event-driven-program-controller-review-repair in the existing "
        "python-sdk-controller integration trunk. Start from exact predecessor 43901ab2f9e42e95e483a2eea5a954706cc873a8 "
        "and close PROGRAM-ACTIVE-RECOVERY-LOOP-001, PROGRAM-EFFECT-ACK-STUCK-001, "
        "PROGRAM-IPC-CRASH-ON-EFFECT-ERROR-001 and PROGRAM-INTEGRATION-TOCTOU-001. Preserve the "
        "deterministic supervisor, single SQLite authority and closed public contracts; create no new "
        "module, schema table, command group, service or transport. Commit only owned surfaces, run all "
        "named gates, do not start the terminal-UI successor, and return exactly one typed terminal result."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

### Event-driven controller promotion

The bounded controller recovery is accepted at exact program-trunk commit
`7833c01b2908dff63c9fb45c7280c1996e275b79`.  Independent objective and
architecture reviews both report P0=0/P1=0.  Post-CAS recovery now synchronizes
the checkout only while its index, tracked files and relevant untracked state
still match the exact pre-CAS authority; external work is preserved and routed
to one typed attention event.  The retained wheel is SHA-256
`0b12f4a62bd6cd36d654034ca650407bfb08e7493e78f52a387b82a4921b0250`,
421586 bytes.  The canonical installer and protected supervisor refresh have
activated these bytes at supervisor epoch 46 with no active worker lease or
refresh fence. The terminal-UI successor was executed directly in the serial
program worktree.

## Next execution — live-coding-agent-terminal-ui

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make the codex-flow TUI behave like a modern coding-agent terminal by streaming "
        "the active assistant response and inline tool-call state without polling or alternate authority."
    ),
    decomposition=(
        "Project documented assistant-text and tool lifecycle events from the official SDK into bounded non-persisted typed values.",
        "Coalesce each active turn into cumulative redacted keyframes while preserving heartbeat, control, lease and terminal-result priority.",
        "Carry exact identity-bound keyframes through one bounded authenticated IPC subscription and an ephemeral supervisor broker.",
        "Update the selected TUI conversation in place, render tool state inline and reconcile terminal or reconnected views from stable Thread.read history.",
        "Prove realistic streaming, slow-consumer loss, reconnect, privacy, controls and exact 80x24 and wide rendering without a timer or polling loop.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "During one real delayed SDK turn the selected worker's assistant text grows in place and each tool call progresses through typed visible states without manual refresh, one row per token or App APIs.",
        "Cumulative keyframes bind dispatch, generation, attempt, thread, turn and live revision; stale, replaced, replayed, cross-subject or post-terminal frames are discarded before display.",
        "A slow or disconnected TUI cannot delay supervisor lease renewal, worker heartbeat, steer or interrupt acknowledgement, terminal result ingress or successor scheduling; only intermediate live frames may be lost.",
        "Raw live transcript and tool output are never written to SQLite, logs, evidence or recovery state; redaction, typed path/URL/image presence and memory/frame/subscriber bounds fail closed without altering stable history.",
        "Reconnect and terminalization discard ephemeral state and reconstruct the complete persisted user/agent conversation from official Thread.read in SDK order, without diagnostics or deltas filling gaps.",
        "Wide light/dark and exact 80x24 light/no-color renders retain controller-over-workers hierarchy, readable progressive assistant text, inline tool state, scrolling/load older and safe actions with no important zero-height panel.",
        "Focused adapter, worker, supervisor, IPC, client and TUI tests, a provider-free slow-consumer stress proof, one authorized real streaming sentinel, affected partitions, full make check, exact-wheel service proof and objective/architecture reviews close with P0=0/P1=0; the cancelled independent visual review is not reinstated.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/control_client.py",
        "src/codex_flow/tui_client.py",
        "src/codex_flow/tui_models.py",
        "src/codex_flow/tui.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_local_ipc.py",
        "tests/test_supervisor_recovery.py",
        "tests/test_live_worker_control.py",
        "tests/test_workflow_control.py",
        "tests/test_production_pilots.py",
        "tests/test_plan_compilation.py",
        "docs/reviews/evidence/human-terminal-ui.json",
        "docs/reviews/evidence/human-terminal-ui/conversation-first-visual-contract.md",
        "docs/reviews/evidence/human-terminal-ui/conversation-first-visual-contract.yaml",
        "docs/reviews/evidence/human-terminal-ui/conversation-light-wide.svg",
        "docs/reviews/evidence/human-terminal-ui/conversation-dark-wide.svg",
        "docs/reviews/evidence/human-terminal-ui/conversation-light-80x24.svg",
        "docs/reviews/evidence/human-terminal-ui/conversation-no-color-80x24.svg",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "src/codex_flow/ledger.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/program_controller.py",
        "src/codex_flow/contracts.py",
        "src/codex_flow/plan_capsule.py",
        "src/codex_flow/projection.py",
        "src/codex_flow/service.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/native_profile.py",
        "src/codex_flow/app_native.py",
        "pyproject.toml",
        "uv.lock",
        "Makefile",
        "workflow.toml",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only live-coding-agent-terminal-ui in the registered python-sdk-controller "
        "program worktree after the accepted event-driven controller cutover. Preserve the frozen architecture "
        "and zero durable-artifact budget. Keep Textual, "
        "the official SDK and the single supervisor/SQLite authority; add no polling, alternate TUI, "
        "transport, module, schema, table, dependency or App lifecycle call. Stream only bounded redacted "
        "ephemeral cumulative assistant/tool keyframes, prioritize terminal result ingress, reconcile from "
        "Thread.read, run the exact objective/architecture gates and return one typed terminal result."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

### Terminal-UI candidate and recovery diagnosis

The implementation is a coherent local serial commit
`925d505d55e963c1d01dd541f67c939f1f716e5c`. Its provider-free implementation
and package gates passed, but its worker truthfully returned
`external_blocked` because the separately authorized real delayed-streaming
sentinel and independent objective/architecture reviews remained outstanding.
That outcome exposed a controller defect rather than invalidating the commit.

Program `codex-flow-remaining-milestones` currently records durable trunk
`05516e208225aadd1f87343c41bd8e345170baec`, revision 4, program state
`needs_decision`, and the terminal-UI node as `FAILED` with no candidate. The
physical program worktree nevertheless contains exact candidate `925d505`.
The harness currently records a candidate only when the model result status is
`completed`; consequently the controller cannot review, repair, adopt or
promote useful committed work returned as `external_blocked`,
`needs_decision` or `failed`. The next milestone repairs that state model
before any retry of the streaming worker or either later milestone.

## Next execution — terminal-candidate-retention-and-harness-cutover

This serial repair separates three facts that are currently conflated:
executor outcome, candidate existence, and promotion readiness. Every terminal
worker result first closes the SDK/IPC attempt, then the deterministic harness
inspects the exact execution workspace and records either one verified commit
candidate or an explicit preserved-dirty-workspace fact. It never resets,
deletes or hides work because a worker returned a non-success status. The
worker's `next_action` remains advisory; the event-driven controller owns
review, repair, replan, adoption, promotion, integration or abandonment.

Candidate-bearing terminal outcomes map to milestone state without inventing
success: `completed` becomes `COMPLETED`, `external_blocked` becomes `BLOCKED`,
`needs_decision` becomes `NEEDS_DECISION`, and `failed` becomes
`REPAIR_REQUIRED`. A `failed` result without a coherent commit remains
`FAILED`; any dirty workspace is preserved and identified but is not promoted.
Review and same-owner repair may target any verified candidate. Direct
promotion still requires the milestone's exact objective/architecture gates
and P0/P1 zero, regardless of the worker's suggested outcome.

A new closed typed blocker projection binds `gate_id`, `kind` (`external`,
`decision`, or `execution`), `scope` (`current_promotion`, `current_repair`,
`future_milestone`, or `whole_program`), `promotion_blocking`, and a bounded
`required_action`. A future-milestone decision cannot block review or promotion
of the current candidate or an independent ready DAG lane. Missing or malformed
blocker facts fail closed as an execution blocker for the current milestone,
without erasing the candidate. Raw provider text, transcript, tool output and
secrets never enter these facts.

The same milestone performs the requested forward-only terminology cutover.
The deterministic process becomes `WorkflowHarness` in `harness.py`; the CLI
is `codex-flow harness`; the private entrypoint, service description, socket,
authority and current persisted identifiers use `harness`. `Supervisor`,
`supervisor.py`, the `supervisor` command and the old socket are removed with no
runtime alias, fallback import, dual read or dual command. Historical immutable
schema/evidence text may retain its published spelling only as provenance. A
single forward schema migration converts live current authority rows and
recovery state before the replacement harness starts; migration failure leaves
the old installed process stopped and the new process unstarted rather than
serving mixed identities.

The Git policy is hybrid and uses ordinary Git. Serial milestones, including
this one, commit directly in the program integration worktree and need only a
logical promotion receipt; parallel mutable milestones use semantic sibling
worktrees from the exact promoted base and require controller-authorized
integration through the existing `WorktreeManager`. The harness owns workspace
creation, exact-HEAD/CAS verification, deterministic integration and later
cleanup. Workers use normal `git status`, `git diff`, `git add` and `git
commit`; no wrapper VCS, automatic push, rebase, reset or history rewrite is
introduced.

The one bounded existing-program recovery verifies that `925d505` descends
from durable trunk `05516e2`, matches the terminal-UI dispatch workspace and
contains no unrelated commit before registering it as that node's candidate.
It does not replay the worker, rerun the consumed provider attempt or treat the
candidate as promoted. The controller can then start the declared objective
and architecture reviews; the cancelled visual review is not reinstated.

### Review repair and concurrent-baseline reconciliation

The first fixed candidate `a2230ad79d2883d8bb2c5955b37709cce0e12315`
received one objective P1 and four architecture P1 findings. All five are one
same-owner serial repair; the tip is not promoted or installed. The repair
must close:

- `CANDIDATE-PROMOTION-BLOCKER-BYPASS`: `PROMOTE_CANDIDATE` must evaluate the
  current exact candidate's durable terminal blocker in addition to review
  findings. A `promotion_blocking` blocker permits review and repair but never
  promotion until one typed controller action resolves or supersedes it.
- `ARCH-P1-002`: migration from an installed v18 predecessor is an explicit
  one-shot transition. The refresh path validates the exact legacy unit,
  connects only to its verified `supervisor.sock`, arms and observes the old
  shutdown fence, waits for exact PID/unit inactivity, then performs the v19
  migration, replaces the unit/command/socket and starts `harness`. This
  migration-only legacy handoff is deleted from reachability after success and
  is not a runtime alias, dual command, dual socket listener or fallback.
- `ARCH-P1-003`: before candidate recording and again before
  promotion/integration, `WorktreeManager` validates every commit and changed
  path from the capsule base against exact mutable/protected ownership. An
  out-of-scope or protected committed path produces a typed candidate-integrity
  blocker and never `VERIFIED_COMMIT`.
- `ARCH-P1-004`: retained evidence binds the final successor tip and exact
  reviewed range, while preserving `9c3dcbc` and `a2230ad` only as superseded
  predecessor provenance. A later repair commit cannot inherit their review
  identity.
- `ARCH-P1-001`: before the worker START, three concurrent dirty paths were
  observed and explicitly excluded from the planning commit:
  `plugins/personal-workflow-skills/skills/collect-evidence/SKILL.md`,
  `plugins/personal-workflow-skills/skills/recover-milestone/SKILL.md`, and
  `schemas/evidence.schema.json`. They were subsequently committed by their
  separate owner as `6fa09a3701d97f39a5183701687b4b8bcf62dc17` while this
  serial task was outstanding. This plan neither claims nor reverts those
  bytes. For this milestone, `6fa09a3` is the frozen external trunk baseline;
  implementation ownership and evidence begin strictly after it. Reviewers
  verify that boundary separately and review the final successor range
  `6fa09a3..final-tip`, without attributing the baseline commit to this owner.

The failed installed v18 service and already-migrated real checkout ledger are
diagnostic state, not implementation evidence. Repair tests use disposable
units and ledgers only. After fresh objective and architecture acceptance, the
integration owner performs one controlled installation/recovery of the real
unit; the implementation worker does not mutate it.

The implementation architecture map is:

- Rename `src/codex_flow/supervisor.py` to `src/codex_flow/harness.py` and
  `Supervisor`/`SupervisorError` to `WorkflowHarness`/`HarnessError`; this
  module remains the only deterministic queue, lease, child-process, IPC,
  candidate-inspection and program-effect runtime owner.
- Modify `src/codex_flow/domain.py` and `src/codex_flow/contracts.py` for the
  closed candidate/blocker values and controller projection. Do not change the
  four model result status strings or ask the model to serialize Git facts.
- Modify `src/codex_flow/ledger.py` through one forward migration and atomic
  result-plus-candidate transition. Keep SQLite as the only durable state
  authority, reject promotion while the exact candidate retains a blocking
  terminal fact, and keep last promoted trunk SHA distinct from physical
  candidate HEAD.
- Modify `src/codex_flow/program_controller.py` and
  `src/codex_flow/worktrees.py` only for candidate-bearing state transitions,
  exact committed-path/history ownership validation, serial logical promotion
  and parallel integration validation.
- Modify `src/codex_flow/cli.py`, `src/codex_flow/service.py`, worker/control
  clients and internal imports for the no-alias harness cutover and one typed
  candidate adoption/recovery command. The service remains one repository unit;
  refresh handles one verified legacy predecessor as a migration-only handoff
  and remains forbidden while a worker or controller child is active.
- Rename focused supervisor test/evidence terminology where it is current,
  retain immutable historical names where provenance requires them, and add
  one semantic evidence record
  `docs/reviews/evidence/terminal-candidate-retention-and-harness-cutover.json`.

The durable-artifact budget is one forward migration and one evidence record;
the module and focused test renames are replacements, not parallel artifacts.
New semantic vocabulary is limited to the verified candidate disposition and
the typed blocker because they encode distinct Git/recovery and gating
invariants. No second database, scheduler, transport, service, model loop,
worktree manager or compatibility facade may be added.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Preserve every coherent terminal worker candidate independently of executor outcome, let the "
        "controller recover it safely, and complete the no-alias supervisor-to-harness cutover."
    ),
    decomposition=(
        "Record a verified commit or preserved dirty-workspace fact atomically for every terminal worker status.",
        "Project typed blocker kind, scope and promotion impact so the controller can review, repair, replan, adopt or abandon without losing work.",
        "Validate every committed candidate path and terminal promotion blocker before candidate recording, promotion or integration.",
        "Implement serial in-place logical promotion and retain existing exact-worktree integration for parallel mutable lanes.",
        "Rename the deterministic runtime, CLI, service, socket and current persisted authority from supervisor to harness through one fenced migration-only legacy handoff with no surviving alias.",
        "Recover exact terminal-UI commit 925d505 as an unpromoted candidate and prove the controller can start its objective and architecture reviews without replaying the worker.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Completed, external_blocked, needs_decision and failed worker results each preserve a coherent exact workspace commit when present; dirty or commitless failure remains inspectable and never becomes a fabricated candidate.",
        "A candidate-bearing external block, decision or failure can be reviewed and repaired by the controller, while promotion still requires the exact declared review authorities and zero promotion-blocking P0/P1 findings.",
        "A current candidate with a durable promotion-blocking terminal blocker cannot be promoted or integrated after clean reviews until one revision-bound typed action resolves or supersedes that blocker.",
        "Typed blocker scope prevents a future-milestone prerequisite from blocking the current candidate or independent ready nodes; malformed, stale and cross-program blocker/candidate facts fail closed.",
        "Candidate recording and promotion reject any commit range that changes a path outside capsule mutable ownership or inside protected ownership, including a clean committed out-of-scope edit.",
        "A serial candidate already committed in the program worktree advances by logical promotion without a same-checkout Git mutation; a parallel candidate still requires exact-base controller-authorized WorktreeManager integration.",
        "One exact installed v18 unit is fenced and stopped through its verified legacy socket before migration; afterward only codex-flow harness, WorkflowHarness, harness.py and the harness socket/current authority remain reachable, and old commands/imports/socket/current persisted identities fail rather than aliasing.",
        "Exact commit 925d505 is adopted only after ancestry, dispatch-workspace and unrelated-commit validation, remains unpromoted, and becomes eligible for objective and architecture review without a provider retry or visual review.",
        "Evidence binds the final successor commit and exact review range after external baseline 6fa09a3; predecessor candidates remain historical and the three baseline plugin/evidence-schema paths are neither claimed nor reverted.",
        "Focused result, ledger migration, controller, harness, IPC, service, worktree and CLI tests, affected semantic partitions, exact-wheel install/refresh proof, full make check and independent objective/architecture reviews close with P0=0/P1=0.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/contracts.py",
        "schemas/result.schema.json",
        "src/codex_flow/ledger.py",
        "src/codex_flow/supervisor.py -> src/codex_flow/harness.py",
        "src/codex_flow/program_controller.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/controller_recovery.py",
        "src/codex_flow/worktrees.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/service.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/control_client.py",
        "src/codex_flow/tui_client.py",
        "src/codex_flow/tui_models.py",
        "src/codex_flow/tui.py",
        "src/codex_flow/projection.py",
        "src/codex_flow/live_control_sentinel.py",
        "src/codex_flow/workflow_control_pilot.py",
        "scripts/install_personal_workflow_skills.py",
        "scripts/validate.py",
        "README.md",
        "Makefile",
        "workflow.toml",
        "config/test-partitions.toml",
        "tests/test_supervisor_recovery.py -> tests/test_harness_recovery.py",
        "tests/test_program_controller.py",
        "tests/test_ledger_integrity.py",
        "tests/test_live_worker_control.py",
        "tests/test_local_ipc.py",
        "tests/test_controller_execution.py",
        "tests/test_controller_turn_recovery.py",
        "tests/test_descriptive_naming.py",
        "tests/test_production_pilots.py",
        "tests/test_plan_compilation.py",
        "tests/test_plugin_installation.py",
        "tests/test_public_api_semantic_cutover.py",
        "tests/test_service_lifecycle.py",
        "docs/reviews/evidence/terminal-candidate-retention-and-harness-cutover.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "src/codex_flow/backends/codex_sdk.py and live TUI event projection",
        "model-facing capsule/result status compatibility outside the additive typed blocker projection",
        "native authentication/profile and plugin capability authority",
        "historical immutable evidence and migration definitions",
        "plugins/personal-workflow-skills/skills/collect-evidence/SKILL.md, plugins/personal-workflow-skills/skills/recover-milestone/SKILL.md and schemas/evidence.schema.json at external baseline 6fa09a3",
        "global Codex/App state, remotes, pushes, rebases, resets, history rewrites and implicit cleanup",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only terminal-candidate-retention-and-harness-cutover in the existing "
        "python-sdk-controller program integration worktree. Preserve the serial terminal-UI commit 925d505 "
        "and all unrelated bytes. Separate result status, candidate existence and promotion readiness; add "
        "typed blocker scope; support direct serial logical promotion and existing parallel WorktreeManager "
        "integration. Rename supervisor to harness everywhere current with no command/import/socket/persisted "
        "alias, using one forward migration. Do not replay the TUI worker, consume a provider attempt, run a "
        "visual review, push, rebase, reset or clean work. Close all five named P1 findings, including terminal-blocker promotion gating, committed-path ownership validation, exact final-tip evidence and the migration-only v18 unit/socket handoff. Treat 6fa09a3 as the external baseline and neither claim nor revert its three paths. Run all named gates, stage only owned surfaces, inspect "
        "the staged diff, create one coherent local commit and return one terminal result with reviews pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

## Next execution — live-plan-dag-revision

This serial successor makes the canonical plan safely revisable while a
program is running. It starts only after the terminal-candidate/harness repair
and the terminal-UI candidate are accepted because these milestones touch the
program action contract, harness IPC and shared domain types. The user-facing rule is
explicit: future, never-started milestones may be changed without waiting for
an unrelated active worker; a milestone whose immutable capsule has already
been dispatched changes only through one typed durable `ReplanNotice`.

The Markdown plan remains the sole static authority for intent, architecture,
ownership, dependencies and acceptance.  SQLite remains the sole dynamic
authority for program revision, node state, dispatch identity, results,
reviews and integration.  A replan compiles the complete new graph through the
existing non-executing plan parser, computes a closed old-to-new graph delta
and applies it with expected program revision, old and new plan digest and
exact trunk HEAD.  It never executes Markdown, asks a model to serialize JSON,
or creates a second scheduler, plan store or controller.

Never-started nodes in `PLANNED` or dependency-blocked state may be added,
removed or replaced atomically when they have no dispatch, candidate, review,
promotion, integration or external-effect fact.  Completed, accepted or
integrated nodes are immutable historical authority.  Independent active
nodes continue under their issued capsule while future-node changes become
visible immediately.  When the notice changes the active node itself, its
only v1 disposition is `supersede_after_terminal`: the current SDK writer is
not interrupted, refreshed or given a changed contract.  Its terminal result
and worktree commit are retained, integration under the old capsule is
forbidden, and the new revision continues as a same-lane repair from that
exact candidate.  A missing terminal result, ambiguous writer or dirty
workspace becomes controller attention; no blind replacement is started.

`ReplanNotice` binds program id, expected program revision, old and new plan
digests, exact trunk HEAD, ordered node changes, dependency changes, active
disposition and a bounded reason.  The forward schema migration adds one
`program_replan_notices` table inside the existing ledger solely to preserve a
pending active-node transition and its applied/rejected receipt.  It is not a
plan replica: the full compiled graph remains in the existing `runs` program
record, and notice payloads contain identities and deltas rather than prompts
or transcripts.  At most one unapplied notice exists per program revision.

The implementation architecture map is:

- Modify `src/codex_flow/domain.py` for `ReplanNotice`, closed node-change and
  active-disposition values only.
- Modify `src/codex_flow/contracts.py` for the model-facing request/replan
  action branch without changing capsule or result compatibility.
- Modify `src/codex_flow/plan_capsule.py` for a pure complete-graph diff that
  accepts only exact `Next execution` capsules and explicit dependencies.
- Modify `src/codex_flow/ledger.py` for the one forward migration, program
  revision CAS, future-node replacement and pending active-node transition.
- Modify `src/codex_flow/harness.py` and
  `src/codex_flow/program_controller.py` only to apply/reconcile the notice at
  event boundaries; neither polls nor edits Markdown.
- Modify `src/codex_flow/cli.py` and `src/codex_flow/control_client.py` for one
  authenticated `codex-flow program replan` command using the compiled plan,
  expected revision and disposition.
- Modify the existing program, plan-compilation, ledger, harness, CLI and
  production-pilot tests, and create the one semantic retained evidence record
  `docs/reviews/evidence/live-plan-dag-revision.json`.
- Preserve worker SDK execution, TUI rendering/history, Git integration,
  service lifecycle, native authentication/profile, plugin authority, model
  routing, unrelated evidence and App/global state.

The durable-artifact budget is one schema table and one evidence record.  No
production module, database, daemon, service, socket, transport, registry or
alternate plan file may be added.  New vocabulary is limited to the notice,
its node delta and its active disposition because each carries a persisted
revision or transition invariant.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Allow a running codex-flow program to adopt a revised canonical milestone DAG through one "
        "typed durable ReplanNotice without interrupting unrelated active work or creating another authority."
    ),
    decomposition=(
        "Compile an exact old-to-new complete graph delta from the canonical plan without executing plan text.",
        "Apply future-node additions, removals, capsule changes and dependency changes atomically under program-revision CAS.",
        "Defer an active-node replacement until its current immutable dispatch terminates, then continue from its retained candidate as a same-lane repair.",
        "Expose one authenticated program replan command and reconcile notices through the existing event-driven harness.",
        "Prove restart, stale notice, cross-program, active-writer, historical-node and independent-worker behavior with no polling or lost work.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "A revision-bound notice may change or add never-started future nodes while an independent worker remains active; that worker's dispatch, lease, capsule and result contract remain byte-identical.",
        "A notice that changes the active node is durable and pending until its exact dispatch terminates; the old candidate and commit are retained, old-revision integration is forbidden and the new revision starts only one same-lane repair.",
        "Accepted, integrated or otherwise historical nodes cannot be rewritten or removed, and stale revision, digest, trunk, cross-program, overlapping-ownership or cyclic graph changes fail before mutation.",
        "Markdown remains static intent authority and the existing SQLite ledger remains dynamic authority; notice rows retain no transcript, tool output, secret or duplicate full plan.",
        "Restart before and after notice application is idempotent, one program revision has at most one pending notice, and no timer, polling controller or second scheduler is introduced.",
        "Focused compiler/ledger/harness/CLI tests, affected semantic partitions, migration compatibility, exact-wheel parity, full make check and independent objective/architecture reviews close with P0=0/P1=0.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/contracts.py",
        "src/codex_flow/plan_capsule.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/harness.py",
        "src/codex_flow/program_controller.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/control_client.py",
        "tests/test_plan_compilation.py",
        "tests/test_program_controller.py",
        "tests/test_ledger_integrity.py",
        "tests/test_harness_recovery.py",
        "tests/test_controller_execution.py",
        "tests/test_production_pilots.py",
        "docs/reviews/evidence/live-plan-dag-revision.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "src/codex_flow/worker.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/tui.py",
        "src/codex_flow/tui_client.py",
        "src/codex_flow/tui_models.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/service.py",
        "src/codex_flow/native_profile.py",
        "src/codex_flow/worktrees.py",
        "pyproject.toml",
        "uv.lock",
        "Makefile",
        "workflow.toml",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only live-plan-dag-revision after live-coding-agent-terminal-ui is "
        "accepted and integrated. Implement the frozen ReplanNotice architecture with one forward migration "
        "and no second plan, scheduler, controller or transport. Future untouched nodes may change immediately; "
        "an active node keeps its immutable worker and changes only after its exact terminal result, preserving "
        "the candidate as a same-lane repair. Run all named gates, commit only owned surfaces and return one typed result."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

## Next execution — module-responsibility-decomposition

This successor reduces the accidental coupling in the largest Python modules
after live DAG revision is accepted and integrated.  It is deliberately a
behavior-preserving structural milestone, not a rewrite of SQLite, harness
lifecycle or public APIs.  The dependency is serial because the preceding two
milestones add their final live-view and replan types to the same source
modules; extracting before those contracts settle would create duplicate
movement and unstable ownership.

The first bounded decomposition targets four already coherent boundaries:

- Create `src/codex_flow/ledger_schema.py` for schema versions, canonical DDL,
  migration definitions, schema inventory/fingerprint and read-only
  compatibility inspection.  `Ledger` remains the sole connection,
  transaction, lifecycle and record authority.
- Create `src/codex_flow/conversation.py` for conversation/live-view value
  types, redaction and JSON codecs.  `domain.py` retains compatibility
  re-exports, identifiers and workflow/program lifecycle types.
- Create `src/codex_flow/capsule_codec.py` for pure execution-capsule and
  execution-record JSON conversion currently embedded in `controller.py`.
  `Controller` retains execution, validation, recovery and Git authority.
- Create `src/codex_flow/control_decoding.py` for closed response-shape and
  typed IPC decoding currently embedded in `control_client.py`.
  `LiveWorkerControlClient` and `ControllerDecisionClient` retain transport,
  authentication and command methods.

Dependencies point inward from the existing facade modules to these pure
boundary modules.  The new modules must not import `Ledger`, `WorkflowHarness`,
`Controller` or a concrete control client, open SQLite, spawn processes, touch
Git, own sockets or add runtime configuration.  Existing documented imports
remain valid through explicit re-exports; there is one canonical class object
per public type, not parallel legacy/new representations.  Circular imports,
runtime import fallbacks, star exports and compatibility copies are forbidden.

The durable-artifact budget is the four named production modules and one
semantic evidence record
`docs/reviews/evidence/module-responsibility-decomposition.json`.  No public
class, protocol, state enum, schema table, command, dependency, service or
configuration file is added.  Line counts and import-graph size are diagnostic
only; promotion depends on preserved behavior, identity, ownership and a
strictly acyclic import graph.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Split codex-flow's largest modules along four existing responsibility boundaries while preserving "
        "one lifecycle authority, public type identity and all observable behavior."
    ),
    decomposition=(
        "Extract static SQLite schema definition and compatibility inspection from the transactional Ledger implementation.",
        "Extract conversation and live-view values, redaction and codecs from the general workflow domain module.",
        "Extract pure capsule/record serialization from controller execution and pure closed IPC decoding from control clients.",
        "Preserve explicit compatibility re-exports and prove there is one class/type identity and one production call path.",
        "Delete the moved definitions from their former modules and verify an acyclic dependency direction without fallback imports.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Every selected definition has exactly one implementation in its responsibility module; old supported import paths resolve to the same object identity and no duplicate compatibility implementation remains.",
        "Ledger alone owns SQLite connections, transactions, migration execution and records; extracted schema code is pure data/inspection and introduces no store or lifecycle authority.",
        "Controller and control clients retain all side effects, process/Git/transport ownership and public commands; the extracted codecs/decoders are pure closed boundary functions.",
        "The conversation and live-view production paths retain exact ordering, paging, redaction, subscription identity and memory bounds after extraction.",
        "The import graph for the five affected source modules and four new modules is acyclic with no star import, dynamic import, getattr interface fallback or test-only production seam.",
        "Focused import-identity, schema migration, controller, IPC, conversation and TUI tests, affected semantic partitions, exact-wheel source parity, full make check and independent objective/architecture reviews close with P0=0/P1=0.",
    ),
    mutable_surfaces=(
        "src/codex_flow/ledger_schema.py",
        "src/codex_flow/conversation.py",
        "src/codex_flow/capsule_codec.py",
        "src/codex_flow/control_decoding.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/domain.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/control_client.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/harness.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/tui_client.py",
        "src/codex_flow/tui_models.py",
        "src/codex_flow/tui.py",
        "tests/test_h2_ledger.py",
        "tests/test_ledger_integrity.py",
        "tests/test_controller_execution.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_local_ipc.py",
        "tests/test_live_worker_control.py",
        "tests/test_workflow_control.py",
        "tests/test_program_controller.py",
        "tests/test_production_pilots.py",
        "docs/reviews/evidence/module-responsibility-decomposition.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "src/codex_flow/program_controller.py",
        "src/codex_flow/worktrees.py",
        "src/codex_flow/service.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/native_profile.py",
        "src/codex_flow/app_native.py",
        "src/codex_flow/contracts.py",
        "src/codex_flow/plan_capsule.py",
        "src/codex_flow/projection.py",
        "pyproject.toml",
        "uv.lock",
        "Makefile",
        "workflow.toml",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone for only module-responsibility-decomposition after live-plan-dag-revision is "
        "accepted and integrated. Perform only the four frozen behavior-preserving extractions. Keep one canonical "
        "type object and compatibility re-exports, no fallback imports or duplicate code, and preserve Ledger, "
        "Controller, harness and control-client side-effect ownership. Run all named gates, commit only owned "
        "surfaces and return one typed result."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

## Activation recovery successor — safe-refresh-and-control-list-paging

Real activation accepted source candidate
`f459cccafad45511d336138aa09d3b8c6dfa8345` after independent correctness and
architecture reviews both returned `ACCEPT` with P0=0/P1=0. The later
evidence-only trunk tip is `dbfaa59a0c6d72e5cb039ee9a54d752b91da9427`.
The installed target service `codex-flow-937220b34ee45f7f.service` is now
healthy only because the operator used the supported `harness install` then
`harness refresh` sequence. It is active at observed epoch 49 with schema v19,
`harness_authority`, `requested_shutdown=0`, `harness.sock`, and no active
worker/controller child, lease or claim. The unrelated
`codex-flow-71308abd5aeed226.service` remains protected.

That activation exposed two bounded implementation defects. This is an
`architecture_replan`: it preserves the accepted single harness, SQLite and
authenticated IPC authorities while replacing two disproven implementation
assumptions. `CANONICAL-V19-LEGACY-UNIT-RECOVERY` showed that automatic
bootstrap assumed schema and installed unit terminology always advance
together; the real interrupted post-migration state instead had a complete
schema-v19 ledger and fenced `harness_authority` paired with the exact stopped
v18 supervisor unit. `CONTROLLER-PENDING-FRAME-OVERFLOW` showed that an
unbounded list projection cannot satisfy the existing 65,536-byte IPC frame:
`controller_pending` returned `response_too_large`, so the headless TUI marked
itself disconnected and hid otherwise reachable workers and decisions.

The serial repair retains the existing service and IPC entrypoints. It adds no
transport, socket, service, table, persisted page state, polling loop, App
scrape, transcript merge or compatibility alias. Provider-free tests use only
disposable ledgers, sockets, units and homes. The implementation worker must
not install, refresh or stop either real service and must not consume the
provider. After candidate acceptance, the integration owner alone refreshes
the target service, reruns provider-free activation step 5, and then runs the
one already-authorized real streaming sentinel exactly once.

### Canonical interrupted-post-migration refresh transition

`refresh_with_credential` gains one closed recovery discriminator before its
normal unit validation. The discriminator is true only when all of these facts
hold in one pre-mutation inspection:

- `ledger_schema_compatibility` reports exact schema 19, migration marker
  complete and no migration requirement; the sole current authority table is
  `harness_authority` and no `supervisor_authority` table or current legacy
  authority survives;
- the authority row matches the exact repository root, state root and
  installed controller version, has `requested_shutdown=1`, and its PID plus
  process-birth identity is no longer live;
- the repository unit is inactive with systemd's exact inactive result, is a
  private single-link regular file, and its complete bytes match only
  `_legacy_supervisor_unit(expected_harness_unit)`, permitting the already
  supported well-formed profile-digest substitution and no other Description,
  ExecStart, environment or content drift;
- no dispatch is `claimed`, `starting` or `running`, no unexited worker
  liveness lease exists, no controller generation is `delivery_starting` or
  `active`, and no live human/model controller claim exists; and
- neither `supervisor.sock` nor `harness.sock` exists as a filesystem entry.
  A symlink, regular file, dangling link, stale socket or any other entry at
  either path fails closed rather than being removed.

When and only when that complete state matches, the same `harness refresh`
invocation installs the exact harness unit over the recognized stopped legacy
unit, validates the written bytes, daemon-reloads, imports only the named
volatile credential, starts the one repository unit and waits for an active
harness whose exact PID/birth identity is live, whose epoch is greater than
the fenced predecessor epoch, and whose authority has
`requested_shutdown=0`. Failure leaves the v19 fence and stopped recognized
unit explicit for a retry; it never edits SQLite manually, reconstructs v18,
starts the legacy command, creates another unit, or accepts arbitrary
ExecStart drift. The canonical bootstrap continues to fence before replacing
the installed tool and then invokes this same refresh command, so
`make install-personal-workflow-skills` closes the exact state in one supported
invocation.

### Bounded control-list paging protocol

The existing `status` and `controller_pending` authenticated operations each
gain a closed version-2 request/page shape; `controller_pending` is paged in
place and is not replaced by a parallel API. `ControlListRequest` carries
`list_kind` (`workers` or `decisions`), `visibility` (`all` or `active`), a
nullable `page_token`, and `page_items` from 1 through 24 with default 24.
`ControlListPage[T]` carries `list_kind`, `visibility`, `snapshot_id`, ordered
typed `items`, nullable `next_token`, `complete`, and `status` (`available` or
`stale`). Request tokens are ASCII and at most 1,024 bytes. A successful
encoded response is capped at 49,152 bytes, leaving framing/error headroom
below `MAX_FRAME_BYTES=65_536`; the page builder adds whole typed items until
either the 24-item or byte budget is reached. It never truncates an item. A
single item that cannot fit returns the bounded `item_too_large` error.

The first page computes `snapshot_id` as SHA-256 over the list kind,
visibility, schema version, harness epoch and every ordered source identity
plus its mutable identity fields: worker dispatch id/sequence/state/generation/
attempt/updated-at and decision id/state/revision/current-generation/updated-at.
The opaque continuation encodes that snapshot id, kind, visibility and the
last deterministic ordering key, and is HMAC-SHA256 authenticated with the
current in-memory `WorkflowHarness.owner_nonce`. No token or page is persisted.
Every continuation recomputes the source fingerprint before selecting rows.
A well-formed authenticated token whose fingerprint changed returns a typed
`stale` page with zero items and no successor; a malformed token returns
`malformed_page_token`; a token from another ledger, operation, visibility or
harness epoch returns `page_token_identity_mismatch`. A restarted harness
therefore rejects the old continuation, and the TUI discards all accumulated
pages before beginning a fresh first page.

Ordering is total and source-owned. Workers sort first by visibility class:
(0) `claimed`, `starting` or `running`; (1) any nonterminal worker or a worker
referenced by a current-attention decision; (2) terminal/inactive history;
then by descending queue sequence and ascending dispatch id. Current-attention
decisions are exactly `pending_delivery`, `awaiting_claim`, `claimed`,
`action_committed` and `human_attention_required`; they sort before
`acknowledged`, `superseded` and `legacy_closed`, then by ascending deadline
and decision id. `visibility=active` excludes only worker class 2 and terminal
decision history, so it can never hide a current-attention decision or its
worker session. The ledger owns these filters and ordering keys; harness page
projection does not sort or infer state after reading.

`LiveWorkerControlClient.status_page` and
`ControllerDecisionClient.pending_page` decode and retain the exact page
identity. Existing `status()` and `pending()` remain source-compatible
convenience methods for small callers and exhaust version-2 pages with a hard
2,048-page guard; they never request the legacy unbounded response. The
harness continues accepting version-1 `status` and `controller_pending`
requests for installed old clients only when the complete preflight-encoded
response fits the 65,536-byte frame; otherwise the existing bounded
`response_too_large` error is the explicit backwards-closed result. There is
no truncation or version fallback.

`TerminalUiClient.refresh()` requests only the first worker and decision pages,
atomically replaces its current accumulation after both identities decode,
and can render that useful active/current-attention-first state immediately.
`load_more_sessions()` consumes exactly the saved next tokens and deduplicates
by dispatch/decision identity, rejecting conflicting repeated identities.
Reconnect, offline transition, filter change or stale/error response clears
tokens and accumulated typed status maps before a fresh first page. The TUI
adds `F` for `Active only`/`All sessions`, `M` for explicit `Load more
sessions`, and `tui --active-only`; attention remains visible in both modes.
The existing `L` continues to mean conversation `Load older` and is not
overloaded. No timer or background pagination is added.

### Frozen implementation architecture map

Production paths and dependency direction are exact:

- Modify `src/codex_flow/domain.py` to own `ControlListKind`,
  `ControlListVisibility`, `ControlListPageStatus`, `ControlListRequest` and
  generic `ControlListPage`, including the 24-item, 1,024-byte token and
  49,152-byte response limits. These are ephemeral typed boundary values, not
  persisted/public workflow schemas.
- Modify `src/codex_flow/ledger.py` to expose deterministic filtered worker and
  decision source rows plus source fingerprints and continuation-key queries.
  It also exposes one read-only exact refresh-state inspection used by the
  service. SQLite remains the sole durable state authority; no schema version,
  table, row or migration is added.
- Modify `src/codex_flow/harness.py` to authenticate/validate page tokens with
  its existing owner nonce, project whole typed items within the response
  budget, and serve version-2 `status`/`controller_pending` plus the bounded
  version-1 compatibility behavior. It remains the only IPC and runtime owner.
- Modify `src/codex_flow/control_client.py` for the page decoders and client
  convenience methods. Client -> authenticated IPC -> harness -> ledger is the
  only dependency direction; clients never inspect SQLite.
- Modify `src/codex_flow/tui_client.py`, `src/codex_flow/tui_models.py` and
  `src/codex_flow/tui.py` for first-page replacement, explicit load-more,
  visibility state, deduplication, reconnect clearing, `F` and `M`.
- Modify `src/codex_flow/cli.py` only to pass the `--active-only` initial
  visibility into the existing TUI. No alternate command or TUI entrypoint is
  created.
- Modify `src/codex_flow/service.py` for the one exact schema-v19/stopped-v18-
  unit recovery discriminator and transition. Modify
  `scripts/install_personal_workflow_skills.py` only so its existing fence and
  refresh sequence recognizes and reaches that same transition; it does not
  duplicate service logic.
- Modify existing focused tests
  `tests/test_service_lifecycle.py`, `tests/test_plugin_installation.py`,
  `tests/test_local_ipc.py`, `tests/test_program_controller.py`,
  `tests/test_harness_recovery.py`, `tests/test_live_worker_control.py` and
  existing TUI/headless test files selected by current collection. Modify
  `config/test-partitions.toml` only if current semantic membership requires
  the already-existing tests to be listed.
- Create one semantic retained evidence record at
  `docs/reviews/evidence/safe-refresh-and-control-list-paging.json`. It binds
  source candidate, final implementation commit, disposable large-ledger
  reproduction, exact wheel/source parity, reviews and post-promotion
  activation/sentinel receipts. No other production, test, schema, fixture,
  runner or evidence path is created. Nothing is removed.

The new durable-artifact budget is exactly one evidence JSON record and zero
production modules, tables, schemas, migrations, services, sockets or
entrypoints. `docs/reviews/peer-thread-workflow.md`, `AGENTS.md`, the accepted
terminal-candidate/harness history, immutable prior evidence and migrations,
SDK/provider adapter and live keyframe semantics, model-facing capsule/result
contracts, workflow/plugin skills, retained wheels until regenerated by the
named parity gate, global Codex/App state, both installed services, remotes and
unrelated worktree bytes are protected during implementation except where an
exact mutable path above is named.

### Acceptance and promotion

The falsification checkpoint is a disposable real-shape ledger large enough
that the old complete `controller_pending` or `status` envelope exceeds 65,536
bytes. Before broad work, version-2 first pages for both lists must encode below
49,152 bytes, show active/current-attention rows, and permit an unchanged
snapshot to load every remaining identity exactly once. Failure of that check
returns to planning rather than increasing the frame or adding another API.

Closure requires:

- provider-free large-ledger tests prove the former overflow, bounded first
  page and exhaustive all-pages traversal with no omission or duplicate;
- active-only never hides current attention or its referenced worker and `F`
  restores inactive rows; `M` alone loads subsequent inactive/history pages;
- a real-shape headless TUI/control client stays connected and renders useful
  state from its first pages, while small ledgers preserve current ordering and
  behavior;
- malformed, stale, cross-ledger, cross-operation, cross-visibility and
  previous-epoch tokens fail closed with the specified bounded outcomes;
- disposable service/bootstrap tests close the exact schema-v19,
  fenced-harness-authority, stopped-v18-unit, no-child/no-lease/no-claim and
  no-socket state in one invocation, while arbitrary unit drift and every
  unsafe/ambiguous state still fail before replacement start;
- focused service, installer, IPC, controller, control-client and TUI tests,
  every affected semantic partition, `git diff --check`, the full
  `make check`, and exact source/wheel parity pass;
- independent Luna XHigh correctness and Sol Medium architecture reviews bind
  the exact candidate and return P0=0/P1=0 before promotion; and
- only after acceptance, the integration owner reinstalls/refreshes
  `codex-flow-937220b34ee45f7f.service`, reruns provider-free activation step 5,
  verifies the protected service was untouched, then consumes exactly one
  already-authorized real streaming sentinel. Provider evidence is not an
  implementation or planning gate and is never retried.

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make canonical activation recover the exact fenced schema-v19/stopped-v18-unit state in one "
        "supported invocation, and keep large control/TUI snapshots connected through bounded explicit paging."
    ),
    decomposition=(
        "Add one closed service refresh discriminator for schema v19 plus the exact stopped legacy unit, preserving arbitrary-drift rejection.",
        "Page the existing version-2 status and controller_pending operations with deterministic ledger ordering and harness-authenticated snapshot continuations.",
        "Keep legacy version-1 list requests only as bounded small-response compatibility, returning response_too_large instead of truncation.",
        "Load active/current-attention first pages immediately, expose F visibility and M load-more actions, and clear accumulation on reconnect, stale identity or filter change.",
        "Prove the former overflow and interrupted activation in disposable provider-free fixtures, then run package, parity and review gates without touching real services or providers."
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "A disposable ledger whose former status/controller_pending envelope exceeds 65,536 bytes returns each first page below 49,152 bytes and all identities exactly once across unchanged continuation pages.",
        "ControlListRequest permits 1..24 items, tokens are at most 1,024 ASCII bytes, whole items are never truncated, and an individually oversized item returns item_too_large.",
        "Tokens bind list kind, visibility, complete ordered source fingerprint, schema, harness epoch and owner nonce; malformed, stale, cross-ledger, cross-operation, cross-visibility and previous-epoch use fails closed.",
        "Workers order active then nonterminal/current-attention-referenced then terminal history; decisions order current attention before terminal history with the exact frozen tie-breakers.",
        "F toggles Active only and All sessions, M explicitly loads more sessions, L remains conversation Load older, --active-only is supported, and current attention plus its worker is never hidden.",
        "TerminalUiClient renders useful first-page state while connected, deduplicates loaded pages by exact identity, and discards accumulated rows and tokens on reconnect, stale page or visibility change.",
        "Small ledgers preserve behavior; version-1 callers receive the complete response only when it fits and otherwise receive response_too_large without truncation or fallback.",
        "One canonical refresh invocation accepts only complete schema v19, exact fenced harness authority, dead birth-bound predecessor, inactive exact v18 unit, no child/lease/claim and absent legacy/current sockets, then starts one healthy higher-epoch harness.",
        "Arbitrary ExecStart/content drift, live or ambiguous authority, any active child/lease/claim, unsafe socket entry or protected service identity fails before unit replacement or start.",
        "Focused service/installer/IPC/controller/control-client/TUI tests, affected semantic partitions, full make check, diff hygiene, exact source/wheel parity and independent correctness/architecture reviews close with P0=0/P1=0."
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/harness.py",
        "src/codex_flow/control_client.py",
        "src/codex_flow/tui_client.py",
        "src/codex_flow/tui_models.py",
        "src/codex_flow/tui.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/service.py",
        "scripts/install_personal_workflow_skills.py",
        "tests/test_service_lifecycle.py",
        "tests/test_plugin_installation.py",
        "tests/test_local_ipc.py",
        "tests/test_program_controller.py",
        "tests/test_harness_recovery.py",
        "tests/test_live_worker_control.py",
        "existing TUI/headless tests selected by current collection",
        "config/test-partitions.toml only if current semantic membership requires it",
        "docs/reviews/evidence/safe-refresh-and-control-list-paging.json"
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md and AGENTS.md",
        "accepted terminal-candidate-retention-and-harness-cutover history, immutable prior evidence and migration definitions",
        "src/codex_flow/backends/codex_sdk.py, provider/authentication/profile authority and live keyframe semantics",
        "model-facing capsule/result contracts, workflow/plugin skills and unrelated tests or production modules",
        "global Codex/App state, installed services codex-flow-937220b34ee45f7f.service and codex-flow-71308abd5aeed226.service, providers and retained evidence during implementation",
        "remotes, pushes, rebases, resets, history rewrites, implicit cleanup and unrelated worktree bytes"
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer"))
    ),
    prompt=(
        "Use execute-milestone as the single Luna XHigh mutable owner for only "
        "safe-refresh-and-control-list-paging in the existing python-sdk-controller integration worktree. "
        "Implement the frozen exact paths, types, 24-item/1,024-byte-token/49,152-byte-response limits and "
        "ordering. Page version-2 status and controller_pending in place; add no API, transport, polling, "
        "table, migration, production module, service, socket, transcript merge or compatibility alias. "
        "Accept only the exact fenced schema-v19 plus stopped recognized v18 unit state; arbitrary drift and "
        "ambiguous sockets/children/leases/claims fail closed. Preserve both real services and consume no "
        "provider. Run the disposable overflow and refresh falsification checks first, then focused tests, "
        "affected partitions, make check and exact wheel/source parity. Create only the named evidence record, "
        "stage only owned surfaces, inspect the staged diff, run git diff --cached --check, create one coherent "
        "local implementation commit, and return one typed terminal result with reviews pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000
)
```

## Historical selection before final lifecycle planning

The closure candidate `9d3071d544f16f2f34176ce4dfbb31ea0aa191d4`
descends from planning base `a3c3dd66410d06cbbbfeb74c086a9ac2a5b5656f`.
The full causal review base remains
`ba05399d57da5ca65250f64727e36270f786c213`. CI-001 token framing,
authenticated 120-worker/120-decision traversal, `Bootstrap.execute()`
reachability, and the ordinary acquired-replacement refence, stop, rollback and
retry path are accepted implementation inputs and must not be repeated or
redesigned.

The final correctness review returned `REPLAN_RECOMMENDED` for two reachable
gaps. CI-003 permits a post-start authority row whose `requested_shutdown`
value is malformed to raise before `replacement_authority_seen` is set, after
which the outer exception path restores v18 bytes over a possibly live
replacement. CI-004 clears `TerminalUiClient` control maps and page tokens on
stale/error, but then republishes the previous worker and decision rows in the
disconnected snapshot. ART-001 also makes the retained wheel non-promotable:
SHA-256 `26d545487fc81a55468e1427b3919aba19009d005827d5208e076b4f04e203e2`
has only 32/33 Python-path parity and contains stale
`codex_flow/service.py`. The current evidence file's externally computed
SHA-256 `067fc2d1e427f6cd93c83ea4283236a99fd5c75e31aaed9d3f9fc75d852577b6`
describes that stale candidate and is not closure evidence.

The serial milestone DAG is now:

    safe-refresh-and-control-disconnect-final-closure [ready; one mutable owner]
      -> independent Luna XHigh correctness causal re-review
      -> independent Sol Medium architecture causal re-review
      -> integration-owner promotion/install/refresh/provider-free activation step 5
      -> one already-authorized real streaming sentinel
      -> live-plan-dag-revision
      -> module-responsibility-decomposition

Each edge is an acceptance dependency. There is no mutable fan-out because the
two production repairs, their public-path regressions and the one evidence
record must bind the same final candidate and wheel. The cancelled visual review
remains cancelled.

## Final safe-refresh and disconnected-snapshot architecture replan

Outcome: every state after a replacement start is issued is classified before
legacy restoration, so malformed, missing or ambiguous authority can never
hide a live replacement behind v18 unit bytes; and every offline, stale or
error TUI snapshot immediately exposes cleared control-list state until fresh
first pages arrive. The successor keeps every public and persisted contract,
the accepted paging implementation, bootstrap path and ordinary replacement
rollback protocol.

Non-goals are redesigning service lifecycle or paging, changing the installer,
adding another recovery authority, changing conversation history, installing or
touching a real service, consuming a provider, using App APIs or promoting the
candidate. No real service, provider, App, network or global configuration
action is authorized.

### Monotonic post-start failure classification

`refresh_with_credential` retains its current pre-start recovery behavior but
replaces the parse-dependent `replacement_authority_seen` decision with one
monotonic post-start/possible-replacement guard. Set that guard immediately
before invoking `systemctl --user start`, before the call can return, raise or
make a replacement possible. Once set, it never clears during the invocation.
Every exception path consults it before considering direct v18 restoration.
After start has been issued, mark the observed state as post-start before
parsing any authority field, especially `requested_shutdown`; no decoder or
validation exception may fall back to the pre-start branch.

Direct legacy restoration is eligible in exactly two cases:

1. an error occurs before the start guard is set and existing checks still
   prove the current unit inactive and the captured predecessor fence intact;
   or
2. after start was issued, a new positive no-replacement proof establishes in
   the same bounded observation that the unit is inactive, systemd exposes no
   live current process identity, the sole authority is exactly the captured
   dead predecessor with the same epoch/PID/birth identity and valid
   `requested_shutdown=1`, and no replacement authority or process could have
   existed.

Absence of evidence is never this proof. A start command returning nonzero or
raising is still post-start because systemd may have accepted work before the
client observed failure. It may restore directly only if case 2 is positively
proved; otherwise it leaves the current harness bytes installed and raises
`ServiceRefreshFailed`.

The complete post-start classification is fixed:

| Observation | Required action | Legacy restore eligibility |
| --- | --- | --- |
| Exact captured predecessor, fenced, dead, unit inactive, and no systemd process | Record positive no-replacement proof; a failed start or bounded wait may use the direct restore path | Eligible only from this complete proof |
| Exact captured predecessor but unit active, a systemd process is live, or liveness is uncertain | Leave current unit bytes installed and fail closed | Ineligible |
| Matching replacement with greater epoch, distinct valid PID/birth identity and `requested_shutdown=0` | Use the accepted atomic refence, exact-unit stop, unit-inactive plus birth-death proof, same-row revalidation and rollback protocol | Eligible only after the whole accepted stop/death proof |
| Matching replacement already carrying `requested_shutdown=1` | Do not refence again; stop the exact unit, prove inactivity and birth-bound death, revalidate the same fenced row, then use the accepted rollback protocol | Eligible only after the whole stop/death proof |
| Matching predecessor or replacement with malformed `requested_shutdown`, including `2` | Leave current unit bytes installed and raise; parsing failure is post-start and cannot select direct restoration | Permanently ineligible in this invocation |
| Missing row, malformed row, ambiguous multiplicity/shape, unrelated repository/state/version, unexpected epoch, invalid PID/birth identity or identity drift | Leave current unit bytes installed and fail closed; do not infer an owner or issue direct SQL | Permanently ineligible in this invocation |
| Valid matching replacement whose process or unit remains active after fence/stop, or whose stop/fence/revalidation fails | Leave current unit bytes installed so the possibly live process remains visible and fail closed | Ineligible |

This is a private orchestration change in `src/codex_flow/service.py`. The
ledger remains the only durable fence authority and systemd remains the only
unit authority. No new public flag, enum, result, method, table or recovery
service is introduced.

### Disconnected TUI snapshot contract

The minimal production change is confined to
`TerminalUiClient.refresh()` in `src/codex_flow/tui_client.py`. On an offline,
stale or error transition it continues to close the live stream and perform
the existing conversation-history cleanup exactly as today. It clears worker
and decision maps, next-page tokens and snapshot identities, then emits a
disconnected `TerminalUiSnapshot` from that cleared state: zero worker rows,
zero decision rows, no retained control-list tokens or snapshot identities,
and `workers_complete=False` plus `decisions_complete=False` because there is
no valid accumulated snapshot whose completeness can be asserted. It must not
copy `previous_workers` or `previous_decisions` into the emitted snapshot.

The next successful `refresh()` makes new tokenless first-page requests and
atomically replaces the empty disconnected state with those results. Its
completion flags again derive from the fresh pages' successor tokens. Existing
conversation page retention/clearing, selected-subject reconciliation, live
keyframes, history loading and `L` behavior are unchanged; this capsule changes
only the control-list rows and completeness reported by a disconnected
snapshot.

### Frozen implementation architecture map and ownership

One Luna XHigh mutable implementation owner has exactly five paths:

- **Modify** `src/codex_flow/service.py` for the monotonic pre-start/post-start
  guard, authority classification and positive no-replacement proof. Reuse the
  current rollback helpers and public `refresh_with_credential` entrypoint.
- **Modify** `src/codex_flow/tui_client.py` only in the stale/error/offline
  `refresh()` transition so the disconnected public snapshot is empty and
  incomplete until fresh first pages arrive.
- **Modify** `tests/test_service_lifecycle.py` with public
  `refresh_with_credential` regressions for malformed
  `requested_shutdown=2` while the replacement is live, missing authority,
  ambiguous/malformed authority, identity drift, matching predecessor,
  matching replacement, active/live process and start failure. Each test binds
  start issuance, installed unit bytes, stop/fence calls and liveness so it
  proves no possibly live replacement is hidden by direct restoration.
- **Modify** `tests/test_live_worker_control.py` through
  `TerminalUiClient.for_state_root` and public `refresh()` with the existing
  real authenticated `harness.sock` fixture. Prove stale and error/offline
  transitions expose zero rows, no tokens/snapshot ids and false completeness,
  then recover by requesting fresh first worker and decision pages. Keep the
  accepted 120/120 traversal, `M`, `F`, IPC and bootstrap reachability
  regressions green.
- **Modify** the existing
  `docs/reviews/evidence/safe-refresh-and-control-list-paging.json` after the
  final source wheel is independently compared. Replace the stale wheel and
  unsupported closure claims with exact final source/test and evidence commit
  lineage, focused/full gate results, 33/33 parity and external wheel
  path/hash/size. Do not embed the evidence file's own digest.

**Preserve** `scripts/install_personal_workflow_skills.py`,
`tests/test_plugin_installation.py`, `src/codex_flow/ledger.py`,
`src/codex_flow/harness.py`, `src/codex_flow/control_client.py`,
`src/codex_flow/tui_models.py`, `src/codex_flow/tui.py`,
`src/codex_flow/cli.py`, all other production/test/evidence paths, schemas,
migrations, contracts, entrypoints and test-partition configuration. If a
discriminating test proves any of those need a material change, return to
planning rather than widening ownership.

Create and remove nothing. The new durable-artifact budget is zero: no new
artifact, module, table, schema, migration, API, transport, service, socket,
alias, polling path, provider/App path, fixture or runner. The semantic-
vocabulary delta is zero: this capsule completes two existing invariants using
existing types and entrypoints and deletes no concept.

### Focused public-path regressions

The service matrix must execute the production `refresh_with_credential`
entrypoint, not a replacement helper in isolation. The malformed-shutdown test
must cause start to be issued, expose an otherwise matching live replacement
with `requested_shutdown=2`, and prove the current harness bytes remain
installed; no legacy restoration may hide that process. Missing and ambiguous
authority equivalents prove the same invariant. Separate rows prove exact
predecessor/no-replacement restoration, ordinary matching replacement
refence-stop-death restoration, already-fenced replacement handling, identity
drift, still-live process and start-command failure.

The TUI matrix must use `TerminalUiClient.for_state_root` through the existing
authenticated production clients. After a connected snapshot has rows and
tokens, both a typed stale page and an IPC/client error must produce a public
disconnected snapshot with empty worker/decision tuples, cleared internal
maps/tokens/snapshot ids and both completeness flags false. The next refresh
must send page tokens `None` for both lists, publish fresh first-page rows and
derive completeness only from those fresh pages. Conversation-history behavior
must remain byte-for-behavior unchanged. Existing accepted token framing,
120-worker/120-decision traversal, paging accumulation, visibility, bootstrap
reachability and acquired-replacement retry tests remain green and are evidence
inputs, not new implementation scope.

### Artifact and evidence sequencing

ART-001 makes sequencing a correctness gate rather than bookkeeping:

1. After focused and affected tests pass, stage only the four source/test
   paths, inspect the staged diff, run `git diff --cached --check`, and create
   one coherent source/test commit descended from this planning commit.
2. Build one fresh wheel only from that exact source/test commit into a new
   external temporary directory. Do not reuse or overwrite the stale retained
   wheel. Independently enumerate every packaged Python path and compare bytes
   against the exact source tree: acceptance is 33/33, including
   `codex_flow/service.py` and `codex_flow/tui_client.py`. Any mismatch blocks
   evidence update and review.
3. Only after the independent 33/33 comparison, update the existing JSON with
   the exact source/test commit, wheel external path, SHA-256, byte size,
   enumerated 33/33 result and test results. Wheel facts belong in the JSON
   because this evidence-only edit changes no packaged Python source. The JSON
   must explicitly supersede stale wheel SHA-256
   `26d545487fc81a55468e1427b3919aba19009d005827d5208e076b4f04e203e2`
   and stale evidence SHA-256
   `067fc2d1e427f6cd93c83ea4283236a99fd5c75e31aaed9d3f9fc75d852577b6`;
   neither may be presented as passing parity.
4. Stage only that existing JSON, inspect it, run
   `git diff --cached --check`, and create one evidence-only commit. This
   evidence commit is the exact final candidate tip; its Python tree must equal
   the wheel-bound source/test commit. Compute the final evidence file SHA-256
   and byte size after the commit and carry them in the external review and
   promotion receipt, never inside the self-hashed JSON.

The two-commit sequence is one serial milestone and one mutable owner. It does
not authorize an extra artifact path. Evidence never claims parity before the
independent comparison, and review binds both commits.

### Closure and promotion boundary

Provider-free implementation closure requires:

- the focused service classification and TUI disconnected/recovery tests pass,
  and all accepted IPC/bootstrap/refence/paging regressions remain green;
- every affected service and live-control/TUI semantic partition passes,
  followed by the full `make check` because production behavior, tests,
  packaging and evidence change;
- staged-diff inspection and `git diff --cached --check` pass for each of the
  two commits, final status is clean, the source/test commit descends from this
  planning commit, the evidence-only tip descends directly from it, and no
  unrelated commit or path entered the range;
- the fresh external wheel has independently verified 33/33 packaged-Python
  byte parity with the source/test commit and therefore with the final
  evidence-only tip, and its path/hash/size are recorded truthfully; and
- independent Luna XHigh correctness and Sol Medium architecture causal
  re-reviews each bind the exact final evidence-only tip, wheel hash, external
  evidence hash and complete
  `ba05399d57da5ca65250f64727e36270f786c213..FINAL_TIP` history, inspect every
  commit rather than only the final tree, and return P0=0/P1=0. Finding
  severity and `promotion_blocking` remain distinct; every deferred finding
  names an owner and `defer_to`.

These gates prove provider-free closure only. Install, target-service refresh,
provider-free activation step 5, protected-service verification and the one
already-authorized real streaming sentinel remain integration-owner actions
after both reviews accept.

## Historical execution capsule — safe-refresh-and-control-disconnect-final-closure

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Close CI-003 so no post-start malformed, missing or ambiguous authority can restore legacy bytes over a possible replacement; close CI-004 so disconnected TUI snapshots expose cleared control state; and replace ART-001 with independently verified 33/33 wheel parity."
    ),
    decomposition=(
        "Make post-start/possible-replacement state monotonic before authority parsing and allow direct legacy restoration only after positive proof that no replacement process or authority could have existed.",
        "Emit stale/error/offline TerminalUiClient snapshots with zero control rows, cleared tokens and false completeness, then recover from fresh first pages without changing conversation-history behavior.",
        "Add public production-entrypoint regressions for the full service classification and TUI disconnect/recovery matrix while preserving accepted paging, bootstrap and ordinary rollback proof.",
        "Commit source/tests first, build and independently compare one fresh external wheel at 33/33 Python paths, then record truthful wheel facts in one evidence-only successor commit.",
        "Run focused, affected-partition, full repository, ancestry, parity and two independent causal review gates before promotion."
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "The post-start guard is set before systemctl start and before requested_shutdown parsing; once set, direct v18 restoration is permanently ineligible unless unit inactivity, no live systemd process and the exact dead fenced predecessor positively prove no replacement could have existed.",
        "Malformed requested_shutdown=2 on an otherwise matching live replacement, missing or ambiguous authority, identity drift, active/live process and uncertain start failure all leave current harness bytes installed and fail closed without hiding a process or issuing direct SQL.",
        "Matching predecessor, matching replacement with shutdown 0 or 1, start failure, stop/fence failure and successful stop/death rollback follow the frozen classification, while the accepted later higher-epoch retry remains green.",
        "A stale or error/offline public TerminalUiClient refresh exposes zero workers, zero decisions, cleared page tokens and snapshot ids, and workers_complete=False plus decisions_complete=False; the next refresh requests fresh first pages and publishes only fresh results.",
        "Conversation history, token framing, authenticated 120-worker/120-decision traversal, M/F behavior, Bootstrap.execute reachability and ordinary acquired-replacement refence/stop/rollback/retry remain unchanged and green.",
        "Only the four source/test paths are committed first; a fresh external wheel built afterward matches all 33 packaged Python source paths byte-for-byte, including service.py and tui_client.py.",
        "The existing evidence JSON is updated only after parity, records exact wheel path/hash/size and 33/33 truth, supersedes the stale wheel/evidence hashes, and does not contain its own hash; its evidence-only commit changes no packaged Python source.",
        "Focused tests, affected semantic partitions, full make check, staged diff hygiene, clean two-commit ancestry and independent correctness plus architecture causal re-reviews of ba05399d57da5ca65250f64727e36270f786c213..FINAL_TIP close at P0=0/P1=0."
    ),
    mutable_surfaces=(
        "src/codex_flow/service.py",
        "src/codex_flow/tui_client.py",
        "tests/test_service_lifecycle.py",
        "tests/test_live_worker_control.py",
        "docs/reviews/evidence/safe-refresh-and-control-list-paging.json"
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md and AGENTS.md after this planning commit",
        "scripts/install_personal_workflow_skills.py, tests/test_plugin_installation.py and all other production, test and evidence paths",
        "ledger, harness, control-client, TUI model/UI/CLI, schemas, migrations, contracts, entrypoints and test-partition configuration",
        "real services, stale retained wheel bytes, provider/App/network/global Codex state and immutable prior evidence",
        "remotes, push, rebase, history rewrite, discard, implicit cleanup and unrelated worktree bytes"
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer"))
    ),
    prompt=(
        "Use execute-milestone as the single Luna XHigh mutable owner for only safe-refresh-and-control-disconnect-final-closure in the existing integration worktree. Modify exactly service.py, tui_client.py, the two named tests and the existing evidence JSON; add no path. In service.py set a monotonic post-start/possible-replacement guard before invoking start and before parsing requested_shutdown. Direct legacy restoration after that point requires positive proof of the exact dead fenced predecessor, inactive unit and no live systemd process; malformed value 2, missing/ambiguous authority, identity drift, live/uncertain process or incomplete stop/fence proof leaves current bytes installed and fails closed. Preserve the accepted matching-replacement refence/stop/death/rollback/retry path. In TerminalUiClient.refresh, stale/error/offline clears control maps/tokens/ids and emits empty disconnected rows with both completeness flags false; the next call starts at fresh first pages, with conversation history unchanged. Add public-entrypoint regressions for every frozen case and keep accepted IPC/bootstrap/paging tests green. After focused and affected gates, commit only source/tests; then build one fresh external wheel and independently compare every Python path at 33/33. Only then update the existing JSON with exact wheel path/hash/size and truthful supersession, commit that JSON alone, and compute its hash externally. Run full make check, staged diff and clean ancestry gates. Touch no real service, provider, App, network or global state. Return one terminal result with exact two-commit lineage, wheel/evidence identities and independent causal re-reviews pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000
)
```

## Active continuation — acceptance-owned control and incremental planning

The prior implementation is retained and must not be replayed. Its exact
source/test commit is `47a8fa3d67575ef00662f1b5693efaf45c7b52bd` and its
evidence-only candidate tip is
`977d8f5c8c55459625dbff8133c262e12f91bba0`. The latter descends directly
from the former, the checkout was clean at planning start, and the candidate is not yet promoted:
declared objective and architecture acceptance still belong to independent
authorities. The stale earlier wheel and evidence identities remain historical
only. No implementation, provider call, service refresh, install or App/global
mutation is part of this planning continuation.

Two independently closable follow-ups have disjoint mutable surfaces:

1. `single-milestone-acceptance-lifecycle` owns the production `codex-flow
   control` lifecycle. It is selected next because it can adopt and finish the
   exact retained candidate without replaying implementation.
2. `incremental-planning-readiness-policy` owns reusable planning instructions
   and their behavior fixtures. Its design is decision-ready, but execution is
   deferred until the selected lifecycle milestone closes.

Their production/test mutable paths are disjoint, but one concrete `shared
contract` edge keeps execution serial: the
lifecycle owner must work under the currently frozen root instructions, while
the policy owner later changes that same repository instruction authority. The
future policy node and any blocker local to it do not block lifecycle review or
promotion. After lifecycle acceptance, the policy owner works in the same
program worktree from the promoted tip. Once both follow-ups are promoted, the
current branch is installed and receives one self-hosted closure smoke. A
separate SDK-fork recovery successor then starts from that exact production
baseline; it does not reopen or block either active follow-up. The final TUI
successor remains last and starts only after the recovery successor is accepted
and installed. The lifecycle milestone itself retains real
acceptance-dependency edges:

    exact candidate adoption
      -> every declared independent review
      -> accepted promotion
         or one bounded same-owner repair
           -> every declared fresh successor re-review
             -> accepted promotion or truthful terminal blocker

The program controller remains responsible only for program DAG readiness,
parallel-lane integration and successor milestones. It does not compensate for
an incomplete single-milestone `control` lifecycle.

### Follow-up 1 — single-milestone acceptance lifecycle

#### Outcome and boundaries

`codex-flow control` must distinguish an executor turn ending from a milestone
closing. A successful executor result with a verified commit becomes an exact
candidate and leaves the milestone nonterminal while any declared acceptance
authority is missing. The harness starts every authority declared by the
capsule, binds each result to the exact candidate, and promotes only when every
authority accepts with no open promotion-blocking P0/P1 finding or applicable
terminal blocker.

One or more concrete P0/P1 findings route to one bounded same-owner repair
dispatch over the retained candidate. A coherent repair commit supersedes the
candidate and invalidates all prior reviews; every declared authority then
receives one fresh read-only re-review of the successor. A second blocking
review, unavailable required authority, unresolvable candidate-integrity fact,
or genuinely external/decision prerequisite closes as the matching truthful
terminal blocker. It never becomes `completed` merely because an executor turn
completed.

This is a single-milestone lifecycle, not a one-node synthetic program graph.
The existing SQLite ledger and detached `WorkflowHarness` remain sole runtime
authority. The normal `control --plan-path ... --milestone-id ...` entrypoint
owns enqueue/reconciliation; workers remain leaves; review and repair dispatch
use the existing role routing, closed result contracts, candidate inspection,
generation identities and one-shot external-effect semantics. No alternate
scheduler, callback, polling loop, transport, table, schema version, service,
socket, worktree manager or public result family is authorized.

#### Current-candidate migration and adoption

After this lifecycle implementation is promoted, one ordinary control
reconciliation of historical milestone
`safe-refresh-and-control-disconnect-final-closure` must inspect its already
durable completed executor result and terminal workspace rather than invoke the
executor again. Adoption succeeds only when the retained lineage proves exact
source/test commit `47a8fa3d67575ef00662f1b5693efaf45c7b52bd`, direct
evidence-only tip `977d8f5c8c55459625dbff8133c262e12f91bba0`, clean
workspace identity, capsule ownership, protected-path integrity and no
unrelated commit. It records `977d8f5...` as the unpromoted candidate and
queues both the declared `code-reviewer` and `architecture-reviewer` exactly
once. Existing matching adoption/review facts are idempotently reused; any
conflicting identity fails closed. No implementation generation, provider
execution retry, manual candidate copy or invented promotion receipt is
allowed.

#### Frozen implementation architecture map

One Luna XHigh mutable owner may modify exactly these existing paths:

- **Modify `src/codex_flow/cli.py`:** make `control` report candidate,
  reviewing, repair and accepted/blocked closure truthfully and reconcile an
  already completed exact execution without restarting it. Keep the existing
  CLI command and options unless a focused test proves one generic adoption
  selector is unavoidable; any new public option requires a bounded plan
  update before implementation.
- **Modify `src/codex_flow/controller.py`:** extend the existing `Controller`
  and `ReviewWorkflow` composition so one compiled capsule drives candidate
  retention, every declared review authority, one bounded repair and fresh
  successor re-review. Do not introduce another manager, runner or protocol.
- **Modify `src/codex_flow/ledger.py`:** reuse the current execution,
  lifecycle, review, dispatch and candidate facts to keep executor completion
  nonterminal until promotion or a truthful blocker. Add no table, migration,
  schema version, alternate state store or duplicate program fact family.
- **Modify `src/codex_flow/harness.py`:** translate terminal single-milestone
  executor/reviewer/repair queue rows into the same exact-candidate lifecycle
  and enqueue each controller-authorized role once. Keep program DAG and
  integration effects separate.
- **Modify `tests/test_workflow_control.py`,
  `tests/test_controller_execution.py`, `tests/test_harness_recovery.py` and
  `tests/test_plan_compilation.py` only:** prove public entrypoint behavior,
  crash/restart idempotency, authority completeness, repair/re-review and exact
  retained-candidate adoption. `config/test-partitions.toml` may be modified
  only if current test membership requires it.

Dependency direction remains `cli -> Controller/ReviewWorkflow -> Ledger`,
with `WorkflowHarness -> Controller/Ledger` only at the existing authenticated
queue boundary. `WorktreeManager`, typed domain/contracts, SDK adapter and
configuration remain dependencies, never reverse callers.

Preserve `src/codex_flow/domain.py`, `src/codex_flow/contracts.py`,
`src/codex_flow/plan_capsule.py`, `src/codex_flow/worktrees.py`,
`src/codex_flow/program_controller.py`, `workflow.toml`, schemas, migrations,
service/IPC framing, SDK/profile/authentication, TUI/live-keyframe behavior,
plugins/skills/templates, AGENTS.md, all evidence including
`docs/reviews/evidence/safe-refresh-and-control-list-paging.json`, real services,
provider/App/global state and remotes. The 128-character `durable_status`
contract is unchanged. Add no alias or persisted/public type; improve existing
worker/reviewer prompt guidance with one bounded example only if a failing
behavior test proves ambiguity, and never widen the schema to solve prose.

The new durable-artifact and production-module budgets are zero. The semantic
delta is one existing lifecycle invariant: executor completion creates a
candidate; acceptance promotion closes a milestone. Existing candidate,
review, finding, repair, blocker, generation and authority vocabulary must be
reused.

#### Acceptance and promotion gates

- A completed executor with pending declared authorities is observably
  nonterminal, owns one verified exact candidate, and queues every declared
  authority exactly once across wake, restart and replay.
- Reviews bind exact candidate, role and acceptance mode. Missing, stale,
  duplicated, cross-candidate or counterfeit reviews never satisfy promotion.
- Any P0/P1 set produces one same-owner repair generation;
  its successor commit invalidates all earlier reviews and every declared
  authority re-reviews the exact successor. There is no second repair.
- Promotion requires all declared fresh accepted reviews, P0=0/P1=0 and no
  applicable unresolved candidate blocker. Closure otherwise uses an accurate
  `NEEDS_DECISION`, `EXTERNAL_BLOCKED` or `FAILED` fact with retained candidate
  and evidence.
- The program controller neither starts these single-milestone reviews nor
  marks their node complete; program readiness consumes only the accepted
  promotion receipt after this lifecycle closes.
- A migration regression adopts `47a8fa3... -> 977d8f5...` from retained
  durable facts, starts objective and architecture reviews without executor
  replay, and remains idempotent under crash/restart.
- Focused lifecycle/adoption tests, every affected controller/harness/review
  semantic partition, plan compilation, strict schema/validator/compile gates,
  full `make check`, exact staged-diff inspection, `git diff --cached --check`
  and independent Luna XHigh correctness plus Sol Medium architecture reviews
  close at P0=0/P1=0 before promotion. No provider or real-service gate is
  implied.

### Follow-up 2 — incremental planning readiness policy

This decision-ready policy milestone corrects reusable planning guidance
without embedding any project-specific legal identifier or operational
example. Stable program-level planning freezes the observable outcome,
non-negotiable whole-program invariants, shared/public/persisted contracts,
state and integration authority, production entrypoints, dependency DAG and
current readiness. A decision-ready implementation architecture map is
required only for a milestone whose prerequisites are satisfied and which is
eligible for execution now.

Later migration, cutover, rollout, cleanup or production-operation nodes may
remain explicitly `deferred` with named prerequisite evidence and a bounded
decision owner. They receive a full architecture map before becoming ready,
after predecessor dry-run or retained evidence resolves the operational facts.
A blocker scoped only to a future node does not block current milestone review
or promotion unless it changes a shared contract, state/entrypoint authority,
security/integrity boundary or other recorded whole-program invariant.

Once the shared-instruction edge is satisfied, one Luna High mechanical owner
may modify only:

- `plugins/personal-workflow-skills/skills/plan-work/SKILL.md` for the
  ready-milestone architecture-map rule and deferred-node contract;
- `templates/AGENTS.workflow.md` and root `AGENTS.md` for the same reusable
  controller/planner boundary, merged without weakening stronger local policy;
- `tests/fixtures/prompt-input/plan-work.json` with a universal ready/deferred
  example containing prerequisite evidence and blocker scope; and
- `tests/test_workflow_assets.py` for proportional positive and adversarial
  behavior tests. `scripts/validate_workflow.py` may be modified only if the
  existing validator cannot express these assertions; that is a bounded Sol
  replan trigger, not executor discretion.

Create/remove nothing; new durable-artifact, production-module and public-type
budgets are zero. Preserve runtime/controller code, schemas, other skills,
tests and fixtures, plan history/evidence, installed plugins, global state and
remotes. Acceptance requires consistent skill/template/root wording, one
execution-ready node with a full map, one deferred operational node without
speculative module/class design, readiness blocked until named predecessor
evidence exists, and current promotion unaffected by a future-only blocker.
Adversaries must reject calling an execution-ready map optional, promoting a
deferred node without its prerequisite/map, or misclassifying a shared-contract
or whole-program blocker as future-only. Run focused workflow-asset tests,
validators, fixture/schema checks, affected partitions and full `make check`;
commit only owned surfaces and obtain independent objective and architecture
acceptance at P0=0/P1=0.

### Deferred recovery successor — SDK fork continuity

This recovery milestone begins only after both active follow-ups close and the
current branch is installed and proven by its self-hosted smoke. It uses a new
managed sibling worktree
`/home/adam/personal-workflow-skills.worktrees/codex-flow-thread-fork-recovery`
on branch `agent/codex-flow-thread-fork-recovery`, created from that exact
installed production tip. It is new scope and does not invalidate candidate
`cca8ab207285bd11bd5b3327b73d67b7f6221d69` or its pending reviews.

The pinned official `openai-codex==0.147.0` transport exposes
`Codex.thread_fork(thread_id, ...)` and the typed low-level
`CodexClient.thread_fork(thread_id, ThreadForkParams(...))`. The latter supports
`last_turn_id`, so the harness can fork a persisted source thread through one
exact completed turn without depending on a Codex App task API. Codex Flow does
not expose that capability yet. The currently active virtual environment also
contains a locally replaced `openai_codex/__init__.py` whose bytes do not match
the pinned wheel RECORD; `api.py` and `client.py` do match. Before any runtime or
provider proof, the ordinary reproducible setup must restore the exact pinned
SDK and verify package integrity. This environment drift is a prerequisite,
not evidence against the SDK capability.

Forking is a recovery strategy of last useful continuity, not another generic
retry. Typed 429/500/502/503/504 failures continue to use the existing bounded
same-thread continuation budget and durable backoff. A fork is eligible only
after read-only `thread/read(includeTurns=true)` proves that no source writer is
active, the current thread cannot safely continue, and an exact prior completed
turn can be named. It forks through that completed turn, thereby excluding a
failed or interrupted later turn while retaining the earlier persisted
conversation. Active, ambiguous, malformed, missing-turn and permission/profile
drift cases fail closed. A source with no completed turn uses the existing fresh
thread recovery capsule instead of pretending that continuity was preserved.

The fork RPC is a single externally visible effect. The ledger records one
prepared replacement intent before the call, then either the exact returned
child identity or an ambiguous/failed terminal fact. A crash or lost response
after the RPC is never authorization to issue a second fork. Only the bound
child may receive the next turn; the source remains persisted and readable but
cannot retain an active worker lease or receive concurrent continuation. The
new identity inherits and revalidates the exact workspace, model, service tier,
effective sandbox/approval authority, plugin compatibility and candidate facts.
No prompt, transcript, tool output, image bytes or secret is copied into SQLite;
the official SDK owns stored-history copying and the ledger retains only typed
identities, turn boundary, strategy and effect state.

#### Frozen implementation architecture map

One Luna XHigh mutable owner may modify only these existing production paths:

- **Modify `src/codex_flow/backends/codex_sdk.py`:** add one typed
  `fork_thread(source, through_turn_id)` adapter operation over the official
  low-level `thread/fork` boundary, return only the exact child
  `ThreadIdentity`, and apply the same leaf-worker permission/profile checks as
  start/resume. The raw SDK client remains private to the adapter.
- **Modify `src/codex_flow/domain.py`:** extend the existing closed recovery
  vocabulary with a semantic fork strategy and bounded prepared, starting,
  bound, failed and ambiguous effect states. Add no general task or transcript
  model.
- **Modify `src/codex_flow/ledger.py`:** extend the existing `recovery_state`
  authority in one forward migration with the minimal source thread, source
  completed-turn, replacement state and child-thread facts needed for one
  crash-safe fork attempt. Do not add a second table, scheduler, queue or result
  authority.
- **Modify `src/codex_flow/worker.py`:** accept one capability-bound fork source
  and completed-turn identity, invoke the adapter before `bind_worker`, and
  bind only the returned child identity. Existing start/resume behavior remains
  unchanged.
- **Modify `src/codex_flow/harness.py`:** select fork only from the frozen
  eligibility facts, persist the intent before process/RPC execution, reject a
  concurrent or stale source, and reconcile bound, failed or ambiguous outcomes
  without reissuing an uncertain fork.

Focused tests may modify only `tests/test_codex_sdk_adapter.py`,
`tests/test_ledger_integrity.py`, `tests/test_supervisor_recovery.py`,
`tests/test_harness_recovery.py`, `tests/test_live_worker_control.py` and
`tests/test_production_pilots.py`; `config/test-partitions.toml` may change only
if the new tests otherwise escape their affected semantic partitions. A single
new forward schema migration is allowed inside `ledger.py`; new modules,
tables, services, sockets, CLIs, public commands, result families, App APIs and
alternate transports are not. The semantic delta is one recovery relationship:
an exact source thread and completed turn may produce at most one exact child
thread, while all lifecycle authority remains in SQLite and `WorkflowHarness`.

Acceptance modes are `objective` and `architecture`. Promotion requires:

- provider-free adapter fixtures prove exact `thread/fork` parameters,
  child-identity validation, permission/profile parity and fail-closed SDK
  capability absence;
- public recovery tests prove source-active/ambiguous/malformed rejection,
  same-thread retry precedence, exact completed-turn selection, one child bind,
  no concurrent source continuation and no second fork after crash/lost reply;
- restart and migration tests preserve every existing dispatch, retry,
  candidate, result, review and controller fact while reconstructing the fork
  intent/result exactly once;
- a disposable read-only real SDK sentinel, separately authorized after all
  deterministic gates pass, forks one sentinel thread through a known completed
  turn, observes the new thread identity and persisted history, archives only
  the sentinel identities it created, and proves no repository-byte change;
- focused and affected partitions, SDK/package integrity, full `make check`,
  exact candidate review and independent Luna XHigh objective plus Sol Medium
  architecture acceptance close with P0=0/P1=0.

This milestone is fail-closed if the pinned SDK or real sentinel cannot prove
the documented behavior. It never calls `mcp__codex_app__fork_thread`, never
makes the ChatGPT/Codex App the lifecycle authority, and never labels a fresh
thread plus recovery prompt as a true fork.

### Deferred final successor — coding-agent terminal experience

This is the final milestone after the two active follow-ups and the SDK-fork
recovery successor close and their resulting runtime/plugin are installed and
verified. It starts from that exact latest installed production tip in a new
managed sibling worktree
`/home/adam/personal-workflow-skills.worktrees/codex-flow-terminal-experience`
on branch `agent/codex-flow-terminal-experience`. The branch/worktree is not
created before those prerequisites close and is never named from a task or
model identity.

The presentation milestone replaces the current controller summary
label with a first-class selectable controller row and makes the Textual
application look and behave more like a modern coding-agent CLI. Codex CLI is
only a visual and interaction reference. Codex-flow keeps its existing official
Python SDK transport, authenticated local IPC, SQLite state authority, detached
`WorkflowHarness`, lifecycle rules and Textual implementation.

The observable target is one coherent terminal shell with a session rail for
controller and worker conversations, a dominant transcript, inline mutable tool
activity, compact lifecycle/status context, a bounded input composer and a
command palette. Controller, implementer, reviewer and recovery sessions remain
visibly related but independently selectable. `Active/all` hides or reveals
inactive and completed sessions without deleting their durable identity.
Human-attention decisions remain a distinct action surface rather than being
the only way a controller conversation becomes visible.

The final milestone is not ready for mutable execution until these prerequisites
exist:

- `single-milestone-acceptance-lifecycle` is independently accepted, installed
  and proven to create controller, review and repair sessions through the
  production harness;
- representative existing controller, worker, reviewer, tool-call, completed
  and attention states are available for deterministic UI fixtures; and
- the implementation owner confirms whether the existing read-only control
  projection already exposes completed controller sessions. If it does not,
  one bounded Sol Medium replan may add only the smallest read-only projection;
  no lifecycle, persistence or scheduling change is allowed.

The implementation remains presentation-focused:

- SQLite and `WorkflowHarness` remain the sole durable authorities; the TUI is
  an event-driven typed consumer and never schedules, retries, promotes or
  invents controller state;
- controller history comes from official persisted SDK thread items through
  the existing privacy/redaction boundary; App task lists, rollout files and
  diagnostic prose never substitute for a transcript;
- live assistant/tool projections remain bounded, lossy and ephemeral, while
  terminal results and persisted conversations remain authoritative;
- the composer is a visual wrapper around existing typed steer, interrupt and
  decision actions with their current identity/CAS checks; it is not an
  arbitrary shell or alternate SDK prompt transport;
- paths, URLs, secrets and image bytes retain typed presence markers and safe
  open actions. Portable bitmap rendering, an embedded browser, another UI
  framework, terminal-emulator ownership and native Codex App embedding are
  non-goals.

Acceptance modes are `objective` and `visual`. Architecture review is added
only if the bounded replan actually changes a public/control boundary.
Promotion requires all of the following observable evidence:

- an inactive or completed controller is visible and selectable in `all` mode,
  an active controller is visible in `active` mode, and loading its persisted
  conversation does not require a pending human decision;
- controller -> worker -> reviewer/repair relationships are readable without
  opaque IDs in the primary surface, while exact identities remain available
  in diagnostics;
- assistant text grows in place, tool calls update one inline card through
  typed states, and the composer/control acknowledgements remain responsive
  without polling or blocking result ingress;
- wide and exact 80x24 keyboard flows retain the session rail, readable
  transcript, input/status area, load-older/scroll controls and complete
  controller/worker actions; active/all filtering is reversible and does not
  discard cached identity-bound history incorrectly;
- provider-free state, reconnect, slow-consumer, paging, privacy and fail-closed
  adversaries pass, followed by an installed-runtime smoke against retained
  local controller and worker sessions without a new provider call; and
- affected semantic partitions, packaging parity, full `make check`, exact
  candidate review and independent objective and visual acceptance close with
  P0=0/P1=0.

This future-only visual/product work does not reopen the accepted historical
TUI milestone or reinstate its cancelled visual review. It is a new observable
scope and therefore receives its own fixed visual contract and qualitative
promotion authority when it becomes ready.

## Next execution — single-milestone-acceptance-lifecycle

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Close the remaining exact-candidate terminal-authority defect so one production-shaped repair can "
        "create, validate and re-review a distinct successor without weakening restart adoption."
    ),
    decomposition=(
        "Remove the historical-adoption bypass and compare the exact retained candidate, terminal workspace, Git authority and protected identity facts.",
        "Separate generation-1 executor terminal authority from generation-2 repair terminal authority while binding the repair to its immutable predecessor and finding context.",
        "Capture generation-2 workspace, Git-authority and protected-path identities on the authenticated turn/completed boundary, then bind the result digest to that immutable dispatch-owned record during result ingress.",
        "Allow the accepted completed-execution review and one-repair states without permitting arbitrary candidate replacement or a second repair.",
        "Prove the real Controller execution path accepts one direct clean repair successor and rejects candidate, workspace, Git-authority or protected-path drift.",
        "Retain the already-closed exact dispatch-context validation and every existing review, promotion, program-controller and persistence boundary."
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Historical 47a8fa3d -> 977d8f5c adoption succeeds only with exact retained terminal workspace, Git-authority and protected-path identities; later schema-v20 test adaptations are accepted only through their exact authorized blob/patch identity, while placeholders, conflicting facts and non-allowlisted drift fail closed.",
        "A production-shaped completed execution reaches REVIEWING, records one repair request, accepts one direct clean successor with its own exact terminal facts, invalidates prior reviews and queues every fresh authority.",
        "One dispatch_terminal_integrity row captures HEAD, workspace, Git authority and protected digest on authenticated turn/completed; result ingress atomically binds its digest without recapture, and all facts remain exact before candidate recording, each fresh review and promotion.",
        "Persisted-thread result recovery without the pre-existing completed-turn snapshot fails closed and never manufactures terminal authority after worker exit.",
        "The successor is not compared to the generation-1 terminal HEAD, but its predecessor, findings, workspace, Git authority, protected identity and direct ancestry are all exact and immutable.",
        "Candidate drift, dirty workspace bytes, wrong predecessor/findings, stale generation, duplicate repair or mismatched durable authority never produces a reviewable successor.",
        "Exactly one forward schema-v20 migration and one dispatch_terminal_integrity child table are introduced; no second repair, public type, transport, lifecycle owner or program-controller responsibility is introduced.",
        "Focused production-shaped regressions, affected partitions, plan compilation, full make check, diff hygiene and fresh independent correctness plus architecture reviews close with P0=0/P1=0."
    ),
    mutable_surfaces=(
        "src/codex_flow/harness.py",
        "src/codex_flow/ledger.py",
        "tests/test_controller_execution.py",
        "tests/test_harness_recovery.py",
        "tests/test_ledger_integrity.py",
        "tests/test_service_lifecycle.py",
        "tests/test_plan_compilation.py",
        "config/test-partitions.toml only if current test membership requires it"
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md and AGENTS.md after this planning commit",
        "CLI, Controller implementation, domain, contracts, plan compiler, worktree manager, program controller, workflow configuration and every schema/migration except the one ledger.py v19-to-v20 migration",
        "SDK adapter, profile/authentication, service/IPC framing, TUI/live-keyframe behavior and reusable workflow policy surfaces",
        "all evidence including docs/reviews/evidence/safe-refresh-and-control-list-paging.json",
        "real services, providers, App/global Codex state, remotes, history rewrite, discard, implicit cleanup and unrelated worktree bytes",
        "the closed CONTROL-DISPATCH-CONTEXT-002 behavior, 128-character durable_status contract and all unrelated tests and evidence"
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer"))
    ),
    prompt=(
        "Use recover-milestone as one Sol Medium mutable recovery owner for only ARCH-CONFORMANCE-HISTORICAL-ADOPTION-004 and ARCH-CONFORMANCE-PREINGESTION-PROOF-005 at exact predecessor b23ed62816926933933c30ddbbef2dad3b2520d3 in the existing python-sdk-controller integration worktree. Both independent reviews agree the one-table v20 authority is sound after ingestion, but the historical adoption check is broader than immutable provenance and recovered persisted-thread results capture too late. Modify only harness.py, ledger.py and the five named tests. Revise the unpromoted schema-v20 dispatch_terminal_integrity table in place, without v21: authenticated generation-2 turn/completed inserts the immutable terminal HEAD/workspace/Git/protected snapshot with an unbound result; submit_result atomically binds the exact result digest and queue terminal row without recapturing workspace authority. A recovered persisted-thread result may bind only an already-captured matching snapshot; otherwise require human attention and never capture after exit. Retain the necessary schema-v20 adaptations in tests/test_service_lifecycle.py and replace only the overbroad post-historical no-diff condition with an exact allowlisted blob/patch identity check for those authorized successor adaptations. The retained 47a8fa3d -> 977d8f5c candidate facts remain exact and immutable; any placeholder, conflicting identity or non-allowlisted successor drift fails closed. Add production-shaped Git, workspace and protected drift between completed-turn capture and result ingress, plus missing-capture recovery, post-ingestion review/promotion drift, restart idempotency and exact success. Preserve one repair only, dispatch-context checks, review invalidation/requeue, SQLite and WorkflowHarness authority and program-controller separation. Add no second table, schema version, type, module, public option, transport or lifecycle owner; do not call providers, App connectors, services or installers. Run focused tests, ledger migration and affected partitions, the exact historical adoption test, plan compilation and full make check in a clean disposable checkout; stage only owned paths, inspect the staged diff, run git diff --cached --check, create one coherent recovery commit and return one terminal result with fresh objective and architecture re-reviews pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000
)
```
