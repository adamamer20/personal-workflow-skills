# Codex controller compatibility

## Supported H3 contract

- Python baseline: 3.12.
- SDK transport: `openai-codex==0.147.0`, high-level `Codex` API only.
- Permissions: H1 compatibility checks may explicitly request `read_only`.
  H3 defaults to `inherit_native`, supplies no SDK sandbox/approval override,
  and therefore follows the active native Codex config on each new execution.
  A capsule may request `read_only` as a typed monotonic restriction; no H3
  value can broaden native authority.
- Runtime: local Linux, with Git worktrees and the repository-local SQLite
  schema v8 ledger as authority. Schema v8 retains schema v7 causal turn-start
  and per-milestone workspace-baseline facts, and atomically adds each accepted
  terminal milestone's HEAD plus predecessor-owned workspace snapshot.
- Workspace modes: current checkout, an exact existing linked worktree, or a
  controller-created semantic sibling managed worktree.
- Resume: before a new SDK client or `thread_resume`, the controller re-resolves
  native configuration and atomically persists the meet of the durable and
  current permission authorities. Tightening is inherited; broadening retains
  the prior restriction. Provider, routing, model-catalog, or discovery changes
  fail closed as a distinct same-thread compatibility error.
- Workspace identity: repository and workspace spellings resolve to one
  canonical physical path before capsule serialization, execution/lease keys,
  or ledger lookup. Lexical, relative, trailing-component, and symlink aliases
  cannot acquire another owner or SDK thread. Noncanonical legacy keys fail
  schema validation closed.
- Migration: schema-v5/v6 terminal history is preserved as legacy evidence.
  Schema-v7 rows migrate forward without inventing terminal content; a
  completed v7 predecessor that lacks an accepted terminal snapshot requires
  explicit reconciliation before a successor can start. An in-flight legacy
  execution without reconstructable baseline or compatibility authority fails
  same-thread resume closed instead of guessing.

Before adapter construction and immediately before every SDK
`thread_start`/`thread_resume`/turn call, one canonical authorization operation
double-captures the physical worktree authority and requires two identical
bounded observations. The observation must exactly equal the durable
per-milestone baseline HEAD, file/type/mode/content and empty-directory
topology, protected-path digest, Git config/index/refs/reflogs/history and
operation metadata, physical lease facts, and every applicable accepted
predecessor terminal snapshot. The Git-authority digest is bound atomically
with the schema-v8 baseline and native-profile facts. A restart never adopts
new authority. If authorization rejects after a durable external-call marker
but before the call, the marker returns to its prior safe checkpoint; a crash
at the actual call boundary remains uncertain and non-replayable.

Native-profile facts v2 recursively fingerprint each configured `skills`,
`plugins`, and `memories` tree through no-follow directory descriptors. The
identity covers relative path bytes, type and ownership metadata, file-content
digests, internal symlink targets, discovery-root identity, and every absolute
ancestor identity. External/dangling/cyclic symlinks, including links to the
discovery root or any active ancestor, hard-linked regular files, special file
types, substitutions, and scan races fail closed. Each
surface is bounded to 100,000 entries, depth 64, 4,096 relative-path bytes,
512 MiB per file, and 2 GiB total file bytes; exceeding a bound is an error,
never truncation. Durable facts retain only the compatibility digest and
sanitized bounded metadata, never discovery file contents or sensitive raw
configuration. An in-flight prior-algorithm fingerprint reopens as a typed
compatibility failure before adapter construction.

Workspace mutation authority combines Git-visible paths with a bounded
no-follow directory-topology delta. Empty directory creation, deletion, and
rename are therefore visible for current, existing, and managed worktrees;
only the controller's actual repository-bound `.codex-flow` state tree is
exempt. Accepted terminal snapshots cover mutable-root absence, file and
directory type/mode/content, empty subdirectories, symlink rejection, hardlink
count, terminal HEAD, and Git authority. Every completed predecessor is
verified before a successor baseline, lease, adapter, or external call.

Native config projection is a closed field-typed schema for native MCP server
definitions. Supported command/URL, arguments, working directory, timeouts,
enablement/required flags, tool filters, bearer reference, environment,
environment-reference, and HTTP-header fields retain their native semantics;
all other fields fail closed. Concatenated, camel-case, case, separator,
nested, or colliding aliases of `http_headers` and `env_http_headers` are
rejected before projection. Literal MCP HTTP headers and every literal MCP
stdio environment entry, regardless of its name, become
`env_http_headers`/`env_vars` references backed only by the SDK child's
ephemeral environment. Duplicate, case/separator-normalized, or cross-source
stdio environment names fail closed before projection; no source silently
overrides another. Authorization, proxy authorization, cookies, arbitrary
header values, API keys/tokens/passwords/client secrets, arbitrary environment
values, case variants, and nested header shapes cannot reach the ledger,
artifacts, evidence, errors, or private runtime files. Conflicting environment
semantics fail closed. Existing native references such as provider `env_key`,
MCP `bearer_token_env_var`, and `env_http_headers` remain references.

`CODEX_HOME` is the one controller-reserved stdio collision with a supported
source-bound meaning. When a literal MCP environment entry uses that name, the
projection replaces only the reference with a deterministic
`CODEX_FLOW_MCP_COLLISION_<digest>` alias and retains the value only in the
ephemeral child environment. The pinned app-server has no environment-alias
map, so the projected stdio command uses a fixed `/bin/sh` argv shim to export
the alias back to `CODEX_HOME` immediately before `exec`; the SDK/app-server
process continues to receive the controller-private `CODEX_HOME`. Provider-key
collisions and cross-source/cross-server duplicates still fail closed. The
active three-server `codex-lb` profile and the pinned runtime config diagnostic
exercise this path without a provider turn.

Execution output schemas use one strict recursive predicate at capsule
construction/deserialization and again after the SDK response. Capsules take
one caller-controlled Mapping/Sequence snapshot into owned immutable JSON data
before validating that snapshot, and `plan` independently repeats
canonicalization/validation before any durable write. The root and
every nested object must explicitly declare `properties`, list every property
exactly once in `required`, and set `additionalProperties = false`; arrays must
declare one explicit item schema; scalars accept only their type. Unsupported
keywords, optional or duplicate requirements, undeclared/missing output, and
open or itemless containers fail closed. Schema/output depth is limited to 32,
each object to 128 properties, a schema/output to 1,024 total properties, each
array to 1,024 items, property names to 256 UTF-8 bytes, and structured output
to 1 MiB. SDK structured output uses one strict JSON decoder that first decodes
bytes as strict UTF-8 without a BOM, then rejects
non-standard numeric constants, non-finite exponents, duplicate object keys at
every depth, and unpaired Unicode surrogates. All controller JSON
serialization rejects non-finite values defensively before SQLite or artifact
writes.

## Native runtime/profile/private-state matrix

| Surface | H3 authority | Persistence and proof |
| --- | --- | --- |
| Bundled runtime/app-server | Python SDK | Normal SDK child launch; no CLI/direct-RPC transport and no mandatory Bubblewrap wrapper |
| Provider/profile semantics | Native Codex config | Typed projection preserves selected provider definition, model catalog, permissions, MCP, skills, plugins, memories, hooks, projects, and shell environment; sanitized facts plus digest are durable |
| Model, effort, cwd/workspace | Controller capsule | Explicit SDK thread/turn inputs; they do not alter inherited permission authority |
| Approval and sandbox | Native Codex by default | Schema-v6 effective authority is rebound before resume; optional `read_only` and previously inherited restrictions can only be retained or tightened |
| Session/database/log state | Controller-private `CODEX_HOME` | New runtime home per run/milestone; projected config contains references only; global config remains source-only |
| Provider/MCP authentication | Environment references | Provider `env_key`, MCP bearer/header references, and structurally converted literal headers plus every stdio environment value reach only the SDK child environment; raw values are never persisted |
| Worktree/Git controls | Controller ownership/evidence | Canonical physical leases, schema-v8 baselines/terminal snapshots, bounded topology facts, and Git authority remain ownership/evidence controls, not an OS containment claim |

## Persisted-empty-thread boundary

The pinned SDK runtime does not materialize a resumable rollout at
`thread_start` alone. A new SDK client returns `no rollout found` if the first
client stops after identity but before the first turn creates the rollout. The
controller records that identity and never starts a replacement thread; the
resume error is therefore fail-closed rather than a duplicate-owner fallback.

The H3 real promotion sentinel injects its process boundary at the first
resumable checkpoint: thread identity and the first turn observation are both
durable, while validation, terminal result, state transition, and projection
remain incomplete. A fresh controller then resumes the same SDK thread, runs
validation, and returns with both the terminal result and projection durable.
That real observation proves survival across the process boundary, not their
relative write order. Result-before-projection ordering is proved separately by
the deterministic `before_projection` fault-injection regression, which reads
the committed terminal status at the exact projection boundary.

## Evidence

`docs/reviews/evidence/h3-controller-sentinel.json` retains the passing corrected
run for implementation commit `04e0f889678770bfecd7da3c0a8cbd03344f123f`.
It proves one Python-SDK/bundled-app-server dispatch through `codex-lb`, inherited
native `danger-full-access`/`never`, an injected post-turn process boundary,
fresh-process same-thread resume, the allowed workspace edit, equal Git-authority
digests, and unchanged global native config bytes. Schema-v8 terminal authority,
canonical workspace identity, directory topology, discovery-cycle rejection,
secret projection, exact pre-external revalidation, deterministic lease
selection, and strict recursive output schemas retain the proven
SDK/provider/model/permission route. Repair implementation `eb25987` leaves the
retained sentinel bytes and canonical schema unchanged. The active `codex-lb`
profile still projects all three configured MCP servers with zero ephemeral
secret references and the pinned runtime's config diagnostic loads it
successfully; no additional real provider run was consumed. External-write
denial is not an H3 requirement when the inherited native profile permits
repository-external writes.

Desktop visibility, idle wake, remote hosts, permission-profile survival, and
native review remain outside H3. Later milestones must label each as proven,
unsupported, not exposed, or not run; none is inferred from local SDK success.

## H4-A objective walking skeleton

H4-A adds `workflow.toml` as the typed role/limit authority and layers typed
review finding, repair, recovery, decision, budget, and lifecycle contracts on
the existing controller and SQLite path. H3's schema-v8 tables and the sole
Python SDK transport remain unchanged; H4 lifecycle facts are atomically
attached to the same SQLite connection in the controller-owned `.h4` fact
store, while state transitions remain authoritative in the H3 ledger.

The bounded walking skeleton is objective execution -> fresh read-only review
-> one deliberate promotion-blocking rejection -> same durable-owner repair ->
fresh read-only re-review -> acceptance. Finding identity, causal class,
severity, promotion impact/reason, evidence, criterion, optional `defer_to`,
and exact-prior-repair survival are typed and retained. Recovery outcomes keep
`CONTINUE_WITH_REPLAN`, `NEEDS_DECISION`, `EXTERNAL_BLOCKED`, and `FAILED`
distinct; stale review and exhausted limits are separately classified.

The one authorized real disposable pilot is retained at
`docs/reviews/evidence/h4-a-objective-pilot.json`. It used the Python SDK and
`codex-lb`, made an observable authorized edit, rejected the first review,
repaired through the same SDK thread, passed a fresh re-review, and finished in
`ACCEPTED` with two SDK turns and one repair. Evidence is sanitized to hashes,
counts, lifecycle kinds, route, and status; no provider prose or credentials
are persisted.

## H4-B multi-authority acceptance

H4-B extends the same H3 controller, SQLite fact authority, workspace lease,
checkpoint, finding lineage, and result-before-projection path. A capsule's
`objective`, `visual`, and `architecture` acceptance modes derive the executor,
objective/code-reviewer, visual-reviewer, architecture-reviewer, recovery, and
decision routes from `workflow.toml`; missing or aliased required routes fail
closed without model, effort, provider, permission, or transport substitution.
Visual reviewers receive an immutable fixed rendered-evidence digest. All
declared modes must accept, while severity remains independent of successor
promotion impact and non-blocking findings retain their `defer_to` reason.

An architecture rejection is durably recorded, diagnosed as a bounded
`CONTINUE_WITH_REPLAN` only when the revised strategy preserves intent,
public/persisted contracts, security/privacy boundary, material cost,
destructive authority, and milestone scope, then repaired by the same durable
owner and re-reviewed by fresh distinct authorities. Stale authority, route
unavailability, limits, notification failure, and terminal recovery outcomes
remain distinct typed facts.

The one authorized real disposable multi-authority pilot is retained at
`docs/reviews/evidence/h4-b-multi-authority-pilot.json`. It used one Python
SDK/`codex-lb` thread and two bounded turns, made an observable workspace edit,
reviewed fixed rendered evidence independently, rejected architecture,
accepted a same-boundary replan, repaired in the same workspace, and reached
`ACCEPTED` after six authority reviews. The retained packet contains only
sanitized hashes, route identities, counts, lifecycle kinds, and status.

## H6-C App-native hosting boundary

Schema v9 adds one `app_native_dispatches` authority beside the existing SDK
execution tables. `control --hosting app-native` plans and leases the selected
checkout, captures Git/protected/controller-state baselines, atomically claims
executor generation one, and returns one closed non-blocking native-task
action. It does not construct the SDK adapter, discover an app-server, or
attach to a Desktop socket. The hosting Codex app creates the visible task and
constructs one closed `HostReceipt` from the actual native response. It binds
the dispatch, action digest, controller challenge, claim capability and exact
host/thread pair. The receipt proves capability possession, not cryptographic
App attestation: the host workflow is the explicit trusted authority.
`app-bind` verifies that receipt; `app-result`
accepts one closed `ModelFacingResult` only for that capability and identity.
Prepare, bind, and result replay are idempotent; conflicts fail closed. Status
retains prepared/bound/terminal recovery facts in the same SQLite authority.

The action carries the repository-configured model and effort, exact existing
checkout, bounded prompt, and closed output contract. App result ingestion
re-runs validation and checks protected content, mutation scope, Git authority,
controller static state, and terminal workspace authority. Git-ignored build
output remains outside ordinary mutation/topology facts, while tracked and
non-ignored out-of-scope changes still fail. SDK-headless `control` remains the
default and is never substituted for an App-native request.

The SDK worker cannot invoke the host's native task action, so the visible-App
promotion proof remains host-only. After this H6-C execution is terminal, the
planning owner can generate a bounded same-run successor capsule and prepare
its action exactly as follows:

```bash
uv run codex-flow h6-app-pilot-capsule \
  --parent-run-id model-2c1f6c447b3f22b8ca2e91daaed16e07 \
  --state-root /home/adam/personal-workflow-skills.worktrees/python-sdk-controller
uv run codex-flow control --hosting app-native \
  --capsule /home/adam/personal-workflow-skills.worktrees/python-sdk-controller/.codex-flow/capsules/model-2c1f6c447b3f22b8ca2e91daaed16e07/h6-c-visible-app-pilot.json \
  --state-root /home/adam/personal-workflow-skills.worktrees/python-sdk-controller --json
```

The App host must then issue exactly the returned native action and run the
returned `app-bind`/`app-result` sequence. No UI visibility claim is made until
that host-only pilot binds a real visible thread.

Before any installed controller opens or migrates a live ledger, refresh and
prove the candidate package in this exact order, with `DIST` set to a newly
created temporary directory:

```bash
DIST="$(mktemp -d /tmp/codex-flow-candidate.XXXXXX)"
uv build --wheel --out-dir "$DIST"
uvx --from "$DIST"/codex_flow-0.2.0-py3-none-any.whl codex-flow --help
uv tool install --force "$DIST"/codex_flow-0.2.0-py3-none-any.whl
codex-flow schema-compatibility --state-root /absolute/path/to/selected-checkout
codex-flow status --run-id RUN --milestone-id MILESTONE \
  --state-root /absolute/path/to/selected-checkout --json
```

`schema-compatibility` opens the ledger read-only and reports both versions.
Only the final controller command may migrate v8 to v9 or canonicalize the
empty source-draft v9 App table. A non-empty draft table fails closed. Building,
proving and installing must finish first; an older schema-v8 executable must
not open the ledger after the candidate migrates it.
App-native visible pilot: passed
