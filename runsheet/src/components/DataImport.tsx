import {
  CheckCircle,
  ChevronRight,
  Database,
  History,
} from "lucide-react";
import { useState } from "react";
import type {
  DataType,
  ImportResult,
  ValidationResult,
} from "../types/import";

// Sub-components will be created in subsequent tasks (12.2–12.8).
// Lazy-load them so the wizard compiles even before they exist.
import { lazy, Suspense } from "react";

const DataTypeSelector = lazy(() => import("./import/DataTypeSelector"));
const SourceUploader = lazy(() => import("./import/SourceUploader"));
const FieldMapper = lazy(() => import("./import/FieldMapper"));
const ValidationPreview = lazy(() => import("./import/ValidationPreview"));
const ImportProgress = lazy(() => import("./import/ImportProgress"));
const ImportComplete = lazy(() => import("./import/ImportComplete"));
const ImportHistory = lazy(() => import("./import/ImportHistory"));

// ─── Workflow State ──────────────────────────────────────────────────────────

type WorkflowStep =
  | "select-type"
  | "upload"
  | "map-fields"
  | "validate"
  | "commit"
  | "complete";

interface ImportWorkflowState {
  step: WorkflowStep;
  dataType: DataType | null;
  sessionId: string | null;
  sourceColumns: string[];
  sampleRows: Record<string, string>[];
  fieldMapping: Record<string, string>;
  validationResult: ValidationResult | null;
  importResult: ImportResult | null;
}

const INITIAL_STATE: ImportWorkflowState = {
  step: "select-type",
  dataType: null,
  sessionId: null,
  sourceColumns: [],
  sampleRows: [],
  fieldMapping: {},
  validationResult: null,
  importResult: null,
};

// ─── Step Definitions ────────────────────────────────────────────────────────

const STEPS: { key: WorkflowStep; label: string; number: number }[] = [
  { key: "select-type", label: "Select Type", number: 1 },
  { key: "upload", label: "Upload", number: 2 },
  { key: "map-fields", label: "Map Fields", number: 3 },
  { key: "validate", label: "Validate", number: 4 },
  { key: "commit", label: "Import", number: 5 },
  { key: "complete", label: "Complete", number: 6 },
];

// ─── Step Indicator ──────────────────────────────────────────────────────────

function StepIndicator({ currentStep }: { currentStep: WorkflowStep }) {
  const currentIndex = STEPS.findIndex((s) => s.key === currentStep);

  return (
    <nav aria-label="Import progress" className="flex items-center gap-2">
      {STEPS.map((step, index) => {
        const isCompleted = index < currentIndex;
        const isCurrent = index === currentIndex;

        return (
          <div key={step.key} className="flex items-center">
            <div className="flex items-center gap-2">
              <div
                className={`flex items-center justify-center w-8 h-8 rounded-full text-sm font-medium transition-colors ${
                  isCompleted
                    ? "bg-green-100 text-green-700"
                    : isCurrent
                      ? "bg-[#232323] text-white"
                      : "bg-gray-100 text-gray-400"
                }`}
              >
                {isCompleted ? (
                  <CheckCircle className="w-4 h-4" />
                ) : (
                  step.number
                )}
              </div>
              <span
                className={`text-sm hidden sm:inline ${
                  isCurrent
                    ? "font-medium text-[#232323]"
                    : isCompleted
                      ? "text-green-700"
                      : "text-gray-400"
                }`}
              >
                {step.label}
              </span>
            </div>
            {index < STEPS.length - 1 && (
              <ChevronRight className="w-4 h-4 mx-2 text-gray-300 flex-shrink-0" />
            )}
          </div>
        );
      })}
    </nav>
  );
}

// ─── Placeholder for missing sub-components ──────────────────────────────────

function StepPlaceholder({ name }: { name: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-20 text-gray-400">
      <Database className="w-12 h-12 mb-4" />
      <p className="text-sm font-medium">{name}</p>
      <p className="text-xs mt-1">This step will be available soon.</p>
    </div>
  );
}

function StepFallback() {
  return (
    <div className="flex items-center justify-center py-20">
      <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gray-900" />
    </div>
  );
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function DataImport() {
  const [state, setState] = useState<ImportWorkflowState>(INITIAL_STATE);
  const [showHistory, setShowHistory] = useState(false);

  // ── Step transition helpers ──────────────────────────────────────────────

  const goToStep = (step: WorkflowStep) => {
    setState((prev) => ({ ...prev, step }));
  };

  const handleDataTypeSelected = (dataType: DataType) => {
    setState((prev) => ({
      ...prev,
      dataType,
      step: "upload",
    }));
  };

  const handleSourceParsed = (data: {
    sessionId: string;
    columns: string[];
    sampleRows: Record<string, string>[];
    suggestedMapping: Record<string, string>;
  }) => {
    setState((prev) => ({
      ...prev,
      sessionId: data.sessionId,
      sourceColumns: data.columns,
      sampleRows: data.sampleRows,
      fieldMapping: data.suggestedMapping,
      step: "map-fields",
    }));
  };

  const handleMappingConfirmed = (fieldMapping: Record<string, string>) => {
    setState((prev) => ({
      ...prev,
      fieldMapping,
      step: "validate",
    }));
  };

  const handleValidationComplete = (result: ValidationResult) => {
    setState((prev) => ({
      ...prev,
      validationResult: result,
    }));
  };

  const handleCommitStart = () => {
    setState((prev) => ({ ...prev, step: "commit" }));
  };

  const handleImportComplete = (result: ImportResult) => {
    setState((prev) => ({
      ...prev,
      importResult: result,
      step: "complete",
    }));
  };

  const handleStartNewImport = () => {
    setState(INITIAL_STATE);
    setShowHistory(false);
  };

  const handleBackToMapping = () => {
    setState((prev) => ({
      ...prev,
      step: "map-fields",
      validationResult: null,
    }));
  };

  const handleBackToUpload = () => {
    setState((prev) => ({
      ...prev,
      step: "upload",
      sessionId: null,
      sourceColumns: [],
      sampleRows: [],
      fieldMapping: {},
      validationResult: null,
    }));
  };

  // ── Render active step ───────────────────────────────────────────────────

  const renderStep = () => {
    if (showHistory) {
      return (
        <Suspense fallback={<StepFallback />}>
          <ImportHistory onClose={() => setShowHistory(false)} />
        </Suspense>
      );
    }

    switch (state.step) {
      case "select-type":
        return (
          <Suspense fallback={<StepFallback />}>
            <DataTypeSelector onSelect={handleDataTypeSelected} />
          </Suspense>
        );

      case "upload":
        return (
          <Suspense fallback={<StepFallback />}>
            <SourceUploader
              dataType={state.dataType!}
              onParsed={handleSourceParsed}
              onBack={() => goToStep("select-type")}
            />
          </Suspense>
        );

      case "map-fields":
        return (
          <Suspense fallback={<StepFallback />}>
            <FieldMapper
              dataType={state.dataType!}
              sourceColumns={state.sourceColumns}
              sampleRows={state.sampleRows}
              initialMapping={state.fieldMapping}
              onConfirm={handleMappingConfirmed}
              onBack={handleBackToUpload}
            />
          </Suspense>
        );

      case "validate":
        return (
          <Suspense fallback={<StepFallback />}>
            <ValidationPreview
              sessionId={state.sessionId!}
              fieldMapping={state.fieldMapping}
              validationResult={state.validationResult}
              onValidationComplete={handleValidationComplete}
              onCommit={handleCommitStart}
              onBackToMapping={handleBackToMapping}
              onCancel={handleBackToUpload}
            />
          </Suspense>
        );

      case "commit":
        return (
          <Suspense fallback={<StepFallback />}>
            <ImportProgress
              sessionId={state.sessionId!}
              skipErrors={
                (state.validationResult?.error_count ?? 0) > 0
              }
              onComplete={handleImportComplete}
            />
          </Suspense>
        );

      case "complete":
        return (
          <Suspense fallback={<StepFallback />}>
            <ImportComplete
              result={state.importResult!}
              onStartNew={handleStartNewImport}
            />
          </Suspense>
        );

      default:
        return <StepPlaceholder name="Unknown Step" />;
    }
  };

  // ── Main render ──────────────────────────────────────────────────────────

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6 flex-shrink-0">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h1 className="text-2xl font-semibold text-[#232323] mb-1">
              Data Import
            </h1>
            <p className="text-gray-500">
              Migrate and onboard your data — import fleet records, orders,
              inventory, and more from CSV files or Google Sheets.
            </p>
          </div>

          <button
            onClick={() => setShowHistory(!showHistory)}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-xl transition-colors ${
              showHistory
                ? "bg-[#232323] text-white"
                : "text-gray-600 hover:text-[#232323] hover:bg-gray-50"
            }`}
          >
            <History className="w-4 h-4" />
            Import History
          </button>
        </div>

        {/* Step indicator — hidden when viewing history */}
        {!showHistory && <StepIndicator currentStep={state.step} />}
      </div>

      {/* Content area */}
      <div className="flex-1 overflow-y-auto p-8">{renderStep()}</div>
    </div>
  );
}
