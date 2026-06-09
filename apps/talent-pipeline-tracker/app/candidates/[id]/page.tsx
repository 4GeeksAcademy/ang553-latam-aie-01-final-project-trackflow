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
    <main className="min-h-screen bg-slate-100 px-4 py-8 sm:px-8 lg:px-12">
      <div className="mx-auto w-full max-w-5xl">
        <CandidateDetail id={id} />
      </div>
    </main>
  );
}
