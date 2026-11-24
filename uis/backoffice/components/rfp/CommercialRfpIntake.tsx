import { useEffect, useState } from "react";
import { getRfpCommercialTicket, RfpApiError } from "@/lib/rfpApi";
import { newerRfpCommercialTicket } from "@/lib/rfpFeedState";
import { DEPARTMENT_LABELS, displayRfpDate, JOB_LABELS, TICKET_LABELS } from "@/lib/rfpLabels";
import type { RfpConnectionState, useRfpNotifications } from "@/lib/useRfpNotifications";
import type { RfpCommercialTicket, RfpDepartmentId } from "@/types/rfp";

const CONNECTION_LABELS: Record<RfpConnectionState, string> = {
  connecting: "Conectando avisos…",
  connected: "Avisos conectados",
  reconnecting: "Reconectando avisos…",
  recovering: "Recuperando avisos…",
  forbidden: "Avisos no autorizados",
};

type RfpFeed = ReturnType<typeof useRfpNotifications>;

function serviceLabels(services: RfpDepartmentId[]) {
  return services.map((service) => DEPARTMENT_LABELS[service]).join(", ");
}

export function RfpNotificationStatus({ feed }: { feed: RfpFeed }) {
  return (
    <div className="space-y-2">
      <p role="status" className="text-sm text-slate-300">{CONNECTION_LABELS[feed.connection]}</p>
      <div aria-live="polite" aria-atomic="true">
        {feed.notice && (
          <div className="rounded-xl border border-cyan-400/40 bg-cyan-950/40 p-4 text-sm text-cyan-100">
            <p className="font-semibold">Nueva RFP en análisis</p>
            <p>{feed.notice.client_name ?? "Cliente por identificar"} · {serviceLabels(feed.notice.services_requested)}</p>
            <p className="break-all text-xs text-cyan-200">Ticket {feed.notice.ticket_id}</p>
          </div>
        )}
      </div>
    </div>
  );
}

export function NewTicketBadge() {
  return <span className="rounded-full border border-cyan-400/40 bg-cyan-900/60 px-2 py-0.5 text-xs font-semibold text-cyan-100">Nuevo</span>;
}

function CommercialRfpDetail({ initial, reconcileTicket }: {
  initial: RfpCommercialTicket;
  reconcileTicket: (ticket: RfpCommercialTicket) => void;
}) {
  const { ticket_id } = initial;
  const [loaded, setLoaded] = useState<RfpCommercialTicket | null>(null);
  const [failed, setFailed] = useState(false);
  const [denied, setDenied] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = async () => {
      try {
        const ticket = await getRfpCommercialTicket(ticket_id, controller.signal);
        if (controller.signal.aborted) return;
        if (ticket.ticket_id !== ticket_id) throw new RfpApiError("El detalle no corresponde al ticket solicitado.");
        setLoaded((prior) => prior ? newerRfpCommercialTicket(prior, ticket) : ticket);
        reconcileTicket(ticket);
        setFailed(false);
        setDenied(false);
        if (ticket.status === "analizando" && !["failed", "dead_letter", "succeeded"].includes(ticket.job_status ?? "")) {
          timer = setTimeout(() => void refresh(), 3000);
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        setFailed(true);
        if (error instanceof RfpApiError && [401, 403, 404].includes(error.statusCode ?? 0)) {
          setDenied(true);
        } else {
          timer = setTimeout(() => void refresh(), 3000);
        }
      }
    };
    void refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [ticket_id, reconcileTicket]);
  if (denied) return <p role="alert" className="text-sm text-rose-200">El ticket ya no está disponible.</p>;
  const ticket = loaded ? newerRfpCommercialTicket(initial, loaded) : initial;

  return (
    <section aria-label="Detalle comercial de RFP" className="rounded-xl border border-white/10 bg-slate-950/60 p-4 text-sm">
      {failed && <p role="alert" className="mb-2 text-rose-200">No se pudo actualizar el detalle.</p>}
      <h3 className="break-all font-semibold text-white">Ticket {ticket.ticket_id}</h3>
      <dl className="mt-3 grid gap-3 text-slate-100 sm:grid-cols-2">
        <div><dt className="text-slate-400">Cliente</dt><dd>{ticket.client_name ?? "Por identificar"}</dd></div>
        <div><dt className="text-slate-400">RFP</dt><dd>{ticket.rfp_id ?? "Sin identificador en el documento"}</dd></div>
        <div><dt className="text-slate-400">País</dt><dd>{ticket.client_country ?? "Sin confirmar"}</dd></div>
        <div><dt className="text-slate-400">Servicios solicitados</dt><dd>{serviceLabels(ticket.services_requested) || "Sin confirmar"}</dd></div>
        <div><dt className="text-slate-400">Estado</dt><dd>{TICKET_LABELS[ticket.status]}{ticket.job_status && ` · ${JOB_LABELS[ticket.job_status]}`}</dd></div>
      </dl>
    </section>
  );
}

export function CommercialRfpIntake({ feed }: { feed: RfpFeed }) {
  const [selected, setSelected] = useState<RfpCommercialTicket | null>(null);
  const detail = selected ? feed.tickets.find((ticket) => ticket.ticket_id === selected.ticket_id) ?? selected : null;
  return (
    <section aria-labelledby="rfp-commercial-title" className="space-y-4 rounded-2xl border border-white/10 bg-slate-900/70 p-5">
      <h2 id="rfp-commercial-title" className="text-lg font-semibold text-white">Recepción comercial</h2>
      <RfpNotificationStatus feed={feed} />
      {feed.tickets.length === 0 ? <p className="text-sm text-slate-400">Todavía no hay RFPs disponibles.</p> : (
        <ul className="grid gap-2">{feed.tickets.map((row) => (
          <li key={row.ticket_id}>
            <button type="button" aria-pressed={selected?.ticket_id === row.ticket_id} onClick={() => setSelected(row)}
              className={`w-full rounded-xl border p-3 text-left ${selected?.ticket_id === row.ticket_id ? "border-cyan-400/50" : "border-white/10 hover:border-white/20"}`}>
              <span className="flex flex-wrap items-center gap-2 text-sm font-medium text-white">
                {row.client_name ?? row.rfp_id ?? row.ticket_id}
                {feed.newTickets.has(row.ticket_id) && <NewTicketBadge />}
              </span>
              <span className="block text-xs text-slate-400">{TICKET_LABELS[row.status]} · {displayRfpDate(row.created_at)}</span>
            </button>
          </li>
        ))}</ul>
      )}
      {detail && <CommercialRfpDetail key={detail.ticket_id} initial={detail} reconcileTicket={feed.reconcileTicket} />}
    </section>
  );
}
