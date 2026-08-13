"use client";

import { useRef, useState } from "react";

interface Props {
  /** Called when the user clicks “Analyse” with a valid CSV file. */
  onAnalyze: (file: File) => void;
  /** While ``true`` the “Analyse” button shows a spinner. */
  isLoading: boolean;
  /** An optional error message to display after a failed attempt. */
  errorMessage: string | null;
}

export function IncidentUploadCard({ onAnalyze, isLoading, errorMessage }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = e.target.files?.[0] ?? null;
    setLocalError(null);

    if (!selected) {
      setFile(null);
      return;
    }

    if (!selected.name.toLowerCase().endsWith(".csv")) {
      setLocalError("Only .csv files are accepted.");
      setFile(null);
      // Reset the input so the same invalid file can't be re-submitted
      // without re-selecting.
      if (inputRef.current) inputRef.current.value = "";
      return;
    }

    setFile(selected);
  };

  const handleClick = () => {
    if (isLoading) return;
    if (!file) return;
    onAnalyze(file);
  };

  const displayError = localError ?? errorMessage;

  return (
    <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6 shadow-[0_10px_40px_rgba(15,23,42,0.35)]">
      <h3 className="text-xl font-semibold text-white">Upload CSV</h3>
      <p className="mt-1 text-sm text-slate-400">
        Select a TrackFlow incident CSV file to analyse its metrics.
      </p>

      {/* ── Drop / click zone ── */}
      <div className="mt-6 flex flex-col items-center gap-4 rounded-xl border-2 border-dashed border-white/15 bg-slate-950/40 px-8 py-12">
        <svg
          className="h-10 w-10 text-slate-500"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5m-13.5-9L12 3m0 0 4.5 4.5M12 3v13.5"
          />
        </svg>

        {/* Selected file name */}
        {file ? (
          <p className="max-w-full truncate text-sm font-medium text-cyan-300">
            {file.name}
          </p>
        ) : (
          <>
            <p className="text-sm text-slate-400">
              <span
                className="cursor-pointer font-medium text-slate-300 hover:text-white"
                onClick={() => inputRef.current?.click()}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    inputRef.current?.click();
                  }
                }}
                role="button"
                tabIndex={0}
              >
                Click to select
              </span>{" "}
              or drag and drop
            </p>
            <p className="text-xs text-slate-500">.csv files only</p>
          </>
        )}

        {/* Hidden file input */}
        <input
          ref={inputRef}
          type="file"
          accept=".csv"
          className="hidden"
          onChange={handleFileChange}
        />

        {/* Action button */}
        {file && (
          <button
            onClick={handleClick}
            disabled={isLoading}
            className={`rounded-lg border px-5 py-2.5 text-sm font-medium shadow-sm transition ${
              isLoading
                ? "cursor-not-allowed border-cyan-800/40 bg-cyan-900/30 text-cyan-600"
                : "cursor-pointer border-cyan-600/40 bg-cyan-600/20 text-cyan-300 hover:bg-cyan-600/30"
            }`}
          >
            {isLoading ? (
              <span className="flex items-center gap-2">
                <svg
                  className="h-4 w-4 animate-spin"
                  viewBox="0 0 24 24"
                  fill="none"
                  aria-hidden="true"
                >
                  <circle
                    className="opacity-25"
                    cx="12"
                    cy="12"
                    r="10"
                    stroke="currentColor"
                    strokeWidth="4"
                  />
                  <path
                    className="opacity-75"
                    fill="currentColor"
                    d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"
                  />
                </svg>
                Analysing…
              </span>
            ) : (
              "Analyse CSV"
            )}
          </button>
        )}

        {/* Error message */}
        {displayError && (
          <p className="max-w-full text-center text-sm text-rose-400">
            {displayError}
          </p>
        )}
      </div>
    </section>
  );
}