# Modeling Rubric

Use this rubric when replacing weak typing with stronger types.

## Choose The Model Shape

### Prefer `pydantic.BaseModel`

Use for:
- request and response payloads
- validated JSON or dict boundaries
- persisted structured payloads
- data crossing service or process boundaries

Prefer when malformed input should fail immediately and visibly.

### Prefer `@dataclass(slots=True)`

Use for:
- internal config objects
- lightweight value objects
- orchestration inputs that are already validated
- small records passed across internal layers

Prefer when construction is trusted and low overhead matters more than field-level validation.

### Prefer `TypedDict`

Use only for:
- narrow raw mapping boundaries
- interop with libraries that naturally expose dictionary shapes
- temporary bridging during an incremental refactor

Convert to a dataclass or Pydantic model as early as practical.

### Prefer `Protocol`, `TypeVar`, `Generic`, `ParamSpec`

Use for:
- reusable algorithms parameterized by record or payload type
- strategy objects and pluggable behaviors
- wrappers that preserve input/output type relationships
- callables whose signatures must remain linked

Do not add generics when one concrete model would be clearer.

## Eliminate Weak Types

### Replacing `dict[str, Any]`

Ask:
1. Is the key set known?
2. Does the boundary require validation?
3. Is the data internal or external?

Then:
- known external shape -> `BaseModel`
- known internal shape -> `@dataclass(slots=True)`
- partially known raw mapping -> `TypedDict` at the edge, then convert

### Replacing `Any`

Keep only when:
- a third-party library is untyped
- the boundary is intentionally dynamic and immediately normalized
- the alternative would be dishonest

Contain `Any` to one adapter or parsing layer. Do not let it cross service boundaries or spread through core domain code.

### Replacing `object`

Keep only when code genuinely treats the value as opaque.

If code branches on attributes, keys, literals, or shape, `object` is too broad. Use a protocol, union, model, or type variable instead.

## Boundary Rules

- Convert raw dictionaries to typed models at the first honest boundary.
- Update all repository call sites in the same change by default.
- Do not preserve legacy untyped interfaces for compatibility unless explicitly requested.
- Do not silently coerce missing or invalid fields just to satisfy a type.
- Mirror runtime invariants in the chosen model instead of validating the same shape repeatedly downstream.

## Validation Rules

- Run the repository's documented focused tests and full check.
- Exercise runtime type enforcement only when the repository already defines it as a test or validation path.
- If a dedicated static checker exists, run it.
- If no dedicated static checker exists, say so explicitly in the result and avoid overstating confidence.
