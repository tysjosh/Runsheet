"use client";

/**
 * Dispatch: Board · Jobs · Plans (UI revamp R8.1, D2/D3).
 *
 * The Board view shows only while `GET /api/fuel/board/status` answers a mode:
 * 404 (flag disabled) or 403 (role) hides it. It is the default view when the
 * mode is active; otherwise Jobs is. `?tab=board|jobs|plans` picks a view, and
 * the old `scheduling` / `distribution` values still work as aliases.
 *
 * The view switcher sits in the title row. Task 2.2 moves the board's own
 * toolbar into the shared title/toolbar rows; until then the board renders
 * its toolbar below.
 */
import { CalendarClock, Droplets, LayoutGrid } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import {
  type BoardMode,
  boardErrorStatus,
  getBoardStatus,
} from "../services/dispatchBoardApi";
import LoadingSpinner from "./LoadingSpinner";
import {
  PageChromeProvider,
  PageHeader,
  type TabItem,
  TabPanel,
  useUrlTab,
} from "./ui";

const SchedulingJobBoard = lazy(() => import("./dispatch/SchedulingJobBoard"));
const FuelDistributionPage = lazy(() => import("./ops/FuelDistributionPage"));
const DispatchBoard = lazy(() => import("./dispatch-board/DispatchBoard"));

/** K13 kill switch: `/status` is re-read this often and on window focus. */
export const BOARD_STATUS_POLL_MS = 5 * 60 * 1000;

export const DISPATCH_TAB_ALIASES: Record<string, string> = {
  scheduling: "jobs",
  distribution: "plans",
};

const BOARD_TAB: TabItem = {
  id: "board",
  label: "Board",
  icon: <LayoutGrid className="w-4 h-4" />,
};
const TABS: TabItem[] = [
  { id: "jobs", label: "Jobs", icon: <CalendarClock className="w-4 h-4" /> },
  { id: "plans", label: "Plans", icon: <Droplets className="w-4 h-4" /> },
];

export default function DispatchPage() {
  const [boardMode, setBoardMode] = useState<BoardMode | null>(null);

  const refreshStatus = useCallback(async () => {
    try {
      const { mode } = await getBoardStatus();
      setBoardMode(mode);
    } catch (err) {
      const status = boardErrorStatus(err);
      // 404 disabled / 403 role: hide. Anything else is transient: keep as is.
      if (status === 404 || status === 403) setBoardMode(null);
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
    const id = setInterval(() => void refreshStatus(), BOARD_STATUS_POLL_MS);
    const onFocus = () => void refreshStatus();
    window.addEventListener("focus", onFocus);
    return () => {
      clearInterval(id);
      window.removeEventListener("focus", onFocus);
    };
  }, [refreshStatus]);

  const tabs = boardMode ? [BOARD_TAB, ...TABS] : TABS;
  const boardDefault =
    boardMode === "active_gated" || boardMode === "active_auto";
  const [active, setActive] = useUrlTab(
    tabs.map((t) => t.id),
    {
      aliases: DISPATCH_TAB_ALIASES,
      fallback: boardDefault ? "board" : "jobs",
    },
  );

  return (
    <PageChromeProvider>
      <div className="flex flex-col h-full">
        <PageHeader
          host
          title="Dispatch"
          help="Plan the day on the board, work the job list, and review fuel distribution plans"
          tabs={tabs}
          tab={active}
          onTabChange={setActive}
          tabIdBase="dispatch"
        />
        <TabPanel
          idBase="dispatch"
          value={active}
          className="flex-1 overflow-auto"
        >
          <Suspense fallback={<LoadingSpinner message="Loading..." />}>
            {active === "board" && boardMode && (
              <DispatchBoard
                mode={boardMode}
                onExit={() => setActive("jobs")}
              />
            )}
            {active === "jobs" && <SchedulingJobBoard />}
            {active === "plans" && <FuelDistributionPage />}
          </Suspense>
        </TabPanel>
      </div>
    </PageChromeProvider>
  );
}
