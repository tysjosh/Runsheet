"use client";
import { CalendarClock, Droplets, LayoutGrid } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  type BoardMode,
  boardErrorStatus,
  getBoardStatus,
} from "../services/dispatchBoardApi";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader, type Tab, TabNavigation } from "./ui";

const SchedulingJobBoard = lazy(() => import("../app/ops/scheduling/page"));
const FuelDistributionPage = lazy(() => import("./ops/FuelDistributionPage"));
const DispatchBoard = lazy(() => import("./dispatch-board/DispatchBoard"));

/** K13 kill switch: `/status` is re-read this often and on window focus. */
export const BOARD_STATUS_POLL_MS = 5 * 60 * 1000;

const BOARD_TAB: Tab = {
  id: "board",
  label: "Board",
  icon: <LayoutGrid className="w-4 h-4" />,
};

const TABS: Tab[] = [
  {
    id: "scheduling",
    label: "Scheduling",
    icon: <CalendarClock className="w-4 h-4" />,
  },
  {
    id: "distribution",
    label: "Fuel Distribution",
    icon: <Droplets className="w-4 h-4" />,
  },
];

type TabId = string;
const KNOWN_TABS = new Set(["board", "scheduling", "distribution"]);

/**
 * Dispatch area. The Board tab (dispatch-board R1) shows only while
 * `GET /api/fuel/board/status` answers a mode: 404 (flag disabled) or 403
 * (role) hides it. It is the default tab when the mode is active, and
 * `?tab=` picks a tab directly.
 */
export default function DispatchPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const requested = searchParams?.get("tab") ?? null;
  const [boardMode, setBoardMode] = useState<BoardMode | null>(null);
  const [activeTab, setActiveTab] = useState<TabId>(
    requested && KNOWN_TABS.has(requested) && requested !== "board"
      ? requested
      : "scheduling",
  );
  // Once the user picks a tab (or a ?tab= names one) the status answer
  // no longer changes the tab, except to leave a board that was switched off.
  const chosen = useRef(Boolean(requested && KNOWN_TABS.has(requested)));
  const requestedRef = useRef(requested);

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

  // Apply the status answer to the active tab.
  useEffect(() => {
    if (boardMode === null) {
      setActiveTab((t) => (t === "board" ? "scheduling" : t));
      return;
    }
    if (requestedRef.current === "board") {
      requestedRef.current = null;
      setActiveTab("board");
      return;
    }
    if (
      !chosen.current &&
      (boardMode === "active_gated" || boardMode === "active_auto")
    ) {
      setActiveTab("board");
    }
  }, [boardMode]);

  const changeTab = useCallback(
    (tab: TabId) => {
      chosen.current = true;
      requestedRef.current = null;
      setActiveTab(tab);
      const params = new URLSearchParams(searchParams?.toString() ?? "");
      params.set("tab", tab);
      router.replace(`?${params.toString()}`, { scroll: false });
    },
    [router, searchParams],
  );

  const tabs = boardMode ? [BOARD_TAB, ...TABS] : TABS;

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="Dispatch"
        subtitle="Schedule jobs and plan fuel distribution runs"
        icon={<CalendarClock className="w-5 h-5" />}
      />
      <TabNavigation tabs={tabs} activeTab={activeTab} onChange={changeTab} />
      <div className="flex-1 overflow-auto">
        <Suspense fallback={<LoadingSpinner message="Loading..." />}>
          {activeTab === "board" && boardMode && (
            <DispatchBoard
              mode={boardMode}
              onExit={() => changeTab("scheduling")}
            />
          )}
          {activeTab === "scheduling" && <SchedulingJobBoard />}
          {activeTab === "distribution" && <FuelDistributionPage />}
        </Suspense>
      </div>
    </div>
  );
}
