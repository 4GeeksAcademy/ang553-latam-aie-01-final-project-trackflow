export const SUPPLIER_CATEGORIES = [
  "carrier_last_mile",
  "carrier_international",
  "warehouse_supplies",
  "packaging_materials",
  "reverse_logistics",
  "fleet_maintenance",
  "it_and_wms_software",
  "cleaning_and_facilities",
] as const;

export const SUPPLIER_COUNTRIES = ["USA", "Spain"] as const;

export const SUPPLIER_CURRENCIES = ["USD", "EUR"] as const;

export const SUPPLIER_STATUSES = ["active", "suspended"] as const;

export type SupplierCategory = (typeof SUPPLIER_CATEGORIES)[number];
export type SupplierCountry = (typeof SUPPLIER_COUNTRIES)[number];
export type SupplierCurrency = (typeof SUPPLIER_CURRENCIES)[number];
export type SupplierStatus = (typeof SUPPLIER_STATUSES)[number];

export interface Supplier {
  id: number;
  name: string;
  country: SupplierCountry;
  categories: SupplierCategory[];
  rate_per_shipment: number;
  currency: SupplierCurrency;
  status: SupplierStatus;
  updated_at: string;
  service_zone: string | null;
  contact_email: string | null;
  notes: string | null;
}

export interface SupplierCreate {
  name: string;
  country: SupplierCountry;
  categories: SupplierCategory[];
  rate_per_shipment: number;
  currency: SupplierCurrency;
  status: SupplierStatus;
  service_zone?: string;
  contact_email?: string;
  notes?: string;
}

export interface SupplierRateUpdate {
  rate_per_shipment: number;
}

export interface SupplierStatusUpdate {
  status: SupplierStatus;
}

export interface SupplierFilters {
  country?: SupplierCountry;
  category?: SupplierCategory;
}
