import type { Carrier, Product, Shipment } from "../../../src/types/models";
import { filterLowStockProducts, sortProductsByStock } from "../../../src/utils/collections";
import { findProductBySKU } from "../../../src/utils/search";
import {
  calculateTotalInventoryValue,
  countProductsByCategory,
  selectBestCarrier,
} from "../../../src/utils/transformations";
import {
  validateCarrier,
  validateProduct,
  validateShipment,
} from "../../../src/utils/validations";

const sampleProducts: Product[] = [
  {
    sku: "TF-FSN-001",
    name: "Chaqueta Softshell Urban",
    category: "Fashion",
    weightKg: 1.2,
    dimensions: { lengthCm: 40, widthCm: 28, heightCm: 8 },
    warehouse: "Los Angeles",
    stockQuantity: 42,
    minStockThreshold: 15,
    unitCostUSD: 18.5,
    isFragile: false,
    status: "Active",
  },
  {
    sku: "TF-ELC-204",
    name: "Auriculares NoiseGuard Pro",
    category: "Electronics",
    weightKg: 3.4,
    dimensions: { lengthCm: 24, widthCm: 18, heightCm: 12 },
    warehouse: "Los Angeles",
    stockQuantity: 8,
    minStockThreshold: 10,
    unitCostUSD: 120,
    isFragile: false,
    status: "Low stock",
  },
  {
    sku: "TF-COS-088",
    name: "Kit Dermacare Travel",
    category: "Cosmetics",
    weightKg: 0.6,
    dimensions: { lengthCm: 16, widthCm: 12, heightCm: 10 },
    warehouse: "Zaragoza",
    stockQuantity: 5,
    minStockThreshold: 12,
    unitCostUSD: 22,
    isFragile: true,
    status: "Low stock",
  },
];

const sampleCarriers: Carrier[] = [
  {
    id: "mrw-es",
    name: "MRW Iberia",
    operatesIn: ["Spain"],
    baseRateUSD: 10,
    ratePerKgUSD: 2.4,
    ratePerKmUSD: 0.018,
    avgDeliveryDays: 2,
    onTimeRate: 88,
    maxWeightKg: 20,
    handlesFragile: false,
    acceptsPriority: ["Standard", "Express"],
  },
  {
    id: "dhl-global",
    name: "DHL Global Express",
    operatesIn: ["United States", "Spain"],
    baseRateUSD: 12,
    ratePerKgUSD: 2.8,
    ratePerKmUSD: 0.02,
    avgDeliveryDays: 2,
    onTimeRate: 93,
    maxWeightKg: 25,
    handlesFragile: true,
    acceptsPriority: ["Standard", "Express", "Same-day"],
  },
  {
    id: "ups-us",
    name: "UPS Cross Border",
    operatesIn: ["United States", "Spain"],
    baseRateUSD: 15,
    ratePerKgUSD: 3.1,
    ratePerKmUSD: 0.019,
    avgDeliveryDays: 3,
    onTimeRate: 91,
    maxWeightKg: 30,
    handlesFragile: true,
    acceptsPriority: ["Standard", "Express"],
  },
];

const sampleShipment: Shipment = {
  id: "SHP-1001",
  sku: "TF-ELC-204",
  quantity: 2,
  origin: "Los Angeles",
  destination: {
    city: "Zaragoza",
    country: "Spain",
    postalCode: "50001",
    distanceKm: 16000,
  },
  priority: "Express",
  declaredValueUSD: 350,
  carrier: null,
  status: "Pending",
  createdAt: new Date("2026-07-01T09:00:00Z"),
};

export interface OperationalSnapshot {
  totalInventoryValue: number;
  lowStockProducts: Product[];
  categoryCounts: Record<Product["category"], number>;
  bestCarrierRecommendation: { name: string; score: number; cost: number } | null;
  validationSummary: {
    validProducts: number;
    totalProducts: number;
    validCarriers: number;
    totalCarriers: number;
    shipmentValid: boolean;
  };
}

export function buildOperationalSnapshot(): OperationalSnapshot {
  // Reutiliza la lógica de Hito 2 directamente desde src para evitar drift entre UI y core de negocio.
  const totalInventoryValue = calculateTotalInventoryValue(sampleProducts);
  const lowStockProducts = sortProductsByStock(filterLowStockProducts(sampleProducts), "asc");
  const categoryCounts = countProductsByCategory(sampleProducts);

  const shipmentProduct = findProductBySKU(sampleProducts, sampleShipment.sku);
  const bestCarrier = shipmentProduct
    ? selectBestCarrier(sampleCarriers, sampleShipment, shipmentProduct)
    : null;

  const validationSummary = {
    validProducts: sampleProducts.filter((product) => validateProduct(product).valid).length,
    totalProducts: sampleProducts.length,
    validCarriers: sampleCarriers.filter((carrier) => validateCarrier(carrier).valid).length,
    totalCarriers: sampleCarriers.length,
    shipmentValid: validateShipment(sampleShipment).valid,
  };

  return {
    totalInventoryValue,
    lowStockProducts,
    categoryCounts,
    bestCarrierRecommendation: bestCarrier
      ? {
          name: bestCarrier.carrier.name,
          score: bestCarrier.score,
          cost: bestCarrier.cost,
        }
      : null,
    validationSummary,
  };
}
