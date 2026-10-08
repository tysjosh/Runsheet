"use client";

/**
 * Margin settings: the weighted-average window, rack staleness, the
 * below-floor margin (tenant-wide and per product) and the timezone used for
 * sale dates. Changes are audit-logged with before/after values.
 */
import { type FormEvent, useEffect, useState } from "react";
import {
  getMarginSettings,
  type MarginSettings,
  updateMarginSettings,
} from "../../../services/marginApi";
import { Button } from "../../ui";
import { apiFieldErrors } from "./CostEntryForm";
import { microsToDecimalString } from "./marginFormat";

const inputClass =
  "mt-1 block w-full rounded border border-gray-300 px-2 py-1 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40";

export const SETTINGS_WARNINGS: Record<string, string> = {
  wac_window_may_exceed_bol_scan_cap:
    "Saved. A window over 30 days may read more BOLs than the 5,000 scan limit; costs then show as missing until you narrow it.",
};

/** `{DIESEL_2: 150000}` -> "DIESEL_2=0.150000" lines. */
function floorsToText(floors: Record<string, number>): string {
  return Object.entries(floors)
    .map(([code, micros]) => `${code}=${microsToDecimalString(micros)}`)
    .join("\n");
}

/** "CODE=0.15" lines -> `{CODE: "0.15"}`; returns null on a malformed line. */
export function parseProductFloors(
  text: string,
): Record<string, string> | null {
  const out: Record<string, string> = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const match = /^([^=\s]+)\s*=\s*(\S+)$/.exec(line);
    if (!match) return null;
    out[match[1]] = match[2];
  }
  return out;
}

export default function MarginSettingsForm() {
  const [settings, setSettings] = useState<MarginSettings | null>(null);
  const [wacDays, setWacDays] = useState("30");
  const [staleDays, setStaleDays] = useState("4");
  const [floor, setFloor] = useState("0.100000");
  const [floors, setFloors] = useState("");
  const [timezone, setTimezone] = useState("America/Chicago");
  const [errors, setErrors] = useState<string[]>([]);
  const [status, setStatus] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await getMarginSettings();
        if (cancelled) return;
        setSettings(s);
        setWacDays(String(s.wac_window_days));
        setStaleDays(String(s.rack_staleness_days));
        setFloor(microsToDecimalString(s.floor_micros));
        setFloors(floorsToText(s.product_floors));
        setTimezone(s.timezone);
      } catch {
        if (!cancelled)
          setErrors(["Margin settings could not be loaded. Try again."]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const productFloors = parseProductFloors(floors);
    if (productFloors === null) {
      setErrors(["Write product floors one per line as PRODUCT=0.150000."]);
      return;
    }
    setSaving(true);
    setErrors([]);
    setStatus("");
    try {
      const result = await updateMarginSettings({
        wac_window_days: Number.parseInt(wacDays, 10),
        rack_staleness_days: Number.parseInt(staleDays, 10),
        floor_usd_per_gallon: floor.trim(),
        product_floors: productFloors,
        timezone: timezone.trim(),
      });
      setSettings(result.settings);
      setStatus(
        result.warnings.map((w) => SETTINGS_WARNINGS[w] ?? w).join(" ") ||
          "Settings saved",
      );
    } catch (e) {
      setErrors(apiFieldErrors(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section aria-labelledby="margin-settings-heading" className="space-y-3">
      <h2 id="margin-settings-heading" className="text-lg font-semibold">
        Margin settings
      </h2>
      <form
        onSubmit={onSubmit}
        className="grid grid-cols-2 gap-3 max-w-2xl"
        aria-label="Margin settings"
      >
        <label className="text-sm">
          Weighted-average window (days)
          <input
            type="number"
            min={1}
            max={365}
            required
            className={inputClass}
            value={wacDays}
            onChange={(e) => setWacDays(e.target.value)}
          />
        </label>
        <label className="text-sm">
          Rack price staleness (days)
          <input
            type="number"
            min={1}
            max={30}
            required
            className={inputClass}
            value={staleDays}
            onChange={(e) => setStaleDays(e.target.value)}
          />
        </label>
        <label className="text-sm">
          Below-floor margin (USD per gallon)
          <input
            required
            inputMode="decimal"
            className={inputClass}
            value={floor}
            onChange={(e) => setFloor(e.target.value)}
          />
        </label>
        <label className="text-sm">
          Timezone for sale dates
          <input
            required
            className={inputClass}
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
          />
        </label>
        <label className="text-sm col-span-2">
          Per-product floors (one per line, PRODUCT=USD per gallon)
          <textarea
            className={inputClass}
            rows={3}
            value={floors}
            onChange={(e) => setFloors(e.target.value)}
          />
        </label>
        <div
          role="alert"
          className={
            errors.length ? "col-span-2 text-sm text-error" : "sr-only"
          }
        >
          {errors.join(" ")}
        </div>
        <p
          role="status"
          className={status ? "col-span-2 text-sm text-gray-800" : "sr-only"}
        >
          {status}
        </p>
        <div className="col-span-2 flex items-center justify-between">
          <span className="text-xs text-gray-500">
            {settings?.updated_at
              ? `Last changed ${settings.updated_at.slice(0, 10)} by ${settings.updated_by}`
              : "Using defaults"}
          </span>
          <Button type="submit" loading={saving}>
            Save settings
          </Button>
        </div>
      </form>
    </section>
  );
}
