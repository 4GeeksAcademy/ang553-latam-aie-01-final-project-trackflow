/**
 * Types for TrackFlow Backoffice Inventory Management.
 *
 * Matches the backend schemas defined in
 * ``services/api/inventory_schemas.py`` — field names, casing, and
 * nullability reflect the real Pydantic models.
 *
 * @see https://github.com/4GeeksAcademy/ang553-latam-aie-01-final-project-trackflow
 */

/* ── Domain enumerations ──────────────────────────────────────────────────── */

/** Valid product categories in TrackFlow. */
export const CATEGORIES = ["fashion", "electronics", "cosmetics"] as const;
export type Category = (typeof CATEGORIES)[number];

/** Valid warehouse locations in TrackFlow. */
export const WAREHOUSES = ["LA", "ZGZ"] as const;
export type Warehouse = (typeof WAREHOUSES)[number];

/** Valid stock exit types in TrackFlow. */
export const EXIT_TYPES = ["dispatch", "loss"] as const;
export type ExitType = (typeof EXIT_TYPES)[number];

/** Valid movement types returned by the orders endpoint. */
export const MOVEMENT_TYPES = ["inbound", "outbound"] as const;
export type MovementType = (typeof MOVEMENT_TYPES)[number];

/* ── SKU schemas ─────────────────────────────────────────────────────────── */

/** Public representation of a SKU, including computed stock. */
export interface SKUResponse {
  id: number;
  name: string;
  sku: string;
  client_name: string;
  category: Category;
  warehouse: Warehouse;
  current_stock: number;
}

/* ── StockEntry schemas (inbound) ─────────────────────────────────────────── */

/** Payload for recording a stock-in movement. */
export interface StockEntryCreate {
  sku_id: number;
  quantity: number;
  reference: string;
  warehouse: Warehouse;
}

/** Public representation of a stock-in movement. */
export interface StockEntryResponse {
  id: number;
  sku_id: number;
  quantity: number;
  reference: string;
  warehouse: Warehouse;
  created_at: string;
  user_uuid: string;
}

/* ── StockExit schemas (outbound) ─────────────────────────────────────────── */

/** Payload for recording a stock-out movement. */
export interface StockExitCreate {
  sku_id: number;
  quantity: number;
  exit_type: ExitType;
  tracking_number: string | null;
  warehouse: Warehouse;
}

/** Public representation of a stock-out movement. */
export interface StockExitResponse {
  id: number;
  sku_id: number;
  quantity: number;
  exit_type: ExitType;
  tracking_number: string | null;
  warehouse: Warehouse;
  created_at: string;
  user_uuid: string;
}

/* ── Order (combined) schemas ─────────────────────────────────────────────── */

/** Lightweight SKU representation included inside order responses. */
export interface SKUSummary {
  id: number;
  name: string;
  sku: string;
  client_name: string;
  category: Category;
  warehouse: Warehouse;
}

/** Unified response for a stock movement (inbound or outbound). */
export interface InventoryOrderResponse {
  id: number;
  movement_type: MovementType;
  sku_id: number;
  quantity: number;
  warehouse: Warehouse;
  created_at: string;
  user_uuid: string;
  sku: SKUSummary;
  reference: string | null;
  exit_type: ExitType | null;
  tracking_number: string | null;
}