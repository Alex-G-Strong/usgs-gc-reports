"""The "Models" tab: for each gas, a peak-area-vs-amount calibration built from
standard-linked runs loaded from the Selector tab. Every candidate point is
guaranteed to carry a real standard (build_calibration_candidates only returns
(run, gas) pairs where the run is standard-linked, has a pressure, and that standard
defines a composition for that gas). Series membership - which points feed which
regression - is assigned explicitly through a points table (per-point, bulk, or via
the same checkbox/click/shift-click/drag scratchpad used elsewhere in the app), never
by dragging on the chart; the chart itself is a read-only visualization with
scroll-to-zoom, matplotlib's own pan/zoom-rectangle/home toolbar, and its own
undo history shared with point-assignment actions."""

import math
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from gc_pipeline import calibration, db, export
from gc_pipeline.pressure_wizard import PressureEntryWindow
from gc_pipeline.widgets import bind_fast_hscroll, justify_columns

DEFAULT_SERIES_NAME = "All points"
ADD_NEW_SERIES_PREFIX = "+ New series: "
CHECK_ON = "☑"
CHECK_OFF = "☐"
EXCLUDED_ON = "☑"
EXCLUDED_OFF = "☐"
SHIFT_MASK = 0x0001
CTRL_MASK = 0x0004
NOTES_SCROLL_STEP = 4  # characters revealed per wheel notch while hovering the Notes cell
ZERO_LINE_COLOR = "#bbbbbb"

SERIES_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                  "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]


def _draw_zero_lines(ax):
    ax.axhline(0, color=ZERO_LINE_COLOR, linewidth=0.8, zorder=0)
    ax.axvline(0, color=ZERO_LINE_COLOR, linewidth=0.8, zorder=0)


class ModelsPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.conn = app.conn

        self._candidates = {}  # gas -> [{"run_id", "sample_name", "standard_name", "notes", "area", "amount"}]
        self._gas_tabs = {}    # gas -> GasCalibrationTab
        self._shared_chart_sash_y = None  # last chart/points-area sash position, applied to every gas tab
        self._run_ids = []  # the run_ids most recently passed to load_runs

        self._build()

    def _build(self):
        header = ttk.Frame(self)
        header.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(header, text="Models", font=("", 10, "bold")).pack(side="left")
        self.status_label = ttk.Label(
            header,
            text='No runs loaded yet - select runs in the Selector tab and click '
                 '"Load selected into Models".',
            foreground="#666666",
        )
        self.status_label.pack(side="left", padx=(10, 0))
        ttk.Button(header, text="Edit selected pressures", command=self.on_edit_pressures).pack(side="right")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self.overview_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.overview_tab, text="Overview")
        self._overview_canvas = None
        self._overview_summary = None
        self._overview_toolbar = None
        self._overview_pan_start = None  # (ax, pixel_x, pixel_y, xlim, ylim) mid-drag
        self._overview_point_artists = []  # [(ax, scatter_artist, xs, ys, names), ...]
        self._overview_hover_marker = None
        self._overview_hover_annotation = None
        self._overview_hover_ax = None
        self._overview_equation_rows = []

        # A plain grey cover + spinner, shown over the notebook while gas tabs are
        # being (re)built - each tab is a real matplotlib Figure/Canvas/Toolbar, so
        # rebuilding several at once used to be visible as the notebook's contents
        # jumping/flickering tab-by-tab. Processing one gas per event-loop tick
        # (_process_gas_queue below) instead of all of them in one blocking call
        # means the spinner actually animates, and the overlay hides the
        # in-progress flicker entirely until everything's ready at once.
        self._loading_overlay = tk.Frame(self, bg="#d9d9d9")
        self._loading_label = tk.Label(
            self._loading_overlay, text="", bg="#d9d9d9", fg="#444444", font=("", 14),
        )
        self._loading_label.place(relx=0.5, rely=0.5, anchor="center")
        self._spinner_frames = ["◐", "◓", "◑", "◒"]
        self._spinner_index = 0
        self._spinner_after_id = None
        self._gas_queue = []

    # -- load flow ---------------------------------------------------------
    def load_runs(self, run_ids):
        run_ids = list(run_ids)
        if not run_ids:
            messagebox.showwarning("Nothing selected", "No runs are selected in the Selector tab.")
            return
        self._run_ids = run_ids

        rows = db.list_runs_for_pressure_entry(self.conn, run_ids=run_ids, missing_only=False)
        incomplete = [r for r in rows if r["standard_id"] is None or r["pressure"] is None]

        if incomplete:
            PressureEntryWindow(
                self.app, run_ids=[r["run_id"] for r in incomplete], missing_only=False,
                on_close_callback=lambda: self._reload(run_ids),
                auto_close_on_save=True,
            )

        self._render_candidates(run_ids, len(incomplete))

    def edit_run(self, run_id):
        """Opens a single run (double-clicked from a gas tab's chart or points
        table) in the pressure entry wizard, then reloads Models afterward - a
        run's amount is derived from its pressure, so a correction there needs to
        ripple back into the chart, not just the database."""
        PressureEntryWindow(
            self.app, run_ids=[run_id], missing_only=False,
            on_close_callback=lambda: self._reload(self._run_ids),
        )

    def on_edit_pressures(self):
        if not self._run_ids:
            messagebox.showwarning("Nothing loaded", "No runs are currently loaded into Models.")
            return
        PressureEntryWindow(
            self.app, run_ids=self._run_ids, missing_only=False,
            on_close_callback=lambda: self._reload(self._run_ids),
        )

    def _reload(self, run_ids):
        # Re-check the same original run_ids set after the pressure wizard closes -
        # whatever's now complete gets picked up without a second click.
        rows = db.list_runs_for_pressure_entry(self.conn, run_ids=run_ids, missing_only=False)
        incomplete_count = sum(1 for r in rows if r["standard_id"] is None or r["pressure"] is None)
        self._render_candidates(run_ids, incomplete_count)

    def _render_candidates(self, run_ids, incomplete_count):
        # build_calibration_candidates only ever returns (run, gas) points for runs
        # that are standard-linked, have a pressure, and whose standard defines that
        # gas - so nothing without a real standard can end up here.
        self._candidates = calibration.build_calibration_candidates(self.conn, run_ids)

        total = len(run_ids)
        ready = total - incomplete_count
        if incomplete_count:
            self.status_label.config(
                text=f"{ready} of {total} selected runs loaded "
                     f"({incomplete_count} missing a standard or pressure - see the entry window)."
            )
        else:
            self.status_label.config(text=f"{ready} of {total} selected runs loaded.")

        self._rebuild_gas_tabs()

    def _show_loading_overlay(self, text):
        self._loading_label.config(text=text)
        self._loading_overlay.place(in_=self.notebook, relx=0, rely=0, relwidth=1, relheight=1)
        self._loading_overlay.lift()
        self._spinner_index = 0
        self._tick_spinner()

    def _tick_spinner(self):
        frame = self._spinner_frames[self._spinner_index % len(self._spinner_frames)]
        self._spinner_index += 1
        self._loading_label.config(text=f"{frame}  Loading models...")
        self._spinner_after_id = self.after(120, self._tick_spinner)

    def _hide_loading_overlay(self):
        if self._spinner_after_id is not None:
            self.after_cancel(self._spinner_after_id)
            self._spinner_after_id = None
        self._loading_overlay.place_forget()

    def _rebuild_gas_tabs(self):
        # Building a brand-new gas's tab means a fresh matplotlib Figure +
        # FigureCanvasTkAgg + NavigationToolbar2Tk, and a first load can mean
        # several of those at once - real work that used to happen in one blocking
        # call, visible as the notebook's tabs jumping/flickering into place one at
        # a time. Removing stale tabs is cheap and stays synchronous; building/
        # updating the current ones is staged one gas per event-loop tick instead,
        # under the grey loading overlay, so nothing jumps and the spinner actually
        # animates rather than just freezing on frame one.
        gases = sorted(self._candidates.keys())

        for gas in list(self._gas_tabs.keys()):
            if gas not in gases:
                tab = self._gas_tabs.pop(gas)
                self.notebook.forget(tab.frame)

        self._gas_queue = list(gases)
        self._show_loading_overlay("Loading models...")
        self._process_gas_queue()

    def _process_gas_queue(self):
        if not self._gas_queue:
            self._render_overview()
            self._hide_loading_overlay()
            return
        gas = self._gas_queue.pop(0)
        if gas not in self._gas_tabs:
            tab = GasCalibrationTab(
                self.notebook, self.app, gas,
                on_remove_runs=self.remove_runs,
                on_data_changed=self._render_overview,
                on_sash_changed=self._on_gas_tab_sash_changed,
                on_edit_run=self.edit_run,
            )
            self.notebook.add(tab.frame, text=gas)
            self._gas_tabs[gas] = tab
            if self._shared_chart_sash_y is not None:
                tab.frame.after(0, lambda t=tab: t.set_chart_sash(self._shared_chart_sash_y))
        self._gas_tabs[gas].set_points(self._candidates[gas])
        self.after(1, self._process_gas_queue)

    def _on_gas_tab_sash_changed(self, y):
        self._shared_chart_sash_y = y
        for tab in self._gas_tabs.values():
            tab.set_chart_sash(y)

    def remove_runs(self, run_ids):
        """Drops these runs from every gas's candidates and series here in Models,
        then deselects them entirely in the Selector tab - the "remove from the whole
        selection" action triggered by Backspace in a gas tab's points table."""
        run_ids = set(run_ids)
        if not run_ids:
            return
        affected_gases = []
        for gas in list(self._candidates.keys()):
            pts = self._candidates[gas]
            new_pts = [p for p in pts if p["run_id"] not in run_ids]
            if len(new_pts) != len(pts):
                affected_gases.append(gas)
            if new_pts:
                self._candidates[gas] = new_pts
            else:
                del self._candidates[gas]
        for gas in affected_gases:
            for s in db.list_calibration_series(self.conn, gas=gas):
                pts = [p["run_id"] for p in db.get_calibration_series_points(self.conn, s["series_id"])]
                remaining = [rid for rid in pts if rid not in run_ids]
                if len(remaining) != len(pts):
                    db.set_calibration_series_points(self.conn, s["series_id"], remaining, gas)
        for run_id in run_ids:
            self.app.remove_from_selection(run_id)
        self._rebuild_gas_tabs()

    def _render_overview(self):
        # Called after any change to a gas's points/series (create/rename/delete a
        # series, refit, exclude/move a point...) - piggyback on it to keep the
        # Analysis tab's curve selections and computed values in sync too, without
        # Analysis having to poll or duplicate this bookkeeping.
        analysis_panel = getattr(self.app, "analysis_panel", None)
        if analysis_panel is not None:
            analysis_panel.on_curves_changed()

        for child in self.overview_tab.winfo_children():
            child.destroy()
        gases = sorted(self._candidates.keys())
        if not gases:
            ttk.Label(self.overview_tab, text="No calibration points yet.", foreground="#666666").pack(
                padx=10, pady=10
            )
            self._overview_canvas = None
            self._overview_summary = None
            self._overview_toolbar = None
            self._overview_pan_start = None
            self._overview_point_artists = []
            self._overview_hover_marker = None
            self._overview_hover_annotation = None
            self._overview_hover_ax = None
            self._overview_equation_rows = []
            return

        cols = math.ceil(math.sqrt(len(gases)))
        rows = math.ceil(len(gases) / cols)
        # Wide enough for a genuinely readable per-series legend (equation + R²).
        fig = Figure(figsize=(max(cols * 4.6, 6.0), max(rows * 3.6, 4.4)), dpi=95)
        summary_lines = []
        equation_rows = []
        # Every child (including the old figure's artists) was just destroyed above
        # - any hover/point-artist state pointing at them is now stale.
        self._overview_point_artists = []
        self._overview_hover_marker = None
        self._overview_hover_annotation = None
        self._overview_hover_ax = None
        for i, gas in enumerate(gases):
            ax = fig.add_subplot(rows, cols, i + 1)
            _draw_zero_lines(ax)
            base_by_run = {p["run_id"]: p for p in self._candidates[gas]}
            series_list = db.list_calibration_series(self.conn, gas=gas)
            fits = []
            all_xs = []
            for j, s in enumerate(series_list):
                color = SERIES_COLORS[j % len(SERIES_COLORS)]
                point_run_ids = [row["run_id"] for row in db.get_calibration_series_points(self.conn, s["series_id"])]
                xs = [base_by_run[rid]["area"] for rid in point_run_ids if rid in base_by_run]
                ys = [base_by_run[rid]["amount"] for rid in point_run_ids if rid in base_by_run]
                names = [base_by_run[rid]["sample_name"] or str(rid)
                         for rid in point_run_ids if rid in base_by_run]
                if not xs:
                    continue
                all_xs.extend(xs)
                point_artist = ax.scatter(xs, ys, s=16, color=color)
                self._overview_point_artists.append((ax, point_artist, xs, ys, names))
                if s["slope"] is not None:
                    eqn = f'y={s["slope"]:.3g}x' if s["force_through_origin"] else \
                        f'y={s["slope"]:.3g}x+{s["intercept"]:.3g}'
                    label = f'{s["name"]}: {eqn}, R²={s["r_squared"]:.3f}'
                    fits.append((color, s["slope"], s["intercept"], label))
                    summary_lines.append(f"{gas} - {s['name']}: {eqn}, R²={s['r_squared']:.3f}")
                    equation_rows.append([
                        gas, s["name"], f'{s["slope"]:.6g}', f'{s["intercept"]:.6g}',
                        f'{s["r_squared"]:.6g}', eqn, "yes" if s["force_through_origin"] else "no",
                    ])
            # No explicit relim()/autoscale_view() here - scatter() already
            # autoscaled the view correctly as each series was added. Calling
            # relim() would recompute the data limits from every artist's raw
            # coordinates from scratch, including axhline/axvline's "infinite"
            # blended-transform lines, which corrupts the range down to a sliver
            # around zero regardless of where the actual data is.
            xlim = ax.get_xlim()
            ylim = ax.get_ylim()
            for color, slope, intercept, label in fits:
                ax.plot(
                    [xlim[0], xlim[1]], [slope * xlim[0] + intercept, slope * xlim[1] + intercept],
                    linewidth=1.3, color=color, label=label,
                )
                intercept_artist = ax.scatter([0], [intercept], marker="D", s=30, color=color,
                                               edgecolors="#333333", linewidths=0.6, zorder=5)
                self._overview_point_artists.append((ax, intercept_artist, [0], [intercept], ["y-intercept"]))
            ax.set_xlim(xlim)
            ax.set_ylim(ylim)
            ax.set_title(gas, fontsize=11)
            ax.tick_params(labelsize=8)
            if fits:
                ax.legend(fontsize=9, loc="best", frameon=True, handlelength=1.4)
        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=self.overview_tab)
        canvas.draw()

        # The toolbar goes above the canvas (packed first, side="top") - the same
        # Home/Pan(hand)/Zoom(magnifying glass) tools as each gas tab's own chart.
        toolbar_frame = ttk.Frame(self.overview_tab)
        toolbar_frame.pack(fill="x")
        toolbar = NavigationToolbar2Tk(canvas, toolbar_frame)
        toolbar.update()
        # update() only *clears* the navigation stack - it doesn't record a "home"
        # entry by itself, so without this the Home button has nothing to return to
        # and silently does nothing. The real data is already plotted by this point
        # (unlike the gas tabs' toolbar, built before their first render), so this
        # correctly captures the real autoscaled view as home.
        toolbar.push_current()
        toolbar.pack(side="top", fill="x")

        canvas.get_tk_widget().pack(fill="both", expand=True)
        # Double left-click (regardless of which toolbar tool is active) jumps to
        # that gas's own tab. Scroll-wheel zoom and always-on click-drag pan work
        # per-subplot, same as each gas tab's own chart.
        canvas.mpl_connect("button_press_event", lambda event: self._on_overview_click(event, gases))
        canvas.mpl_connect("scroll_event", self._on_overview_scroll)
        canvas.mpl_connect("button_press_event", self._on_overview_pan_press)
        canvas.mpl_connect("motion_notify_event", self._on_overview_pan_motion)
        canvas.mpl_connect("button_release_event", self._on_overview_pan_release)
        canvas.mpl_connect("motion_notify_event", self._on_overview_hover)
        self._overview_canvas = canvas
        self._overview_toolbar = toolbar

        # A plain, real Text widget with the same equations as the legends - the
        # legend text drawn inside the matplotlib canvas is just pixels and can't be
        # selected/copied, so this is what actually makes it copyable.
        self._overview_equation_rows = equation_rows

        summary_frame = ttk.Frame(self.overview_tab)
        summary_frame.pack(fill="x", padx=4, pady=(0, 4))
        summary_header = ttk.Frame(summary_frame)
        summary_header.pack(fill="x")
        ttk.Label(summary_header, text="Equations (selectable/copyable):", foreground="#666666").pack(
            side="left"
        )
        ttk.Button(summary_header, text="Copy table", command=self.on_copy_overview_equations).pack(
            side="right", padx=(4, 0)
        )
        ttk.Button(
            summary_header, text="Export as CSV...", command=self.on_export_overview_equations
        ).pack(side="right")
        text = tk.Text(summary_frame, height=min(max(len(summary_lines), 2), 8), wrap="none")
        text.pack(fill="x")
        text.insert("1.0", "\n".join(summary_lines) if summary_lines else "(no fitted series yet)")
        text.configure(state="disabled")
        self._overview_summary = text

    def _overview_equation_table_rows(self):
        header = ["gas", "series", "slope", "intercept", "r_squared", "equation", "force_through_origin"]
        return [header] + self._overview_equation_rows

    def on_copy_overview_equations(self):
        if not self._overview_equation_rows:
            return
        self.clipboard_clear()
        self.clipboard_append(export.rows_to_tsv(self._overview_equation_table_rows()))

    def on_export_overview_equations(self):
        if not self._overview_equation_rows:
            messagebox.showwarning("Nothing to export", "No fitted series yet.")
            return
        out_path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV file", "*.csv")])
        if not out_path:
            return
        export.write_csv(self._overview_equation_table_rows(), out_path)
        messagebox.showinfo("Export complete", f"Wrote {out_path}")

    def _on_overview_click(self, event, gases):
        # Double left-click jumps to that gas's tab, regardless of which toolbar
        # tool (if any) is currently active - a single click is left alone so
        # panning/box-zoom still work normally. Double left-click (not right) since
        # that's the standard "open/activate" gesture (double-click a file, a tab, a
        # list item) - it doesn't meaningfully conflict with click-drag panning,
        # since a double-click involves no real movement between the two presses.
        if not event.dblclick or event.button != 1:
            return
        if event.inaxes is None:
            return
        axes = event.inaxes.figure.axes
        if event.inaxes not in axes:
            return
        idx = axes.index(event.inaxes)
        if 0 <= idx < len(gases):
            gas = gases[idx]
            if gas in self._gas_tabs:
                self.notebook.select(self._gas_tabs[gas].frame)

    # -- scroll-to-zoom + always-on click-drag pan, per-subplot -----------------
    def _on_overview_scroll(self, event):
        ax = event.inaxes
        if ax is None or event.xdata is None or event.ydata is None:
            return
        base_scale = 1.2
        if event.button == "up":
            scale = 1 / base_scale
        elif event.button == "down":
            scale = base_scale
        else:
            return
        xlim, ylim = ax.get_xlim(), ax.get_ylim()
        x, y = event.xdata, event.ydata
        ax.set_xlim([x - (x - xlim[0]) * scale, x + (xlim[1] - x) * scale])
        ax.set_ylim([y - (y - ylim[0]) * scale, y + (ylim[1] - y) * scale])
        self._overview_canvas.draw_idle()
        if self._overview_toolbar is not None:
            self._overview_toolbar.push_current()

    def _on_overview_pan_press(self, event):
        if self._overview_toolbar is not None and self._overview_toolbar.mode != "":
            return  # a toolbar tool (Pan/Zoom-to-rectangle) is armed - let it own the drag
        if event.inaxes is None or event.button != 1:
            return
        self._overview_pan_start = (
            event.inaxes, event.x, event.y, event.inaxes.get_xlim(), event.inaxes.get_ylim(),
        )

    def _on_overview_pan_motion(self, event):
        if self._overview_pan_start is None or event.x is None or event.y is None:
            return
        ax, x0, y0, xlim0, ylim0 = self._overview_pan_start
        inv = ax.transData.inverted()
        x0_data, y0_data = inv.transform((x0, y0))
        x1_data, y1_data = inv.transform((event.x, event.y))
        dx, dy = x0_data - x1_data, y0_data - y1_data
        ax.set_xlim(xlim0[0] + dx, xlim0[1] + dx)
        ax.set_ylim(ylim0[0] + dy, ylim0[1] + dy)
        self._overview_canvas.draw_idle()

    def _on_overview_pan_release(self, _event):
        if self._overview_pan_start is not None:
            self._overview_pan_start = None
            if self._overview_toolbar is not None:
                self._overview_toolbar.push_current()

    # -- hover: coordinates + indicator circle, per-subplot ---------------------
    def _find_overview_point_near(self, event):
        """The closest matching point in this subplot, by actual pixel distance -
        same reasoning as GasCalibrationTab._find_point_near (a y-intercept marker
        at x=0 can sit close enough to a real point that both register as a hit)."""
        if event.inaxes is None:
            return None
        best, best_dist = None, None
        for ax, artist, xs, ys, names in self._overview_point_artists:
            if ax is not event.inaxes:
                continue
            contains, info = artist.contains(event)
            if not contains or not info["ind"].size:
                continue
            for idx in info["ind"]:
                x, y = xs[idx], ys[idx]
                px, py = ax.transData.transform((x, y))
                dist = (px - event.x) ** 2 + (py - event.y) ** 2
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best = (ax, x, y, names[idx])
        return best

    def _on_overview_hover(self, event):
        if self._overview_pan_start is not None:
            self._clear_overview_hover()
            return
        hit = self._find_overview_point_near(event)
        if hit is None:
            self._clear_overview_hover()
            return
        ax, x, y, name = hit
        if self._overview_hover_ax is not ax:
            # Switching subplots - the old marker/annotation belong to a different
            # axes and can't be reused there, so drop them and let fresh ones get
            # created below on the new axes.
            self._clear_overview_hover()
            self._overview_hover_marker = None
            self._overview_hover_annotation = None
        if self._overview_hover_marker is None:
            self._overview_hover_marker, = ax.plot(
                [], [], marker="o", markersize=10, markerfacecolor="none",
                markeredgecolor="#333333", markeredgewidth=1.3, zorder=10,
            )
        if self._overview_hover_annotation is None:
            self._overview_hover_annotation = ax.annotate(
                "", xy=(0, 0), xytext=(8, 8), textcoords="offset points",
                bbox=dict(boxstyle="round", fc="#ffffe0", ec="#888888"), fontsize=7, zorder=11,
            )
        self._overview_hover_ax = ax
        self._overview_hover_marker.set_data([x], [y])
        self._overview_hover_marker.set_visible(True)
        self._overview_hover_annotation.xy = (x, y)
        self._overview_hover_annotation.set_text(f"{name}\n({x:.4g}, {y:.4g})")
        self._overview_hover_annotation.set_visible(True)
        self._overview_canvas.draw_idle()

    def _clear_overview_hover(self):
        changed = False
        if self._overview_hover_marker is not None and self._overview_hover_marker.get_visible():
            self._overview_hover_marker.set_visible(False)
            changed = True
        if self._overview_hover_annotation is not None and self._overview_hover_annotation.get_visible():
            self._overview_hover_annotation.set_visible(False)
            changed = True
        if changed and self._overview_canvas is not None:
            self._overview_canvas.draw_idle()


class GasCalibrationTab:
    """One gas's calibration workspace: a read-only, scroll-to-zoom area-vs-amount
    chart with the standard matplotlib pan/zoom-rectangle/home toolbar (top) plus a
    points table (bottom-left, one row per candidate point - every one guaranteed to
    carry a real standard) and a series summary (bottom-right). Series membership is
    changed explicitly - clicking a point's series cell, or checkbox/click/
    shift-click/drag-selecting rows and using "Move selected to" - never by
    interacting with the chart, so which points feed which regression is always a
    deliberate, visible choice rather than a mouse gesture on a scatterplot. Ctrl+Z
    undoes the most recent action of any kind here (a selection change, a point's
    series/exclude change, or a zoom step) in the order they happened."""

    def __init__(self, notebook, app, gas, on_remove_runs=None, on_data_changed=None, on_sash_changed=None,
                 on_edit_run=None):
        self.app = app
        self.conn = app.conn
        self.gas = gas
        self._on_remove_runs = on_remove_runs
        self._on_data_changed = on_data_changed
        self._on_sash_changed = on_sash_changed
        self._on_edit_run = on_edit_run
        self.frame = ttk.Frame(notebook)

        self._points = {}        # run_id -> {"sample_name", "standard_name", "notes", "area", "amount"}
        self._point_series = {}  # run_id -> [series_id, ...], refreshed on every render
        self._notes_scroll = {}  # run_id -> character offset into its note, persists across renders
        self._rendered_once = False  # whether to preserve the current view or let it autoscale
        self._pan_start = None  # (pixel_x, pixel_y, xlim, ylim) while a pan-drag is in progress
        self._point_artists = []   # [(scatter_artist, xs, ys, run_ids), ...], rebuilt every render
        self._hover_annotation = None  # the coordinate-text popup shown near a hovered point
        self._hover_marker = None      # the small ring drawn around a hovered point (untouched by the below)
        self._hover_row_run_id = None  # points_tree row currently filled pale blue for the hovered point

        # Same checkbox + click/shift-click/drag selection scheme as the main
        # browser's tree and the Deleted Files window, ported to this flat list -
        # drives "Move selected to" and Backspace-to-remove.
        self._table_selection = set()
        self._anchor_run_id = None
        self._drag_anchor_run_id = None
        self._drag_target_state = None
        self._drag_snapshot = {}

        # One combined undo history for this tab - selection changes, point
        # series/exclude edits, and zoom steps, popped in the order they happened
        # regardless of type.
        self._undo_stack = []

        self._active_editor = None
        self._active_editor_close = None
        self._suppress_next_global_close = False

        self._build()

    def _build(self):
        # Plain tk.PanedWindow (not ttk) for a real, generously-sized sash - lets the
        # user drag the boundary between the chart and the points/series area to
        # resize the chart, same pattern used for the Selector tab's main sashes.
        # Its position is shared across every gas tab (see set_chart_sash).
        self.outer = tk.PanedWindow(
            self.frame, orient="vertical", sashwidth=8, sashrelief="raised", sashpad=1,
            showhandle=False, bd=0,
        )
        self.outer.pack(fill="both", expand=True)
        self.outer.bind("<ButtonRelease-1>", self._on_outer_sash_release)

        plot_frame = ttk.Frame(self.outer)
        self.outer.add(plot_frame, minsize=120, stretch="always")

        self.fig = Figure(figsize=(5, 3), dpi=95)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("peak area")
        self.ax.set_ylabel("amount")
        _draw_zero_lines(self.ax)

        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        # Toolbar packed *before* the canvas (top) - same position as the Overview
        # grid's toolbar, above the chart, not below it.
        toolbar_frame = ttk.Frame(plot_frame)
        toolbar_frame.pack(fill="x", side="top")
        # The standard matplotlib toolbar already provides Home / Back / Forward,
        # a hand-icon Pan tool, and a magnifying-glass Zoom-to-rectangle tool - kept
        # (not rebuilt) and just wired up to also track our own scroll-wheel zoom
        # steps, so Home/Back/Forward behave consistently regardless of which zoom
        # method was used.
        self.toolbar = NavigationToolbar2Tk(self.canvas, toolbar_frame)
        self.toolbar.update()
        # NavigationToolbar2Tk is itself a tk.Frame subclass - it doesn't place
        # itself in its parent automatically, so without this pack() call it's
        # constructed but never actually shown (this was missing entirely before).
        self.toolbar.pack(side="top", fill="x")
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.mpl_connect("scroll_event", self._on_scroll_zoom)
        # Click-drag panning works all the time (not just when the toolbar's own
        # Pan tool is selected) - gated on toolbar.mode == "" so it steps aside for
        # the toolbar's own drag behavior whenever Pan or Zoom-to-rectangle is
        # actually armed, rather than fighting it.
        self.canvas.mpl_connect("button_press_event", self._on_pan_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_pan_motion)
        self.canvas.mpl_connect("button_release_event", self._on_pan_release)
        # Hovering a point shows its (area, amount) coordinates and rings it with a
        # small indicator circle - a second, independent motion_notify_event
        # listener (matplotlib supports multiple), so it doesn't interfere with
        # panning above; it just stays quiet while a drag is in progress.
        self.canvas.mpl_connect("motion_notify_event", self._on_hover)
        # Double left-click a plotted point opens that sample in the pressure entry
        # wizard too - the same "double-click to edit" gesture as the points table
        # below, just reachable directly from the chart. A single click is left
        # alone so panning/box-zoom keep working normally.
        self.canvas.mpl_connect("button_press_event", self._on_chart_double_click)
        self.canvas.get_tk_widget().bind("<Control-z>", self.on_undo)

        bottom = ttk.PanedWindow(self.outer, orient="horizontal")
        self.outer.add(bottom, minsize=150, stretch="always")

        points_frame = ttk.Frame(bottom)
        bottom.add(points_frame, weight=3)

        points_header = ttk.Frame(points_frame)
        points_header.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(points_header, text="Points", font=("", 9, "bold")).pack(side="left")
        self.selection_label = ttk.Label(points_header, text="0 selected", foreground="#666666")
        self.selection_label.pack(side="left", padx=(10, 0))
        ttk.Button(points_header, text="Justify columns", command=self.on_justify_columns).pack(
            side="left", padx=(10, 0)
        )

        self.points_tree = ttk.Treeview(
            points_frame, columns=("sel", "standard", "area", "amount", "series", "excluded", "notes"),
            show="tree headings", selectmode="browse", height=8,
        )
        self.points_tree.heading("#0", text="Sample")
        self.points_tree.column("#0", width=140, anchor="w", stretch=False)
        for col, label, width, anchor in (
            ("sel", "Sel.", 36, "center"), ("standard", "Standard", 80, "w"), ("area", "Peak area", 75, "e"),
            ("amount", "Amount", 75, "e"), ("series", "Series", 120, "w"), ("excluded", "Excl.", 40, "center"),
            ("notes", "Notes", 140, "w"),
        ):
            self.points_tree.heading(col, text=label)
            self.points_tree.column(col, width=width, anchor=anchor, stretch=False)
        self.points_tree.tag_configure("selected", background="#bdddff")
        # Filled (not just bordered) pale blue, distinct from "selected"'s tone -
        # appended last wherever both apply, so hovering a point on the chart reads
        # clearly regardless of whether that row also happens to be checkbox-
        # selected. Deliberately a different shade than the Duplicates wizard's own
        # pale cell-pick blue so the two never look like the same kind of highlight.
        self.points_tree.tag_configure("chart-hover", background="#cfe3ff")
        self.points_tree.pack(fill="both", expand=True, padx=4, pady=(0, 2))
        bind_fast_hscroll(self.points_tree)
        self.points_tree.bind("<ButtonPress-1>", self._on_points_press)
        self.points_tree.bind("<B1-Motion>", self._on_points_drag)
        # Double-click a sample's row - anywhere except the Series/Excl. cells,
        # which already have their own dedicated click behavior - opens it in the
        # pressure entry wizard, same as the Selector tab's own double-click/
        # "Edit selected run(s)" gesture. Reloading Models afterward matters here
        # specifically because a run's amount is derived from its pressure, so
        # fixing a bad pressure needs to actually ripple back into the chart.
        self.points_tree.bind("<Double-Button-1>", self._on_points_double_click)
        # Hovering the Notes column and scrolling reveals more of that cell's text
        # instead of scrolling the table - only intercepted (via "break") when the
        # cursor is actually over that column, so normal row scrolling elsewhere is
        # unaffected.
        self.points_tree.bind("<MouseWheel>", self._on_notes_wheel, add="+")
        self.points_tree.bind("<Control-z>", self.on_undo)
        self.points_tree.bind("<BackSpace>", self._on_backspace_remove)

        move_bar = ttk.Frame(points_frame)
        move_bar.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Label(move_bar, text="Move selected to:").pack(side="left")
        self.move_target_var = tk.StringVar()
        self.move_combo = ttk.Combobox(move_bar, textvariable=self.move_target_var, width=16)
        self.move_combo.pack(side="left", padx=(4, 4))
        ttk.Button(move_bar, text="Move", command=self._on_move_selected).pack(side="left")
        ttk.Button(move_bar, text="Add", command=self._on_add_selected).pack(side="left", padx=(4, 0))

        side = ttk.Frame(bottom)
        bottom.add(side, weight=2)
        ttk.Label(side, text="Series", font=("", 9, "bold")).pack(anchor="w", padx=4, pady=(4, 2))

        self.series_tree = ttk.Treeview(
            side, columns=("points", "equation", "r2", "zero"), show="tree headings", height=8,
            selectmode="browse",
        )
        self.series_tree.heading("#0", text="Name")
        self.series_tree.heading("points", text="N")
        self.series_tree.heading("equation", text="Equation")
        self.series_tree.heading("r2", text="R²")
        self.series_tree.heading("zero", text="Zero")
        self.series_tree.column("#0", width=100, anchor="w")
        self.series_tree.column("points", width=30, anchor="center")
        self.series_tree.column("equation", width=140, anchor="w")
        self.series_tree.column("r2", width=50, anchor="center")
        self.series_tree.column("zero", width=40, anchor="center")
        self.series_tree.pack(fill="both", expand=True, padx=4, pady=(4, 4))
        bind_fast_hscroll(self.series_tree)
        # add="+" so the native row-selection (needed for Rename/Delete) still fires
        # alongside this - only the "zero" column's own cell click is intercepted.
        self.series_tree.bind("<Button-1>", self._on_series_click, add="+")
        self.series_tree.bind("<Control-z>", self.on_undo)

        btn_row = ttk.Frame(side)
        btn_row.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(btn_row, text="New", command=self._on_new_series).pack(side="left")
        ttk.Button(btn_row, text="Rename", command=self._on_rename_series).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Delete", command=self._on_delete_series).pack(side="left")

        # Starts at the same width as before (220) so it never demands its full
        # unwrapped single-line width during initial layout - leaving wraplength
        # unset until the first <Configure> made the label (and everything
        # containing it, up to the window itself) balloon out to fit one long line
        # before the correction had a chance to run. From here it's just narrowed/
        # widened via <Configure> to track the Series panel's own actual width,
        # not the window's.
        hint_label = ttk.Label(
            side,
            text='Click anywhere on a point\'s row (or shift/ctrl+shift-click, or drag) to select points, '
                 'then use "Move" to reassign their series (leaving any other series) or "Add" to also '
                 'include them in another series without removing them from their current one(s) - a point '
                 'can belong to more than one series at a time. Click a point\'s own "Series" cell '
                 'to move just that one. Click "Excl." to leave a point out of its series\' fit. Hover '
                 '"Notes" and scroll to read a long note. Select points and press Backspace to remove '
                 'them from the selection entirely. "Zero" forces a series\' fit through the origin. '
                 "Ctrl+Z undoes the most recent action here, including zoom steps.",
            foreground="#666666", wraplength=220, justify="left",
        )
        hint_label.pack(fill="x", padx=4, pady=(0, 4))
        hint_label.bind("<Configure>", lambda e: hint_label.configure(wraplength=max(e.width - 8, 100)))

        self.frame.bind_all("<Button-1>", self._on_global_click, add="+")

    def on_justify_columns(self):
        justify_columns(self.points_tree, [c for c in self.points_tree["columns"] if c != "sel"])

    # -- shared chart/points-area sash height ---------------------------------
    def _on_outer_sash_release(self, _event=None):
        try:
            _x, y = self.outer.sash_coord(0)
        except tk.TclError:
            return
        if self._on_sash_changed is not None:
            self._on_sash_changed(y)

    def set_chart_sash(self, y):
        try:
            self.outer.sash_place(0, 0, y)
        except tk.TclError:
            pass

    # -- scroll-to-zoom, centered on the cursor -------------------------------
    def _on_scroll_zoom(self, event):
        if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
            return
        base_scale = 1.2
        if event.button == "up":
            scale = 1 / base_scale
        elif event.button == "down":
            scale = base_scale
        else:
            return
        xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        self._push_undo({"type": "zoom", "xlim": xlim, "ylim": ylim})
        x, y = event.xdata, event.ydata
        self.ax.set_xlim([x - (x - xlim[0]) * scale, x + (xlim[1] - x) * scale])
        self.ax.set_ylim([y - (y - ylim[0]) * scale, y + (ylim[1] - y) * scale])
        self.canvas.draw_idle()
        self.toolbar.push_current()

    # -- always-on click-drag panning ------------------------------------------
    def _on_pan_press(self, event):
        if self.toolbar.mode != "" or event.inaxes != self.ax or event.button != 1:
            return  # a toolbar tool (Pan/Zoom-to-rectangle) is armed - let it own the drag
        self._push_undo({"type": "zoom", "xlim": self.ax.get_xlim(), "ylim": self.ax.get_ylim()})
        self._pan_start = (event.x, event.y, self.ax.get_xlim(), self.ax.get_ylim())

    def _on_pan_motion(self, event):
        if self._pan_start is None or event.x is None or event.y is None:
            return
        x0, y0, xlim0, ylim0 = self._pan_start
        inv = self.ax.transData.inverted()
        x0_data, y0_data = inv.transform((x0, y0))
        x1_data, y1_data = inv.transform((event.x, event.y))
        dx, dy = x0_data - x1_data, y0_data - y1_data
        self.ax.set_xlim(xlim0[0] + dx, xlim0[1] + dx)
        self.ax.set_ylim(ylim0[0] + dy, ylim0[1] + dy)
        self.canvas.draw_idle()

    def _on_pan_release(self, _event):
        if self._pan_start is not None:
            self._pan_start = None
            self.toolbar.push_current()

    # -- hover: coordinates + indicator circle ---------------------------------
    def _find_point_near(self, event):
        """The closest matching point across every tracked artist (data points and
        y-intercept markers alike), by actual on-screen pixel distance - not just
        whichever artist happens to be checked first, since an intercept marker at
        x=0 can sit close enough to a real point to make both register as a hit."""
        best, best_dist = None, None
        for artist, xs, ys, run_ids in self._point_artists:
            contains, info = artist.contains(event)
            if not contains or not info["ind"].size:
                continue
            for idx in info["ind"]:
                x, y = xs[idx], ys[idx]
                px, py = self.ax.transData.transform((x, y))
                dist = (px - event.x) ** 2 + (py - event.y) ** 2
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best = (run_ids[idx], x, y)
        return best

    def _on_hover(self, event):
        if self._pan_start is not None or event.inaxes != self.ax:
            self._clear_hover()
            return
        hit = self._find_point_near(event)
        if hit is None:
            self._clear_hover()
            return
        run_id, x, y = hit
        if self._hover_marker is None:
            self._hover_marker, = self.ax.plot(
                [], [], marker="o", markersize=13, markerfacecolor="none",
                markeredgecolor="#333333", markeredgewidth=1.5, zorder=10,
            )
        if self._hover_annotation is None:
            self._hover_annotation = self.ax.annotate(
                "", xy=(0, 0), xytext=(10, 10), textcoords="offset points",
                bbox=dict(boxstyle="round", fc="#ffffe0", ec="#888888"), fontsize=8, zorder=11,
            )
        self._hover_marker.set_data([x], [y])
        self._hover_marker.set_visible(True)
        self._hover_annotation.xy = (x, y)
        sample_name = self._points.get(run_id, {}).get("sample_name") or str(run_id)
        self._hover_annotation.set_text(f"{sample_name}\narea={x:.4g}, amount={y:.4g}")
        self._hover_annotation.set_visible(True)
        self._set_hover_row(run_id)
        self.canvas.draw_idle()

    def _set_hover_row(self, run_id):
        """Fills the hovered point's row in the points table pale blue - the chart's
        own ring marker (self._hover_marker) is left completely alone, this is
        purely an addition."""
        if self._hover_row_run_id == run_id:
            return
        self._clear_hover_row()
        iid = str(run_id)
        if self.points_tree.exists(iid):
            base = [t for t in self.points_tree.item(iid, "tags") if t != "chart-hover"]
            base.append("chart-hover")
            self.points_tree.item(iid, tags=tuple(base))
            self._hover_row_run_id = run_id

    def _clear_hover_row(self):
        if self._hover_row_run_id is None:
            return
        iid = str(self._hover_row_run_id)
        if self.points_tree.exists(iid):
            base = [t for t in self.points_tree.item(iid, "tags") if t != "chart-hover"]
            self.points_tree.item(iid, tags=tuple(base))
        self._hover_row_run_id = None

    def _on_chart_double_click(self, event):
        if not event.dblclick or event.button != 1 or event.inaxes != self.ax:
            return
        hit = self._find_point_near(event)
        if hit is None:
            return
        run_id = hit[0]
        if run_id is None:  # the fitted-intercept marker - not a real sample
            return
        self._open_edit_run(run_id)

    def _clear_hover(self):
        changed = False
        if self._hover_marker is not None and self._hover_marker.get_visible():
            self._hover_marker.set_visible(False)
            changed = True
        if self._hover_annotation is not None and self._hover_annotation.get_visible():
            self._hover_annotation.set_visible(False)
            changed = True
        self._clear_hover_row()
        if changed:
            self.canvas.draw_idle()

    # -- data ---------------------------------------------------------------
    def set_points(self, points):
        self._points = {p["run_id"]: p for p in points}
        self._table_selection &= set(self._points.keys())
        self._assign_unassigned_points()
        self._render()

    def _assign_unassigned_points(self):
        series_list = db.list_calibration_series(self.conn, gas=self.gas)
        if not series_list:
            series_id = db.create_calibration_series(self.conn, self.gas, DEFAULT_SERIES_NAME)
            db.set_calibration_series_points(self.conn, series_id, list(self._points.keys()), self.gas)
            return
        assigned = set()
        for s in series_list:
            for row in db.get_calibration_series_points(self.conn, s["series_id"]):
                assigned.add(row["run_id"])
        unassigned = [rid for rid in self._points if rid not in assigned]
        if not unassigned:
            return
        default_series = next((s for s in series_list if s["name"] == DEFAULT_SERIES_NAME), series_list[0])
        existing_ids = [
            row["run_id"] for row in db.get_calibration_series_points(self.conn, default_series["series_id"])
        ]
        db.set_calibration_series_points(
            self.conn, default_series["series_id"], existing_ids + unassigned, self.gas
        )

    def _move_point_to_series(self, run_id, target_series_id):
        self._set_point_memberships(run_id, [target_series_id])

    def _set_point_memberships(self, run_id, series_ids):
        """Make run_id belong to exactly these series (for this gas) and no others -
        removes it from any series not in series_ids, adds it to any in series_ids
        it isn't already in. series_ids with more than one entry is how a point ends
        up plotted/fit in two series at once."""
        series_ids = set(series_ids)
        for s in db.list_calibration_series(self.conn, gas=self.gas):
            pts = [p["run_id"] for p in db.get_calibration_series_points(self.conn, s["series_id"])]
            in_set = s["series_id"] in series_ids
            has_it = run_id in pts
            if in_set and not has_it:
                pts.append(run_id)
                db.set_calibration_series_points(self.conn, s["series_id"], pts, self.gas)
            elif not in_set and has_it:
                pts.remove(run_id)
                db.set_calibration_series_points(self.conn, s["series_id"], pts, self.gas)

    def _add_point_to_series(self, run_id, series_id):
        db.add_calibration_series_point(self.conn, series_id, run_id, self.gas)

    def _remove_point_from_series(self, run_id, series_id):
        pts = [
            p["run_id"] for p in db.get_calibration_series_points(self.conn, series_id) if p["run_id"] != run_id
        ]
        db.set_calibration_series_points(self.conn, series_id, pts, self.gas)

    def _render(self):
        # Preserve whatever zoom/pan the user has set, instead of resetting to a
        # fresh autoscale every time a point/series edit triggers a re-render.
        prior_xlim = self.ax.get_xlim() if self._rendered_once else None
        prior_ylim = self.ax.get_ylim() if self._rendered_once else None

        self.ax.clear()
        self.ax.set_xlabel("peak area")
        self.ax.set_ylabel("amount")
        self.ax.set_title(self.gas)
        _draw_zero_lines(self.ax)

        self.series_tree.delete(*self.series_tree.get_children())
        self.points_tree.delete(*self.points_tree.get_children())
        self._point_series = {}
        # The old artists just got wiped out by ax.clear() above - any hover state
        # pointing at them is now stale.
        self._point_artists = []
        self._hover_annotation = None
        self._hover_marker = None

        series_list = db.list_calibration_series(self.conn, gas=self.gas)
        series_by_id = {s["series_id"]: s for s in series_list}
        self.move_combo.configure(values=[s["name"] for s in series_list])

        membership = {}  # run_id -> [(series_id, excluded), ...] - a point can belong to more than one series
        for s in series_list:
            for row in db.get_calibration_series_points(self.conn, s["series_id"]):
                membership.setdefault(row["run_id"], []).append((s["series_id"], bool(row["excluded"])))

        fits = []  # (color, slope, intercept) - trendlines drawn once the final view is known
        for i, s in enumerate(series_list):
            series_id = s["series_id"]
            color = SERIES_COLORS[i % len(SERIES_COLORS)]
            included_pts, excluded_xy = [], []
            included_run_ids, excluded_run_ids = [], []
            for run_id, entries in membership.items():
                for sid, excluded in entries:
                    if sid != series_id:
                        continue
                    base = self._points.get(run_id)
                    if base is None:
                        continue
                    if excluded:
                        excluded_xy.append((base["area"], base["amount"]))
                        excluded_run_ids.append(run_id)
                    else:
                        included_pts.append((base["area"], base["amount"]))
                        included_run_ids.append(run_id)

            if included_pts:
                xs, ys = zip(*included_pts)
                artist = self.ax.scatter(xs, ys, s=30, color=color, label=s["name"])
                self._point_artists.append((artist, list(xs), list(ys), included_run_ids))
            if excluded_xy:
                ex, ey = zip(*excluded_xy)
                artist = self.ax.scatter(ex, ey, s=30, facecolors="none", edgecolors=color, linewidths=1.2)
                self._point_artists.append((artist, list(ex), list(ey), excluded_run_ids))

            force_zero = bool(s["force_through_origin"])
            fit = calibration.fit_linear(included_pts, through_origin=force_zero)
            if fit:
                slope, intercept, r_squared = fit
                if (s["slope"], s["intercept"], s["r_squared"]) != fit:
                    db.save_calibration_fit(self.conn, series_id, slope, intercept, r_squared)
                fits.append((color, slope, intercept))
                equation = f"y={slope:.4g}x" if force_zero else f"y={slope:.4g}x+{intercept:.4g}"
                r2_text = f"{r_squared:.3f}"
            else:
                equation, r2_text = "-", "-"

            n_points = sum(1 for entries in membership.values() for sid, _ in entries if sid == series_id)
            self.series_tree.insert(
                "", "end", iid=str(series_id), text=s["name"],
                values=(n_points, equation, r2_text, CHECK_ON if force_zero else CHECK_OFF),
            )

        # Establish the final view (restored, or whatever scatter() already
        # autoscaled to) *before* drawing trendlines, so each line spans the full
        # visible width rather than stopping at its own points. No explicit
        # relim()/autoscale_view() for the fresh-autoscale case - scatter() already
        # set the view correctly as each series was plotted; relim() would recompute
        # data limits from every artist's raw coordinates from scratch, including
        # axhline/axvline's "infinite" blended-transform lines, which corrupts the
        # range down to a sliver around zero regardless of where the data actually is.
        if prior_xlim is not None:
            self.ax.set_xlim(prior_xlim)
            self.ax.set_ylim(prior_ylim)
        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()
        for color, slope, intercept in fits:
            self.ax.plot([xlim[0], xlim[1]], [slope * xlim[0] + intercept, slope * xlim[1] + intercept],
                         color=color, linewidth=1.5)
            # A marker at (0, intercept) - where the trendline crosses the y-axis -
            # distinct from real data points (diamond, dark outline) so it clearly
            # reads as "the fitted intercept" rather than another measurement. Also
            # hoverable, same as a real point (run_id=None - there's no run behind
            # it, just the fitted value).
            intercept_artist = self.ax.scatter([0], [intercept], marker="D", s=45, color=color,
                                                edgecolors="#333333", linewidths=0.8, zorder=5)
            self._point_artists.append((intercept_artist, [0], [intercept], [None]))
        # Adding the trendlines/intercept markers can nudge autoscale again - pin
        # the view back to what was actually decided above.
        self.ax.set_xlim(xlim)
        self.ax.set_ylim(ylim)
        self._rendered_once = True
        if prior_xlim is None:
            # First-ever render for this tab: toolbar.update() (already called once
            # in _build(), before any data existed) reset the toolbar's Home/Back/
            # Forward baseline to whatever the axes looked like at that moment - an
            # empty default (0,1)x(0,1) range, not the real autoscaled data view.
            # update() only *clears* the navigation stack though - it doesn't by
            # itself record a new "home" entry, so without push_current() right
            # after, Home has nothing to return to and silently does nothing. Both
            # calls together are what actually make Home land on the real,
            # automatically-generated matplotlib view.
            self.toolbar.update()
            self.toolbar.push_current()

        if series_list:
            self.ax.legend(fontsize=7, loc="best")
        self.fig.tight_layout()
        self.canvas.draw_idle()

        for run_id in sorted(self._points, key=lambda rid: self._points[rid]["sample_name"] or ""):
            p = self._points[run_id]
            entries = membership.get(run_id, [])
            series_ids = [sid for sid, _excl in entries]
            excluded = any(excl for _sid, excl in entries)
            series_name = ", ".join(
                series_by_id[sid]["name"] for sid in series_ids if sid in series_by_id
            )
            self._point_series[run_id] = series_ids
            selected = run_id in self._table_selection
            note = p.get("notes") or ""
            offset = self._notes_scroll.get(run_id, 0)
            self.points_tree.insert(
                "", "end", iid=str(run_id), text=p["sample_name"],
                values=(
                    CHECK_ON if selected else CHECK_OFF, p.get("standard_name") or "",
                    f'{p["area"]:.4g}', f'{p["amount"]:.4g}', series_name,
                    EXCLUDED_ON if excluded else EXCLUDED_OFF, note[offset:] if offset else note,
                ),
                tags=("selected",) if selected else (),
            )
        self._update_selection_label()

        if self._on_data_changed is not None:
            self._on_data_changed()

    # -- points table: selection scratchpad (checkbox/click/shift/drag) ------
    def _on_points_press(self, event):
        region = self.points_tree.identify_region(event.x, event.y)
        col = self.points_tree.identify_column(event.x)
        row_iid = self.points_tree.identify_row(event.y)

        if region == "cell" and col == "#5" and row_iid:  # series
            self._start_series_edit(row_iid)
            self._drag_anchor_run_id = None
            return
        if region == "cell" and col == "#6" and row_iid:  # excluded
            self._toggle_point_excluded(row_iid)
            self._drag_anchor_run_id = None
            return
        if not row_iid:
            self._drag_anchor_run_id = None
            return
        # Every other column (sel, sample name, standard, area, amount, notes)
        # selects/drags the row - matching the rest of the app, where the whole row
        # is clickable rather than just a narrow checkbox column.

        run_id = int(row_iid)
        shift = bool(event.state & SHIFT_MASK)
        ctrl = bool(event.state & CTRL_MASK)
        self._push_undo({"type": "selection", "before": frozenset(self._table_selection)})
        if shift and ctrl and self._anchor_run_id is not None:
            self._toggle_range(self._anchor_run_id, run_id)
            self._drag_anchor_run_id = None
        elif shift and self._anchor_run_id is not None:
            self._select_range(self._anchor_run_id, run_id)
            self._drag_anchor_run_id = None
        else:
            self._drag_snapshot = {rid: (rid in self._table_selection) for rid in self._flattened_run_ids()}
            was_selected = run_id in self._table_selection
            self._set_row_selected(run_id, not was_selected)
            self._anchor_run_id = run_id
            self._drag_anchor_run_id = run_id
            self._drag_target_state = not was_selected
        self._move_focus(run_id)

    def _on_points_double_click(self, event):
        region = self.points_tree.identify_region(event.x, event.y)
        col = self.points_tree.identify_column(event.x)
        row_iid = self.points_tree.identify_row(event.y)
        if not row_iid or (region == "cell" and col in ("#5", "#6")):
            return  # Series/Excl. cells already have their own click behavior
        self._open_edit_run(int(row_iid))

    def _open_edit_run(self, run_id):
        if self._on_edit_run is not None:
            self._on_edit_run(run_id)

    def _on_points_drag(self, event):
        if self._drag_anchor_run_id is None:
            return
        row_iid = self.points_tree.identify_row(event.y)
        if not row_iid:
            return
        run_id = int(row_iid)
        order = self._flattened_run_ids()
        if self._drag_anchor_run_id not in order or run_id not in order:
            return
        i, j = order.index(self._drag_anchor_run_id), order.index(run_id)
        lo, hi = min(i, j), max(i, j)
        in_range = set(order[lo:hi + 1])
        for rid in order:
            desired = self._drag_target_state if rid in in_range else self._drag_snapshot.get(rid, False)
            if (rid in self._table_selection) != desired:
                self._set_row_selected(rid, desired)
        self._move_focus(run_id)

    def _flattened_run_ids(self):
        return [int(iid) for iid in self.points_tree.get_children("")]

    def _select_range(self, anchor_run_id, target_run_id):
        order = self._flattened_run_ids()
        if anchor_run_id not in order or target_run_id not in order:
            return
        i, j = order.index(anchor_run_id), order.index(target_run_id)
        lo, hi = min(i, j), max(i, j)
        for rid in order[lo:hi + 1]:
            self._set_row_selected(rid, True)

    def _toggle_range(self, anchor_run_id, target_run_id):
        order = self._flattened_run_ids()
        if anchor_run_id not in order or target_run_id not in order:
            return
        desired = target_run_id not in self._table_selection
        i, j = order.index(anchor_run_id), order.index(target_run_id)
        lo, hi = min(i, j), max(i, j)
        for rid in order[lo:hi + 1]:
            self._set_row_selected(rid, desired)

    def _move_focus(self, run_id):
        row_iid = str(run_id)
        self.points_tree.see(row_iid)
        self.points_tree.focus(row_iid)
        self.points_tree.selection_set(row_iid)

    def _set_row_selected(self, run_id, selected):
        if selected:
            self._table_selection.add(run_id)
        else:
            self._table_selection.discard(run_id)
        row_iid = str(run_id)
        if self.points_tree.exists(row_iid):
            self.points_tree.set(row_iid, "sel", CHECK_ON if selected else CHECK_OFF)
            self.points_tree.item(row_iid, tags=("selected",) if selected else ())
        self._update_selection_label()

    def _update_selection_label(self):
        self.selection_label.config(text=f"{len(self._table_selection)} selected")

    # -- unified undo (selection / point actions / zoom) -----------------------
    def _push_undo(self, entry):
        self._undo_stack.append(entry)
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)

    def on_undo(self, _event=None):
        if not self._undo_stack:
            return "break"
        entry = self._undo_stack.pop()
        kind = entry["type"]
        if kind == "selection":
            target = entry["before"]
            current = set(self._table_selection)
            for rid in current - target:
                self._set_row_selected(rid, False)
            for rid in target - current:
                self._set_row_selected(rid, True)
        elif kind == "exclude_multi":
            for series_id, old_excluded in entry["old_states"]:
                db.set_point_state(self.conn, series_id, entry["run_id"], excluded=old_excluded)
            self._render()
        elif kind == "series_move":
            self._set_point_memberships(entry["run_id"], entry["old_series_ids"])
            self._render()
        elif kind == "bulk_series_move":
            for run_id, old_series_ids in entry["moves"]:
                self._set_point_memberships(run_id, old_series_ids)
            self._render()
        elif kind == "bulk_series_add":
            for run_id in entry["run_ids"]:
                self._remove_point_from_series(run_id, entry["series_id"])
            self._render()
        elif kind == "zoom":
            self.ax.set_xlim(entry["xlim"])
            self.ax.set_ylim(entry["ylim"])
            self.canvas.draw_idle()
        return "break"

    # -- notes hover+wheel scroll ----------------------------------------------
    def _on_notes_wheel(self, event):
        col = self.points_tree.identify_column(event.x)
        row_iid = self.points_tree.identify_row(event.y)
        if col != "#7" or not row_iid:
            return  # not over a Notes cell - let the default vertical scroll happen
        run_id = int(row_iid)
        note = self._points.get(run_id, {}).get("notes") or ""
        if not note:
            return "break"
        offset = self._notes_scroll.get(run_id, 0)
        offset += NOTES_SCROLL_STEP if event.delta < 0 else -NOTES_SCROLL_STEP
        offset = max(0, min(offset, max(0, len(note) - 1)))
        self._notes_scroll[run_id] = offset
        self.points_tree.set(row_iid, "notes", note[offset:] if offset else note)
        return "break"

    # -- exclude toggle + per-row series edit ----------------------------------
    def _toggle_point_excluded(self, row_iid):
        run_id = int(row_iid)
        series_ids = self._point_series.get(run_id) or []
        if not series_ids:
            return
        # A point can belong to more than one series now - Excl. is one checkbox per
        # row, so toggling it flips exclusion in every series that point belongs to,
        # uniformly, rather than leaving it ambiguous which membership it affects.
        old_states = []
        for series_id in series_ids:
            row = next(
                (r for r in db.get_calibration_series_points(self.conn, series_id) if r["run_id"] == run_id), None
            )
            if row is not None:
                old_states.append((series_id, bool(row["excluded"])))
        if not old_states:
            return
        new_excluded = not old_states[0][1]
        self._push_undo({
            "type": "exclude_multi", "run_id": run_id, "old_states": old_states,
        })
        for series_id, _old in old_states:
            db.set_point_state(self.conn, series_id, run_id, excluded=new_excluded)
        self._render()

    def _resolve_series_choice(self, raw, names):
        """raw: typed/selected text (possibly the "+ New series: X" sentinel).
        Returns (series_name, error_message). A brand-new name is created on the
        spot; an ambiguous or empty entry is rejected rather than guessed at."""
        raw = raw.strip()
        if raw.startswith(ADD_NEW_SERIES_PREFIX):
            raw = raw[len(ADD_NEW_SERIES_PREFIX):].strip()
        if not raw:
            return None, "Type or pick a series name."
        lowered = raw.lower()
        exact = next((n for n in names if n.lower() == lowered), None)
        if exact:
            return exact, None
        matches = [n for n in names if lowered in n.lower()]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, f'"{raw}" matches multiple series - be more specific.'
        return raw, None  # no match at all - treat as a request to create a new series

    def _series_id_for_name(self, name):
        match = next((s for s in db.list_calibration_series(self.conn, gas=self.gas) if s["name"] == name), None)
        return match["series_id"] if match else db.create_calibration_series(self.conn, self.gas, name)

    def _start_series_edit(self, row_iid):
        self._close_active_editor()
        self.points_tree.see(row_iid)
        self.frame.update_idletasks()
        bbox = self.points_tree.bbox(row_iid, "series")
        if not bbox:
            return
        x, y, w, h = bbox
        run_id = int(row_iid)
        names = [s["name"] for s in db.list_calibration_series(self.conn, gas=self.gas)]
        combo = ttk.Combobox(self.points_tree, values=names, state="normal")
        combo.set(self.points_tree.set(row_iid, "series"))
        combo.place(x=x, y=y, width=w, height=h)
        combo.focus_set()
        combo.selection_range(0, "end")

        def on_keyrelease(event):
            if event.keysym in ("Return", "Tab", "Escape", "Up", "Down"):
                return
            typed = combo.get()
            lowered = typed.lower()
            filtered = [n for n in names if lowered in n.lower()] if typed else list(names)
            display = list(filtered)
            if typed and lowered not in (n.lower() for n in names):
                display.append(f"{ADD_NEW_SERIES_PREFIX}{typed}")
            combo.configure(values=display)

        def commit(_event=None):
            if self._active_editor is not combo:
                return True
            target_name, error = self._resolve_series_choice(combo.get(), names)
            if error:
                messagebox.showwarning("Series not found", error)
                combo.focus_set()
                return False
            self._active_editor = None
            self._active_editor_close = None
            combo.destroy()
            self._push_undo({
                "type": "series_move", "run_id": run_id,
                "old_series_ids": list(self._point_series.get(run_id) or []),
            })
            self._move_point_to_series(run_id, self._series_id_for_name(target_name))
            self._render()
            return True

        def cancel():
            if self._active_editor is not combo:
                return
            self._active_editor = None
            self._active_editor_close = None
            combo.destroy()

        self._active_editor = combo
        self._active_editor_close = commit
        self._suppress_next_global_close = True
        combo.bind("<KeyRelease>", on_keyrelease)
        combo.bind("<<ComboboxSelected>>", commit)
        combo.bind("<Return>", commit)
        combo.bind("<Escape>", lambda _e: cancel())

    def _close_active_editor(self):
        if self._active_editor_close is not None:
            self._active_editor_close()

    def _on_global_click(self, event):
        if event.widget.winfo_toplevel() is not self.frame.winfo_toplevel():
            return
        if self._suppress_next_global_close:
            self._suppress_next_global_close = False
            return
        if self._active_editor is not None and event.widget is self._active_editor:
            return
        self._close_active_editor()

    # -- bulk move ------------------------------------------------------------
    def _on_move_selected(self):
        if not self._table_selection:
            messagebox.showwarning(
                "Nothing selected", 'Click a point\'s row to select it first.'
            )
            return
        names = [s["name"] for s in db.list_calibration_series(self.conn, gas=self.gas)]
        target_name, error = self._resolve_series_choice(self.move_target_var.get(), names)
        if error:
            messagebox.showwarning("Series not found", error)
            return
        target_series_id = self._series_id_for_name(target_name)
        moves = [
            (run_id, list(self._point_series.get(run_id) or [])) for run_id in sorted(self._table_selection)
        ]
        self._push_undo({"type": "bulk_series_move", "moves": moves})
        for run_id, _old_series_ids in moves:
            self._move_point_to_series(run_id, target_series_id)
        self.move_target_var.set("")
        self._render()

    # -- bulk add (keeps existing memberships, unlike Move) ---------------------
    def _on_add_selected(self):
        if not self._table_selection:
            messagebox.showwarning(
                "Nothing selected", 'Click a point\'s row to select it first.'
            )
            return
        names = [s["name"] for s in db.list_calibration_series(self.conn, gas=self.gas)]
        target_name, error = self._resolve_series_choice(self.move_target_var.get(), names)
        if error:
            messagebox.showwarning("Series not found", error)
            return
        target_series_id = self._series_id_for_name(target_name)
        run_ids = sorted(self._table_selection)
        added = [
            run_id for run_id in run_ids
            if target_series_id not in (self._point_series.get(run_id) or [])
        ]
        self._push_undo({"type": "bulk_series_add", "series_id": target_series_id, "run_ids": added})
        for run_id in added:
            self._add_point_to_series(run_id, target_series_id)
        self.move_target_var.set("")
        self._render()

    # -- remove from the whole selection (Backspace) ---------------------------
    def _on_backspace_remove(self, event=None):
        if self._active_editor is not None:
            return  # don't hijack Backspace while typing in the series editor
        run_ids = sorted(self._table_selection)
        if not run_ids:
            focused = self.points_tree.focus()
            if focused:
                run_ids = [int(focused)]
        if not run_ids:
            return "break"

        if len(run_ids) == 1:
            name = self._points.get(run_ids[0], {}).get("sample_name") or str(run_ids[0])
            prompt = (
                f'Remove "{name}" from the selection entirely?\n\n'
                "This deselects it in the Selector tab and removes it from every gas here in Models."
            )
        else:
            prompt = (
                f"Remove {len(run_ids)} points from the selection entirely?\n\n"
                "This deselects them in the Selector tab and removes them from every gas here in Models."
            )
        if self._confirm_remove(prompt):
            self._table_selection.difference_update(run_ids)
            if self._on_remove_runs is not None:
                self._on_remove_runs(run_ids)
        return "break"

    def _confirm_remove(self, message):
        """A small Yes/No dialog with Yes focused by default so Enter confirms -
        not the platform messagebox, since its default-button focus isn't reliable
        enough to guarantee that."""
        dialog = tk.Toplevel(self.frame)
        dialog.title("Remove point")
        dialog.transient(self.frame.winfo_toplevel())
        dialog.grab_set()
        ttk.Label(dialog, text=message, wraplength=320, justify="left").pack(padx=16, pady=(16, 10))

        result = {"value": False}

        def on_yes(_e=None):
            result["value"] = True
            dialog.destroy()

        def on_no(_e=None):
            result["value"] = False
            dialog.destroy()

        btn_bar = ttk.Frame(dialog)
        btn_bar.pack(pady=(0, 14))
        yes_btn = ttk.Button(btn_bar, text="Yes", command=on_yes)
        yes_btn.pack(side="left", padx=6)
        ttk.Button(btn_bar, text="No", command=on_no).pack(side="left", padx=6)
        yes_btn.focus_set()
        dialog.bind("<Return>", on_yes)
        dialog.bind("<Escape>", on_no)
        dialog.wait_window()
        return result["value"]

    # -- series "force through origin" toggle -----------------------------------
    def _on_series_click(self, event):
        region = self.series_tree.identify_region(event.x, event.y)
        if region != "cell" or self.series_tree.identify_column(event.x) != "#4":
            return
        row_iid = self.series_tree.identify_row(event.y)
        if not row_iid:
            return
        series_id = int(row_iid)
        row = next(
            (s for s in db.list_calibration_series(self.conn, gas=self.gas) if s["series_id"] == series_id), None
        )
        if row is not None:
            db.set_series_force_through_origin(self.conn, series_id, not row["force_through_origin"])
            self._render()

    # -- series CRUD ------------------------------------------------------------
    def _on_new_series(self):
        name = simpledialog.askstring("New series", "Series name:", parent=self.frame)
        if not name:
            return
        existing = [s["name"] for s in db.list_calibration_series(self.conn, gas=self.gas)]
        if name in existing:
            messagebox.showwarning("Already exists", f'A series named "{name}" already exists.')
            return
        db.create_calibration_series(self.conn, self.gas, name)
        self._render()

    def _on_rename_series(self):
        sel = self.series_tree.selection()
        if not sel:
            return
        series_id = int(sel[0])
        current = self.series_tree.item(sel[0], "text")
        name = simpledialog.askstring("Rename series", "Series name:", initialvalue=current, parent=self.frame)
        if not name:
            return
        db.rename_calibration_series(self.conn, series_id, name)
        self._render()

    def _on_delete_series(self):
        sel = self.series_tree.selection()
        if not sel:
            return
        series_id = int(sel[0])
        series_list = db.list_calibration_series(self.conn, gas=self.gas)
        if len(series_list) <= 1:
            messagebox.showwarning("Can't delete", "A gas must always have at least one series.")
            return
        name = self.series_tree.item(sel[0], "text")
        if not messagebox.askyesno(
            "Delete series", f'Delete series "{name}"? Its points will be reassigned to another series.'
        ):
            return
        db.delete_calibration_series(self.conn, series_id)
        self._assign_unassigned_points()
        self._render()
