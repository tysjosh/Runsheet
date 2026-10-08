/**
 * URL-synced tabs in every hub (task 1.9, R2.4): `?tab=` selects the tab on
 * load (so refresh and shared links keep it), legacy values alias, and a
 * change goes through `router.replace`. Panels are mocked; DispatchPage has
 * its own suite.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";

const mockReplace = jest.fn();
let mockParams = new URLSearchParams();
let mockPath = "/dashboard";
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: jest.fn() }),
  usePathname: () => mockPath,
  useSearchParams: () => mockParams,
}));
jest.mock("../utils/auth", () => ({
  getCurrentUserRoles: jest
    .fn()
    .mockResolvedValue(["admin", "dispatcher", "platform_admin"]),
}));

// A function declaration (hoisted) so eager imports can use it inside the
// hoisted jest.mock factories.
function mockStub(name: string) {
  return { __esModule: true, default: () => <div>{name} panel</div> };
}
jest.mock("./compliance/AssetCertificationsPage", () =>
  mockStub("Certifications"),
);
jest.mock("./compliance/MeterAuditPage", () => mockStub("Meters"));
jest.mock("./compliance/TerminalBOLsPage", () => mockStub("BOLs"));
jest.mock("./compliance/IFTAReportPage", () => mockStub("IFTA"));
jest.mock("./ops/FuelDashboardView", () => ({
  __esModule: true,
  default: ({ view }: { view: string }) => <div>Fuel dashboard {view}</div>,
}));
jest.mock("./ops/SourcingPage", () => mockStub("Sourcing"));
jest.mock("./compliance/KFactorCalibrationPage", () => mockStub("K-Factor"));
jest.mock("./Analytics", () => mockStub("Overview"));
jest.mock("./ops/SchedulingMetricsPage", () => mockStub("Scheduling metrics"));
jest.mock("./ops/FuelEfficiencyChart", () => mockStub("Efficiency"));
jest.mock("./ops/OpsMonitoringDashboard", () => mockStub("Ops monitoring"));
jest.mock("./commerce/AccountsListPage", () => mockStub("Accounts"));
jest.mock("./commerce/InvoicesListPage", () => mockStub("Invoices"));
jest.mock("./commerce/PaymentsListPage", () => mockStub("Payments"));
jest.mock("./commerce/PriceBookEditor", () => mockStub("Price books"));
jest.mock("./commerce/ARAgingDashboard", () => mockStub("AR aging"));
jest.mock("./ops/ReconciliationPage", () => mockStub("Reconciliation"));
jest.mock("./commerce/margin/MarginHub", () => mockStub("Margin"));
jest.mock("./compliance/PriceProtectionContractsPage", () =>
  mockStub("Contracts"),
);
jest.mock("./compliance/PricingRulesPage", () => mockStub("Pricing rules"));
jest.mock("../services/marginApi", () => ({
  getOpenMarginAlertCount: jest.fn().mockResolvedValue(0),
}));
jest.mock("./FleetTracking", () => mockStub("Tracking"));
jest.mock("./Inventory", () => mockStub("Inventory"));
jest.mock("./drivers/DriverUtilizationView", () => mockStub("Utilization"));
jest.mock("./drivers/DriverQualificationsView", () =>
  mockStub("Qualifications"),
);
jest.mock("./compliance/ExpiryAlertWidget", () => mockStub("Expiry"));
jest.mock("./commerce/CustomersListPage", () => mockStub("Customer list"));
jest.mock("./NotificationHistoryTab", () => mockStub("Communications"));
jest.mock("./admin/DepotsPage", () => mockStub("Depots"));
jest.mock("./admin/RoadRestrictionsPanel", () => mockStub("Road restrictions"));
jest.mock("./admin/WeatherAlertsPage", () => mockStub("Weather alerts"));
jest.mock("./compliance/TaxJurisdictionsPage", () => mockStub("Tax"));
jest.mock("./compliance/ExemptionsPage", () => mockStub("Exemptions"));
jest.mock("./NotificationSettingsTab", () => mockStub("Notification rules"));
jest.mock("./admin/IntegrationMarketplacePage", () => mockStub("Marketplace"));
jest.mock("./admin/IntakeChannelsAdminPanel", () =>
  mockStub("Intake channels"),
);
jest.mock("./admin/StripeIntegrationUI", () => mockStub("Stripe"));
jest.mock("./ops/AgentSettingsPage", () => mockStub("Agent settings"));
jest.mock("./admin/AgentMonitoringDashboard", () =>
  mockStub("Agent monitoring"),
);
jest.mock("./DataImport", () => mockStub("Data import"));
jest.mock("./admin/FeatureFlagsAdmin", () => mockStub("Feature flags"));
jest.mock("./admin/NotificationMetricsDashboard", () => mockStub("Metrics"));

import AnalyticsHub from "./AnalyticsHub";
import CommerceHub from "./CommerceHub";
import ComplianceHub from "./ComplianceHub";
import CustomersHub from "./customers/CustomersHub";
import FleetDashboard from "./FleetDashboard";
import FuelOpsPage from "./FuelOpsPage";
import SettingsPage from "./settings/SettingsPage";

beforeEach(() => {
  mockReplace.mockClear();
});

async function open(ui: React.ReactElement, path: string, query: string) {
  mockPath = path;
  mockParams = new URLSearchParams(query);
  await act(async () => {
    render(ui);
  });
}

const HUBS: [
  string,
  () => React.ReactElement,
  string,
  string,
  string,
  string,
][] = [
  // [name, element, path, ?query, selected tab name, panel text]
  [
    "Compliance",
    () => <ComplianceHub />,
    "/dashboard/compliance",
    "tab=bols",
    "BOLs",
    "BOLs panel",
  ],
  [
    "Fuel",
    () => <FuelOpsPage />,
    "/dashboard/fuel-ops",
    "tab=sourcing",
    "Sourcing",
    "Sourcing panel",
  ],
  [
    "Fuel (alias)",
    () => <FuelOpsPage />,
    "/dashboard/fuel-ops",
    "tab=consumption",
    "Consumption",
    "Fuel dashboard efficiency",
  ],
  [
    "Analytics",
    () => <AnalyticsHub />,
    "/dashboard/analytics",
    "tab=fleet-efficiency",
    "Fleet efficiency",
    "Efficiency panel",
  ],
  [
    "Billing",
    () => <CommerceHub />,
    "/dashboard/billing",
    "tab=reconciliation",
    "Reconciliation",
    "Reconciliation panel",
  ],
  [
    "Fleet",
    () => (
      <FleetDashboard
        selectedTruck={null}
        onTruckSelect={() => {}}
        mapView={null}
      />
    ),
    "/dashboard/fleet",
    "tab=inventory",
    "Inventory",
    "Inventory panel",
  ],
  [
    "Fleet (legacy assets)",
    () => (
      <FleetDashboard
        selectedTruck={null}
        onTruckSelect={() => {}}
        mapView={null}
      />
    ),
    "/dashboard/fleet",
    "tab=assets",
    "Trucks",
    "Tracking panel",
  ],
  [
    "Customers",
    () => <CustomersHub onSelectCustomer={() => {}} />,
    "/dashboard/customers",
    "tab=communications",
    "Communications",
    "Communications panel",
  ],
];

describe.each(HUBS)("%s hub", (_name, ui, path, query, tabName, panel) => {
  it(`restores ?${query} on load`, async () => {
    await open(ui(), path, query);
    expect(await screen.findByText(panel)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: tabName })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(document.querySelectorAll("h1")).toHaveLength(1);
  });

  it("writes a tab change with router.replace", async () => {
    await open(ui(), path, query);
    await screen.findByText(panel);
    const other = screen
      .getAllByRole("tab")
      .find((t) => t.getAttribute("aria-selected") !== "true") as HTMLElement;
    fireEvent.click(other);
    expect(mockReplace).toHaveBeenCalledWith(
      expect.stringMatching(new RegExp(`^${path}\\?tab=`)),
      { scroll: false },
    );
  });
});

describe("Fleet → Drivers", () => {
  it("hosts the driver views as a ?view= control in Fleet's title row", async () => {
    await open(
      <FleetDashboard
        selectedTruck={null}
        onTruckSelect={() => {}}
        mapView={null}
      />,
      "/dashboard/fleet",
      "tab=drivers&view=qualifications",
    );
    expect(await screen.findByText("Qualifications panel")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Qualifications" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(document.querySelectorAll("h1")).toHaveLength(1);
  });
});

describe("Settings", () => {
  it.each([
    ["company", "Depots panel"],
    ["depots", "Depots panel"],
    ["weather-alerts", "Weather alerts panel"],
    ["integrations", "Marketplace panel"],
    ["feature-flags", "Feature flags panel"],
    ["notifications", "Notification rules panel"],
  ])("?tab=%s opens its section", async (tab, panel) => {
    await open(<SettingsPage />, "/dashboard/settings", `tab=${tab}`);
    expect(await screen.findByText(panel)).toBeInTheDocument();
    expect(document.querySelectorAll("h1")).toHaveLength(1);
  });

  it("navigates sections with router.replace", async () => {
    await open(<SettingsPage />, "/dashboard/settings", "");
    expect(await screen.findByText("Depots panel")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Data import" }));
    expect(mockReplace).toHaveBeenCalledWith("/dashboard/settings?tab=import", {
      scroll: false,
    });
  });
});
