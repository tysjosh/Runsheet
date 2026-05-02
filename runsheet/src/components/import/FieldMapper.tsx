import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  ChevronDown,
  Loader2,
  Minus,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { DataType, FieldDef, SchemaTemplate } from "../../types/import";
import { importApi } from "../../services/importApi";

// ─── Props ───────────────────────────────────────────────────────────────────

interface FieldMapperProps {
  dataType: DataType;
  sourceColumns: string[];
  sampleRows: Record<string, string>[];
  initialMapping: Record<string, string>;
  onConfirm: (fieldMapping: Record<string, string>) => void;
  onBack: () => void;
}

// ─── Constants ───────────────────────────────────────────────────────────────

const NOT_MAPPED = "__not_mapped__";

// ─── Component ───────────────────────────────────────────────────────────────

export default function FieldMapper({
  dataType,
  sourceColumns,
  sampleRows,
  initialMapping,
  onConfirm,
  onBack,
}: FieldMapperProps) {
  const [schema, setSchema] = useState<SchemaTemplate | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // mapping: sourceColumn -> targetFieldName (or NOT_MAPPED)
  const [mapping, setMapping] = useState<Record<string, string>>({});

  // ── Fetch schema on mount ──────────────────────────────────────────────

  useEffect(() => {
    let cancelled = false;

    async function fetchSchema() {
      setLoading(true);
      setError(null);

      try {
        const result = await importApi.getSchema(dataType);
        if (!cancelled) {
          setSchema(result);
        }
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof Error
              ? err.message
              : "Failed to load schema for this data type",
          );
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    fetchSchema();
    return () => {
      cancelled = true;
    };
  }, [dataType]);

  // ── Initialize mapping from initialMapping once schema loads ───────────

  useEffect(() => {
    if (!schema) return;

    const validTargetNames = new Set(schema.fields.map((f) => f.name));
    const initial: Record<string, string> = {};

    for (const col of sourceColumns) {
      const suggested = initialMapping[col];
      if (suggested && validTargetNames.has(suggested)) {
        initial[col] = suggested;
      } else {
        initial[col] = NOT_MAPPED;
      }
    }

    setMapping(initial);
  }, [schema, sourceColumns, initialMapping]);

  // ── Derived data ───────────────────────────────────────────────────────

  const targetFields: FieldDef[] = useMemo(
    () => schema?.fields ?? [],
    [schema],
  );

  const requiredFields = useMemo(
    () => targetFields.filter((f) => f.required),
    [targetFields],
  );

  // Set of target field names that are currently mapped by some source column
  const mappedTargets = useMemo(() => {
    const set = new Set<string>();
    for (const target of Object.values(mapping)) {
      if (target !== NOT_MAPPED) {
        set.add(target);
      }
    }
    return set;
  }, [mapping]);

  // Required fields that have no source column mapped to them
  const unmappedRequired = useMemo(
    () => requiredFields.filter((f) => !mappedTargets.has(f.name)),
    [requiredFields, mappedTargets],
  );

  // Count of mapped columns (excluding NOT_MAPPED)
  const mappedCount = useMemo(
    () =>
      Object.values(mapping).filter((v) => v !== NOT_MAPPED).length,
    [mapping],
  );

  // ── Handlers ───────────────────────────────────────────────────────────

  const handleMappingChange = useCallback(
    (sourceColumn: string, targetField: string) => {
      setMapping((prev) => ({
        ...prev,
        [sourceColumn]: targetField,
      }));
    },
    [],
  );

  const handleConfirm = useCallback(() => {
    // Build the final mapping, excluding NOT_MAPPED entries
    const finalMapping: Record<string, string> = {};
    for (const [source, target] of Object.entries(mapping)) {
      if (target !== NOT_MAPPED) {
        finalMapping[source] = target;
      }
    }
    onConfirm(finalMapping);
  }, [mapping, onConfirm]);

  // ── Loading state ──────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-gray-400">
        <Loader2 className="w-8 h-8 animate-spin mb-4" />
        <p className="text-sm">Loading field schema…</p>
      </div>
    );
  }

  // ── Error state ────────────────────────────────────────────────────────

  if (error || !schema) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-red-500">
        <AlertTriangle className="w-10 h-10 mb-4" />
        <p className="text-sm font-medium mb-2">Failed to load schema</p>
        <p className="text-xs text-gray-500">{error}</p>
      </div>
    );
  }

  // ── Render ─────────────────────────────────────────────────────────────

  return (
    <div>
      {/* Header */}
      <div className="mb-6">
        <h2 className="text-lg font-semibold text-[#232323] mb-1">
          Map Fields
        </h2>
        <p className="text-sm text-gray-500">
          Map each source column to a target{" "}
          <span className="font-medium text-[#232323]">
            {dataType.replace(/_/g, " ")}
          </span>{" "}
          field. Required fields are marked with{" "}
          <span className="text-red-500 font-medium">*</span>.
        </p>
      </div>

      {/* Unmapped required fields warning */}
      {unmappedRequired.length > 0 && (
        <div className="mb-6 p-4 rounded-xl bg-amber-50 border border-amber-200">
          <div className="flex items-start gap-3">
            <AlertTriangle className="w-5 h-5 text-amber-600 flex-shrink-0 mt-0.5" />
            <div>
              <p className="text-sm font-medium text-amber-800 mb-1">
                {unmappedRequired.length} required{" "}
                {unmappedRequired.length === 1 ? "field is" : "fields are"}{" "}
                not mapped
              </p>
              <div className="flex flex-wrap gap-2">
                {unmappedRequired.map((field) => (
                  <span
                    key={field.name}
                    className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-amber-700 bg-amber-100 rounded-lg"
                  >
                    <AlertTriangle className="w-3 h-3" />
                    {field.name}
                  </span>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Mapping summary */}
      <div className="flex items-center gap-4 mb-4 text-xs text-gray-500">
        <span>
          <span className="font-medium text-[#232323]">{mappedCount}</span> of{" "}
          {sourceColumns.length} columns mapped
        </span>
        <span className="text-gray-300">•</span>
        <span>
          <span className="font-medium text-[#232323]">
            {requiredFields.length - unmappedRequired.length}
          </span>{" "}
          of {requiredFields.length} required fields covered
        </span>
      </div>

      {/* Mapping table */}
      <div className="rounded-xl border border-gray-200 overflow-hidden mb-6">
        {/* Table header */}
        <div className="grid grid-cols-[1fr_40px_1fr] items-center gap-2 px-4 py-3 bg-gray-50 border-b border-gray-200">
          <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">
            Source Column
          </span>
          <span />
          <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">
            Target Field
          </span>
        </div>

        {/* Mapping rows */}
        {sourceColumns.map((col, idx) => {
          const currentTarget = mapping[col] ?? NOT_MAPPED;
          const targetField = targetFields.find(
            (f) => f.name === currentTarget,
          );
          const isMapped = currentTarget !== NOT_MAPPED;

          // Get a sample value from the first sample row for this column
          const sampleValue =
            sampleRows.length > 0 ? sampleRows[0][col] : undefined;

          return (
            <div
              key={col}
              className={`grid grid-cols-[1fr_40px_1fr] items-center gap-2 px-4 py-3 ${
                idx < sourceColumns.length - 1
                  ? "border-b border-gray-100"
                  : ""
              } ${isMapped ? "bg-white" : "bg-gray-50/50"}`}
            >
              {/* Source column */}
              <div>
                <p className="text-sm font-medium text-[#232323]">{col}</p>
                {sampleValue !== undefined && (
                  <p className="text-xs text-gray-400 mt-0.5 truncate max-w-[240px]">
                    e.g. {sampleValue || "—"}
                  </p>
                )}
              </div>

              {/* Arrow */}
              <div className="flex justify-center">
                {isMapped ? (
                  <ArrowRight className="w-4 h-4 text-green-500" />
                ) : (
                  <Minus className="w-4 h-4 text-gray-300" />
                )}
              </div>

              {/* Target field dropdown */}
              <TargetFieldDropdown
                sourceColumn={col}
                currentTarget={currentTarget}
                targetFields={targetFields}
                mappedTargets={mappedTargets}
                onChange={handleMappingChange}
              />
            </div>
          );
        })}
      </div>

      {/* Footer actions */}
      <div className="flex items-center justify-between pt-6 border-t border-gray-100">
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-2 px-4 py-2.5 text-sm font-medium text-gray-600 hover:text-[#232323] transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
          Back
        </button>

        <button
          type="button"
          onClick={handleConfirm}
          className="px-6 py-2.5 text-sm font-medium rounded-xl bg-[#232323] text-white hover:bg-black transition-colors"
        >
          Confirm Mapping
        </button>
      </div>
    </div>
  );
}

// ─── Target Field Dropdown Sub-component ─────────────────────────────────────

function TargetFieldDropdown({
  sourceColumn,
  currentTarget,
  targetFields,
  mappedTargets,
  onChange,
}: {
  sourceColumn: string;
  currentTarget: string;
  targetFields: FieldDef[];
  mappedTargets: Set<string>;
  onChange: (sourceColumn: string, targetField: string) => void;
}) {
  const isMapped = currentTarget !== NOT_MAPPED;
  const currentField = targetFields.find((f) => f.name === currentTarget);

  return (
    <div className="relative">
      <select
        value={currentTarget}
        onChange={(e) => onChange(sourceColumn, e.target.value)}
        className={`w-full appearance-none pl-3 pr-8 py-2 text-sm rounded-lg border transition-colors cursor-pointer ${
          isMapped
            ? "border-green-200 bg-green-50 text-green-800"
            : "border-gray-200 bg-white text-gray-500"
        } focus:outline-none focus:ring-2 focus:ring-[#232323] focus:border-transparent`}
        aria-label={`Target field for ${sourceColumn}`}
      >
        <option value={NOT_MAPPED}>— Not mapped —</option>

        {/* Required fields group */}
        <optgroup label="Required fields">
          {targetFields
            .filter((f) => f.required)
            .map((field) => {
              const isDisabled =
                mappedTargets.has(field.name) &&
                field.name !== currentTarget;

              return (
                <option
                  key={field.name}
                  value={field.name}
                  disabled={isDisabled}
                >
                  {field.name} * {isDisabled ? "(already mapped)" : ""}
                </option>
              );
            })}
        </optgroup>

        {/* Optional fields group */}
        <optgroup label="Optional fields">
          {targetFields
            .filter((f) => !f.required)
            .map((field) => {
              const isDisabled =
                mappedTargets.has(field.name) &&
                field.name !== currentTarget;

              return (
                <option
                  key={field.name}
                  value={field.name}
                  disabled={isDisabled}
                >
                  {field.name} {isDisabled ? "(already mapped)" : ""}
                </option>
              );
            })}
        </optgroup>
      </select>

      {/* Dropdown chevron */}
      <ChevronDown
        className={`absolute right-2.5 top-1/2 -translate-y-1/2 w-4 h-4 pointer-events-none ${
          isMapped ? "text-green-600" : "text-gray-400"
        }`}
      />

      {/* Field info below dropdown */}
      {currentField && (
        <div className="flex items-center gap-1.5 mt-1">
          {currentField.required ? (
            <span className="text-[10px] font-medium text-red-500 bg-red-50 px-1.5 py-0.5 rounded">
              Required
            </span>
          ) : (
            <span className="text-[10px] font-medium text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">
              Optional
            </span>
          )}
          <span className="text-[10px] text-gray-400 truncate max-w-[180px]">
            {currentField.description}
          </span>
        </div>
      )}
    </div>
  );
}
