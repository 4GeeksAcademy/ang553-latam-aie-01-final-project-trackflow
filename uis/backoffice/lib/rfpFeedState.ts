import type { RfpCommercialTicket, RfpTicketCreated, RfpTicketListItem } from "../types/rfp";

const MAX_VISIBLE_TICKETS = 100;
const TERMINAL_JOBS = new Set(["succeeded", "failed", "dead_letter"]);

export function newerRfpCommercialTicket(
  current: RfpCommercialTicket, incoming: RfpCommercialTicket,
): RfpCommercialTicket {
  if (Date.parse(incoming.updated_at) < Date.parse(current.updated_at)) return current;
  if (incoming.status !== "analizando") return incoming;
  if (current.status !== "analizando") return current;
  return TERMINAL_JOBS.has(current.job_status ?? "") && !TERMINAL_JOBS.has(incoming.job_status ?? "")
    ? current : incoming;
}

export function reconcileRfpCommercialTicket(
  tickets: RfpCommercialTicket[], incoming: RfpCommercialTicket,
): RfpCommercialTicket[] {
  return tickets.map((current) => current.ticket_id === incoming.ticket_id
    ? newerRfpCommercialTicket(current, incoming) : current);
}

export function reconcileRfpCommercialSnapshot(
  tickets: RfpCommercialTicket[], snapshot: RfpCommercialTicket[],
): RfpCommercialTicket[] {
  const byId = new Map(tickets.map((item) => [item.ticket_id, item]));
  return snapshot.map((incoming) => {
    const current = byId.get(incoming.ticket_id);
    return current ? newerRfpCommercialTicket(current, incoming) : incoming;
  });
}

export function mergeRfpCreated(
  tickets: RfpCommercialTicket[], event: RfpTicketCreated,
): RfpCommercialTicket[] {
  const present = tickets.find((item) => item.ticket_id === event.ticket_id);
  const entry: RfpCommercialTicket = present ? {
    ...present,
    rfp_id: present.rfp_id ?? event.rfp_id,
    client_name: present.client_name ?? event.client_name,
    client_country: present.client_country ?? event.client_country,
    services_requested: present.services_requested.length ? present.services_requested : event.services_requested,
  } : {
    ...event, status: "analizando", job_status: "running", updated_at: event.created_at,
  };
  if (present) return tickets.map((item) => item.ticket_id === event.ticket_id ? entry : item);
  return [entry, ...tickets].sort((a, b) =>
    Date.parse(b.created_at) - Date.parse(a.created_at) || b.ticket_id.localeCompare(a.ticket_id),
  ).slice(0, MAX_VISIBLE_TICKETS);
}

export function rememberRfpTicket(ids: Set<string>, id: string): void {
  ids.add(id);
  if (ids.size > MAX_VISIBLE_TICKETS) {
    const oldest = ids.values().next().value;
    if (oldest) ids.delete(oldest);
  }
}

export function mergeRfpAdminList(
  adminTickets: RfpTicketListItem[], commercialTickets: RfpCommercialTicket[],
): RfpTicketListItem[] {
  const byId = new Map(adminTickets.map((ticket) => [ticket.ticket_id, ticket]));
  for (const ticket of commercialTickets) {
    const existing = byId.get(ticket.ticket_id);
    if (!existing) {
      byId.set(ticket.ticket_id, {
        ...ticket, currency: null, departments_needed: ticket.services_requested,
      });
      continue;
    }
    const useFeedStatus = existing.status === "analizando" && (
      ticket.status !== "analizando" || existing.job_status === "queued"
    );
    byId.set(ticket.ticket_id, {
      ...existing,
      rfp_id: existing.rfp_id ?? ticket.rfp_id,
      client_name: existing.client_name ?? ticket.client_name,
      departments_needed: existing.departments_needed.length
        ? existing.departments_needed : ticket.services_requested,
      status: useFeedStatus ? ticket.status : existing.status,
      job_status: useFeedStatus ? ticket.job_status : existing.job_status,
    });
  }
  return [...byId.values()].sort((a, b) =>
    Date.parse(b.created_at) - Date.parse(a.created_at) || b.ticket_id.localeCompare(a.ticket_id),
  ).slice(0, MAX_VISIBLE_TICKETS);
}
