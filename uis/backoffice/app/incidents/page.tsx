"use client";

import { useState, useCallback } from "react";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { IncidentUploadCard } from "@/components/incidents/IncidentUploadCard";
import { IncidentSummary } from "@/components/incidents/IncidentSummary";
import { analyzeIncidents, downloadResultsCsv, ApiError } from "@/lib/incidentsApi";
import type { IncidentAnalysisResult } from "@/types/incidents";

export default function IncidentsPage() {
  const [result, setResult] = useState<IncidentAnalysisResult | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isDownloading, setIsDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const handleAnalyze = useCallback(async (file: File) => {
    setIsLoading(true);
    setError(null);
    setResult(null);

    try {
      const data = await analyzeIncidents(file);
      console.log("✅ Analysis result received:", data.total_records, "records");
      setResult(data);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else if (err instanceof Error) {
        // Unexpected errors — show a user-safe message, log details.
        console.error("Unexpected error during analysis:", err);
        setError("An unexpected error occurred. Please try again.");
      } else {
        setError("An unexpected error occurred. Please try again.");
      }
    } finally {
      setIsLoading(false);
    }
  }, []);

  const handleDownload = useCallback(async () => {
    setIsDownloading(true);
    setDownloadError(null);

    try {
      const blob = await downloadResultsCsv();

      // Trigger browser download
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "results.csv";
      document.body.appendChild(anchor);
      anchor.click();

      // Cleanup
      document.body.removeChild(anchor);
      URL.revokeObjectURL(url);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setDownloadError(err.message);
      } else if (err instanceof Error) {
        console.error("Unexpected error during download:", err);
        setDownloadError("An unexpected error occurred while downloading.");
      } else {
        setDownloadError("An unexpected error occurred while downloading.");
      }
    } finally {
      setIsDownloading(false);
    }
  }, []);

  return (
    <div className="min-h-screen">
      <BackofficeHeader />
      <main className="mx-auto max-w-7xl px-6 py-10">
        {/* ── Page header ── */}
        <div>
          <p className="text-sm font-semibold uppercase tracking-[0.25em] text-cyan-300">
            Incident analysis
          </p>
          <h2 className="mt-3 text-3xl font-bold text-white">Incident Analysis</h2>
          <p className="mt-3 max-w-3xl text-slate-300">
            Upload a TrackFlow incident CSV file to validate records, view aggregate
            metrics, and export the results. All data is processed server-side and no
            individual records are retained.
          </p>
        </div>

        {/* ── Upload card ── */}
        <div className="mt-8">
          <IncidentUploadCard
            onAnalyze={handleAnalyze}
            isLoading={isLoading}
            errorMessage={error}
          />
        </div>

        {/* ── Confidence indicator when result arrives ── */}
        {result && !isLoading && (
          <div className="mt-4 rounded-lg border border-emerald-800/30 bg-emerald-950/30 px-4 py-3 text-sm text-emerald-300">
            Analysis complete. JSON received from backend successfully.
          </div>
        )}

        {/* ── Summary / placeholders ── */}
        <div className="mt-10">
          <IncidentSummary
            result={result}
            onDownload={handleDownload}
            isDownloading={isDownloading}
          />

        {/* ── Download error ── */}
        {downloadError && (
          <div className="mt-4 rounded-lg border border-rose-800/30 bg-rose-950/30 px-4 py-3 text-sm text-rose-300">
            Download error: {downloadError}
          </div>
        )}
        </div>
      </main>
    </div>
  );
}