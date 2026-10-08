"use client";

import { PageHeader } from "../ui";
/**
 * Live (`/dashboard/control`): the shift's live monitoring and agent
 * supervision view. The compact title row replaces the old hero header;
 * task 2.6 rebuilds the body as `components/live/LiveView.tsx`.
 */
import OperationsControlView from "./OperationsControlView";

export default function OperationsControlPage() {
  return (
    <div className="h-full flex flex-col bg-white">
      <PageHeader
        title="Live"
        help="Live monitoring and agent supervision: assets, jobs, fuel, and what the autonomous agents are doing. Pause agents or adjust autonomy here; go to the Dashboard to act manually."
      />
      <div className="flex-1 overflow-hidden">
        <OperationsControlView />
      </div>
    </div>
  );
}
