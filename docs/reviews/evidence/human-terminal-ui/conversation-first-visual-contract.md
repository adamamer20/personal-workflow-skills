# Visual Contract: Codex Flow terminal workspace

## Subject, audience, and job

Codex Flow operators supervise one durable controller and several SDK workers. Their primary job is to see who is active, understand the selected controller or worker conversation, and take the smallest safe contextual action. Ledger identities and retry facts support diagnosis but are not the product hierarchy.

The target environment is an interactive terminal at 120x40 and exact 80x24, with light, dark, and no-color operation. The controller is a real selectable session with its own Sol generation, SDK thread, state, conversation, and decisions; it is not merely a decorative heading above workers.

## Direction

Build a compact coding workspace rather than an operations dashboard: quiet chrome, dense navigation, a dominant transcript, and contextual controls. It should feel as immediate as a modern coding CLI while retaining Codex Flow's controller-to-worker hierarchy and durable truth.

## Reference analysis

The inspected baseline has a useful two-column structure, exact keyboard controls, explicit supervisor state, and visible attention items. Keep those facts.

Do not preserve its oversized typography, tall equal-weight rows, repeated `Current task`, raw empty-delta diagnostics, full-width command legend, or controller-as-count-only summary. Coding CLIs are a pattern reference for compact session navigation, readable chat roles, status badges, transient progress, and command palettes; do not copy another product's branding or conversational semantics.

## Signature

The signature is one continuous session rail:

```text
◆ Controller  Sol · deciding
  ├─ Implementer  Luna · working
  ├─ Reviewer     Luna · queued
  └─ Browser      Luna · complete
```

Controller and workers use the same selection model. Selecting any row opens that exact SDK conversation and changes the contextual action surface. The tree communicates ownership without a separate oversized controller panel.

## Palette

- `Midnight` `#0B1220`: dominant dark background and transcript canvas.
- `Paper` `#F5F7FA`: dominant light background and transcript canvas.
- `Slate` `#667085`: secondary labels, timestamps, inactive borders, and metadata.
- `Electric blue` `#3B82F6`: focus, selected session, links, and non-destructive primary action.
- `Emerald` `#22C55E`: running/healthy state only.
- `Amber` `#F59E0B`: attention, deferred work, and confirmation boundaries; destructive stop uses the terminal theme's error token with text/icon reinforcement.

Text must meet readable terminal contrast in both themes. No-color mode replaces every color-only distinction with glyph, label, border, or weight.

## Typography

Use the terminal's native monospace font at its native size. Keep body text at one cell scale; do not simulate display typography with oversized rows. Use weight and restrained uppercase for utility labels only.

- Workspace title: one line, semibold if supported.
- Session/task label: normal case, semibold selection.
- Transcript role and state badge: compact label, never a full-width heading.
- Body: normal terminal text with readable blank-line rhythm.
- Metadata: dim/slate, one line when possible.

## Layout and density

At 120x40:

- top bar: one row for repository/workflow, connection state, active count, and attention count;
- session rail: 28-34 columns, controller first, then indented workers, with compact one- or two-line selected rows;
- transcript: the dominant remaining width and height;
- optional inspector: a narrow right drawer or transcript overlay opened only by `T`;
- contextual action bar: one row plus an optional focused prompt/editor;
- global key help: command palette or one terse footer row, not a permanent multi-command paragraph.

At 80x24:

- session rail collapses to a 5-7 row top strip containing the controller and active/attention sessions;
- transcript keeps at least 10 rows;
- one compact context row shows state, position, and the two most relevant actions;
- rare actions move to the command palette;
- controller selection, conversation scroll position, and attention access remain discoverable.

Long task summaries truncate with an explicit ellipsis in the rail and remain available in the transcript header or inspector. IDs never determine column width.

## Motion and interaction

There is no ambient timer or provider polling. Animate only current local state:

- a single-cell spinner may indicate a known active controller or worker turn;
- selection and loading transitions preserve the previous stable transcript until the new subject is identified, then show a bounded loading state;
- reduced/no-motion terminals use a static `working` glyph and label;
- `Tab`/arrow keys move through sessions, `[` and `]` scroll, `F` toggles active-only, `L` appears only when older pages exist, and `Ctrl+P` or the existing palette binding opens all commands;
- steer, stop, claim, acknowledge, and checkpoint changes remain exact-identity confirmed.

## Content hierarchy and microcopy

Primary order:

1. Controller or worker task summary.
2. Human state: `Deciding`, `Working`, `Waiting for you`, `Queued`, `Complete`, `Failed`.
3. Persisted user/agent conversation.
4. Contextual action.
5. Model, age, generation, thread, attempt, retry, and route details.

Use `Controller`, `Implementer`, `Reviewer`, `Send direction`, `Stop turn`, `Load older`, and `Details`. Never show `Current task` when a bounded task summary exists. Empty SDK deltas are omitted rather than rendered as messages. Tool calls appear as compact named events with bounded summaries, not raw transport event names.

## Artifact-specific rules

- The controller appears as the first selectable session whenever a current or retained controller generation exists.
- Controller rows show Sol/model, generation state, task/decision summary, and attention badge; selection loads the exact controller SDK thread conversation through the existing history boundary.
- Worker rows show role, bounded task summary, state, and age; model/effort are secondary.
- Attention decisions remain visible under active-only filtering and link back to their controller/worker subject.
- Offline mode retains the last stable snapshot and disables mutation.
- Technical details are secondary and may use a drawer; the transcript never becomes a ledger dump.
- The TUI consumes existing typed local APIs and owns no lifecycle, polling, transcript persistence, or alternate state.

## Anti-patterns

- A decorative `CONTROLLER · N workflows` header that cannot be selected.
- Tall equal-weight worker cards and large unused blank areas.
- Repeated `Current task`, `not exposed ago`, or raw SDK delta event names.
- A permanent footer listing every shortcut.
- Color as the only active/attention/focus signal.
- A dashboard grid of boxes that gives transcript, metadata, and actions equal visual weight.
- Spinner activity without a durable active turn.
- Hiding common context actions or controller access behind the command palette.

## Sentinels

- `conversation-light-wide.svg` at 120x40: true light workspace with selectable controller, three worker states, controller conversation selected, compact action bar, and closed inspector.
- `conversation-dark-wide.svg` at 120x40: dark worker conversation with one bounded tool-call summary, active spinner/state, older-page affordance, and open technical drawer.
- `conversation-light-80x24.svg` at 80x24: compact controller-selected view with the session strip, at least two transcript messages, position cue, and context actions visible.
- `conversation-no-color-80x24.svg` at 80x24: monochrome active-only view proving controller/worker hierarchy, focus, attention, loading, and palette discoverability without color.

Render paths remain under `docs/reviews/evidence/human-terminal-ui/` using the existing exact filenames. These sentinels are implementation targets; existing bytes do not satisfy this revised contract.

## Promotion rubric

- The controller is selectable and visually peer-compatible with worker sessions while remaining the tree root.
- A first glance identifies the selected subject, its task, whether it is active, and whether human attention is required.
- The transcript occupies the dominant readable area and distinguishes user, agent, and tool events without raw transport noise.
- At 120x40, session rail, transcript, status, and contextual controls fit without oversized rows or unused dominant whitespace.
- At 80x24, controller access, active session, two transcript messages, scroll/position, and relevant actions remain visible.
- Active-only filtering never hides attention decisions or the current controller.
- Light, dark, and no-color states remain distinct and readable; every state has a non-color cue.
- Rare commands are discoverable through a palette; common selected-subject actions remain directly reachable.
- Offline, loading, empty, stale, incomplete, failed, and confirmation states are explicit and do not invent content.

## Open material decisions

None. Independent visual promotion review remains omitted by user choice; implementation still requires native-size self-inspection of all four sentinels and objective headless interaction tests.

## Source boundary

`Complete` means all persisted user/agent messages returned with `itemsView=full` by official shared-SDK `thread/read`. Non-message tool/provider interactions may be lossy and are not reconstructed. Raw transcript content is redacted before IPC and is never retained in evidence.
