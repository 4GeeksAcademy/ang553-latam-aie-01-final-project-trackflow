"use client";

import { FormEvent, useEffect, useState } from "react";
import {
  ApiError,
  createOperationalIncident,
  getOperationalIncident,
  listOperationalClients,
  updateOperationalIncident,
} from "@/lib/incidentsApi";
import type { OperationalClient, OperationalIncident } from "@/types/incidents";
import { useAuth } from "@/lib/AuthContext";

const fieldClass = "rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white";

export function IncidentOperations() {
  const { user } = useAuth();
  const [clients, setClients] = useState<OperationalClient[]>([]);
  const [incident, setIncident] = useState<OperationalIncident | null>(null);
  const [lookup, setLookup] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [closingScore, setClosingScore] = useState("");
  const canWrite = user?.role === "admin" || user?.role === "manager";

  useEffect(() => {
    listOperationalClients().then(setClients).catch(() => setMessage("Client access could not be loaded."));
  }, []);

  async function submitCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setMessage(null);
    try {
      const created = await createOperationalIncident({
        client_id: String(data.get("client_id")), incident_date: String(data.get("incident_date")),
        country: String(data.get("country")), customer_type: String(data.get("customer_type")),
        tracking_number: String(data.get("tracking_number")), carrier: String(data.get("carrier")),
        category: String(data.get("category")), description: String(data.get("description")),
        customer_email: String(data.get("customer_email") || "") || undefined, source_channel: "backoffice",
      });
      setIncident(created); setLookup(created.incident_id); setMessage(`Created ${created.incident_id}.`);
      form.reset();
    } catch (error) { setMessage(error instanceof ApiError ? error.message : "Incident could not be created."); }
  }

  async function submitLookup(event: FormEvent) {
    event.preventDefault(); setMessage(null);
    try { setIncident(await getOperationalIncident(lookup.trim().toUpperCase())); }
    catch (error) { setIncident(null); setMessage(error instanceof ApiError ? error.message : "Incident could not be loaded."); }
  }

  async function changeStatus(status: "OPEN" | "CLOSED" | "DISCARDED") {
    if (!incident) return;
    const reason = status === "OPEN" ? window.prompt("Reason for reopening") : undefined;
    if (status === "OPEN" && !reason) return;
    try {
      setIncident(await updateOperationalIncident(incident.incident_id, {
        expected_version: incident.version, status, reason: reason ?? undefined,
        satisfaction_score: status === "CLOSED" ? Number(closingScore) : undefined,
      }));
      setMessage("Status updated.");
    } catch (error) { setMessage(error instanceof ApiError ? error.message : "Status could not be updated."); }
  }

  return <section className="mt-8 rounded-xl border border-slate-800 bg-slate-900/60 p-6">
    <h3 className="text-xl font-semibold text-white">Operational incidents</h3>
    <p className="mt-1 text-sm text-slate-400">Create and query current records within your assigned clients.</p>
    {message && <p role="status" className="mt-3 text-sm text-cyan-300">{message}</p>}
    <form onSubmit={submitLookup} className="mt-5 flex gap-3">
      <input className={fieldClass} value={lookup} onChange={(e) => setLookup(e.target.value)} placeholder="TRF-000001" required />
      <button className="rounded-lg bg-cyan-600 px-4 py-2 text-sm font-semibold text-white">Look up</button>
    </form>
    {canWrite && <form onSubmit={submitCreate} className="mt-6 grid gap-3 md:grid-cols-3">
      <select className={fieldClass} name="client_id" required defaultValue=""><option value="" disabled>Client</option>{clients.map((c) => <option key={c.client_id} value={c.client_id}>{c.display_name}</option>)}</select>
      <input className={fieldClass} name="incident_date" type="date" required />
      <select className={fieldClass} name="country" required defaultValue="US"><option>US</option><option>ES</option></select>
      <select className={fieldClass} name="customer_type" required defaultValue="B2C"><option>B2C</option><option>B2B</option></select>
      <input className={fieldClass} name="tracking_number" placeholder="Tracking number" required />
      <input className={fieldClass} name="carrier" placeholder="Carrier" required />
      <select className={fieldClass} name="category" required defaultValue="DELAYED_DELIVERY"><option>LOST_PARCEL</option><option>DELAYED_DELIVERY</option><option>WRONG_ADDRESS</option><option>RETURN_REQUEST</option><option>DAMAGE</option></select>
      <input className={fieldClass} name="customer_email" type="email" placeholder="Customer email (optional)" />
      <input className={fieldClass} name="description" placeholder="Description" minLength={5} required />
      <button className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white md:col-span-3">Create incident</button>
    </form>}
    {incident && <div className="mt-6 rounded-lg border border-slate-700 p-4 text-sm text-slate-200">
      <div className="flex flex-wrap items-center justify-between gap-3"><strong>{incident.incident_id} · {incident.status}</strong><span>v{incident.version}</span></div>
      <p className="mt-2">{incident.description}</p><p className="mt-1 text-slate-400">{incident.tracking_number} · {incident.carrier}</p>
      {canWrite && <div className="mt-4 flex flex-wrap items-center gap-2">{incident.status === "OPEN" && <><label className="text-xs text-slate-300">Satisfaction score <input className="ml-1 w-16 rounded bg-slate-950 px-2 py-1" type="number" min="1" max="5" value={closingScore} onChange={(event) => setClosingScore(event.target.value)} /></label><button disabled={!/^[1-5]$/.test(closingScore)} onClick={() => changeStatus("CLOSED")} className="rounded bg-emerald-700 px-3 py-1 disabled:opacity-40">Close</button><button onClick={() => changeStatus("DISCARDED")} className="rounded bg-rose-800 px-3 py-1">Discard</button></>}{incident.status === "CLOSED" && <button onClick={() => changeStatus("OPEN")} className="rounded bg-amber-700 px-3 py-1">Reopen</button>}</div>}
      <ol className="mt-4 space-y-1 text-xs text-slate-400">{incident.history.map((item) => <li key={item.version}>v{item.version}: {item.new_status}{item.reason ? ` — ${item.reason}` : ""}</li>)}</ol>
    </div>}
  </section>;
}
