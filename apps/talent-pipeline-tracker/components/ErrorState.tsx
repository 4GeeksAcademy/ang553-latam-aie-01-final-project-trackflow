interface ErrorStateProps {
  message: string;
}

export function ErrorState({ message }: ErrorStateProps) {
  return (
    <div className="rounded-xl border border-rose-200 bg-rose-50 p-6 text-rose-900 shadow-sm">
      <h2 className="text-base font-semibold">No fue posible cargar candidatos</h2>
      <p className="mt-2 text-sm">{message}</p>
    </div>
  );
}
