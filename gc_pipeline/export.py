"""Export a set of runs to an Excel workbook, in long or wide format."""

import csv

from openpyxl import Workbook

from gc_pipeline import db

LONG_HEADERS = [
    "sample_name", "injection_date", "sample_type", "acq_method", "analysis_method",
    "gas", "rt", "rf", "area", "amount", "concentration",
]


def export_long(conn, run_ids, out_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "runs_long"
    ws.append(LONG_HEADERS)

    for run_id in run_ids:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        peaks = db.get_peaks_for_run(conn, run_id)
        for p in peaks:
            ws.append([
                run["sample_name"], run["injection_date"], run["sample_type"],
                run["acq_method"], run["analysis_method"],
                p["gas"], p["rt"], p["rf"], p["area"], p["amount"], p["concentration"],
            ])

    wb.save(out_path)


def export_wide(conn, run_ids, out_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "runs_wide"

    run_rows = []
    gases = []
    for run_id in run_ids:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        peaks = db.get_peaks_for_run(conn, run_id)
        by_gas = {p["gas"]: p for p in peaks}
        for gas in by_gas:
            if gas not in gases:
                gases.append(gas)
        run_rows.append((run, by_gas))

    headers = ["sample_name", "injection_date", "sample_type", "acq_method", "analysis_method"]
    for gas in gases:
        headers += [f"{gas}_amount", f"{gas}_concentration"]
    ws.append(headers)

    for run, by_gas in run_rows:
        row = [run["sample_name"], run["injection_date"], run["sample_type"],
               run["acq_method"], run["analysis_method"]]
        for gas in gases:
            peak = by_gas.get(gas)
            row.append(peak["amount"] if peak else None)
            row.append(peak["concentration"] if peak else None)
        ws.append(row)

    wb.save(out_path)


def rows_to_tsv(table):
    """table: list of rows, each a list of cell values. Tab-separated so it pastes
    into Excel/Sheets as a proper grid rather than one blob per line."""
    return "\n".join("\t".join("" if c is None else str(c) for c in row) for row in table)


def write_csv(table, out_path):
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(table)
