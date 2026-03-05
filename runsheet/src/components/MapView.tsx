"use client";

import { AdvancedMarker, APIProvider, Map } from "@vis.gl/react-google-maps";
import { useEffect, useState } from "react";
import { apiService } from "../services/api";
import type { AssetType, Truck } from "../types/api";

/** Icon and label for each asset type on the map */
const ASSET_TYPE_CONFIG: Record<AssetType, { icon: string; label: string }> = {
  vehicle: { icon: "🚛", label: "Vehicles" },
  vessel: { icon: "🚢", label: "Vessels" },
  equipment: { icon: "🏗️", label: "Equipment" },
  container: { icon: "📦", label: "Containers" },
};

/** Resolve the marker icon for a truck/asset based on its asset_type field */
function getAssetIcon(truck: Truck): string {
  const assetType = (truck as Truck & { assetType?: AssetType }).assetType;
  return ASSET_TYPE_CONFIG[assetType ?? "vehicle"]?.icon ?? "🚛";
}

/** Get a display name for the asset (plate number, vessel name, container number, or fallback) */
function getAssetLabel(truck: Truck): string {
  return truck.name || truck.plateNumber || truck.vesselName || truck.containerNumber || truck.equipmentModel || truck.id;
}

interface MapViewProps {
  selectedTruck?: Truck | null;
  assetTypeFilter?: AssetType | "all";
}

export default function MapView({
  selectedTruck,
  assetTypeFilter = "all",
}: MapViewProps) {
  const [mapMode, setMapMode] = useState<"roadmap" | "satellite" | "hybrid">(
    "roadmap",
  );
  const [showInfo, setShowInfo] = useState(true);
  const [trucks, setTrucks] = useState<Truck[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTypeFilter, setActiveTypeFilter] = useState<AssetType | "all">(
    assetTypeFilter,
  );

  const apiKey = process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY;

  const loadTrucks = async () => {
    try {
      // Fetch all assets (not just trucks) so every type appears on the map
      const response = await apiService.getAssets();
      // Filter assets with valid coordinates
      const validTrucks = response.data.filter((truck) => {
        const lat = truck.currentLocation?.coordinates?.lat;
        const lng = truck.currentLocation?.coordinates?.lon;
        const isValid =
          typeof lat === "number" &&
          typeof lng === "number" &&
          !Number.isNaN(lat) &&
          !Number.isNaN(lng) &&
          lat >= -90 &&
          lat <= 90 &&
          lng >= -180 &&
          lng <= 180;

        if (!isValid) {
          console.warn(`Invalid coordinates for truck ${truck.plateNumber}:`, {
            lat,
            lng,
          });
        }

        return isValid;
      });
      setTrucks(validTrucks);
    } catch (error) {
      console.error("Failed to load trucks:", error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTrucks();
  }, []);

  /** Filter assets by the active type filter */
  const filteredAssets =
    activeTypeFilter === "all"
      ? trucks
      : trucks.filter((t) => {
          const assetType = (t as Truck & { assetType?: AssetType }).assetType;
          return (
            assetType === activeTypeFilter ||
            (!assetType && activeTypeFilter === "vehicle")
          );
        });

  if (!apiKey) {
    return (
      <div className="h-full flex items-center justify-center bg-gray-50">
        <div className="text-center">
          <p className="text-red-600 font-medium">
            Google Maps API key not found
          </p>
          <p className="text-gray-500 text-sm">
            Please add NEXT_PUBLIC_GOOGLE_MAPS_API_KEY to your .env.local file
          </p>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center bg-gray-50">
        <div className="text-center">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gray-900 mx-auto"></div>
          <p className="mt-2 text-gray-600">Loading map...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full relative overflow-hidden flex flex-col bg-white">
      {/* Map Controls */}
      <div className="border-b border-gray-100 bg-white p-4 flex-shrink-0">
        <div className="flex items-center justify-between">
          <div className="flex bg-gray-50 rounded-lg p-0.5 border border-gray-200">
            {(["roadmap", "satellite", "hybrid"] as const).map((mode) => (
              <button
                key={mode}
                onClick={() => setMapMode(mode)}
                className={`px-4 py-2 text-sm font-medium rounded-md transition-all capitalize ${
                  mapMode === mode
                    ? "bg-[#232323] text-white shadow-sm"
                    : "text-gray-600 hover:text-gray-900"
                }`}
              >
                {mode}
              </button>
            ))}
          </div>

          {/* Asset type filter */}
          <div className="flex bg-gray-50 rounded-lg p-0.5 border border-gray-200">
            <button
              onClick={() => setActiveTypeFilter("all")}
              className={`px-3 py-2 text-sm font-medium rounded-md transition-all ${
                activeTypeFilter === "all"
                  ? "bg-[#232323] text-white shadow-sm"
                  : "text-gray-600 hover:text-gray-900"
              }`}
            >
              All
            </button>
            {(
              Object.entries(ASSET_TYPE_CONFIG) as [
                AssetType,
                { icon: string; label: string },
              ][]
            ).map(([type, config]) => (
              <button
                key={type}
                onClick={() => setActiveTypeFilter(type)}
                className={`px-3 py-2 text-sm font-medium rounded-md transition-all ${
                  activeTypeFilter === type
                    ? "bg-[#232323] text-white shadow-sm"
                    : "text-gray-600 hover:text-gray-900"
                }`}
                title={config.label}
              >
                {config.icon}
              </button>
            ))}
          </div>

          {selectedTruck && (
            <div className="flex items-center space-x-3">
              <div className="flex items-center space-x-2 text-sm text-gray-600">
                <span>
                  Tracking:{" "}
                  <span className="font-medium text-gray-900">
                    {selectedTruck.plateNumber}
                  </span>
                </span>
              </div>
              <button
                onClick={() => setShowInfo(!showInfo)}
                className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900 hover:bg-gray-50 rounded-md transition-colors"
              >
                {showInfo ? "Hide" : "Show"} Info
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Google Maps */}
      <div className="flex-1 relative">
        <APIProvider apiKey={apiKey}>
          <Map
            mapId="ff5c9f40e270515093c1c77f"
            defaultCenter={{ lat: -1.2921, lng: 36.8219 }}
            defaultZoom={7}
            mapTypeId={mapMode}
            style={{ width: "100%", height: "100%" }}
            gestureHandling="greedy"
            disableDefaultUI={false}
            zoomControl={true}
            mapTypeControl={false}
            scaleControl={true}
            streetViewControl={false}
            rotateControl={false}
            fullscreenControl={false}
          >
            {/* Asset markers */}
            {filteredAssets.map((truck) => {
              const lat = truck.currentLocation.coordinates.lat;
              const lng = truck.currentLocation.coordinates.lon;
              const icon = getAssetIcon(truck);
              const label = getAssetLabel(truck);

              return (
                <AdvancedMarker
                  key={truck.id}
                  position={{ lat, lng }}
                  title={`${label} - ${truck.driverName || truck.id}`}
                  onClick={() => {
                    // You can add click handler here if needed
                  }}
                >
                  <div
                    className={`px-3 py-1.5 rounded-lg text-xs font-medium flex items-center space-x-1.5 shadow-sm transition-all ${
                      selectedTruck?.id === truck.id
                        ? "bg-[#232323] text-white ring-2 ring-gray-400 scale-110"
                        : "bg-white text-gray-900 hover:shadow-md border border-gray-200"
                    }`}
                  >
                    <span>{icon}</span>
                    <span>{label}</span>
                  </div>
                </AdvancedMarker>
              );
            })}
          </Map>
        </APIProvider>

        {/* Asset info panel */}
        {selectedTruck && showInfo && (
          <div className="absolute top-4 right-4 bg-white rounded-xl shadow-lg p-4 w-72 border border-gray-100 z-10">
            <div className="flex items-start justify-between mb-3">
              <div className="flex items-center space-x-2">
                <h3 className="font-semibold text-gray-900 text-base">
                  {getAssetLabel(selectedTruck)}
                </h3>
                <span
                  className={`px-2.5 py-0.5 rounded-full text-xs font-medium ${
                    selectedTruck.status === "on_time"
                      ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
                      : "bg-red-50 text-red-700 border border-red-200"
                  }`}
                >
                  {selectedTruck.status.replace("_", " ")}
                </span>
              </div>
              <button
                onClick={() => setShowInfo(false)}
                className="text-gray-400 hover:text-gray-600 transition-colors"
              >
                <svg
                  className="w-5 h-5"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M6 18L18 6M6 6l12 12"
                  />
                </svg>
              </button>
            </div>

            <div className="space-y-2.5 text-sm">
              <div className="flex justify-between py-1.5 border-b border-gray-50">
                <span className="text-gray-500">Type</span>
                <span className="text-gray-900 font-medium">
                  {getAssetIcon(selectedTruck)} {selectedTruck.assetSubtype?.replace("_", " ") ?? "truck"}
                </span>
              </div>

              {selectedTruck.driverName && (
                <div className="flex justify-between py-1.5 border-b border-gray-50">
                  <span className="text-gray-500">Driver</span>
                  <span className="text-gray-900 font-medium">
                    {selectedTruck.driverName}
                  </span>
                </div>
              )}

              {selectedTruck.route?.origin?.name && (
                <div className="flex justify-between py-1.5 border-b border-gray-50">
                  <span className="text-gray-500">Route</span>
                  <span className="text-gray-900 text-right">
                    {selectedTruck.route.origin.name} →{" "}
                    {selectedTruck.route?.destination?.name ?? "—"}
                  </span>
                </div>
              )}

              {selectedTruck.route?.distance != null && (
                <div className="flex justify-between py-1.5 border-b border-gray-50">
                  <span className="text-gray-500">Distance</span>
                  <span className="text-gray-900 font-medium">
                    {selectedTruck.route.distance} km
                  </span>
                </div>
              )}

              {selectedTruck.cargo && (
                <div className="flex justify-between py-1.5 border-b border-gray-50">
                  <span className="text-gray-500">Cargo</span>
                  <span className="text-gray-900">
                    {selectedTruck.cargo.type}
                  </span>
                </div>
              )}

              {selectedTruck.vesselName && (
                <div className="flex justify-between py-1.5 border-b border-gray-50">
                  <span className="text-gray-500">Vessel</span>
                  <span className="text-gray-900">{selectedTruck.vesselName}</span>
                </div>
              )}

              {selectedTruck.containerNumber && (
                <div className="flex justify-between py-1.5 border-b border-gray-50">
                  <span className="text-gray-500">Container</span>
                  <span className="text-gray-900">{selectedTruck.containerNumber} {selectedTruck.containerSize ?? ""}</span>
                </div>
              )}

              {selectedTruck.equipmentModel && (
                <div className="flex justify-between py-1.5 border-b border-gray-50">
                  <span className="text-gray-500">Model</span>
                  <span className="text-gray-900">{selectedTruck.equipmentModel}</span>
                </div>
              )}

              <div className="flex justify-between py-1.5">
                <span className="text-gray-500">Location</span>
                <span className="text-gray-900 text-right">
                  {selectedTruck.currentLocation?.name ?? "Unknown"}
                </span>
              </div>
            </div>
          </div>
        )}

        {/* Map attribution */}
        <div className="absolute bottom-3 left-3 bg-white/95 backdrop-blur-sm px-3 py-1.5 rounded-lg text-xs text-gray-500 shadow-sm border border-gray-100 z-10">
          Google Maps • <span className="capitalize">{mapMode}</span>
        </div>
      </div>
    </div>
  );
}
