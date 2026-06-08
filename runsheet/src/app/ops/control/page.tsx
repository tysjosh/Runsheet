"use client";

import { Radio } from "lucide-react";
import OperationsControlView from "../../../components/ops/OperationsControlView";

/**
 * Operations Control Dashboard page — unified command center view.
 *
 * Combines active jobs, asset locations, fuel levels, and delayed
 * operations into a single real-time dashboard.
 *
 * Validates: Requirement 10.1
 */
export default function OperationsControlPage() {
  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 bg-primary rounded-xl flex items-center justify-center">
            <Radio className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-primary">
              Operations Control
            </h1>
            <p className="text-gray-500">
              Live monitoring and agent supervision — assets, jobs, fuel, and
              what the autonomous agents are doing. Pause agents or adjust
              autonomy here; head to Today to act manually.
            </p>
          </div>
        </div>
      </div>

      {/* Dashboard */}
      <div className="flex-1 overflow-hidden">
        <OperationsControlView />
      </div>
    </div>
  );
}
