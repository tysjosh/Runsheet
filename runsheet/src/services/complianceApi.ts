import { ApiError, ApiTimeoutError, fetchWithSession } from "./api";
import {
  buildQueryString,
  fetchWithTimeout,
  type PaginatedResponse,
  type SingleResponse,
} from "./utils";

// Re-export the shared pagination/response envelope types so existing
// downstream imports keep resolving them from this module (Req 2.4/4.3).
export type {
  PaginatedResponse,
  PaginationMeta,
  SingleResponse,
} from "./utils";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// ─── Shared Types ────────────────────────────────────────────────────────────

/** Envelope for endpoints that return a flat list with a ``count``. */
export interface CountResponse<T> {
  data: T[];
  count: number;
  request_id: string;
}

// ─── Tax Types ───────────────────────────────────────────────────────────────

export interface JurisdictionRate {
  jurisdiction_id: string;
  fips_code: string;
  jurisdiction_level: "federal" | "state" | "county" | "city";
  tax_type: "excise" | "ust" | "spcc" | "environmental";
  product_codes: string[];
  rate_cents_per_gallon: number;
  effective_date: string;
  expiry_date: string | null;
  tenant_id: string;
  created_at: string;
  updated_at: string;
}

export interface CreateJurisdictionRatePayload {
  fips_code: string;
  jurisdiction_level: "federal" | "state" | "county" | "city";
  tax_type: "excise" | "ust" | "spcc" | "environmental";
  product_codes: string[];
  rate_cents_per_gallon: number;
  effective_date: string;
  expiry_date?: string | null;
}

export interface TaxExemption {
  exemption_id: string;
  customer_id: string;
  exemption_type: string;
  certificate_number: string;
  expiry_date: string;
  tenant_id: string;
  created_at: string;
  updated_at: string;
}

export interface CreateTaxExemptionPayload {
  customer_id: string;
  exemption_type: string;
  certificate_number: string;
  expiry_date: string;
}

export interface TaxLineItem {
  tax_type: string;
  jurisdiction_level: string;
  fips_code: string;
  rate_cents_per_gallon: number;
  gallons: number;
  amount_cents: number;
}

export interface TaxBreakdown {
  federal_cents: number;
  state_cents: number;
  county_cents: number;
  city_cents: number;
  ust_cents: number;
  spcc_cents: number;
  environmental_cents: number;
  total_tax_cents: number;
  exemptions_applied: string[];
  line_items: TaxLineItem[];
}

export interface ComputeTaxPayload {
  product_code: string;
  net_gallons: number;
  destination_fips: string;
  customer_id: string;
}

// ─── Driver Qualification Types ──────────────────────────────────────────────

export type DriverStatus = "active" | "suspended" | "expired";

export interface Driver {
  driver_id: string;
  tenant_id: string;
  full_name: string;
  cdl_number: string;
  cdl_state: string;
  cdl_class: "A" | "B" | "C";
  cdl_expiry_date: string;
  medical_card_expiry_date: string;
  hazmat_endorsement_expiry_date: string | null;
  tanker_endorsement_expiry_date: string | null;
  last_drug_test_date: string | null;
  last_mvr_date: string | null;
  status: DriverStatus;
  created_at: string;
  updated_at: string;
}

export interface CreateDriverPayload {
  full_name: string;
  cdl_number: string;
  cdl_state: string;
  cdl_class: "A" | "B" | "C";
  cdl_expiry_date: string;
  medical_card_expiry_date: string;
  hazmat_endorsement_expiry_date?: string | null;
  tanker_endorsement_expiry_date?: string | null;
  last_drug_test_date?: string | null;
  last_mvr_date?: string | null;
}

export interface UpdateDriverPayload {
  full_name?: string;
  cdl_number?: string;
  cdl_state?: string;
  cdl_class?: "A" | "B" | "C";
  cdl_expiry_date?: string;
  medical_card_expiry_date?: string;
  hazmat_endorsement_expiry_date?: string | null;
  tanker_endorsement_expiry_date?: string | null;
  last_drug_test_date?: string | null;
  last_mvr_date?: string | null;
  status?: DriverStatus;
}

export interface DriverQualificationStatus {
  qualification_type: string; // cdl | medical_card | hazmat | tanker | drug_test
  expiry_date: string | null;
  days_until_expiry: number | null;
  alert_level: "ok" | "warning" | "urgent" | "critical" | "expired";
  status: string; // valid | expiring_soon | expired | overdue
}

export interface DQFDashboardEntry {
  driver_id: string;
  full_name: string;
  status: DriverStatus;
  qualifications: DriverQualificationStatus[];
}

export interface DQFDashboard {
  drivers: DQFDashboardEntry[];
  total_active: number;
  total_suspended: number;
  total_expiring_soon: number;
}

// ─── Asset Certification Types ───────────────────────────────────────────────

export type CertificationType =
  | "V_test"
  | "K_test"
  | "I_test"
  | "P_test"
  | "UT_test"
  | "meter_seal"
  | "fire_extinguisher";

export type CertificationStatus = "valid" | "expiring_soon" | "expired";

export interface AssetCertification {
  cert_id: string;
  tenant_id: string;
  asset_id: string;
  certification_type: CertificationType;
  certification_date: string;
  expiry_date: string;
  inspector_name: string;
  certificate_number: string;
  status: CertificationStatus;
  created_at: string;
  updated_at: string;
}

export interface CreateAssetCertificationPayload {
  asset_id: string;
  certification_type: CertificationType;
  certification_date: string;
  expiry_date: string;
  inspector_name: string;
  certificate_number: string;
}

/**
 * A single per-certification row in the fleet dashboard.
 *
 * Mirrors the backend ``CertificationSummary`` returned by
 * ``GET /compliance/asset-certifications/dashboard`` — note the backend
 * returns a **flat list of certifications** (one entry per cert), not a
 * per-asset aggregate. The page groups these by ``asset_id`` for the
 * table view (see {@link AssetCertificationSummary}).
 */
export interface FleetCertificationEntry {
  asset_id: string;
  certification_type: CertificationType;
  cert_id: string;
  certification_date: string;
  expiry_date: string;
  status: CertificationStatus;
  days_until_expiry: number;
  inspector_name: string;
  certificate_number: string;
}

export interface AssetCertificationDashboard {
  /** Flat, per-certification rows as returned by the backend. */
  assets: FleetCertificationEntry[];
  total_valid: number;
  total_expiring_soon: number;
  total_expired: number;
}

// ─── Meter Audit Types ───────────────────────────────────────────────────────

export interface MeterRegistration {
  meter_id: string;
  meter_number: string;
  truck_id: string;
  calibration_certificate_number: string;
  calibration_date: string;
  calibration_expiry_date: string;
  weights_measures_authority: string;
  tenant_id: string;
  created_at: string;
  updated_at: string;
}

export interface CreateMeterPayload {
  meter_number: string;
  truck_id: string;
  calibration_certificate_number: string;
  calibration_date: string;
  calibration_expiry_date: string;
  weights_measures_authority: string;
}

export interface MeterAuditEntry {
  audit_id: string;
  meter_id: string;
  delivery_id: string;
  invoice_id: string;
  gross_gallons: number;
  net_gallons: number;
  variance_flag: string | null;
  timestamp: string;
  tenant_id: string;
}

// ─── Terminal BOL Types ──────────────────────────────────────────────────────

export type TerminalBOLStatus = "ingested" | "pending_confirmation" | "linked";

export interface TerminalBOL {
  bol_id: string;
  load_number: string;
  product_code: string;
  gross_gallons: number;
  net_gallons: number;
  temperature_f: number;
  api_gravity: number;
  supplier_name: string;
  terminal_name: string;
  driver_id: string;
  timestamp: string;
  status: TerminalBOLStatus;
  tenant_id: string;
  created_at: string;
  updated_at: string;
}

export interface IngestTerminalBOLPayload {
  edi_payload: string;
}

// ─── Price Protection Types ──────────────────────────────────────────────────

export type ContractType = "fixed_price" | "cap_price" | "collar";
export type ContractStatus = "active" | "exhausted" | "expired";

export interface PriceProtectionContract {
  contract_id: string;
  tenant_id: string;
  customer_id: string;
  account_id: string;
  product_code: string;
  contract_type: ContractType;
  start_date: string;
  end_date: string;
  contracted_gallons: number;
  remaining_gallons: number;
  price_cap_cents: number | null;
  price_floor_cents: number | null;
  fixed_price_cents: number | null;
  status: ContractStatus;
  created_at: string;
  updated_at: string;
}

export interface CreatePriceProtectionContractPayload {
  customer_id: string;
  account_id: string;
  product_code: string;
  contract_type: ContractType;
  start_date: string;
  end_date: string;
  contracted_gallons: number;
  price_cap_cents?: number | null;
  price_floor_cents?: number | null;
  fixed_price_cents?: number | null;
}

export interface UpdatePriceProtectionContractPayload {
  end_date?: string;
  price_cap_cents?: number | null;
  price_floor_cents?: number | null;
  fixed_price_cents?: number | null;
  status?: ContractStatus;
}

// ─── Pricing Rules Types ─────────────────────────────────────────────────────

export type PricingStrategy =
  | "posted_price"
  | "rack_plus_margin"
  | "tiered_volume"
  | "cost_plus";

export interface TierBreak {
  min_gallons: number;
  max_gallons: number | null;
  price_cents: number;
}

export interface PricingRule {
  rule_id: string;
  tenant_id: string;
  customer_id: string | null;
  account_id: string | null;
  product_code: string;
  strategy: PricingStrategy;
  margin_cents: number | null;
  posted_price_cents: number | null;
  tier_thresholds: TierBreak[] | null;
  freight_rate_cents_per_mile: number | null;
  priority: number;
  effective_date: string;
  expiry_date: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreatePricingRulePayload {
  customer_id?: string | null;
  account_id?: string | null;
  product_code: string;
  strategy: PricingStrategy;
  margin_cents?: number | null;
  posted_price_cents?: number | null;
  tier_thresholds?: TierBreak[] | null;
  freight_rate_cents_per_mile?: number | null;
  priority: number;
  effective_date: string;
  expiry_date?: string | null;
}

export interface PriceResolution {
  resolved_price_cents: number;
  strategy_used: PricingStrategy;
  rule_id: string;
  breakdown: Record<string, number>;
}

export interface ResolvePricePayload {
  customer_id: string;
  product_code: string;
  gallons: number;
  terminal_id?: string;
  route_miles?: number;
}

// ─── IFTA Types ──────────────────────────────────────────────────────────────

export interface IFTAJurisdictionEntry {
  jurisdiction: string;
  total_miles: number;
  taxable_miles: number;
  tax_paid_gallons: number;
  net_taxable_gallons: number;
  tax_rate: number;
  tax_due: number;
}

export interface IFTATruckSummary {
  truck_id: string;
  truck_name: string;
  jurisdictions: IFTAJurisdictionEntry[];
  total_miles: number;
  total_gallons: number;
  /** Per-truck average MPG; null/absent when the truck has no fuel data. */
  fleet_mpg: number | null;
}

export interface IFTAReport {
  tenant_id: string;
  quarter: string;
  trucks: IFTATruckSummary[];
  /** Fleet average MPG; null when the quarter has no fuel gallons recorded. */
  fleet_mpg: number | null;
  /** Trucks flagged as ifta_data_incomplete and excluded from the report. */
  incomplete_trucks: IFTAIncompleteDataFlag[];
  generated_at: string;
}

export interface IFTAReportFilters {
  quarter: string;
  truck_id?: string;
}

// ─── K-Factor Calibration Types ──────────────────────────────────────────────

/**
 * A single tank's K-factor calibration status.
 *
 * Mirrors the backend ``KFactorEntry`` returned by
 * ``GET /compliance/kfactor/dashboard`` (a flat array under ``data``).
 * The dashboard derives a display ``status`` from ``read_only`` /
 * ``suggested_kfactor`` client-side.
 */
export interface KFactorEntry {
  tank_id: string;
  customer_id: string;
  current_kfactor: number;
  suggested_kfactor: number | null;
  variance_percent: number | null;
  last_delivery_date: string | null;
  delivery_count: number;
  read_only: boolean;
  read_only_reason: string | null;
}

export interface ApproveKFactorPayload {
  new_kfactor: number;
  operator_id: string;
}

// ─── HTTP Helper ─────────────────────────────────────────────────────────────

async function complianceRequest<T>(
  endpoint: string,
  options?: RequestInit,
): Promise<T> {
  const url = `${API_BASE_URL}${endpoint}`;
  try {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(options?.headers as Record<string, string> | undefined),
    };

    // Session cookie + anti-CSRF token are attached by the SuperTokens SDK;
    // an auth failure triggers a refresh-then-retry, else a redirect to
    // sign-in (Req 8.4, 8.5).
    const response = await fetchWithSession(fetchWithTimeout, url, {
      ...options,
      headers,
    });

    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(
        body.detail || body.message || `HTTP error! status: ${response.status}`,
        response.status,
      );
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiTimeoutError || error instanceof ApiError) {
      throw error;
    }
    throw new ApiError(
      error instanceof Error ? error.message : "Unknown error",
      0,
    );
  }
}

// ─── Tax Endpoints ───────────────────────────────────────────────────────────

/** GET /compliance/tax-jurisdictions — list jurisdiction rates */
export async function getTaxJurisdictions(
  filters: {
    fips_code?: string;
    tax_type?: string;
    page?: number;
    size?: number;
  } = {},
): Promise<PaginatedResponse<JurisdictionRate>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return complianceRequest<PaginatedResponse<JurisdictionRate>>(
    `/compliance/tax-jurisdictions${qs}`,
  );
}

/** POST /compliance/tax-jurisdictions — create a jurisdiction rate */
export async function createTaxJurisdiction(
  payload: CreateJurisdictionRatePayload,
): Promise<SingleResponse<JurisdictionRate>> {
  return complianceRequest<SingleResponse<JurisdictionRate>>(
    "/compliance/tax-jurisdictions",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** POST /compliance/tax/compute — compute tax for a delivery */
export async function computeTax(
  payload: ComputeTaxPayload,
): Promise<SingleResponse<TaxBreakdown>> {
  return complianceRequest<SingleResponse<TaxBreakdown>>(
    "/compliance/tax/compute",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** GET /compliance/exemptions — list tax exemptions */
export async function getTaxExemptions(
  filters: {
    customer_id?: string;
    exemption_type?: string;
    page?: number;
    size?: number;
  } = {},
): Promise<PaginatedResponse<TaxExemption>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return complianceRequest<PaginatedResponse<TaxExemption>>(
    `/compliance/exemptions${qs}`,
  );
}

/** POST /compliance/exemptions — create a tax exemption */
export async function createTaxExemption(
  payload: CreateTaxExemptionPayload,
): Promise<SingleResponse<TaxExemption>> {
  return complianceRequest<SingleResponse<TaxExemption>>(
    "/compliance/exemptions",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

// ─── Driver Qualification Endpoints ──────────────────────────────────────────

/** GET /compliance/drivers — list all drivers */
export async function getDrivers(
  filters: { status?: DriverStatus; page?: number; size?: number } = {},
): Promise<PaginatedResponse<Driver>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return complianceRequest<PaginatedResponse<Driver>>(
    `/compliance/drivers${qs}`,
  );
}

/** GET /compliance/drivers/:id — get a single driver */
export async function getDriver(
  driverId: string,
): Promise<SingleResponse<Driver>> {
  return complianceRequest<SingleResponse<Driver>>(
    `/compliance/drivers/${encodeURIComponent(driverId)}`,
  );
}

/** POST /compliance/drivers — create a new driver */
export async function createDriver(
  payload: CreateDriverPayload,
): Promise<SingleResponse<Driver>> {
  return complianceRequest<SingleResponse<Driver>>("/compliance/drivers", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** PUT /compliance/drivers/:id — update a driver */
export async function updateDriver(
  driverId: string,
  payload: UpdateDriverPayload,
): Promise<SingleResponse<Driver>> {
  return complianceRequest<SingleResponse<Driver>>(
    `/compliance/drivers/${encodeURIComponent(driverId)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
  );
}

/** GET /compliance/drivers/dashboard — DQF compliance dashboard */
export async function getDriversDashboard(): Promise<
  SingleResponse<DQFDashboard>
> {
  return complianceRequest<SingleResponse<DQFDashboard>>(
    "/compliance/drivers/dashboard",
  );
}

// ─── Asset Certification Endpoints ───────────────────────────────────────────

/** GET /compliance/asset-certifications — list certifications */
export async function getAssetCertifications(
  filters: {
    asset_id?: string;
    certification_type?: CertificationType;
    status?: CertificationStatus;
    page?: number;
    size?: number;
  } = {},
): Promise<PaginatedResponse<AssetCertification>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return complianceRequest<PaginatedResponse<AssetCertification>>(
    `/compliance/asset-certifications${qs}`,
  );
}

/** POST /compliance/asset-certifications — create a certification */
export async function createAssetCertification(
  payload: CreateAssetCertificationPayload,
): Promise<SingleResponse<AssetCertification>> {
  return complianceRequest<SingleResponse<AssetCertification>>(
    "/compliance/asset-certifications",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** GET /compliance/asset-certifications/dashboard — fleet cert dashboard */
export async function getAssetCertificationsDashboard(): Promise<
  SingleResponse<AssetCertificationDashboard>
> {
  return complianceRequest<SingleResponse<AssetCertificationDashboard>>(
    "/compliance/asset-certifications/dashboard",
  );
}

// ─── Meter Audit Endpoints ───────────────────────────────────────────────────

/** GET /compliance/meters — list registered meters */
export async function getMeters(
  filters: { truck_id?: string; page?: number; size?: number } = {},
): Promise<PaginatedResponse<MeterRegistration>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return complianceRequest<PaginatedResponse<MeterRegistration>>(
    `/compliance/meters${qs}`,
  );
}

/** POST /compliance/meters — register a new meter */
export async function createMeter(
  payload: CreateMeterPayload,
): Promise<SingleResponse<MeterRegistration>> {
  return complianceRequest<SingleResponse<MeterRegistration>>(
    "/compliance/meters",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** GET /compliance/meters/:id/audit-trail — per-meter audit trail */
export async function getMeterAuditTrail(
  meterId: string,
  filters: { page?: number; size?: number } = {},
): Promise<PaginatedResponse<MeterAuditEntry>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return complianceRequest<PaginatedResponse<MeterAuditEntry>>(
    `/compliance/meters/${encodeURIComponent(meterId)}/audit-trail${qs}`,
  );
}

// ─── Terminal BOL Endpoints ──────────────────────────────────────────────────

/** GET /compliance/terminal-bols — list terminal BOLs */
export async function getTerminalBOLs(
  filters: {
    status?: TerminalBOLStatus;
    product_code?: string;
    driver_id?: string;
    page?: number;
    size?: number;
  } = {},
): Promise<PaginatedResponse<TerminalBOL>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return complianceRequest<PaginatedResponse<TerminalBOL>>(
    `/compliance/terminal-bols${qs}`,
  );
}

/** POST /compliance/terminal-bols — ingest a terminal BOL via EDI */
export async function ingestTerminalBOL(
  payload: IngestTerminalBOLPayload,
): Promise<SingleResponse<TerminalBOL>> {
  return complianceRequest<SingleResponse<TerminalBOL>>(
    "/compliance/terminal-bols",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** POST /compliance/terminal-bols/upload — upload a terminal BOL manually (PDF/image) */
export async function uploadTerminalBOL(
  file: File,
): Promise<SingleResponse<TerminalBOL>> {
  const formData = new FormData();
  formData.append("file", file);

  const url = `${API_BASE_URL}/compliance/terminal-bols/upload`;
  try {
    const response = await fetchWithTimeout(url, {
      method: "POST",
      body: formData,
      // Do not set Content-Type — let browser set multipart boundary
    });

    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(
        body.detail || body.message || `HTTP error! status: ${response.status}`,
        response.status,
      );
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiTimeoutError || error instanceof ApiError) {
      throw error;
    }
    throw new ApiError(
      error instanceof Error ? error.message : "Unknown error",
      0,
    );
  }
}

// ─── Price Protection Endpoints ──────────────────────────────────────────────

/** GET /commerce/price-protection-contracts — list contracts */
export async function getPriceProtectionContracts(
  filters: {
    customer_id?: string;
    status?: ContractStatus;
    page?: number;
    size?: number;
  } = {},
): Promise<PaginatedResponse<PriceProtectionContract>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return complianceRequest<PaginatedResponse<PriceProtectionContract>>(
    `/commerce/price-protection-contracts${qs}`,
  );
}

/** POST /commerce/price-protection-contracts — create a contract */
export async function createPriceProtectionContract(
  payload: CreatePriceProtectionContractPayload,
): Promise<SingleResponse<PriceProtectionContract>> {
  return complianceRequest<SingleResponse<PriceProtectionContract>>(
    "/commerce/price-protection-contracts",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** PUT /commerce/price-protection-contracts/:id — update a contract */
export async function updatePriceProtectionContract(
  contractId: string,
  payload: UpdatePriceProtectionContractPayload,
): Promise<SingleResponse<PriceProtectionContract>> {
  return complianceRequest<SingleResponse<PriceProtectionContract>>(
    `/commerce/price-protection-contracts/${encodeURIComponent(contractId)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
  );
}

// ─── Pricing Rules Endpoints ─────────────────────────────────────────────────

/** GET /commerce/pricing-rules — list pricing rules */
export async function getPricingRules(
  filters: {
    customer_id?: string;
    product_code?: string;
    strategy?: PricingStrategy;
    page?: number;
    size?: number;
  } = {},
): Promise<PaginatedResponse<PricingRule>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return complianceRequest<PaginatedResponse<PricingRule>>(
    `/commerce/pricing-rules${qs}`,
  );
}

/** POST /commerce/pricing-rules — create a pricing rule */
export async function createPricingRule(
  payload: CreatePricingRulePayload,
): Promise<SingleResponse<PricingRule>> {
  return complianceRequest<SingleResponse<PricingRule>>(
    "/commerce/pricing-rules",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** POST /commerce/pricing/resolve — resolve price for a delivery */
export async function resolvePrice(
  payload: ResolvePricePayload,
): Promise<SingleResponse<PriceResolution>> {
  return complianceRequest<SingleResponse<PriceResolution>>(
    "/commerce/pricing/resolve",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

// ─── IFTA Endpoints ──────────────────────────────────────────────────────────

export interface CreateMileageAdjustmentPayload {
  truck_id: string;
  jurisdiction: string;
  miles: number;
  quarter: string;
  reason: string;
}

export interface MileageAdjustment {
  adjustment_id: string;
  tenant_id: string;
  truck_id: string;
  jurisdiction: string;
  miles: number;
  quarter: string;
  operator_id: string;
  reason: string;
  created_at: string;
}

/** GET /compliance/ifta/report — get IFTA quarterly report */
export async function getIFTAReport(
  filters: IFTAReportFilters,
): Promise<SingleResponse<IFTAReport>> {
  const qs = buildQueryString(
    filters as unknown as Record<string, string | number | boolean | undefined>,
  );
  return complianceRequest<SingleResponse<IFTAReport>>(
    `/compliance/ifta/report${qs}`,
  );
}

/** POST /compliance/ifta/adjustments — record a manual mileage adjustment */
export async function createMileageAdjustment(
  payload: CreateMileageAdjustmentPayload,
): Promise<SingleResponse<MileageAdjustment>> {
  return complianceRequest<SingleResponse<MileageAdjustment>>(
    "/compliance/ifta/adjustments",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

// ─── K-Factor Calibration Endpoints ──────────────────────────────────────────

/**
 * GET /compliance/kfactor/dashboard — K-factor calibration dashboard.
 *
 * Returns a flat array of per-tank {@link KFactorEntry} rows under
 * ``data`` (plus a ``count``), NOT a wrapped ``{ entries, totals }``
 * object. The page derives summary counts and a display status
 * client-side.
 */
export async function getKFactorDashboard(): Promise<
  CountResponse<KFactorEntry>
> {
  return complianceRequest<CountResponse<KFactorEntry>>(
    "/compliance/kfactor/dashboard",
  );
}

/** POST /compliance/kfactor/:tankId/approve — approve a K-factor adjustment */
export async function approveKFactorAdjustment(
  tankId: string,
  payload: ApproveKFactorPayload,
): Promise<
  SingleResponse<{
    tank_id: string;
    old_kfactor: number;
    new_kfactor: number;
    approved_at: string;
  }>
> {
  return complianceRequest<
    SingleResponse<{
      tank_id: string;
      old_kfactor: number;
      new_kfactor: number;
      approved_at: string;
    }>
  >(`/compliance/kfactor/${encodeURIComponent(tankId)}/approve`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ─── K-Factor Variance & Suggestion ──────────────────────────────────────────

export interface KFactorVarianceResult {
  delivery_id: string;
  tank_id: string;
  /** K-factor × accumulated HDD since the last delivery. */
  predicted_gallons: number;
  /** Actual gallons delivered. */
  actual_gallons: number;
  /** (actual − predicted) / predicted × 100. */
  variance_percent: number;
  /** Suggested revised K-factor when variance exceeds the threshold. */
  suggested_kfactor: number | null;
  /** True when variance exceeds the configured threshold (default ±15%). */
  flagged: boolean;
}

export interface KFactorSuggestion {
  tank_id: string;
  /** null when insufficient data exists or variance is within threshold. */
  suggested_kfactor: number | null;
}

/**
 * GET /compliance/kfactor/:tankId/variance — compute predicted-vs-actual
 * variance for a specific delivery. ``deliveryId`` is required and passed
 * as the ``delivery_id`` query parameter.
 */
export async function getKFactorVariance(
  tankId: string,
  deliveryId: string,
): Promise<SingleResponse<KFactorVarianceResult>> {
  const qs = buildQueryString({ delivery_id: deliveryId });
  return complianceRequest<SingleResponse<KFactorVarianceResult>>(
    `/compliance/kfactor/${encodeURIComponent(tankId)}/variance${qs}`,
  );
}

/**
 * GET /compliance/kfactor/:tankId/suggest — get the suggested K-factor for a
 * tank. Returns ``suggested_kfactor: null`` when there are fewer than three
 * deliveries or the variance is within threshold.
 */
export async function getKFactorSuggestion(
  tankId: string,
): Promise<SingleResponse<KFactorSuggestion>> {
  return complianceRequest<SingleResponse<KFactorSuggestion>>(
    `/compliance/kfactor/${encodeURIComponent(tankId)}/suggest`,
  );
}

// ─── IFTA Completeness & Fleet MPG ───────────────────────────────────────────

export interface IFTAIncompleteDataFlag {
  truck_id: string;
  flag_type: string;
  quarter: string;
  reason: string;
  flagged_at: string;
}

export interface IFTACompletenessResult {
  data: IFTAIncompleteDataFlag[];
  count: number;
  /** True when no trucks are flagged for the quarter. */
  complete: boolean;
  request_id: string;
}

export interface FleetMPGResult {
  quarter: string;
  /** Fleet average MPG (total_miles / total_gallons); 0.0 when no data. */
  fleet_mpg: number;
}

/**
 * GET /compliance/ifta/completeness?quarter= — list trucks missing Geotab
 * telemetry for the quarter (and therefore excluded from the automated IFTA
 * return). Unlike the single/paginated envelopes, this response carries
 * ``count`` and ``complete`` flags alongside ``data``.
 */
export async function getIFTACompleteness(
  quarter: string,
): Promise<IFTACompletenessResult> {
  const qs = buildQueryString({ quarter });
  return complianceRequest<IFTACompletenessResult>(
    `/compliance/ifta/completeness${qs}`,
  );
}

/** GET /compliance/ifta/fleet-mpg?quarter= — fleet average MPG for a quarter. */
export async function getFleetMPG(
  quarter: string,
): Promise<SingleResponse<FleetMPGResult>> {
  const qs = buildQueryString({ quarter });
  return complianceRequest<SingleResponse<FleetMPGResult>>(
    `/compliance/ifta/fleet-mpg${qs}`,
  );
}

// ─── Single Asset Certification (read / update) ──────────────────────────────

export interface UpdateAssetCertificationPayload {
  certification_date?: string;
  expiry_date?: string;
  inspector_name?: string;
  certificate_number?: string;
  status?: CertificationStatus;
}

/** GET /compliance/asset-certifications/:certId — fetch a single certification. */
export async function getAssetCertification(
  certId: string,
): Promise<SingleResponse<AssetCertification>> {
  return complianceRequest<SingleResponse<AssetCertification>>(
    `/compliance/asset-certifications/${encodeURIComponent(certId)}`,
  );
}

/** PUT /compliance/asset-certifications/:certId — update a certification. */
export async function updateAssetCertification(
  certId: string,
  payload: UpdateAssetCertificationPayload,
): Promise<SingleResponse<AssetCertification>> {
  return complianceRequest<SingleResponse<AssetCertification>>(
    `/compliance/asset-certifications/${encodeURIComponent(certId)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
  );
}

// ─── Price Protection Contract Variance ──────────────────────────────────────

export interface PriceProtectionVarianceBreakdownItem {
  delivery_id: string;
  market_price_cents: number;
  effective_price_cents: number;
  gallons: number;
  variance_cents: number;
}

export interface PriceProtectionVarianceReport {
  contract_id: string;
  /** Signed from the customer's perspective: positive = customer saved money. */
  total_variance_cents: number;
  total_gallons: number;
  delivery_count: number;
  breakdown: PriceProtectionVarianceBreakdownItem[];
  /** Full contract document, enriched server-side for the portfolio row. */
  contract: PriceProtectionContract;
}

/**
 * GET /commerce/price-protection-contracts/:contractId/variance — portfolio
 * variance report for a single contract. Returns 404 when the contract id is
 * unknown to the tenant.
 */
export async function getPriceProtectionVariance(
  contractId: string,
): Promise<SingleResponse<PriceProtectionVarianceReport>> {
  return complianceRequest<SingleResponse<PriceProtectionVarianceReport>>(
    `/commerce/price-protection-contracts/${encodeURIComponent(contractId)}/variance`,
  );
}

// ─── Terminal BOL Confirm / Link ──────────────────────────────────────────────

/**
 * Operator-confirmed fields for a pending manual BOL. Every field is
 * optional — only the fields provided are applied, then the BOL transitions
 * from ``pending_confirmation`` to ``ingested``.
 */
export interface ConfirmTerminalBOLPayload {
  load_number?: string;
  product_code?: string;
  gross_gallons?: number;
  net_gallons?: number;
  observed_temperature_f?: number;
  api_gravity?: number;
  supplier_name?: string;
  terminal_name?: string;
  driver_id?: string;
  /** ISO 8601 timestamp the BOL was issued at the terminal. */
  timestamp?: string;
}

/**
 * POST /compliance/terminal-bols/:bolId/confirm — confirm operator-reviewed
 * fields on a pending manual BOL and mark it ``ingested``.
 */
export async function confirmTerminalBOL(
  bolId: string,
  payload: ConfirmTerminalBOLPayload = {},
): Promise<SingleResponse<TerminalBOL>> {
  return complianceRequest<SingleResponse<TerminalBOL>>(
    `/compliance/terminal-bols/${encodeURIComponent(bolId)}/confirm`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** Summary returned by the BOL-link endpoint (not a full BOL document). */
export interface LinkTerminalBOLResult {
  bol_id: string;
  load_plan_id: string;
  status: TerminalBOLStatus;
}

/**
 * POST /compliance/terminal-bols/:bolId/link — link a BOL to a load plan for
 * chain-of-custody traceability (transitions ``ingested`` → ``linked``).
 * Returns a small ``{ bol_id, load_plan_id, status }`` summary rather than
 * the full BOL document.
 */
export async function linkTerminalBOL(
  bolId: string,
  payload: { load_plan_id: string },
): Promise<SingleResponse<LinkTerminalBOLResult>> {
  return complianceRequest<SingleResponse<LinkTerminalBOLResult>>(
    `/compliance/terminal-bols/${encodeURIComponent(bolId)}/link`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}
