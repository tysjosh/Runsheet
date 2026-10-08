"use client";

/** The customer's active tanks with forecast and next delivery (R7.1–R7.3). */

import {
  PortalLoadError,
  PortalLoading,
} from "../../../components/portal/PageState";
import { usePortalMe } from "../../../components/portal/PortalContext";
import { pageHeading } from "../../../components/portal/styles";
import TankCard from "../../../components/portal/TankCard";
import {
  portalErrorMessage,
  usePortalData,
} from "../../../components/portal/usePortalData";
import { listPortalTanks } from "../../../services/portalApi";

export default function PortalTanksPage() {
  const me = usePortalMe();
  const tanks = usePortalData(() => listPortalTanks(), []);

  return (
    <div className="space-y-6">
      <h1 className={pageHeading}>Your tanks</h1>
      {tanks.loading ? (
        <PortalLoading label="Loading your tanks…" />
      ) : tanks.error ? (
        <PortalLoadError
          message={portalErrorMessage(tanks.error, {
            fallback: "We couldn't load your tanks.",
          })}
          onRetry={tanks.reload}
        />
      ) : (tanks.data?.data.length ?? 0) === 0 ? (
        <p className="text-sm text-gray-700">No tanks are set up yet.</p>
      ) : (
        <ul className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {tanks.data?.data.map((tank) => (
            <li key={tank.customer_tank_id}>
              <TankCard tank={tank} volumeUnit={me.measurement_units.volume} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
