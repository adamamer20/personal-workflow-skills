# Dedup And Naming Rubric

Use this rubric to decide whether to extract shared code or rename symbols.

## Dedup Decision

Choose `extract now` when all are true:
- At least 2 active call sites have the same responsibility
- Inputs/outputs can be represented with one stable typed interface
- Differences are parameterized, not hard-coded branches

Choose `wait` when any is true:
- Behavior differs in error handling or side effects
- Extraction requires many boolean flags
- Readability decreases for domain-specific workflows

## Naming Decision

Rename when any is true:
- Two names are lexically close but semantically different
- One name is reused across layers with different meanings
- Name hides lifecycle role (`builder`, `runner`, `store`, `migration`)

Keep name when all are true:
- Term is domain-standard and unambiguous in scope
- Local context makes intent obvious without extra qualifiers

## Severity

- High: bug risk or data drift likely
- Medium: maintenance tax and onboarding confusion
- Low: style-level consistency opportunity
