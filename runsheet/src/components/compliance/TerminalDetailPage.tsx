"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Badge, Button } from "@/components/ui";
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
  const [error, setError] = useState<string | null>(null);

  const fetchTerminal = useCallback(async () => {
    setLoading(true);
    setError(null);
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
      setError(
        err instanceof Error ? err.message : "Failed to load terminal details",
      );
    } finally {
      setLoading(false);
    }
  }, [terminalId]);

  useEffect(() => {
    fetchTerminal();
  }, [fetchTerminal]);

  const statusVariant = (status: string): "success" | "warning" | "default" => {
    if (status === "active") return "success";
    if (status === "inactive") return "warning";
    return "default";
  };

  if (loading) {
    return (
      <div role="status" className="flex justify-center py-12">
        <span className="sr-only">Loading terminal details...</span>
        <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div role="alert" className="p-6">
        <div className="bg-error-light border border-error-light text-error-dark p-4 rounded">
          {error}
        </div>
      </div>
    );
  }

  if (!terminal) return null;

  return (
    <div className="p-6">
      <header className="mb-6">
        <div className="flex items-center gap-4 mb-2">
          <Button
            variant="ghost"
            onClick={() => (onBack ? onBack() : router.back())}
          >
            ← Back
          </Button>
        </div>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold">{terminal.name}</h1>
            {terminal.branded && <Badge variant="info">Branded</Badge>}
          </div>
          <Badge variant={statusVariant(terminal.status)}>
            {terminal.status}
          </Badge>
        </div>
      </header>

      <section aria-labelledby="info-heading" className="mb-8">
        <h2 id="info-heading" className="text-lg font-semibold mb-3">
          Terminal Information
        </h2>
        <div className="border rounded p-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div>
              <p className="text-sm text-gray-600 mb-1">Operator</p>
              <p className="font-medium">{terminal.operator}</p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Supplier Brand</p>
              <p className="font-medium">{terminal.supplier_brand || "—"}</p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Address</p>
              <p className="font-medium">{terminal.address || "—"}</p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Timezone</p>
              <p className="font-medium">{terminal.timezone || "—"}</p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Location</p>
              <p className="font-mono text-sm">
                {terminal.location_lat}, {terminal.location_lon}
              </p>
            </div>
            <div>
              <p className="text-sm text-gray-600 mb-1">Supported Products</p>
              <div className="flex flex-wrap gap-1.5">
                {terminal.supported_products?.length ? (
                  terminal.supported_products.map((p) => (
                    <Badge key={p} variant="default" size="sm">
                      {p}
                    </Badge>
                  ))
                ) : (
                  <span className="text-gray-500">—</span>
                )}
              </div>
            </div>
            {wait && (
              <div>
                <p className="text-sm text-gray-600 mb-1">
                  Avg Wait (rolling 2h)
                </p>
                <p className="font-medium">
                  {wait.avg_wait_minutes != null
                    ? `${Math.round(wait.avg_wait_minutes)} min`
                    : "—"}
                  {wait.wait_warning_exceeded && (
                    <Badge variant="warning" size="sm" className="ml-2">
                      Wait warning
                    </Badge>
                  )}
                </p>
              </div>
            )}
            <div>
              <p className="text-sm text-gray-600 mb-1">Terminal ID</p>
              <p className="font-mono text-sm">{terminal.terminal_id}</p>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
