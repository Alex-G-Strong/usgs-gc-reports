"""The "Analysis" tab: applies the Models tab's fitted calibration curves to sample
runs (anything linked to the reserved "Sample" standard). Population is automatic -
whatever lands in Models via "Load selected into Models" that's Sample-linked and has
a pressure shows up here too, no separate load step. For each gas the user picks
which fitted curve applies to that sample; the raw peak area is inverted through that
curve's equation to get a % composition, out-of-calibration-range values are flagged,
duplicate runs can be dragged together into a persisted, averaged synthetic entry
(Illustrator-layers-panel style), and the selected row's bottle composition is drawn
as a stacked bar."""

import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.patches import ConnectionPatch

from gc_pipeline import calibration, db, export
from gc_pipeline.pressure_wizard import PressureEntryWindow
from gc_pipeline.widgets import bind_fast_hscroll, bind_tooltip, justify_columns

DRAG_HANDLE_GLYPH = "⋮"
DRAG_THRESHOLD_PX = 5
NONE_SERIES_LABEL = "(none)"

# Muted/low-toned per-gas palette, loosely following the industrial-gas color chart
# (gray CO2, near-black N2, brown/orange He, green O2, red CH4, teal Ar/sulfur gases)
# but desaturated so it sits comfortably alongside the rest of the app's UI. The
# hydrocarbon family (CH4 through C6H14) gets its own warm ramp so they read as a
# related-but-distinct group rather than all collapsing into one color.
GAS_COLORS = {
    "CO2": "#8c8c86",
    "CO": "#6b6bb0",
    "N2": "#2b2b2b",
    "HE": "#a9714a",
    "O2": "#5b8c5a",
    "AR": "#3f6b6b",
    "SO2": "#4f8f8a",
    "H2S": "#4f8f8a",
    "H2": "#5b7a9c",
    # hydrocarbon ramp - warm reds/oranges/golds, darkening/cooling slightly as
    # the carbon chain gets longer so each one stays visually distinguishable
    "CH4": "#b5534c",
    "C2H6": "#c17a3f",
    "C3H8": "#c2a13f",
    "C4H10": "#8a9c3f",
    "C5H12": "#5f9c6b",
    "C6H14": "#3f8a7a",
}
DEFAULT_GAS_COLOR = "#7a7a7a"
UNMEASURED_COLOR = "#ffffff"

# Any gas not in GAS_COLORS (a brand-new one the instrument starts reporting, or
# an unrecognized variant) gets a color from here instead of the flat default grey
# - assigned deterministically by name (not randomly/by insertion order) so a given
# gas always renders the same color across restarts without needing this dict
# maintained by hand every time a new gas shows up.
FALLBACK_GAS_COLOR_PALETTE = [
    "#7a6fa6", "#a65f8f", "#5f8fa6", "#a68a5f", "#6fa67a",
    "#a67373", "#7373a6", "#8fa65f", "#a65f73", "#5fa6a0",
]

MATH_EXPLANATION = (
    "For each gas cell: percent = 14.65 x (slope x area + intercept) / pressure.\n\n"
    "slope/intercept come from this cell's selected calibration curve (the fitted "
    "line from the Models tab); area is this run's own raw peak area for that gas; "
    "pressure is this run's own entered pressure. The raw area is plugged into the "
    "curve's equation exactly as it was fit, then the result is divided by this "
    "run's pressure (and rescaled by 14.65) to recover a percent composition."
)


def _base_gas_name(gas):
    """The leading alphanumeric token of a gas name, uppercased - e.g. both
    "CH4 - Meth_channel" and "CH4 - HC_channel" reduce to "CH4", "C2H6-Meth
    channel" reduces to "C2H6". Peak gas names carry a channel suffix that
    varies by acquisition method (see channels.py), but the underlying gas -
    and therefore its color - is the same regardless of which channel measured
    it."""
    match = re.match(r"[A-Za-z0-9]+", gas or "")
    return match.group(0).upper() if match else ""


def _gas_color(gas):
    base = _base_gas_name(gas)
    if base in GAS_COLORS:
        return GAS_COLORS[base]
    if not base:
        return DEFAULT_GAS_COLOR
    # Deterministic (not random) so the same not-explicitly-mapped gas always
    # gets the same color across renders/restarts.
    index = sum(ord(c) for c in base) % len(FALLBACK_GAS_COLOR_PALETTE)
    return FALLBACK_GAS_COLOR_PALETTE[index]


def _text_color_for_bg(hex_color):
    """White text on dark segments, black text on light ones - relative-luminance
    based so it keeps working automatically as gas colors are tuned or new gases
    are added, rather than maintaining a separate "which gases are dark" list."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#ffffff" if luminance < 0.5 else "#000000"


class _TranslatedEvent:
    """A minimal stand-in for a Tk event, carrying only tree-relative x/y - used to
    forward a press/motion that landed on a cell overlay (a Label placed on top of
    the tree) into the same handlers the tree's own events go through, since the
    overlay's own event.x/y are relative to the overlay itself, not the tree."""

    def __init__(self, x, y):
        self.x = x
        self.y = y


class AnalysisPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.conn = app.conn

        self._run_ids = []          # last set of run_ids passed to load_runs
        self._rows = {}             # run_id -> row from list_runs_for_pressure_entry
        self._top_level_ids = []    # ordered top-level iids-worth of run_ids (incl. group run_ids)
        self._group_members = {}    # group_run_id -> [member_run_id, ...] (only shown groups)
        self._gases = []
        self._series_by_gas = {}
        self._series_selection = {}  # (run_id, gas) -> series_id or None
        self._bounds_by_series = {}  # series_id -> (min_area, max_area) or None
        self._peaks_map = {}

        self._drag_row_iid = None
        self._drag_press_xy = None
        self._drag_active = False
        self._drag_hover_iid = None
        self._status_text_before_drag = None

        self._active_popup = None
        self._active_popup_close = None
        self._suppress_next_global_close = False
        self.tree = None
        self._vsb = None
        self._hsb = None
        self._cell_overlays = {}  # (run_id, gas) -> [tk.Label, ...] (4 border strips), out-of-range/range-selected
        self._highlight_overlays = {}  # run_id -> tk.Label chip, same technique as the Selector tab
        self._cell_highlights = {}  # (run_id, gas) -> (color, label), refreshed each _update_overlays() call

        # Excel-style range-select within a single gas column, for bulk-reassigning
        # a calibration curve across several rows at once.
        self._range_selection = None   # {"gas": str, "run_ids": [int, ...]} or None
        self._range_drag_gas = None    # the gas column a select-drag is currently in, or None
        self._range_drag_anchor = None  # the iid the current select-drag started from

        # results-table percent precision - loads at 4 by default, "More digits"
        # still cycles 2 -> 6 -> 2 (so 4 -> 5 -> 6 -> 2 -> 3 -> 4 from the new start).
        self._decimal_places = 4
        self._expanded_groups = set()  # group_run_id of groups currently expanded, survives _render_tree()

        # "The selected cell" - drives both the calibration-range chart and the
        # raw CSV/peaks inspector underneath the results table. gas is None
        # whenever a non-gas cell was what set run_id (the range chart has
        # nothing to show then, but the raw inspector still can).
        self._inspector_run_id = None
        self._inspector_gas = None

        self._build()

    # -- layout -------------------------------------------------------------
    def _build(self):
        header = ttk.Frame(self)
        header.pack(fill="x", padx=6, pady=(6, 0))
        self.status_label = ttk.Label(header, text="Nothing loaded yet.", foreground="#666666")
        self.status_label.pack(side="left")
        info_label = ttk.Label(header, text="(?)", foreground="#4477aa", cursor="question_arrow")
        info_label.pack(side="left", padx=(8, 0))
        bind_tooltip(info_label, MATH_EXPLANATION, wraplength=420)
        ttk.Button(header, text="Edit selected pressures", command=self.on_edit_pressures).pack(side="right")
        ttk.Button(
            header, text="Inspect current selection in Selector",
            command=self.on_inspect_in_selector,
        ).pack(side="right", padx=(0, 6))
        ttk.Button(header, text="Reset view", command=self._reset_view_layout).pack(side="right", padx=(0, 6))

        # A vertical split: the existing results-table/chart split on top, a
        # calibration-range diagnostic + raw CSV/peaks inspector underneath - so
        # inspecting a run's raw data (or seeing how far off-curve a flagged
        # value is) no longer means leaving this tab (see on_cell_click / on_
        # cell_double_click below, and _set_inspector_cell).
        self._vpaned = ttk.PanedWindow(self, orient="vertical")
        self._vpaned.pack(fill="both", expand=True, padx=6, pady=6)

        self.paned = ttk.PanedWindow(self._vpaned, orient="horizontal")
        self._vpaned.add(self.paned, weight=3)

        self.table_frame = ttk.Frame(self.paned)
        self.paned.add(self.table_frame, weight=5)
        table_header = ttk.Frame(self.table_frame)
        table_header.pack(fill="x", pady=(0, 4))
        ttk.Label(table_header, text="Results table", font=("", 10, "bold")).pack(side="left")
        self.reassign_button = ttk.Button(
            table_header, text="Reassign curve for selection...",
            command=self.on_reassign_curve_for_selection, state="disabled",
        )
        self.reassign_button.pack(side="left", padx=(10, 0))
        self._show_highlights_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            table_header, text="Show highlights", variable=self._show_highlights_var,
            command=self._update_overlays,
        ).pack(side="left", padx=(10, 0))
        ttk.Button(table_header, text="Justify columns", command=self.on_justify_columns).pack(
            side="right"
        )
        ttk.Button(table_header, text="Copy table", command=self.on_copy_results_table).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(table_header, text="Export as CSV...", command=self.on_export_results_csv).pack(
            side="right", padx=(6, 0)
        )
        # Excel's "increase decimal" button, but a single control cycling 2 -> 6 -> 2
        # decimal places rather than separate increase/decrease buttons (per the
        # user's "add a more-digits button" ask, singular). Trailing zeros are always
        # stripped in _format_percent - this only raises the ceiling on how much real
        # precision can show, it never pads a round number with fake digits.
        self.more_digits_button = ttk.Button(
            table_header, text=f".{'0' * self._decimal_places} →", width=8, command=self.on_toggle_more_digits,
        )
        self.more_digits_button.pack(side="right", padx=(0, 6))
        bind_tooltip(
            self.more_digits_button,
            lambda: f"Show up to {self._decimal_places} decimal places (click to increase, wraps back at 6). "
                    "Never pads a value with digits it doesn't actually have.",
        )
        self._tree_area = ttk.Frame(self.table_frame)
        self._tree_area.pack(fill="both", expand=True)
        self._build_tree()

        # The PanedWindow fills its packed geometry by weight on layout/resize, so
        # the pane's actual on-screen width tracks this ratio rather than the
        # frame's initial width= hint once it's been stretched. Wider than the
        # single-chart layout used to be, since there are now two side-by-side
        # charts plus a zoom slider to fit here.
        self.chart_frame = ttk.Frame(self.paned, width=320)
        self.paned.add(self.chart_frame, weight=3)
        self._build_chart(self.chart_frame)

        # Replaces the old embedded Data Viewer pivot table - a click on any gas
        # cell above ("the selected cell") drives two things down here instead:
        # a compact horizontal chart showing where that cell's own peak area
        # falls relative to its calibration curve's own point range (so it's
        # visually obvious how far off-curve, if at all, a flagged value is),
        # and underneath it, the same raw CSV/peaks inspector the Selector tab
        # has, scoped to that one run. A nested vertical PanedWindow (not a plain
        # Frame) so this split is also user-resizable, same as every other pane
        # in this tab.
        self._bottom_vpaned = ttk.PanedWindow(self._vpaned, orient="vertical")
        self._vpaned.add(self._bottom_vpaned, weight=2)

        range_chart_frame = ttk.Frame(self._bottom_vpaned)
        self._bottom_vpaned.add(range_chart_frame, weight=1)
        self._build_range_chart(range_chart_frame)

        raw_inspector_frame = ttk.Frame(self._bottom_vpaned)
        self._bottom_vpaned.add(raw_inspector_frame, weight=3)
        self._build_raw_inspector(raw_inspector_frame)

        self.bind_all("<Button-1>", self._on_global_click, add="+")
        self.after(0, self._set_initial_sash)

    def on_inspect_in_selector(self):
        if not self._run_ids:
            messagebox.showwarning("Nothing loaded", "No runs are currently loaded into Analysis.")
            return
        self.app.set_selected_run_ids(self._run_ids)
        self.app.notebook.select(self.app.selector_tab)

    def _set_initial_sash(self):
        self._reset_view_layout(retry=True)

    def _reset_view_layout(self, retry=False):
        """Restores all three resizable panes (table/composition-chart split,
        top/bottom split, and range-chart/raw-inspector split) to their default
        ratios - both the first-layout call from _build() and the "Reset view"
        button use this. weight alone doesn't reliably land a pane at its target
        fraction on first layout (it only governs redistribution on later
        resizes), so each sash is pinned explicitly once real geometry is
        available - retrying shortly if the widget isn't sized yet (only
        relevant for the very first call, right after construction; a later
        "Reset view" click always has real geometry already)."""
        total = self.paned.winfo_width()
        vtotal = self._vpaned.winfo_height()
        bottom_total = self._bottom_vpaned.winfo_height()
        if total <= 50 or vtotal <= 50 or bottom_total <= 50:
            if retry:
                self.after(50, self._set_initial_sash)
            return
        # Matches each pane's own weight ratio: 5:3 table:chart, 3:2 top:bottom,
        # 1:3 range-chart:raw-inspector (the range chart only needs a compact
        # strip; the raw inspector benefits from more room to read a full CSV).
        self.paned.sashpos(0, int(total * 0.625))
        self._vpaned.sashpos(0, int(vtotal * 0.6))
        self._bottom_vpaned.sashpos(0, int(bottom_total * 0.25))

    def on_justify_columns(self):
        if self.tree is not None:
            justify_columns(self.tree, list(self.tree["columns"]))
            self._update_overlays()

    def on_edit_pressures(self):
        if not self._run_ids:
            messagebox.showwarning("Nothing loaded", "No runs are currently loaded into Analysis.")
            return
        PressureEntryWindow(
            self.app, run_ids=self._run_ids, missing_only=False,
            on_close_callback=self._reload,
        )

    # Dark, near-black bracket color for the zoom-region lines/pill - deliberately
    # not one of the gas colors, so it always reads as UI chrome, not data.
    BRACKET_COLOR = "#333333"
    PILL_HIT_PAD = 6  # extra px of grab tolerance around the pill's rendered bbox

    # -- calibration-range diagnostic chart + raw inspector, for "the selected cell" --
    RANGE_POINT_COLOR = "#7f9bb5"     # muted blue - the curve's own standard points
    RANGE_SAMPLE_IN_COLOR = "#3a9b52"  # green - sample area falls inside the curve's range
    RANGE_SAMPLE_OUT_COLOR = "#c0392b"  # red - sample area falls outside it

    def _build_range_chart(self, parent):
        header = ttk.Frame(parent)
        header.pack(fill="x")
        ttk.Label(header, text="Calibration range - selected cell", font=("", 9, "bold")).pack(side="left")
        self.range_chart_label = ttk.Label(header, text="", foreground="#666666")
        self.range_chart_label.pack(side="left", padx=(8, 0))
        self.range_fig = Figure(figsize=(6, 1.2), dpi=95)
        self.range_ax = self.range_fig.add_subplot(111)
        self.range_canvas = FigureCanvasTkAgg(self.range_fig, master=parent)
        self.range_canvas.get_tk_widget().pack(fill="both", expand=True)
        self._render_range_chart()

    def _render_range_chart(self):
        self.range_ax.clear()
        self.range_ax.set_yticks([])
        self.range_ax.set_xlabel("peak area", fontsize=8)
        self.range_ax.tick_params(labelsize=7)
        self.range_ax.axvline(0, color="#bbbbbb", linewidth=0.8, zorder=0)

        run_id, gas = self._inspector_run_id, self._inspector_gas
        if run_id is None or gas is None:
            self.range_ax.text(
                0.5, 0.5, "Click a gas cell above to see its calibration range",
                ha="center", va="center", transform=self.range_ax.transAxes,
                color="#888888", fontsize=8,
            )
            self.range_chart_label.config(text="")
            self.range_fig.tight_layout()
            self.range_canvas.draw_idle()
            return

        series = self._series_for(run_id, gas)
        peaks_map = db.get_peaks_map_for_runs(self.conn, [run_id])
        peak = peaks_map.get(run_id, {}).get(gas)
        row = self._rows.get(run_id)
        sample_name = (row["sample_name"] if row is not None else None) or f"run {run_id}"

        if peak is None or peak["area"] is None:
            self.range_ax.text(
                0.5, 0.5, f"{sample_name}: no {gas} peak on this run",
                ha="center", va="center", transform=self.range_ax.transAxes,
                color="#888888", fontsize=8,
            )
            self.range_chart_label.config(text="")
            self.range_fig.tight_layout()
            self.range_canvas.draw_idle()
            return
        sample_area = peak["area"]

        if series is None or series["slope"] is None:
            self.range_ax.text(
                0.5, 0.5, f"{sample_name}: no calibration curve selected for {gas}",
                ha="center", va="center", transform=self.range_ax.transAxes,
                color="#888888", fontsize=8,
            )
            self.range_chart_label.config(text=f"This run's own {gas} area: {sample_area:.4g}")
            self.range_fig.tight_layout()
            self.range_canvas.draw_idle()
            return

        curve_points = calibration.get_series_areas(self.conn, series["series_id"])
        curve_areas = [p["area"] for p in curve_points]

        if curve_areas:
            self.range_ax.scatter(
                curve_areas, [0] * len(curve_areas), color=self.RANGE_POINT_COLOR, s=40, zorder=2,
                label=f'{series["name"]} points',
            )
            lo, hi = min(curve_areas), max(curve_areas)
            is_out = sample_area < lo or sample_area > hi
        else:
            lo = hi = None
            is_out = False

        color = self.RANGE_SAMPLE_OUT_COLOR if is_out else self.RANGE_SAMPLE_IN_COLOR
        self.range_ax.axvline(sample_area, color=color, linewidth=2.5, zorder=3, label=sample_name)
        self.range_ax.scatter([sample_area], [0], color=color, s=90, marker="D", zorder=4, edgecolors="#333333")

        # Positive peak areas across the standards vs. a sample can easily span
        # more than an order of magnitude (confirmed against real data - e.g.
        # 593 to 171344 for one CH4 channel) - log scale keeps a point that's
        # merely 2x off the range from looking indistinguishable from one that's
        # 50x off, which a linear axis would otherwise compress into.
        all_positive = sample_area > 0 and all(a > 0 for a in curve_areas)
        self.range_ax.set_xscale("log" if all_positive else "linear")

        if is_out:
            direction = "below" if sample_area < lo else "above"
            status = f"OUT OF RANGE ({direction} the curve's {lo:.4g}-{hi:.4g} range)"
        elif lo is not None:
            status = f"within the curve's {lo:.4g}-{hi:.4g} range"
        else:
            status = "curve has no fitted points yet"
        self.range_chart_label.config(
            text=f"{sample_name}: area={sample_area:.4g} - {status}", foreground=color if is_out else "#666666"
        )
        self.range_ax.legend(fontsize=6, loc="upper right")
        self.range_fig.tight_layout()
        self.range_canvas.draw_idle()

    def _build_raw_inspector(self, parent):
        ttk.Label(parent, text="Raw inspector", font=("", 9, "bold")).pack(anchor="w", pady=(0, 2))
        text_frame = ttk.Frame(parent)
        text_frame.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(text_frame, orient="vertical")
        self.raw_inspector_text = tk.Text(
            text_frame, wrap="none", state="disabled", yscrollcommand=scroll.set,
        )
        scroll.config(command=self.raw_inspector_text.yview)
        self.raw_inspector_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._refresh_raw_inspector()

    def _refresh_raw_inspector(self):
        self.raw_inspector_text.configure(state="normal")
        self.raw_inspector_text.delete("1.0", "end")
        run_id = self._inspector_run_id
        if run_id is None:
            self.raw_inspector_text.insert("end", "Click a gas cell above to inspect that run's raw CSV data.")
        else:
            run = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                self.raw_inspector_text.insert("end", "This run no longer exists.")
            else:
                self.raw_inspector_text.insert("end", f"{run['sample_name']}  ({run['source_file']})\n\n")
                self.raw_inspector_text.insert("end", "--- Raw header ---\n")
                self.raw_inspector_text.insert("end", (run["raw_header_text"] or "") + "\n\n")
                self.raw_inspector_text.insert("end", "--- Peaks ---\n")
                self.raw_inspector_text.insert("end", "gas\trt\trf\tarea\tamount\tconcentration\n")
                for p in db.get_peaks_for_run(self.conn, run_id):
                    self.raw_inspector_text.insert(
                        "end", f"{p['gas']}\t{p['rt']}\t{p['rf']}\t{p['area']}\t{p['amount']}\t{p['concentration']}\n"
                    )
        self.raw_inspector_text.configure(state="disabled")

    def _set_inspector_cell(self, run_id, gas=None):
        self._inspector_run_id = run_id
        self._inspector_gas = gas
        self._render_range_chart()
        self._refresh_raw_inspector()

    def _build_chart(self, parent):
        ttk.Label(parent, text="Composition breakdown", font=("", 10, "bold")).pack(
            fill="x", pady=(0, 4)
        )

        # A single figure with two side-by-side subplots (not two separate figures) -
        # this is what lets the zoom-region brackets be drawn as real matplotlib
        # artists (ConnectionPatch) spanning between the two axes, rather than
        # faked with overlaid Tk widgets.
        self.figure = Figure(figsize=(3.6, 4), dpi=100)
        self.figure.subplots_adjust(wspace=0.6)
        self.ax_full = self.figure.add_subplot(1, 2, 1)
        self.ax_zoom = self.figure.add_subplot(1, 2, 2)
        self.canvas = FigureCanvasTkAgg(self.figure, master=parent)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.mpl_connect("motion_notify_event", self._on_chart_motion)
        self.canvas.mpl_connect("button_press_event", self._on_chart_press)
        self.canvas.mpl_connect("button_release_event", self._on_chart_release)
        # Shift/Alt+wheel horizontal scroll, matching the fast-hscroll convention
        # already used on every tree in the app (bind_fast_hscroll) - once more
        # samples are selected than comfortably fit, this pans the window of bars
        # shown instead of squeezing them all into the same fixed chart width.
        self.canvas.get_tk_widget().bind("<Shift-MouseWheel>", self._on_chart_hscroll)
        self.canvas.get_tk_widget().bind("<Alt-MouseWheel>", self._on_chart_hscroll)
        self._chart_scroll_offset = 0  # index of the leftmost bar currently in view

        # zoom_ceiling: the right-hand chart always shows 0..ceiling% at full chart
        # height, so small trace gases that get squeezed flat against the bottom of
        # the full 0-100% chart become readable. Set by dragging the pill sitting on
        # the top bracket line - no separate Tk slider widget.
        self._zoom_ceiling = 20.0
        self._dragging_pill = False
        self._pill_artist = None
        self._pill_bbox = None  # (x0, y0, x1, y1) in display pixels, for hit-testing
        self._bracket_connectors = []  # ConnectionPatch artists spanning the two axes
        self._chart_hover_annotation = None
        self._chart_segments = []  # [(ax, rect, gas, percent, reason)]
        self._chart_data = []      # last-drawn [(label, {gas: percent}), ...]

        btn_bar = ttk.Frame(parent)
        btn_bar.pack(fill="x", pady=(4, 0))
        ttk.Button(btn_bar, text="Copy chart data", command=self.on_copy_chart_data).pack(side="left")
        ttk.Button(btn_bar, text="Export chart as image...", command=self.on_export_chart).pack(
            side="left", padx=(6, 0)
        )
        self._render_chart()

    def _build_tree(self):
        if self.tree is not None:
            self.tree.destroy()
        columns = ("highlight", "sample_name", "injection_date", "pressure") + tuple(self._gases) + \
            ("sum", "notes")
        self.tree = ttk.Treeview(self._tree_area, columns=columns, show="tree headings")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=90, stretch=False, anchor="w")
        labels = {"highlight": "Highlight", "sample_name": "Sample", "injection_date": "Date",
                  "pressure": "Pressure", "sum": "Sum", "notes": "Notes"}
        self._column_labels = labels
        for col in columns:
            self.tree.heading(col, text=labels.get(col, col))
            if col == "highlight":
                self.tree.column(col, width=22, stretch=False, anchor="center")
                continue
            if col in self._gases:
                # Clicking a gas column's header selects the whole column at once
                # (every currently-visible row) - the same range-selection used by
                # a manual drag-select, just pre-filled - so "Reassign curve for
                # selection..." can be applied to an entire gas in one click instead
                # of dragging through every row by hand.
                self.tree.heading(col, command=lambda g=col: self._on_gas_header_click(g))
            width = 160 if col in ("sample_name", "notes") else 90
            self.tree.column(col, width=width, stretch=(col in ("sample_name", "notes")), anchor="w")
        self.tree.tag_configure("group", background="#e8eef7")
        self.tree.tag_configure("group-member", foreground="#444444")

        if self._vsb is not None:
            self._vsb.destroy()
        if self._hsb is not None:
            self._hsb.destroy()
        self._vsb = ttk.Scrollbar(self._tree_area, orient="vertical", command=self.tree.yview)
        self._hsb = ttk.Scrollbar(self._tree_area, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=self._vsb.set, xscrollcommand=self._hsb.set)
        self._tree_area.columnconfigure(0, weight=1)
        self._tree_area.rowconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self._vsb.grid(row=0, column=1, sticky="ns")
        self._hsb.grid(row=1, column=0, sticky="ew")
        bind_fast_hscroll(self.tree)

        self.tree.bind("<Button-1>", self.on_cell_click, add="+")
        self.tree.bind("<Double-Button-1>", self.on_cell_double_click, add="+")
        self.tree.bind("<Button-3>", self.on_row_right_click)
        self.tree.bind("<ButtonPress-1>", self.on_row_press, add="+")
        self.tree.bind("<B1-Motion>", self.on_row_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self.on_row_release, add="+")
        self.tree.bind("<<TreeviewSelect>>", self._on_selection_changed)
        # Tracks which groups are expanded so _render_tree() (called by, among other
        # things, the "More digits" button) can restore that state instead of
        # hardcoding every group closed on every re-render.
        self.tree.bind("<<TreeviewOpen>>", self._on_group_open, add="+")
        self.tree.bind("<<TreeviewClose>>", self._on_group_close, add="+")
        self.tree.bind("<Configure>", lambda _e: self._update_overlays(), add="+")
        # Dragging a column border to resize it doesn't fire <Configure> on the
        # tree itself (only the widget's own outer size change does, and a column
        # resize is purely internal layout) - without this, every cell-border/
        # highlight overlay stays stuck at its pre-resize .place() coordinates
        # until some *other* event happens to trigger a refresh. A resize always
        # ends in a button release over the tree, so catching it here (layered
        # on top of on_row_release's own unrelated row-drag handling) is a simple,
        # reliable enough hook without having to specifically detect "that release
        # was a column-border drag."
        self.tree.bind("<ButtonRelease-1>", lambda _e: self._update_overlays(), add="+")
        self._hsb.configure(command=self._on_hscroll)
        self._vsb.configure(command=self._on_vscroll)
        self.tree.bind("<MouseWheel>", lambda _e: self.after(1, self._update_overlays), add="+")
        self.tree.bind("<Shift-MouseWheel>", lambda _e: self.after(1, self._update_overlays), add="+")

    def _on_hscroll(self, *args):
        self.tree.xview(*args)
        self._update_overlays()

    def _on_vscroll(self, *args):
        self.tree.yview(*args)
        self._update_overlays()

    # -- loading --------------------------------------------------------------
    def load_runs(self, run_ids):
        self._run_ids = list(run_ids)
        self._reload()

    def _reload(self):
        self._close_active_popup()
        self._range_selection = None
        rows = db.list_runs_for_pressure_entry(self.conn, run_ids=self._run_ids)
        sample_rows = [
            r for r in rows
            if r["pressure"] is not None
            and (r["standard_name"] or "").lower() == db.SAMPLE_STANDARD_NAME.lower()
        ]
        qualifying_ids = {r["run_id"] for r in sample_rows}
        self._rows = {r["run_id"]: r for r in sample_rows}

        all_groups = db.list_sample_groups(self.conn)
        self._group_members = {
            group_run_id: members for group_run_id, members in all_groups.items()
            if len(members) >= 2 and all(m in qualifying_ids for m in members)
        }
        grouped_member_ids = {m for members in self._group_members.values() for m in members}
        if self._group_members:
            group_rows = db.list_runs_for_pressure_entry(
                self.conn, run_ids=list(self._group_members.keys())
            )
            self._rows.update({r["run_id"]: r for r in group_rows})

        self._top_level_ids = [
            r["run_id"] for r in sample_rows if r["run_id"] not in grouped_member_ids
        ] + list(self._group_members.keys())

        all_ids = list(self._rows.keys())
        self._peaks_map = db.get_peaks_map_for_runs(self.conn, all_ids)
        gas_set = {s["gas"] for s in db.list_calibration_series(self.conn)}
        for gases in self._peaks_map.values():
            gas_set.update(gases.keys())
        self._gases = sorted(gas_set)
        self._series_by_gas = {g: db.list_calibration_series(self.conn, gas=g) for g in self._gases}
        self._series_selection = db.get_analysis_series_selection(self.conn, all_ids)
        self._ensure_series_selection(all_ids, self._gases)

        self._bounds_by_series = {}
        for gas, options in self._series_by_gas.items():
            for series in options:
                self._bounds_by_series[series["series_id"]] = calibration.get_series_area_bounds(
                    self.conn, series["series_id"]
                )

        self.status_label.configure(
            text=f"{len(sample_rows)} sample run(s) loaded, {len(self._group_members)} averaged group(s)."
            if sample_rows else "No Sample-linked runs with a pressure are currently loaded into Models."
        )
        self._build_tree()
        self._render_tree()
        self._render_chart()
        self._update_reassign_button_state()
        # A reload can drop the run the range chart/raw inspector were pointed
        # at (or its selected gas may no longer be relevant) - reset rather than
        # risk showing stale data for a run that's no longer in view.
        if self._inspector_run_id not in self._rows:
            self._set_inspector_cell(None, None)
        # Auto-justify on every load rather than requiring a manual click - a fresh
        # gas column (or one going away) otherwise leaves Notes squeezed down to its
        # old fixed width, which is exactly what cuts it off. winfo_width() needs
        # real screen geometry, which isn't there yet if the Analysis tab isn't the
        # one currently showing - see _on_notebook_tab_changed in gui.py for the
        # follow-up pass once it actually becomes visible.
        self.on_justify_columns()

    def _ensure_series_selection(self, run_ids, gases):
        """Defaults every (run_id, gas) cell to the first available curve (by the
        same order the picker lists them in) whenever it has no selection yet, or
        its previously-selected curve no longer exists - e.g. it was just deleted in
        Models, in which case this falls back to whatever is now first in the list.
        A selection that's still valid among the current options is always left
        untouched, so an edit elsewhere never silently overrides a deliberate pick.

        Critical exception: a gas that none of the *currently-loaded* standards
        (models_panel._candidates - exactly what "Load selected into Models" just
        built) actually has composition data for never gets auto-selected, even if
        some series for that gas happens to still exist in the database from a
        totally unrelated earlier batch of standards. Auto-picking a leftover curve
        silently produced nonsense (e.g. negative %) computed from a slope/intercept
        that has nothing to do with the samples on screen - a real, observed bug,
        not a hypothetical one. Any selection an earlier (buggy) load already made
        this way is actively cleared here too, since it was never a deliberate user
        choice - only a choice made through the per-cell picker counts as that."""
        model_gases = set(getattr(self.app.models_panel, "_candidates", {}).keys())
        for gas in gases:
            options = self._series_by_gas.get(gas, [])
            option_ids = {s["series_id"] for s in options}
            supported = gas in model_gases
            fallback = options[0]["series_id"] if options and supported else None
            for run_id in run_ids:
                current = self._series_selection.get((run_id, gas))
                if not supported:
                    if current is not None:
                        db.set_analysis_series_selection(self.conn, run_id, gas, None)
                        self._series_selection[(run_id, gas)] = None
                    continue
                if current is not None and current in option_ids:
                    continue
                if current == fallback:
                    continue
                db.set_analysis_series_selection(self.conn, run_id, gas, fallback)
                self._series_selection[(run_id, gas)] = fallback

    def on_curves_changed(self):
        """Called from Models whenever a gas's calibration series are created,
        renamed, deleted, or refit - keeps curve selections and computed values in
        sync without rebuilding the table, so scroll position and the chart's
        current selection survive. A brand-new gas with no existing column here only
        appears on the next full load (e.g. the next "Load selected into Models")."""
        if not self._rows or self.tree is None:
            return
        all_ids = list(self._rows.keys())
        self._series_by_gas = {g: db.list_calibration_series(self.conn, gas=g) for g in self._gases}
        self._ensure_series_selection(all_ids, self._gases)
        self._bounds_by_series = {}
        for gas, options in self._series_by_gas.items():
            for series in options:
                self._bounds_by_series[series["series_id"]] = calibration.get_series_area_bounds(
                    self.conn, series["series_id"]
                )
        for run_id in all_ids:
            self._refresh_row(run_id)
        self._render_chart()

    # -- per-cell math --------------------------------------------------------
    def _series_for(self, run_id, gas):
        series_id = self._series_selection.get((run_id, gas))
        if series_id is None:
            return None
        for series in self._series_by_gas.get(gas, []):
            if series["series_id"] == series_id:
                return series
        return None

    def compute_row_percentages(self, run_id):
        """{gas: (value, reason)} for every gas column - value is:
        - "BD" (below detection) if this run has no peak at all for that gas,
        - None if it has a peak but no calibration curve is selected for it yet,
        - a float percent otherwise.
        reason is None (in calibration range, or not applicable), "above", or
        "below" - whether this run's peak area falls outside the selected curve's
        own calibrated area range, and on which side."""
        row = self._rows.get(run_id)
        peaks = self._peaks_map.get(run_id, {})
        result = {}
        if row is None:
            return result
        pressure = row["pressure"]
        for gas in self._gases:
            peak = peaks.get(gas)
            if peak is None or peak["area"] is None:
                result[gas] = ("BD", None)
                continue
            series = self._series_for(run_id, gas)
            # A series with fewer than 2 points (or 1 without "force through
            # origin") has never been fit - slope/intercept are still NULL. Treat
            # that the same as "no curve selected" rather than crashing.
            if series is None or series["slope"] is None or series["intercept"] is None:
                result[gas] = (None, None)
                continue
            percent = calibration.compute_percent(peak["area"], pressure, series["slope"], series["intercept"])
            bounds = self._bounds_by_series.get(series["series_id"])
            reason = None
            if bounds is not None:
                if peak["area"] < bounds[0]:
                    reason = "below"
                elif peak["area"] > bounds[1]:
                    reason = "above"
            result[gas] = (percent, reason)
        return result

    # -- rendering --------------------------------------------------------------
    # Per-cell highlight for out-of-range gas values, via 4 thin border-strip
    # overlays (not a filled label) - ttk.Treeview has no native way to color one
    # cell's background, but a filled overlay label has to draw its own copy of the
    # cell's text, which drifts out of sync with the real cell (e.g. "More digits"
    # changing precision) and visibly hides the real value underneath. A border
    # leaves the cell's own real text fully visible, showing through the middle.
    # Colored by which side of the calibrated range the value fell off of - both
    # pale, paired tones (same lightness/saturation, blue vs. orange) rather than a
    # strong red, so an out-of-range flag doesn't visually compete with the
    # negative-value flag below (which keeps red, since a negative percent is a
    # correctness problem, not just "outside the curve's tested range"). Similar in
    # character to the Duplicates wizard's pale cell-pick blue but deliberately not
    # the exact same shade, so the two never read as the same kind of highlight.
    OUT_OF_RANGE_ABOVE_BORDER = "#f0b88f"  # pale orange - above the top of the range
    OUT_OF_RANGE_BELOW_BORDER = "#8fb8f0"  # pale blue - below the bottom of the range
    NEGATIVE_VALUE_BORDER = "#f0a0a0"      # pale red - a physically-impossible negative percent
    RANGE_SELECTION_BORDER = "#1a56db"     # a range-selected cell (for bulk curve reassignment) -
                                            # a strong blue, matching the app's selection-blue family
                                            # (e.g. the Selector tab's "#bdddff" selected-row tint)
                                            # but bolder, since a thin border needs more saturation
                                            # than a fill to read clearly at only a couple pixels wide.
    CELL_BORDER_THICKNESS = 2

    def on_toggle_more_digits(self):
        self._decimal_places = self._decimal_places + 1 if self._decimal_places < 6 else 2
        self.more_digits_button.configure(text=f".{'0' * self._decimal_places} →")
        self._render_tree()

    def _format_percent(self, value):
        # Fixed to the current precision, then trailing zeros (and a bare trailing
        # "." for whole numbers) are stripped - raising the digit ceiling never
        # fabricates precision a value doesn't actually have, it only lets real
        # extra digits show through when they exist.
        s = f"{value:.{self._decimal_places}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return f"{s}%"

    def _row_values(self, run_id):
        row = self._rows[run_id]
        percentages = self.compute_row_percentages(run_id)
        gas_cells = []
        total = 0.0
        for gas in self._gases:
            value, _reason = percentages.get(gas, ("BD", None))
            if value == "BD":
                gas_cells.append("BD")
            elif value is None:
                gas_cells.append("(select curve)")
            else:
                gas_cells.append(self._format_percent(value))
                total += value
        pressure = "" if row["pressure"] is None else f"{row['pressure']:g}"
        values = ("", row["sample_name"] or "", (row["injection_date"] or "")[:19], pressure) + tuple(
            gas_cells
        ) + (self._format_percent(total), row["notes"] or "")
        return values

    def _results_table_rows(self):
        """Mirrors exactly what's on screen (including group/member rows), but with
        the "highlight" chip and any per-cell highlight resolved to real text -
        both are drawn as color-only overlays on screen, which copy/export can't
        carry, so this is what makes them "transfer with the copy/export buttons"
        as an actual annotation instead of silently vanishing."""
        columns = self.tree["columns"]
        header = [self._column_labels.get(c, c) for c in columns]
        rows = [header]

        run_ids = []
        for iid in self.tree.get_children(""):
            prefix, _, rest = iid.partition("-")
            if prefix in ("row", "group"):
                run_ids.append(int(rest))
            for child in self.tree.get_children(iid):
                cprefix, _, crest = child.partition("-")
                if cprefix in ("row", "group"):
                    run_ids.append(int(crest))
        cell_highlights = db.get_cell_highlights_for_runs(self.conn, run_ids)
        run_highlights = db.get_run_highlight_labels(self.conn)

        def row_for(iid, run_id):
            values = list(self.tree.item(iid, "values"))
            for i, col in enumerate(columns):
                if col == "highlight":
                    highlight = run_highlights.get(run_id)
                    values[i] = (highlight[1] or highlight[0]) if highlight else ""
                elif col in self._gases:
                    highlight = cell_highlights.get((run_id, col))
                    if highlight is not None:
                        label = highlight[1] or highlight[0]
                        values[i] = f"{values[i]} [{label}]"
            return values

        for iid in self.tree.get_children(""):
            prefix, _, rest = iid.partition("-")
            if prefix not in ("row", "group"):
                continue
            run_id = int(rest)
            rows.append(row_for(iid, run_id))
            for child in self.tree.get_children(iid):
                cprefix, _, crest = child.partition("-")
                if cprefix not in ("row", "group"):
                    continue
                rows.append(row_for(child, int(crest)))
        return rows

    def on_copy_results_table(self):
        rows = self._results_table_rows()
        if len(rows) <= 1:
            return
        self.clipboard_clear()
        self.clipboard_append(export.rows_to_tsv(rows))

    def on_export_results_csv(self):
        rows = self._results_table_rows()
        if len(rows) <= 1:
            messagebox.showwarning("Nothing to export", "The results table is empty.")
            return
        out_path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV file", "*.csv")])
        if not out_path:
            return
        export.write_csv(rows, out_path)
        messagebox.showinfo("Export complete", f"Wrote {out_path}")

    def _on_group_open(self, _event=None):
        iid = self.tree.focus()
        if iid.startswith("group-"):
            self._expanded_groups.add(int(iid.split("-", 1)[1]))

    def _on_group_close(self, _event=None):
        iid = self.tree.focus()
        if iid.startswith("group-"):
            self._expanded_groups.discard(int(iid.split("-", 1)[1]))

    def _render_tree(self):
        self.tree.delete(*self.tree.get_children())
        for run_id in self._top_level_ids:
            if run_id in self._group_members:
                values = self._row_values(run_id)
                self.tree.insert(
                    "", "end", iid=f"group-{run_id}", text="",
                    open=(run_id in self._expanded_groups), values=values, tags=["group"],
                )
                for member_run_id in self._group_members[run_id]:
                    if member_run_id not in self._rows:
                        continue
                    m_values = self._row_values(member_run_id)
                    self.tree.insert(
                        f"group-{run_id}", "end", iid=f"row-{member_run_id}",
                        text=DRAG_HANDLE_GLYPH, values=m_values, tags=["group-member"],
                    )
            else:
                if run_id not in self._rows:
                    continue
                values = self._row_values(run_id)
                self.tree.insert(
                    "", "end", iid=f"row-{run_id}", text=DRAG_HANDLE_GLYPH, values=values, tags=[]
                )
        self._update_overlays()

    def _refresh_row(self, run_id):
        for iid in (f"row-{run_id}", f"group-{run_id}"):
            if self.tree.exists(iid):
                self.tree.item(iid, values=self._row_values(run_id))
        self._update_overlays()

    def _place_cell_border(self, x, y, w, h, color, iid, run_id, gas, tooltip_text=None, labels=None):
        """4 thin colored strips outlining the cell's bbox, not a filled label -
        leaves the real Treeview text in the middle fully visible/legible (a filled
        overlay would have to draw its own copy of that text, which is exactly what
        went stale and hid extra digits when "More digits" raised the precision).

        `labels`, if given, is an existing list of 4 Labels from a prior call for
        this same (run_id, gas) - they're repositioned/recolored in place instead
        of destroyed and recreated. Reuse matters here specifically: this table can
        have dozens of flagged cells at once, and creating 4 brand-new Tk widgets
        per cell on *every* scroll tick/resize/edit (the old behavior - destroy
        everything, rebuild from scratch, every single call) is what made the
        table noticeably slow to interact with. Click/drag bindings are still
        rebound every call regardless, since they close over this cell's current
        x/y, which does change across calls (scroll, resize, row reorder)."""
        t = self.CELL_BORDER_THICKNESS
        strips = [
            (x, y, w, t),               # top
            (x, y + h - t, w, t),       # bottom
            (x, y, t, h),               # left
            (x + w - t, y, t, h),       # right
        ]
        is_new = labels is None
        if is_new:
            labels = [tk.Label(self.tree, bd=0) for _ in strips]
        for label, (sx, sy, sw, sh) in zip(labels, strips):
            label.configure(bg=color)
            label.place(x=sx, y=sy, width=sw, height=sh)
            # Text is stored on the widget and read back by a callable passed to
            # bind_tooltip below, rather than updated by rebinding on every call -
            # bind_tooltip's own bindings use add="+", so calling it more than
            # once per widget would stack duplicate handlers on a *reused* label
            # (harmless for a brand-new one, since the old code never reused
            # widgets, but very much not harmless once reuse is in play).
            label._tooltip_text = tooltip_text or ""
            # Same reasoning for the plain click/drag bindings below: since a
            # label can now be reused across calls with a *different* x/y (the
            # cell scrolled, a column resized, ...), they're rebound every call -
            # but with plain .bind() (no add="+"), which replaces rather than
            # stacks the previous handler for that event on this widget.
            label.bind("<Button-1>", lambda _e, ii=iid: self._select_row_via_overlay(ii))
            label.bind("<ButtonPress-1>", lambda e, lx=x, ly=y: self._on_overlay_press(e, lx, ly))
            label.bind("<B1-Motion>", lambda e, lx=x, ly=y: self._on_overlay_motion(e, lx, ly))
            label.bind("<ButtonRelease-1>", self.on_row_release)
            label.bind(
                "<Double-Button-1>", lambda _e, ii=iid, ri=run_id, g=gas: self._open_series_picker(ii, ri, g)
            )
            if is_new:
                bind_tooltip(label, lambda lbl=label: getattr(lbl, "_tooltip_text", ""))
        return labels

    def _update_out_of_range_overlays(self):
        """(Re)places a colored border around every currently-visible, out-of-range
        (or range-selected) gas cell - the only way to mark a single ttk.Treeview
        cell, since the widget has no native per-cell background/border. Re-run
        after any render and after scrolling/resizing/column-resizing, since
        .place() coordinates are viewport-relative and go stale the moment the
        view moves or a column's width changes.

        Reuses existing Label widgets for any (run_id, gas) that was already
        overlaid last call (see _place_cell_border) and only destroys/creates for
        cells whose flagged status actually changed - a resize/scroll that
        doesn't change *which* cells are flagged now costs a handful of cheap
        .place() calls instead of hundreds of widget creations."""
        if self.tree is None:
            return
        # Without this, the very first call right after _build_tree() (a freshly
        # created Treeview, columns not yet laid out) reads bbox() before the
        # widget's real column geometry is committed - each overlay lands about a
        # character-width off from the cell it's supposed to cover, until the next
        # scroll/resize event happens to trigger a recompute against the by-then-
        # correct geometry.
        self.tree.update_idletasks()
        self._cell_highlights = db.get_cell_highlights_for_runs(self.conn, list(self._rows.keys()))
        still_needed = set()
        for iid in self.tree.get_children("") + tuple(
            child for parent in self.tree.get_children("") for child in self.tree.get_children(parent)
        ):
            prefix, _, rest = iid.partition("-")
            if prefix not in ("row", "group"):
                continue
            run_id = int(rest)
            percentages = self.compute_row_percentages(run_id)
            for gas in self._gases:
                value, reason = percentages.get(gas, ("BD", None))
                is_out_of_range = reason is not None and isinstance(value, (int, float))
                is_negative = isinstance(value, (int, float)) and value < 0
                is_range_selected = (
                    self._range_selection is not None
                    and self._range_selection["gas"] == gas
                    and run_id in self._range_selection["run_ids"]
                )
                cell_highlight = self._cell_highlights.get((run_id, gas))
                # "Show highlights" hides the decorative flags (out-of-range,
                # negative, a saved cell-highlight annotation) but never an active
                # range-selection - that's live operational feedback for whatever
                # you're mid-way through doing (e.g. picking cells to reassign a
                # curve for), not a decorative marking the toggle is meant to
                # declutter.
                if not self._show_highlights_var.get() and not is_range_selected:
                    continue
                if not is_out_of_range and not is_negative and not is_range_selected and cell_highlight is None:
                    continue
                bbox = self.tree.bbox(iid, gas)
                if not bbox:
                    continue
                still_needed.add((run_id, gas))
                x, y, w, h = bbox
                # Priority: an active range-selection (transient, mid-operation) wins
                # over a user's own deliberate cell highlight (a persistent
                # annotation), which wins over a negative value (a correctness
                # problem worth flagging above a merely-out-of-range one), which in
                # turn wins over the automatic out-of-range flag (informational) -
                # so a click-to-select never gets masked by a note someone left
                # earlier.
                tooltip_text = None
                if is_range_selected:
                    color = self.RANGE_SELECTION_BORDER
                elif cell_highlight is not None:
                    color, note_label = cell_highlight
                    tooltip_text = note_label or None
                elif is_negative:
                    color = self.NEGATIVE_VALUE_BORDER
                    tooltip_text = f"This run's computed {gas} percent is negative - not physically possible."
                elif reason == "above":
                    color = self.OUT_OF_RANGE_ABOVE_BORDER
                else:
                    color = self.OUT_OF_RANGE_BELOW_BORDER
                if tooltip_text is None and is_out_of_range:
                    tooltip_text = f"This run's {gas} peak area is {reason} the selected curve's calibrated range."
                self._cell_overlays[(run_id, gas)] = self._place_cell_border(
                    x, y, w, h, color, iid, run_id, gas, tooltip_text,
                    labels=self._cell_overlays.get((run_id, gas)),
                )
        # Anything overlaid last time but not needed anymore (the flag cleared, or
        # the row/gas scrolled out of the currently-rendered set) gets torn down -
        # this is the only remaining destroy path, scoped to just the delta.
        for key in list(self._cell_overlays):
            if key not in still_needed:
                for label in self._cell_overlays.pop(key):
                    label.destroy()

    def _update_highlight_overlays(self):
        """Same colored-chip-over-a-cell technique as the Selector tab's own
        "highlight" column - keeps a run's highlight swatch visible here too."""
        for label in self._highlight_overlays.values():
            label.destroy()
        self._highlight_overlays = {}
        if self.tree is None:
            return
        self.tree.update_idletasks()
        colors = db.get_run_highlight_colors(self.conn)
        if not colors:
            return
        for iid in self.tree.get_children("") + tuple(
            child for parent in self.tree.get_children("") for child in self.tree.get_children(parent)
        ):
            prefix, _, rest = iid.partition("-")
            if prefix not in ("row", "group"):
                continue
            run_id = int(rest)
            color = colors.get(run_id)
            if not color:
                continue
            bbox = self.tree.bbox(iid, "highlight")
            if not bbox:
                continue
            x, y, w, h = bbox
            label = tk.Label(self.tree, bg=color)
            label.place(x=x, y=y, width=w, height=h)
            self._highlight_overlays[run_id] = label

    def _update_overlays(self):
        self._update_out_of_range_overlays()
        self._update_highlight_overlays()

    def _select_row_via_overlay(self, iid):
        self.tree.selection_set(iid)
        self.tree.focus(iid)

    def _on_overlay_press(self, event, label_x, label_y):
        self.on_row_press(_TranslatedEvent(label_x + event.x, label_y + event.y))

    def _on_overlay_motion(self, event, label_x, label_y):
        self.on_row_motion(_TranslatedEvent(label_x + event.x, label_y + event.y))

    # -- gas-cell series picker -------------------------------------------------
    def _cell_at(self, event):
        """(row_iid, run_id, col_name) under this click, or None if it's not a real
        data cell."""
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        col_id = self.tree.identify_column(event.x)
        row_iid = self.tree.identify_row(event.y)
        if not row_iid or not row_iid.startswith("row-") and not row_iid.startswith("group-"):
            return None
        run_id = int(row_iid.split("-", 1)[1])
        columns = self.tree["columns"]
        try:
            col_name = columns[int(col_id.lstrip("#")) - 1]
        except (ValueError, IndexError):
            return None
        return row_iid, run_id, col_name

    def on_cell_click(self, event):
        # Single click: notes edits, or (for a gas cell) selects it as "the
        # selected cell" - driving the calibration-range chart and raw
        # inspector underneath the table. Changing a gas cell's curve still
        # requires a double click, so a single click can't accidentally
        # reassign a curve mid-scroll.
        hit = self._cell_at(event)
        if hit is None:
            return
        row_iid, run_id, col_name = hit
        if col_name == "notes":
            self._open_notes_editor(row_iid, run_id)
        elif col_name in self._gases:
            self._set_inspector_cell(run_id, col_name)
        else:
            self._set_inspector_cell(run_id, None)

    def on_cell_double_click(self, event):
        hit = self._cell_at(event)
        if hit is None:
            return
        row_iid, run_id, col_name = hit
        if col_name in self._gases:
            self._open_series_picker(row_iid, run_id, col_name)

    def _open_series_picker(self, row_iid, run_id, gas):
        self._close_active_popup()
        bbox = self.tree.bbox(row_iid, gas)
        if not bbox:
            return
        x, y, w, h = bbox
        options = self._series_by_gas.get(gas, [])
        names = [NONE_SERIES_LABEL] + [s["name"] for s in options]
        listbox = tk.Listbox(self.tree, height=min(len(names), 8), exportselection=False)
        for name in names:
            listbox.insert("end", name)
        listbox.place(x=x, y=y, width=max(w, 140))

        current = self._series_selection.get((run_id, gas))
        if current is not None:
            for i, s in enumerate(options):
                if s["series_id"] == current:
                    listbox.selection_set(i + 1)
                    break
        else:
            listbox.selection_set(0)

        def commit(_event=None):
            selection = listbox.curselection()
            if not selection:
                self._close_active_popup()
                return
            idx = selection[0]
            series_id = None if idx == 0 else options[idx - 1]["series_id"]
            db.set_analysis_series_selection(self.conn, run_id, gas, series_id)
            self._series_selection[(run_id, gas)] = series_id
            self._close_active_popup()
            self._refresh_row(run_id)
            # The range chart caches "the selected cell" independently of the
            # results table - without this it kept showing whichever curve was
            # assigned *before* this reassignment (confirmed bug: for a gas with
            # more than one curve, like He with separate Std-1/Std-3 fits, moving
            # a cell from one to the other left the chart showing the old curve's
            # range until some unrelated action happened to trigger a redraw).
            if self._inspector_run_id == run_id and self._inspector_gas == gas:
                self._render_range_chart()

        listbox.bind("<<ListboxSelect>>", commit)
        listbox.bind("<Escape>", lambda _e: self._close_active_popup())
        listbox.focus_set()
        self._active_popup = listbox
        self._active_popup_close = None  # the listbox commits itself on selection
        self._suppress_next_global_close = True

    def _open_notes_editor(self, row_iid, run_id):
        self._close_active_popup()
        bbox = self.tree.bbox(row_iid, "notes")
        if not bbox:
            return
        x, y, w, h = bbox
        entry = tk.Entry(self.tree)
        entry.insert(0, self._rows[run_id]["notes"] or "")
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.selection_range(0, "end")

        def save():
            db.set_run_notes(self.conn, run_id, entry.get())
            row = db.list_runs_for_pressure_entry(self.conn, run_ids=[run_id])
            if row:
                self._rows[run_id] = row[0]
            self._refresh_row(run_id)

        entry.bind("<Return>", lambda _e: self._close_active_popup())
        entry.bind("<Escape>", lambda _e: self._close_active_popup(commit=False))
        self._active_popup = entry
        self._active_popup_close = save  # invoked on Enter or click-away, skipped on Escape
        self._suppress_next_global_close = True

    def _close_active_popup(self, commit=True):
        if self._active_popup is not None:
            popup = self._active_popup
            close_cb = self._active_popup_close
            self._active_popup = None
            self._active_popup_close = None
            if commit and close_cb is not None:
                close_cb()
            try:
                popup.destroy()
            except tk.TclError:
                pass

    def _on_global_click(self, event):
        if self._suppress_next_global_close:
            # The very click that opened a popup (on_cell_click, bound directly to
            # the tree) also reaches this bind_all handler on its way through - since
            # widget bindings fire before "all" bindings, without this the popup
            # would be destroyed on the same click that just created it.
            self._suppress_next_global_close = False
            return
        if self._active_popup is not None and event.widget is not self._active_popup:
            self._close_active_popup()

    # -- drag-to-group / range-select --------------------------------------------
    def on_row_press(self, event):
        col_id = self.tree.identify_column(event.x)
        if col_id == "#0":
            iid = self.tree.identify_row(event.y)
            if iid and iid.startswith("row-"):
                self._drag_row_iid = iid
                self._drag_press_xy = (event.x, event.y)
                self._drag_active = False
            return
        col_name = self._column_name_at(col_id)
        if col_name in self._gases:
            iid = self.tree.identify_row(event.y)
            if iid:
                self._range_drag_gas = col_name
                self._range_drag_anchor = iid
                self._apply_range_selection(col_name, [iid])

    def _column_name_at(self, col_id):
        columns = self.tree["columns"]
        try:
            return columns[int(col_id.lstrip("#")) - 1]
        except (ValueError, IndexError):
            return None

    def _on_gas_header_click(self, gas):
        self._apply_range_selection(gas, self._flattened_result_iids())

    def _apply_range_selection(self, gas, iids):
        run_ids = []
        for iid in iids:
            prefix, _, rest = iid.partition("-")
            if prefix in ("row", "group"):
                run_ids.append(int(rest))
        self._range_selection = {"gas": gas, "run_ids": run_ids}
        self._update_overlays()
        self._update_reassign_button_state()

    def _update_reassign_button_state(self):
        enabled = bool(self._range_selection and self._range_selection["run_ids"])
        self.reassign_button.configure(state="normal" if enabled else "disabled")

    def on_reassign_curve_for_selection(self):
        if not self._range_selection or not self._range_selection["run_ids"]:
            return
        gas = self._range_selection["gas"]
        run_ids = list(self._range_selection["run_ids"])
        options = self._series_by_gas.get(gas, [])
        if not options:
            messagebox.showwarning("No curves", f"No calibration curves exist for {gas} yet.")
            return

        popup = tk.Toplevel(self)
        popup.title(f"Reassign {gas} curve")
        popup.transient(self.winfo_toplevel())
        ttk.Label(
            popup, text=f"Apply a curve to {len(run_ids)} selected {gas} cell(s):",
        ).pack(anchor="w", padx=8, pady=(8, 4))
        listbox = tk.Listbox(popup, exportselection=False, height=min(len(options) + 1, 10))
        listbox.pack(fill="both", expand=True, padx=8)
        listbox.insert("end", NONE_SERIES_LABEL)
        for series in options:
            listbox.insert("end", series["name"])
        listbox.selection_set(0)
        listbox.focus_set()

        def apply_and_close():
            selection = listbox.curselection()
            if not selection:
                popup.destroy()
                return
            idx = selection[0]
            series_id = None if idx == 0 else options[idx - 1]["series_id"]
            for run_id in run_ids:
                db.set_analysis_series_selection(self.conn, run_id, gas, series_id)
                self._series_selection[(run_id, gas)] = series_id
            popup.destroy()
            self._range_selection = None
            self._update_reassign_button_state()
            for run_id in run_ids:
                self._refresh_row(run_id)
            self._render_chart()
            if self._inspector_gas == gas and self._inspector_run_id in run_ids:
                self._render_range_chart()

        listbox.bind("<Double-Button-1>", lambda _e: apply_and_close())
        listbox.bind("<Return>", lambda _e: apply_and_close())
        listbox.bind("<Escape>", lambda _e: popup.destroy())

        btn_bar = ttk.Frame(popup)
        btn_bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(btn_bar, text="Cancel", command=popup.destroy).pack(side="right")
        ttk.Button(btn_bar, text="Apply", command=apply_and_close).pack(side="right", padx=(0, 6))

    def _flattened_result_iids(self):
        result = []
        for iid in self.tree.get_children(""):
            result.append(iid)
            result.extend(self.tree.get_children(iid))
        return result

    def on_row_motion(self, event):
        if self._range_drag_gas is not None:
            iid = self.tree.identify_row(event.y)
            if not iid:
                return
            order = self._flattened_result_iids()
            if self._range_drag_anchor not in order or iid not in order:
                return
            i, j = order.index(self._range_drag_anchor), order.index(iid)
            lo, hi = min(i, j), max(i, j)
            self._apply_range_selection(self._range_drag_gas, order[lo:hi + 1])
            # Dragging into a second gas column mid-drag makes the selection
            # ambiguous about which curve it's for - the button stays disabled
            # (see on_reassign_curve_for_selection) until the user starts a fresh
            # drag confined to one column.
            if self._column_name_at(self.tree.identify_column(event.x)) not in (None, self._range_drag_gas):
                self.reassign_button.configure(state="disabled")
            return
        if not self._drag_row_iid:
            return
        if not self._drag_active:
            px, py = self._drag_press_xy
            if (event.x - px) ** 2 + (event.y - py) ** 2 < DRAG_THRESHOLD_PX ** 2:
                return
            self._drag_active = True
            self.tree.configure(cursor="hand2")
            self._status_text_before_drag = self.status_label.cget("text")
        target_iid = self.tree.identify_row(event.y)
        if target_iid != self._drag_hover_iid:
            if self._drag_hover_iid and self.tree.exists(self._drag_hover_iid):
                self._clear_hover_tag(self._drag_hover_iid)
            self._drag_hover_iid = target_iid
            if target_iid and target_iid != self._drag_row_iid:
                self._set_hover_tag(target_iid)
                target_name = self.tree.set(target_iid, "sample_name") or target_iid
                self.status_label.configure(text=f"Drop to average with “{target_name}”")
            elif not target_iid:
                self.status_label.configure(text="Drop here to remove from its averaged group")
            else:
                self.status_label.configure(text=self._status_text_before_drag)

    def _set_hover_tag(self, iid):
        tags = list(self.tree.item(iid, "tags"))
        if "drop-target" not in tags:
            tags.append("drop-target")
            self.tree.tag_configure("drop-target", background="#3a7bd5", foreground="#ffffff")
            self.tree.item(iid, tags=tags)

    def _clear_hover_tag(self, iid):
        tags = [t for t in self.tree.item(iid, "tags") if t != "drop-target"]
        self.tree.item(iid, tags=tags)

    def on_row_release(self, _event):
        if self._range_drag_gas is not None:
            # The selection itself (self._range_selection) deliberately survives the
            # release - it stays highlighted and the button stays enabled until the
            # user starts a new drag (or the table reloads), same as a normal
            # spreadsheet range staying selected after you let go of the mouse.
            self._range_drag_gas = None
            self._range_drag_anchor = None
            return
        dragged_iid = self._drag_row_iid
        was_dragging = self._drag_active
        target_iid = self._drag_hover_iid
        self._drag_row_iid = None
        self._drag_active = False
        self._drag_hover_iid = None
        self.tree.configure(cursor="")
        if self._status_text_before_drag is not None:
            self.status_label.configure(text=self._status_text_before_drag)
            self._status_text_before_drag = None
        if target_iid and self.tree.exists(target_iid):
            self._clear_hover_tag(target_iid)
        if not dragged_iid or not was_dragging:
            return
        dragged_run_id = int(dragged_iid.split("-", 1)[1])
        dragged_parent = self.tree.parent(dragged_iid)

        if not target_iid or target_iid == dragged_iid:
            if dragged_parent.startswith("group-"):
                group_run_id = int(dragged_parent.split("-", 1)[1])
                db.remove_sample_group_member(self.conn, group_run_id, dragged_run_id)
                self._reload()
            return

        if target_iid.startswith("group-"):
            group_run_id = int(target_iid.split("-", 1)[1])
            if dragged_parent == target_iid:
                return
            # If the dragged run already belongs to a different group, leave that one
            # first - otherwise it would end up counted in two groups' averages at
            # once, which "moving" a row should never do.
            if dragged_parent.startswith("group-") and dragged_parent != target_iid:
                db.remove_sample_group_member(self.conn, int(dragged_parent.split("-", 1)[1]), dragged_run_id)
            db.add_sample_group_member(self.conn, group_run_id, dragged_run_id)
            self._reload()
            return

        if target_iid.startswith("row-"):
            target_run_id = int(target_iid.split("-", 1)[1])
            if target_run_id == dragged_run_id:
                return
            target_parent = self.tree.parent(target_iid)
            if target_parent == dragged_parent and target_parent.startswith("group-"):
                return  # already members of the same group
            if dragged_parent.startswith("group-"):
                db.remove_sample_group_member(self.conn, int(dragged_parent.split("-", 1)[1]), dragged_run_id)
            if target_parent.startswith("group-"):
                group_run_id = int(target_parent.split("-", 1)[1])
                db.add_sample_group_member(self.conn, group_run_id, dragged_run_id)
            else:
                db.create_sample_group(self.conn, [target_run_id, dragged_run_id])
            self._reload()

    def on_row_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        menu = tk.Menu(self, tearoff=0)

        if iid.startswith("group-"):
            group_run_id = int(iid.split("-", 1)[1])
            menu.add_command(
                label="Dissolve group",
                command=lambda: (db.dissolve_sample_group(self.conn, group_run_id), self._reload()),
            )

        hit = self._cell_at(event)
        if hit is not None:
            _row_iid, run_id, col_name = hit
            if col_name in self._gases:
                # If there's an active range-selection on this same gas column,
                # "Set highlight" applies to every cell in it, not just the one
                # that happened to be right-clicked - matches "Reassign curve for
                # selection...", the other bulk action on this same selection.
                # Right-clicking a cell outside any selection (or on a different
                # gas) still targets just that one cell, same as before.
                if (self._range_selection is not None and self._range_selection["gas"] == col_name
                        and run_id in self._range_selection["run_ids"]):
                    target_run_ids = list(self._range_selection["run_ids"])
                else:
                    target_run_ids = [run_id]
                if menu.index("end") is not None:
                    menu.add_separator()
                highlight_menu = tk.Menu(menu, tearoff=0)
                highlight_menu.add_command(
                    label="(none)",
                    command=lambda: self._set_cell_highlight_bulk(target_run_ids, col_name, None),
                )
                swatches = db.list_highlight_swatches(self.conn)
                if swatches:
                    highlight_menu.add_separator()
                    for swatch in swatches:
                        label = swatch["label"] or swatch["color"]
                        highlight_menu.add_command(
                            label=label, background=swatch["color"],
                            command=lambda sid=swatch["swatch_id"]: self._set_cell_highlight_bulk(
                                target_run_ids, col_name, sid
                            ),
                        )
                highlight_menu.add_separator()
                highlight_menu.add_command(label="Edit swatches...", command=self.app.on_edit_highlight_swatches)
                count_suffix = f" - {len(target_run_ids)} cell(s)" if len(target_run_ids) > 1 else ""
                menu.add_cascade(label=f"Set highlight ({col_name}){count_suffix}", menu=highlight_menu)

        if menu.index("end") is None:
            return
        menu.tk_popup(event.x_root, event.y_root)

    def _set_cell_highlight_bulk(self, run_ids, gas, swatch_id):
        for run_id in run_ids:
            db.set_cell_highlight(self.conn, run_id, gas, swatch_id)
        self._update_overlays()

    # -- composition chart --------------------------------------------------------
    def _on_selection_changed(self, _event=None):
        # Re-center the zoomed panel's default height on whatever's actually in
        # this selection - a mostly-N2 standard and a trace-gas-heavy one shouldn't
        # start at the same fixed 20% every time. Only reset here (a genuinely new
        # selection), not on every re-render (curve edits, group changes, the pill
        # drag itself) - those should leave whatever height the user is already
        # looking at alone.
        self._zoom_ceiling = self._default_zoom_ceiling(self._selected_run_ids())
        self._render_chart()

    def _default_zoom_ceiling(self, run_ids):
        """1.15x the largest *stacked total* (the sum of every gas's percent,
        per row) among these rows - not just the single tallest gas. The chart
        is a stacked bar, so its actual full height is the sum of its segments;
        sizing the zoom off only the biggest individual gas could leave the top
        of a bar cut off the moment a sample has more than one measured gas
        contributing real height. E.g. a run with 20% + 10% + 5% across three
        gases needs headroom for the full 35% stack, not just 20%*1.15.
        Falls back to the old fixed default when there's nothing measured yet
        (nothing selected, or every cell is still BD/unselected-curve)."""
        max_sum = 0.0
        for run_id in run_ids:
            row_sum = sum(
                value for value, _reason in self.compute_row_percentages(run_id).values()
                if isinstance(value, (int, float))
            )
            max_sum = max(max_sum, row_sum)
        return max_sum * 1.15 if max_sum > 0 else 20.0

    def _selected_run_ids(self):
        run_ids = []
        for iid in self.tree.selection() if self.tree else []:
            prefix, _, rest = iid.partition("-")
            if prefix in ("row", "group"):
                run_ids.append(int(rest))
        return run_ids

    BAR_EDGE_COLOR = "#999999"       # grey, not black - the bar/segment outlines
    OUT_OF_RANGE_EDGE_COLOR = "#e0a800"  # yellow/amber outline for an out-of-range segment
    BARS_PER_VIEW = 6  # composition chart shows at most this many bars at once before scrolling

    def _on_chart_hscroll(self, event):
        n = len(self._selected_run_ids())
        if n <= self.BARS_PER_VIEW:
            return
        delta = 1 if event.delta < 0 else -1
        max_offset = n - self.BARS_PER_VIEW
        new_offset = max(0, min(max_offset, self._chart_scroll_offset + delta))
        if new_offset != self._chart_scroll_offset:
            self._chart_scroll_offset = new_offset
            self._render_chart()

    def _render_chart(self):
        self.ax_full.clear()
        self.ax_zoom.clear()
        self._chart_segments = []
        self._chart_data = []
        if self._pill_artist is not None:
            try:
                self._pill_artist.remove()
            except (ValueError, NotImplementedError):
                pass
            self._pill_artist = None
        self._pill_bbox = None
        # The bracket connectors are added via figure.add_artist() (they span
        # between two axes, so they can't belong to just one) - ax.clear() above
        # only clears each axes' OWN artists (which is why the axhlines are fine),
        # never these, so without this they silently piled up forever, one more
        # pair every single render - including every mouse-motion tick while
        # dragging the pill, which is exactly what produced the accumulating fan
        # of stale diagonal lines.
        for connector in self._bracket_connectors:
            try:
                connector.remove()
            except (ValueError, NotImplementedError):
                pass
        self._bracket_connectors = []
        run_ids = self._selected_run_ids()
        max_offset = max(0, len(run_ids) - self.BARS_PER_VIEW)
        self._chart_scroll_offset = max(0, min(max_offset, self._chart_scroll_offset))
        if not run_ids:
            for ax in (self.ax_full, self.ax_zoom):
                ax.text(0.5, 0.5, "Select a\nsample row",
                        ha="center", va="center", transform=ax.transAxes, color="#888888", fontsize=8)
                ax.set_xticks([])
                ax.set_yticks([])
            self._chart_hover_annotation = None
            self.canvas.draw_idle()
            return

        ceiling = max(self._zoom_ceiling, 0.5)
        x_positions = range(len(run_ids))
        for x, run_id in zip(x_positions, run_ids):
            row = self._rows.get(run_id)
            label = (row["sample_name"] or f"run {run_id}") if row else f"run {run_id}"
            percentages = self.compute_row_percentages(run_id)
            gas_percents = {g: v[0] for g, v in percentages.items() if isinstance(v[0], (int, float))}
            gas_reasons = {g: v[1] for g, v in percentages.items()}
            self._chart_data.append((label, gas_percents))
            for ax, label_min in ((self.ax_full, 3.0), (self.ax_zoom, ceiling * 0.03)):
                bottom = 0.0
                for gas in self._gases:
                    percent = gas_percents.get(gas)
                    if percent is None:
                        continue
                    reason = gas_reasons.get(gas)
                    color = _gas_color(gas)
                    edge = self.OUT_OF_RANGE_EDGE_COLOR if reason else self.BAR_EDGE_COLOR
                    lw = 2.0 if reason else 0.5
                    rect = ax.bar(x, percent, bottom=bottom, width=0.6, color=color,
                                  edgecolor=edge, linewidth=lw, zorder=2)[0]
                    self._chart_segments.append((ax, rect, gas, percent, reason))
                    if percent >= label_min:
                        text_color = _text_color_for_bg(color)
                        ax.text(x, bottom + percent / 2, f"{percent:.1f}%", ha="center", va="center",
                                color=text_color, fontsize=7, zorder=3)
                    bottom += percent
                remainder = max(0.0, 100.0 - bottom)
                if remainder > 0:
                    ax.bar(x, remainder, bottom=bottom, width=0.6, facecolor=UNMEASURED_COLOR,
                          edgecolor=self.BAR_EDGE_COLOR, linewidth=0.7, zorder=2)

        self.ax_full.set_xticks([])
        self.ax_full.set_ylim(0, 100)
        self.ax_full.set_ylabel("% of bottle")
        self.ax_full.set_title("Full", fontsize=8)

        self.ax_zoom.set_xticks([])
        self.ax_zoom.set_ylim(0, ceiling)
        self.ax_zoom.set_title(f"Zoomed to {ceiling:g}%", fontsize=8)

        if len(run_ids) > self.BARS_PER_VIEW:
            lo = self._chart_scroll_offset - 0.6
            hi = self._chart_scroll_offset + self.BARS_PER_VIEW - 0.4
            self.ax_full.set_xlim(lo, hi)
            self.ax_zoom.set_xlim(lo, hi)

        self._chart_hover_annotation = None
        # Fixed margins, not tight_layout() - tight_layout tries to measure and fit
        # every label's actual rendered extent, and silently fails (leaving the two
        # subplots unevenly sized - one visibly squashed) whenever a bar's percent
        # label is unusually long, e.g. from a bad/extrapolated curve fit producing
        # a huge percentage. Static margins never depend on content, so they can't
        # fail this way. The axes limits above must still be locked in before the
        # bracket/pill positions are computed - both read back the axes' actual
        # final transforms/pixel positions.
        self.figure.subplots_adjust(left=0.16, right=0.94, top=0.90, bottom=0.10, wspace=0.6)
        self.canvas.draw()
        self._draw_zoom_bracket(ceiling)
        self._position_pill(ceiling)
        self.canvas.draw()
        if self._pill_artist is not None:
            self._pill_bbox = self._pill_artist.get_window_extent(self.canvas.get_renderer())

    def _draw_zoom_bracket(self, ceiling):
        """The zoom-region indicator: a solid line across the full chart's whole
        width at the cutoff height, continuing across the zoomed panel's own top
        edge too (and a matching flat pair at the baseline, y=0, since the zoomed
        view always starts at 0%) - the same "stratigraphic column zoom callout"
        shape confirmed in the design mockup, drawn as real matplotlib artists
        (ConnectionPatch) spanning between the two axes."""
        for y in (0, ceiling):
            self.ax_full.axhline(y, color=self.BRACKET_COLOR, linewidth=1.25, zorder=4)
            self.ax_zoom.axhline(y, color=self.BRACKET_COLOR, linewidth=1.25, zorder=4)
            connector = ConnectionPatch(
                xyA=(self.ax_full.get_xlim()[1], y), coordsA=self.ax_full.transData,
                xyB=(self.ax_zoom.get_xlim()[0], y), coordsB=self.ax_zoom.transData,
                color=self.BRACKET_COLOR, linewidth=1.25, zorder=4,
            )
            self.figure.add_artist(connector)
            self._bracket_connectors.append(connector)

    def _position_pill(self, ceiling):
        """The draggable readout - anchored at the exact point where the bracket
        line leaves the Full chart's own right edge (not floating at the midpoint
        of the connector), so it reads as a marker on that column itself - dragging
        it feels like directly moving the Full chart's own cutoff line, not some
        detached control in the gap."""
        p1 = self.ax_full.transData.transform((self.ax_full.get_xlim()[1], ceiling))
        fig_x, fig_y = self.figure.transFigure.inverted().transform(p1)
        self._pill_artist = self.figure.text(
            fig_x, fig_y, f"{ceiling:.1f}", ha="center", va="center", fontsize=9,
            color="#ffffff", zorder=10, family="monospace",
            bbox=dict(boxstyle="round,pad=0.35", facecolor=self.BRACKET_COLOR, edgecolor="none"),
        )

    # -- zoom-pill drag + hover ------------------------------------------------
    def _on_chart_press(self, event):
        if self._pill_bbox is None or event.x is None or event.y is None:
            return
        pad = self.PILL_HIT_PAD
        if (self._pill_bbox.x0 - pad <= event.x <= self._pill_bbox.x1 + pad
                and self._pill_bbox.y0 - pad <= event.y <= self._pill_bbox.y1 + pad):
            self._dragging_pill = True

    def _on_chart_release(self, _event):
        self._dragging_pill = False

    def _on_chart_motion(self, event):
        if self._dragging_pill:
            self._drag_pill(event)
            return
        self._on_chart_hover(event)

    def _drag_pill(self, event):
        if event.y is None:
            return
        _, data_y = self.ax_full.transData.inverted().transform((event.x, event.y))
        data_y = max(0.5, min(100.0, data_y))
        data_y = round(data_y * 2) / 2  # resolution 0.5, matching the old Scale widget
        if data_y != self._zoom_ceiling:
            self._zoom_ceiling = data_y
            self._render_chart()

    def _on_chart_hover(self, event):
        if event.inaxes not in (self.ax_full, self.ax_zoom):
            if self._chart_hover_annotation is not None:
                self._chart_hover_annotation.set_visible(False)
                self.canvas.draw_idle()
            return
        for ax, rect, gas, percent, reason in self._chart_segments:
            if ax is not event.inaxes:
                continue
            contains, _ = rect.contains(event)
            if contains:
                if self._chart_hover_annotation is None or self._chart_hover_annotation.axes is not ax:
                    if self._chart_hover_annotation is not None:
                        self._chart_hover_annotation.remove()
                    self._chart_hover_annotation = ax.annotate(
                        "", xy=(0, 0), xytext=(10, 10), textcoords="offset points",
                        bbox=dict(boxstyle="round", fc="#ffffe0"), zorder=20,
                    )
                text = f"{gas}: {percent:.2f}%"
                if reason:
                    text += f"\n({reason} calibrated range)"
                self._chart_hover_annotation.xy = (event.xdata, event.ydata)
                self._chart_hover_annotation.set_text(text)
                self._chart_hover_annotation.set_visible(True)
                self.canvas.draw_idle()
                return
        if self._chart_hover_annotation is not None and self._chart_hover_annotation.get_visible():
            self._chart_hover_annotation.set_visible(False)
            self.canvas.draw_idle()

    def on_copy_chart_data(self):
        if not self._chart_data:
            return
        table = [["sample", "gas", "percent"]]
        for label, gas_percents in self._chart_data:
            for gas, percent in gas_percents.items():
                table.append([label, gas, f"{percent:.4f}"])
        self.clipboard_clear()
        self.clipboard_append(export.rows_to_tsv(table))

    def on_export_chart(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".png", filetypes=[("PNG image", "*.png")]
        )
        if not path:
            return
        self.figure.savefig(path, dpi=150, bbox_inches="tight")
