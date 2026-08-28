import {
  validateCarrier,
  validateProduct,
  validateShipment,
} from "../src/utils/validations";
import type { Carrier, Product, Shipment } from "../src/types/models";

function buildValidProduct(overrides: Partial<Product> = {}): Product {
  return {
    sku: "SKU-100",
    name: "Thermal Bottle",
    category: "Home",
    weightKg: 1.2,
    dimensions: {
      lengthCm: 15,
      widthCm: 8,
      heightCm: 30,
    },
    warehouse: "Los Angeles",
    stockQuantity: 120,
    minStockThreshold: 15,
    unitCostUSD: 18.5,
    isFragile: false,
    status: "Active",
    ...overrides,
  };
}

function buildValidShipment(overrides: Partial<Shipment> = {}): Shipment {
  return {
    id: "SHP-001",
    sku: "SKU-100",
    quantity: 3,
    origin: "Zaragoza",
    destination: {
      city: "Madrid",
      country: "Spain",
      postalCode: "28001",
      distanceKm: 620,
    },
    priority: "Standard",
    declaredValueUSD: 55,
    carrier: "carrier-01",
    status: "Pending",
    createdAt: new Date("2026-01-10T10:00:00.000Z"),
    ...overrides,
  };
}

function buildValidCarrier(overrides: Partial<Carrier> = {}): Carrier {
  return {
    id: "carrier-01",
    name: "Iberia Express Logistics",
    operatesIn: ["Spain"],
    baseRateUSD: 5,
    ratePerKgUSD: 1.5,
    ratePerKmUSD: 0.08,
    avgDeliveryDays: 2,
    onTimeRate: 96,
    maxWeightKg: 80,
    handlesFragile: true,
    acceptsPriority: ["Standard", "Express"],
    ...overrides,
  };
}

describe("validateProduct", () => {
  it("TS-PRODUCT-HP-01: acepta producto completamente valido", () => {
    const result = validateProduct(buildValidProduct());

    expect(result.valid).toBe(true);
    expect(result.errors).toEqual([]);
  });

  it("TS-PRODUCT-FAIL-01: rechaza sku vacio", () => {
    const invalidProduct = buildValidProduct({ sku: "   " });

    const result = validateProduct(invalidProduct);

    expect(result.valid).toBe(false);
    expect(result.errors).toContain("sku must not be empty");
  });

  it("TS-PRODUCT-EDGE-01: respeta boundary de weightKg > 0 y <= 100", () => {
    const maxAllowedWeight = buildValidProduct({ weightKg: 100 });
    const aboveMaxWeight = buildValidProduct({ weightKg: 100.01 });

    const validResult = validateProduct(maxAllowedWeight);
    const invalidResult = validateProduct(aboveMaxWeight);

    expect(validResult.valid).toBe(true);
    expect(validResult.errors).toEqual([]);
    expect(invalidResult.valid).toBe(false);
    expect(invalidResult.errors).toContain("weightKg must be > 0 and <= 100");
  });
});

describe("validateShipment", () => {
  it("TS-SHIPMENT-HP-01: acepta shipment valido", () => {
    const result = validateShipment(buildValidShipment());

    expect(result.valid).toBe(true);
    expect(result.errors).toEqual([]);
  });

  it("TS-SHIPMENT-FAIL-01: rechaza quantity igual a 0", () => {
    const invalidShipment = buildValidShipment({ quantity: 0 });

    const result = validateShipment(invalidShipment);

    expect(result.valid).toBe(false);
    expect(result.errors).toContain("quantity must be > 0");
  });

  it("TS-SHIPMENT-EDGE-01: acepta distanceKm 0 y rechaza distanceKm negativo", () => {
    const zeroDistanceShipment = buildValidShipment({
      destination: {
        city: "Barcelona",
        country: "Spain",
        postalCode: "08001",
        distanceKm: 0,
      },
    });
    const negativeDistanceShipment = buildValidShipment({
      destination: {
        city: "Barcelona",
        country: "Spain",
        postalCode: "08001",
        distanceKm: -1,
      },
    });

    const validResult = validateShipment(zeroDistanceShipment);
    const invalidResult = validateShipment(negativeDistanceShipment);

    expect(validResult.valid).toBe(true);
    expect(validResult.errors).toEqual([]);
    expect(invalidResult.valid).toBe(false);
    expect(invalidResult.errors).toContain(
      "destination.distanceKm must be >= 0"
    );
  });
});

describe("validateCarrier", () => {
  it("TS-CARRIER-HP-01: acepta carrier valido", () => {
    const result = validateCarrier(buildValidCarrier());

    expect(result.valid).toBe(true);
    expect(result.errors).toEqual([]);
  });

  it("TS-CARRIER-FAIL-01: rechaza onTimeRate fuera de rango", () => {
    const invalidCarrier = buildValidCarrier({ onTimeRate: 101 });

    const result = validateCarrier(invalidCarrier);

    expect(result.valid).toBe(false);
    expect(result.errors).toContain("onTimeRate must be between 0 and 100");
  });

  it("TS-CARRIER-EDGE-01: acepta boundaries 0 y 100 en onTimeRate", () => {
    const minBoundaryCarrier = buildValidCarrier({ onTimeRate: 0 });
    const maxBoundaryCarrier = buildValidCarrier({ onTimeRate: 100 });

    const minResult = validateCarrier(minBoundaryCarrier);
    const maxResult = validateCarrier(maxBoundaryCarrier);

    expect(minResult.valid).toBe(true);
    expect(minResult.errors).toEqual([]);
    expect(maxResult.valid).toBe(true);
    expect(maxResult.errors).toEqual([]);
  });

  it("TS-CARRIER-EDGE-02: rechaza valores fuera de boundary en onTimeRate", () => {
    const belowBoundaryCarrier = buildValidCarrier({ onTimeRate: -1 });
    const aboveBoundaryCarrier = buildValidCarrier({ onTimeRate: 101 });

    const belowResult = validateCarrier(belowBoundaryCarrier);
    const aboveResult = validateCarrier(aboveBoundaryCarrier);

    expect(belowResult.valid).toBe(false);
    expect(belowResult.errors).toContain("onTimeRate must be between 0 and 100");
    expect(aboveResult.valid).toBe(false);
    expect(aboveResult.errors).toContain("onTimeRate must be between 0 and 100");
  });
});