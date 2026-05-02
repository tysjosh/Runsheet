import {
  CheckCircle,
  Download,
  Database,
  Loader2,
  Truck,
  Package,
  Users,
  Fuel,
  Boxes,
  HeadsetIcon,
  CalendarClock,
} from "lucide-react";
import { useEffect, useState } from "react";
import type { DataType, SchemaTemplate } from "../../types/import";
import { importApi } from "../../services/importApi";

// ─── Props ───────────────────────────────────────────────────────────────────

interface DataTypeSelectorProps {
  onSelect: (dataType: DataType) => void;
}

// ─── Data Type Display Config ────────────────────────────────────────────────

const DATA_TYPE_CONFIG: Record<
  DataType,
  { label: string; icon: React.ElementType }
> = {
  fleet: { label: "Fleet", icon: Truck },
  orders: { label: "Orders", icon: Package },
  riders: { label: "Riders", icon: Users },
  fuel_stations: { label: "Fuel Stations", icon: Fuel },
  inventory: { label: "Inventory", icon: Boxes },
  support_tickets: { label: "Support Tickets", icon: HeadsetIcon },
  jobs: { label: "Jobs / Scheduling", icon: CalendarClock },
};

const ALL_DATA_TYPES: DataType[] = [
  "fleet",
  "orders",
  "riders",
  "fuel_stations",
  "inventory",
  "support_tickets",
  "jobs",
];

// ─── Component ───────────────────────────────────────────────────────────────

export default function DataTypeSelector({ onSelect }: DataTypeSelectorProps) {
  const [schemas, setSchemas] = useState<Record<string, SchemaTemplate>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<DataType | null>(null);

  // Fetch all 7 schemas on mount
  useEffect(() => {
    let cancelled = false;

    async function fetchSchemas() {
      setLoading(true);
      setError(null);

      try {
        const results = await Promise.all(
          ALL_DATA_TYPES.map(async (dt) => {
            const schema = await importApi.getSchema(dt);
            return [dt, schema] as const;
          }),
        );

        if (!cancelled) {
          const schemaMap: Record<string, SchemaTemplate> = {};
          for (const [dt, schema] of results) {
            schemaMap[dt] = schema;
          }
          setSchemas(schemaMap);
        }
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof Error
              ? err.message
              : "Failed to load data type schemas",
          );
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    fetchSchemas();

    return () => {
      cancelled = true;
    };
  }, []);

  const handleCardClick = (dataType: DataType) => {
    setSelected(dataType);
  };

  const handleDownloadTemplate = (
    e: React.MouseEvent,
    dataType: DataType,
  ) => {
    e.stopPropagation();
    importApi.downloadTemplate(dataType);
  };

  const handleProceed = () => {
    if (selected) {
      onSelect(selected);
    }
  };

  // ── Loading state ────────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-gray-400">
        <Loader2 className="w-8 h-8 animate-spin mb-4" />
        <p className="text-sm">Loading data type schemas…</p>
      </div>
    );
  }

  // ── Error state ──────────────────────────────────────────────────────────

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-red-500">
        <Database className="w-10 h-10 mb-4" />
        <p className="text-sm font-medium mb-2">Failed to load schemas</p>
        <p className="text-xs text-gray-500">{error}</p>
      </div>
    );
  }

  // ── Card grid ────────────────────────────────────────────────────────────

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-lg font-semibold text-[#232323] mb-1">
          Select Data Type
        </h2>
        <p className="text-sm text-gray-500">
          Choose the type of data you want to import. Each type maps to a
          specific Elasticsearch index with its own schema.
        </p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
        {ALL_DATA_TYPES.map((dataType) => {
          const config = DATA_TYPE_CONFIG[dataType];
          const schema = schemas[dataType];
          const isSelected = selected === dataType;
          const Icon = config.icon;

          const requiredCount = schema
            ? schema.fields.filter((f) => f.required).length
            : 0;
          const optionalCount = schema
            ? schema.fields.filter((f) => !f.required).length
            : 0;

          return (
            <button
              key={dataType}
              type="button"
              onClick={() => handleCardClick(dataType)}
              className={`relative text-left rounded-xl border-2 p-5 transition-all cursor-pointer hover:shadow-md ${
                isSelected
                  ? "border-[#232323] bg-gray-50 shadow-md"
                  : "border-gray-200 bg-white hover:border-gray-300"
              }`}
            >
              {/* Selected indicator */}
              {isSelected && (
                <div className="absolute top-3 right-3">
                  <CheckCircle className="w-5 h-5 text-green-600" />
                </div>
              )}

              {/* Icon + Name */}
              <div className="flex items-center gap-3 mb-3">
                <div
                  className={`flex items-center justify-center w-10 h-10 rounded-lg ${
                    isSelected
                      ? "bg-[#232323] text-white"
                      : "bg-gray-100 text-gray-600"
                  }`}
                >
                  <Icon className="w-5 h-5" />
                </div>
                <h3 className="text-sm font-semibold text-[#232323]">
                  {config.label}
                </h3>
              </div>

              {/* Description */}
              {schema && (
                <p className="text-xs text-gray-500 mb-3 line-clamp-2">
                  {schema.description}
                </p>
              )}

              {/* ES Index */}
              {schema && (
                <div className="flex items-center gap-1.5 mb-3">
                  <Database className="w-3.5 h-3.5 text-gray-400" />
                  <span className="text-xs text-gray-400 font-mono">
                    {schema.es_index}
                  </span>
                </div>
              )}

              {/* Field counts */}
              {schema && (
                <div className="flex items-center gap-3 mb-4">
                  <span className="text-xs text-gray-600">
                    <span className="font-medium text-[#232323]">
                      {requiredCount}
                    </span>{" "}
                    required
                  </span>
                  <span className="text-xs text-gray-400">•</span>
                  <span className="text-xs text-gray-600">
                    <span className="font-medium text-gray-500">
                      {optionalCount}
                    </span>{" "}
                    optional
                  </span>
                </div>
              )}

              {/* Download Template button */}
              <div
                role="button"
                tabIndex={0}
                onClick={(e) => handleDownloadTemplate(e, dataType)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    handleDownloadTemplate(
                      e as unknown as React.MouseEvent,
                      dataType,
                    );
                  }
                }}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-600 bg-gray-100 rounded-lg hover:bg-gray-200 hover:text-[#232323] transition-colors"
              >
                <Download className="w-3.5 h-3.5" />
                Download Template
              </div>
            </button>
          );
        })}
      </div>

      {/* Proceed button */}
      <div className="flex justify-end">
        <button
          type="button"
          onClick={handleProceed}
          disabled={!selected}
          className={`px-6 py-2.5 text-sm font-medium rounded-xl transition-colors ${
            selected
              ? "bg-[#232323] text-white hover:bg-black"
              : "bg-gray-100 text-gray-400 cursor-not-allowed"
          }`}
        >
          Continue with{" "}
          {selected ? DATA_TYPE_CONFIG[selected].label : "…"}
        </button>
      </div>
    </div>
  );
}
