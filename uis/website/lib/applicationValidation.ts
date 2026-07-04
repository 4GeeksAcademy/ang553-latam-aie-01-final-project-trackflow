export const SIMPLE_FIELD_NAMES = [
  "companyName",
  "contactPerson",
  "corporateEmail",
  "phone",
  "website",
  "operationCountry",
  "productType",
  "monthlyVolume",
] as const;

export type SimpleFieldName = (typeof SIMPLE_FIELD_NAMES)[number];

export function isSimpleFieldName(value: string): value is SimpleFieldName {
  return SIMPLE_FIELD_NAMES.includes(value as SimpleFieldName);
}

export function validateSimpleField(field: SimpleFieldName, value: string): string {
  const trimmedValue = value.trim();

  switch (field) {
    case "companyName": {
      return trimmedValue.length >= 2
        ? ""
        : "El nombre de la empresa debe tener al menos 2 caracteres";
    }
    case "contactPerson": {
      const words = trimmedValue.split(/\s+/).filter(Boolean);
      const isValid = words.length >= 2 && words.every((word) => word.length >= 2);
      return isValid ? "" : "Ingresa nombre y apellido del contacto";
    }
    case "corporateEmail": {
      const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
      const isValid = trimmedValue !== "" && emailPattern.test(trimmedValue);
      return isValid ? "" : "Ingresa un email corporativo válido (ejemplo: nombre@empresa.com)";
    }
    case "phone": {
      const phonePattern = /^\+\d[\d\s().-]{6,}$/;
      const isValid = trimmedValue !== "" && phonePattern.test(trimmedValue);
      return isValid ? "" : "El teléfono debe incluir código de país (ejemplo: +1 213 555 0147)";
    }
    case "website": {
      if (trimmedValue === "") {
        return "";
      }
      return /^https?:\/\/.+/i.test(trimmedValue) ? "" : "Si incluyes sitio web, debe ser una URL válida";
    }
    case "operationCountry": {
      return trimmedValue !== "" ? "" : "Selecciona el país de operación principal";
    }
    case "productType": {
      return trimmedValue !== "" ? "" : "Selecciona el tipo de producto que manejas";
    }
    case "monthlyVolume": {
      return trimmedValue !== "" ? "" : "Selecciona el volumen mensual estimado";
    }
    default:
      return "";
  }
}

export function validateServicesInterest(value: string[]): string {
  return value.length > 0 ? "" : "Selecciona al menos un servicio de interés";
}

export function validateCurrent3PL(value: string): string {
  return value.trim() !== "" ? "" : "Indica si actualmente trabajas con otro proveedor logístico";
}

export function validatePrivacyPolicy(value: boolean): string {
  return value ? "" : "Debes aceptar la política de privacidad para continuar";
}

const LOW_VOLUME_RELEVANT_PRODUCTS = ["Moda", "Electrónica", "Cosmética"] as const;

export function shouldShowLowVolumeWarning(monthlyVolume: string, productType: string): boolean {
  // Regla de negocio heredada del formulario original de Hito 1.
  return monthlyVolume === "0-100" && LOW_VOLUME_RELEVANT_PRODUCTS.includes(productType as (typeof LOW_VOLUME_RELEVANT_PRODUCTS)[number]);
}
