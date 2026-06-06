"use client";

import { AlertCircle, Check, Flag, Info, X } from "lucide-react";
import { useState } from "react";
import {
  type FeatureFlagState,
  setOrderIntakePipelineState,
} from "../../services/adminApi";
import { Badge, Button, PageHeader } from "../ui";

// ─── Helper Functions ────────────────────────────────────────────────────────

function getStateBadge(state: FeatureFlagState) {
  switch (state) {
    case "disabled":
      return <Badge variant="default">Disabled</Badge>;
    case "shadow":
      return <Badge variant="info">Shadow Mode</Badge>;
    case "active_gated":
      return <Badge variant="warning">Active (Gated)</Badge>;
    case "active_auto":
      return <Badge variant="success">Active (Auto)</Badge>;
    default:
      return <Badge variant="default">{state}</Badge>;
  }
}

function getStateDescription(state: FeatureFlagState): string {
  switch (state) {
    case "disabled":
      return "Feature is completely disabled. All orders route through legacy pipeline.";
    case "shadow":
      return "Feature runs in shadow mode. New pipeline processes orders but results are logged only (not persisted).";
    case "active_gated":
      return "Feature is active with manual approval. Orders are processed by new pipeline but require dispatcher approval.";
    case "active_auto":
      return "Feature is fully active. Orders are automatically processed by new pipeline without manual approval.";
    default:
      return "Unknown state";
  }
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function FeatureFlagsAdmin() {
  const [tenantId, setTenantId] = useState("demo-tenant");
  const [currentState, setCurrentState] =
    useState<FeatureFlagState>("disabled");
  const [selectedState, setSelectedState] =
    useState<FeatureFlagState>("disabled");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const handleUpdateFlag = async () => {
    if (selectedState === currentState) {
      setError("Selected state is the same as current state");
      return;
    }

    setLoading(true);
    setError(null);
    setSuccess(null);

    try {
      const response = await setOrderIntakePipelineState(
        tenantId,
        selectedState,
      );
      setCurrentState(response.data.new_state);
      setSuccess(
        `Feature flag updated successfully. ${response.data.ws_broadcast ? "WebSocket broadcast sent." : ""}`,
      );
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to update feature flag",
      );
    } finally {
      setLoading(false);
    }
  };

  const states: FeatureFlagState[] = [
    "disabled",
    "shadow",
    "active_gated",
    "active_auto",
  ];

  return (
    <div className="p-6">
      <PageHeader
        title="Feature Flags"
        subtitle="Manage feature rollout and experimentation flags"
        icon={<Flag className="w-5 h-5" />}
      />

      {/* Info Banner */}
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-6 flex items-start gap-3">
        <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
        <div className="text-sm text-blue-900">
          <p className="font-medium mb-1">About Feature Flags</p>
          <p>
            Feature flags allow you to control feature rollout without code
            deployments. Changes take effect within 60 seconds and are broadcast
            to all active WebSocket clients.
          </p>
        </div>
      </div>

      {/* Order Intake Pipeline Flag */}
      <div className="bg-white rounded-xl border border-gray-200 p-6 mb-6">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">
              Order Intake Pipeline
            </h2>
            <p className="text-sm text-gray-600 mt-1">
              Control the new order intake pipeline rollout
            </p>
          </div>
          {getStateBadge(currentState)}
        </div>

        {/* Tenant Selection */}
        <div className="mb-6">
          <label className="block text-sm font-medium text-gray-700 mb-2">
            Tenant ID
          </label>
          <input
            type="text"
            value={tenantId}
            onChange={(e) => setTenantId(e.target.value)}
            placeholder="demo-tenant"
            className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary focus:border-primary"
          />
        </div>

        {/* State Selection */}
        <div className="mb-6">
          <label className="block text-sm font-medium text-gray-700 mb-3">
            Select New State
          </label>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {states.map((state) => (
              <button
                key={state}
                type="button"
                onClick={() => setSelectedState(state)}
                className={`
                  relative p-4 border-2 rounded-lg text-left transition-all
                  ${
                    selectedState === state
                      ? "border-primary bg-primary/5"
                      : "border-gray-200 hover:border-gray-300"
                  }
                  ${state === currentState ? "opacity-50" : ""}
                `}
              >
                <div className="flex items-start justify-between mb-2">
                  <div className="flex items-center gap-2">
                    {getStateBadge(state)}
                    {state === currentState && (
                      <span className="text-xs text-gray-500">(Current)</span>
                    )}
                  </div>
                  {selectedState === state && (
                    <Check className="w-5 h-5 text-primary" />
                  )}
                </div>
                <p className="text-sm text-gray-600">
                  {getStateDescription(state)}
                </p>
              </button>
            ))}
          </div>
        </div>

        {/* Error/Success Messages */}
        {error && (
          <div className="bg-error-light border border-error text-error-dark p-3 rounded-lg mb-4 flex items-center gap-2">
            <AlertCircle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm">{error}</span>
          </div>
        )}

        {success && (
          <div className="bg-success-light border border-success text-success-dark p-3 rounded-lg mb-4 flex items-center gap-2">
            <Check className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm">{success}</span>
          </div>
        )}

        {/* Action Buttons */}
        <div className="flex items-center gap-3">
          <Button
            onClick={handleUpdateFlag}
            disabled={loading || selectedState === currentState}
          >
            {loading ? "Updating..." : "Update Flag"}
          </Button>
          <Button
            variant="ghost"
            onClick={() => setSelectedState(currentState)}
            disabled={loading}
          >
            Reset
          </Button>
        </div>
      </div>

      {/* State Transition Guide */}
      <div className="bg-white rounded-xl border border-gray-200 p-6">
        <h3 className="text-lg font-semibold text-gray-900 mb-4">
          Recommended Rollout Path
        </h3>
        <div className="space-y-3">
          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-full bg-gray-100 flex items-center justify-center flex-shrink-0 text-sm font-medium">
              1
            </div>
            <div>
              <p className="font-medium text-gray-900">
                Start with Shadow Mode
              </p>
              <p className="text-sm text-gray-600">
                Test the new pipeline without affecting production. Review logs
                to ensure correctness.
              </p>
            </div>
          </div>

          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-full bg-gray-100 flex items-center justify-center flex-shrink-0 text-sm font-medium">
              2
            </div>
            <div>
              <p className="font-medium text-gray-900">Enable Active (Gated)</p>
              <p className="text-sm text-gray-600">
                Process real orders with manual dispatcher approval. Catch edge
                cases before full automation.
              </p>
            </div>
          </div>

          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-full bg-gray-100 flex items-center justify-center flex-shrink-0 text-sm font-medium">
              3
            </div>
            <div>
              <p className="font-medium text-gray-900">
                Full Rollout (Active Auto)
              </p>
              <p className="text-sm text-gray-600">
                Enable automatic processing once confidence is high. Monitor
                metrics closely.
              </p>
            </div>
          </div>

          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-full bg-red-100 flex items-center justify-center flex-shrink-0">
              <X className="w-4 h-4 text-red-600" />
            </div>
            <div>
              <p className="font-medium text-gray-900">Emergency Rollback</p>
              <p className="text-sm text-gray-600">
                If issues arise, immediately set to Disabled to route all
                traffic through legacy pipeline.
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
