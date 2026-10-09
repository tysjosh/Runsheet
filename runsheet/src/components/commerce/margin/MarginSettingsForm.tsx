"use client";

/**
 * Margin settings: the weighted-average window, rack staleness, the
 * below-floor margin (tenant-wide and per product) and the timezone used for
 * sale dates. Changes are audit-logged with before/after values.
 *
 * UI revamp task 3.4: a read-only summary with "Edit settings" opening an md
 * FormDialog (design.md §5). Per-product floors are rows with a product
 * picker (names, not codes). Money stays a decimal string (6 dp), so nothing
 * is rounded in the browser.
 */
import { Plus, Trash2 } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { dateTime, number, productName } from "../../../lib/format";
import {
  getMarginSettings,
  type MarginSettings,
  updateMarginSettings,
} from "../../../services/marginApi";
import { PRODUCT_CODES } from "../../../styles/tokens";
import {
  Button,
  Field,
  FormDialog,
  IconButton,
  INPUT_CLASS,
  NumberField,
  ProductSelect,
} from "../../ui";
import { notify } from "../../ui/toast/notify";
import { apiFieldErrors } from "./CostEntryForm";
import { microsToDecimalString } from "./marginFormat";

export const SETTINGS_WARNINGS: Record<string, string> = {
  wac_window_may_exceed_bol_scan_cap:
    "Saved. A window over 30 days may read more BOLs than the 5,000 scan limit; costs then show as missing until you narrow it.",
};

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

type FloorRow = { id: number; product: string; usd: string };

type Values = {
  wac_window_days: number | null;
  rack_staleness_days: number | null;
  floor: string;
  timezone: string;
  floors: FloorRow[];
};

const DECIMAL = /^\d+(\.\d{1,6})?$/;

export function validateMarginSettings(v: Values) {
  const errors: Record<string, string | undefined> = {};
  if (
    v.wac_window_days == null ||
    v.wac_window_days < 1 ||
    v.wac_window_days > 365
  )
    errors.wac_window_days = "Enter 1 to 365 days.";
  if (
    v.rack_staleness_days == null ||
    v.rack_staleness_days < 1 ||
    v.rack_staleness_days > 30
  )
    errors.rack_staleness_days = "Enter 1 to 30 days.";
  if (!DECIMAL.test(v.floor.trim()))
    errors.floor = "Use dollars with up to 6 decimals, e.g. 0.100000.";
  if (!v.timezone.trim()) errors.timezone = "Enter a time zone.";
  const seen = new Set<string>();
  for (const row of v.floors) {
    if (!row.product || !DECIMAL.test(row.usd.trim())) {
      errors.floors =
        "Each product floor needs a product and dollars with up to 6 decimals.";
      break;
    }
    if (seen.has(row.product)) {
      errors.floors = `${productName(row.product)} is listed twice.`;
      break;
    }
    seen.add(row.product);
  }
  return errors;
}

function valuesFrom(s: MarginSettings): Values {
  return {
    wac_window_days: s.wac_window_days,
    rack_staleness_days: s.rack_staleness_days,
    floor: microsToDecimalString(s.floor_micros),
    timezone: s.timezone,
    floors: Object.entries(s.product_floors).map(([product, micros], i) => ({
      id: i,
      product,
      usd: microsToDecimalString(micros),
    })),
  };
}

export default function MarginSettingsForm() {
  const [settings, setSettings] = useState<MarginSettings | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await getMarginSettings();
        if (!cancelled) setSettings(s);
      } catch {
        if (!cancelled)
          setLoadError("Margin settings could not be loaded. Try again.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const submit = async (v: Values) => {
    const product_floors: Record<string, string> = {};
    for (const row of v.floors) product_floors[row.product] = row.usd.trim();
    try {
      return await updateMarginSettings({
        wac_window_days: v.wac_window_days ?? 30,
        rack_staleness_days: v.rack_staleness_days ?? 4,
        floor_usd_per_gallon: v.floor.trim(),
        product_floors,
        timezone: v.timezone.trim(),
      });
    } catch (e) {
      throw new Error(apiFieldErrors(e).join(" "));
    }
  };

  return (
    <section aria-labelledby="margin-settings-heading" className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h2 id="margin-settings-heading" className="text-sm font-semibold">
          Margin settings
        </h2>
        <Button
          size="sm"
          variant="secondary"
          disabled={!settings}
          onClick={() => setEditing(true)}
        >
          Edit settings
        </Button>
      </div>
      {loadError && (
        <p role="alert" className="text-sm text-red-800">
          {loadError}
        </p>
      )}
      {settings && (
        <dl className="grid max-w-3xl grid-cols-2 gap-3 rounded-lg border border-slate-200 p-4 text-sm md:grid-cols-3">
          <Fact label="Weighted-average window">
            {number(settings.wac_window_days)} days
          </Fact>
          <Fact label="Rack price staleness">
            {number(settings.rack_staleness_days)} days
          </Fact>
          <Fact label="Below-floor margin">
            ${microsToDecimalString(settings.floor_micros)}/gal
          </Fact>
          <Fact label="Timezone for sale dates">{settings.timezone}</Fact>
          <Fact label="Per-product floors">
            {Object.keys(settings.product_floors).length === 0
              ? "None"
              : Object.entries(settings.product_floors)
                  .map(
                    ([code, micros]) =>
                      `${productName(code)} $${microsToDecimalString(micros)}`,
                  )
                  .join(", ")}
          </Fact>
          <Fact label="Last changed">
            {settings.updated_at
              ? `${dateTime(settings.updated_at)} by ${settings.updated_by}`
              : "Using defaults"}
          </Fact>
        </dl>
      )}
      {editing && settings && (
        <FormDialog<Values, Awaited<ReturnType<typeof submit>>>
          open
          size="md"
          title="Edit margin settings"
          help="Changes are audit-logged with before and after values."
          submitLabel="Save settings"
          successMessage={null}
          initialValues={valuesFrom(settings)}
          validate={validateMarginSettings}
          onSubmit={submit}
          onSaved={(result) => {
            setSettings(result.settings);
            const warning = result.warnings
              .map((w) => SETTINGS_WARNINGS[w] ?? w)
              .join(" ");
            notify(
              warning
                ? { type: "warning", message: warning }
                : { type: "success", message: "Settings saved" },
            );
          }}
          onClose={() => setEditing(false)}
        >
          {({ values, set, errors }) => (
            <>
              <Field
                label="Weighted-average window"
                required
                span={1}
                error={errors.wac_window_days}
              >
                <NumberField
                  id="margin-wac"
                  value={values.wac_window_days}
                  onChange={(n) => set("wac_window_days", n)}
                  unit="days"
                  decimals={0}
                  min={1}
                  max={365}
                />
              </Field>
              <Field
                label="Rack price staleness"
                required
                span={1}
                error={errors.rack_staleness_days}
              >
                <NumberField
                  id="margin-stale"
                  value={values.rack_staleness_days}
                  onChange={(n) => set("rack_staleness_days", n)}
                  unit="days"
                  decimals={0}
                  min={1}
                  max={30}
                />
              </Field>
              <Field
                label="Below-floor margin (USD per gallon)"
                required
                span={1}
                error={errors.floor}
              >
                <input
                  id="margin-floor"
                  inputMode="decimal"
                  className={INPUT_CLASS}
                  value={values.floor}
                  onChange={(e) => set("floor", e.target.value)}
                />
              </Field>
              <Field
                label="Timezone for sale dates"
                required
                span={1}
                error={errors.timezone}
              >
                <input
                  id="margin-timezone"
                  className={INPUT_CLASS}
                  value={values.timezone}
                  onChange={(e) => set("timezone", e.target.value)}
                  placeholder="America/Chicago"
                />
              </Field>
              <fieldset className="col-span-2">
                <legend className="mb-1 text-xs font-medium text-slate-700">
                  Per-product floors (USD per gallon)
                </legend>
                {values.floors.length === 0 && (
                  <p className="text-xs text-text-muted">
                    None. The tenant-wide floor applies to every product.
                  </p>
                )}
                <ul className="space-y-2">
                  {values.floors.map((row, i) => (
                    <li key={row.id} className="flex items-center gap-2">
                      <ProductSelect
                        aria-label={`Product for floor ${i + 1}`}
                        value={row.product || null}
                        options={
                          row.product &&
                          !(PRODUCT_CODES as readonly string[]).includes(
                            row.product,
                          )
                            ? [...PRODUCT_CODES, row.product]
                            : [...PRODUCT_CODES]
                        }
                        onChange={(code) =>
                          set(
                            "floors",
                            values.floors.map((r) =>
                              r.id === row.id ? { ...r, product: code } : r,
                            ),
                          )
                        }
                        className="flex-1"
                      />
                      <input
                        aria-label={`Floor for ${row.product ? productName(row.product) : `row ${i + 1}`}`}
                        inputMode="decimal"
                        className={`${INPUT_CLASS} w-32`}
                        value={row.usd}
                        onChange={(e) =>
                          set(
                            "floors",
                            values.floors.map((r) =>
                              r.id === row.id
                                ? { ...r, usd: e.target.value }
                                : r,
                            ),
                          )
                        }
                        placeholder="0.150000"
                      />
                      <IconButton
                        label={`Remove floor ${i + 1}`}
                        size="sm"
                        icon={<Trash2 className="h-3.5 w-3.5" />}
                        onClick={() =>
                          set(
                            "floors",
                            values.floors.filter((r) => r.id !== row.id),
                          )
                        }
                      />
                    </li>
                  ))}
                </ul>
                {errors.floors && (
                  <p role="alert" className="mt-1 text-xs text-red-800">
                    {errors.floors}
                  </p>
                )}
                <Button
                  type="button"
                  size="sm"
                  variant="secondary"
                  className="mt-2"
                  icon={<Plus className="h-3.5 w-3.5" />}
                  onClick={() =>
                    set("floors", [
                      ...values.floors,
                      { id: Date.now(), product: "", usd: "" },
                    ])
                  }
                >
                  Add product floor
                </Button>
              </fieldset>
            </>
          )}
        </FormDialog>
      )}
    </section>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="font-medium text-text">{children}</dd>
    </div>
  );
}
