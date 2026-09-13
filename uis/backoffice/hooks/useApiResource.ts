"use client";

import { useCallback, useEffect, useState } from "react";

type ApiResourceLoader<T> = () => Promise<T>;

interface ApiResource<T> {
  data: T;
  isLoading: boolean;
  error: string | null;
  reload: () => void;
}

export function useApiResource<T>(
  loader: ApiResourceLoader<T>,
  fallbackMessage: string,
  initialData: T,
): ApiResource<T> {
  const [data, setData] = useState<T>(initialData);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const reload = useCallback(() => {
    setReloadKey((current) => current + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      setIsLoading(true);
      setError(null);

      try {
        const nextData = await loader();
        if (!cancelled) {
          setData(nextData);
        }
      } catch (loadError: unknown) {
        if (!cancelled) {
          setError(
            loadError instanceof Error ? loadError.message : fallbackMessage,
          );
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, [fallbackMessage, loader, reloadKey]);

  return { data, isLoading, error, reload };
}
