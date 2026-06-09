export function LoadingState() {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <div className="mb-3 h-4 w-40 animate-pulse rounded bg-slate-200" />
      <div className="mb-2 h-3 w-full animate-pulse rounded bg-slate-100" />
      <div className="mb-2 h-3 w-11/12 animate-pulse rounded bg-slate-100" />
      <div className="h-3 w-3/4 animate-pulse rounded bg-slate-100" />
      <p className="mt-4 text-sm text-slate-500">Cargando candidaturas...</p>
    </div>
  );
}
