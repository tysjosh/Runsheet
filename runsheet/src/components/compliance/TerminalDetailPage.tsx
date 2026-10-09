"use client";

import { useRouter } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import {
  LoadErrorState,
  PageHeader,
  ProductChip,
  Skeleton,
  StatusBadge,
} from "@/components/ui";
import { duration, number } from "../../lib/format";
import { classifyLoadError, type LoadFailure } from "../../services/apiErrors";
import {
  getTerminal,
  getTerminalWaitSummary,
  type Terminal,
  type TerminalWaitSummary,
} from "../../services/fuelApi";

interface TerminalDetailPageProps {
  terminalId: string;
  /** Optional in-shell back handler; falls back to browser history. */
  onBack?: () => void;
}

export default function TerminalDetailPage({
  terminalId,
  onBack,
}: TerminalDetailPageProps) {
  const router = useRouter();
  const [terminal, setTerminal] = useState<Terminal | null>(null);
  const [wait, setWait] = useState<TerminalWaitSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailure, setLoadFailure] = useState<LoadFailure | null>(null);

  const fetchTerminal = useCallback(async () => {
    setLoading(true);
    setLoadFailure(null);
    try {
      const data = await getTerminal(terminalId);
      setTerminal(data);
      // Wait summary is supplementary — never fail the page if it 404s/errors.
      try {
        setWait(await getTerminalWaitSummary(terminalId));
      } catch {
        setWait(null);
      }
    } catch (err) {
      setLoadFailure(classifyLoadError(err, "Failed to load terminal details"));
    } finally {
      setLoading(false);
    }
  }, [terminalId]);

  useEffect(() => {
    fetchTerminal();
  }, [fetchTerminal]);

  if (loading) {
    return (
      <div className="p-4">
        <Skeleton rows={6} label="Loading terminal details" />
      </div>
    );
  }

  if (loadFailure) {
    return (
      <LoadErrorState
        failure={loadFailure}
        entityLabel="Terminal"
        entityId={terminalId}
        onBack={() => (onBack ? onBack() : router.back())}
        homeHref="/dashboard/compliance"
        homeLabel="Go to Compliance"
        onRetry={fetchTerminal}
      />
    );
  }

  if (!terminal) return null;

  const back = () => (onBack ? onBack() : router.back());
  const statusKey =
    terminal.status === "active"
      ? "ok"
      : terminal.status === "inactive"
        ? "warning"
        : "draft";

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        host
        title={terminal.name}
        back={{ label: "Back", onClick: back }}
        badge={
          <span className="flex items-center gap-1.5">
            <StatusBadge
              status={statusKey}
              label={
                terminal.status.charAt(0).toUpperCase() +
                terminal.status.slice(1)
              }
            />
            {terminal.branded && (
              <span className="rounded-full border border-blue-300 bg-blue-100 px-2 text-xs font-semibold text-blue-800">
                Branded
              </span>
            )}
          </span>
        }
      />
      <div className="flex-1 overflow-auto p-4">
        <section aria-labelledby="info-heading">
          <h2
            id="info-heading"
            className="mb-2 text-sm font-semibold text-text"
          >
            Terminal information
          </h2>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-3 rounded-lg border border-slate-200 p-4 text-sm md:grid-cols-2">
            <Row label="Operator">{terminal.operator}</Row>
            <Row label="Supplier brand">{terminal.supplier_brand || "—"}</Row>
            <Row label="Address">{terminal.address || "—"}</Row>
            <Row label="Timezone">{terminal.timezone || "—"}</Row>
            <Row label="Location">
              <span className="font-mono text-xs">
                {number(terminal.location_lat, { decimals: 5 })},{" "}
                {number(terminal.location_lon, { decimals: 5 })}
              </span>
            </Row>
            <Row label="Supported products">
              <span className="flex flex-wrap gap-1.5">
                {terminal.supported_products?.length
                  ? terminal.supported_products.map((p) => (
                      <ProductChip key={p} code={p} />
                    ))
                  : "—"}
              </span>
            </Row>
            {wait && (
              <Row label="Average wait (last 2 h)">
                <span className="inline-flex items-center gap-2">
                  {wait.avg_wait_minutes != null
                    ? duration(wait.avg_wait_minutes * 60)
                    : "—"}
                  {wait.wait_warning_exceeded && (
                    <StatusBadge status="warning" label="Wait warning" />
                  )}
                </span>
              </Row>
            )}
            <Row label="Terminal ID">
              <span className="font-mono text-xs">{terminal.terminal_id}</span>
            </Row>
          </dl>
        </section>
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="font-medium text-text">{children}</dd>
    </div>
  );
}
