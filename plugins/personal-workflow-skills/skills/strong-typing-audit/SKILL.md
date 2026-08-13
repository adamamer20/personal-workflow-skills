---
name: strong-typing-audit
description: Audit and strengthen weak Python typing across a repository. Use when Codex needs to replace `dict[str, Any]`, broad `Any`/`object`, ad-hoc raw dictionaries, or unstable container types with precise dataclasses, validation models, generics, protocols, or narrowly scoped boundary types; or when tightening application and pipeline interfaces for static checking without spreading runtime-only enforcement through production paths.
---

# Strong Typing Audit

Strengthen type boundaries without inventing speculative abstractions. Prefer honest domain models, stable variable types, and fail-fast validation over permissive containers and silent coercion.

## Workflow

1. Scope the typing work.
   Determine whether the user wants:
   - implementation across selected files
   - a repository audit with findings only
   - a hybrid pass with fixes plus remaining gaps

2. Find weak typing sites quickly.
   Search for patterns such as:
   - `Any`, `object`, `cast(...)`
   - `dict[str, Any]`, `dict[str, object]`, `Mapping[str, Any]`
   - raw `json.loads(...)` results flowing through multiple layers
   - functions returning heterogeneous dictionaries
   - variables that change type across branches
   - repeated key access into the same dictionary shape

3. Classify each boundary before changing code.
   Choose the narrowest honest type:
   - Use `pydantic.BaseModel` for validated external boundaries such as API payloads, persisted JSON payloads, and other data that must be parsed and rejected when malformed.
   - Use `@dataclass(slots=True)` for internal configuration objects, value objects, and small immutable or near-immutable records that do not need Pydantic validation.
   - Use `TypeVar`, `Generic`, `Protocol`, `ParamSpec`, or `TypeAlias` when the real abstraction is reusable behavior or a container family rather than one concrete record shape.
   - Use `TypedDict` only as a narrow temporary boundary for raw mapping-shaped data when a full model is not yet appropriate; convert to a real model early.
   - Use `object` only when the value is truly opaque and the code does not inspect structure.
   - Use `Any` only for unavoidable untyped third-party edges, and contain it at the boundary instead of letting it spread inward.

4. Push typing inward from the boundary.
   Replace weak boundary types first, then update all downstream call sites in the same change.
   Do not add compatibility shims or dual-typed paths unless the user explicitly requests them.

5. Remove coercion and reconstruction.
   Replace dictionary key digging and shape guessing with typed construction at the edge.
   If a field is known, model it. If the shape varies, represent that variability explicitly with unions, tagged models, protocols, or generic parameters.

6. Keep runtime enforcement scoped correctly.
   Support static checking always.
   Follow the repository's existing runtime type-checking policy when one exists.
   Do not introduce production-path runtime wrapping unless the repository requires it and the user asked for it.

7. Validate the change honestly.
   Run the narrowest meaningful checks first, then broader repo checks when the change is substantial.
   Use the repository's documented focused tests, static checker, and full check.
   If the repository has no dedicated static type checker configured, state that explicitly. Do not claim static validation passed unless a real static checker ran.

## Refactor Heuristics

- Prefer one stable type per variable through the whole function.
- Prefer constructing typed models once over passing raw dictionaries through multiple layers.
- Prefer explicit unions to sentinel-heavy `None` plus extra flags when multiple states are real.
- Prefer named type aliases for repeated compound types.
- Prefer protocols over inheritance when only behavior matters.
- Reject generic wrappers that hide a concrete domain shape that should just be a model.
- Reject `dict[str, Any]` returns from internal helpers when the set of keys is known.
- Reject `object` as a placeholder for “I have not modeled this yet.”

## Output Expectations

If the user asked for an audit or review, return:
1. Findings first, highest impact first
2. The replacement type or model for each weak boundary
3. File references with line numbers
4. Validation status and remaining gaps

If the user asked for implementation, do the refactor and report:
1. What boundaries were tightened
2. Which weak types remain and why
3. What checks ran
4. Any static-checking gap that still exists in the repository

Load `references/modeling-rubric.md` when deciding between dataclasses, Pydantic models, generics, protocols, and narrow mapping boundary types.
