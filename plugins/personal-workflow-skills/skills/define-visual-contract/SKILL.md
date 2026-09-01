---
name: define-visual-contract
description: Turn product context, real content, existing renders, brand constraints, and visual references into one explicit visual direction, sentinel set, and observable rubric before visual implementation. Use for frontend/UI, slides, document generation, visual systems, or other work whose success depends on qualitative rendered judgment. Do not implement the production artifact.
---

# Define Visual Contract

Create the visual decision artifact that implementation and independent review
will share.

This worker is a visual direction authority, not an implementation owner. It is
read-only except for writing the visual contract and supporting reference board
artifacts. Do not edit production code or presentation generators.

The controller owns dispatch, callbacks, routing, retries, successor
scheduling, worktrees, and ledger mutation. This skill owns visual cognition
and contract artifacts only.

## 1. Ground the direction in the actual subject

Identify:

- product or artifact;
- target audience;
- single primary job;
- real content and information hierarchy;
- established brand/product constraints;
- intended viewing environment and sizes;
- existing interface/deck/document baseline;
- references supplied or accepted by the user.

Use the subject's own domain, materials, vocabulary, and user behavior. Do not
select a generic “premium SaaS” style that could be applied unchanged to another
product.

When references conflict, identify the conflict and choose a recommended
resolution. Ask the user only when the conflict changes brand or product intent
materially.

## 2. Inspect actual references and baselines

Open the real references and current renders. Do not rely on filenames,
descriptions, component metadata, or extracted text alone.

For each reference record:

- what should be borrowed;
- what should not be copied;
- why it fits the subject;
- applicable artifact/view types;
- confidence and limitations.

Label what is observed in the reference or baseline as a source fact/evidence, what is
inferred as design rationale, and what remains a user-owned decision. Preserve
uncertainty where the provided material does not settle intent.

A deterministic design scout or searchable design catalog may suggest palettes,
patterns, typography, and anti-patterns. Treat it as retrieval support, not
qualitative authority.

## 3. Choose one coherent direction

Unless the user explicitly requests alternatives, select one direction.

Define:

### Direction

A short thesis describing the intended character and user impression.

### Signature

One memorable, content-appropriate device. Spend visual boldness in one place;
keep supporting elements disciplined.

### Palette

Four to six named colors with hex values and roles. Establish dominance,
support, accent, backgrounds, text, states, and contrast expectations.

### Typography

Define roles:

- display or title;
- body/reading;
- utility/data/caption.

Record required fonts, fallbacks, scale, weights, line-height, and fit
constraints. For generated office artifacts, distinguish requested fonts from
fonts reliable in the rendering/QA environment.

### Layout and density

Define grids, spacing rhythm, content width, alignment, hierarchy, responsive
behavior, and information density. State what changes at sentinel sizes.

### Motion and interaction

Use motion only when it serves orientation, causality, or feedback. Define
reduced-motion behavior and final semantic states.

### Content and microcopy

Use real domain language. Labels name what users recognize and control. Avoid
placeholder marketing copy that changes the design's meaning.

## 4. Artifact-specific requirements

### Frontend/UI

Define:

- shell and navigation hierarchy;
- primary task flow;
- responsive sentinel sizes;
- loading, empty, error, focus, and selected states;
- accessibility floor;
- long-text, zoom, and localization resilience.

### Slides

Define:

- narrative arc;
- deck motif;
- title/content/conclusion contrast;
- slide archetypes;
- chart and table treatment;
- maximum information density;
- image/icon approach;
- repeated alignment anchors;
- presenter and projection viewing assumptions.

Do not prescribe one layout for every slide.

### Documents/reports

Define:

- page rhythm and hierarchy;
- reading measure;
- heading/table/callout system;
- page-break and overflow behavior;
- print/PDF assumptions.

## 5. Name anti-patterns

List the specific defaults this work must avoid.

Examples:

- generic gradient hero unrelated to the subject;
- repeated equal-weight cards;
- decorative numbering without semantic order;
- dense text shrunk to fit;
- accent stripes used as the only motif;
- animation scattered without purpose;
- template layouts repeated regardless of content;
- visual identity copied from the parent brand when differentiation is required.

Anti-patterns must be relevant to this brief, not a universal taste manifesto.

## 6. Define three to five sentinels

Sentinels must expose the hardest and most representative visual decisions.

Each sentinel records:

- ID and artifact/view;
- exact input/content state;
- intended size or viewing context;
- why it is diagnostic;
- required elements;
- failure modes to inspect;
- render path convention.

Examples:

- desktop dense search results;
- narrow responsive workspace;
- empty/error state;
- data-heavy slide;
- narrative opening slide.

Do not scale the visual system before the sentinel set passes.

## 7. Write an observable rubric

Use criteria with explicit pass conditions:

- hierarchy;
- composition;
- typography;
- density;
- alignment and rhythm;
- content fidelity;
- responsive/overflow behavior;
- interaction and accessibility;
- distinctiveness and subject fit;
- consistency without monotony.

Avoid vague criteria such as “looks professional.” State what a reviewer should
observe.

## 8. Write the artifacts

Write:

- `visual-contract.yaml` conforming to
  `schemas/visual-contract.schema.json`;
- `visual-contract.md`;
- optional annotated reference board paths.

Use this Markdown shape:

```markdown
# Visual Contract: <artifact>

## Subject, audience, and job
## Direction
## Reference analysis
## Signature
## Palette
## Typography
## Layout and density
## Motion and interaction
## Content hierarchy and microcopy
## Artifact-specific rules
## Anti-patterns
## Sentinels
## Promotion rubric
## Open material decisions
```

Return artifact paths and stop. The controller routes implementation separately.
