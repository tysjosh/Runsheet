// API Response Types
export interface ApiResponse<T> {
  data: T;
  success: boolean;
  message?: string;
  timestamp: string;
}

// Fleet Types

export interface Location {
  id: string;
  name: string;
  type: "station" | "warehouse" | "depot";
  coordinates: {
    lat: number;
    lon: number;
  };
  address: string;
}

export interface Route {
  id: string;
  origin: Location;
  destination: Location;
  waypoints: Location[];
  distance: number; // in km
  estimatedDuration: number; // in minutes
  actualDuration?: number;
}

export interface CargoInfo {
  type: string;
  weight: number;
  volume: number;
  description: string;
  priority: "low" | "medium" | "high" | "urgent";
}

export type TruckStatus =
  | "on_time"
  | "delayed"
  | "stopped"
  | "loading"
  | "unloading"
  | "maintenance";

// Asset Types (multi-asset tracking)
export type AssetType = "vehicle" | "vessel" | "equipment" | "container";

export type AssetSubtype =
  | "truck"
  | "fuel_truck"
  | "personnel_vehicle"
  | "boat"
  | "barge"
  | "crane"
  | "forklift"
  | "cargo_container"
  | "ISO_tank";

export interface Asset {
  id: string;
  assetType: AssetType;
  assetSubtype: AssetSubtype;
  name: string;
  status: string;
  currentLocation: Location;
  destination?: Location;
  route?: Route;
  estimatedArrival?: string;
  lastUpdate: string;
  // Vehicle fields
  plateNumber?: string;
  driverId?: string;
  driverName?: string;
  cargo?: CargoInfo;
  // Vessel fields
  vesselName?: string;
  imoNumber?: string;
  // Equipment fields
  equipmentModel?: string;
  liftingCapacityTonnes?: number;
  // Container fields
  containerNumber?: string;
  containerSize?: string;
  weightTonnes?: number;
}

// Backward compat: Truck is just an Asset with asset_type=vehicle
export type Truck = Asset;

// Fleet Tracking
export interface FleetSummary {
  totalTrucks: number;
  activeTrucks: number;
  onTimeTrucks: number;
  delayedTrucks: number;
  averageDelay: number; // in minutes
}

export interface AssetSummary extends FleetSummary {
  byType: Record<AssetType, number>;
  bySubtype: Record<AssetSubtype, number>;
}

// Map Types
export interface MapMarker {
  id: string;
  type: "truck" | "location" | "incident";
  position: {
    lat: number;
    lng: number;
  };
  data: unknown;
}

// Filter and Search
export interface FleetFilters {
  status?: TruckStatus[];
  route?: string[];
  driver?: string[];
  dateRange?: {
    start: string;
    end: string;
  };
}


// ─── Scheduling Types (Logistics Scheduling & Dispatch) ──────────────────────

export type JobType =
  | "cargo_transport"
  | "passenger_transport"
  | "vessel_movement"
  | "airport_transfer"
  | "crane_booking";

export type JobStatus =
  | "scheduled"
  | "assigned"
  | "in_progress"
  | "completed"
  | "cancelled"
  | "failed";

export type CargoItemStatus =
  | "pending"
  | "loaded"
  | "in_transit"
  | "delivered"
  | "damaged";

export type Priority = "low" | "normal" | "high" | "urgent";

export interface SchedulingCargoItem {
  item_id: string;
  description: string;
  weight_kg: number;
  container_number?: string;
  seal_number?: string;
  item_status: CargoItemStatus;
}

export interface Job {
  job_id: string;
  job_type: JobType;
  status: JobStatus;
  tenant_id: string;
  asset_assigned?: string;
  origin: string;
  destination: string;
  scheduled_time: string;
  estimated_arrival?: string;
  started_at?: string;
  completed_at?: string;
  created_at: string;
  updated_at: string;
  created_by?: string;
  priority: Priority;
  delayed: boolean;
  delay_duration_minutes?: number;
  failure_reason?: string;
  notes?: string;
  cargo_manifest?: SchedulingCargoItem[];
}

export interface JobEvent {
  event_id: string;
  job_id: string;
  event_type: string;
  actor_id?: string;
  event_timestamp: string;
  event_payload: Record<string, unknown>;
}

export interface JobSummary {
  total_jobs: number;
  scheduled: number;
  assigned: number;
  in_progress: number;
  completed: number;
  cancelled: number;
  failed: number;
  delayed: number;
}

export interface OperationsControlSummary {
  active_jobs: number;
  delayed_jobs: number;
  available_assets: number;
  fuel_alerts: number;
}
