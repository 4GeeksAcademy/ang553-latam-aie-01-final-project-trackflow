"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
} from "react";
import { telemetryService } from "@/lib/telemetry";

type InventoryWorkflow = "inbound" | "outbound";

interface WorkflowLifecycleState {
  workflow: InventoryWorkflow;
  startedAtMs: number;
  hasCreateAttempt: boolean;
  hadValidationError: boolean;
  completedSuccessfully: boolean;
  hasAbandoned: boolean;
}

interface InventoryWorkflowLifecycleValue {
  beginWorkflow: (workflow: InventoryWorkflow) => void;
  markValidationError: () => void;
  markCreateAttempt: () => void;
  markCompletedSuccessfully: () => void;
  finalizeAbandonment: () => void;
  clearWorkflow: () => void;
}

const InventoryWorkflowLifecycleContext =
  createContext<InventoryWorkflowLifecycleValue | null>(null);

function getNowMs(): number {
  if (typeof performance !== "undefined") {
    return performance.now();
  }
  return Date.now();
}

export function InventoryWorkflowLifecycleProvider({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement {
  const lifecycleRef = useRef<WorkflowLifecycleState | null>(null);

  const beginWorkflow = useCallback((workflow: InventoryWorkflow): void => {
    lifecycleRef.current = {
      workflow,
      startedAtMs: getNowMs(),
      hasCreateAttempt: false,
      hadValidationError: false,
      completedSuccessfully: false,
      hasAbandoned: false,
    };
  }, []);

  const markValidationError = useCallback((): void => {
    const lifecycle = lifecycleRef.current;
    if (
      lifecycle === null ||
      lifecycle.completedSuccessfully ||
      lifecycle.hasAbandoned
    ) {
      return;
    }

    lifecycle.hadValidationError = true;
  }, []);

  const markCreateAttempt = useCallback((): void => {
    const lifecycle = lifecycleRef.current;
    if (
      lifecycle === null ||
      lifecycle.completedSuccessfully ||
      lifecycle.hasAbandoned
    ) {
      return;
    }

    lifecycle.hasCreateAttempt = true;
  }, []);

  const markCompletedSuccessfully = useCallback((): void => {
    const lifecycle = lifecycleRef.current;
    if (lifecycle === null || lifecycle.hasAbandoned) {
      return;
    }

    lifecycle.completedSuccessfully = true;
  }, []);

  const finalizeAbandonment = useCallback((): void => {
    const lifecycle = lifecycleRef.current;
    if (lifecycle === null || lifecycle.hasAbandoned) {
      return;
    }

    if (lifecycle.completedSuccessfully) {
      lifecycleRef.current = null;
      return;
    }

    lifecycle.hasAbandoned = true;
    telemetryService.track("inventory_workflow_abandoned", {
      workflow: lifecycle.workflow,
      abandonment_stage: lifecycle.hasCreateAttempt
        ? "after_submit"
        : "before_submit",
      had_validation_error: lifecycle.hadValidationError,
      duration_ms: Math.max(0, getNowMs() - lifecycle.startedAtMs),
    });
    lifecycleRef.current = null;
  }, []);

  const clearWorkflow = useCallback((): void => {
    lifecycleRef.current = null;
  }, []);

  useEffect(() => {
    const handlePageHide = (): void => {
      finalizeAbandonment();
    };

    window.addEventListener("pagehide", handlePageHide, true);
    return () => {
      window.removeEventListener("pagehide", handlePageHide, true);
    };
  }, [finalizeAbandonment]);

  const value = useMemo<InventoryWorkflowLifecycleValue>(
    () => ({
      beginWorkflow,
      markValidationError,
      markCreateAttempt,
      markCompletedSuccessfully,
      finalizeAbandonment,
      clearWorkflow,
    }),
    [
      beginWorkflow,
      markValidationError,
      markCreateAttempt,
      markCompletedSuccessfully,
      finalizeAbandonment,
      clearWorkflow,
    ],
  );

  return (
    <InventoryWorkflowLifecycleContext.Provider value={value}>
      {children}
    </InventoryWorkflowLifecycleContext.Provider>
  );
}

export function useInventoryWorkflowLifecycle(): InventoryWorkflowLifecycleValue {
  const context = useContext(InventoryWorkflowLifecycleContext);
  if (context === null) {
    throw new Error(
      "useInventoryWorkflowLifecycle must be used within an InventoryWorkflowLifecycleProvider",
    );
  }

  return context;
}
