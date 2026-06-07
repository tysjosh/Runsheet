"use client";

/**
 * Standalone route at ``/commerce/accounts/:id``. Reads the path param and
 * renders the shared {@link AccountDetailPage}; the Commerce hub renders the
 * same component in-place with its own handlers. This route is the canonical
 * owning-module destination that {@link EntityLink} links an ``account``
 * reference to.
 */

import { useRouter } from "next/navigation";
import { use } from "react";
import AccountDetailPage from "../../../../components/commerce/AccountDetailPage";

export default function AccountPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  return (
    <AccountDetailPage
      accountId={id}
      onBack={() => router.back()}
      onViewCustomer={(customerId) =>
        router.push(`/commerce/customers/${encodeURIComponent(customerId)}`)
      }
    />
  );
}
