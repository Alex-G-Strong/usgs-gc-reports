"""A window for entering known standard pressures against runs, with clipboard-paste
support for bulk entry from a lab book, sortable/draggable rows to align the on-screen
order with however the physical book is arranged, and undo for edits."""

import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont

from gc_pipeline import db
from gc_pipeline.widgets import DropdownChecklist, bind_fast_hscroll, justify_columns
from gc_pipeline.run_rounds_dialog import RoundAssignDialog

COLUMNS = ("handle", "run_number", "injection_date", "sample_name", "highlight", "pressure", "standard", "notes",
           "round")
DRAG_HANDLE_GLYPH = "⋮"  # vertical ellipsis - a little three-dot drag handle
HANDLE_COLUMN_ID = "#1"
RUN_NUMBER_COLUMN_ID = "#2"
NONE_STANDARD_LABEL = "(none)"
NO_ROUND_LABEL = "(no round)"
ADD_NEW_STANDARD_PREFIX = "+ Add new standard: "
TAB_CYCLE = ("pressure", "standard", "notes")
MAX_MATCH_ROWS = 8  # how many candidates the floating standard-match list shows at once
# Typing any of these into the pressure cell marks it "deliberately not recorded"
# (e.g. the scientist forgot to take a reading) instead of trying to parse a number -
# stops the missing-pressure nag without faking a 0 or leaving it looking "not yet done".
NOT_RECORDED_TOKENS = {"NR", "N/A", "NA", "UNKNOWN"}
# Typing this into the pressure cell explicitly clears it - pressure AND the
# "not recorded" flag both go back to unset, unlike just deleting the text (which
# leaves any existing "not recorded" flag alone, since blank could just mean "I
# haven't touched this field yet" rather than "actually erase what's there").
NULL_TOKENS = {"NULL"}


def _fuzzy_match_score(candidate, typed):
    """None if typed's characters don't all appear in candidate in the same order
    (a subsequence match, e.g. "s8" matching "std-8"); otherwise a score where lower
    is a better match - exact equality, then prefix, then plain substring, then a
    sparser in-order subsequence. Both arguments are expected already lowercased."""
    if candidate == typed:
        return 0
    if candidate.startswith(typed):
        return 1
    if typed in candidate:
        return 2
    idx = 0
    for ch in typed:
        idx = candidate.find(ch, idx)
        if idx == -1:
            return None
        idx += 1
    return 3


class PressureEntryPanel(ttk.Frame):
    """The wizard's actual content - a plain ttk.Frame so it can be embedded either
    in the standalone popup window (PressureEntryWindow, below) or directly as a
    sub-tab of the Load Data tab."""

    def __init__(self, parent, app, run_ids=None, missing_only=False, on_close_callback=None,
                 auto_close_on_save=False, show_save_button=True, preselected_run_ids=None):
        super().__init__(parent)
        self.app = app
        self.conn = app.conn
        self.run_ids = run_ids
        # Always shown regardless of the Round filter's check state - the Selector
        # tab's "Enter pressures..." button uses this to open scoped to exactly
        # whatever was checked there, while still letting the Round filter (unchecked
        # by default in that case - see _build) widen the view to other unentered
        # runs on request, rather than hard-restricting to just this set forever.
        self._always_show_run_ids = set(preselected_run_ids or [])
        self.on_close_callback = on_close_callback
        # Off when this panel is embedded somewhere that already has its own Save
        # action driving the same on_save() (the Load Data tab's Pressure entry
        # sub-tab, which also finalizes/numbers runs as part of its own Save) - two
        # Save buttons doing overlapping things in the same screen was confusing,
        # so only the embedding page's button remains in that case.
        self.show_save_button = show_save_button
        # Set for the "you just loaded runs that need this before they can proceed"
        # flow (e.g. Models tab's load_runs) - Save both saves and dismisses the
        # window in one step there, instead of leaving it open for a second manual
        # close. Windows opened for general/manual editing (the toolbar's "Enter
        # pressures..."/"Edit selected pressures" buttons) leave this off so Save
        # behaves like a normal, non-dismissing save.
        self.auto_close_on_save = auto_close_on_save
        self.pending = {}      # run_id -> str value not yet saved
        self._undo_stack = []  # list of list[(run_id, old_value)]
        # run_ids actually edited (pressure, standard, or a pending value later
        # saved) during this window's lifetime - keeps the end-of-session validation
        # warning scoped to what just happened here, not every unrelated gap
        # anywhere in the database.
        self._touched_run_ids = set()
        self._drag_row_iid = None
        self._active_editor = None       # currently-open Entry/Combobox placed over a cell
        self._active_editor_close = None  # callable that commits + destroys it
        self._suppress_next_global_close = False
        self._composition_popup = None   # the inline standard-composition mini-editor, if open
        self._composition_popup_done = None  # callback to run once that popup closes

        self._sort_column = "run_number"
        self._sort_reverse = False
        self._round_names = {}  # round_id -> name, refreshed each _load_rows()
        self._highlight_overlays = {}  # run_id -> tk.Label chip, same technique as the Selector tab

        self.show_all_var = tk.BooleanVar(value=not missing_only)

        self._build()
        self._load_rows()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=6)
        self.round_filter = DropdownChecklist(
            top, "Round", self._load_available_rounds(), self._on_round_filter_changed
        )
        self.round_filter.pack(side="left")
        if self._always_show_run_ids:
            # Opened scoped to a specific selection (Selector tab's "Enter
            # pressures..." with something checked) - default to showing just that
            # selection, not every unentered run in every round too. Set directly
            # (not via select_none()/on_change) since self.tree doesn't exist yet.
            for var in self.round_filter.vars.values():
                var.set(False)
            self.round_filter._update_label()
        ttk.Button(top, text="Justify columns", command=self.on_justify_columns).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Assign to round...", command=self.on_assign_to_round).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(top, text="Show already entered runs", variable=self.show_all_var,
                         command=self._on_show_all_changed).pack(side="left", padx=(12, 0))

        # Only shown when every row currently in view belongs to the same round -
        # numbering mode is a per-round setting, so there's no single toggle state
        # to show when rows from multiple rounds (or no round) are mixed together.
        self._current_round_id = None
        self.round_mode_frame = ttk.Frame(top)
        ttk.Label(self.round_mode_frame, text="Numbering:").pack(side="left", padx=(12, 4))
        self.round_mode_var = tk.StringVar(value="time")
        ttk.Radiobutton(
            self.round_mode_frame, text="By time", variable=self.round_mode_var, value="time",
            command=self._on_round_mode_changed,
        ).pack(side="left")
        ttk.Radiobutton(
            self.round_mode_frame, text="By sample ID", variable=self.round_mode_var, value="sample_id",
            command=self._on_round_mode_changed,
        ).pack(side="left")

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self.tree = ttk.Treeview(tree_frame, columns=COLUMNS, show="headings", selectmode="browse")
        self.tree.heading("handle", text="")
        self.tree.column("handle", width=24, anchor="center", stretch=False)
        for col in COLUMNS:
            if col == "handle":
                continue
            if col == "highlight":
                self.tree.heading(col, text="Highlight")
                self.tree.column(col, width=22, stretch=False, anchor="center")
                continue
            self.tree.heading(col, command=lambda c=col: self._on_sort_click(c))
            if col in ("run_number", "injection_date"):
                width, stretch = 70, False
            else:
                width, stretch = 140, True
            self.tree.column(col, width=width, stretch=stretch, anchor="w")
        # ttk.Treeview has no way to draw real per-cell gridlines (that would need a
        # custom-drawn widget) - faint alternating row banding is the closest
        # practical stand-in for "lines to help guide the eye through the table",
        # applied first in each row's tags so edited/sample-marker still win.
        self.tree.tag_configure("band", background="#f4f4f4")
        self.tree.tag_configure("edited", background="#fff2cc")
        # A banded row that's also edited gets its own, slightly darker shade of the
        # same yellow (darkened by the same ratio "band" darkens white) rather than
        # just "edited" flatly overriding the band color - otherwise the alternating
        # stripe rhythm visibly breaks on every edited row, since a plain yellow
        # looks identical whether or not that row was banded.
        self.tree.tag_configure("edited-band", background="#f4e8c3")
        # "edited"/"edited-band" is listed after "sample-marker" wherever both apply
        # (see _load_rows/_apply_standard_row_style), so an edited Sample-marked row
        # still shows the more actionable yellow highlight rather than being masked
        # by gray.
        self.tree.tag_configure("sample-marker", background="#f0f0f0")
        self._autosize_standard_column()

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        bind_fast_hscroll(self.tree)

        if self.show_save_button:
            save_bar = ttk.Frame(self)
            save_bar.pack(fill="x", padx=6, pady=(0, 8))
            ttk.Button(save_bar, text="Save", command=self.on_save).pack(side="right")

        hint = ttk.Label(
            self,
            text=(
                "Click a pressure, standard, or notes cell to edit it - all three autosave "
                "immediately as you commit each value, no separate save step needed. Tab "
                "cycles between the three for the same row (Shift+Tab goes backward); Enter "
                "moves down to the next row in the same column. Ctrl+Z undoes the last edit "
                "(and re-saves the restored value), and Ctrl+V pastes a column of values "
                "starting from the selected row - handy for transcribing a lab book. Type "
                "NR if a pressure was never recorded, or NULL to explicitly clear a "
                "pressure (and any NR flag) back to empty.\n\n"
                "Click a header to sort, or drag a row by its handle to reorder it to match "
                "the lab book. Right-click a row (or select it and press Delete) to remove "
                "its entry entirely.\n\n"
                "Pressure, standard, and notes can each be filled in in any order - the Save "
                "button (and closing this window) just double-checks for mismatches (e.g. a "
                "standard with no pressure) and catches anything typed that didn't parse."
            ),
            foreground="#666666", wraplength=780, justify="left",
        )
        hint.pack(fill="x", padx=6, pady=(0, 6))

        self.tree.bind("<Button-1>", self.on_cell_click, add="+")
        self.tree.bind("<Double-Button-1>", self.on_cell_double_click, add="+")
        self.tree.bind("<Control-v>", self.on_paste)
        self.tree.bind("<Control-z>", self.on_undo)
        self.tree.bind("<Button-3>", self.on_row_right_click)
        self.tree.bind("<Delete>", self.on_delete_key)

        # add="+" so these layer on top of ttk's native row-selection handling
        # (still needed to pick the paste anchor row) rather than replacing it.
        self.tree.bind("<ButtonPress-1>", self.on_row_press, add="+")
        self.tree.bind("<B1-Motion>", self.on_row_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self.on_row_release, add="+")

        # Catch clicks anywhere in this window (scrollbar, hint text, checkbox, a
        # different row/column) so an open pressure/standard editor always closes
        # instead of lingering - a plain <FocusOut> on the editor isn't reliable
        # here since two place()'d widgets can swap focus without either ever
        # losing it to something outside the tree.
        self.bind_all("<Button-1>", self._on_global_click, add="+")

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

    def _update_highlight_overlays(self):
        """Same colored-chip-over-a-cell technique the Selector tab's own
        "highlight" column uses - ttk.Treeview has no native per-cell background."""
        for label in self._highlight_overlays.values():
            label.destroy()
        self._highlight_overlays = {}
        if not hasattr(self, "tree"):
            return
        self.tree.update_idletasks()
        colors = db.get_run_highlight_colors(self.conn)
        if not colors:
            return
        for row_iid in self.tree.get_children(""):
            run_id = int(row_iid)
            color = colors.get(run_id)
            if not color:
                continue
            bbox = self.tree.bbox(row_iid, "highlight")
            if not bbox:
                continue
            x, y, w, h = bbox
            label = tk.Label(self.tree, bg=color, cursor="hand2")
            label.place(x=x, y=y, width=w, height=h)
            label.bind(
                "<Button-1>", lambda e, rid=run_id: self._open_highlight_picker(rid, e.x_root, e.y_root)
            )
            self._highlight_overlays[run_id] = label

    def _open_highlight_picker(self, run_id, x_root, y_root):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="(none)", command=lambda: self._set_run_highlight(run_id, None))
        swatches = db.list_highlight_swatches(self.conn)
        if swatches:
            menu.add_separator()
            for swatch in swatches:
                label = swatch["label"] or swatch["color"]
                menu.add_command(
                    label=label, background=swatch["color"],
                    command=lambda sid=swatch["swatch_id"]: self._set_run_highlight(run_id, sid),
                )
        menu.add_separator()
        menu.add_command(label="Edit swatches...", command=self.app.on_edit_highlight_swatches)
        menu.tk_popup(x_root, y_root)

    def _set_run_highlight(self, run_id, swatch_id):
        db.set_run_highlight(self.conn, run_id, swatch_id)
        self._update_highlight_overlays()
        self.app.notify_data_changed()
        if hasattr(self.app, "analysis_panel"):
            self.app.analysis_panel._update_highlight_overlays()
        if hasattr(self.app, "_update_highlight_overlays"):
            self.app._update_highlight_overlays()

    def on_justify_columns(self):
        justify_columns(self.tree, [c for c in COLUMNS if c != "handle"])

    def _autosize_standard_column(self):
        names = [s["name"] for s in db.list_standards(self.conn)] + [NONE_STANDARD_LABEL, "standard"]
        f = tkfont.nametofont("TkDefaultFont")
        width = max(f.measure(n) for n in names) + 24
        self.tree.column("standard", width=max(140, min(width, 300)))

    def _on_global_click(self, event):
        if event.widget.winfo_toplevel() is not self:
            # Could be a click inside the composition mini-editor, which is its own
            # borderless Toplevel - leave it alone; anything else outside this
            # window closes it, same click-away convention as every editor here.
            if self._composition_popup is not None and event.widget.winfo_toplevel() is self._composition_popup:
                return
            if self._composition_popup is not None:
                self._close_composition_popup()
            return
        if self._suppress_next_global_close:
            # The same double-click that just opened this editor also generates a
            # plain <Button-1> (it lands on the tree cell, not the entry/combo that
            # didn't exist yet at click time) - without this, that click immediately
            # closed the editor it had just opened.
            self._suppress_next_global_close = False
            return
        if self._active_editor is not None and event.widget is self._active_editor:
            return
        self._close_active_editor()

    def _load_available_rounds(self):
        rows = db.list_runs_for_pressure_entry(
            self.conn, run_ids=self.run_ids, missing_only=not self.show_all_var.get()
        )
        round_names = {rr["round_id"]: rr["name"] for rr in db.list_run_rounds(self.conn)}
        return sorted({round_names.get(r["round_id"], NO_ROUND_LABEL) for r in rows})

    def _on_round_filter_changed(self):
        self._load_rows()

    def _on_show_all_changed(self):
        # The scope of runs changed, so which rounds are even available may have
        # changed too - refresh the dropdown's option list before reloading.
        self.round_filter.set_options(self._load_available_rounds())
        self._load_rows()

    def _load_rows(self):
        self.tree.delete(*self.tree.get_children())
        rows = db.list_runs_for_pressure_entry(
            self.conn, run_ids=self.run_ids, missing_only=not self.show_all_var.get()
        )
        self._round_names = {rr["round_id"]: rr["name"] for rr in db.list_run_rounds(self.conn)}
        # Refreshed on every load (not just when "Show already entered" toggles) -
        # a run can gain a round assignment (via "Assign to round..." elsewhere)
        # while this window is already open, and a brand-new round option must
        # default to checked so that run doesn't just silently vanish from view.
        self.round_filter.set_options(self._load_available_rounds())
        allowed_round_names = set(self.round_filter.selected())
        rows = [
            r for r in rows
            if self._round_names.get(r["round_id"], NO_ROUND_LABEL) in allowed_round_names
            or r["run_id"] in self._always_show_run_ids
        ]
        rows = sorted(rows, key=self._sort_key, reverse=self._sort_reverse)
        for i, r in enumerate(rows):
            run_id = r["run_id"]
            if run_id in self.pending:
                pressure = self.pending[run_id]
            elif r["pressure"] is None and r["pressure_not_recorded"]:
                pressure = "NR"
            else:
                pressure = "" if r["pressure"] is None else str(r["pressure"])
            tags = []
            is_band = i % 2 == 1
            if is_band:
                tags.append("band")
            if (r["standard_name"] or "").lower() == db.SAMPLE_STANDARD_NAME.lower():
                tags.append("sample-marker")
            if run_id in self.pending:
                tags.append("edited-band" if is_band else "edited")
            self.tree.insert(
                "", "end", iid=str(run_id),
                # Positionally aligned with COLUMNS = (handle, run_number,
                # injection_date, sample_name, highlight, pressure, standard, notes,
                # round) - "highlight" has no text of its own (it's a colored-chip
                # overlay drawn separately by _update_highlight_overlays), but it
                # still needs its own "" placeholder here, or every column after it
                # silently shifts left by one - which is exactly what was showing
                # pressures under "Highlight" and standards under "Pressure".
                values=(DRAG_HANDLE_GLYPH, r["run_number"] if r["run_number"] is not None else "",
                        r["injection_date"], r["sample_name"], "", pressure, r["standard_name"] or "",
                        r["notes"] or "", self._round_names.get(r["round_id"], "")),
                tags=tags,
            )
        self._update_headings()
        self._update_round_mode_toggle(rows)
        self._update_highlight_overlays()

    def _update_round_mode_toggle(self, rows):
        round_ids = {r["round_id"] for r in rows if r["round_id"] is not None}
        if len(round_ids) != 1:
            self._current_round_id = None
            self.round_mode_frame.pack_forget()
            return
        self._current_round_id = next(iter(round_ids))
        round_row = next(
            (r for r in db.list_run_rounds(self.conn) if r["round_id"] == self._current_round_id), None
        )
        if round_row is None:
            self._current_round_id = None
            self.round_mode_frame.pack_forget()
            return
        self.round_mode_var.set(round_row["numbering_mode"])
        self.round_mode_frame.pack(side="left")

    def on_assign_to_round(self):
        run_ids = [int(iid) for iid in self.tree.get_children("")]
        if not run_ids:
            messagebox.showinfo("Nothing to assign", "There are no runs currently shown to assign.")
            return
        RoundAssignDialog(
            self.app, run_ids, preselect_round_id=self._current_round_id, on_done=self._load_rows
        )

    def _on_round_mode_changed(self):
        if self._current_round_id is None:
            return
        db.set_round_numbering_mode(self.conn, self._current_round_id, self.round_mode_var.get())
        self._load_rows()
        self.app.refresh()  # renumbers every run in the round - same as the manual edit above

    def _sort_key(self, row):
        col = self._sort_column
        if col == "run_number":
            return (row["run_number"] is None, row["run_number"] or 0)
        if col == "pressure":
            return (row["pressure"] is None, row["pressure"] or 0.0)
        if col == "standard":
            return row["standard_name"] or ""
        if col == "notes":
            return row["notes"] or ""
        if col == "round":
            name = self._round_names.get(row["round_id"], "")
            return (name == "", name)
        return row[col] or ""

    def _on_sort_click(self, col):
        if self._sort_column == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = col
            self._sort_reverse = False
        # A header click always fully re-derives the order, which also means it
        # intentionally overrides any manual drag-reordering.
        self._load_rows()

    def _update_headings(self):
        for col in COLUMNS:
            if col == "handle":
                continue
            label = col
            if col == self._sort_column:
                label += " ▼" if self._sort_reverse else " ▲"
            self.tree.heading(col, text=label)

    # -- drag-to-reorder ---------------------------------------------------
    def on_row_press(self, event):
        # Only the drag-handle column starts a reorder - clicking anywhere else on
        # the row just selects it (e.g. to anchor a paste), as normal.
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != HANDLE_COLUMN_ID:
            return
        self._drag_row_iid = self.tree.identify_row(event.y)

    def on_row_motion(self, event):
        if not self._drag_row_iid:
            return
        target_iid = self.tree.identify_row(event.y)
        if target_iid and target_iid != self._drag_row_iid:
            self.tree.move(self._drag_row_iid, "", self.tree.index(target_iid))

    def on_row_release(self, _event):
        self._drag_row_iid = None

    # -- inline edit -----------------------------------------------------
    def _column_name_at(self, event_x):
        col_id = self.tree.identify_column(event_x)
        try:
            return COLUMNS[int(col_id.lstrip("#")) - 1]
        except (ValueError, IndexError):
            return None

    def on_cell_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        row_iid = self.tree.identify_row(event.y)
        if not row_iid:
            return
        col_name = self._column_name_at(event.x)
        if col_name == "pressure":
            self._start_edit(row_iid)
        elif col_name == "standard":
            self._start_standard_edit(row_iid)
        elif col_name == "notes":
            self._start_notes_edit(row_iid)
        elif col_name == "highlight":
            self._open_highlight_picker(int(row_iid), event.x_root, event.y_root)

    def on_cell_double_click(self, event):
        # run_number is deliberately double-click-only (not part of the single-click
        # convention the other three editable columns use) - it's a rarer, more
        # consequential edit (it cascades every later run in the round), so it
        # shouldn't be one accidental click away.
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        col = self.tree.identify_column(event.x)
        row_iid = self.tree.identify_row(event.y)
        if row_iid and col == RUN_NUMBER_COLUMN_ID:
            self._start_run_number_edit(row_iid)

    def _open_field(self, row_iid, column, suppress_close=False):
        """Used by Tab/Enter navigation to jump straight to the pressure, standard, or
        notes editor of some row without the click-driven suppress-close bookkeeping
        (there's no real mouse click for the global click-away handler to race against
        here)."""
        if not self.tree.exists(row_iid):
            return
        if column == "pressure":
            self._start_edit(row_iid, suppress_close=suppress_close)
        elif column == "standard":
            self._start_standard_edit(row_iid, suppress_close=suppress_close)
        else:
            self._start_notes_edit(row_iid, suppress_close=suppress_close)

    def _advance_row(self, row_iid, column):
        """Enter: move down to the same column in the next row."""
        all_iids = self.tree.get_children("")
        idx = all_iids.index(row_iid)
        if idx + 1 < len(all_iids):
            self._open_field(all_iids[idx + 1], column)

    def _swap_column(self, row_iid, column, reverse=False):
        """Tab / Shift-Tab: cycle pressure -> standard -> notes -> (back to) pressure
        for the same row, Shift-Tab running that cycle in reverse."""
        idx = TAB_CYCLE.index(column)
        nxt = TAB_CYCLE[(idx + (-1 if reverse else 1)) % len(TAB_CYCLE)]
        self._open_field(row_iid, nxt)

    def _start_standard_edit(self, row_iid, suppress_close=True):
        """An OnShape-variable-style selector: type a few characters, a small
        floating list below the cell fuzzy-matches standards whose name contains
        those characters *in order* (not necessarily contiguously - "s8" matches
        "Std-8"), ranked exact > prefix > substring > sparse subsequence, so e.g.
        "8" alone puts "Std-8" right at the top. Up/Down retarget the highlighted
        match without the entry losing focus; Up with nothing typed yet instead
        loads the previous row's standard straight in. Enter commits the
        highlighted/typed choice and advances to the next cell. Typing something
        that doesn't match anything offers a trailing "+ Add new standard: X" row -
        picking it creates the standard on the spot and (unless it's the reserved
        Sample marker) immediately opens a small composition mini-editor "in
        sequence" before advancing."""
        self._close_active_editor()
        self.tree.see(row_iid)
        self.update_idletasks()
        bbox = self.tree.bbox(row_iid, "standard")
        if not bbox:
            return
        x, y, w, h = bbox
        run_id = int(row_iid)
        names = [s["name"] for s in db.list_standards(self.conn)]
        all_options = [NONE_STANDARD_LABEL] + names

        entry_var = tk.StringVar(value=self.tree.set(row_iid, "standard") or NONE_STANDARD_LABEL)
        entry = tk.Entry(self.tree, textvariable=entry_var, relief="solid", borderwidth=1)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.select_range(0, "end")

        listbox = tk.Listbox(self.tree, exportselection=False, activestyle="none", relief="solid", borderwidth=1)
        listbox_shown = {"value": False}

        def compute_matches(typed):
            lowered = typed.lower()
            if lowered:
                scored = []
                for option in all_options:
                    score = _fuzzy_match_score(option.lower(), lowered)
                    if score is not None:
                        scored.append((score, option))
                scored.sort(key=lambda pair: (pair[0], pair[1]))
                matches = [option for _, option in scored]
            else:
                matches = list(all_options)
            exact = any(m.lower() == lowered for m in matches)
            # The "add new" row only shows up when there's real ambiguity (no match,
            # or more than one) - a single unique prefix match is already
            # unambiguous, so offering "create new" alongside it would just be noise
            # (typing further to diverge from that one match brings it back).
            if typed and not exact and len(matches) != 1:
                matches = matches + [f"{ADD_NEW_STANDARD_PREFIX}{typed}"]
            return matches[:MAX_MATCH_ROWS]

        def show_matches(matches):
            listbox.delete(0, "end")
            for m in matches:
                listbox.insert("end", m)
            if matches:
                listbox.selection_clear(0, "end")
                listbox.selection_set(0)
                listbox.activate(0)
                listbox.configure(height=len(matches))
                listbox.place(x=x, y=y + h, width=max(w, 180))
                listbox_shown["value"] = True
            else:
                listbox.place_forget()
                listbox_shown["value"] = False

        def move_selection(delta):
            size = listbox.size()
            if not listbox_shown["value"] or size == 0:
                return
            cur = listbox.curselection()
            idx = max(0, min(size - 1, (cur[0] if cur else 0) + delta))
            listbox.selection_clear(0, "end")
            listbox.selection_set(idx)
            listbox.activate(idx)
            listbox.see(idx)

        def current_choice_text():
            if listbox_shown["value"]:
                sel = listbox.curselection()
                if sel:
                    return listbox.get(sel[0])
            return entry_var.get().strip()

        def load_previous_row_value():
            """Up, pressed before anything's been typed - fills in the previous
            row's standard directly, as a fast "same as the row above" shortcut."""
            all_iids = self.tree.get_children("")
            idx = all_iids.index(row_iid)
            if idx == 0:
                return False
            prev_value = self.tree.set(all_iids[idx - 1], "standard")
            if not prev_value:
                return False
            entry_var.set(prev_value)
            entry.select_range(0, "end")
            entry.icursor("end")
            show_matches(compute_matches(prev_value))
            return True

        def on_keyrelease(event):
            if event.keysym in ("Return", "Tab", "Escape"):
                return
            if event.keysym == "Up":
                current = entry_var.get().strip()
                if current in ("", NONE_STANDARD_LABEL) and load_previous_row_value():
                    return "break"
                move_selection(-1)
                return "break"
            if event.keysym == "Down":
                move_selection(1)
                return "break"
            show_matches(compute_matches(entry_var.get().strip()))

        show_matches(compute_matches(entry_var.get().strip()))

        def commit(on_success=None):
            """Resolves the highlighted/typed choice to a real standard - an exact
            (case-insensitive) match wins, otherwise the sole remaining prefix match;
            anything else is rejected with a warning rather than silently saved, so a
            typo can't quietly clear or mislink a run's standard. `on_success`, if
            given, runs only once the whole thing (including a just-created
            standard's composition popup, if one opens) has finished - the caller
            uses it to advance to the next cell at the right moment rather than
            immediately, since opening the composition popup should defer that."""
            if self._active_editor is not entry:
                return
            raw = current_choice_text()
            new_standard_id = None
            is_new = False
            if raw.startswith(ADD_NEW_STANDARD_PREFIX):
                new_name = raw[len(ADD_NEW_STANDARD_PREFIX):].strip()
                if not new_name:
                    messagebox.showwarning("Standard name required", "Type a name for the new standard.")
                    entry.focus_set()
                    return
                existing = next(
                    (s for s in db.list_standards(self.conn) if s["name"].lower() == new_name.lower()), None
                )
                if existing is not None:
                    choice, new_standard_id = existing["name"], existing["standard_id"]
                else:
                    new_standard_id = db.create_standard(self.conn, new_name)
                    choice = new_name
                    is_new = True
            elif not raw:
                choice = NONE_STANDARD_LABEL
            else:
                lowered = raw.lower()
                exact = next((o for o in all_options if o.lower() == lowered), None)
                if exact:
                    choice = exact
                else:
                    matches = [o for o in all_options if o.lower().startswith(lowered)]
                    if len(matches) == 1:
                        choice = matches[0]
                    else:
                        messagebox.showwarning(
                            "Standard not found",
                            f'"{raw}" doesn\'t match a known standard'
                            + (" - narrow it down further." if len(matches) > 1 else " name."),
                        )
                        entry.focus_set()
                        return

            self._active_editor = None
            self._active_editor_close = None
            entry.destroy()
            listbox.destroy()

            standard_id = new_standard_id
            if standard_id is None and choice != NONE_STANDARD_LABEL:
                match = next((s for s in db.list_standards(self.conn) if s["name"] == choice), None)
                standard_id = match["standard_id"] if match else None

            db.set_run_standard(self.conn, run_id, standard_id)
            self._touched_run_ids.add(run_id)
            self.tree.set(row_iid, "standard", "" if standard_id is None else choice)
            self._apply_standard_row_style(row_iid)
            self.app.notify_data_changed()

            if is_new and choice.lower() != db.SAMPLE_STANDARD_NAME.lower() and standard_id is not None:
                self._open_composition_popup(row_iid, standard_id, choice, on_done=on_success)
            elif on_success is not None:
                on_success()

        def cancel():
            if self._active_editor is not entry:
                return
            self._active_editor = None
            self._active_editor_close = None
            entry.destroy()
            listbox.destroy()

        def on_return(_event=None):
            commit(lambda: self._advance_row(row_iid, "standard"))
            return "break"

        def on_tab(_event=None):
            commit(lambda: self._swap_column(row_iid, "standard", reverse=False))
            return "break"

        def on_shift_tab(_event=None):
            commit(lambda: self._swap_column(row_iid, "standard", reverse=True))
            return "break"

        self._active_editor = entry
        self._active_editor_close = commit
        if suppress_close:
            self._suppress_next_global_close = True
        entry.bind("<KeyRelease>", on_keyrelease)
        entry.bind("<Return>", on_return)
        entry.bind("<Tab>", on_tab)
        entry.bind("<Shift-Tab>", on_shift_tab)
        entry.bind("<Escape>", lambda _e: cancel())
        listbox.bind("<Double-Button-1>", lambda _e: commit(lambda: self._advance_row(row_iid, "standard")))

    def _apply_standard_row_style(self, row_iid):
        if not self.tree.exists(row_iid):
            return
        tags = [t for t in self.tree.item(row_iid, "tags") if t != "sample-marker"]
        standard_name = self.tree.set(row_iid, "standard")
        if (standard_name or "").lower() == db.SAMPLE_STANDARD_NAME.lower():
            tags.insert(0, "sample-marker")
        self.tree.item(row_iid, tags=tuple(tags))

    # -- inline standard-composition mini-editor -------------------------------
    def _open_composition_popup(self, row_iid, standard_id, name, on_done=None):
        self._close_composition_popup()
        self.tree.see(row_iid)
        self.update_idletasks()
        bbox = self.tree.bbox(row_iid, "standard")
        x = self.winfo_rootx() + (bbox[0] if bbox else 0)
        y = self.winfo_rooty() + ((bbox[1] + bbox[3]) if bbox else 0)

        popup = tk.Toplevel(self)
        popup.wm_overrideredirect(True)
        popup.geometry(f"+{x}+{y}")
        self._composition_popup = popup
        self._composition_popup_done = on_done

        border = ttk.Frame(popup, relief="solid", borderwidth=1, padding=8)
        border.pack()
        ttk.Label(border, text=f'"{name}" composition (%):', font=("", 9, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6)
        )

        existing = {row["gas"]: row["value"] for row in db.get_standard_compositions(self.conn, standard_id)}
        gases = db.list_gases(self.conn)
        first_entry = None
        for i, gas in enumerate(gases, start=1):
            ttk.Label(border, text=gas, width=6).grid(row=i, column=0, sticky="w", pady=1)
            var = tk.StringVar(value=str(existing[gas]) if gas in existing else "")

            def autosave(_e=None, g=gas, v=var):
                raw = v.get().strip()
                if not raw:
                    db.delete_standard_composition(self.conn, standard_id, g)
                    return
                try:
                    value = float(raw)
                except ValueError:
                    return  # live-typing: leave the prior value alone until this parses
                db.upsert_standard_composition(self.conn, standard_id, g, value)

            gas_entry = ttk.Entry(border, textvariable=var, width=8)
            gas_entry.grid(row=i, column=1, padx=(4, 4))
            gas_entry.bind("<KeyRelease>", autosave)
            gas_entry.bind("<FocusOut>", autosave)
            ttk.Label(border, text="%").grid(row=i, column=2, sticky="w")
            if first_entry is None:
                first_entry = gas_entry

        btn_row = ttk.Frame(border)
        btn_row.grid(row=len(gases) + 1, column=0, columnspan=3, pady=(8, 0), sticky="ew")
        ttk.Button(btn_row, text="Done", command=self._close_composition_popup).pack(side="left")

        popup.bind("<Escape>", lambda _e: self._close_composition_popup())
        popup.protocol("WM_DELETE_WINDOW", self._close_composition_popup)
        if first_entry is not None:
            first_entry.focus_set()

    def _close_composition_popup(self):
        popup = self._composition_popup
        if popup is None:
            return
        self._composition_popup = None
        on_done = self._composition_popup_done
        self._composition_popup_done = None
        popup.destroy()
        if on_done is not None:
            on_done()

    def _start_run_number_edit(self, row_iid):
        run_id = int(row_iid)
        row = db.list_runs_for_pressure_entry(self.conn, run_ids=[run_id])
        if not row or row[0]["round_id"] is None:
            messagebox.showwarning(
                "No round assigned",
                "This run isn't part of a round yet - assign it to one from the "
                "Selector tab's \"Assign to round...\" button before editing its "
                "run number by hand.",
            )
            return
        self._close_active_editor()
        self.tree.see(row_iid)
        self.update_idletasks()
        bbox = self.tree.bbox(row_iid, "run_number")
        if not bbox:
            return
        x, y, w, h = bbox
        var = tk.StringVar(value=self.tree.set(row_iid, "run_number"))
        entry = tk.Entry(self.tree, textvariable=var, relief="solid", borderwidth=1)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.select_range(0, "end")

        def commit(_event=None):
            if self._active_editor is not entry:
                return
            self._active_editor = None
            self._active_editor_close = None
            raw = var.get().strip()
            entry.destroy()
            if raw == self.tree.set(row_iid, "run_number"):
                return
            try:
                new_number = int(raw)
            except ValueError:
                messagebox.showerror("Invalid value", f"'{raw}' is not a whole number.")
                return
            # Writes immediately (and cascades every later run in the round) rather
            # than going through the pending/Save flow the pressure field uses - a
            # renumber is a structural change to the whole round, not a value that
            # makes sense to leave half-applied until Save.
            db.set_run_number_manual(self.conn, run_id, new_number)
            self._load_rows()
            # The cascade can change many OTHER runs' numbers too, not just this
            # one - the Selector tab shows run_number in its own tree and has no
            # other way to learn this happened, so it needs a real refresh, not
            # just notify_data_changed() (which only touches the Data Viewer).
            self.app.refresh()

        def cancel(_event=None):
            if self._active_editor is not entry:
                return
            self._active_editor = None
            self._active_editor_close = None
            entry.destroy()

        self._active_editor = entry
        self._active_editor_close = commit
        self._suppress_next_global_close = True
        entry.bind("<Return>", commit)
        entry.bind("<Escape>", cancel)

    def _start_edit(self, row_iid, suppress_close=True):
        self._close_active_editor()
        self.tree.see(row_iid)
        self.update_idletasks()
        bbox = self.tree.bbox(row_iid, "pressure")
        if not bbox:
            return
        x, y, w, h = bbox
        var = tk.StringVar(value=self.tree.set(row_iid, "pressure"))
        # Explicit solid border so the edit box clearly reads as "a text box you can
        # type in", rather than blending into the row like ttk's default subtle border.
        entry = tk.Entry(self.tree, textvariable=var, relief="solid", borderwidth=1)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.select_range(0, "end")

        def commit(_event=None):
            if self._active_editor is not entry:
                return
            self._active_editor = None
            self._active_editor_close = None
            old = self.tree.set(row_iid, "pressure")
            new = var.get().strip()
            if new != old:
                self._push_undo([(int(row_iid), old)])
            self._set_cell(row_iid, new)
            entry.destroy()

        def on_return(_event=None):
            commit()
            self._advance_row(row_iid, "pressure")
            return "break"

        def on_tab(_event=None):
            commit()
            self._swap_column(row_iid, "pressure", reverse=False)
            return "break"

        def on_shift_tab(_event=None):
            commit()
            self._swap_column(row_iid, "pressure", reverse=True)
            return "break"

        def cancel(_event=None):
            if self._active_editor is not entry:
                return
            self._active_editor = None
            self._active_editor_close = None
            entry.destroy()

        self._active_editor = entry
        self._active_editor_close = commit
        if suppress_close:
            self._suppress_next_global_close = True
        entry.bind("<Return>", on_return)
        entry.bind("<Tab>", on_tab)
        entry.bind("<Shift-Tab>", on_shift_tab)
        entry.bind("<Escape>", cancel)

    def _start_notes_edit(self, row_iid, suppress_close=True):
        self._close_active_editor()
        self.tree.see(row_iid)
        self.update_idletasks()
        bbox = self.tree.bbox(row_iid, "notes")
        if not bbox:
            return
        x, y, w, h = bbox
        run_id = int(row_iid)
        var = tk.StringVar(value=self.tree.set(row_iid, "notes"))
        entry = tk.Entry(self.tree, textvariable=var, relief="solid", borderwidth=1)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.select_range(0, "end")

        def commit(_event=None):
            # No pending/undo tracking here - notes write immediately, same precedent
            # as the standard field, since this isn't bulk numeric transcription.
            if self._active_editor is not entry:
                return
            self._active_editor = None
            self._active_editor_close = None
            new = var.get().strip()
            entry.destroy()
            db.set_run_notes(self.conn, run_id, new)
            self.tree.set(row_iid, "notes", new)
            self.app.sync_notes_display(run_id, new)
            self.app.notify_data_changed()

        def on_return(_event=None):
            commit()
            self._advance_row(row_iid, "notes")
            return "break"

        def on_tab(_event=None):
            commit()
            self._swap_column(row_iid, "notes", reverse=False)
            return "break"

        def on_shift_tab(_event=None):
            commit()
            self._swap_column(row_iid, "notes", reverse=True)
            return "break"

        def cancel(_event=None):
            if self._active_editor is not entry:
                return
            self._active_editor = None
            self._active_editor_close = None
            entry.destroy()

        self._active_editor = entry
        self._active_editor_close = commit
        if suppress_close:
            self._suppress_next_global_close = True
        entry.bind("<Return>", on_return)
        entry.bind("<Tab>", on_tab)
        entry.bind("<Shift-Tab>", on_shift_tab)
        entry.bind("<Escape>", cancel)

    def _close_active_editor(self):
        if self._active_editor_close is not None:
            self._active_editor_close()
        self._close_composition_popup()

    def _set_cell(self, row_iid, value):
        self.tree.set(row_iid, "pressure", value)
        run_id = int(row_iid)
        self.pending[run_id] = value
        tags = [t for t in self.tree.item(row_iid, "tags") if t not in ("edited", "edited-band")]
        edited_tag = "edited-band" if "band" in tags else "edited"
        if edited_tag not in tags:
            tags.append(edited_tag)
            self.tree.item(row_iid, tags=tags)
        if self._autosave_pressure(run_id, value):
            # Written straight to the database already - no longer actually
            # "pending" (undo works off self._undo_stack, and the "edited" tag is
            # tracked directly on the tree item, so dropping it here is safe and
            # is what lets a reload's tag logic stop flagging it as unsaved).
            self.pending.pop(run_id, None)

    def _autosave_pressure(self, run_id, raw):
        """Pressure now autosaves the same way the standard/notes fields already
        do - writes straight to the database as soon as a cell edit, paste, or undo
        commits a value that actually parses, instead of only ever landing in the
        database once "Save" is clicked. A value that doesn't parse yet (e.g. a
        half-finished paste row) is deliberately left alone here - it stays
        "pending" and gets caught by on_save()'s existing validation, same as
        before this existed. Returns True if it was written."""
        self._touched_run_ids.add(run_id)
        raw = raw.strip()
        if raw == "":
            db.set_pressures(self.conn, {run_id: None})
            return True
        if raw.upper() in NULL_TOKENS:
            db.set_pressures(self.conn, {run_id: None})
            db.set_pressure_not_recorded(self.conn, run_id, False)
            return True
        if raw.upper() in NOT_RECORDED_TOKENS:
            db.set_pressure_not_recorded(self.conn, run_id, True)
            return True
        try:
            value = float(raw)
        except ValueError:
            return False
        db.set_pressures(self.conn, {run_id: value})
        return True

    # -- undo ---------------------------------------------------------------
    def _push_undo(self, changes):
        if not changes:
            return
        self._undo_stack.append(changes)
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)

    def on_undo(self, _event=None):
        if not self._undo_stack:
            return "break"
        for run_id, old_value in self._undo_stack.pop():
            row_iid = str(run_id)
            if self.tree.exists(row_iid):
                self._set_cell(row_iid, old_value)
        return "break"

    # -- clipboard paste ---------------------------------------------------
    def on_paste(self, _event):
        sel = self.tree.selection()
        if not sel:
            return "break"
        try:
            clip = self.clipboard_get()
        except tk.TclError:
            return "break"
        values = [line.strip() for line in clip.replace("\r", "").split("\n") if line.strip() != ""]
        if not values:
            return "break"

        all_iids = self.tree.get_children("")
        start_idx = all_iids.index(sel[0])
        changes = []
        for offset, value in enumerate(values):
            idx = start_idx + offset
            if idx >= len(all_iids):
                break
            row_iid = all_iids[idx]
            old = self.tree.set(row_iid, "pressure")
            if value != old:
                changes.append((int(row_iid), old))
            self._set_cell(row_iid, value)
        self._push_undo(changes)
        return "break"

    # -- remove --------------------------------------------------------------
    def on_row_right_click(self, event):
        row_iid = self.tree.identify_row(event.y)
        if not row_iid:
            return
        # Right-clicking directly on an editable cell opens it for editing (every
        # _start_*_edit already selects the existing text, so this doubles as
        # "highlight and place the cursor" for click-and-type replacement) instead
        # of the row context menu below - that's still reachable by right-clicking
        # any other column (handle/run number/date/sample name).
        col = self.tree.identify_column(event.x)
        col_name = self._column_name_at(event.x)
        if col_name == "pressure":
            self._start_edit(row_iid)
            return
        if col == RUN_NUMBER_COLUMN_ID:
            self._start_run_number_edit(row_iid)
            return
        if col_name == "standard":
            self._start_standard_edit(row_iid)
            return
        if col_name == "notes":
            self._start_notes_edit(row_iid)
            return
        self.tree.selection_set(row_iid)
        menu = tk.Menu(self, tearoff=0)
        standard_name = self.tree.set(row_iid, "standard")
        if standard_name and standard_name.lower() != db.SAMPLE_STANDARD_NAME.lower():
            menu.add_command(
                label=f'Edit composition for "{standard_name}"...',
                command=lambda: self._edit_existing_composition(row_iid, standard_name),
            )
        current_pressure = self.tree.set(row_iid, "pressure")
        if current_pressure == "NR":
            menu.add_command(
                label="Clear \"not recorded\" flag", command=lambda: self._clear_not_recorded(row_iid)
            )
        else:
            menu.add_command(
                label="Mark pressure as not recorded (NR)",
                command=lambda: self._mark_not_recorded(row_iid),
            )
        menu.add_command(
            label="Remove entry (clear pressure & standard)",
            command=lambda: self.on_remove_entry(row_iid),
        )
        menu.tk_popup(event.x_root, event.y_root)

    def _mark_not_recorded(self, row_iid):
        run_id = int(row_iid)
        self.pending.pop(run_id, None)
        db.set_pressure_not_recorded(self.conn, run_id, True)
        self._load_rows()
        self.app.update_pressure_button()
        self.app.notify_data_changed()

    def _clear_not_recorded(self, row_iid):
        run_id = int(row_iid)
        db.set_pressure_not_recorded(self.conn, run_id, False)
        self._load_rows()
        self.app.update_pressure_button()
        self.app.notify_data_changed()

    def _edit_existing_composition(self, row_iid, standard_name):
        match = next((s for s in db.list_standards(self.conn) if s["name"] == standard_name), None)
        if match is not None:
            self._open_composition_popup(row_iid, match["standard_id"], standard_name)

    def on_delete_key(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return "break"
        self.on_remove_entry(sel[0])
        return "break"

    def on_remove_entry(self, row_iid):
        if not self.tree.exists(row_iid):
            return
        sample_name = self.tree.set(row_iid, "sample_name")
        if not messagebox.askyesno(
            "Remove entry", f'Clear the pressure and standard link for "{sample_name}"?'
        ):
            return
        self._close_active_editor()
        run_id = int(row_iid)
        db.remove_pressure_entry(self.conn, run_id)
        self.pending.pop(run_id, None)
        self._load_rows()
        self.app.update_pressure_button()
        self.app.notify_data_changed()

    # -- validation -----------------------------------------------------------
    def _validation_issues(self):
        """Rows with a standard linked but no pressure entered - the one inconsistency
        worth flagging, since pressure/standard/notes are otherwise fine to fill in in
        any order or leave blank."""
        rows = db.list_runs_for_pressure_entry(self.conn, run_ids=self.run_ids, missing_only=False)
        return [
            r for r in rows
            if r["standard_id"] is not None and r["pressure"] is None and not r["pressure_not_recorded"]
        ]

    def _warn_validation_issues(self):
        """Updates the app's passive orange toolbar warning instead of popping a
        blocking dialog - and only for runs actually touched (edited) in this
        session, not every "standard linked, no pressure" gap in the whole
        database. A run that gets fixed later in the same session (or was already
        fine) is removed from the warning if it had been flagged before."""
        if not self._touched_run_ids:
            return
        still_invalid = {
            r["run_id"] for r in self._validation_issues() if r["run_id"] in self._touched_run_ids
        }
        warn_ids = self.app._pressure_missing_for_standard_run_ids
        warn_ids.difference_update(self._touched_run_ids)
        warn_ids.update(still_invalid)
        self.app.update_load_pending_indicator()

    # -- save / close ----------------------------------------------------------
    def on_save(self):
        to_write = {}
        not_recorded = []
        explicit_clear = []
        self._touched_run_ids.update(self.pending.keys())
        for run_id, raw in self.pending.items():
            raw = raw.strip()
            if raw == "":
                to_write[run_id] = None
                continue
            if raw.upper() in NULL_TOKENS:
                to_write[run_id] = None
                explicit_clear.append(run_id)
                continue
            if raw.upper() in NOT_RECORDED_TOKENS:
                not_recorded.append(run_id)
                continue
            try:
                to_write[run_id] = float(raw)
            except ValueError:
                messagebox.showerror(
                    "Invalid value",
                    f"'{raw}' is not a number (run_id {run_id}). Type NR if the pressure was never recorded, "
                    "or NULL to clear it entirely.",
                )
                return
        if to_write:
            db.set_pressures(self.conn, to_write)
        for run_id in not_recorded:
            db.set_pressure_not_recorded(self.conn, run_id, True)
        for run_id in explicit_clear:
            db.set_pressure_not_recorded(self.conn, run_id, False)
        self.pending = {}
        self._undo_stack = []
        self._load_rows()
        self.app.update_pressure_button()
        self.app.notify_data_changed()
        self._warn_validation_issues()
        if self.auto_close_on_save:
            # Only ever set True for the standalone popup - closes the real
            # Toplevel window it's hosted in, not just this content Frame (which,
            # when embedded in the Load Data tab, must never be destroyed).
            self.winfo_toplevel().destroy()
            if self.on_close_callback is not None:
                self.on_close_callback()

    def on_close(self):
        """Bound to the popup window's WM_DELETE_WINDOW by PressureEntryWindow -
        never invoked when this panel is embedded in the Load Data tab, since
        there's no window-close event to bind it to there."""
        if self.pending and messagebox.askyesno(
            "Unsaved changes", "Save pending pressure edits before closing?"
        ):
            self.on_save()
        else:
            self._warn_validation_issues()
        self.winfo_toplevel().destroy()
        if self.on_close_callback is not None:
            self.on_close_callback()


class PressureEntryWindow(tk.Toplevel):
    """Standalone popup entry point ("Enter pressures...", "Edit selected
    pressures", the auto-prompt after loading incomplete runs into Models) - just a
    plain window hosting one PressureEntryPanel. Save closes the window once the
    values are actually written (auto_close_on_save defaults True here) - a caller
    can still pass False to keep it open, but every current entry point wants
    Save to mean "I'm done," not "keep this open until I close it myself." This
    default is specific to the standalone popup - PressureEntryPanel's own default
    stays False, since that class is also embedded directly (not behind a Toplevel)
    in the Load Data tab, where calling winfo_toplevel().destroy() would tear down
    the whole app window instead of just this panel."""

    def __init__(self, app, run_ids=None, missing_only=False, on_close_callback=None,
                 auto_close_on_save=True, preselected_run_ids=None):
        super().__init__(app)
        self.title("Pressure entry")
        self.geometry("820x520")
        self.minsize(760, 400)
        self.panel = PressureEntryPanel(
            self, app, run_ids=run_ids, missing_only=missing_only,
            on_close_callback=on_close_callback, auto_close_on_save=auto_close_on_save,
            preselected_run_ids=preselected_run_ids,
        )
        self.panel.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.panel.on_close)
