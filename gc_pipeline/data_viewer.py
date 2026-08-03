"""The "Data viewer" panel: a pivot table (one row per selected run, one column per
parameter) built from the main browser's selection scratchpad. Columns and rows are
both user-reorderable via drag, any column can be added/removed, and the table can be
copied (tab-separated, pasteable into Excel) or exported as CSV."""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from gc_pipeline import db, export
from gc_pipeline.channels import AR_CHANNEL_GASES, channel_of
from gc_pipeline.pressure_wizard import PressureEntryWindow
from gc_pipeline.widgets import DropdownChecklist, bind_fast_hscroll, justify_columns

DRAG_HANDLE_GLYPH = "⋮"
COLUMN_HANDLE_GLYPH = "⋯"  # small, low-contrast hint - not the heavy "hamburger" glyph
DRAG_THRESHOLD_PX = 5
REMOVE_COLUMN_ID = "remove:row"
REMOVE_GLYPH = "×"
SHIFT_MASK = 0x0001
CTRL_MASK = 0x0004

# What "Add Gases" actually adds by default - it's the raw integrated peak area, not
# a calibrated amount (no calibration exists yet), so that's what it's labeled as.
DEFAULT_GAS_METRIC = "area"

# The order gas columns should appear in when bulk-added via "Add Gases" - not
# alphabetical, a fixed sequence the scientist expects (elution order, roughly).
GAS_ORDER = AR_CHANNEL_GASES

FIELD_LABELS = {
    "highlight": "Highlight",
    "sample_name": "Sample name",
    "run_number": "Run number",
    "injection_date": "Injection date",
    "folder": "Folder",
    "acq_method": "Acquisition method",
    "analysis_method": "Analysis method",
    "sample_type": "Sample type",
    "sample_amount": "Sample amount",
    "instrument": "Instrument",
    "duplicate_of": "Duplicate of",
    "excluded": "Excluded",
    "pressure": "Pressure",
    "standard": "Standard",
    "notes": "Notes",
}
FIELD_WIDTHS = {
    "sample_name": 160, "notes": 200, "acq_method": 160, "analysis_method": 160,
    "injection_date": 130, "run_number": 70, "instrument": 110, "highlight": 26,
}
GAS_COLUMN_WIDTH = 64  # gas columns run narrower/tighter than field columns by default

# "amount" is reserved for the Models tab's calculated calibration value (peak area
# vs. amount) - peaks.amount is a different thing (the instrument's own raw "Amount
# []" field), so it's labeled distinctly here to avoid the two being confused.
GAS_METRIC_LABELS = {"amount": "instrument amount", "rt": "RT", "rf": "RF",
                      "concentration": "concentration", "area": "peak area"}
GAS_METRICS = ("amount", "rt", "rf", "concentration", "area")


def _gas_sort_key(gas):
    return (GAS_ORDER.index(gas), gas) if gas in GAS_ORDER else (len(GAS_ORDER), gas)


def _gas_channel(gas, gas_channels):
    """Which single channel a gas belongs to, if it's exclusive to one - a gas seen
    on both channels (or never classified) isn't part of either's collapsible group."""
    channels = gas_channels.get(gas)
    if not channels or len(channels) != 1:
        return None
    return next(iter(channels))


def _column_label(col_id):
    if col_id.startswith("field:"):
        name = col_id.split(":", 1)[1]
        return FIELD_LABELS.get(name, name)
    _, gas, metric = col_id.split(":")
    return f"{gas} {GAS_METRIC_LABELS.get(metric, metric)}"


class DataViewerPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.conn = app.conn

        self._data = {}         # run_id -> sqlite3.Row (from db.get_data_viewer_rows)
        self._peaks = {}        # run_id -> {gas: peaks-row}
        self._flattened = []    # render cache: [{"kind": "header"/"run", ...}, ...]

        self._column_order = []     # seeded lazily on first non-empty refresh()
        self._defaults_seeded = False
        self._display_columns = []  # _column_order plus any spliced-in channel toggle columns
        self._row_order = []        # list[int] run_id, user-orderable, survives refresh()
        self._picker_ids = []
        self._label_to_id = {}
        self._collapsed_channels = set()  # {"Ar", "He"} channels currently shown narrow
        self._auto_added_gases = set()    # gases added by the last "Add Gases" click (toggle state)
        self._auto_rt_gases = set()       # gases whose RT column came from the last "Add RT" click

        self._active_editor = None
        self._active_editor_close = None
        self._suppress_next_global_close = False

        self._drag_row_iid = None
        self._header_drag_col_id = None
        self._header_press_x = None
        self._header_drag_active = False

        # Drag/click/shift-click multi-select of table rows (independent of the #0
        # handle column's own drag-to-reorder) - feeds the "Remove from selection"
        # button, letting several rows be pulled out of the main selection at once.
        self._table_selection = set()   # run_id set, currently checked in this table
        self._selection_anchor = None
        self._select_drag_anchor = None
        self._select_drag_target_state = None
        self._select_drag_snapshot = {}

        self._highlight_overlays = {}  # run_id -> tk.Label chip, same technique as the Selector tab

        self._build()

    # -- layout -------------------------------------------------------
    def _build(self):
        ttk.Label(self, text="Data viewer", font=("", 10, "bold")).pack(anchor="w", padx=4, pady=(4, 2))

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=4, pady=(0, 4))
        self.column_picker = DropdownChecklist(toolbar, "Columns", [], self._on_picker_changed)
        self.column_picker.pack(side="left")
        ttk.Button(toolbar, text="Add Gases", command=self.on_add_gases).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Add RT", command=self.on_add_rt_columns).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Justify columns", command=self.on_justify_columns).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Copy table", command=self.on_copy_table).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Export as CSV...", command=self.on_export_csv).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Reload selection", command=self.on_reload_selection).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(
            toolbar, text="Load Models/Analysis selection", command=self.on_load_models_selection
        ).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Remove from selection", command=self.on_remove_checked).pack(
            side="left", padx=(6, 0)
        )

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self.tree = ttk.Treeview(tree_frame, columns=(), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=32, anchor="center", stretch=False)
        self.tree.tag_configure("channel-header", background="#e8eef7")
        self.tree.tag_configure("selected", background="#bdddff")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        # Headers don't sort here - a stationary click removes the column, a drag
        # live-displaces columns as the mouse crosses each boundary (Excel-style),
        # disambiguated from a plain click by movement distance.
        self.tree.bind("<ButtonPress-1>", self._on_press, add="+")
        self.tree.bind("<B1-Motion>", self._on_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._on_release, add="+")
        self.tree.bind("<Button-3>", self._on_row_right_click, add="+")
        # Same click-away-closes-the-open-editor pattern as pressure_wizard.py.
        self.bind_all("<Button-1>", self._on_global_click, add="+")

        bind_fast_hscroll(self.tree)

        vsb.configure(command=self._on_vscroll)
        hsb.configure(command=self._on_hscroll)
        self.tree.bind("<Configure>", lambda _e: self._update_highlight_overlays(), add="+")
        self.tree.bind("<MouseWheel>", lambda _e: self.after(1, self._update_highlight_overlays), add="+")
        self.tree.bind(
            "<Shift-MouseWheel>", lambda _e: self.after(1, self._update_highlight_overlays), add="+"
        )

    def _on_vscroll(self, *args):
        self.tree.yview(*args)
        self._update_highlight_overlays()

    def _on_hscroll(self, *args):
        self.tree.xview(*args)
        self._update_highlight_overlays()

    def on_justify_columns(self):
        justify_columns(self.tree, [c for c in self._column_order])

    # -- gas / channel classification ---------------------------------------
    def _gas_channels(self):
        return db.get_gas_channels(self.conn)

    def _selected_channels(self):
        return {channel_of(row["acq_method"]) for row in self._data.values()}

    def _relevant_gases(self, gases, gas_channels, selected_channels):
        """A gas is relevant if it's never been classified to a specific channel
        (safe default: show it), or if at least one of the channels it's been
        reported on is actually present among the currently selected runs."""
        return [
            g for g in gases
            if not gas_channels.get(g) or (gas_channels[g] & selected_channels)
        ]

    # -- column model ---------------------------------------------------
    def _all_available_columns(self):
        cols = [f"field:{n}" for n in (
            "highlight", "sample_name", "run_number", "injection_date", "folder", "acq_method",
            "analysis_method", "sample_type", "sample_amount", "instrument", "duplicate_of", "excluded",
            "pressure", "standard", "notes",
        )]
        for gas in db.list_gases(self.conn):
            for metric in GAS_METRICS:
                cols.append(f"gas:{gas}:{metric}")
        return cols

    def _build_picker_options(self):
        self._picker_ids = self._all_available_columns()
        labels = [_column_label(c) for c in self._picker_ids]
        self._label_to_id = dict(zip(labels, self._picker_ids))
        self.column_picker.set_options(labels)
        for label, col_id in zip(labels, self._picker_ids):
            self.column_picker.set_checked(label, col_id in self._column_order)

    def _on_picker_changed(self):
        selected_ids = {self._label_to_id[l] for l in self.column_picker.selected() if l in self._label_to_id}
        kept = [c for c in self._column_order if c in selected_ids]
        added = [c for c in self._picker_ids if c in selected_ids and c not in kept]
        self._column_order = kept + added
        # Gas columns always clump together horizontally, whichever way they were
        # added - checking a new gas metric here shouldn't leave it stranded away
        # from the rest of the gas block.
        self._resequence_gas_columns(self._all_known_gas_order())
        self._render()

    def _all_known_gas_order(self):
        return sorted(db.list_gases(self.conn), key=_gas_sort_key)

    def _remove_column(self, col_id):
        if col_id not in self._column_order:
            return
        self._column_order.remove(col_id)
        self.column_picker.set_checked(_column_label(col_id), False)
        if col_id.startswith("gas:"):
            _, gas, metric = col_id.split(":")
            if metric == DEFAULT_GAS_METRIC:
                self._auto_added_gases.discard(gas)
            elif metric == "rt":
                self._auto_rt_gases.discard(gas)
        self._render()

    def on_add_gases(self):
        """Toggle: click once to add every gas relevant to the current selection's
        channel(s), click again to remove exactly the ones that click added."""
        if self._auto_added_gases:
            for gas in self._auto_added_gases:
                col_id = f"gas:{gas}:{DEFAULT_GAS_METRIC}"
                if col_id in self._column_order:
                    self._column_order.remove(col_id)
                    self.column_picker.set_checked(_column_label(col_id), False)
            self._auto_added_gases = set()
            self._render()
            return

        all_gases = self._all_known_gas_order()
        gas_channels = self._gas_channels()
        relevant = self._relevant_gases(all_gases, gas_channels, self._selected_channels())
        added = set()
        for gas in relevant:
            col_id = f"gas:{gas}:{DEFAULT_GAS_METRIC}"
            if col_id not in self._column_order:
                self._column_order.append(col_id)
                self.column_picker.set_checked(_column_label(col_id), True)
                added.add(gas)
        self._resequence_gas_columns(all_gases)
        self._auto_added_gases = added
        self._render()

    def _resequence_gas_columns(self, gas_order):
        """Put every currently-present gas column (amount, RT, ...) into GAS_ORDER,
        keeping each gas's own sub-columns (e.g. amount + RT) glued together in
        whatever relative order they were already in - so an existing RT column
        moves along with its gas instead of being left behind."""
        gas_cols_by_gas = {}
        first_gas_idx = None
        for i, c in enumerate(self._column_order):
            if c.startswith("gas:"):
                gas = c.split(":")[1]
                gas_cols_by_gas.setdefault(gas, []).append(c)
                if first_gas_idx is None:
                    first_gas_idx = i
        if first_gas_idx is None:
            return
        before = [c for c in self._column_order[:first_gas_idx] if not c.startswith("gas:")]
        after = [c for c in self._column_order[first_gas_idx:] if not c.startswith("gas:")]
        ordered_gas_cols = []
        for gas in gas_order:
            ordered_gas_cols.extend(gas_cols_by_gas.pop(gas, []))
        for cols in gas_cols_by_gas.values():  # any gas outside the known order
            ordered_gas_cols.extend(cols)
        self._column_order = before + ordered_gas_cols + after

    def _toggle_channel_collapse(self, channel):
        if channel in self._collapsed_channels:
            self._collapsed_channels.discard(channel)
        else:
            self._collapsed_channels.add(channel)
        self._render()

    def on_add_rt_columns(self):
        """Toggle: click once to add an RT column next to every gas currently shown
        (whatever metric that gas is showing), click again to remove exactly the
        RT columns that click added."""
        if self._auto_rt_gases:
            for gas in self._auto_rt_gases:
                rt_id = f"gas:{gas}:rt"
                if rt_id in self._column_order:
                    self._column_order.remove(rt_id)
                    self.column_picker.set_checked(_column_label(rt_id), False)
            self._auto_rt_gases = set()
            self._render()
            return

        gases_shown = sorted({c.split(":")[1] for c in self._column_order if c.startswith("gas:")})
        added = set()
        for gas in gases_shown:
            rt_id = f"gas:{gas}:rt"
            if rt_id in self._column_order:
                continue
            # Insert right after this gas's last existing column, whatever metric(s)
            # it currently has - not hardcoded to a specific "amount"/"area" column.
            last_idx = max(i for i, c in enumerate(self._column_order) if c.startswith(f"gas:{gas}:"))
            self._column_order.insert(last_idx + 1, rt_id)
            self.column_picker.set_checked(_column_label(rt_id), True)
            added.add(gas)
        self._auto_rt_gases = added
        self._render()

    # -- data / refresh ---------------------------------------------------
    def refresh(self, run_ids):
        run_ids = list(run_ids)

        self._close_active_editor()
        self._data = {}
        self._peaks = {}
        if run_ids:
            self._data = {r["run_id"]: r for r in db.get_data_viewer_rows(self.conn, run_ids)}
            self._peaks = db.get_peaks_map_for_runs(self.conn, run_ids)

        # Deferred until the first non-empty selection - seeding while nothing is
        # selected yet (e.g. at app startup) would have no runs to judge channel
        # relevance against, and wrongly exclude every channel-classified gas.
        if not self._defaults_seeded and run_ids:
            self._defaults_seeded = True
            all_gases = sorted(db.list_gases(self.conn), key=_gas_sort_key)
            gas_channels = self._gas_channels()
            relevant = self._relevant_gases(all_gases, gas_channels, self._selected_channels())
            self._column_order = (
                ["field:highlight", "field:sample_name", "field:run_number", "field:pressure"]
                + [f"gas:{g}:{DEFAULT_GAS_METRIC}" for g in relevant]
                + ["field:notes"]
            )
            self._build_picker_options()

        selected_set = set(run_ids)
        self._table_selection &= selected_set
        survivors = [rid for rid in self._row_order if rid in selected_set]
        survivor_set = set(survivors)
        new_ids = [rid for rid in run_ids if rid not in survivor_set]
        new_ids.sort(key=lambda rid: (
            (self._data[rid]["injection_date"] or "") if rid in self._data else "",
            (self._data[rid]["run_number"] or 0) if rid in self._data else 0,
        ))
        self._row_order = survivors + new_ids
        self._render()

    def _cell_display(self, run_id, col_id):
        row = self._data.get(run_id)
        if row is None:
            return ""
        if col_id.startswith("field:"):
            name = col_id.split(":", 1)[1]
            if name == "highlight":
                return ""  # drawn as a colored chip overlay, not text - see _update_highlight_overlays
            if name == "standard":
                return row["standard_name"] or ""
            if name == "excluded":
                return "yes" if row["excluded"] else ""
            if name == "folder":
                # Derived, not a stored column - the calendar-date folder the raw
                # CSV actually lives under (e.g. "2026-07-06"), same value the
                # Selector used to group runs by before grouping switched to rounds.
                return (row["injection_date"] or "")[:10]
            val = row[name]
            return "" if val is None else val
        _, gas, metric = col_id.split(":")
        peak = self._peaks.get(run_id, {}).get(gas)
        if peak is None or peak[metric] is None:
            return "BD"
        return peak[metric]

    def _compute_flattened(self):
        flattened = []
        prev_channel = object()  # sentinel forces a header before the first row
        for run_id in self._row_order:
            row = self._data.get(run_id)
            if row is None:
                continue
            channel = channel_of(row["acq_method"])
            if channel != prev_channel:
                flattened.append({"kind": "header", "label": row["acq_method"] or "(no acquisition method)"})
                prev_channel = channel
            flattened.append({"kind": "run", "run_id": run_id})
        return flattened

    def _compute_display_columns(self, gas_channels):
        """self._column_order (the real, user-managed columns) plus a small toggle
        pseudo-column spliced in right before the first gas column of any channel
        that currently has 2+ columns present - clicking it collapses/expands that
        whole channel's columns to narrow width. Toggle columns are pure UI chrome:
        they never appear in self._column_order, exports, or copy output."""
        counts = {}
        for c in self._column_order:
            if c.startswith("gas:"):
                gas = c.split(":")[1]
                ch = _gas_channel(gas, gas_channels)
                if ch:
                    counts[ch] = counts.get(ch, 0) + 1

        display = [REMOVE_COLUMN_ID]
        inserted_for = set()
        for c in self._column_order:
            ch = None
            if c.startswith("gas:"):
                ch = _gas_channel(c.split(":")[1], gas_channels)
            if ch and counts.get(ch, 0) >= 2 and ch not in inserted_for:
                display.append(f"toggle:{ch}")
                inserted_for.add(ch)
            display.append(c)
        return display

    def _render(self):
        self._flattened = self._compute_flattened()
        gas_channels = self._gas_channels()
        self._display_columns = self._compute_display_columns(gas_channels)
        self.tree.configure(columns=tuple(self._display_columns))

        for col_id in self._display_columns:
            if col_id == REMOVE_COLUMN_ID:
                # One big X above the per-row X's - clears every run currently
                # loaded here out of the selection in one click, same as clicking
                # every row's own remove button individually.
                self.tree.heading(col_id, text=REMOVE_GLYPH)
                self.tree.column(col_id, width=22, anchor="center", stretch=False)
                continue
            if col_id.startswith("toggle:"):
                channel = col_id.split(":")[1]
                collapsed = channel in self._collapsed_channels
                self.tree.heading(col_id, text="+" if collapsed else "−")
                self.tree.column(col_id, width=18, anchor="center", stretch=False)
                continue
            channel = _gas_channel(col_id.split(":")[1], gas_channels) if col_id.startswith("gas:") else None
            collapsed = channel is not None and channel in self._collapsed_channels
            if collapsed:
                self.tree.heading(col_id, text=_column_label(col_id)[:3])
                self.tree.column(col_id, width=30, anchor="center", stretch=False)
            else:
                # The handle glyph hints that headers are draggable-to-reorder (a
                # plain click removes the column instead - see _on_release).
                self.tree.heading(col_id, text=f"{COLUMN_HANDLE_GLYPH} {_column_label(col_id)}")
                if col_id.startswith("gas:"):
                    width = GAS_COLUMN_WIDTH
                else:
                    name = col_id.split(":", 1)[1]
                    width = FIELD_WIDTHS.get(name, 110)
                # stretch=False: columns keep their natural width as more get added
                # rather than shrinking to cram everything into view - overflow
                # scrolls instead (horizontally, or via "Justify columns" to fill
                # any left-over space on demand).
                self.tree.column(col_id, width=width, anchor="w", stretch=False)

        self.tree.delete(*self.tree.get_children())
        for i, item in enumerate(self._flattened):
            if item["kind"] == "header":
                # The acq_method label goes in the first real (non-chrome) column -
                # not always index 0, since the remove/toggle columns sit ahead of it.
                values = []
                label_placed = False
                for c in self._display_columns:
                    if c == REMOVE_COLUMN_ID or c.startswith("toggle:"):
                        values.append("")
                    elif not label_placed:
                        values.append(item["label"])
                        label_placed = True
                    else:
                        values.append("")
                self.tree.insert(
                    "", "end", iid=f"hdr-{i}", text="", open=True,
                    values=values, tags=("channel-header",),
                )
            else:
                run_id = item["run_id"]
                values = [
                    REMOVE_GLYPH if c == REMOVE_COLUMN_ID
                    else "" if c.startswith("toggle:")
                    else self._cell_display(run_id, c)
                    for c in self._display_columns
                ]
                tags = ("run", "selected") if run_id in self._table_selection else ("run",)
                self.tree.insert("", "end", iid=f"run-{run_id}", text=DRAG_HANDLE_GLYPH,
                                  values=values, tags=tags)

        self._update_highlight_overlays()

    def _update_highlight_overlays(self):
        """Same overlay-Label technique the Selector tab uses for its "highlight"
        column - ttk.Treeview has no native per-cell background, so a colored chip
        is placed directly on top of the cell instead."""
        for label in self._highlight_overlays.values():
            label.destroy()
        self._highlight_overlays = {}
        if "field:highlight" not in self._display_columns:
            return
        self.tree.update_idletasks()
        colors = db.get_run_highlight_colors(self.conn)
        if not colors:
            return
        for iid in self.tree.get_children(""):
            if not iid.startswith("run-"):
                continue
            run_id = int(iid.split("-", 1)[1])
            color = colors.get(run_id)
            if not color:
                continue
            bbox = self.tree.bbox(iid, "field:highlight")
            if not bbox:
                continue
            x, y, w, h = bbox
            label = tk.Label(self.tree, bg=color)
            label.place(x=x, y=y, width=w, height=h)
            self._highlight_overlays[run_id] = label

    # -- column drag-reorder / click-to-remove, row drag-reorder ------------
    def _on_press(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region == "heading":
            col = self.tree.identify_column(event.x)
            if col != "#0":
                idx = int(col[1:]) - 1
                if 0 <= idx < len(self._display_columns):
                    col_id = self._display_columns[idx]
                    if col_id == REMOVE_COLUMN_ID:
                        if self._data:
                            self.app.remove_from_selection_many(list(self._data.keys()))
                        return
                    if col_id.startswith("toggle:"):
                        self._toggle_channel_collapse(col_id.split(":")[1])
                        return
                    self._header_drag_col_id = col_id
                    self._header_press_x = event.x
                    self._header_drag_active = False
                    # "fleur" (a 4-way move cursor) hints that the header can be
                    # dragged to reorder it, for as long as the mouse button is held.
                    self.tree.configure(cursor="fleur")
            return
        if region == "cell":
            col = self.tree.identify_column(event.x)
            row_iid = self.tree.identify_row(event.y)
            if row_iid and row_iid.startswith("run-"):
                idx = int(col[1:]) - 1
                if 0 <= idx < len(self._display_columns):
                    col_id = self._display_columns[idx]
                    if col_id == REMOVE_COLUMN_ID:
                        self.app.remove_from_selection(int(row_iid.split("-", 1)[1]))
                        return
                    shift = bool(event.state & SHIFT_MASK)
                    ctrl = bool(event.state & CTRL_MASK)
                    if col_id == "field:notes" and not shift and not ctrl:
                        self._start_notes_edit(row_iid)
                        return
                    if not col_id.startswith("toggle:"):
                        self._handle_row_selection_click(row_iid, shift, ctrl)
                        return
        if self.tree.identify_column(event.x) == "#0":
            iid = self.tree.identify_row(event.y)
            if iid and iid.startswith("run-"):
                self._drag_row_iid = iid

    def _on_motion(self, event):
        if self._header_drag_col_id is not None:
            if not self._header_drag_active:
                if abs(event.x - self._header_press_x) < DRAG_THRESHOLD_PX:
                    return
                self._header_drag_active = True
            target_col = self.tree.identify_column(event.x)
            if target_col in ("", "#0"):
                return
            idx = int(target_col[1:]) - 1
            if not (0 <= idx < len(self._display_columns)):
                return
            target_id = self._display_columns[idx]
            if target_id == REMOVE_COLUMN_ID or target_id.startswith("toggle:"):
                return  # can't drop into a chrome column's slot - wait for a real column
            dst_idx = self._column_order.index(target_id)
            src_idx = self._column_order.index(self._header_drag_col_id)
            # Live displacement, Excel-style: the dragged column swaps into whatever
            # slot the cursor is currently over, re-rendering immediately rather than
            # waiting for release.
            if dst_idx != src_idx:
                self._column_order.pop(src_idx)
                self._column_order.insert(dst_idx, self._header_drag_col_id)
                self._render()
            return

        if self._select_drag_anchor is not None:
            row_iid = self.tree.identify_row(event.y)
            if not row_iid or not row_iid.startswith("run-"):
                return
            run_id = int(row_iid.split("-", 1)[1])
            order = self._flattened_run_ids()
            if self._select_drag_anchor not in order or run_id not in order:
                return
            i, j = order.index(self._select_drag_anchor), order.index(run_id)
            lo, hi = min(i, j), max(i, j)
            in_range = set(order[lo:hi + 1])
            for rid in order:
                desired = (
                    self._select_drag_target_state if rid in in_range
                    else self._select_drag_snapshot.get(rid, False)
                )
                if (rid in self._table_selection) != desired:
                    self._set_row_table_selected(rid, desired)
            return

        if not self._drag_row_iid:
            return
        target = self.tree.identify_row(event.y)
        if target and target != self._drag_row_iid:
            self.tree.move(self._drag_row_iid, "", self.tree.index(target))

    def _on_release(self, event):
        if self._header_drag_col_id is not None:
            self.tree.configure(cursor="")
            col_id = self._header_drag_col_id
            was_dragging = self._header_drag_active
            self._header_drag_col_id = None
            self._header_drag_active = False
            if not was_dragging:
                # Never actually moved - a plain click removes the column instead.
                self._remove_column(col_id)
            return

        if self._select_drag_anchor is not None:
            self._select_drag_anchor = None
            return

        if self._drag_row_iid:
            self._row_order = [int(i.split("-", 1)[1]) for i in self.tree.get_children("")
                                if i.startswith("run-")]
            self._drag_row_iid = None
            self._render()

    # -- multi-row selection (click/shift-click/ctrl-shift-click/drag) --------
    def _flattened_run_ids(self):
        return [int(iid.split("-", 1)[1]) for iid in self.tree.get_children("") if iid.startswith("run-")]

    def _handle_row_selection_click(self, row_iid, shift, ctrl):
        run_id = int(row_iid.split("-", 1)[1])
        if shift and ctrl and self._selection_anchor is not None:
            self._toggle_selection_range(self._selection_anchor, run_id)
            self._select_drag_anchor = None
        elif shift and self._selection_anchor is not None:
            self._select_selection_range(self._selection_anchor, run_id)
            self._select_drag_anchor = None
        else:
            self._select_drag_snapshot = {rid: (rid in self._table_selection) for rid in self._flattened_run_ids()}
            was_selected = run_id in self._table_selection
            self._set_row_table_selected(run_id, not was_selected)
            self._selection_anchor = run_id
            self._select_drag_anchor = run_id
            self._select_drag_target_state = not was_selected

    def _select_selection_range(self, anchor_run_id, target_run_id):
        order = self._flattened_run_ids()
        if anchor_run_id not in order or target_run_id not in order:
            return
        i, j = order.index(anchor_run_id), order.index(target_run_id)
        lo, hi = min(i, j), max(i, j)
        for rid in order[lo:hi + 1]:
            self._set_row_table_selected(rid, True)

    def _toggle_selection_range(self, anchor_run_id, target_run_id):
        order = self._flattened_run_ids()
        if anchor_run_id not in order or target_run_id not in order:
            return
        desired = target_run_id not in self._table_selection
        i, j = order.index(anchor_run_id), order.index(target_run_id)
        lo, hi = min(i, j), max(i, j)
        for rid in order[lo:hi + 1]:
            self._set_row_table_selected(rid, desired)

    def _set_row_table_selected(self, run_id, selected):
        if selected:
            self._table_selection.add(run_id)
        else:
            self._table_selection.discard(run_id)
        row_iid = f"run-{run_id}"
        if self.tree.exists(row_iid):
            tags = ("run", "selected") if selected else ("run",)
            self.tree.item(row_iid, tags=tags)

    def _on_row_right_click(self, event):
        row_iid = self.tree.identify_row(event.y)
        if not row_iid or not row_iid.startswith("run-"):
            return
        run_id = int(row_iid.split("-", 1)[1])
        # Same "act on the checked selection if there is one, else just the
        # right-clicked row" convention as the main Selector tree's own right-click
        # menu - a right-click on a row outside the current selection targets just
        # that row rather than silently discarding it in favor of the selection.
        run_ids = list(self._table_selection) if self._table_selection else [run_id]
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label=f"Edit {len(run_ids)} run(s) in pressure entry...",
            command=lambda: self._open_pressure_editor(run_ids),
        )
        menu.tk_popup(event.x_root, event.y_root)

    def _open_pressure_editor(self, run_ids):
        PressureEntryWindow(
            self.app, run_ids=run_ids, missing_only=False,
            on_close_callback=self.on_reload_selection,
        )

    def on_remove_checked(self):
        if not self._table_selection:
            messagebox.showwarning(
                "Nothing checked", "Click (or shift/ctrl-shift-click, or drag) rows in the table first."
            )
            return
        run_ids = list(self._table_selection)
        self._table_selection.clear()
        self.app.remove_from_selection_many(run_ids)

    # -- notes cell editing --------------------------------------------------
    def _close_active_editor(self):
        if self._active_editor_close is not None:
            self._active_editor_close()

    def _on_global_click(self, event):
        if event.widget.winfo_toplevel() is not self.winfo_toplevel():
            return
        if self._suppress_next_global_close:
            self._suppress_next_global_close = False
            return
        if self._active_editor is not None and event.widget is self._active_editor:
            return
        self._close_active_editor()

    def _start_notes_edit(self, row_iid):
        self._close_active_editor()
        self.tree.see(row_iid)
        self.update_idletasks()
        bbox = self.tree.bbox(row_iid, "field:notes")
        if not bbox:
            return
        x, y, w, h = bbox
        run_id = int(row_iid.split("-", 1)[1])
        var = tk.StringVar(value=self.tree.set(row_iid, "field:notes"))
        entry = tk.Entry(self.tree, textvariable=var, relief="solid", borderwidth=1)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.select_range(0, "end")

        def commit(_event=None):
            if self._active_editor is not entry:
                return
            self._active_editor = None
            self._active_editor_close = None
            new = var.get().strip()
            entry.destroy()
            db.set_run_notes(self.conn, run_id, new)
            self.tree.set(row_iid, "field:notes", new)
            # Keep the always-visible Notes box (owned by the main App, populated by
            # double-clicking a run in the main tree) in sync if it's showing this run.
            self.app.sync_notes_display(run_id, new)

        def on_return(_event=None):
            commit()
            run_iids = [f"run-{item['run_id']}" for item in self._flattened if item["kind"] == "run"]
            if row_iid in run_iids:
                idx = run_iids.index(row_iid)
                if idx + 1 < len(run_iids):
                    self._start_notes_edit(run_iids[idx + 1])

        def cancel(_event=None):
            if self._active_editor is not entry:
                return
            self._active_editor = None
            self._active_editor_close = None
            entry.destroy()

        self._active_editor = entry
        self._active_editor_close = commit
        self._suppress_next_global_close = True
        entry.bind("<Return>", on_return)
        entry.bind("<Escape>", cancel)

    # -- copy / CSV export ----------------------------------------------------
    def _table_rows(self):
        header = [_column_label(c) for c in self._column_order]
        rows = [header]
        for item in self._flattened:
            if item["kind"] == "header":
                rows.append([item["label"]] + [""] * (len(self._column_order) - 1))
            else:
                rows.append([self._cell_display(item["run_id"], c) for c in self._column_order])
        return rows

    def on_copy_table(self):
        if not self._column_order:
            return
        self.clipboard_clear()
        self.clipboard_append(export.rows_to_tsv(self._table_rows()))

    def on_export_csv(self):
        if not self._column_order:
            messagebox.showwarning("Nothing to export", "The data viewer table is empty.")
            return
        out_path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV file", "*.csv")])
        if not out_path:
            return
        export.write_csv(self._table_rows(), out_path)
        messagebox.showinfo("Export complete", f"Wrote {out_path}")

    # -- pulling a run set back into this table --------------------------------
    def on_reload_selection(self):
        """Re-syncs this table against the main tree's current checkbox selection -
        normally automatic (every selection change already calls refresh()), but
        offered as an explicit action too for a manual "did I lose sync" refresh."""
        self.refresh(self.app.selected_run_ids)

    def on_load_models_selection(self):
        """Pulls back whatever run set is currently loaded in the Models/Analysis
        tabs (via "Load selected into Models") and makes that the main tree's
        selection again - useful once the Selector's own checkbox selection has
        moved on to something else, without having to go re-find and re-check every
        one of those runs by hand."""
        run_ids = list(self.app.models_panel._run_ids) or list(self.app.analysis_panel._run_ids)
        if not run_ids:
            messagebox.showwarning(
                "Nothing loaded", "No runs are currently loaded in the Models/Analysis tabs."
            )
            return
        self.app.set_selected_run_ids(run_ids)

    # -- called by the main App when a run's notes change elsewhere ----------
    def sync_notes_cell(self, run_id, new_text):
        """The App's always-visible Notes box (populated by double-clicking a run in
        the main tree) is the other place a run's notes can be edited - keep this
        table's own notes cell in sync if that run happens to be a visible row."""
        row_iid = f"run-{run_id}"
        if self.tree.exists(row_iid) and "field:notes" in self._column_order:
            self.tree.set(row_iid, "field:notes", new_text)
