import {
  ArrowLeft,
  AlertCircle,
  FileSpreadsheet,
  Link2,
  Loader2,
  RotateCcw,
  Upload,
  CheckCircle,
} from "lucide-react";
import { useCallback, useRef, useState } from "react";
import type { DataType, ParseResponse } from "../../types/import";
import { importApi } from "../../services/importApi";

// ─── Props ───────────────────────────────────────────────────────────────────

interface SourceUploaderProps {
  dataType: DataType;
  onParsed: (data: {
    sessionId: string;
    columns: string[];
    sampleRows: Record<string, string>[];
    suggestedMapping: Record<string, string>;
  }) => void;
  onBack: () => void;
}

// ─── Constants ───────────────────────────────────────────────────────────────

const MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024; // 10MB
const MAX_FILE_SIZE_LABEL = "10MB";

type SourceTab = "csv" | "sheets";

// ─── Component ───────────────────────────────────────────────────────────────

export default function SourceUploader({
  dataType,
  onParsed,
  onBack,
}: SourceUploaderProps) {
  const [activeTab, setActiveTab] = useState<SourceTab>("csv");

  // CSV state
  const [dragOver, setDragOver] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Google Sheets state
  const [sheetsUrl, setSheetsUrl] = useState("");

  // Shared state
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [parseResult, setParseResult] = useState<ParseResponse | null>(null);

  // ── File validation ────────────────────────────────────────────────────

  const validateFile = useCallback((file: File): string | null => {
    if (file.size > MAX_FILE_SIZE_BYTES) {
      return `File exceeds the ${MAX_FILE_SIZE_LABEL} size limit. Please split your data or reduce the file size.`;
    }
    const extension = file.name.split(".").pop()?.toLowerCase();
    if (extension !== "csv") {
      return "Only CSV files are supported. Please select a .csv file.";
    }
    return null;
  }, []);

  // ── CSV upload handler ─────────────────────────────────────────────────

  const handleFileSelected = useCallback(
    async (file: File) => {
      setError(null);
      setParseResult(null);

      const validationError = validateFile(file);
      if (validationError) {
        setError(validationError);
        setSelectedFile(null);
        return;
      }

      setSelectedFile(file);
      setLoading(true);

      try {
        const result = await importApi.uploadCSV(file, dataType);
        setParseResult(result);
      } catch (err) {
        setError(
          err instanceof Error
            ? err.message
            : "Failed to parse CSV file. Please check the file format and try again.",
        );
      } finally {
        setLoading(false);
      }
    },
    [dataType, validateFile],
  );

  // ── Drag-and-drop handlers ────────────────────────────────────────────

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(false);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setDragOver(false);

      const files = e.dataTransfer.files;
      if (files.length > 0) {
        handleFileSelected(files[0]);
      }
    },
    [handleFileSelected],
  );

  const handleFileInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = e.target.files;
      if (files && files.length > 0) {
        handleFileSelected(files[0]);
      }
      // Reset input so the same file can be re-selected
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    },
    [handleFileSelected],
  );

  // ── Google Sheets handler ─────────────────────────────────────────────

  const handleSheetsSubmit = useCallback(async () => {
    if (!sheetsUrl.trim()) return;

    setError(null);
    setParseResult(null);
    setLoading(true);

    try {
      const result = await importApi.uploadSheets(sheetsUrl.trim(), dataType);
      setParseResult(result);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Could not access the Google Sheet. Please check the URL and sharing permissions.",
      );
    } finally {
      setLoading(false);
    }
  }, [sheetsUrl, dataType]);

  // ── Try Again handler ─────────────────────────────────────────────────

  const handleTryAgain = useCallback(() => {
    setError(null);
    setParseResult(null);
    setSelectedFile(null);
    setSheetsUrl("");
    setLoading(false);
  }, []);

  // ── Proceed handler ───────────────────────────────────────────────────

  const handleProceed = useCallback(() => {
    if (!parseResult) return;
    onParsed({
      sessionId: parseResult.session_id,
      columns: parseResult.columns,
      sampleRows: parseResult.sample_rows,
      suggestedMapping: parseResult.suggested_mapping,
    });
  }, [parseResult, onParsed]);

  // ── Tab switch ────────────────────────────────────────────────────────

  const handleTabSwitch = useCallback(
    (tab: SourceTab) => {
      if (loading) return;
      setActiveTab(tab);
      setError(null);
      setParseResult(null);
      setSelectedFile(null);
      setSheetsUrl("");
    },
    [loading],
  );

  // ── Render ────────────────────────────────────────────────────────────

  return (
    <div>
      {/* Header */}
      <div className="mb-6">
        <h2 className="text-lg font-semibold text-[#232323] mb-1">
          Upload Source Data
        </h2>
        <p className="text-sm text-gray-500">
          Upload a CSV file or connect a Google Sheet to import your{" "}
          <span className="font-medium text-[#232323]">
            {dataType.replace(/_/g, " ")}
          </span>{" "}
          data.
        </p>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-gray-200 mb-6">
        <button
          type="button"
          onClick={() => handleTabSwitch("csv")}
          className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
            activeTab === "csv"
              ? "border-[#232323] text-[#232323]"
              : "border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300"
          }`}
        >
          <FileSpreadsheet className="w-4 h-4" />
          CSV Upload
        </button>
        <button
          type="button"
          onClick={() => handleTabSwitch("sheets")}
          className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
            activeTab === "sheets"
              ? "border-[#232323] text-[#232323]"
              : "border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300"
          }`}
        >
          <Link2 className="w-4 h-4" />
          Google Sheets
        </button>
      </div>

      {/* Tab content */}
      {!parseResult && !error && !loading && (
        <>
          {activeTab === "csv" && (
            <CsvDropZone
              dragOver={dragOver}
              selectedFile={selectedFile}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
              onBrowseClick={() => fileInputRef.current?.click()}
              fileInputRef={fileInputRef}
              onFileInputChange={handleFileInputChange}
            />
          )}

          {activeTab === "sheets" && (
            <SheetsInput
              url={sheetsUrl}
              onUrlChange={setSheetsUrl}
              onSubmit={handleSheetsSubmit}
            />
          )}
        </>
      )}

      {/* Loading state */}
      {loading && <LoadingState activeTab={activeTab} />}

      {/* Error state */}
      {error && !loading && (
        <ErrorDisplay message={error} onTryAgain={handleTryAgain} />
      )}

      {/* Parse result preview */}
      {parseResult && !loading && !error && (
        <ParsePreview result={parseResult} />
      )}

      {/* Footer actions */}
      <div className="flex items-center justify-between mt-8 pt-6 border-t border-gray-100">
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-2 px-4 py-2.5 text-sm font-medium text-gray-600 hover:text-[#232323] transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
          Back
        </button>

        {parseResult && !loading && !error && (
          <button
            type="button"
            onClick={handleProceed}
            className="px-6 py-2.5 text-sm font-medium rounded-xl bg-[#232323] text-white hover:bg-black transition-colors"
          >
            Continue to Field Mapping
          </button>
        )}
      </div>
    </div>
  );
}

// ─── CSV Drop Zone Sub-component ─────────────────────────────────────────────

function CsvDropZone({
  dragOver,
  selectedFile,
  onDragOver,
  onDragLeave,
  onDrop,
  onBrowseClick,
  fileInputRef,
  onFileInputChange,
}: {
  dragOver: boolean;
  selectedFile: File | null;
  onDragOver: (e: React.DragEvent) => void;
  onDragLeave: (e: React.DragEvent) => void;
  onDrop: (e: React.DragEvent) => void;
  onBrowseClick: () => void;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  onFileInputChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
}) {
  return (
    <div>
      <div
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        className={`flex flex-col items-center justify-center rounded-xl border-2 border-dashed p-12 transition-colors cursor-pointer ${
          dragOver
            ? "border-[#232323] bg-gray-50"
            : "border-gray-300 bg-white hover:border-gray-400 hover:bg-gray-50"
        }`}
        onClick={onBrowseClick}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onBrowseClick();
          }
        }}
        aria-label="Drop CSV file here or click to browse"
      >
        <Upload
          className={`w-10 h-10 mb-4 ${dragOver ? "text-[#232323]" : "text-gray-400"}`}
        />
        <p className="text-sm font-medium text-[#232323] mb-1">
          {dragOver ? "Drop your file here" : "Drag and drop your CSV file"}
        </p>
        <p className="text-xs text-gray-500 mb-4">
          or click to browse your files
        </p>
        <div className="flex items-center gap-3 text-xs text-gray-400">
          <span>.csv files only</span>
          <span>•</span>
          <span>Max {MAX_FILE_SIZE_LABEL}</span>
        </div>
      </div>

      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        accept=".csv"
        onChange={onFileInputChange}
        className="hidden"
        aria-hidden="true"
      />

      {/* Selected file indicator */}
      {selectedFile && (
        <div className="mt-3 flex items-center gap-2 text-sm text-gray-600">
          <FileSpreadsheet className="w-4 h-4 text-gray-400" />
          <span>{selectedFile.name}</span>
          <span className="text-xs text-gray-400">
            ({(selectedFile.size / 1024).toFixed(1)} KB)
          </span>
        </div>
      )}
    </div>
  );
}

// ─── Google Sheets Input Sub-component ───────────────────────────────────────

function SheetsInput({
  url,
  onUrlChange,
  onSubmit,
}: {
  url: string;
  onUrlChange: (url: string) => void;
  onSubmit: () => void;
}) {
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && url.trim()) {
      onSubmit();
    }
  };

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-6">
      <div className="flex items-start gap-3 mb-4">
        <div className="flex items-center justify-center w-10 h-10 rounded-lg bg-gray-100 text-gray-600 flex-shrink-0">
          <Link2 className="w-5 h-5" />
        </div>
        <div>
          <h3 className="text-sm font-semibold text-[#232323] mb-1">
            Google Sheets URL
          </h3>
          <p className="text-xs text-gray-500">
            Paste the URL of a publicly shared Google Sheet. The sheet must be
            accessible via link sharing.
          </p>
        </div>
      </div>

      <div className="flex gap-3">
        <input
          type="url"
          value={url}
          onChange={(e) => onUrlChange(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="https://docs.google.com/spreadsheets/d/..."
          className="flex-1 px-4 py-2.5 text-sm border border-gray-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-[#232323] focus:border-transparent placeholder:text-gray-400"
        />
        <button
          type="button"
          onClick={onSubmit}
          disabled={!url.trim()}
          className={`px-5 py-2.5 text-sm font-medium rounded-xl transition-colors ${
            url.trim()
              ? "bg-[#232323] text-white hover:bg-black"
              : "bg-gray-100 text-gray-400 cursor-not-allowed"
          }`}
        >
          Import
        </button>
      </div>
    </div>
  );
}

// ─── Loading State Sub-component ─────────────────────────────────────────────

function LoadingState({ activeTab }: { activeTab: SourceTab }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-gray-400">
      <Loader2 className="w-8 h-8 animate-spin mb-4" />
      <p className="text-sm font-medium text-[#232323]">
        {activeTab === "csv"
          ? "Parsing CSV file…"
          : "Fetching Google Sheet data…"}
      </p>
      <p className="text-xs text-gray-500 mt-1">
        Extracting columns and sample rows
      </p>
    </div>
  );
}

// ─── Error Display Sub-component ─────────────────────────────────────────────

function ErrorDisplay({
  message,
  onTryAgain,
}: {
  message: string;
  onTryAgain: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      <div className="flex items-center justify-center w-12 h-12 rounded-full bg-red-50 mb-4">
        <AlertCircle className="w-6 h-6 text-red-500" />
      </div>
      <p className="text-sm font-medium text-[#232323] mb-2">
        Upload Failed
      </p>
      <p className="text-xs text-gray-500 max-w-md mb-6">{message}</p>
      <button
        type="button"
        onClick={onTryAgain}
        className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-gray-600 bg-gray-100 rounded-xl hover:bg-gray-200 hover:text-[#232323] transition-colors"
      >
        <RotateCcw className="w-4 h-4" />
        Try Again
      </button>
    </div>
  );
}

// ─── Parse Preview Sub-component ─────────────────────────────────────────────

function ParsePreview({ result }: { result: ParseResponse }) {
  return (
    <div>
      {/* Success banner */}
      <div className="flex items-center gap-3 mb-6 p-4 rounded-xl bg-green-50 border border-green-100">
        <CheckCircle className="w-5 h-5 text-green-600 flex-shrink-0" />
        <div>
          <p className="text-sm font-medium text-green-800">
            Source parsed successfully
          </p>
          <p className="text-xs text-green-600">
            {result.columns.length} columns detected •{" "}
            {result.total_rows} total rows
          </p>
        </div>
      </div>

      {/* Detected columns */}
      <div className="mb-6">
        <h3 className="text-sm font-semibold text-[#232323] mb-3">
          Detected Columns
        </h3>
        <div className="flex flex-wrap gap-2">
          {result.columns.map((col) => (
            <span
              key={col}
              className="inline-flex items-center px-3 py-1 text-xs font-medium text-gray-700 bg-gray-100 rounded-lg"
            >
              {col}
            </span>
          ))}
        </div>
      </div>

      {/* Sample rows table */}
      <div>
        <h3 className="text-sm font-semibold text-[#232323] mb-3">
          Sample Preview{" "}
          <span className="font-normal text-gray-400">
            (first {result.sample_rows.length} rows)
          </span>
        </h3>
        <div className="overflow-x-auto rounded-xl border border-gray-200">
          <table className="w-full text-xs">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-200">
                <th className="px-3 py-2 text-left font-medium text-gray-500 w-12">
                  #
                </th>
                {result.columns.map((col) => (
                  <th
                    key={col}
                    className="px-3 py-2 text-left font-medium text-gray-500 whitespace-nowrap"
                  >
                    {col}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.sample_rows.map((row, idx) => (
                <tr
                  key={idx}
                  className={`border-b border-gray-100 ${
                    idx % 2 === 0 ? "bg-white" : "bg-gray-50/50"
                  }`}
                >
                  <td className="px-3 py-2 text-gray-400 font-mono">
                    {idx + 1}
                  </td>
                  {result.columns.map((col) => (
                    <td
                      key={col}
                      className="px-3 py-2 text-gray-700 whitespace-nowrap max-w-[200px] truncate"
                    >
                      {row[col] ?? "—"}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
