"use client";

/**
 * Fleet → Inventory (UI revamp task 3.1, design.md §5 "Inventory item": md).
 *
 * One toolbar (search, status chips with the summary counts, a Filters
 * popover for category, low-stock alert count), a DataTable with a row menu
 * (Restock, Consume, Edit, History, Delete), and every flow on the shared
 * dialogs: create, edit and stock adjustment are FormDialogs; delete is a
 * confirm Modal; history is a Drawer. The 5-card stats band became the chip
 * counts plus the stock value in the title row.
 */
import {
  AlertTriangle,
  ArrowDownCircle,
  ArrowUpCircle,
  Clock,
  Pencil,
  Plus,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { dateLong, dateTime, humanize, money, number } from "../lib/format";
import { apiService, type InventoryItem } from "../services/api";
import {
  adjustStock,
  type CreateInventoryItemPayload,
  createItem,
  deleteItem,
  getAlerts,
  getItemHistory,
  getSummary,
  type InventoryItem as InvApiItem,
  type InventoryCategory,
  type InventorySummary,
  type StockMovementEvent,
} from "../services/inventoryApi";
import type { StatusKey } from "../styles/tokens";
import {
  Button,
  type Column,
  DataTable,
  Drawer,
  Field,
  FilterChips,
  FilterPopover,
  FormDialog,
  INPUT_CLASS,
  Modal,
  NumberField,
  Select,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "./ui";
import { notify } from "./ui/toast/notify";

/** Stock status → badge style and label (icon + text, not colour alone). */
export const INVENTORY_STATUS: Record<
  InventoryItem["status"],
  { status: StatusKey; label: string }
> = {
  in_stock: { status: "ok", label: "In stock" },
  low_stock: { status: "warning", label: "Low stock" },
  out_of_stock: { status: "critical", label: "Out of stock" },
};
const STATUS_IDS = Object.keys(INVENTORY_STATUS) as InventoryItem["status"][];

const CATEGORY_OPTIONS: { value: InventoryCategory; label: string }[] = [
  { value: "tires", label: "Tires" },
  { value: "engine_parts", label: "Engine parts" },
  { value: "brake_parts", label: "Brake parts" },
  { value: "fluids", label: "Fluids" },
  { value: "filters", label: "Filters" },
  { value: "electrical", label: "Electrical" },
  { value: "fuel_equipment", label: "Fuel equipment" },
  { value: "safety", label: "Safety" },
  { value: "general", label: "General" },
];

const PAGE_SIZE = 20;

export default function Inventory() {
  const [inventory, setInventory] = useState<InventoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [filterCategory, setFilterCategory] = useState("all");
  const [filterStatus, setFilterStatus] = useState("all");
  const [page, setPage] = useState(1);
  const [editingItem, setEditingItem] = useState<InventoryItem | null>(null);
  const [adjusting, setAdjusting] = useState<{
    item: InventoryItem;
    type: "restock" | "consume";
  } | null>(null);
  const [creating, setCreating] = useState(false);
  const [deletingItem, setDeletingItem] = useState<InventoryItem | null>(null);
  const [historyItem, setHistoryItem] = useState<InventoryItem | null>(null);
  const [summary, setSummary] = useState<InventorySummary | null>(null);
  const [alerts, setAlerts] = useState<InvApiItem[]>([]);
  const [showAlerts, setShowAlerts] = useState(false);

  const loadInventoryData = useCallback(async () => {
    try {
      setLoading(true);
      setLoadError(null);
      const response = await apiService.getInventory();
      setInventory(response.data);
    } catch (error) {
      setLoadError(
        error instanceof Error ? error.message : "Failed to load inventory",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDashboardData = useCallback(async () => {
    const [summaryRes, alertsRes] = await Promise.allSettled([
      getSummary(),
      getAlerts(),
    ]);
    if (summaryRes.status === "fulfilled") setSummary(summaryRes.value.data);
    if (alertsRes.status === "fulfilled") setAlerts(alertsRes.value.data);
  }, []);

  useEffect(() => {
    loadInventoryData();
    loadDashboardData();
  }, [loadInventoryData, loadDashboardData]);

  const reloadAll = () => {
    loadInventoryData();
    loadDashboardData();
  };

  const q = searchTerm.trim().toLowerCase();
  const matchesBase = (item: InventoryItem) =>
    (!q ||
      item.name.toLowerCase().includes(q) ||
      item.category.toLowerCase().includes(q)) &&
    (filterCategory === "all" ||
      item.category.toLowerCase() === filterCategory);
  const filteredInventory = inventory.filter(
    (item) =>
      matchesBase(item) &&
      (filterStatus === "all" || item.status === filterStatus),
  );
  const totalPages = Math.max(
    1,
    Math.ceil(filteredInventory.length / PAGE_SIZE),
  );
  const paginatedInventory = filteredInventory.slice(
    (page - 1) * PAGE_SIZE,
    page * PAGE_SIZE,
  );

  // Chip counts: the summary endpoint's totals when nothing narrows the
  // list, otherwise the loaded rows under the current search and category.
  const narrowed = q !== "" || filterCategory !== "all";
  const statusCounts: Record<string, number> = narrowed
    ? {
        all: inventory.filter(matchesBase).length,
        ...Object.fromEntries(
          STATUS_IDS.map((s) => [
            s,
            inventory.filter((i) => matchesBase(i) && i.status === s).length,
          ]),
        ),
      }
    : {
        all: summary?.total_items ?? inventory.length,
        in_stock:
          summary?.in_stock ??
          inventory.filter((i) => i.status === "in_stock").length,
        low_stock:
          summary?.low_stock ??
          inventory.filter((i) => i.status === "low_stock").length,
        out_of_stock:
          summary?.out_of_stock ??
          inventory.filter((i) => i.status === "out_of_stock").length,
      };

  const categories = Array.from(
    new Set(inventory.map((item) => item.category.toLowerCase())),
  );

  const actions = useMemo(
    () => (
      <Button
        size="sm"
        icon={<Plus className="h-3.5 w-3.5" />}
        onClick={() => setCreating(true)}
      >
        Add item
      </Button>
    ),
    [],
  );
  const counts = useMemo(
    () =>
      summary?.total_value != null ? (
        <span className="whitespace-nowrap text-xs text-text-muted">
          Stock value {money(summary.total_value, { decimals: 0 })}
        </span>
      ) : null,
    [summary],
  );
  const embedded = usePageChrome({ actions, counts });

  const columns: Column<InventoryItem>[] = [
    {
      key: "item",
      header: "Item",
      truncate: true,
      title: (i) => `${i.name} (${i.id})`,
      cell: (i) => (
        <span className="block min-w-0">
          <span className="block truncate font-medium text-text">{i.name}</span>
          <span className="block truncate font-mono text-xs text-text-muted">
            {i.id}
          </span>
        </span>
      ),
    },
    {
      key: "category",
      header: "Category",
      className: "text-slate-700",
      cell: (i) => humanize(i.category),
    },
    {
      key: "location",
      header: "Location",
      truncate: true,
      title: (i) => i.location,
      className: "text-slate-700",
      cell: (i) => i.location,
    },
    {
      key: "quantity",
      header: "Quantity",
      align: "right",
      className: "tabular-nums font-semibold text-text whitespace-nowrap",
      cell: (i) => `${number(i.quantity)} ${i.unit}`,
    },
    {
      key: "status",
      header: "Status",
      width: 120,
      cell: (i) => {
        const cfg = INVENTORY_STATUS[i.status];
        return cfg ? (
          <StatusBadge status={cfg.status} label={cfg.label} />
        ) : (
          humanize(i.status)
        );
      },
    },
    {
      key: "lastUpdated",
      header: "Updated",
      className: "text-slate-700 whitespace-nowrap",
      cell: (i) => dateLong(i.lastUpdated),
    },
  ];

  return (
    <div className="flex h-full flex-col bg-surface">
      <Toolbar
        label="Inventory"
        search={
          <input
            type="search"
            value={searchTerm}
            onChange={(e) => {
              setSearchTerm(e.target.value);
              setPage(1);
            }}
            placeholder="Search name or category"
            aria-label="Search inventory"
            className="h-7 w-full rounded-lg border border-slate-300 bg-surface px-2.5 text-xs text-slate-900 placeholder:text-slate-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          />
        }
        filters={
          <>
            <FilterChips
              label="Stock status"
              collapse
              options={[
                { id: "all", label: "All", count: statusCounts.all },
                ...STATUS_IDS.map((id) => ({
                  id,
                  label: INVENTORY_STATUS[id].label,
                  count: statusCounts[id],
                  status: INVENTORY_STATUS[id].status,
                })),
              ]}
              value={filterStatus}
              onChange={(v) => {
                setFilterStatus(v as string);
                setPage(1);
              }}
            />
            <FilterPopover
              count={filterCategory === "all" ? 0 : 1}
              label="Inventory filters"
              onClear={() => {
                setFilterCategory("all");
                setPage(1);
              }}
            >
              <Field label="Category" id="inv-filter-category">
                <Select
                  id="inv-filter-category"
                  value={filterCategory}
                  onChange={(v) => {
                    setFilterCategory(v);
                    setPage(1);
                  }}
                  options={[
                    { value: "all", label: "All categories" },
                    ...categories.map((c) => ({
                      value: c,
                      label: humanize(c),
                    })),
                  ]}
                />
              </Field>
            </FilterPopover>
          </>
        }
        end={
          <>
            {alerts.length > 0 && (
              <button
                type="button"
                aria-expanded={showAlerts}
                onClick={() => setShowAlerts((v) => !v)}
                className="inline-flex h-7 items-center gap-1 whitespace-nowrap rounded-full border border-amber-300 bg-amber-50 px-2 text-xs font-semibold text-amber-900 hover:bg-amber-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
              >
                <AlertTriangle aria-hidden="true" className="h-3 w-3" />
                {alerts.length} low-stock alert{alerts.length === 1 ? "" : "s"}
              </button>
            )}
            {!embedded && counts}
            {!embedded && actions}
          </>
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<InventoryItem>
          ariaLabel="Inventory items"
          columns={columns}
          data={loading || loadError ? [] : paginatedInventory}
          loading={loading}
          error={
            loadError
              ? { message: loadError, onRetry: loadInventoryData }
              : null
          }
          getRowId={(i) => i.id}
          rowLabel={(i) => i.name}
          rowMenu={(i) => [
            {
              id: "restock",
              label: "Restock",
              icon: <ArrowUpCircle className="h-3.5 w-3.5" />,
              onSelect: () => setAdjusting({ item: i, type: "restock" }),
            },
            {
              id: "consume",
              label: "Consume",
              icon: <ArrowDownCircle className="h-3.5 w-3.5" />,
              onSelect: () => setAdjusting({ item: i, type: "consume" }),
            },
            {
              id: "edit",
              label: "Edit item",
              icon: <Pencil className="h-3.5 w-3.5" />,
              onSelect: () => setEditingItem(i),
            },
            {
              id: "history",
              label: "Stock history",
              icon: <Clock className="h-3.5 w-3.5" />,
              onSelect: () => setHistoryItem(i),
            },
            {
              id: "delete",
              label: "Delete item",
              icon: <Trash2 className="h-3.5 w-3.5" />,
              onSelect: () => setDeletingItem(i),
            },
          ]}
          pagination={
            totalPages > 1
              ? {
                  page,
                  totalPages,
                  totalItems: filteredInventory.length,
                  onPageChange: setPage,
                }
              : undefined
          }
          emptyState={
            <div className="text-text-muted">
              <p className="text-sm font-medium">No inventory items found</p>
              <p className="mt-1 text-xs">
                Try adjusting your search or filters
              </p>
            </div>
          }
        />
      </div>

      <Drawer
        open={showAlerts && alerts.length > 0}
        onClose={() => setShowAlerts(false)}
        title="Low-stock alerts"
        width={420}
      >
        <ul className="space-y-2">
          {alerts.map((a) => {
            const cfg =
              INVENTORY_STATUS[a.status as InventoryItem["status"]] ??
              INVENTORY_STATUS.low_stock;
            return (
              <li
                key={a.item_id}
                className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 px-3 py-2"
              >
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium text-text">
                    {a.name}
                  </span>
                  <span className="block truncate text-xs text-text-muted">
                    {humanize(a.category)} · {a.location}
                  </span>
                </span>
                <StatusBadge
                  status={cfg.status}
                  label={`${number(a.quantity)} / ${number(a.min_threshold)} min`}
                />
              </li>
            );
          })}
        </ul>
      </Drawer>

      {editingItem && (
        <EditItemDialog
          item={editingItem}
          onClose={() => setEditingItem(null)}
          onSaved={(updated) => {
            setInventory((prev) =>
              prev.map((i) => (i.id === updated.id ? updated : i)),
            );
            setEditingItem(null);
            loadDashboardData();
          }}
        />
      )}
      {adjusting && (
        <StockAdjustmentDialog
          item={adjusting.item}
          type={adjusting.type}
          onClose={() => setAdjusting(null)}
          onComplete={() => {
            setAdjusting(null);
            reloadAll();
          }}
        />
      )}
      {creating && (
        <CreateItemDialog
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            reloadAll();
          }}
        />
      )}
      {deletingItem && (
        <DeleteItemDialog
          item={deletingItem}
          onClose={() => setDeletingItem(null)}
          onDeleted={() => {
            setDeletingItem(null);
            reloadAll();
          }}
        />
      )}
      <Drawer
        open={historyItem !== null}
        onClose={() => setHistoryItem(null)}
        title={historyItem ? `Stock history · ${historyItem.name}` : "History"}
        width={640}
      >
        {historyItem && <StockHistory item={historyItem} />}
      </Drawer>
    </div>
  );
}

// ─── Stock adjustment ────────────────────────────────────────────────────────

const RESTOCK_REASONS = [
  { value: "restock", label: "Restock" },
  { value: "return", label: "Return" },
  { value: "correction", label: "Correction" },
];
const CONSUME_REASONS = [
  { value: "used_for_maintenance", label: "Used for maintenance" },
  { value: "damaged", label: "Damaged" },
  { value: "expired", label: "Expired" },
  { value: "correction", label: "Correction" },
];

function StockAdjustmentDialog({
  item,
  type,
  onClose,
  onComplete,
}: {
  item: InventoryItem;
  type: "restock" | "consume";
  onClose: () => void;
  onComplete: () => void;
}) {
  type V = { quantity: number | null; reason: string; reference_id: string };
  return (
    <FormDialog<V>
      open
      size="sm"
      title={type === "restock" ? "Restock item" : "Consume stock"}
      help={`${item.name} · current ${number(item.quantity)} ${item.unit} · ${item.location}`}
      submitLabel={type === "restock" ? "Restock" : "Consume"}
      successMessage="Stock adjusted"
      initialValues={{
        quantity: null,
        reason: type === "restock" ? "restock" : "used_for_maintenance",
        reference_id: "",
      }}
      validate={(v) =>
        v.quantity == null || Number.isNaN(v.quantity) || v.quantity <= 0
          ? { quantity: "Enter a quantity above 0." }
          : {}
      }
      onSubmit={(v) =>
        adjustStock(item.id, {
          quantity_change:
            type === "consume"
              ? -(v.quantity as number)
              : (v.quantity as number),
          reason: v.reason,
          ...(v.reference_id.trim() && {
            reference_id: v.reference_id.trim(),
          }),
        })
      }
      onSaved={onComplete}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field
            label={`Quantity to ${type === "restock" ? "add" : "deduct"}`}
            required
            error={errors.quantity}
            id="adj-qty"
          >
            <NumberField
              id="adj-qty"
              unit={item.unit}
              min={1}
              value={values.quantity}
              onChange={(n) => set("quantity", n)}
            />
          </Field>
          <Field label="Reason" id="adj-reason">
            <Select
              id="adj-reason"
              value={values.reason}
              onChange={(r) => set("reason", r)}
              options={type === "restock" ? RESTOCK_REASONS : CONSUME_REASONS}
            />
          </Field>
          <Field label="Reference ID">
            <input
              id="adj-ref"
              type="text"
              value={values.reference_id}
              onChange={(e) => set("reference_id", e.target.value)}
              placeholder="e.g. PO-12345 or JOB-456"
              className={INPUT_CLASS}
            />
          </Field>
        </>
      )}
    </FormDialog>
  );
}

// ─── Edit ────────────────────────────────────────────────────────────────────

function EditItemDialog({
  item,
  onClose,
  onSaved,
}: {
  item: InventoryItem;
  onClose: () => void;
  onSaved: (updated: InventoryItem) => void;
}) {
  type V = {
    quantity: number | null;
    status: InventoryItem["status"];
    location: string;
  };
  return (
    <FormDialog<V, InventoryItem>
      open
      size="md"
      title="Edit inventory item"
      help={`${item.name} · ${item.id} · ${humanize(item.category)}`}
      submitLabel="Save changes"
      successMessage="Item saved"
      initialValues={{
        quantity: item.quantity,
        status: item.status,
        location: item.location,
      }}
      validate={(v) => {
        const e: Record<string, string | undefined> = {};
        if (v.quantity == null || Number.isNaN(v.quantity) || v.quantity < 0)
          e.quantity = "Enter a quantity of 0 or more.";
        if (!v.location.trim()) e.location = "Enter a location.";
        return e;
      }}
      onSubmit={async (v) => {
        const response = await apiService.updateInventoryItem(item.id, {
          quantity: v.quantity as number,
          status: v.status,
          location: v.location.trim(),
        });
        return response.data;
      }}
      onSaved={onSaved}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field
            label="Quantity"
            required
            error={errors.quantity}
            span={1}
            id="edit-qty"
          >
            <NumberField
              id="edit-qty"
              unit={item.unit}
              min={0}
              value={values.quantity}
              onChange={(n) => set("quantity", n)}
            />
          </Field>
          <Field label="Status" span={1} id="edit-status">
            <Select
              id="edit-status"
              value={values.status}
              onChange={(s) => set("status", s as InventoryItem["status"])}
              options={STATUS_IDS.map((s) => ({
                value: s,
                label: INVENTORY_STATUS[s].label,
              }))}
            />
          </Field>
          <Field label="Location" required error={errors.location}>
            <input
              id="edit-location"
              type="text"
              value={values.location}
              onChange={(e) => set("location", e.target.value)}
              placeholder="e.g. Warehouse A"
              className={INPUT_CLASS}
            />
          </Field>
        </>
      )}
    </FormDialog>
  );
}

// ─── Create ──────────────────────────────────────────────────────────────────

function CreateItemDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  type V = {
    name: string;
    category: InventoryCategory;
    unit: string;
    location: string;
    quantity: number | null;
    min_threshold: number | null;
    max_capacity: number | null;
    unit_cost: number | null;
    supplier: string;
  };
  return (
    <FormDialog<V>
      open
      size="md"
      title="Add inventory item"
      submitLabel="Create item"
      successMessage="Item created"
      initialValues={{
        name: "",
        category: "general",
        unit: "pieces",
        location: "",
        quantity: 0,
        min_threshold: 5,
        max_capacity: 100,
        unit_cost: null,
        supplier: "",
      }}
      validate={(v) => {
        const e: Record<string, string | undefined> = {};
        if (!v.name.trim()) e.name = "Enter a name.";
        if (!v.unit.trim()) e.unit = "Enter a unit.";
        if (!v.location.trim()) e.location = "Enter a location.";
        if (v.max_capacity != null && v.max_capacity < 1)
          e.max_capacity = "Capacity is at least 1.";
        return e;
      }}
      onSubmit={(v) => {
        const payload: CreateInventoryItemPayload = {
          name: v.name.trim(),
          category: v.category,
          quantity: v.quantity ?? 0,
          unit: v.unit.trim(),
          min_threshold: v.min_threshold ?? 0,
          max_capacity: v.max_capacity ?? 100,
          location: v.location.trim(),
          unit_cost: v.unit_cost,
          supplier: v.supplier.trim() || null,
        };
        return createItem(payload);
      }}
      onSaved={onCreated}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field label="Name" required error={errors.name}>
            <input
              id="inv-name"
              type="text"
              value={values.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="e.g. Bridgestone R260 tire"
              className={INPUT_CLASS}
            />
          </Field>
          <Field label="Category" span={1} id="inv-category">
            <Select
              id="inv-category"
              value={values.category}
              onChange={(c) => set("category", c as InventoryCategory)}
              options={CATEGORY_OPTIONS}
            />
          </Field>
          <Field label="Unit" required error={errors.unit} span={1}>
            <input
              id="inv-unit"
              type="text"
              value={values.unit}
              onChange={(e) => set("unit", e.target.value)}
              placeholder="pieces, liters, sets"
              className={INPUT_CLASS}
            />
          </Field>
          <Field label="Location" required error={errors.location}>
            <input
              id="inv-location"
              type="text"
              value={values.location}
              onChange={(e) => set("location", e.target.value)}
              placeholder="e.g. Houston Terminal"
              className={INPUT_CLASS}
            />
          </Field>
          <Field label="Initial quantity" span={1} id="inv-qty">
            <NumberField
              id="inv-qty"
              min={0}
              value={values.quantity}
              onChange={(n) => set("quantity", n)}
            />
          </Field>
          <Field label="Low-stock threshold" span={1} id="inv-min">
            <NumberField
              id="inv-min"
              min={0}
              value={values.min_threshold}
              onChange={(n) => set("min_threshold", n)}
            />
          </Field>
          <Field
            label="Max capacity"
            error={errors.max_capacity}
            span={1}
            id="inv-max"
          >
            <NumberField
              id="inv-max"
              min={1}
              value={values.max_capacity}
              onChange={(n) => set("max_capacity", n)}
            />
          </Field>
          <Field label="Unit cost" span={1} id="inv-cost">
            <NumberField
              id="inv-cost"
              unit="$"
              decimals={2}
              min={0}
              value={values.unit_cost}
              onChange={(n) => set("unit_cost", n)}
            />
          </Field>
          <Field label="Supplier">
            <input
              id="inv-supplier"
              type="text"
              value={values.supplier}
              onChange={(e) => set("supplier", e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
        </>
      )}
    </FormDialog>
  );
}

// ─── Delete ──────────────────────────────────────────────────────────────────

function DeleteItemDialog({
  item,
  onClose,
  onDeleted,
}: {
  item: InventoryItem;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const confirm = async () => {
    setSubmitting(true);
    setError("");
    try {
      await deleteItem(item.id);
      notify({ type: "success", message: `Deleted ${item.name}` });
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete item");
    } finally {
      setSubmitting(false);
    }
  };
  return (
    <Modal
      isOpen
      onClose={onClose}
      title="Delete item?"
      size="sm"
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="danger" onClick={confirm} loading={submitting}>
            Delete item
          </Button>
        </div>
      }
    >
      {error && (
        <p
          role="alert"
          className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800"
        >
          {error}
        </p>
      )}
      <p className="text-sm text-slate-700">
        <span className="font-medium text-text">{item.name}</span> ({item.id},{" "}
        {humanize(item.category)}, {item.location}) will be removed. This can't
        be undone.
      </p>
    </Modal>
  );
}

// ─── History ─────────────────────────────────────────────────────────────────

function StockHistory({ item }: { item: InventoryItem }) {
  const [events, setEvents] = useState<StockMovementEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError("");
      try {
        const result = await getItemHistory(item.id, 1, 50);
        if (!cancelled) setEvents(result.data ?? []);
      } catch (err) {
        if (!cancelled)
          setError(
            err instanceof Error ? err.message : "Failed to load history",
          );
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [item.id]);
  const columns: Column<StockMovementEvent>[] = [
    {
      key: "timestamp",
      header: "When",
      className: "whitespace-nowrap text-slate-700",
      cell: (e) => dateTime(e.event_timestamp),
    },
    {
      key: "change",
      header: "Change",
      align: "right",
      className: "tabular-nums font-semibold",
      cell: (e) => (
        <span
          className={
            e.quantity_change > 0
              ? "text-green-800"
              : e.quantity_change < 0
                ? "text-red-800"
                : "text-slate-700"
          }
        >
          {e.quantity_change > 0 ? "+" : e.quantity_change < 0 ? "−" : ""}
          {number(Math.abs(e.quantity_change))}
        </span>
      ),
    },
    {
      key: "reason",
      header: "Reason",
      className: "text-slate-700",
      cell: (e) => humanize(e.reason),
    },
    {
      key: "reference",
      header: "Reference",
      className: "font-mono text-xs text-text-muted",
      cell: (e) => e.reference_id || "—",
    },
    {
      key: "after",
      header: "Result",
      align: "right",
      className: "tabular-nums text-slate-700",
      cell: (e) => number(e.quantity_after),
    },
  ];
  return (
    <>
      <p className="mb-3 text-xs text-text-muted">
        Current quantity {number(item.quantity)} {item.unit}
      </p>
      <DataTable<StockMovementEvent>
        ariaLabel="Stock movements"
        rowHeight="compact"
        columns={columns}
        data={loading || error ? [] : events}
        loading={loading}
        error={error ? { message: error } : null}
        getRowId={(e) => e.event_id}
        emptyState={
          <p className="text-sm text-text-muted">No stock movements recorded</p>
        }
      />
    </>
  );
}
