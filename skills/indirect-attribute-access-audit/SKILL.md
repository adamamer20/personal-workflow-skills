---
name: "indirect-attribute-access-audit"
description: >-
  Find and prioritize Python interface-drift risks caused by indirect attribute
  or method access. Use when auditing code for getattr, setattr, hasattr,
  dynamic method calls, fallback-heavy interface checks, weak duck typing, or
  when a user asks to enforce direct typed access through concrete classes,
  protocols, dataclasses, Pydantic models, or other validated boundaries.
---

# Indirect Attribute Access Audit

## Overview

Use this skill to remove dynamic attribute access from expected Python interfaces. The goal is direct access that lets static analysis, tests, and runtime failures expose contract drift early.

## Audit Workflow

1. Run the bundled scanner from the repository root:

   ```bash
   python3 "$SKILL_ROOT/scripts/find_indirect_attribute_access.py" . --fail-on-findings
   ```

   Resolve `SKILL_ROOT` to the directory containing this `SKILL.md` before running it.

2. Inspect every finding before editing. Treat these calls as violations by default:
   - `getattr(obj, "field")`
   - `getattr(obj, "method")(...)`
   - `getattr(obj, "field", default)`
   - `setattr(obj, "field", value)`
   - `hasattr(obj, "field")`
   - `builtins.getattr`, `builtins.setattr`, or `builtins.hasattr`

3. For each violation, identify the expected interface and replace dynamic access with one of:
   - A concrete class, dataclass, SQLModel, or Pydantic model.
   - A `typing.Protocol` when several implementations share the same surface.
   - A typed boundary conversion from raw JSON or dictionaries before business logic.
   - An explicit narrow dispatch table when behavior is truly dynamic and finite.

4. Update all call sites in the same change when the interface changes. Do not add compatibility shims, dual paths, or silent fallbacks unless the user explicitly requests them.

5. Re-run the scanner and the relevant tests. For repository-wide changes, use the repo's documented check command.

## Triage Rules

- Mark `setattr` as high risk because it hides write-side contract drift.
- Mark `getattr` with a default as high risk because it silently accepts missing members.
- Mark `getattr(...)(...)` as high risk even if the scanner reports only the `getattr` call; dynamic method dispatch bypasses static checks.
- Mark `hasattr` as medium or high risk depending on whether the branch masks an expected interface member.
- Keep true plugin or framework reflection rare, documented, and isolated behind a typed adapter. Do not let reflection leak into application, storage, or pipeline logic.

## Replacement Patterns

Prefer direct typed access:

```python
class HasWorkId(Protocol):
    work_id: str


def load_work(item: HasWorkId) -> str:
    return item.work_id
```

Prefer explicit finite dispatch when the operation is genuinely selected by name:

```python
HANDLERS: Mapping[str, Callable[[Payload], Result]] = {
    "create": create_payload,
    "update": update_payload,
}

handler = HANDLERS[action]
return handler(payload)
```

Avoid replacing `getattr` with raw dictionary access in domain logic. Convert raw dictionaries into typed boundary models first when the structure is known.

## Reporting

Report findings with file and line references, the call used, why it is risky, and the recommended direct-access replacement. If you leave a dynamic access in place, state the narrow reason and the typed boundary that contains it.
