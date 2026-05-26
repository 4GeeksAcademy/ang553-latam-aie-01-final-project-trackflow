import {
  Carrier,
  Product,
  ProductCategory,
  WarehouseLocation,
} from "../types/models";

export function filterProductsByWarehouse(
  products: Product[],
  warehouse: WarehouseLocation
): Product[] {
  return products.filter((product: Product) => product.warehouse === warehouse);
}

export function filterProductsByCategory(
  products: Product[],
  category: ProductCategory
): Product[] {
  return products.filter((product: Product) => product.category === category);
}

export function filterLowStockProducts(products: Product[]): Product[] {
  return products.filter(
    (product: Product) => product.stockQuantity <= product.minStockThreshold
  );
}

export function sortProductsByStock(
  products: Product[],
  order: "asc" | "desc"
): Product[] {
  const productsCopy: Product[] = [...products];

  return productsCopy.sort((a: Product, b: Product) => {
    return order === "asc"
      ? a.stockQuantity - b.stockQuantity
      : b.stockQuantity - a.stockQuantity;
  });
}

export function sortCarriersByReliability(
  carriers: Carrier[],
  order: "asc" | "desc"
): Carrier[] {
  const carriersCopy: Carrier[] = [...carriers];

  return carriersCopy.sort((a: Carrier, b: Carrier) => {
    return order === "asc" ? a.onTimeRate - b.onTimeRate : b.onTimeRate - a.onTimeRate;
  });
}
