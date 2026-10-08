"use client";

/**
 * The customer's tanks ranked by level status (R7.1–R7.3, R14.8), 2-up from
 * 640 px, each with its own Request delivery (D24).
 */
import { Fuel } from "lucide-react";
import { useMemo } from "react";
import {
  PortalEmpty,
  PortalLoading,
  PortalSectionError,
} from "../../../components/portal/PageState";
import { usePortalMe } from "../../../components/portal/PortalContext";
import PortalTitleRow from "../../../components/portal/PortalTitleRow";
import RequestDeliveryDialog from "../../../components/portal/RequestDeliveryDialog";
import { listSection, space } from "../../../components/portal/styles";
import TankRow from "../../../components/portal/TankRow";
import { sortTanks } from "../../../components/portal/tankLevel";
import {
  usePortalTanks,
  useRequestDialog,
} from "../../../components/portal/usePortalTanks";

export default function PortalTanksPage() {
  const me = usePortalMe();
  const unit = me.measurement_units.volume;
  const tanks = usePortalTanks();
  const dialog = useRequestDialog();
  const ranked = useMemo(
    () =>
      sortTanks(
        tanks.tanks,
        (t) => tanks.titles.get(t.customer_tank_id) ?? t.label,
      ),
    [tanks.tanks, tanks.titles],
  );

  return (
    <>
      <PortalTitleRow title="Tanks" />
      {tanks.loading ? (
        <div data-portal-first className={`${listSection} ${space.inset}`}>
          <PortalLoading label="Loading your tanks…" />
        </div>
      ) : tanks.error ? (
        <div data-portal-first>
          <PortalSectionError message={tanks.error} onRetry={tanks.reload} />
        </div>
      ) : ranked.length === 0 ? (
        <div data-portal-first className={listSection}>
          <PortalEmpty
            icon={<Fuel className="h-8 w-8" />}
            title="No tanks are set up yet."
            description={`Contact ${me.supplier_name} to add one.`}
          />
        </div>
      ) : (
        <ul
          data-portal-first
          className="grid grid-cols-1 divide-y divide-slate-100 sm:grid-cols-2 sm:gap-3 sm:divide-y-0"
        >
          {ranked.map((tank) => (
            <li
              key={tank.customer_tank_id}
              className="sm:rounded-xl sm:border sm:border-border sm:bg-surface sm:px-3.5 md:px-0"
            >
              <TankRow
                tank={tank}
                title={tanks.titles.get(tank.customer_tank_id) ?? tank.label}
                unit={unit}
                headingLevel={2}
                onRequest={(id) => dialog.openFor(id)}
              />
            </li>
          ))}
        </ul>
      )}
      <RequestDeliveryDialog
        open={dialog.open}
        onClose={dialog.close}
        tanks={tanks.tanks}
        titles={tanks.titles}
        tanksLoading={tanks.loading}
        tanksError={tanks.error}
        onRetryTanks={tanks.reload}
        orderingAvailable={me.ordering_available}
        supplierName={me.supplier_name}
        unit={unit}
        initialTankId={dialog.tankId}
        onCreated={tanks.reload}
      />
    </>
  );
}
