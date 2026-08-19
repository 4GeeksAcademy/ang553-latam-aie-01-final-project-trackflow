"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/AuthContext";

export function BackofficeHeader() {
  const router = useRouter();
  const { logout } = useAuth();

  const handleLogout = () => {
    logout();
    router.replace("/login");
  };

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
          <button
            type="button"
            onClick={handleLogout}
            className="rounded-full border border-rose-400/30 bg-rose-400/10 px-4 py-2 text-sm text-rose-200 transition-colors hover:bg-rose-400/20 hover:text-rose-100"
          >
            Logout
          </button>
        </nav>
      </div>
    </header>
  );
}
