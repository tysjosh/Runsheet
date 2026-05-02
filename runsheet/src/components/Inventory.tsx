import { Filter, Package, Pencil, Search, X } from "lucide-react";
import { useEffect, useState } from "react";
import { apiService, type InventoryItem } from "../services/api";
import LoadingSpinner from "./LoadingSpinner";

const INVENTORY_STATUSES: { value: InventoryItem["status"]; label: string }[] = [
  { value: "in_stock", label: "In Stock" },
  { value: "low_stock", label: "Low Stock" },
  { value: "out_of_stock", label: "Out of Stock" },
];

export default function Inventory() {
  const [inventory, setInventory] = useState<InventoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState("");
  const [filterCategory, setFilterCategory] = useState("all");
  const [editingItem, setEditingItem] = useState<InventoryItem | null>(null);

  const loadInventoryData = async () => {
    try {
      setLoading(true);
      const response = await apiService.getInventory();
      setInventory(response.data);
    } catch (error) {
      console.error("Failed to load inventory data:", error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadInventoryData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const getStatusColor = (status: string) => {
    switch (status) {
      case "in_stock":
        return "text-green-700 bg-green-50";
      case "low_stock":
        return "text-yellow-700 bg-yellow-50";
      case "out_of_stock":
        return "text-red-700 bg-red-50";
      default:
        return "text-gray-700 bg-gray-50";
    }
  };

  const getStatusText = (status: string) => {
    switch (status) {
      case "in_stock":
        return "In Stock";
      case "low_stock":
        return "Low Stock";
      case "out_of_stock":
        return "Out of Stock";
      default:
        return status;
    }
  };

  const filteredInventory = inventory.filter((item) => {
    const matchesSearch =
      item.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
      item.category.toLowerCase().includes(searchTerm.toLowerCase());
    const matchesCategory =
      filterCategory === "all" ||
      item.category.toLowerCase() === filterCategory;
    return matchesSearch && matchesCategory;
  });

  const categories = [
    "all",
    ...Array.from(
      new Set(inventory.map((item) => item.category.toLowerCase())),
    ),
  ];

  const handleEditSaved = (updatedItem: InventoryItem) => {
    setInventory((prev) =>
      prev.map((item) => (item.id === updatedItem.id ? updatedItem : item)),
    );
    setEditingItem(null);
  };

  if (loading) {
    return <LoadingSpinner message="Loading inventory..." />;
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
            <Package className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-[#232323]">
              Inventory Management
            </h1>
            <p className="text-gray-500">Track and manage inventory levels</p>
          </div>
        </div>

        {/* Search and Filter */}
        <div className="flex gap-4">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
            <input
              type="text"
              placeholder="Search inventory..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="w-full pl-10 pr-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
            />
          </div>
          <div className="relative">
            <Filter className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
            <select
              value={filterCategory}
              onChange={(e) => setFilterCategory(e.target.value)}
              className="pl-10 pr-8 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white min-w-[160px]"
            >
              {categories.map((category) => (
                <option key={category} value={category}>
                  {category === "all"
                    ? "All Categories"
                    : category.charAt(0).toUpperCase() + category.slice(1)}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* Stats */}
      <div className="border-b border-gray-100 px-8 py-4">
        <div className="grid grid-cols-4 gap-6">
          <div className="text-center">
            <div className="text-2xl font-semibold text-[#232323]">
              {inventory.length}
            </div>
            <div className="text-sm text-gray-500">Total Items</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-semibold text-green-600">
              {inventory.filter((i) => i.status === "in_stock").length}
            </div>
            <div className="text-sm text-gray-500">In Stock</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-semibold text-yellow-600">
              {inventory.filter((i) => i.status === "low_stock").length}
            </div>
            <div className="text-sm text-gray-500">Low Stock</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-semibold text-red-600">
              {inventory.filter((i) => i.status === "out_of_stock").length}
            </div>
            <div className="text-sm text-gray-500">Out of Stock</div>
          </div>
        </div>
      </div>

      {/* Table View */}
      <div className="flex-1 overflow-y-auto">
        <table className="w-full">
          <thead className="bg-gray-50 sticky top-0 border-b border-gray-100">
            <tr>
              <th className="px-8 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Item
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Category
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Location
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Quantity
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Status
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Last Updated
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Actions
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {filteredInventory.map((item) => (
              <tr key={item.id} className="hover:bg-gray-50 transition-colors">
                <td className="px-8 py-4">
                  <div className="font-medium text-[#232323]">{item.name}</div>
                  <div className="text-sm text-gray-500">{item.id}</div>
                </td>
                <td className="px-6 py-4">
                  <span className="text-sm text-gray-700">{item.category}</span>
                </td>
                <td className="px-6 py-4">
                  <span className="text-sm text-gray-700">{item.location}</span>
                </td>
                <td className="px-6 py-4">
                  <div className="text-sm font-semibold text-[#232323]">
                    {item.quantity.toLocaleString()} {item.unit}
                  </div>
                </td>
                <td className="px-6 py-4">
                  <span
                    className={`inline-flex items-center px-3 py-1 rounded-lg text-xs font-medium ${getStatusColor(item.status)}`}
                  >
                    {getStatusText(item.status)}
                  </span>
                </td>
                <td className="px-6 py-4">
                  <span className="text-sm text-gray-600">
                    {new Date(item.lastUpdated).toLocaleDateString("en-US", {
                      month: "short",
                      day: "numeric",
                      year: "numeric",
                    })}
                  </span>
                </td>
                <td className="px-6 py-4">
                  <button
                    onClick={() => setEditingItem(item)}
                    className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-gray-200 text-gray-600 hover:bg-gray-100 transition-colors"
                  >
                    <Pencil className="w-3 h-3" />
                    Edit
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {filteredInventory.length === 0 && (
          <div className="text-center py-16 text-gray-500">
            <Package className="w-16 h-16 mx-auto mb-4 text-gray-300" />
            <p className="text-lg font-medium text-gray-400">
              No inventory items found
            </p>
            <p className="text-sm text-gray-400 mt-1">
              Try adjusting your search or filter criteria
            </p>
          </div>
        )}
      </div>

      {/* Edit Inventory Modal */}
      {editingItem && (
        <EditInventoryModal
          item={editingItem}
          onClose={() => setEditingItem(null)}
          onSaved={handleEditSaved}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Edit Inventory Modal                                                */
/* ------------------------------------------------------------------ */

interface EditInventoryModalProps {
  item: InventoryItem;
  onClose: () => void;
  onSaved: (updatedItem: InventoryItem) => void;
}

function EditInventoryModal({ item, onClose, onSaved }: EditInventoryModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    quantity: String(item.quantity),
    status: item.status,
    location: item.location,
  });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    const parsedQuantity = parseInt(form.quantity, 10);
    if (isNaN(parsedQuantity) || parsedQuantity < 0) {
      setError("Quantity must be a non-negative number.");
      return;
    }
    if (!form.location.trim()) {
      setError("Location is required.");
      return;
    }

    setError("");
    setSubmitting(true);
    try {
      const response = await apiService.updateInventoryItem(item.id, {
        quantity: parsedQuantity,
        status: form.status,
        location: form.location.trim(),
      });
      onSaved(response.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to update inventory item",
      );
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-[#232323]">
            Edit Inventory Item
          </h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {error && (
            <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">
              {error}
            </p>
          )}

          {/* Read-only item info */}
          <div className="bg-gray-50 px-3 py-2 rounded-lg">
            <p className="text-sm font-medium text-[#232323]">{item.name}</p>
            <p className="text-xs text-gray-500">
              {item.id} · {item.category}
            </p>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Quantity ({item.unit})
            </label>
            <input
              type="number"
              value={form.quantity}
              onChange={(e) => setForm({ ...form, quantity: e.target.value })}
              min="0"
              className={inputClass}
              required
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Status
            </label>
            <select
              value={form.status}
              onChange={(e) =>
                setForm({
                  ...form,
                  status: e.target.value as InventoryItem["status"],
                })
              }
              className={inputClass}
            >
              {INVENTORY_STATUSES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Location
            </label>
            <input
              type="text"
              value={form.location}
              onChange={(e) => setForm({ ...form, location: e.target.value })}
              placeholder="e.g. Warehouse A"
              className={inputClass}
              required
            />
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50"
              style={{ backgroundColor: "#232323" }}
            >
              {submitting ? "Saving..." : "Save Changes"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
