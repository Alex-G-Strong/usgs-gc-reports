"""Pure calculation layer for the Models tab's standards-calibration regression - no
Tkinter here, just turning (standard composition, pressure, peak area) into a
plottable (area, amount) point per gas, and fitting a line through a set of them."""

import numpy as np

from gc_pipeline import db

ATMOSPHERIC_PRESSURE = 14.65  # per-lab convention: amount = percent * pressure / this


def compute_amount(percent, pressure):
    return percent * pressure / ATMOSPHERIC_PRESSURE


def build_calibration_candidates(conn, run_ids):
    """{gas: [{"run_id", "sample_name", "standard_name", "notes", "area", "amount"},
    ...]} for every (run, gas) pair where run_id is standard-linked, has a pressure,
    and that standard has a composition entry for that gas. A run missing any of
    those three simply doesn't contribute a point for that gas - it's not shown as a
    broken/BD point here, so every point this returns is guaranteed to carry a real
    standard."""
    run_ids = list(run_ids)
    if not run_ids:
        return {}

    runs = db.list_runs_for_pressure_entry(conn, run_ids=run_ids, missing_only=False)
    ready = [r for r in runs if r["standard_id"] is not None and r["pressure"] is not None]
    if not ready:
        return {}

    peaks_map = db.get_peaks_map_for_runs(conn, [r["run_id"] for r in ready])
    compositions_by_standard = {}

    candidates = {}
    for r in ready:
        standard_id = r["standard_id"]
        if standard_id not in compositions_by_standard:
            compositions_by_standard[standard_id] = {
                row["gas"]: row["value"] for row in db.get_standard_compositions(conn, standard_id)
            }
        composition = compositions_by_standard[standard_id]
        run_peaks = peaks_map.get(r["run_id"], {})
        for gas, percent in composition.items():
            peak = run_peaks.get(gas)
            if peak is None or peak["area"] is None:
                continue
            amount = compute_amount(percent, r["pressure"])
            candidates.setdefault(gas, []).append({
                "run_id": r["run_id"],
                "sample_name": r["sample_name"],
                "standard_name": r["standard_name"],
                "notes": r["notes"] or "",
                "area": peak["area"],
                "amount": amount,
            })
    return candidates


def compute_percent(area, pressure, slope, intercept):
    """Invert the fitted line for a sample: the curve was fit as
    amount = slope*area + intercept (amount = percent*pressure/14.65 for standards),
    so solving for percent given a sample's own raw area and pressure gives
    percent = 14.65 * (slope*area + intercept) / pressure. The raw area is plugged in
    unchanged (matching how the curve was actually fit) - only the final result is
    normalized by the sample's own pressure."""
    if not pressure:
        return None
    return ATMOSPHERIC_PRESSURE * (slope * area + intercept) / pressure


def get_series_areas(conn, series_id):
    """[{"run_id", "sample_name", "area"}, ...] for a series' non-excluded points,
    resolving each point's effective area as override_area if set else the run's
    real peaks.area for that series' gas - the same resolution
    get_series_area_bounds uses, just returning every point instead of collapsing
    them to a min/max (for the calibration-range diagnostic chart, which plots
    each one)."""
    series = conn.execute(
        "SELECT gas FROM calibration_series WHERE series_id = ?", (series_id,)
    ).fetchone()
    if series is None:
        return []
    gas = series["gas"]
    points = [p for p in db.get_calibration_series_points(conn, series_id) if not p["excluded"]]
    if not points:
        return []
    run_ids = [p["run_id"] for p in points]
    peaks_map = db.get_peaks_map_for_runs(conn, run_ids)
    names = {row["run_id"]: row["sample_name"] for row in db.get_data_viewer_rows(conn, run_ids)}
    result = []
    for p in points:
        if p["override_area"] is not None:
            area = p["override_area"]
        else:
            peak = peaks_map.get(p["run_id"], {}).get(gas)
            area = peak["area"] if peak is not None else None
        if area is not None:
            result.append({"run_id": p["run_id"], "sample_name": names.get(p["run_id"]), "area": area})
    return result


def get_series_area_bounds(conn, series_id):
    """(min_area, max_area) across a series' non-excluded points. None if fewer
    than 1 usable point."""
    areas = [p["area"] for p in get_series_areas(conn, series_id)]
    if not areas:
        return None
    return min(areas), max(areas)


def fit_linear(points, through_origin=False):
    """points: [(area, amount), ...], already filtered to whatever should count
    toward the fit (excluded points removed by the caller). Returns
    (slope, intercept, r_squared), or None if there isn't enough data to fit.

    through_origin=True locks intercept at 0 (a common calibration-curve convention:
    zero amount should read zero signal) and needs only 1 point rather than 2, since
    the line's other degree of freedom is already fixed."""
    if through_origin:
        if len(points) < 1:
            return None
        xs = np.array([p[0] for p in points], dtype=float)
        ys = np.array([p[1] for p in points], dtype=float)
        denom = np.sum(xs * xs)
        if denom == 0:
            return None  # every point sits at x=0 - no finite slope through the origin
        slope = float(np.sum(xs * ys) / denom)
        intercept = 0.0
    else:
        if len(points) < 2:
            return None
        xs = np.array([p[0] for p in points], dtype=float)
        ys = np.array([p[1] for p in points], dtype=float)
        if np.all(xs == xs[0]):
            return None  # a vertical scatter can't produce a finite slope
        slope, intercept = (float(v) for v in np.polyfit(xs, ys, 1))

    predicted = slope * xs + intercept
    ss_res = np.sum((ys - predicted) ** 2)
    ss_tot = np.sum((ys - np.mean(ys)) ** 2)
    r_squared = 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot
    return float(slope), float(intercept), float(r_squared)
