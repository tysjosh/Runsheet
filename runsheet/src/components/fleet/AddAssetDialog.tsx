"use client";

import { humanize } from "../../lib/format";
import type { CreateAssetPayload } from "../../services/api";
import { apiService } from "../../services/api";
import type { Asset, AssetSubtype, AssetType } from "../../types/api";
/**
 * Add asset (UI revamp task 3.1): the former bespoke Fleet "Add asset" modal
 * on FormDialog (md). Posts to `POST /fleet/assets`; a tanker's compartments
 * are then defined from the row's Compartments drawer.
 */
import { Field, FormDialog, INPUT_CLASS, NumberField, Select } from "../ui";

/** Asset subtype options grouped by the parent asset type. */
export const SUBTYPE_OPTIONS: Record<AssetType, AssetSubtype[]> = {
  vehicle: ["truck", "fuel_truck", "personnel_vehicle"],
  vessel: ["boat", "barge"],
  equipment: ["crane", "forklift"],
  container: ["cargo_container", "ISO_tank"],
};

const SUBTYPE_LABEL: Partial<Record<AssetSubtype, string>> = {
  ISO_tank: "ISO tank",
};
export const subtypeLabel = (s: string) =>
  SUBTYPE_LABEL[s as AssetSubtype] ?? humanize(s);

type Values = {
  asset_id: string;
  name: string;
  asset_type: AssetType;
  asset_subtype: AssetSubtype;
  identifier: string;
  address: string;
  lat: number | null;
  lon: number | null;
};

const INITIAL: Values = {
  asset_id: "",
  name: "",
  asset_type: "vehicle",
  asset_subtype: "fuel_truck",
  identifier: "",
  address: "",
  lat: 0,
  lon: 0,
};

/** The type-specific identifier the backend requires, if any. */
export function identifierFor(type: AssetType) {
  if (type === "vehicle")
    return { key: "plate_number" as const, label: "Plate number" };
  if (type === "vessel")
    return { key: "vessel_name" as const, label: "Vessel name" };
  if (type === "container")
    return { key: "container_number" as const, label: "Container number" };
  return null; // equipment needs no extra identifier
}

export function validateAsset(v: Values) {
  const errors: Record<string, string | undefined> = {};
  if (!v.asset_id.trim()) errors.asset_id = "Enter an asset ID.";
  if (!v.name.trim()) errors.name = "Enter a name.";
  const ident = identifierFor(v.asset_type);
  if (ident && !v.identifier.trim())
    errors.identifier = `Enter the ${ident.label.toLowerCase()}.`;
  if (v.lat == null || Number.isNaN(v.lat) || v.lat < -90 || v.lat > 90)
    errors.lat = "Latitude is between -90 and 90.";
  if (v.lon == null || Number.isNaN(v.lon) || v.lon < -180 || v.lon > 180)
    errors.lon = "Longitude is between -180 and 180.";
  return errors;
}

export default function AddAssetDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const submit = async (v: Values): Promise<Asset> => {
    const id = v.asset_id.trim();
    const address = v.address.trim() || "Unspecified";
    const ident = identifierFor(v.asset_type);
    const payload: CreateAssetPayload = {
      asset_id: id,
      asset_type: v.asset_type,
      asset_subtype: v.asset_subtype,
      name: v.name.trim(),
      status: "active",
      current_location: {
        id: `loc-${id}`,
        name: address,
        type: "site",
        coordinates: { lat: v.lat as number, lon: v.lon as number },
        address,
      },
      ...(ident ? { [ident.key]: v.identifier.trim() } : {}),
    };
    const res = await apiService.createAsset(payload);
    return res.data;
  };
  return (
    <FormDialog<Values, Asset>
      open
      size="md"
      title="Add asset"
      submitLabel="Create asset"
      successMessage="Asset created"
      initialValues={INITIAL}
      validate={validateAsset}
      onSubmit={submit}
      onSaved={onCreated}
      onClose={onClose}
    >
      {({ values, set, setValues, errors }) => {
        const ident = identifierFor(values.asset_type);
        return (
          <>
            <Field label="Asset ID" required error={errors.asset_id} span={1}>
              <input
                id="asset-id"
                type="text"
                value={values.asset_id}
                onChange={(e) => set("asset_id", e.target.value)}
                placeholder="TNK-001"
                className={INPUT_CLASS}
              />
            </Field>
            <Field label="Name" required error={errors.name} span={1}>
              <input
                id="asset-name"
                type="text"
                value={values.name}
                onChange={(e) => set("name", e.target.value)}
                placeholder="Tanker 1"
                className={INPUT_CLASS}
              />
            </Field>
            <Field label="Type" span={1} id="asset-type">
              <Select
                id="asset-type"
                value={values.asset_type}
                onChange={(t) =>
                  setValues((prev) => ({
                    ...prev,
                    asset_type: t as AssetType,
                    asset_subtype: SUBTYPE_OPTIONS[t as AssetType][0],
                    identifier: "",
                  }))
                }
                options={(Object.keys(SUBTYPE_OPTIONS) as AssetType[]).map(
                  (t) => ({ value: t, label: humanize(t) }),
                )}
              />
            </Field>
            <Field label="Subtype" span={1} id="asset-subtype">
              <Select
                id="asset-subtype"
                value={values.asset_subtype}
                onChange={(s) => set("asset_subtype", s as AssetSubtype)}
                options={SUBTYPE_OPTIONS[values.asset_type].map((s) => ({
                  value: s,
                  label: subtypeLabel(s),
                }))}
              />
            </Field>
            {ident && (
              <Field label={ident.label} required error={errors.identifier}>
                <input
                  id="asset-identifier"
                  type="text"
                  value={values.identifier}
                  onChange={(e) => set("identifier", e.target.value)}
                  placeholder={
                    values.asset_type === "vehicle" ? "ABC-1234" : ident.label
                  }
                  className={INPUT_CLASS}
                />
              </Field>
            )}
            <Field label="Location or address" error={errors.address}>
              <input
                id="asset-address"
                type="text"
                value={values.address}
                onChange={(e) => set("address", e.target.value)}
                placeholder="Main depot"
                className={INPUT_CLASS}
              />
            </Field>
            <Field label="Latitude" error={errors.lat} span={1} id="asset-lat">
              <NumberField
                id="asset-lat"
                decimals={5}
                min={-90}
                max={90}
                value={values.lat}
                onChange={(n) => set("lat", n)}
              />
            </Field>
            <Field label="Longitude" error={errors.lon} span={1} id="asset-lon">
              <NumberField
                id="asset-lon"
                decimals={5}
                min={-180}
                max={180}
                value={values.lon}
                onChange={(n) => set("lon", n)}
              />
            </Field>
          </>
        );
      }}
    </FormDialog>
  );
}
