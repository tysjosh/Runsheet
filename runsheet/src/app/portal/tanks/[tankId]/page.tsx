"use client";

/** One tank with its delivery history (R7.4, R7.5). */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback } from "react";
import DeliveryHistoryTable from "../../../../components/portal/DeliveryHistoryTable";
import {
  PortalLoadError,
  PortalLoading,
} from "../../../../components/portal/PageState";
import { usePortalMe } from "../../../../components/portal/PortalContext";
import {
  pageHeading,
  secondaryButton,
  sectionHeading,
  textLink,
} from "../../../../components/portal/styles";
import TankCard from "../../../../components/portal/TankCard";
import { usePagedList } from "../../../../components/portal/usePagedList";
import {
  portalErrorMessage,
  usePortalData,
} from "../../../../components/portal/usePortalData";
import {
  getPortalTank,
  listPortalTankDeliveries,
} from "../../../../services/portalApi";

export default function PortalTankDetailPage() {
  const params = useParams<{ tankId: string }>();
  const tankId = params?.tankId ?? "";
  const me = usePortalMe();
  const unit = me.measurement_units.volume;
  const tank = usePortalData(() => getPortalTank(tankId), [tankId]);
  const fetchPage = useCallback(
    (cursor: string | null) =>
      listPortalTankDeliveries(tankId, { cursor, limit: 25 }),
    [tankId],
  );
  const history = usePagedList(fetchPage, [fetchPage]);

  const back = (
    <p>
      <Link href="/portal/tanks" className={textLink}>
        Back to your tanks
      </Link>
    </p>
  );

  if (tank.loading) return <PortalLoading label="Loading the tank…" />;
  if (tank.error || !tank.data) {
    return (
      <div className="space-y-3">
        {back}
        <h1 className={pageHeading}>Tank</h1>
        <PortalLoadError
          message={portalErrorMessage(tank.error, {
            notFound: "We couldn't find that tank.",
            fallback: "We couldn't load the tank.",
          })}
          onRetry={tank.reload}
        />
      </div>
    );
  }

  const t = tank.data.data;
  return (
    <div className="space-y-6">
      {back}
      <h1 className={pageHeading}>Tank {t.label}</h1>
      <TankCard tank={t} volumeUnit={unit} linkToDetail={false} />

      <section aria-labelledby="history-heading" className="space-y-3">
        <h2 id="history-heading" className={sectionHeading}>
          Delivery history
        </h2>
        {history.loading ? (
          <PortalLoading label="Loading deliveries…" />
        ) : history.error && history.items.length === 0 ? (
          <PortalLoadError
            message={portalErrorMessage(history.error, {
              fallback: "We couldn't load the delivery history.",
            })}
            onRetry={history.reload}
          />
        ) : (
          <DeliveryHistoryTable deliveries={history.items} volumeUnit={unit} />
        )}
        {Boolean(history.error) && history.items.length > 0 && (
          <p role="alert" className="text-sm text-gray-900">
            {portalErrorMessage(history.error, {
              fallback: "We couldn't load more deliveries.",
            })}
          </p>
        )}
        {history.hasMore && !history.loading && (
          <button
            type="button"
            className={secondaryButton}
            onClick={history.loadMore}
            aria-disabled={history.loadingMore ? true : undefined}
          >
            {history.loadingMore ? "Loading…" : "Show more deliveries"}
          </button>
        )}
      </section>
    </div>
  );
}
