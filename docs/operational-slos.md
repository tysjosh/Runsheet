# Operational SLOs

This document defines the Service Level Objectives (SLOs) for autonomous agents and periodic jobs in the Runsheet logistics platform. These SLOs establish measurable targets for platform reliability and inform alerting thresholds.

## Agent Restart SLOs

| SLO | Target | Constant | Description |
|-----|--------|----------|-------------|
| Max Restart Latency | 5 seconds | `SLO_MAX_RESTART_SECONDS` | Maximum time from agent crash detection to successful restart |
| Max Consecutive Failures | 3 | `SLO_MAX_CONSECUTIVE_FAILURES` | Maximum restart attempts within the restart window before escalation |
| Restart Window | 300 seconds (5 min) | `SLO_RESTART_WINDOW_SECONDS` | Rolling window for counting consecutive restart attempts |
| Min Uptime | 99.0% | `SLO_MIN_UPTIME_PCT` | Minimum uptime percentage per rolling 24-hour window |
| Uptime Window | 86400 seconds (24 hr) | `SLO_UPTIME_WINDOW_SECONDS` | Rolling window for uptime percentage calculation |

## Periodic Job SLOs

| SLO | Target | Constant | Description |
|-----|--------|----------|-------------|
| Max Cycle Duration | 5 seconds | `SLO_MAX_CYCLE_DURATION_SECONDS` | Maximum execution time for a single monitoring cycle |
| Schedule Drift | ±10% | `SLO_SCHEDULE_DRIFT_PCT` | Maximum deviation from the configured polling interval |

## Measurement Methods

### Agent Uptime Percentage

Uptime is calculated over a rolling 24-hour window using interval-based tracking:

1. Each agent maintains a list of `UptimeRecord` entries, each with a start time, end time, and `is_up` flag.
2. When an agent starts or restarts successfully, an uptime interval begins.
3. When an agent crashes or enters the restarting state, the uptime interval closes and a downtime interval begins.
4. The uptime percentage is: `(total_up_seconds / total_tracked_seconds) * 100`.
5. Records older than 24 hours are pruned to limit memory usage.

### Restart Latency

Restart latency is measured as the wall-clock time between crash detection (agent task completion with exception) and the agent returning to `running` status. The scheduler enforces a 0.5-second pause before restart to avoid tight crash loops, keeping total restart time well within the 5-second SLO.

### Cycle Duration

Each monitoring cycle's duration is measured by the `AutonomousAgentBase._run_loop()` method and logged via the Activity Log Service. The benchmark test verifies that a single cycle completes within the `SLO_MAX_CYCLE_DURATION_SECONDS` budget.

## Alerting Thresholds

| Alert | Condition | Severity | Action |
|-------|-----------|----------|--------|
| `agent_uptime_slo_violation` | Agent uptime < 99% in rolling 24-hour window | Warning | Investigate agent stability; check logs for recurring errors |
| `agent_failed` | Agent exceeds max consecutive failures (3 in 5 min) | Critical | Immediate investigation required; agent will not auto-restart |

### Alert Payload

**`agent_uptime_slo_violation`**:
- `agent_id`: The identifier of the affected agent
- `uptime_pct`: Current uptime percentage
- `slo_target`: The SLO target (99.0%)
- `window_seconds`: The measurement window (86400 seconds)

**`agent_failed`**:
- `agent_id`: The identifier of the failed agent
- `error`: The last error message before failure

## Managed Agents

| Agent | Restart Policy | Description |
|-------|---------------|-------------|
| `delay_response_agent` | `on_failure` | Monitors shipments for delays and triggers automated responses |
| `fuel_management_agent` | `on_failure` | Monitors fuel levels and triggers refill alerts |
| `sla_guardian_agent` | `on_failure` | Monitors SLA compliance and triggers breach alerts |

## SLO Constants Location

All SLO constants are defined in `Runsheet-backend/bootstrap/agent_scheduler.py` and can be adjusted via code changes. Future work may externalize these to environment variables or a configuration service.
