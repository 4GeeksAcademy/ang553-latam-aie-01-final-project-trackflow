export interface WeeklyPerformanceEntry {
  warehouse: string;
  client_id: string;
  inbound_units_count: number;
  outbound_orders_count: number;
  stockout_events_count: number;
  discrepancy_events_count: number;
  discrepancy_rate: number;
}

export interface WeeklyPerformanceReport {
  week_start: string;
  entries: WeeklyPerformanceEntry[];
}
