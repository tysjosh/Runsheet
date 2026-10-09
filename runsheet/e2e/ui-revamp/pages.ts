/**
 * Shell pages the UI revamp measures (design.md §6). Every page meets the
 * 172 px budget and has zero axe critical/serious findings, so nothing is
 * marked `test.fail()` any more (task 3.10 removed the marks).
 */
export interface ShellPage {
  id: string;
  path: string;
  h1: string;
  /** Selector that proves the page's data rendered. */
  ready?: string;
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
  },
  {
    id: "dispatch-board",
    path: "/dashboard/dispatch?tab=board",
    h1: "Dispatch",
    ready: '[role="grid"] [role="row"]',
  },
  {
    id: "dispatch-jobs",
    path: "/dashboard/dispatch?tab=jobs",
    h1: "Dispatch",
    ready: "tbody tr",
  },
  {
    id: "dispatch-plans",
    path: "/dashboard/dispatch?tab=plans",
    ready: "tbody tr",
    h1: "Dispatch",
  },
  {
    id: "orders",
    path: "/dashboard/orders",
    h1: "Orders",
    ready: "tbody tr",
  },
  {
    id: "live",
    path: "/dashboard/control",
    ready: "[data-feed-row]",
    h1: "Live",
  },
  {
    id: "fleet-trucks",
    path: "/dashboard/fleet?tab=trucks",
    h1: "Fleet",
    ready: "tbody tr",
  },
  {
    id: "fleet-drivers",
    path: "/dashboard/fleet?tab=drivers",
    h1: "Fleet",
    ready: "tbody tr",
  },
  {
    id: "fleet-drivers-qualifications",
    path: "/dashboard/fleet?tab=drivers&view=qualifications",
    h1: "Fleet",
    ready: "tbody tr",
  },
  {
    id: "fleet-inventory",
    path: "/dashboard/fleet?tab=inventory",
    h1: "Fleet",
    ready: "tbody tr",
  },
  {
    id: "customers",
    path: "/dashboard/customers",
    h1: "Customers",
    ready: "tbody tr",
  },
  {
    id: "customers-communications",
    path: "/dashboard/customers?tab=communications",
    h1: "Customers",
    ready: "tbody tr",
  },
  {
    id: "fuel-stations",
    path: "/dashboard/fuel-ops?tab=stations",
    h1: "Fuel",
    ready: "tbody tr",
  },
  {
    id: "compliance-certifications",
    path: "/dashboard/compliance?tab=certifications",
    h1: "Compliance",
    ready: "tbody tr",
  },
  {
    id: "compliance-meters",
    path: "/dashboard/compliance?tab=meters",
    h1: "Compliance",
    ready: "tbody tr",
  },
  {
    id: "compliance-bols",
    path: "/dashboard/compliance?tab=bols",
    h1: "Compliance",
    ready: "tbody tr",
  },
  {
    id: "compliance-ifta",
    path: "/dashboard/compliance?tab=ifta",
    h1: "Compliance",
    ready: "tbody tr",
  },
  {
    id: "billing-invoices",
    path: "/dashboard/billing?tab=invoices",
    h1: "Billing",
    ready: "tbody tr",
  },
  {
    id: "billing-accounts",
    path: "/dashboard/billing?tab=accounts",
    h1: "Billing",
    ready: "tbody tr",
  },
  {
    id: "billing-payments",
    path: "/dashboard/billing?tab=payments",
    h1: "Billing",
    ready: "tbody tr",
  },
  {
    id: "billing-ar-aging",
    path: "/dashboard/billing?tab=ar-aging",
    h1: "Billing",
    ready: "tbody tr",
  },
  {
    id: "billing-reconciliation",
    path: "/dashboard/billing?tab=reconciliation",
    h1: "Billing",
    ready: "tbody tr",
  },
  {
    id: "billing-price-books",
    path: "/dashboard/billing?tab=price-books",
    h1: "Billing",
    ready: "tbody tr",
  },
  {
    id: "billing-pricing-rules",
    path: "/dashboard/billing?tab=pricing-rules",
    h1: "Billing",
    ready: "tbody tr",
  },
  {
    id: "billing-contracts",
    path: "/dashboard/billing?tab=contracts",
    h1: "Billing",
    ready: "tbody tr",
  },
  {
    id: "analytics",
    path: "/dashboard/analytics",
    h1: "Analytics",
    listPage: false,
  },
  {
    id: "settings-depots",
    path: "/dashboard/settings?tab=company",
    h1: "Settings",
    ready: "tbody tr",
  },
  {
    id: "settings-flags",
    path: "/dashboard/settings?tab=flags",
    h1: "Settings",
    ready: "tbody tr",
  },
  {
    id: "settings-tax",
    path: "/dashboard/settings?tab=tax",
    h1: "Settings",
    ready: "tbody tr",
  },
  {
    id: "settings-exemptions",
    path: "/dashboard/settings?tab=exemptions",
    h1: "Settings",
    ready: "tbody tr",
  },
];

export const VIEWPORTS = [
  { width: 1280, height: 800 },
  { width: 1440, height: 900 },
] as const;
