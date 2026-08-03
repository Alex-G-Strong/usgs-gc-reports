"""The "Duplicates wizard": every same-identity group (2+ non-synthetic runs sharing
sample_name + injection_date, but not necessarily byte-identical - a plain re-ingest
of the same file, or a reprocessed export whose peak values changed after review)
lands here, regardless of whether its fields agree or not. One flat table lists every
candidate run from every pending group consecutively, with a divider row between
groups.

Each field column is pre-filled with a smart default (db.default_field_selections:
newest run for measured peak values, earliest entry for anything a human typed in) -
click a different run's cell in a column (any gas, not just a fixed Ar-suite list -
whatever gases the group's own runs actually reported) to override that group's pick
for it, then merge. The chosen cell in each column gets a light-blue overlay
(ttk.Treeview has no native per-cell background, so this uses the same place()'d-
widget-over-a-cell technique as the Selector/pressure wizard's row-highlight chips) -
exactly one per column per group, Excel-style. Clicking the already-picked cell again
deselects it instead of re-picking it - useful when every version's value for that
field is actually wrong (e.g. a peak the instrument mis-detected that isn't real) and
the field should just be left out of the merged run entirely, shown as "— excluded"
on the group's own divider row (in the same grey as the rest of that row, since the
exclusion is a property of the group, not any one version's cell).

The "Sample" column is different - clicking (or shift-clicking, or dragging through)
a row there selects/deselects that single row (never the divider header above its
group), painted with a light grey bar (native ttk row selection is turned off
entirely so nothing fights with the per-cell blue). Selecting any one row is enough
to mark its whole group for "Merge selection", since a merge always resolves a group
down to one surviving run regardless of which row you happened to click.

Sorting: clicking "Sample" or "Sample date" reorders which group appears where in the
table (those two are the same for every row in a group, so there's nothing to sort
*within* a group). Every other column instead reorders each group's own rows by that
column's value, independently per group - clicking "Date added to DB" doesn't move
groups around, it reorders the 2-3 rows inside each one."""

import tkinter as tk
from tkinter import ttk, messagebox

from gc_pipeline import db
from gc_pipeline.widgets import bind_fast_hscroll, bind_tooltip

FIXED_COLUMNS = ("pressure", "standard_id", "round_id", "run_number", "notes")
FIXED_LABELS = {
    "pressure": "Pressure", "standard_id": "Standard", "round_id": "Round",
    "run_number": "Run #", "notes": "Notes",
}
# Leftmost block: sample date, then sample id (the row-selection column), then a
# purely-decorative separator column - none of these three are ever a per-run pick,
# and "sep" isn't even sortable. "ingested_at" (Date added to DB) IS a real per-row
# value, just not one that gets merged/picked - it only ever sorts within a group.
GROUP_SELECT_COLUMN = "sample_name"
INERT_COLUMNS = ("injection_date", "sample_name", "sep", "ingested_at")
WHOLE_TABLE_SORT_COLUMNS = ("sample_name", "injection_date")

CELL_SELECTED_BG = "#cfe3ff"
CELL_SELECTED_FG = "#1a3d6d"
DIVIDER_BG = "#e4e4e4"
ROW_SELECTED_BG = "#e4e4e4"
# A thin outline (not a fill - the pale blue fill already means "this is the current
# pick") around a picked cell whose value actually disagrees with another version's
# non-null value for the same field in the same group - a "heads up, these didn't
# match" flag distinct from the fill it wraps.
DISAGREEMENT_OUTLINE = "#2563eb"
# Shown in place of the blue pick when a field's been deliberately cleared (e.g. a
# peak the instrument mis-detected that shouldn't survive into the merged run at
# all) - lives on the group's own divider row (not any one version's row, since the
# exclusion isn't about any particular run), in the same grey as that row so it
# reads as one continuous strip rather than a separate floating box.
NULL_SELECTION_BG = DIVIDER_BG
NULL_SELECTION_FG = "#666666"
NULL_SELECTION_TEXT = "— excluded"  # em dash
SHIFT_MASK = 0x0001
CTRL_MASK = 0x0004


class DuplicatesWizardPanel(ttk.Frame):
    """The wizard's actual content - a plain ttk.Frame so it can be embedded either
    in the standalone popup window (DuplicatesWizardWindow, below) or directly as a
    sub-tab of the Load Data tab."""

    def __init__(self, parent, app, on_next=None):
        super().__init__(parent)
        self.app = app
        self.conn = app.conn
        # Only set by the Load Data tab (not the standalone popup, which has no
        # "next tab" to go to) - clicking "Save selection, and continue" merges
        # whatever's still unresolved using the defaults shown, then moves on.
        self._on_next = on_next

        self._groups = {}        # (sample_name, injection_date) -> [run_id, ...], oldest-first
        self._group_order = []   # group keys, in current whole-table sort order
        self._matrices = {}      # group_key -> field_matrix (db.get_group_field_matrix)
        self._selections = {}    # group_key -> {field_name: run_id} - survives across refreshes
        self._standard_names = {}
        self._round_names = {}
        self._ingested_at = {}   # run_id -> ingested_at (own value, not part of the field matrix)
        self._gas_columns = []   # ordered "gas:<gas>" columns present across currently-loaded groups
        self._row_group = {}     # tree iid -> group_key

        # Whole-table group ordering (Sample / Sample date headers only) - both
        # default to earliest-first.
        self._group_sort_column = "injection_date"
        self._group_sort_reverse = False
        # Within-group row ordering (every other column) - also defaults to
        # earliest-added-first, same as db.find_same_identity_groups' own natural
        # order, but made an explicit active sort (rather than implicit/"None") so
        # the header shows it and re-sorting behaves identically to every other
        # column.
        self._row_sort_column = "ingested_at"
        self._row_sort_reverse = False

        self._selection_overlays = {}  # (group_key, field) -> tk.Label
        self._disagreement_cache = {}  # (group_key, field) -> bool, cleared each refresh()
        self._selected_rows = set()    # row iids ("row-<run_id>") - never a divider iid
        self._row_click_anchor = None
        self._drag_active = False
        self._drag_target_state = None
        self._drag_snapshot = {}

        # "Assign run number from name" (the single toggle above the Run # column) -
        # for whichever groups it's covering, the run_number column's normal
        # per-cell pick is suppressed and the merge instead computes the number
        # straight from the sample name's own trailing digits (e.g. "Air_Ar_015" ->
        # 15), applied to the surviving run right after the merge.
        self._auto_run_number_groups = set()
        self._run_number_checkboxes = {}  # group_key -> tk.Checkbutton overlay (per-group, in-table)
        self._run_number_header_checkbox = None  # the global toggle - a real widget, set up in _build()
        self._run_number_toggle_var = None

        self.tree = None
        self._tree_gas_columns = None  # gas columns the current tree widget was built for
        self._build()
        self.refresh()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="Duplicates wizard", font=("", 11, "bold")).pack(side="left")
        self.status_label = ttk.Label(top, text="", foreground="#666666")
        self.status_label.pack(side="left", padx=(12, 0))

        hint = ttk.Label(
            self,
            text="Runs here share a sample name and injection date but aren't byte-identical - usually a "
                 "reprocessed export whose peak values changed after review. Each column starts pre-filled "
                 "with a sensible default (newest file for measured values, earliest entry for anything you "
                 "typed in); click a different run's cell to override that group's pick (shown in light "
                 "blue), or click an already-picked cell again to exclude that field entirely. Checking a "
                 "row in the Sample column (grey) also adopts that whole version's own values everywhere "
                 "it has one, instead of clicking each cell by hand. \"Merge selection\" merges whichever "
                 "groups have a row checked, or - if none are checked - every group shown, using whatever's "
                 "currently picked. Click a header to sort - Sample/Sample date reorder the whole list, any "
                 "other column reorders just the rows within each group.",
            foreground="#666666", wraplength=940, justify="left",
        )
        hint.pack(fill="x", padx=8, pady=(0, 6))

        # A real widget in the panel's own layout, not a place()'d overlay tied to
        # the tree's scroll position/row geometry - it doesn't need per-render
        # rebuilding the way the per-group checkboxes on the divider rows do, and
        # it stays visible regardless of horizontal scroll.
        run_number_bar = ttk.Frame(self)
        run_number_bar.pack(fill="x", padx=8, pady=(0, 6))
        self._run_number_toggle_var = tk.BooleanVar(value=False)
        self._run_number_header_checkbox = ttk.Checkbutton(
            run_number_bar, text="Assign run number from name", variable=self._run_number_toggle_var,
            command=self._on_toggle_all_auto_run_number,
        )
        self._run_number_header_checkbox.pack(side="left")
        bind_tooltip(
            self._run_number_header_checkbox,
            "Assigns every eligible group's run number from its own sample name's trailing digits (e.g. "
            "\"Air_Ar_015\" -> 15), instead of picking one from an existing version. Skips (and doesn't "
            "affect) any group whose sample name has no trailing number - assign those manually in the "
            "Pressure Entry wizard after saving. Each group also has its own toggle in the Run # column.",
        )

        self.tree_frame = ttk.Frame(self)
        self.tree_frame.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.tree_frame.columnconfigure(0, weight=1)
        self.tree_frame.rowconfigure(0, weight=1)

        bottom_bar = ttk.Frame(self)
        bottom_bar.pack(fill="x", padx=8, pady=(0, 8))
        merge_style = ttk.Style(self)
        merge_style.configure("Merge.TButton", font=("", 11, "bold"), padding=(14, 8))
        ttk.Button(
            bottom_bar, text="Merge selection", style="Merge.TButton", command=self.on_merge_selection
        ).pack(side="left")
        if self._on_next is not None:
            ttk.Label(
                bottom_bar,
                text="Once every group is merged, you'll move on to pressure entry automatically.",
                foreground="#666666",
            ).pack(side="left", padx=(10, 0))

    # -- loading ----------------------------------------------------------------
    def refresh(self):
        self._groups = db.find_same_identity_groups(self.conn)
        valid_run_ids = {rid for run_ids in self._groups.values() for rid in run_ids}
        self._selected_rows = {
            iid for iid in self._selected_rows if int(iid.split("-", 1)[1]) in valid_run_ids
        }
        self._auto_run_number_groups &= set(self._groups.keys())
        self._disagreement_cache = {}
        self._matrices = {}
        self._standard_names = {row["standard_id"]: row["name"] for row in db.list_standards(self.conn)}
        self._round_names = {row["round_id"]: row["name"] for row in db.list_run_rounds(self.conn)}
        all_run_ids = [rid for run_ids in self._groups.values() for rid in run_ids]
        self._ingested_at = {}
        if all_run_ids:
            placeholders = ",".join("?" for _ in all_run_ids)
            for row in self.conn.execute(
                f"SELECT run_id, ingested_at FROM runs WHERE run_id IN ({placeholders})", all_run_ids
            ):
                self._ingested_at[row["run_id"]] = row["ingested_at"]

        gas_set = set()
        selections = {}
        for group_key, run_ids in self._groups.items():
            matrix = db.get_group_field_matrix(self.conn, run_ids)
            self._matrices[group_key] = matrix
            defaults = db.default_field_selections(matrix, run_ids)
            prior = self._selections.get(group_key, {})
            # Keep any prior manual choice the user already made for a field that's
            # still valid (that run is still in the group and still has a value);
            # anything else falls back to the freshly-computed default.
            merged = dict(defaults)
            for field, run_id in prior.items():
                if run_id is None:
                    # An explicit "exclude this field" choice - not "no info yet",
                    # so it must survive a refresh the same way a real pick does,
                    # or the default would silently reassert itself.
                    merged[field] = None
                elif matrix.get(field, {}).get(run_id) is not None:
                    merged[field] = run_id
            selections[group_key] = merged
            # Any gas the group's own runs actually reported, whatever it's called -
            # no Ar-suite-specific list here, so a He-suite gas (or anything else)
            # shows up automatically the moment a group with it needs review.
            gas_set.update(k for k in matrix if k.startswith(db.GROUP_PEAK_FIELD_PREFIX))
        self._selections = selections
        self._gas_columns = sorted(gas_set)
        # Rebuilding the whole ttk.Treeview (and its scrollbars) is only actually
        # needed when the set of gas columns changed - otherwise it's a needless
        # destroy/recreate of the entire widget on every refresh (every tab switch,
        # every merge), which is exactly what read as "weird"/flashing on load.
        # Same column set -> reuse the existing tree, just re-render onto it.
        if self.tree is None or self._gas_columns != self._tree_gas_columns:
            self._rebuild_tree_columns()
            self._tree_gas_columns = list(self._gas_columns)
        self._resort_and_render()
        self.status_label.config(
            text=f"{len(self._groups)} group(s) pending" if self._groups else "Nothing pending."
        )

    def _rebuild_tree_columns(self):
        if self.tree is not None:
            self.tree.destroy()
            self.tree_vsb.destroy()
            self.tree_hsb.destroy()
        columns = (
            ("injection_date", "sample_name", "sep", "ingested_at")
            + FIXED_COLUMNS + tuple(self._gas_columns)
        )
        self.tree = ttk.Treeview(self.tree_frame, columns=columns, show="headings", selectmode="none")
        self.tree.heading(
            "injection_date", text="Sample date", command=lambda: self._on_sort_click("injection_date")
        )
        self.tree.column("injection_date", width=130, anchor="w")
        self.tree.heading("sample_name", text="Sample", command=lambda: self._on_sort_click("sample_name"))
        self.tree.column("sample_name", width=170, anchor="w")
        # A narrow, unlabeled, unsortable column whose cells just show a vertical
        # bar character - ttk.Treeview has no per-column border/rule, so this fakes
        # one, marking off "the sample columns" from everything that's part of a
        # per-run merge pick.
        self.tree.heading("sep", text="")
        self.tree.column("sep", width=10, minwidth=10, stretch=False, anchor="center")
        self.tree.heading("ingested_at", text="Date added to DB", command=lambda: self._on_sort_click("ingested_at"))
        self.tree.column("ingested_at", width=140, anchor="w")
        for col in FIXED_COLUMNS:
            self.tree.heading(col, text=FIXED_LABELS[col], command=lambda c=col: self._on_sort_click(c))
            # run_number is wider than the others - its divider-row cell hosts the
            # per-group "Assign from name" checkbox, which needs real room.
            self.tree.column(col, width=150 if col == "run_number" else 100, anchor="w")
        for col in self._gas_columns:
            gas_name = col[len(db.GROUP_PEAK_FIELD_PREFIX):]
            self.tree.heading(col, text=gas_name, command=lambda c=col: self._on_sort_click(c))
            self.tree.column(col, width=90, anchor="w")
        self.tree.tag_configure("divider", background="#e4e4e4")
        self.tree.tag_configure("row-selected", background=ROW_SELECTED_BG)

        self.tree_vsb = ttk.Scrollbar(self.tree_frame, orient="vertical", command=self._on_vscroll)
        self.tree_hsb = ttk.Scrollbar(self.tree_frame, orient="horizontal", command=self._on_hscroll)
        self.tree.configure(yscrollcommand=self.tree_vsb.set, xscrollcommand=self.tree_hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree_vsb.grid(row=0, column=1, sticky="ns")
        self.tree_hsb.grid(row=1, column=0, sticky="ew")
        bind_fast_hscroll(self.tree)
        self.tree.bind("<ButtonPress-1>", self._on_tree_press)
        self.tree.bind("<B1-Motion>", self._on_tree_drag)
        # bbox() (used to place the blue selection overlays) comes back empty until
        # the tree is actually mapped on screen - true the first time this panel is
        # built, since it's a sub-tab of the Load Data tab and the app doesn't open
        # on that tab. <Map>/<Configure> cover "becomes visible"/"resized or
        # scrolled"; the mouse wheel ones matter because a wheel event over the
        # tree's own background (not one of our overlay labels) still needs a
        # reposition pass afterward.
        self.tree.bind("<Map>", lambda _e: self._update_selection_overlays(), add="+")
        self.tree.bind("<Configure>", lambda _e: self._update_selection_overlays(), add="+")
        self.tree.bind("<MouseWheel>", lambda _e: self.after(1, self._update_selection_overlays), add="+")
        self.tree.bind(
            "<Shift-MouseWheel>", lambda _e: self.after(1, self._update_selection_overlays), add="+"
        )

    def _on_vscroll(self, *args):
        self.tree.yview(*args)
        self._update_selection_overlays()

    def _on_hscroll(self, *args):
        self.tree.xview(*args)
        self._update_selection_overlays()

    # -- sorting: Sample/Sample date reorder the whole table; every other column
    # reorders each group's own rows independently -------------------------------
    def _on_sort_click(self, col):
        if col == "sep":
            return
        if col in WHOLE_TABLE_SORT_COLUMNS:
            if self._group_sort_column == col:
                self._group_sort_reverse = not self._group_sort_reverse
            else:
                self._group_sort_column = col
                self._group_sort_reverse = False
        else:
            if self._row_sort_column == col:
                self._row_sort_reverse = not self._row_sort_reverse
            else:
                self._row_sort_column = col
                self._row_sort_reverse = False
        self._resort_and_render()

    def _heading_text(self, col, label):
        if col in WHOLE_TABLE_SORT_COLUMNS:
            if col == self._group_sort_column:
                return label + (" ▼" if self._group_sort_reverse else " ▲")
            return label
        if col == self._row_sort_column:
            return label + (" ▼" if self._row_sort_reverse else " ▲")
        return label

    def _group_sort_key(self, group_key):
        col = self._group_sort_column
        if col == "sample_name":
            return (group_key[0] or "").lower()
        return group_key[1] or ""

    def _row_sort_key(self, group_key, run_id):
        col = self._row_sort_column
        if col == "ingested_at":
            raw = self._ingested_at.get(run_id)
            return (raw is None, raw or "")
        matrix = self._matrices.get(group_key, {})
        raw = matrix.get(col, {}).get(run_id)
        if raw is None:
            return (True, 0)
        if col == "pressure" or col == "run_number":
            return (False, raw)
        if col == "standard_id":
            return (False, (self._standard_names.get(raw) or "").lower())
        if col == "round_id":
            return (False, (self._round_names.get(raw) or "").lower())
        if col == "notes":
            return (False, (raw or "").lower())
        if col.startswith(db.GROUP_PEAK_FIELD_PREFIX):
            area = raw["area"]
            return (area is None, area if area is not None else 0.0)
        return (False, str(raw))

    def _sorted_group_run_ids(self, group_key):
        run_ids = self._groups.get(group_key, [])
        if self._row_sort_column is None:
            return run_ids
        return sorted(run_ids, key=lambda rid: self._row_sort_key(group_key, rid), reverse=self._row_sort_reverse)

    def _resort_and_render(self):
        self._group_order = sorted(
            self._groups.keys(), key=self._group_sort_key, reverse=self._group_sort_reverse
        )
        self.tree.heading("injection_date", text=self._heading_text("injection_date", "Sample date"))
        self.tree.heading("sample_name", text=self._heading_text("sample_name", "Sample"))
        self.tree.heading("ingested_at", text=self._heading_text("ingested_at", "Date added to DB"))
        for col in FIXED_COLUMNS:
            self.tree.heading(col, text=self._heading_text(col, FIXED_LABELS[col]))
        for col in self._gas_columns:
            self.tree.heading(col, text=self._heading_text(col, col[len(db.GROUP_PEAK_FIELD_PREFIX):]))
        self._render()

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self._row_group = {}
        gas_blank_count = len(self._gas_columns)
        for i, group_key in enumerate(self._group_order):
            run_ids = self._sorted_group_run_ids(group_key)
            sample_name, injection_date = group_key
            divider_iid = f"divider-{i}"
            self.tree.insert(
                "", "end", iid=divider_iid,
                values=(
                    (injection_date or "")[:19], f"{sample_name} - {len(run_ids)} version(s)", "│", ""
                ) + ("",) * (len(FIXED_COLUMNS) + gas_blank_count),
                tags=("divider",),
            )
            self._row_group[divider_iid] = group_key

            matrix = self._matrices[group_key]
            for run_id in run_ids:
                row_iid = f"row-{run_id}"
                values = [(injection_date or "")[:19], sample_name, "│", (self._ingested_at.get(run_id) or "")[:19]]
                for col in FIXED_COLUMNS + tuple(self._gas_columns):
                    values.append(self._format_cell(matrix, col, run_id))
                tags = ("row-selected",) if row_iid in self._selected_rows else ()
                self.tree.insert("", "end", iid=row_iid, values=tuple(values), tags=tags)
                self._row_group[row_iid] = group_key
        self._update_selection_overlays()
        # bbox() (used above) comes back empty until the tree is actually mapped -
        # true whenever this render happens on a hidden tab (e.g. the very first
        # one, since the app doesn't open on the Load Data tab). One more pass a
        # tick later, once Tk has actually processed the mapping/geometry, is what
        # makes the overlays reliably show up rather than silently staying absent
        # until some unrelated scroll/resize happens to trigger one.
        self.after(1, self._update_selection_overlays)

    def _format_cell(self, matrix, field, run_id):
        raw = matrix.get(field, {}).get(run_id)
        return self._display_value(field, raw)

    def _display_value(self, field, raw):
        if raw is None:
            return ""
        if field == "pressure":
            return f"{raw:g}"
        if field == "standard_id":
            return self._standard_names.get(raw, "")
        if field == "round_id":
            return self._round_names.get(raw, "")
        if field.startswith(db.GROUP_PEAK_FIELD_PREFIX):
            area = raw["area"]
            return "" if area is None else f"{area:g}"
        return str(raw)

    # -- Excel-style single light-blue cell highlight per column per group -------
    def _update_selection_overlays(self):
        # update_idletasks() below can synchronously deliver a queued <Configure>
        # event on the tree (first-time mapping/geometry settling is exactly when
        # that happens) - and <Configure> is bound to this same method, so without
        # this guard that inner call finishes, then the outer call's own
        # now-stale loop resumes and clobbers the inner call's correct result with
        # whatever it had already computed (often nothing, if the outer call
        # started before the tree was actually mapped).
        if getattr(self, "_updating_overlays", False):
            return
        self._updating_overlays = True
        try:
            self._do_update_selection_overlays()
        finally:
            self._updating_overlays = False
        self._update_run_number_checkboxes()

    def _do_update_selection_overlays(self):
        for label in self._selection_overlays.values():
            label.destroy()
        self._selection_overlays = {}
        if self.tree is None:
            return
        self.tree.update_idletasks()
        tree_columns = self.tree["columns"]
        for group_key, selections in self._selections.items():
            for field, run_id in selections.items():
                if field not in tree_columns:
                    # Part of the mergeable field set (e.g. highlight_swatch_id) but
                    # not surfaced as its own column here - nothing to draw.
                    continue
                if field == "run_number" and group_key in self._auto_run_number_groups:
                    # Suppressed while auto-assign is active - there's no longer a
                    # "picked run" for this column, the value comes from the sample
                    # name instead (see the checkbox overlay).
                    continue
                if run_id is None:
                    self._place_null_overlay(group_key, field)
                else:
                    self._place_overlay(group_key, field, run_id)

    def _group_field_disagrees(self, group_key, field):
        """True if 2+ runs in this group have a non-null value for `field` and
        those values aren't all the same - a genuine disagreement worth flagging,
        as opposed to one version simply not having a value at all. Memoized per
        refresh() - the underlying data can't change without one, but this gets
        called again for the same (group, field) on every scroll/resize repaint,
        and re-walking the group's members each time is pure waste."""
        cache_key = (group_key, field)
        cached = self._disagreement_cache.get(cache_key)
        if cached is not None:
            return cached
        matrix = self._matrices.get(group_key, {})
        by_run = matrix.get(field, {})
        values = []
        for run_id in self._groups.get(group_key, []):
            raw = by_run.get(run_id)
            if raw is None:
                continue
            value = raw["area"] if field.startswith(db.GROUP_PEAK_FIELD_PREFIX) else raw
            if value is not None:
                values.append(value)
        result = len(set(values)) > 1
        self._disagreement_cache[cache_key] = result
        return result

    def _place_overlay(self, group_key, field, run_id):
        matrix = self._matrices.get(group_key, {})
        if matrix.get(field, {}).get(run_id) is None:
            return
        row_iid = f"row-{run_id}"
        if not self.tree.exists(row_iid):
            return
        bbox = self.tree.bbox(row_iid, field)
        if not bbox:
            return  # scrolled out of view - nothing to draw
        x, y, w, h = bbox
        text = self._display_value(field, matrix.get(field, {}).get(run_id))
        disagrees = self._group_field_disagrees(group_key, field)
        label = tk.Label(
            self.tree, text=text, bg=CELL_SELECTED_BG, fg=CELL_SELECTED_FG, anchor="w", padx=2,
            highlightthickness=2 if disagrees else 0,
            highlightbackground=DISAGREEMENT_OUTLINE, highlightcolor=DISAGREEMENT_OUTLINE,
        )
        label.place(x=x, y=y, width=w, height=h)
        if disagrees:
            bind_tooltip(
                label,
                f"This group's other version(s) have a different value for this field - "
                f"\"{text}\" is only one of them.",
            )
        # A click or wheel event over an overlay lands on the overlay Label, not the
        # tree beneath it. Wheel needs forwarding so scrolling doesn't silently
        # "stick" over a selected cell; the click needs forwarding so clicking an
        # *already*-picked cell to deselect it (see _pick_cell_value) actually does
        # anything at all - without this the label just absorbs the click and the
        # toggle-to-null gesture never reaches the handler underneath it.
        label.bind("<Button-1>", lambda _e, gk=group_key, f=field, rid=run_id: self._on_overlay_click(gk, f, rid))
        label.bind("<MouseWheel>", self._on_overlay_wheel)
        label.bind("<Shift-MouseWheel>", self._on_overlay_shift_wheel)
        self._selection_overlays[(group_key, field)] = label

    def _on_overlay_click(self, group_key, field, run_id):
        """A click on an already-selected cell's overlay - always a deselect
        (there's nothing else it could mean, since clicking any *other* run's own
        cell in this column reaches the tree directly and picks that one instead)."""
        if field == "run_number" and group_key in self._auto_run_number_groups:
            return
        if self._selections.get(group_key, {}).get(field) != run_id:
            return  # stale overlay mid-refresh - ignore
        self._selections.setdefault(group_key, {})[field] = None
        self._update_single_overlay(group_key, field)

    def _place_null_overlay(self, group_key, field):
        """Anchored on the group's own divider row, not any one version's cell -
        an explicit "exclude this field" choice isn't about any particular run's
        value, it's a property of the group as a whole, same as the divider itself.
        Drawn in the divider's own grey so it reads as part of that row rather than
        a separate floating box sitting on top of it."""
        if group_key not in self._group_order:
            return
        divider_iid = f"divider-{self._group_order.index(group_key)}"
        if not self.tree.exists(divider_iid):
            return
        bbox = self.tree.bbox(divider_iid, field)
        if not bbox:
            return
        x, y, w, h = bbox
        label = tk.Label(
            self.tree, text=NULL_SELECTION_TEXT, bg=NULL_SELECTION_BG, fg=NULL_SELECTION_FG,
            anchor="w", padx=2,
        )
        label.place(x=x, y=y, width=w, height=h)
        bind_tooltip(
            label,
            "This field is deliberately left out of the merged run - click any version's own value in this "
            "column to use it instead.",
        )
        label.bind("<MouseWheel>", self._on_overlay_wheel)
        label.bind("<Shift-MouseWheel>", self._on_overlay_shift_wheel)
        self._selection_overlays[(group_key, field)] = label

    def _update_single_overlay(self, group_key, field):
        """Redraws exactly one (group, field) overlay instead of the whole table's
        worth - clicking a cell only ever changes one selection, and rebuilding
        every overlay on every click is the "lag" a full table's worth of them
        produces for no reason."""
        old = self._selection_overlays.pop((group_key, field), None)
        if old is not None:
            old.destroy()
        if field == "run_number" and group_key in self._auto_run_number_groups:
            return
        group_selections = self._selections.get(group_key, {})
        if field not in group_selections:
            return
        run_id = group_selections[field]
        if run_id is None:
            self._place_null_overlay(group_key, field)
        else:
            self._place_overlay(group_key, field, run_id)

    # -- "Assign run number from name" - a per-group toggle in the table, plus one
    # global toggle living outside it in the panel's own layout -------------------
    def _update_run_number_checkboxes(self):
        """One small checkbox per group, on its divider row in the Run # column -
        the per-group control. The global one above the column (a real widget in
        the panel, not an overlay - see _build) just batches these, and doesn't
        need rebuilding on every render the way these do."""
        for cb in self._run_number_checkboxes.values():
            cb.destroy()
        self._run_number_checkboxes = {}
        if self.tree is not None:
            self.tree.update_idletasks()
            for i, group_key in enumerate(self._group_order):
                divider_iid = f"divider-{i}"
                if not self.tree.exists(divider_iid):
                    continue
                bbox = self.tree.bbox(divider_iid, "run_number")
                if not bbox:
                    continue
                x, y, w, h = bbox
                match = db.RUN_NUMBER_SAMPLE_ID_RE.search(group_key[0] or "")
                var = tk.BooleanVar(value=group_key in self._auto_run_number_groups)
                cb = tk.Checkbutton(
                    self.tree, text=" Assign from name", variable=var, anchor="w",
                    bg=DIVIDER_BG, activebackground=DIVIDER_BG, font=("", 8), padx=0,
                    command=lambda gk=group_key, v=var: self._on_toggle_auto_run_number(gk, v),
                )
                if match is None:
                    cb.configure(state="disabled")
                    var.set(False)
                    self._auto_run_number_groups.discard(group_key)
                    bind_tooltip(
                        cb,
                        "This sample name has no trailing run number to parse, so it can't be assigned "
                        "automatically here - assign this run's number manually in the Pressure Entry "
                        "wizard after saving.",
                    )
                else:
                    bind_tooltip(
                        cb,
                        f"When checked, assigns run number {int(match.group(1))} automatically (parsed "
                        f"from \"{group_key[0]}\") instead of picking one from an existing version.",
                    )
                cb.place(x=x, y=y, width=w, height=h)
                self._run_number_checkboxes[group_key] = cb
        self._sync_run_number_toggle()

    def _on_toggle_auto_run_number(self, group_key, var):
        if var.get():
            self._auto_run_number_groups.add(group_key)
        else:
            self._auto_run_number_groups.discard(group_key)
        self._update_single_overlay(group_key, "run_number")
        self._sync_run_number_toggle()

    def _sync_run_number_toggle(self):
        """Keeps the global checkbox (a persistent widget, not rebuilt per-render)
        reflecting the current group states - checked only once every eligible
        group is on, disabled entirely if no group here is eligible at all."""
        if self._run_number_header_checkbox is None:
            return
        assignable = [
            gk for gk in self._groups if db.RUN_NUMBER_SAMPLE_ID_RE.search(gk[0] or "") is not None
        ]
        all_on = bool(assignable) and all(gk in self._auto_run_number_groups for gk in assignable)
        self._run_number_toggle_var.set(all_on)
        self._run_number_header_checkbox.configure(state="normal" if assignable else "disabled")

    def _on_toggle_all_auto_run_number(self):
        turn_on = self._run_number_toggle_var.get()
        for group_key in list(self._groups.keys()):
            if db.RUN_NUMBER_SAMPLE_ID_RE.search(group_key[0] or "") is None:
                continue  # can't be auto-assigned - left alone regardless of the bulk toggle
            if turn_on:
                self._auto_run_number_groups.add(group_key)
            else:
                self._auto_run_number_groups.discard(group_key)
        for group_key in list(self._groups.keys()):
            self._update_single_overlay(group_key, "run_number")
        self._update_run_number_checkboxes()

    def _on_overlay_wheel(self, event):
        self.tree.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._update_selection_overlays()
        return "break"

    def _on_overlay_shift_wheel(self, event):
        self.tree.xview_scroll(int(-1 * (event.delta / 120)), "units")
        self._update_selection_overlays()
        return "break"

    # -- click dispatch: Sample column selects/paints a single row, everything else
    # (except the inert date/separator columns) picks that cell's value ----------
    def _on_tree_press(self, event):
        if self.tree.identify_region(event.x, event.y) == "heading":
            return  # handled by the heading's own sort command
        row_iid = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if not row_iid or not col_id:
            self._drag_active = False
            return
        columns = self.tree["columns"]
        col_index = int(col_id[1:]) - 1
        if col_index < 0 or col_index >= len(columns):
            return
        col_name = columns[col_index]
        group_key = self._row_group.get(row_iid)
        if group_key is None:
            return

        if col_name == GROUP_SELECT_COLUMN:
            self._handle_row_select_press(event, row_iid)
        elif col_name in INERT_COLUMNS:
            self._drag_active = False
        else:
            self._drag_active = False
            self._pick_cell_value(row_iid, col_name, group_key)

    def _on_tree_drag(self, event):
        if not self._drag_active or self._row_click_anchor is None:
            return
        row_iid = self.tree.identify_row(event.y)
        if not row_iid:
            return
        order = self._data_row_order()
        if row_iid not in order or self._row_click_anchor not in order:
            return
        i, j = order.index(self._row_click_anchor), order.index(row_iid)
        lo, hi = min(i, j), max(i, j)
        in_range = set(order[lo:hi + 1])
        for iid in order:
            desired = self._drag_target_state if iid in in_range else self._drag_snapshot.get(iid, False)
            if desired != (iid in self._selected_rows):
                self._set_row_selected(iid, desired)

    # -- row selection (the grey bar), Sample column only - a single row, never the
    # divider header above its group ----------------------------------------------
    def _data_row_order(self):
        return [iid for iid in self.tree.get_children("") if iid.startswith("row-")]

    def _handle_row_select_press(self, event, row_iid):
        if not row_iid.startswith("row-"):
            return  # the divider header - never selectable
        order = self._data_row_order()
        if row_iid not in order:
            return
        shift = bool(event.state & SHIFT_MASK)
        ctrl = bool(event.state & CTRL_MASK)
        if shift and ctrl and self._row_click_anchor is not None:
            self._toggle_row_range(self._row_click_anchor, row_iid, order)
            self._drag_active = False
        elif shift and self._row_click_anchor is not None:
            self._select_row_range(self._row_click_anchor, row_iid, order)
            self._drag_active = False
        else:
            was_selected = row_iid in self._selected_rows
            self._drag_snapshot = {iid: (iid in self._selected_rows) for iid in order}
            self._set_row_selected(row_iid, not was_selected)
            self._row_click_anchor = row_iid
            self._drag_target_state = not was_selected
            self._drag_active = True

    def _select_row_range(self, anchor_iid, target_iid, order):
        if anchor_iid not in order or target_iid not in order:
            return
        i, j = order.index(anchor_iid), order.index(target_iid)
        lo, hi = min(i, j), max(i, j)
        for iid in order[lo:hi + 1]:
            self._set_row_selected(iid, True)

    def _toggle_row_range(self, anchor_iid, target_iid, order):
        if anchor_iid not in order or target_iid not in order:
            return
        desired = target_iid not in self._selected_rows
        i, j = order.index(anchor_iid), order.index(target_iid)
        lo, hi = min(i, j), max(i, j)
        for iid in order[lo:hi + 1]:
            self._set_row_selected(iid, desired)

    def _set_row_selected(self, row_iid, selected):
        if selected:
            self._selected_rows.add(row_iid)
        else:
            self._selected_rows.discard(row_iid)
        if not self.tree.exists(row_iid):
            return
        base = [t for t in self.tree.item(row_iid, "tags") if t != "row-selected"]
        if selected:
            base.append("row-selected")
        self.tree.item(row_iid, tags=tuple(base))
        if selected:
            self._adopt_row_values(row_iid)

    def _adopt_row_values(self, row_iid):
        """Checking a row in the Sample column is already a strong, deliberate
        signal - "this whole version is the one" - so it also sets every one of
        that run's own field picks at once (the blue boxes), instead of making the
        user click each one by hand to match. Only touches fields this run
        actually has a value for, and overrides whatever was picked (or excluded)
        there before, same as clicking each cell individually would."""
        run_id = int(row_iid.split("-", 1)[1])
        group_key = self._row_group.get(row_iid)
        if group_key is None:
            return
        matrix = self._matrices.get(group_key, {})
        changed_fields = []
        for field in FIXED_COLUMNS + tuple(self._gas_columns):
            if field == "run_number" and group_key in self._auto_run_number_groups:
                continue  # derived from the sample name instead - not this run's pick
            if matrix.get(field, {}).get(run_id) is None:
                continue  # this run has nothing here - leave whatever's picked alone
            if self._selections.get(group_key, {}).get(field) == run_id:
                continue
            self._selections.setdefault(group_key, {})[field] = run_id
            changed_fields.append(field)
        for field in changed_fields:
            self._update_single_overlay(group_key, field)

    # -- picking a column's source run for a group --------------------------------
    def _pick_cell_value(self, row_iid, col_name, group_key):
        if col_name == "run_number" and group_key in self._auto_run_number_groups:
            return  # the number comes from the sample name while auto-assign is on
        run_id = int(row_iid.split("-", 1)[1])
        matrix = self._matrices.get(group_key, {})
        if matrix.get(col_name, {}).get(run_id) is None:
            return  # nothing to pick - this run has no value for this field
        current = self._selections.get(group_key, {}).get(col_name)
        if current == run_id:
            # Clicking the already-picked cell again deselects it, marking the
            # field "excluded" - e.g. a peak every version shares but that's
            # actually a mis-detection, which shouldn't survive into the merge at
            # all rather than being forced to pick one wrong value over another.
            self._selections.setdefault(group_key, {})[col_name] = None
        else:
            self._selections.setdefault(group_key, {})[col_name] = run_id
        self._update_single_overlay(group_key, col_name)

    # -- merging ------------------------------------------------------------------
    def _merge_group(self, group_key):
        run_ids = self._groups.get(group_key)
        if not run_ids:
            return
        target_run_id = run_ids[0]
        selections = self._selections.get(group_key, {})
        db.merge_same_identity_group(self.conn, target_run_id, run_ids, selections)
        if group_key in self._auto_run_number_groups:
            match = db.RUN_NUMBER_SAMPLE_ID_RE.search(group_key[0] or "")
            if match is not None:
                # A direct override, not routed through set_run_number_manual (which
                # requires an existing round assignment this run may not have yet at
                # this stage) - whatever real numbering pass runs later (recompute_*
                # on finalize, or a round's own renumbering) can still supersede it,
                # same as any other manually-typed run number can.
                self.conn.execute(
                    "UPDATE runs SET run_number = ? WHERE run_id = ?",
                    (int(match.group(1)), target_run_id),
                )
                self.conn.commit()
        self._selections.pop(group_key, None)
        self._auto_run_number_groups.discard(group_key)
        for rid in run_ids:
            self._selected_rows.discard(f"row-{rid}")

    def _after_merge(self, attempted_group_keys):
        self.refresh()
        self.app.refresh()
        self.app.notify_data_changed()
        self.app.update_pressure_button()
        self.app.update_load_pending_indicator()
        # Confirm against the DB, not just "the code ran" - re-check that none of the
        # groups we just tried to merge are still pending after refresh() reloads
        # from the database. This is what actually tells the user the merge stuck,
        # rather than assuming success just because no exception was raised.
        still_pending = sum(1 for gk in attempted_group_keys if gk in self._groups)
        merged_count = len(attempted_group_keys) - still_pending
        if still_pending:
            messagebox.showwarning(
                "Merge incomplete",
                f"Merged {merged_count} of {len(attempted_group_keys)} group(s) - "
                f"{still_pending} did not merge and are still pending. Check them and try again.",
            )
        else:
            noun = "group" if merged_count == 1 else "groups"
            messagebox.showinfo("Merged", f"Merged {merged_count} {noun} successfully.")
        # "Merge selection" is the only action left in this tab now (replacing the
        # old separate "Save selection, and continue" button) - once a merge leaves
        # nothing pending, that *is* "done here", so move on to pressure entry the
        # same way that button used to, without a second click.
        if self._on_next is not None and not self._groups:
            self._on_next()

    def on_merge_selection(self):
        """What actually gets merged: whichever groups have rows checked via the
        Sample column (the grey bar), if any - a deliberate "just these" scope. But
        the picks a user makes by clicking cells (the blue boxes) are themselves
        already a real, complete selection for every group shown - every field in
        every group always has a value chosen, whether that's still the default or
        something overridden/excluded - so requiring the *separate* grey-row
        gesture on top of that, and erroring out when it's skipped, was a second
        selection concept nobody asked for. With nothing grey-checked, this merges
        everything visible instead, using exactly the picks currently shown."""
        if not self._groups:
            messagebox.showinfo("Nothing to merge", "No same-identity groups are currently pending.")
            return
        if self._selected_rows:
            group_keys = {self._row_group[iid] for iid in self._selected_rows if iid in self._row_group}
            if not group_keys:
                return
        else:
            group_keys = set(self._groups.keys())
            if not messagebox.askyesno(
                "Merge all pending groups",
                f"No rows are checked in the Sample column, so this merges all {len(group_keys)} pending "
                "group(s) using the selections currently shown (defaults, plus any cell you've picked or "
                "excluded). Continue?",
            ):
                return
        for group_key in group_keys:
            self._merge_group(group_key)
        self._after_merge(group_keys)


class DuplicatesWizardWindow(tk.Toplevel):
    """Standalone popup entry point - just a plain window hosting one
    DuplicatesWizardPanel."""

    def __init__(self, app):
        super().__init__(app)
        self.title("Duplicates wizard")
        self.geometry("1180x640")
        self.minsize(900, 440)
        self.panel = DuplicatesWizardPanel(self, app)
        self.panel.pack(fill="both", expand=True)
