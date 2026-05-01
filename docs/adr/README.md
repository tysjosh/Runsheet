# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) for the Runsheet logistics platform. Each ADR documents a significant architectural choice, its context, the decision made, and the consequences.

## ADR Index

| Number | Title | Status | Link |
|--------|-------|--------|------|
| 001 | Agent Orchestration Model | Accepted | [001-agent-orchestration-model.md](001-agent-orchestration-model.md) |
| 002 | Safety Confirmation Model | Accepted | [002-safety-confirmation-model.md](002-safety-confirmation-model.md) |
| 003 | Domain Decomposition | Accepted | [003-domain-decomposition.md](003-domain-decomposition.md) |
| 004 | WebSocket Architecture | Accepted | [004-websocket-architecture.md](004-websocket-architecture.md) |
| 005 | Elasticsearch Data Layer | Accepted | [005-elasticsearch-data-layer.md](005-elasticsearch-data-layer.md) |

## ADR Format

Each ADR follows the standard format:

- **Title**: Short descriptive name
- **Status**: Proposed, Accepted, Deprecated, or Superseded
- **Context**: The situation and forces at play
- **Decision**: What was decided and why
- **Consequences**: The resulting effects, both positive and negative

## Creating a New ADR

1. Copy the template below
2. Number sequentially (e.g., `006-your-decision.md`)
3. Update this README index
4. Submit for review

```markdown
# ADR NNN: Title

## Status
Proposed

## Context
[Describe the situation and forces at play]

## Decision
[Describe the decision and rationale]

## Consequences
[Describe the positive and negative effects]
```
