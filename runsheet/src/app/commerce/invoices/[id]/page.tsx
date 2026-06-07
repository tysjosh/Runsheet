"use client";

/**
 * Standalone route at ``/commerce/invoices/:id``. Reads the path param and
 * renders the shared {@link InvoiceDetailPage}; the Commerce hub renders the
 * same component in-place with its own back/cross-nav handlers, so invoice
 * detail lives in one place. This route is the canonical owning-module
 * destination that {@link EntityLink} links an ``invoice`` reference to.
 */

import { useRouter } from "next/navigation";
import { use } from "react";
import InvoiceDetailPage from "../../../../components/commerce/InvoiceDetailPage";

export default function InvoicePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  return (
    <InvoiceDetailPage
      invoiceId={id}
      onBack={() => router.back()}
      onViewAccount={(accountId) =>
        router.push(`/commerce/accounts/${encodeURIComponent(accountId)}`)
      }
    />
  );
}
