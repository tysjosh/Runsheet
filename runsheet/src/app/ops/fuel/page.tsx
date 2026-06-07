"use client";

/**
 * Standalone route at ``/ops/fuel``. Renders the shared {@link FuelDashboardView}
 * with its own header + tabs; the Fuel Ops hub renders the same view embedded
 * (header/tabs suppressed) so monitoring lives in one place.
 */

import FuelDashboardView from "../../../components/ops/FuelDashboardView";

export default function FuelDashboardPage() {
  return <FuelDashboardView />;
}
