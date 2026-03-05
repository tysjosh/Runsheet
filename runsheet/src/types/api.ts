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
