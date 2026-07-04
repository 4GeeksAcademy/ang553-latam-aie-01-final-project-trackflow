import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { OperationalSummary } from "@/components/dashboard/OperationalSummary";

export default function HomePage() {
  return (
    <div className="min-h-screen">
      <BackofficeHeader />
      <main className="mx-auto max-w-7xl px-6 py-10">
        <OperationalSummary />
      </main>
    </div>
  );
}
