import { AuthGuard } from "@/components/AuthGuard";
import { Header } from "@/components/Header";
import { CandidateDetail } from "@/components/CandidateDetail";

interface CandidateDetailPageProps {
  params: Promise<{
    id: string;
  }>;
}

export default async function CandidateDetailPage({
  params,
}: CandidateDetailPageProps) {
  const { id } = await params;

  return (
    <AuthGuard>
      <Header />
      <main className="min-h-screen bg-slate-100 px-4 py-8 sm:px-8 lg:px-12">
        <div className="mx-auto w-full max-w-5xl">
          <CandidateDetail id={id} />
        </div>
      </main>
    </AuthGuard>
  );
}
