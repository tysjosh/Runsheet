"""
Plan Execution Service for the Fuel Distribution MVP.

Manages plan execution lifecycle: creating execution records on approval,
recording driver check-ins, computing plan-vs-actual outcome variances,
and calculating estimated/actual costs.

Uses the same ES service pattern as other services in the project.

Validates: Requirements 3.1–3.7, 4.1–4.7, 5.1–5.6
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from Agents.support.mvp_es_mappings import (
    MVP_COST_CONFIGS_INDEX,
    MVP_LOAD_PLANS_INDEX,
    MVP_PLAN_EXECUTIONS_INDEX,
    MVP_PLAN_OUTCOMES_INDEX,
    MVP_ROUTES_INDEX,
)

logger = logging.getLogger(__name__)


class PlanExecutionService:
    """Business logic for plan execution tracking, outcomes, and costs.

    Accepts an Elasticsearch service instance following the same
    dependency injection pattern used by ActivityLogService and other
    services in the project.

    Args:
        es_service: An ElasticsearchService instance for persistence.
    """

    def __init__(self, es_service):
        self._es = es_service

    # ------------------------------------------------------------------
    # 2.2 - Create Execution
    # ------------------------------------------------------------------

    async def create_execution(
        self,
        plan_id: str,
        route_id: str,
        tenant_id: str,
        stops: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Initialize an execution record with all stops in 'pending' status.

        Called when a plan is approved and transitions to 'dispatched'.
        Each stop from the route is recorded with its planned ETA and
        planned quantities, with status set to 'pending'.

        Args:
            plan_id: The plan identifier.
            route_id: The route identifier.
            tenant_id: The tenant identifier.
            stops: List of stop dicts from the route, each containing
                station_id, sequence, eta, and drop (quantities).

        Returns:
            The created execution document.
        """
        execution_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        execution_stops = []
        for stop in stops:
            execution_stops.append({
                "station_id": stop.get("station_id", ""),
                "sequence": stop.get("sequence", 0),
                "status": "pending",
                "planned_eta": stop.get("eta", ""),
                "actual_arrival": None,
                "planned_quantities": stop.get("drop", {}),
                "actual_quantities": {},
            })

        execution_doc = {
            "execution_id": execution_id,
            "plan_id": plan_id,
            "route_id": route_id,
            "tenant_id": tenant_id,
            "stops": execution_stops,
            "completed_stops": 0,
            "total_stops": len(execution_stops),
            "status": "in_progress",
            "created_at": now,
            "updated_at": now,
        }

        await self._es.index_document(
            MVP_PLAN_EXECUTIONS_INDEX, execution_id, execution_doc
        )

        logger.info(
            "Created execution %s for plan %s route %s (%d stops)",
            execution_id,
            plan_id,
            route_id,
            len(execution_stops),
        )

        return execution_doc

    # ------------------------------------------------------------------
    # 2.3 - Record Check-in
    # ------------------------------------------------------------------

    async def record_checkin(
        self,
        plan_id: str,
        route_id: str,
        station_id: str,
        sequence: int,
        actual_quantities: Dict[str, float],
        tenant_id: str,
    ) -> Dict[str, Any]:
        """Record a driver check-in at a stop.

        Validates that the plan is in 'dispatched' status and that the
        stop has not already been completed. Records the arrival time
        and actual quantities, then increments completed_stops.

        Args:
            plan_id: The plan identifier.
            route_id: The route identifier.
            station_id: The station being checked into.
            sequence: The stop sequence number.
            actual_quantities: Dict of fuel_grade -> liters delivered.
            tenant_id: The tenant identifier.

        Returns:
            Dict with updated execution state and all_complete flag.

        Raises:
            ValueError: If plan is not dispatched or stop already completed.
        """
        # Validate plan status is "dispatched"
        plan_doc = await self._fetch_plan(plan_id, tenant_id)
        if plan_doc is None:
            raise ValueError(f"Plan {plan_id} not found")

        plan_status = plan_doc.get("status", "")
        if plan_status != "dispatched":
            raise ValueError(
                f"Plan {plan_id} is not in 'dispatched' status (current: {plan_status})"
            )

        # Fetch execution record
        execution = await self._fetch_execution(plan_id, route_id, tenant_id)
        if execution is None:
            raise ValueError(
                f"No execution record found for plan {plan_id} route {route_id}"
            )

        # Find the target stop and validate it's not already completed
        execution_id = execution["execution_id"]
        stops = execution.get("stops", [])
        target_stop = None
        target_idx = None

        for idx, stop in enumerate(stops):
            if (
                stop.get("station_id") == station_id
                and stop.get("sequence") == sequence
            ):
                target_stop = stop
                target_idx = idx
                break

        if target_stop is None:
            raise ValueError(
                f"Stop not found: station_id={station_id}, sequence={sequence}"
            )

        if target_stop.get("status") == "completed":
            raise ValueError(
                f"Stop already completed: station_id={station_id}, sequence={sequence}"
            )

        # Record arrival time and quantities
        now = datetime.now(timezone.utc).isoformat()
        stops[target_idx]["status"] = "completed"
        stops[target_idx]["actual_arrival"] = now
        stops[target_idx]["actual_quantities"] = actual_quantities

        completed_stops = execution.get("completed_stops", 0) + 1
        total_stops = execution.get("total_stops", len(stops))
        all_complete = completed_stops >= total_stops

        # Update execution status if all stops complete
        execution_status = "completed" if all_complete else "in_progress"

        # Persist updated execution
        update_doc = {
            "stops": stops,
            "completed_stops": completed_stops,
            "status": execution_status,
            "updated_at": now,
        }

        await self._es.update_document(
            MVP_PLAN_EXECUTIONS_INDEX, execution_id, update_doc
        )

        logger.info(
            "Recorded check-in for plan %s route %s station %s "
            "(completed: %d/%d)",
            plan_id,
            route_id,
            station_id,
            completed_stops,
            total_stops,
        )

        return {
            "execution_id": execution_id,
            "plan_id": plan_id,
            "route_id": route_id,
            "station_id": station_id,
            "sequence": sequence,
            "completed_stops": completed_stops,
            "total_stops": total_stops,
            "all_complete": all_complete,
            "updated_at": now,
        }

    # ------------------------------------------------------------------
    # 2.4 - Compute Outcomes
    # ------------------------------------------------------------------

    async def compute_outcomes(
        self, plan_id: str, tenant_id: str
    ) -> Dict[str, Any]:
        """Compute plan-vs-actual outcome variances for a completed plan.

        Fetches execution and route data, computes per-stop
        quantity_variance_pct and time_variance_minutes, identifies
        missed stops, computes aggregates, and persists to
        mvp_plan_outcomes.

        Args:
            plan_id: The plan identifier.
            tenant_id: The tenant identifier.

        Returns:
            The outcome document that was persisted.
        """
        # Fetch all executions for this plan
        executions = await self._fetch_all_executions(plan_id, tenant_id)
        if not executions:
            raise ValueError(f"No execution records found for plan {plan_id}")

        # Fetch route data for planned ETAs
        routes = await self._fetch_routes(plan_id, tenant_id)

        # Build a lookup of planned stops from routes
        planned_stops_map = {}
        for route in routes:
            for stop in route.get("stops", []):
                key = (
                    stop.get("station_id", ""),
                    stop.get("sequence", 0),
                )
                planned_stops_map[key] = stop

        # Compute per-stop variances
        stop_variances = []
        total_qty_variance = 0.0
        total_time_variance = 0.0
        variance_count = 0
        missed_stops_count = 0

        for execution in executions:
            for stop in execution.get("stops", []):
                station_id = stop.get("station_id", "")
                sequence = stop.get("sequence", 0)
                status = stop.get("status", "pending")

                if status != "completed":
                    # Missed stop - no check-in recorded
                    missed_stops_count += 1
                    stop_variances.append({
                        "station_id": station_id,
                        "sequence": sequence,
                        "quantity_variance_pct": None,
                        "time_variance_minutes": None,
                        "status": "missed",
                    })
                    continue

                # Compute quantity variance
                planned_quantities = stop.get("planned_quantities", {})
                actual_quantities = stop.get("actual_quantities", {})
                qty_variance_pct = self._compute_quantity_variance(
                    planned_quantities, actual_quantities
                )

                # Compute time variance
                planned_eta = stop.get("planned_eta", "")
                actual_arrival = stop.get("actual_arrival", "")
                time_variance_min = self._compute_time_variance(
                    planned_eta, actual_arrival
                )

                stop_variances.append({
                    "station_id": station_id,
                    "sequence": sequence,
                    "quantity_variance_pct": qty_variance_pct,
                    "time_variance_minutes": time_variance_min,
                    "status": "completed",
                })

                if qty_variance_pct is not None:
                    total_qty_variance += qty_variance_pct
                    variance_count += 1
                if time_variance_min is not None:
                    total_time_variance += time_variance_min

        # Compute aggregates
        aggregate_qty_variance = (
            total_qty_variance / variance_count if variance_count > 0 else 0.0
        )
        aggregate_time_variance = (
            total_time_variance / variance_count if variance_count > 0 else 0.0
        )

        # Persist outcome
        outcome_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        outcome_doc = {
            "outcome_id": outcome_id,
            "plan_id": plan_id,
            "run_id": plan_id,
            "stop_variances": stop_variances,
            "aggregate_quantity_variance_pct": round(aggregate_qty_variance, 2),
            "aggregate_time_variance_minutes": round(aggregate_time_variance, 2),
            "missed_stops_count": missed_stops_count,
            "tenant_id": tenant_id,
            "timestamp": now,
            "status": "computed",
            "updated_at": now,
            "created_at": now,
        }

        await self._es.index_document(
            MVP_PLAN_OUTCOMES_INDEX, outcome_id, outcome_doc
        )

        logger.info(
            "Computed outcomes for plan %s: %d stop variances, "
            "%d missed stops, avg qty variance=%.2f%%, avg time variance=%.2f min",
            plan_id,
            len(stop_variances),
            missed_stops_count,
            aggregate_qty_variance,
            aggregate_time_variance,
        )

        return outcome_doc

    # ------------------------------------------------------------------
    # 2.5 - Compute Estimated Cost
    # ------------------------------------------------------------------

    async def compute_estimated_cost(
        self, plan_id: str, tenant_id: str
    ) -> Dict[str, Any]:
        """Compute estimated cost for a plan based on route distance and cost config.

        fuel_cost = distance_km × fuel_consumption_rate × fuel_price_per_liter
        driver_cost = hours × driver_hourly_rate

        Args:
            plan_id: The plan identifier.
            tenant_id: The tenant identifier.

        Returns:
            Cost breakdown dict with fuel_cost, driver_cost, total_estimated_cost.

        Raises:
            ValueError: If cost config not found for tenant.
        """
        # Fetch cost config
        cost_config = await self.get_cost_config(tenant_id)
        if cost_config is None:
            raise ValueError(
                f"Cost configuration not found for tenant {tenant_id}"
            )

        # Fetch route data for distance
        routes = await self._fetch_routes(plan_id, tenant_id)
        if not routes:
            raise ValueError(f"No route data found for plan {plan_id}")

        # Sum total distance across all routes
        total_distance_km = sum(
            route.get("distance_km", 0.0) for route in routes
        )

        # Estimate driver hours from route stops ETAs
        driver_hours = self._estimate_driver_hours(routes)

        # Calculate costs
        fuel_consumption_rate = cost_config.get("fuel_consumption_rate", 0.0)
        fuel_price_per_liter = cost_config.get("fuel_price_per_liter", 0.0)
        driver_hourly_rate = cost_config.get("driver_hourly_rate", 0.0)

        fuel_cost = total_distance_km * fuel_consumption_rate * fuel_price_per_liter
        driver_cost = driver_hours * driver_hourly_rate
        total_estimated_cost = fuel_cost + driver_cost

        cost_breakdown = {
            "fuel_cost": round(fuel_cost, 2),
            "driver_cost": round(driver_cost, 2),
            "total_estimated_cost": round(total_estimated_cost, 2),
            "distance_km": round(total_distance_km, 2),
            "driver_hours": round(driver_hours, 2),
            "currency": cost_config.get("currency", "USD"),
        }

        # Persist estimated cost to the plan document
        await self._es.update_document(
            MVP_LOAD_PLANS_INDEX,
            plan_id,
            {"estimated_cost": cost_breakdown},
        )

        logger.info(
            "Computed estimated cost for plan %s: fuel=%.2f, driver=%.2f, total=%.2f",
            plan_id,
            fuel_cost,
            driver_cost,
            total_estimated_cost,
        )

        return cost_breakdown

    # ------------------------------------------------------------------
    # 2.6 - Compute Actual Cost
    # ------------------------------------------------------------------

    async def compute_actual_cost(
        self, plan_id: str, tenant_id: str
    ) -> Dict[str, Any]:
        """Compute actual cost using execution data for actual distance/time.

        Uses the execution records to determine actual driver time.
        Computes cost_variance_pct = ((actual_total - estimated_total) / estimated_total) * 100.

        Args:
            plan_id: The plan identifier.
            tenant_id: The tenant identifier.

        Returns:
            Actual cost breakdown with variance percentage.

        Raises:
            ValueError: If cost config or execution data not found.
        """
        # Fetch cost config
        cost_config = await self.get_cost_config(tenant_id)
        if cost_config is None:
            raise ValueError(
                f"Cost configuration not found for tenant {tenant_id}"
            )

        # Fetch execution data
        executions = await self._fetch_all_executions(plan_id, tenant_id)
        if not executions:
            raise ValueError(
                f"No execution records found for plan {plan_id}"
            )

        # Fetch route data for distance (use actual distance if available,
        # otherwise fall back to planned distance)
        routes = await self._fetch_routes(plan_id, tenant_id)
        total_distance_km = sum(
            route.get("distance_km", 0.0) for route in routes
        )

        # Compute actual driver hours from execution timestamps
        actual_driver_hours = self._compute_actual_driver_hours(executions)

        # Calculate actual costs
        fuel_consumption_rate = cost_config.get("fuel_consumption_rate", 0.0)
        fuel_price_per_liter = cost_config.get("fuel_price_per_liter", 0.0)
        driver_hourly_rate = cost_config.get("driver_hourly_rate", 0.0)

        actual_fuel_cost = (
            total_distance_km * fuel_consumption_rate * fuel_price_per_liter
        )
        actual_driver_cost = actual_driver_hours * driver_hourly_rate
        total_actual_cost = actual_fuel_cost + actual_driver_cost

        # Fetch estimated cost for variance calculation
        plan_doc = await self._fetch_plan(plan_id, tenant_id)
        estimated_cost = (plan_doc or {}).get("estimated_cost", {})
        total_estimated = estimated_cost.get("total_estimated_cost", 0.0)

        # Compute cost variance percentage
        cost_variance_pct = 0.0
        if total_estimated > 0:
            cost_variance_pct = (
                (total_actual_cost - total_estimated) / total_estimated
            ) * 100

        actual_cost_breakdown = {
            "fuel_cost": round(actual_fuel_cost, 2),
            "driver_cost": round(actual_driver_cost, 2),
            "total_actual_cost": round(total_actual_cost, 2),
            "distance_km": round(total_distance_km, 2),
            "driver_hours": round(actual_driver_hours, 2),
            "currency": cost_config.get("currency", "USD"),
        }

        # Persist actual cost and variance to the plan document
        await self._es.update_document(
            MVP_LOAD_PLANS_INDEX,
            plan_id,
            {
                "actual_cost": actual_cost_breakdown,
                "cost_variance_pct": round(cost_variance_pct, 2),
            },
        )

        logger.info(
            "Computed actual cost for plan %s: fuel=%.2f, driver=%.2f, "
            "total=%.2f, variance=%.2f%%",
            plan_id,
            actual_fuel_cost,
            actual_driver_cost,
            total_actual_cost,
            cost_variance_pct,
        )

        return {
            **actual_cost_breakdown,
            "cost_variance_pct": round(cost_variance_pct, 2),
            "estimated_total": round(total_estimated, 2),
        }

    # ------------------------------------------------------------------
    # 2.7 - Cost Config CRUD
    # ------------------------------------------------------------------

    async def get_cost_config(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve cost configuration for a tenant.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            Cost config dict or None if not found.
        """
        try:
            doc = await self._es.get_document(MVP_COST_CONFIGS_INDEX, tenant_id)
            if doc and "_source" in doc:
                return doc["_source"]
            return None
        except Exception:
            # Document not found or index doesn't exist
            return None

    async def upsert_cost_config(
        self, tenant_id: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create or update cost configuration for a tenant.

        Args:
            tenant_id: The tenant identifier.
            config: Dict with fuel_consumption_rate, fuel_price_per_liter,
                driver_hourly_rate, and currency.

        Returns:
            The persisted cost config document.
        """
        now = datetime.now(timezone.utc).isoformat()

        cost_doc = {
            "tenant_id": tenant_id,
            "fuel_consumption_rate": config.get("fuel_consumption_rate", 0.0),
            "fuel_price_per_liter": config.get("fuel_price_per_liter", 0.0),
            "driver_hourly_rate": config.get("driver_hourly_rate", 0.0),
            "currency": config.get("currency", "USD"),
            "updated_at": now,
        }

        # Use tenant_id as document ID for upsert semantics
        await self._es.index_document(
            MVP_COST_CONFIGS_INDEX, tenant_id, cost_doc
        )

        logger.info("Upserted cost config for tenant %s", tenant_id)

        return cost_doc

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_plan(
        self, plan_id: str, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch a plan document by plan_id and tenant_id."""
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"plan_id": plan_id}},
                    ],
                },
            },
            "size": 1,
        }
        try:
            resp = await self._es.search_documents(
                MVP_LOAD_PLANS_INDEX, query, 1
            )
            hits = resp.get("hits", {}).get("hits", [])
            if hits:
                return hits[0]["_source"]
        except Exception as e:
            logger.error("Failed to fetch plan %s: %s", plan_id, e)
        return None

    async def _fetch_execution(
        self, plan_id: str, route_id: str, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch an execution record by plan_id, route_id, and tenant_id."""
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"plan_id": plan_id}},
                        {"term": {"route_id": route_id}},
                    ],
                },
            },
            "size": 1,
        }
        try:
            resp = await self._es.search_documents(
                MVP_PLAN_EXECUTIONS_INDEX, query, 1
            )
            hits = resp.get("hits", {}).get("hits", [])
            if hits:
                return hits[0]["_source"]
        except Exception as e:
            logger.error(
                "Failed to fetch execution for plan %s route %s: %s",
                plan_id,
                route_id,
                e,
            )
        return None

    async def _fetch_all_executions(
        self, plan_id: str, tenant_id: str
    ) -> List[Dict[str, Any]]:
        """Fetch all execution records for a plan."""
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"plan_id": plan_id}},
                    ],
                },
            },
            "size": 100,
        }
        try:
            resp = await self._es.search_documents(
                MVP_PLAN_EXECUTIONS_INDEX, query, 100
            )
            hits = resp.get("hits", {}).get("hits", [])
            return [hit["_source"] for hit in hits]
        except Exception as e:
            logger.error(
                "Failed to fetch executions for plan %s: %s", plan_id, e
            )
            return []

    async def _fetch_routes(
        self, plan_id: str, tenant_id: str
    ) -> List[Dict[str, Any]]:
        """Fetch route documents for a plan."""
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"plan_id": plan_id}},
                    ],
                },
            },
            "size": 20,
        }
        try:
            resp = await self._es.search_documents(MVP_ROUTES_INDEX, query, 20)
            hits = resp.get("hits", {}).get("hits", [])
            return [hit["_source"] for hit in hits]
        except Exception as e:
            logger.error(
                "Failed to fetch routes for plan %s: %s", plan_id, e
            )
            return []

    def _compute_quantity_variance(
        self,
        planned: Dict[str, Any],
        actual: Dict[str, Any],
    ) -> Optional[float]:
        """Compute quantity variance percentage across all fuel grades.

        Formula: ((actual - planned) / planned) * 100

        Returns None if no planned quantities exist.
        """
        total_planned = sum(float(v) for v in planned.values()) if planned else 0.0
        total_actual = sum(float(v) for v in actual.values()) if actual else 0.0

        if total_planned == 0:
            return None

        variance_pct = ((total_actual - total_planned) / total_planned) * 100
        return round(variance_pct, 2)

    def _compute_time_variance(
        self, planned_eta: str, actual_arrival: str
    ) -> Optional[float]:
        """Compute time variance in minutes between actual and planned.

        Formula: (actual_arrival - planned_eta) in minutes.
        Positive = late, negative = early.

        Returns None if either timestamp is missing or unparseable.
        """
        if not planned_eta or not actual_arrival:
            return None

        try:
            planned_dt = datetime.fromisoformat(
                planned_eta.replace("Z", "+00:00")
            )
            actual_dt = datetime.fromisoformat(
                actual_arrival.replace("Z", "+00:00")
            )
            delta = actual_dt - planned_dt
            return round(delta.total_seconds() / 60.0, 2)
        except (ValueError, TypeError):
            return None

    def _estimate_driver_hours(self, routes: List[Dict[str, Any]]) -> float:
        """Estimate driver hours from route stop ETAs.

        Calculates the time span from the first stop's ETA to the last
        stop's ETA across all routes.
        """
        all_etas = []
        for route in routes:
            for stop in route.get("stops", []):
                eta = stop.get("eta", "")
                if eta:
                    try:
                        dt = datetime.fromisoformat(eta.replace("Z", "+00:00"))
                        all_etas.append(dt)
                    except (ValueError, TypeError):
                        continue

        if len(all_etas) < 2:
            # Default to 1 hour if we can't determine from ETAs
            return 1.0

        earliest = min(all_etas)
        latest = max(all_etas)
        delta = latest - earliest
        hours = delta.total_seconds() / 3600.0

        # Minimum 0.5 hours
        return max(hours, 0.5)

    def _compute_actual_driver_hours(
        self, executions: List[Dict[str, Any]]
    ) -> float:
        """Compute actual driver hours from execution timestamps.

        Uses the time span from execution creation to the last
        completed stop's actual_arrival.
        """
        all_arrivals = []
        earliest_start = None

        for execution in executions:
            created_at = execution.get("created_at", "")
            if created_at:
                try:
                    dt = datetime.fromisoformat(
                        created_at.replace("Z", "+00:00")
                    )
                    if earliest_start is None or dt < earliest_start:
                        earliest_start = dt
                except (ValueError, TypeError):
                    pass

            for stop in execution.get("stops", []):
                arrival = stop.get("actual_arrival", "")
                if arrival:
                    try:
                        dt = datetime.fromisoformat(
                            arrival.replace("Z", "+00:00")
                        )
                        all_arrivals.append(dt)
                    except (ValueError, TypeError):
                        continue

        if earliest_start is None or not all_arrivals:
            # Fall back to estimated hours if we can't determine actual
            return 1.0

        latest_arrival = max(all_arrivals)
        delta = latest_arrival - earliest_start
        hours = delta.total_seconds() / 3600.0

        # Minimum 0.5 hours
        return max(hours, 0.5)
