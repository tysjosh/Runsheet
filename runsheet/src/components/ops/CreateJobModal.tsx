"use client";

import { X } from "lucide-react";
import { useState } from "react";
import type { Job, JobType, Priority } from "../../types/api";
import { createJob } from "../../services/schedulingApi";

const JOB_TYPES: { value: JobType; label: string }[] = [
  { value: "cargo_transport", label: "Cargo Transport" },
  { value: "passenger_transport", label: "Passenger Transport" },
  { value: "vessel_movement", label: "Vessel Movement" },
  { value: "airport_transfer", label: "Airport Transfer" },
  { value: "crane_booking", label: "Crane Booking" },
];

const PRIORITIES: { value: Priority; label: string }[] = [
  { value: "low", label: "Low" },
  { value: "normal", label: "Normal" },
  { value: "high", label: "High" },
  { value: "urgent", label: "Urgent" },
];

// Asset options per job type (must match JOB_ASSET_COMPATIBILITY on backend)
const ASSETS_BY_JOB_TYPE: Record<string, { value: string; label: string }[]> = {
  cargo_transport: [
    { value: "TRK-001", label: "TRK-001 — Volvo FH16 (ABC-123-LG)" },
    { value: "TRK-002", label: "TRK-002 — MAN TGX (DEF-456-AB)" },
    { value: "TRK-003", label: "TRK-003 — Scania R500 (GHI-789-KN)" },
    { value: "TRK-004", label: "TRK-004 — DAF XF (JKL-012-PH)" },
    { value: "TRK-005", label: "TRK-005 — Mercedes Actros (MNO-345-IB)" },
    { value: "TRK-006", label: "TRK-006 — Iveco Stralis (PQR-678-EN)" },
    { value: "TRK-007", label: "TRK-007 — Toyota HiAce (STU-901-KD)" },
    { value: "TRK-008", label: "TRK-008 — Ford Transit (VWX-234-BC)" },
    { value: "TRF-001", label: "TRF-001 — Howo Tanker (YZA-567-LG)" },
    { value: "TRF-002", label: "TRF-002 — Sinotruk Tanker (BCD-890-PH)" },
  ],
  passenger_transport: [
    { value: "TRK-007", label: "TRK-007 — Toyota HiAce (STU-901-KD)" },
    { value: "TRK-008", label: "TRK-008 — Ford Transit (VWX-234-BC)" },
  ],
  airport_transfer: [
    { value: "TRK-007", label: "TRK-007 — Toyota HiAce (STU-901-KD)" },
    { value: "TRK-008", label: "TRK-008 — Ford Transit (VWX-234-BC)" },
  ],
  vessel_movement: [],
  crane_booking: [],
};

interface CreateJobModalProps {
  onClose: () => void;
  onCreated: (job: Job) => void;
}

export default function CreateJobModal({ onClose, onCreated }: CreateJobModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    job_type: "cargo_transport" as JobType,
    origin: "",
    destination: "",
    scheduled_time: "",
    asset_assigned: "",
    priority: "normal" as Priority,
    notes: "",
  });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.origin || !form.destination || !form.scheduled_time) {
      setError("Origin, destination, and scheduled time are required.");
      return;
    }
    setError("");
    setSubmitting(true);
    try {
      const res = await createJob({
        job_type: form.job_type,
        origin: form.origin,
        destination: form.destination,
        scheduled_time: new Date(form.scheduled_time).toISOString(),
        asset_assigned: form.asset_assigned || undefined,
        priority: form.priority,
        notes: form.notes || undefined,
      });
      onCreated(res.data);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create job");
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-[#232323]">Create Job</h2>
          <button onClick={onClose} className="p-1 text-gray-400 hover:text-gray-600 rounded">
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {error && (
            <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">{error}</p>
          )}

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Job Type</label>
              <select
                value={form.job_type}
                onChange={(e) => setForm({ ...form, job_type: e.target.value as JobType })}
                className={inputClass}
              >
                {JOB_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Priority</label>
              <select
                value={form.priority}
                onChange={(e) => setForm({ ...form, priority: e.target.value as Priority })}
                className={inputClass}
              >
                {PRIORITIES.map((p) => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Origin</label>
            <input
              type="text"
              value={form.origin}
              onChange={(e) => setForm({ ...form, origin: e.target.value })}
              placeholder="e.g. Lagos Depot"
              className={inputClass}
              required
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Destination</label>
            <input
              type="text"
              value={form.destination}
              onChange={(e) => setForm({ ...form, destination: e.target.value })}
              placeholder="e.g. Abuja Warehouse"
              className={inputClass}
              required
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Scheduled Time</label>
              <input
                type="datetime-local"
                value={form.scheduled_time}
                onChange={(e) => setForm({ ...form, scheduled_time: e.target.value })}
                className={inputClass}
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Asset (optional)</label>
              <select
                value={form.asset_assigned}
                onChange={(e) => setForm({ ...form, asset_assigned: e.target.value })}
                className={inputClass}
              >
                <option value="">— None —</option>
                {(ASSETS_BY_JOB_TYPE[form.job_type] || []).map((a) => (
                  <option key={a.value} value={a.value}>{a.label}</option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Notes (optional)</label>
            <textarea
              value={form.notes}
              onChange={(e) => setForm({ ...form, notes: e.target.value })}
              placeholder="Any additional details..."
              rows={2}
              className={inputClass + " resize-none"}
            />
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50"
              style={{ backgroundColor: "#232323" }}
            >
              {submitting ? "Creating..." : "Create Job"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
