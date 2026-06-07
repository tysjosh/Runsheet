"use client";

/**
 * Standalone route at ``/compliance/terminals/:id``. Reads the path param and
 * renders the shared {@link TerminalDetailPage}. This is the canonical
 * owning-module destination that {@link EntityLink} links a ``terminal``
 * reference to.
 */

import { useRouter } from "next/navigation";
import { use } from "react";
import TerminalDetailPage from "../../../../components/compliance/TerminalDetailPage";

export default function TerminalPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  return <TerminalDetailPage terminalId={id} onBack={() => router.back()} />;
}
