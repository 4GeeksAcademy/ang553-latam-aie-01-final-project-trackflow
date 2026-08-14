import Link from "next/link";

export function BackofficeHeader() {
  return (
    <header className="border-b border-white/10 bg-slate-950/70 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-5">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.3em] text-cyan-300">TrackFlow Internal</p>
          <h1 className="mt-2 text-2xl font-bold text-white">TrackFlow Backoffice</h1>
        </div>
        <nav className="flex items-center gap-6">
          <Link
            href="/"
            className="text-sm font-medium text-slate-300 transition-colors hover:text-white"
          >
            Dashboard
          </Link>
          <Link
            href="/incidents"
            className="text-sm font-medium text-slate-300 transition-colors hover:text-white"
          >
            Incidents
          </Link>
          <Link
            href="/suppliers"
            className="text-sm font-medium text-slate-300 transition-colors hover:text-white"
          >
            Suppliers
          </Link>
          <div className="rounded-full border border-cyan-400/30 bg-cyan-400/10 px-4 py-2 text-sm text-cyan-100">
            Dashboard operativo inicial
          </div>
        </nav>
      </div>
    </header>
  );
}
