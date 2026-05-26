import {
  Carrier,
  Product,
  ProductCategory,
  Shipment,
  ShipmentStatus,
} from "../types/models";

function roundToTwoDecimals(value: number): number {
  return Math.round(value * 100) / 100;
}

export function calculateShippingCost(
  shipment: Shipment,
  product: Product,
  carrier: Carrier
): number {
  const base: number = carrier.baseRateUSD;
  const weightCost: number =
    product.weightKg * carrier.ratePerKgUSD * shipment.quantity;
  const distanceCost: number =
    shipment.destination.distanceKm * carrier.ratePerKmUSD;
  const subtotal: number = base + weightCost + distanceCost;

  let priorityMultiplier: number = 1;

  if (shipment.priority === "Express") {
    priorityMultiplier = 1.3;
  } else if (shipment.priority === "Same-day") {
    priorityMultiplier = 1.6;
  }

  return roundToTwoDecimals(subtotal * priorityMultiplier);
}

export function scoreCarrierForShipment(
  carrier: Carrier,
  shipment: Shipment,
  product: Product
): number {
  let score: number = 0;

  if (carrier.operatesIn.includes(shipment.destination.country)) {
    score += 20;
  }

  const shipmentWeight: number = product.weightKg * shipment.quantity;
  if (shipmentWeight <= carrier.maxWeightKg) {
    score += 20;
  }

  if (carrier.acceptsPriority.includes(shipment.priority)) {
    score += 15;
  }

  if (!product.isFragile || carrier.handlesFragile) {
    score += 15;
  }

  score += carrier.onTimeRate * 0.3;

  return roundToTwoDecimals(score);
}

export function selectBestCarrier(
  carriers: Carrier[],
  shipment: Shipment,
  product: Product
): { carrier: Carrier; score: number; cost: number } | null {
  let bestOption: { carrier: Carrier; score: number; cost: number } | null = null;

  for (const carrier of carriers) {
    const score: number = scoreCarrierForShipment(carrier, shipment, product);
    const cost: number = calculateShippingCost(shipment, product, carrier);

    if (score < 50) {
      continue;
    }

    if (bestOption === null || cost < bestOption.cost) {
      bestOption = { carrier, score, cost };
    }
  }

  return bestOption;
}

export function countProductsByCategory(
  products: Product[]
): Record<ProductCategory, number> {
  const counts: Record<ProductCategory, number> = {
    Fashion: 0,
    Electronics: 0,
    Cosmetics: 0,
    Home: 0,
    Other: 0,
  };

  for (const product of products) {
    counts[product.category] += 1;
  }

  return counts;
}

export function calculateTotalInventoryValue(products: Product[]): number {
  let totalValue: number = 0;

  for (const product of products) {
    totalValue += product.stockQuantity * product.unitCostUSD;
  }

  return roundToTwoDecimals(totalValue);
}

export function calculateAverageShipmentDistance(shipments: Shipment[]): number {
  if (shipments.length === 0) {
    return 0;
  }

  let totalDistance: number = 0;

  for (const shipment of shipments) {
    totalDistance += shipment.destination.distanceKm;
  }

  return roundToTwoDecimals(totalDistance / shipments.length);
}

export function groupShipmentsByStatus(
  shipments: Shipment[]
): Record<ShipmentStatus, Shipment[]> {
  const grouped: Record<ShipmentStatus, Shipment[]> = {
    Pending: [],
    Assigned: [],
    "In transit": [],
    Delivered: [],
    Failed: [],
  };

  for (const shipment of shipments) {
    grouped[shipment.status].push(shipment);
  }

  return grouped;
}

export function findTopCarriers(
  shipments: Shipment[],
  topN: number
): Array<{ carrier: string; count: number }> {
  if (topN <= 0 || shipments.length === 0) {
    return [];
  }

  const carrierUsage: Record<string, number> = {};

  for (const shipment of shipments) {
    if (shipment.carrier === null) {
      continue;
    }

    carrierUsage[shipment.carrier] = (carrierUsage[shipment.carrier] ?? 0) + 1;
  }

  return Object.entries(carrierUsage)
    .map(([carrier, count]: [string, number]) => ({ carrier, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, topN);
}
