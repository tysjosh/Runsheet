# ADR 001: Agent Orchestration Model

## Status

Accepted

## Context

The Runsheet logistics platform includes an AI-powered conversational interface that handles user requests across multiple operational domains: fleet management, scheduling, fuel monitoring, ops/shipment tracking, and reporting. The platform needed an architecture for routing user requests to the appropriate domain logic and synthesizing responses.

Two primary approaches were considered:

1. **Single monolithic agent**: One large language model agent with access to all tools across all domains. The agent decides which tools to call based on the full context of the conversation.

2. **Keyword-based routing to specialist agents**: An orchestrator classifies user intent using keyword matching against a routing table, then delegates to domain-specific specialist agents (FleetAgent, SchedulingAgent, FuelAgent, OpsIntelligenceAgent, ReportingAgent). Each specialist has access only to its domain's tools.

The platform serves logistics operations where response latency matters, tool sets are well-partitioned by domain, and the number of available tools is large enough that a single agent would face context window and tool selection challenges.

## Decision

We chose keyword-based routing to specialist agents via an `AgentOrchestrator`.

The orchestrator maintains a `ROUTING_TABLE` mapping domain names to trigger keywords. When a user message arrives, the orchestrator:

1. Classifies intent by scanning the message for keyword matches against each domain's keyword list.
2. Falls back to the reporting agent if no domain matches.
3. Detects complex multi-step requests (containing phrases like "and then", "followed by") and routes them through an `ExecutionPlanner` for structured plan execution.
4. For simple requests, invokes matched specialist(s) sequentially and synthesizes their results.

Each specialist agent (e.g., `FleetAgent`, `SchedulingAgent`) implements an `async handle(task, context)` interface and has access only to its domain's tools (search tools, mutation tools, lookup tools).

Key design choices:

- **Keyword matching over LLM-based classification**: Keyword matching is deterministic, fast (sub-millisecond), and debuggable. It avoids an extra LLM call for routing, reducing latency and cost.
- **Domain-scoped tool access**: Each specialist sees only its domain's tools, reducing the chance of incorrect tool selection and keeping the tool context small.
- **Multi-domain fallback**: When keywords from multiple domains match, all matched specialists are invoked and results are synthesized.
- **ExecutionPlanner for complex requests**: Multi-step requests are decomposed into a structured plan rather than handled as a single prompt, improving reliability for cross-domain operations.

## Consequences

### Positive

- **Low latency**: Keyword classification adds negligible overhead compared to an LLM routing call. Simple requests go directly to one specialist.
- **Debuggability**: The routing decision is fully deterministic and logged. When a request is misrouted, the fix is adding or adjusting keywords in the routing table.
- **Isolation**: Specialist agents can be developed, tested, and modified independently. A change to fuel tools does not affect the scheduling agent.
- **Scalability**: New domains are added by creating a new specialist agent and adding an entry to the routing table. No changes to existing specialists are needed.
- **Cost efficiency**: No additional LLM calls are needed for routing. The LLM is only invoked within the specialist agent that handles the request.

### Negative

- **Keyword ambiguity**: Some user messages may match multiple domains or no domains. The fallback to reporting mitigates the no-match case, but multi-match can produce verbose responses.
- **Maintenance burden**: The keyword list must be manually maintained as the domain vocabulary evolves. New terminology requires routing table updates.
- **Limited semantic understanding**: Keyword matching cannot handle nuanced intent that requires understanding context or synonyms not in the keyword list. For example, "Where is my package?" would not match the fleet domain unless "package" is added as a keyword.
- **Sequential execution**: When multiple specialists are matched, they execute sequentially rather than in parallel, which adds latency for multi-domain requests.
