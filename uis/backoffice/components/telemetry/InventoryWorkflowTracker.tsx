"use client";

import { useCallback, useEffect, useRef } from "react";
import { usePathname } from "next/navigation";
import { useAuth } from "@/lib/AuthContext";
import { telemetryService } from "@/lib/telemetry";
import { useInventoryWorkflowLifecycle } from "@/components/telemetry/InventoryWorkflowLifecycle";

type InventoryWorkflow = "inbound" | "outbound";

const WORKFLOW_PATHS: ReadonlyArray<{
  basePath: string;
  workflow: InventoryWorkflow;
}> = [
  {
    basePath: "/backoffice/inventory/orders/inbound",
    workflow: "inbound",
  },
  {
    basePath: "/backoffice/inventory/orders/outbound",
    workflow: "outbound",
  },
];

function getWorkflowForPathname(
  pathname: string | null,
): InventoryWorkflow | null {
  if (pathname === null) {
    return null;
  }

  const matchingPath = WORKFLOW_PATHS.find(
    ({ basePath }) =>
      pathname === basePath || pathname.startsWith(`${basePath}/`),
  );

  return matchingPath?.workflow ?? null;
}

export function InventoryWorkflowTracker(): null {
  const pathname = usePathname();
  const { user, isLoading } = useAuth();
  const { beginWorkflow, finalizeAbandonment, clearWorkflow } =
    useInventoryWorkflowLifecycle();
  const lastEmittedWorkflow = useRef<InventoryWorkflow | null>(null);

  const startWorkflow = useCallback(
    (workflow: InventoryWorkflow): void => {
      beginWorkflow(workflow);
      telemetryService.track("inventory_workflow_started", {
        workflow,
      });
      lastEmittedWorkflow.current = workflow;
    },
    [beginWorkflow],
  );

  useEffect(() => {
    if (isLoading) {
      return;
    }

    if (user === null) {
      lastEmittedWorkflow.current = null;
      clearWorkflow();
      return;
    }

    const currentWorkflow = getWorkflowForPathname(pathname);

    if (currentWorkflow === null) {
      if (lastEmittedWorkflow.current !== null) {
        finalizeAbandonment();
      }
      lastEmittedWorkflow.current = null;
      return;
    }

    if (currentWorkflow === lastEmittedWorkflow.current) {
      return;
    }

    if (lastEmittedWorkflow.current !== null) {
      finalizeAbandonment();
    }

    startWorkflow(currentWorkflow);
  }, [
    pathname,
    isLoading,
    user,
    finalizeAbandonment,
    clearWorkflow,
    startWorkflow,
  ]);

  useEffect(() => {
    const handlePageShow = (event: PageTransitionEvent): void => {
      if (!event.persisted) {
        return;
      }

      lastEmittedWorkflow.current = null;

      if (isLoading) {
        return;
      }

      if (user === null) {
        clearWorkflow();
        return;
      }

      const currentWorkflow = getWorkflowForPathname(pathname);
      if (currentWorkflow === null) {
        clearWorkflow();
        return;
      }

      startWorkflow(currentWorkflow);
    };

    window.addEventListener("pageshow", handlePageShow);
    return () => {
      window.removeEventListener("pageshow", handlePageShow);
    };
  }, [isLoading, user, pathname, clearWorkflow, startWorkflow]);

  return null;
}
