#!/usr/bin/env python3
"""
TrackFlow Incident Analysis — CLI entry point.

Usage:
    python3 scripts/analyze.py <path-to-csv>

Loads a TrackFlow CSV, validates and analyzes the records, then prints
a formatted report to stdout.
"""

from __future__ import annotations

import sys

from incidents.analyzer import analyze_records, export_results_csv, load_csv
from incidents import CsvLoadError, ERROR_LABELS

# ── Helpers ──────────────────────────────────────────────────────────────────


def _fmt_bar(label: str, count: int, total: int, is_last: bool) -> str:
    """Format one breakdown line with a bar."""
    prefix = "└─" if is_last else "├─"
    pct = count / total * 100 if total > 0 else 0.0
    return f"{prefix} {label} {'':>16s} {count} ({pct:.1f}%)"


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/analyze.py <path-to-csv>", file=sys.stderr)
        sys.exit(1)

    csv_path = sys.argv[1]

    # ── Load ──
    try:
        records = load_csv(csv_path)
    except CsvLoadError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # ── Analyze ──
    try:
        result = analyze_records(records)
    except Exception:
        print(
            "An unexpected error occurred while analyzing the incidents. "
            "Please try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    total = result["total_records"]
    valid = result["valid_records"]
    invalid = result["invalid_records"]

    # ── CLI header ──
    basename = csv_path.rsplit("/", 1)[-1]
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"{'TRACKFLOW — INCIDENT REPORT ANALYSIS':^60}")
    print(f"{sep}")

    # ── Totals ──
    print(f"Source file: {basename}")
    print(f"{'TOTAL RECORDS IN FILE':<39} {total}")
    print(f"{'├─ Valid records':<39} {valid}")
    print(f"{'└─ Invalid / incomplete':<39} {invalid}")
    print()

    # ── Invalid breakdown ──
    if invalid > 0:
        print("INVALID RECORDS BREAKDOWN")
        ib = result["invalid_breakdown"]
        # Sort by error code for consistent ordering
        sorted_errors = sorted(ib.items())
        for idx, (code, count) in enumerate(sorted_errors):
            label = ERROR_LABELS.get(code, code)
            is_last = idx == len(sorted_errors) - 1
            print(f"  {'└─' if is_last else '├─'} {label} {'':>16s} {count}")
        print()

    # ── Category breakdown ──
    if valid > 0:
        print("BREAKDOWN BY CATEGORY (valid records)")
        cat = result["category_breakdown"]
        sorted_cat = sorted(cat.items())
        for idx, (cat_name, count) in enumerate(sorted_cat):
            is_last = idx == len(sorted_cat) - 1
            print(f"  {_fmt_bar(cat_name, count, valid, is_last)}")
        print()

        # ── Status breakdown ──
        print("BREAKDOWN BY STATUS (valid records)")
        st = result["status_breakdown"]
        sorted_st = sorted(st.items())
        for idx, (st_name, count) in enumerate(sorted_st):
            is_last = idx == len(sorted_st) - 1
            print(f"  {_fmt_bar(st_name, count, valid, is_last)}")
        print()

        # ── Country breakdown ──
        print("BREAKDOWN BY COUNTRY (valid records)")
        co = result["country_breakdown"]
        sorted_co = sorted(co.items())
        for idx, (co_name, count) in enumerate(sorted_co):
            is_last = idx == len(sorted_co) - 1
            print(f"  {_fmt_bar(co_name, count, valid, is_last)}")
        print()

    # ── Satisfaction ──
    closed_total = result["status_breakdown"].get("CLOSED", 0)
    closed_scored = result["closed_scored"]
    avg = result["average_satisfaction"]
    score_dist = result["score_distribution"]

    print("SATISFACTION INDEX (closed incidents)")
    print(f"Scored incidents: {closed_scored} of {closed_total}")
    print(f"Average score: {avg:.2f} / 5.00")

    score_labels = {
        1: "Score 1 (Very dissatisfied)",
        2: "Score 2 (Dissatisfied)",
        3: "Score 3 (Neutral)",
        4: "Score 4 (Satisfied)",
        5: "Score 5 (Very satisfied)",
    }
    for s in range(1, 6):
        count = score_dist.get(s, 0)
        is_last = s == 5
        print(f"  {'└─' if is_last else '├─'} {score_labels[s]:<33} {count}")
    print(f"{sep}\n")

    # ── Export prompt ──
    _prompt_export(result)


def _prompt_export(result: dict) -> None:
    """Ask user whether to export results and handle the response."""
    while True:
        try:
            answer = input("Export results to CSV? [y / n]: ").strip().lower()
        except EOFError:
            print("No input was provided. Exiting.", file=sys.stderr)
            sys.exit(1)
        if answer == "y":
            output_path = "results.csv"
            export_results_csv(result, output_path)
            print(f"Results exported to {output_path}")
            break
        elif answer == "n":
            break
        else:
            print('Please enter "y" or "n".')


if __name__ == "__main__":
    main()