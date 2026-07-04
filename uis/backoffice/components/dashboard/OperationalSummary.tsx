import { buildOperationalSnapshot } from "@/lib/operationalSnapshot";

function MetricCard({ title, value, hint }: { title: string; value: string; hint: string }) {
  return (
    <article className="rounded-2xl border border-white/10 bg-slate-900/70 p-5 shadow-[0_10px_40px_rgba(15,23,42,0.35)]">
      <p className="text-sm font-medium text-slate-400">{title}</p>
      <p className="mt-3 text-3xl font-bold text-white">{value}</p>
      <p className="mt-2 text-sm text-slate-400">{hint}</p>
    </article>
  );
}

export function OperationalSummary() {
  const snapshot = buildOperationalSnapshot();

  return (
    <section className="space-y-8">
      <div>
        <p className="text-sm font-semibold uppercase tracking-[0.25em] text-cyan-300">Resumen operativo</p>
        <h2 className="mt-3 text-3xl font-bold text-white">TrackFlow Backoffice</h2>
        <p className="mt-3 max-w-3xl text-slate-300">
          Visión consolidada del inventario, alertas de stock y recomendaciones logísticas para apoyar la operación diaria de TrackFlow.
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <MetricCard
          title="Valor total del inventario"
          value={`$${snapshot.totalInventoryValue.toFixed(2)}`}
          hint="Valor consolidado del inventario disponible.."
        />
        <MetricCard
          title="Productos con stock bajo"
          value={String(snapshot.lowStockProducts.length)}
          hint="Productos actualmente por debajo de su nivel mínimo de stock.."
        />
        <MetricCard
          title="Datos de muestra válidos"
          value={`${snapshot.validationSummary.validProducts}/${snapshot.validationSummary.totalProducts} productos`}
          hint={`${snapshot.validationSummary.validCarriers}/${snapshot.validationSummary.totalCarriers} carriers válidos y envío ${snapshot.validationSummary.shipmentValid ? "válido" : "inválido"}.`}
        />
      </div>

      <div className="grid gap-6 xl:grid-cols-[1.2fr_1fr]">
        <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
          <div className="flex items-center justify-between gap-4 border-b border-white/10 pb-4">
            <div>
              <h3 className="text-xl font-semibold text-white">Alertas de stock</h3>
              <p className="mt-1 text-sm text-slate-400">Productos que requieren atención por niveles bajos de inventario.</p>
            </div>
            <span className="rounded-full bg-amber-400/15 px-3 py-1 text-sm font-medium text-amber-200">
              {snapshot.lowStockProducts.length} alertas
            </span>
          </div>
          <div className="mt-5 space-y-4">
            {snapshot.lowStockProducts.map((product) => (
              <article key={product.sku} className="rounded-xl border border-white/8 bg-slate-950/60 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-base font-semibold text-white">{product.name}</p>
                    <p className="mt-1 text-sm text-slate-400">{product.sku} · {product.warehouse}</p>
                  </div>
                  <div className="text-right">
                    <p className="text-sm font-medium text-amber-200">{product.stockQuantity} uds.</p>
                    <p className="text-xs text-slate-500">mínimo {product.minStockThreshold}</p>
                  </div>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="space-y-6">
          <article className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
            <h3 className="text-xl font-semibold text-white">Mejor transportista sugerido</h3>
            <p className="mt-2 text-sm text-slate-400">
              Transportista recomendado según coste, fiabilidad y requisitos del envío.
            </p>
            {snapshot.bestCarrierRecommendation ? (
              <div className="mt-5 rounded-xl border border-cyan-400/20 bg-cyan-400/8 p-4">
                <p className="text-lg font-semibold text-cyan-100">{snapshot.bestCarrierRecommendation.name}</p>
                <div className="mt-3 grid grid-cols-2 gap-3 text-sm text-slate-200">
                  <div>
                    <p className="text-slate-400">Score</p>
                    <p className="mt-1 font-semibold">{snapshot.bestCarrierRecommendation.score}</p>
                  </div>
                  <div>
                    <p className="text-slate-400">Coste estimado</p>
                    <p className="mt-1 font-semibold">${snapshot.bestCarrierRecommendation.cost.toFixed(2)}</p>
                  </div>
                </div>
              </div>
            ) : (
              <p className="mt-5 text-sm text-rose-200">No se encontró una recomendación válida.</p>
            )}
          </article>

          <article className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
            <h3 className="text-xl font-semibold text-white">Distribución por categoría</h3>
            <div className="mt-4 space-y-3 text-sm text-slate-300">
              {Object.entries(snapshot.categoryCounts).map(([category, count]) => (
                <div key={category} className="flex items-center justify-between rounded-lg bg-slate-950/50 px-3 py-2">
                  <span>{category}</span>
                  <span className="font-semibold text-white">{count}</span>
                </div>
              ))}
            </div>
          </article>
        </section>
      </div>
    </section>
  );
}
