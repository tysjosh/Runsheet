"use client";

/** Request a delivery for one of the customer's tanks (R4, PD9, PD10). */

import Link from "next/link";
import OrderRequestForm from "../../../../components/portal/OrderRequestForm";
import {
  PortalLoadError,
  PortalLoading,
} from "../../../../components/portal/PageState";
import { usePortalMe } from "../../../../components/portal/PortalContext";
import { pageHeading, textLink } from "../../../../components/portal/styles";
import {
  portalErrorMessage,
  usePortalData,
} from "../../../../components/portal/usePortalData";
import { listPortalTanks } from "../../../../services/portalApi";

export default function PortalNewOrderPage() {
  const me = usePortalMe();
  const tanks = usePortalData(() => listPortalTanks(), []);

  return (
    <div className="space-y-6">
      <div>
        <p>
          <Link href="/portal/orders" className={textLink}>
            Back to your orders
          </Link>
        </p>
        <h1 className={`${pageHeading} mt-2`}>Request a delivery</h1>
      </div>
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
        <p className="text-sm text-gray-800">
          No tanks are set up for online ordering yet. Please contact your
          supplier.
        </p>
      ) : (
        <OrderRequestForm
          tanks={tanks.data?.data ?? []}
          orderingAvailable={me.ordering_available}
          volumeUnit={me.measurement_units.volume}
        />
      )}
    </div>
  );
}
