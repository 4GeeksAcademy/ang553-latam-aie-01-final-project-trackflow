"use client";

import { useEffect, useRef } from "react";
import { usePathname } from "next/navigation";
import { useAuth } from "@/lib/AuthContext";
import { telemetryService } from "@/lib/telemetry";

type BackofficeSection =
  | "inventory_products"
  | "inventory_orders"
  | "suppliers"
  | "incidents";

const SECTION_PATHS: ReadonlyArray<{
  basePath: string;
  section: BackofficeSection;
}> = [
  {
    basePath: "/backoffice/inventory/products",
    section: "inventory_products",
  },
  {
    basePath: "/backoffice/inventory/orders",
    section: "inventory_orders",
  },
  { basePath: "/suppliers", section: "suppliers" },
  { basePath: "/incidents", section: "incidents" },
];

function getSectionForPathname(
  pathname: string | null,
): BackofficeSection | null {
  if (pathname === null) {
    return null;
  }

  const matchingPath = SECTION_PATHS.find(
    ({ basePath }) =>
      pathname === basePath || pathname.startsWith(`${basePath}/`),
  );

  return matchingPath?.section ?? null;
}

export function BackofficeSectionTracker(): null {
  const pathname = usePathname();
  const { user, isLoading } = useAuth();
  const lastEmittedSection = useRef<BackofficeSection | null>(null);

  useEffect(() => {
    if (isLoading) {
      return;
    }

    if (user === null) {
      lastEmittedSection.current = null;
      return;
    }

    const currentSection = getSectionForPathname(pathname);

    if (currentSection === null) {
      lastEmittedSection.current = null;
      return;
    }

    if (currentSection === lastEmittedSection.current) {
      return;
    }

    telemetryService.track("backoffice_section_entered", {
      section: currentSection,
    });
    lastEmittedSection.current = currentSection;
  }, [pathname, isLoading, user]);

  return null;
}
