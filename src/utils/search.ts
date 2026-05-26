import { Product, Shipment } from "../types/models";

export function findProductBySKU(
  products: Product[],
  sku: string
): Product | null {
  const normalizedSku: string = sku.toLowerCase();

  for (const product of products) {
    if (product.sku.toLowerCase() === normalizedSku) {
      return product;
    }
  }

  return null;
}

export function findShipmentById(
  shipments: Shipment[],
  id: string
): Shipment | null {
  for (const shipment of shipments) {
    if (shipment.id === id) {
      return shipment;
    }
  }

  return null;
}

export function binarySearchProductByWeight(
  sortedProducts: Product[],
  targetWeight: number
): number {
  let left: number = 0;
  let right: number = sortedProducts.length - 1;

  while (left <= right) {
    const mid: number = Math.floor((left + right) / 2);
    const currentWeight: number = sortedProducts[mid].weightKg;

    if (currentWeight === targetWeight) {
      return mid;
    }

    if (currentWeight < targetWeight) {
      left = mid + 1;
    } else {
      right = mid - 1;
    }
  }

  return -1;
}
