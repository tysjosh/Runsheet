"use client";

/**
 * One tank (R7.4, R7.5, R14.8, D29): the level card first, then the facts
 * and the delivery history. The heading is the tank's title, never
 * "Tank Tank …".
 */
import { Plus, Truck } from "lucide-react";
import { useParams } from "next/navigation";
import { useCallback, useRef } from "react";
import LevelBar from "../../../../components/portal/LevelBar";
import {
  PortalEmpty,
  PortalLoading,
  PortalSectionError,
} from "../../../../components/portal/PageState";
import { usePortalMe } from "../../../../components/portal/PortalContext";
import PortalTable, {
  type PortalColumn,
} from "../../../../components/portal/PortalTable";
import PortalTitleRow from "../../../../components/portal/PortalTitleRow";
import {
  dateTime,
  deliveredVolume,
  number,
  productName,
} from "../../../../components/portal/portalFormat";
import RequestDeliveryDialog from "../../../../components/portal/RequestDeliveryDialog";
import {
  card,
  primaryButton,
  secondaryButton,
  sectionHeading,
} from "../../../../components/portal/styles";
import { NextDelivery } from "../../../../components/portal/TankRow";
import {
  levelLine,
  STALE_READING_TEXT,
  tankLevel,
} from "../../../../components/portal/tankLevel";
import { tankTitle } from "../../../../components/portal/tankTitle";
import {
  PORTAL_TABLES,
  useMediaQuery,
} from "../../../../components/portal/useMediaQuery";
import { usePagedList } from "../../../../components/portal/usePagedList";
import {
  portalErrorMessage,
  usePortalData,
} from "../../../../components/portal/usePortalData";
import {
  usePortalTanks,
  useRequestDialog,
} from "../../../../components/portal/usePortalTanks";
import { ProductCap, ProductChip } from "../../../../components/ui/ProductChip";
import { StatusBadge } from "../../../../components/ui/StatusBadge";
import {
  getPortalTank,
  listPortalTankDeliveries,
  type PortalTankDelivery,
} from "../../../../services/portalApi";

const BACK = { href: "/portal/tanks", label: "Back to your tanks" };

export default function PortalTankDetailPage() {
  const params = useParams<{ tankId: string }>();
  const tankId = params?.tankId ?? "";
  const me = usePortalMe();
  const unit = me.measurement_units.volume;
  const wide = useMediaQuery(PORTAL_TABLES);
  const tank = usePortalData(() => getPortalTank(tankId), [tankId]);
  const tanks = usePortalTanks();
  const dialog = useRequestDialog();
  const fetchPage = useCallback(
    (cursor: string | null) =>
      listPortalTankDeliveries(tankId, { cursor, limit: 25 }),
    [tankId],
  );
  const history = usePagedList(fetchPage, [fetchPage]);
  const sent = useRef(false);

  if (tank.loading) {
    return (
      <>
        <PortalTitleRow title="Tank" back={BACK} />
        <div data-portal-first className={`${card} px-4`}>
          <PortalLoading label="Loading the tank…" rows={3} />
        </div>
      </>
    );
  }
  if (tank.error || !tank.data) {
    return (
      <>
        <PortalTitleRow title="Tank" back={BACK} />
        <div data-portal-first>
          <PortalSectionError
            message={portalErrorMessage(tank.error, {
              notFound: "We couldn't find that tank.",
              fallback: "We couldn't load the tank.",
            })}
            onRetry={tank.reload}
          />
        </div>
      </>
    );
  }

  const t = tank.data.data;
  const title = tanks.titles.get(t.customer_tank_id) ?? tankTitle(t);
  const level = tankLevel(t);

  const columns: PortalColumn<PortalTankDelivery>[] = [
    {
      key: "delivered",
      header: "Delivered",
      cell: (d) => dateTime(d.delivered_at),
    },
    {
      key: "product",
      header: "Product",
      cell: (d) =>
        d.product_code ? <ProductChip code={d.product_code} size="md" /> : "—",
    },
    {
      key: "volume",
      header: "Volume",
      align: "right",
      cell: (d) => deliveredVolume(d.delivered_gallons, unit),
    },
    { key: "ticket", header: "Ticket", cell: (d) => d.ticket_number ?? "—" },
  ];

  return (
    <>
      <PortalTitleRow
        title={title}
        back={BACK}
        badge={
          <StatusBadge status={level.status} label={level.label} size="md" />
        }
        action={
          <button
            type="button"
            className={primaryButton}
            onClick={() => dialog.openFor(t.customer_tank_id)}
            aria-label={`Request delivery for ${title}`}
          >
            <Plus aria-hidden="true" className="h-4 w-4" />
            <span className="max-sm:sr-only">Request delivery</span>
          </button>
        }
      />
      <div className="space-y-4">
        <section
          aria-labelledby="level-heading"
          data-portal-first
          className={`${card} space-y-2 p-4`}
        >
          <div className="flex items-center gap-2">
            <ProductCap code={t.product_code} size="md" decorative />
            <h2 id="level-heading" className={sectionHeading}>
              Level
            </h2>
            <span className="text-sm text-text-muted">
              · {productName(t.product_code)}
            </span>
          </div>
          <p className="text-[15px] text-text">{levelLine(t, unit)}</p>
          <LevelBar
            tank={t}
            status={level.status}
            labelledBy="level-heading"
            unit={unit}
          />
          <dl className="grid grid-cols-1 gap-x-6 gap-y-2 pt-1 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-text-muted">Last reading</dt>
              <dd className="font-medium text-text">
                {dateTime(t.last_reading_at)}
                {t.reading_stale ? ` · ${STALE_READING_TEXT}` : ""}
              </dd>
            </div>
            <div>
              <dt className="text-text-muted">Capacity</dt>
              <dd className="font-medium text-text">
                {number(t.capacity_gallons)} {unit}
              </dd>
            </div>
            {t.service_address && (
              <div className="sm:col-span-2">
                <dt className="text-text-muted">Service address</dt>
                <dd className="font-medium text-text">{t.service_address}</dd>
              </div>
            )}
            <div className="sm:col-span-2">
              <dt className="text-text-muted">Next delivery</dt>
              <dd className="mt-0.5">
                <NextDelivery tank={t} />
              </dd>
            </div>
          </dl>
        </section>

        <section aria-labelledby="history-heading" className={card}>
          <h2 id="history-heading" className={`${sectionHeading} px-4 pt-3`}>
            Delivery history
          </h2>
          {history.loading ? (
            <div className="px-4">
              <PortalLoading label="Loading deliveries…" />
            </div>
          ) : history.error && history.items.length === 0 ? (
            <div className="px-4 pb-2">
              <PortalSectionError
                message={portalErrorMessage(history.error, {
                  fallback: "We couldn't load the delivery history.",
                })}
                onRetry={history.reload}
              />
            </div>
          ) : history.items.length === 0 ? (
            <PortalEmpty
              icon={<Truck className="h-8 w-8" />}
              title="No deliveries in the last 24 months."
            />
          ) : wide ? (
            <div className="mt-2">
              <PortalTable
                caption="Delivery history"
                columns={columns}
                rows={history.items}
                rowKey={(d) => d.order_id}
              />
            </div>
          ) : (
            <ul aria-label="Delivery history" className="mt-1">
              {history.items.map((d) => (
                <li
                  key={d.order_id}
                  className="grid grid-cols-[22px_minmax(0,1fr)_auto] items-center gap-x-2.5 gap-y-0.5 border-t border-slate-100 px-4 py-2.5"
                >
                  <span className="row-span-2 self-start pt-0.5">
                    {d.product_code && <ProductCap code={d.product_code} />}
                  </span>
                  <span className="truncate text-[15px] font-semibold text-text">
                    {deliveredVolume(d.delivered_gallons, unit)} delivered
                  </span>
                  <span className="text-sm text-text-muted">
                    {d.ticket_number ? `ticket ${d.ticket_number}` : ""}
                  </span>
                  <span className="col-span-2 col-start-2 truncate text-sm text-text-muted">
                    {dateTime(d.delivered_at)}
                    {d.product_code ? ` · ${productName(d.product_code)}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {Boolean(history.error) && history.items.length > 0 && (
            <div className="px-4 pb-2">
              <PortalSectionError
                message={portalErrorMessage(history.error, {
                  fallback: "We couldn't load more deliveries.",
                })}
              />
            </div>
          )}
          {history.hasMore && !history.loading && (
            <div className="flex justify-center border-t border-slate-100 p-3">
              <button
                type="button"
                className={secondaryButton}
                onClick={history.loadMore}
                aria-disabled={history.loadingMore ? true : undefined}
              >
                {history.loadingMore ? "Loading…" : "Show more deliveries"}
              </button>
            </div>
          )}
        </section>
      </div>
      <RequestDeliveryDialog
        open={dialog.open}
        onClose={() => {
          dialog.close();
          // A sent request changes the next delivery; reload after closing so
          // the confirmation inside the dialog stays mounted.
          if (sent.current) {
            sent.current = false;
            tank.reload();
          }
        }}
        tanks={tanks.tanks.length ? tanks.tanks : [t]}
        titles={
          tanks.titles.size
            ? tanks.titles
            : new Map([[t.customer_tank_id, title]])
        }
        orderingAvailable={me.ordering_available}
        supplierName={me.supplier_name}
        unit={unit}
        initialTankId={dialog.tankId}
        onCreated={() => {
          sent.current = true;
          history.refresh().catch(() => undefined);
        }}
      />
    </>
  );
}
