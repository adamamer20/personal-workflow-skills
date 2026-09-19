# Evidence: where current routing selects Astra

## Conclusion

Pre-change source policy and executable defaults selected Astra for planning,
visual work, visual review, and recovery. The user-requested Sol-first policy therefore
requires synchronized changes to the canonical instructions, public routing
table, controller configuration, validator, focused tests, and the private
controller CLI default. Cognitive skill bodies are already model-independent.

## Claims and support

### Claim 1: Astra is a configured default, not only an escalation

Direct evidence: `workflow.toml` assigns Astra to `planner`,
`visual-reviewer`, and `recovery`; `config/workflow.toml.example` assigns Astra
to planning, visual execution/review, and both recovery roles. `README.md` and
`templates/AGENTS.md` publish the same mapping.

Confidence: high.

### Claim 2: validation deliberately pins the Astra routes

Direct evidence: `scripts/validate.py` contains exact expected route pairs and
required prose for Astra defaults. `tests/test_workflow_assets.py` asserts those
pairs and rejects drift.

Confidence: high.

### Claim 3: one runtime-facing default bypasses the TOML role map

Direct evidence: `src/codex_flow/cli.py` defaults the private
`program-controller-generation` model option to `gpt-6-astra`, while the harness
controller helper already returns Sol Medium.

Confidence: high.

## Contradictions

The active plan names Astra Low for the pending refresh recovery, while the
new user instruction makes Astra exceptional for future dispatch. The retained
capsule is byte-identity-bound to its historical authority, so rewriting that
already-selected route would invalidate its executable identity.

## Unknowns

The protected untracked conversational-TUI visual contract also names Astra
Low. It belongs to another mutable surface and is not safe to rewrite in this
change; any future dispatch from it must apply the newer repository routing
authority unless it is already pinned.

## Recommended follow-up

Planning can proceed.

## Source and artifact paths

- `AGENTS.md`
- `templates/AGENTS.md`
- `templates/AGENTS.workflow.md`
- `README.md`
- `workflow.toml`
- `config/workflow.toml.example`
- `scripts/validate.py`
- `tests/test_workflow_assets.py`
- `src/codex_flow/cli.py`
- `docs/reviews/peer-thread-workflow.md`
