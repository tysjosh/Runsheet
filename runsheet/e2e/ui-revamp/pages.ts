/**
 * Shell pages the UI revamp measures (design.md §6). `chromeTask` /
 * `axeTask` name the task that brings each page under the 172 px budget / to
 * zero serious axe findings; until it lands, chrome.spec and axe.spec mark
 * the page `test.fail()` (flipped as pages land, removed by task 3.10).
 * Phase 2 flipped Dashboard, Dispatch (Board, Jobs, Plans), Orders and Live.
 */
export interface ShellPage {
  id: string;
  path: string;
  h1: string;
  /** Selector that proves the page's data rendered. */
  ready?: string;
  /** Task that finishes the page's chrome (null = met in Phase 1). */
  chromeTask: string | null;
  /**
   * Task that clears the page's axe critical/serious findings (null = clean
   * with the e2e fixtures in Phase 1).
   */
  axeTask: string | null;
  /**
   * False for pages with no list, table or board (not in design.md §6):
   * chrome.spec checks their title row only, not a first data row.
   */
  listPage?: boolean;
}

export const SHELL_PAGES: ShellPage[] = [
  {
    id: "dashboard",
    path: "/dashboard",
    h1: "Dashboard",
    ready: "[data-feed-row]",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "dispatch-board",
    path: "/dashboard/dispatch?tab=board",
    h1: "Dispatch",
    ready: '[role="grid"] [role="row"]',
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "dispatch-jobs",
    path: "/dashboard/dispatch?tab=jobs",
    h1: "Dispatch",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "dispatch-plans",
    path: "/dashboard/dispatch?tab=plans",
    ready: "tbody tr",
    h1: "Dispatch",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "orders",
    path: "/dashboard/orders",
    h1: "Orders",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "live",
    path: "/dashboard/control",
    ready: "[data-feed-row]",
    h1: "Live",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "fleet-trucks",
    path: "/dashboard/fleet?tab=trucks",
    h1: "Fleet",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "fleet-drivers",
    path: "/dashboard/fleet?tab=drivers",
    h1: "Fleet",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "fleet-drivers-qualifications",
    path: "/dashboard/fleet?tab=drivers&view=qualifications",
    h1: "Fleet",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "fleet-inventory",
    path: "/dashboard/fleet?tab=inventory",
    h1: "Fleet",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "customers",
    path: "/dashboard/customers",
    h1: "Customers",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "customers-communications",
    path: "/dashboard/customers?tab=communications",
    h1: "Customers",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "fuel-stations",
    path: "/dashboard/fuel-ops?tab=stations",
    h1: "Fuel",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "compliance-certifications",
    path: "/dashboard/compliance?tab=certifications",
    h1: "Compliance",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "compliance-meters",
    path: "/dashboard/compliance?tab=meters",
    h1: "Compliance",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "compliance-bols",
    path: "/dashboard/compliance?tab=bols",
    h1: "Compliance",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "compliance-ifta",
    path: "/dashboard/compliance?tab=ifta",
    h1: "Compliance",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "billing-invoices",
    path: "/dashboard/billing?tab=invoices",
    h1: "Billing",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "billing-accounts",
    path: "/dashboard/billing?tab=accounts",
    h1: "Billing",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "billing-payments",
    path: "/dashboard/billing?tab=payments",
    h1: "Billing",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "billing-ar-aging",
    path: "/dashboard/billing?tab=ar-aging",
    h1: "Billing",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "billing-reconciliation",
    path: "/dashboard/billing?tab=reconciliation",
    h1: "Billing",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "billing-price-books",
    path: "/dashboard/billing?tab=price-books",
    h1: "Billing",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "billing-pricing-rules",
    path: "/dashboard/billing?tab=pricing-rules",
    h1: "Billing",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "billing-contracts",
    path: "/dashboard/billing?tab=contracts",
    h1: "Billing",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "analytics",
    path: "/dashboard/analytics",
    h1: "Analytics",
    chromeTask: null,
    axeTask: null,
    listPage: false,
  },
  {
    id: "settings-depots",
    path: "/dashboard/settings?tab=company",
    h1: "Settings",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "settings-flags",
    path: "/dashboard/settings?tab=flags",
    h1: "Settings",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "settings-tax",
    path: "/dashboard/settings?tab=tax",
    h1: "Settings",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
  {
    id: "settings-exemptions",
    path: "/dashboard/settings?tab=exemptions",
    h1: "Settings",
    ready: "tbody tr",
    chromeTask: null,
    axeTask: null,
  },
];

export const VIEWPORTS = [
  { width: 1280, height: 800 },
  { width: 1440, height: 900 },
] as const;
