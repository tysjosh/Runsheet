/**
 * Delivery history for one tank (R7.4). A table from 640 px up, stacked
 * definition lists below, like InvoiceTable.
 */

import type { PortalTankDelivery } from "../../services/portalApi";
import { formatDateTime, formatVolume } from "./format";

export default function DeliveryHistoryTable({
  deliveries,
  volumeUnit = "gal",
}: {
  deliveries: PortalTankDelivery[];
  volumeUnit?: string;
}) {
  if (deliveries.length === 0) {
    return (
      <p className="text-sm text-gray-700">
        No deliveries in the last 24 months.
      </p>
    );
  }
  return (
    <>
      <table className="hidden w-full border-collapse text-left text-sm sm:table">
        <caption className="sr-only">Delivery history</caption>
        <thead>
          <tr className="border-b border-gray-200 text-gray-700">
            <th scope="col" className="py-2 pr-3 font-medium">
              Delivered
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Product
            </th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">
              Volume
            </th>
            <th scope="col" className="py-2 font-medium">
              Ticket
            </th>
          </tr>
        </thead>
        <tbody>
          {deliveries.map((d) => (
            <tr key={d.order_id} className="border-b border-gray-100">
              <td className="py-2 pr-3">{formatDateTime(d.delivered_at)}</td>
              <td className="py-2 pr-3">{d.product_code ?? "—"}</td>
              <td className="py-2 pr-3 text-right">
                {formatVolume(d.delivered_gallons, volumeUnit)}
              </td>
              <td className="py-2">{d.ticket_number ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <ul className="space-y-3 sm:hidden" aria-label="Delivery history">
        {deliveries.map((d) => (
          <li
            key={d.order_id}
            className="rounded-xl border border-gray-200 bg-white p-4"
          >
            <dl className="grid grid-cols-1 gap-1 text-sm">
              <div>
                <dt className="inline font-medium text-gray-700">
                  Delivered:{" "}
                </dt>
                <dd className="inline">{formatDateTime(d.delivered_at)}</dd>
              </div>
              <div>
                <dt className="inline font-medium text-gray-700">Product: </dt>
                <dd className="inline">{d.product_code ?? "—"}</dd>
              </div>
              <div>
                <dt className="inline font-medium text-gray-700">Volume: </dt>
                <dd className="inline">
                  {formatVolume(d.delivered_gallons, volumeUnit)}
                </dd>
              </div>
              <div>
                <dt className="inline font-medium text-gray-700">Ticket: </dt>
                <dd className="inline">{d.ticket_number ?? "—"}</dd>
              </div>
            </dl>
          </li>
        ))}
      </ul>
    </>
  );
}
