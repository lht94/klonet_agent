# Resume Project-Priority Redesign

## Goal

Rebalance the one-page Agent-development resume so that the three projects, especially Klonet and the Klonet Agent, carry the technical narrative. Preserve readability at the existing page size and do not reduce project body text below the current readable range.

## Confirmed Content Decisions

- The master's major is Network Engineering.
- Remove unimplemented Coding collaboration claims and the MCP keyword.
- Retain only verified RAG measurements: scope/task type accuracy 100%, Recall@3 83.3%, and general-question RAG false-trigger rate 0%.
- Add two verified Klonet capabilities: dynamic space-air-ground topology simulation and WebTerminal.

## Layout

- Keep the left column for contact details, education, concise technical keywords, and condensed achievements.
- Move and condense professional skills and supplemental information into the left column.
- Remove the right-side professional skills and supplemental information blocks to reserve their vertical space for project cards.
- Use the recovered right-column space to expand the first two projects; keep the Kubernetes project concise and visually separate.

## Project Narrative

- Klonet: describe the platform through architecture, topology scheduling, dynamic space-air-ground simulation, heterogeneous networking, WebTerminal, high-performance networking, and outcomes.
- Klonet Agent: express the business loop, profile-based architecture, RAG routing/evaluation, privilege workflow, and memory/context engineering in varied prose rather than repeated formulaic sentence starts.
- K8S plugin: retain its existing problem, implementation, and measured results.

## Validation

- Create a copied PPTX; do not overwrite the source.
- Extract PPTX text to confirm key claims are present and removed claims are absent.
- Render the finished slide to PNG and inspect for clipping, overlap, unreadable density, and insufficient margins.
