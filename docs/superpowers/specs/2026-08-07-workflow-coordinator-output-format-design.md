# Workflow Coordinator Output Formatting Design

## Goal

Improve the readability of every final `Workflow Coordinator` response without
changing its facts, ordering, or meaning. Discovery progress output remains
unchanged.

## Current Problem

`V4ResponseAgent` already makes one model call to produce the final answer, but
`render_readonly()` then applies `" ".join(text.split())`. This collapses every
line break into a space, so numbered items, paragraphs, and Markdown lists are
printed as one dense line.

## Design

Keep the existing single `V4ResponseAgent` call and make two minimal changes:

1. Extend its existing system prompt with presentation-only requirements:
   preserve the answer content, use natural paragraph breaks, put list items on
   separate lines, and avoid unnecessary blank lines.
2. Replace whitespace flattening with `strip()` so the model's line breaks reach
   the terminal unchanged.

No new formatter module, second model call, semantic rewrite, list parser, or
heuristic sentence splitting will be added.

## Data Flow

`EvidenceConclusion` → existing response prompt → one model response → trim only
leading/trailing whitespace → `Workflow Coordinator` terminal output.

## Error Handling

The existing exception handling and deterministic fallback remain unchanged.
If the model call fails, the current multiline fallback is returned.

## Tests

Add focused tests proving that:

- model-produced line breaks and list indentation are preserved;
- leading and trailing whitespace is removed;
- response facts and order are unchanged;
- no additional model call is introduced;
- existing fallback behavior continues to work.

Run the focused V4 response/coordinator tests followed by the full test suite.
