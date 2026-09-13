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
    <header className="border-b border-white/10 bg-slate-950">
      <div className="mx-auto flex max-w-7xl flex-col items-start gap-5 px-6 py-5 md:flex-row md:items-center md:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.3em] text-cyan-300">TrackFlow Internal</p>
          <h1 className="mt-2 text-2xl font-bold text-white">TrackFlow Backoffice</h1>
        </div>
        <nav className="flex w-full flex-wrap items-center gap-x-4 gap-y-3 md:w-auto md:gap-6">
          <Link
            href="/"
            className="text-sm font-medium text-slate-100 transition-colors hover:text-white"
          >
            Dashboard
          </Link>
          <Link
            href="/incidents"
            className="text-sm font-medium text-slate-100 transition-colors hover:text-white"
          >
            Incidents
          </Link>
          <Link
            href="/suppliers"
            className="text-sm font-medium text-slate-100 transition-colors hover:text-white"
          >
            Suppliers
          </Link>
          <Link
            href="/backoffice/inventory/products"
            className="text-sm font-medium text-slate-100 transition-colors hover:text-white"
          >
            Inventory
          </Link>
          <Link
            href="/backoffice/inventory/orders"
            className="text-sm font-medium text-slate-100 transition-colors hover:text-white"
          >
            Inventory History
          </Link>
          <Link
            href="/account/profile"
            className="text-sm font-medium text-slate-100 transition-colors hover:text-white"
          >
            Profile
          </Link>
          <button
            type="button"
            onClick={handleLogout}
            className="rounded-full border border-rose-400/30 bg-rose-950 px-4 py-2 text-sm text-white transition-colors hover:bg-rose-900 hover:text-white"
          >
            Logout
          </button>
        </nav>
      </div>
    </header>
  );
}
