"""A window for reviewing permanently-deleted runs and restoring them - restoring
only un-blocks re-ingestion (removes the hash from the ignore list); the run itself
comes back the next time "Ingest folder..." is pointed at its original location."""

import re
import tkinter as tk
from tkinter import ttk, messagebox

from tkcalendar import DateEntry

from gc_pipeline import db
from gc_pipeline.widgets import DropdownChecklist, bind_fast_hscroll, justify_columns

COLUMNS = ("sel", "sample_name", "source_file", "run_number", "ignored_at")
FILTER_DEBOUNCE_MS = 250
DATE_FOLDER_RE = re.compile(r"^Results/(\d{4}-\d{2}-\d{2})/")
UNKNOWN_FOLDER = "(no date folder)"

CHECK_ON = "☑"
CHECK_OFF = "☐"
SHIFT_MASK = 0x0001
CTRL_MASK = 0x0004


def _date_folder_of(source_file):
    """The Results/<date>/ folder a deleted file's source_file path came from, if
    any - flat root-level files (no Results/<date>/ prefix) fall into one bucket."""
    if not source_file:
        return UNKNOWN_FOLDER
    match = DATE_FOLDER_RE.match(source_file)
    return match.group(1) if match else UNKNOWN_FOLDER


class DeletedFilesWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.conn = app.conn
        self._filter_after_id = None

        self._sort_column = "ignored_at"
        self._sort_reverse = True  # most recently deleted first, by default

        # Same checkbox + click/shift-click/drag selection scheme as the main
        # browser's tree, ported to this flat (non-grouped) list.
        self.selected_hashes = set()
        self._anchor_hash = None
        self._drag_anchor_hash = None
        self._drag_target_state = None
        self._drag_snapshot = {}
        self._selection_undo_stack = []

        self.title("Deleted files")
        self.geometry("900x480")
        self.minsize(860, 300)

        self._build()
        self._load_rows()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=6)
        ttk.Button(top, text="Restore selected", command=self.on_restore).pack(side="left")
        ttk.Button(top, text="Purge selected", command=self.on_purge).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Justify columns", command=self.on_justify_columns).pack(side="left", padx=(6, 0))
        self.selection_label = ttk.Label(top, text="0 selected")
        self.selection_label.pack(side="left", padx=10)

        filter_bar = ttk.Frame(self)
        filter_bar.pack(fill="x", padx=6, pady=(0, 6))

        ttk.Label(filter_bar, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._schedule_live_filter)
        ttk.Entry(filter_bar, textvariable=self.search_var, width=18).pack(side="left", padx=(2, 10))

        self.date_folder_filter = DropdownChecklist(
            filter_bar, "Folder", self._load_available_folders(), self._load_rows
        )
        self.date_folder_filter.pack(side="left", padx=(0, 10))

        ttk.Label(filter_bar, text="Deleted from:").pack(side="left")
        self.date_from_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_bar, variable=self.date_from_enabled, command=self._load_rows).pack(side="left")
        self.date_from_entry = DateEntry(filter_bar, width=10, date_pattern="yyyy-mm-dd")
        self.date_from_entry.pack(side="left", padx=(0, 10))
        self.date_from_entry.bind("<<DateEntrySelected>>", self._on_date_changed)

        ttk.Label(filter_bar, text="to:").pack(side="left")
        self.date_to_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_bar, variable=self.date_to_enabled, command=self._load_rows).pack(side="left")
        self.date_to_entry = DateEntry(filter_bar, width=10, date_pattern="yyyy-mm-dd")
        self.date_to_entry.pack(side="left", padx=(0, 10))
        self.date_to_entry.bind("<<DateEntrySelected>>", self._on_date_changed)

        ttk.Button(filter_bar, text="Clear filters", command=self.on_clear_filters).pack(side="left")

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self.tree = ttk.Treeview(tree_frame, columns=COLUMNS, show="headings", selectmode="browse")
        for col in COLUMNS:
            if col == "sel":
                self.tree.column(col, width=32, anchor="center", stretch=False)
                continue
            self.tree.heading(col, command=lambda c=col: self._on_sort_click(c))
            self.tree.column(col, width=70 if col == "run_number" else 150, anchor="w")
        self.tree.tag_configure("selected", background="#bdddff")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        bind_fast_hscroll(self.tree)
        self.tree.bind("<ButtonPress-1>", self.on_tree_press)
        self.tree.bind("<B1-Motion>", self.on_tree_drag)
        self.tree.bind("<Control-z>", self.on_undo_selection)

        hint = ttk.Label(
            self,
            text="Restoring removes the file from this list and un-blocks it - the run itself "
                 "comes back the next time you run \"Ingest folder...\" on its original location. "
                 "Purging removes it from this list the same way, for entries you're just clearing "
                 "out and never intend to bring back - note it has the same side effect of "
                 "un-blocking re-ingestion, since there's no way to forget a file without also "
                 "un-blocking it; use Restore instead if you actually want it back.",
            foreground="#666666", wraplength=580, justify="left",
        )
        hint.pack(fill="x", padx=6, pady=(0, 6))

    def on_justify_columns(self):
        justify_columns(self.tree, list(COLUMNS))

    # -- filtering -----------------------------------------------------
    def _schedule_live_filter(self, *_args):
        if self._filter_after_id is not None:
            self.after_cancel(self._filter_after_id)
        self._filter_after_id = self.after(FILTER_DEBOUNCE_MS, self._run_live_filter)

    def _run_live_filter(self):
        self._filter_after_id = None
        self._load_rows()

    def _on_date_changed(self, _event):
        self._load_rows()

    def on_clear_filters(self):
        self.search_var.set("")
        self.date_from_enabled.set(False)
        self.date_to_enabled.set(False)
        self.date_folder_filter.select_all()
        self._load_rows()

    def _load_available_folders(self):
        return sorted({_date_folder_of(r["source_file"]) for r in db.list_ignored_hashes(self.conn)})

    # -- sorting -------------------------------------------------------
    def _on_sort_click(self, col):
        if self._sort_column == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = col
            self._sort_reverse = False
        self._load_rows()

    def _sort_key(self, row):
        if self._sort_column == "run_number":
            return (row["run_number"] is None, row["run_number"] or 0)
        return row[self._sort_column] or ""

    def _update_headings(self):
        for col in COLUMNS:
            if col == "sel":
                continue
            label = col
            if col == self._sort_column:
                label += " ▼" if self._sort_reverse else " ▲"
            self.tree.heading(col, text=label)

    def _load_rows(self):
        self.tree.delete(*self.tree.get_children())
        needle = self.search_var.get().strip().lower()
        date_from = self.date_from_entry.get() if self.date_from_enabled.get() else None
        date_to = self.date_to_entry.get() if self.date_to_enabled.get() else None
        allowed_folders = set(self.date_folder_filter.selected())

        rows = []
        for r in db.list_ignored_hashes(self.conn):
            if needle:
                haystack = f"{r['sample_name'] or ''} {r['source_file'] or ''}".lower()
                if needle not in haystack:
                    continue
            if _date_folder_of(r["source_file"]) not in allowed_folders:
                continue
            ignored_date = (r["ignored_at"] or "")[:10]
            if date_from and ignored_date < date_from:
                continue
            if date_to and ignored_date > date_to:
                continue
            rows.append(r)

        rows.sort(key=self._sort_key, reverse=self._sort_reverse)
        for r in rows:
            h = r["content_hash"]
            selected = h in self.selected_hashes
            self.tree.insert(
                "", "end", iid=h,
                values=(CHECK_ON if selected else CHECK_OFF,
                        r["sample_name"] or "(unknown)", r["source_file"] or "(unknown)",
                        r["run_number"] if r["run_number"] is not None else "", r["ignored_at"]),
                tags=("selected",) if selected else (),
            )
        self._update_headings()
        self._update_selection_label()

    def on_restore(self):
        sel = list(self.selected_hashes)
        if not sel:
            messagebox.showwarning("Nothing selected", "No deleted files are selected.")
            return
        db.unignore_hashes(self.conn, sel)
        self.selected_hashes.clear()
        messagebox.showinfo(
            "Restored",
            f"Restored {len(sel)} file(s). Re-run \"Ingest folder...\" on their original "
            f"location to bring them back.",
        )
        self._load_rows()

    def on_purge(self):
        """Removes selected entries from this list entirely - for old/irrelevant
        deletions the user is just clearing out, not planning to bring back. Same
        underlying db.unignore_hashes call as Restore (there's no separate "forget
        forever but still keep blocked" state in the schema - clearing the tracking
        entry always also un-blocks re-ingestion), just framed and confirmed as its
        own distinct action for that different intent."""
        sel = list(self.selected_hashes)
        if not sel:
            messagebox.showwarning("Nothing selected", "No deleted files are selected.")
            return
        if not messagebox.askyesno(
            "Purge selected?",
            f"Remove {len(sel)} entry(ies) from this list permanently?\n\n"
            "This also un-blocks re-ingestion for their content (same as Restore) - "
            "use Restore instead if you actually intend to bring them back.",
        ):
            return
        db.unignore_hashes(self.conn, sel)
        self.selected_hashes.clear()
        messagebox.showinfo("Purged", f"Removed {len(sel)} entry(ies) from the deleted-files list.")
        self._load_rows()

    # -- selection ("scratchpad"), same click/shift-click/drag scheme as the main
    # browser's tree, adapted to this flat (non-grouped) list keyed by content_hash.
    def on_tree_press(self, event):
        h = self.tree.identify_row(event.y)
        if not h:
            self._drag_anchor_hash = None
            return
        shift = bool(event.state & SHIFT_MASK)
        ctrl = bool(event.state & CTRL_MASK)
        self._push_selection_undo()
        if shift and ctrl and self._anchor_hash is not None:
            self._toggle_range(self._anchor_hash, h)
            self._drag_anchor_hash = None
        elif shift and self._anchor_hash is not None:
            self._select_range(self._anchor_hash, h)
            self._drag_anchor_hash = None
        else:
            self._drag_snapshot = {x: (x in self.selected_hashes) for x in self._flattened_hashes()}
            was_selected = h in self.selected_hashes
            self._set_hash_selected(h, not was_selected)
            self._anchor_hash = h
            self._drag_anchor_hash = h
            self._drag_target_state = not was_selected
        self._move_focus(h)

    def on_tree_drag(self, event):
        if self._drag_anchor_hash is None:
            return
        h = self.tree.identify_row(event.y)
        if not h:
            return
        order = self._flattened_hashes()
        if self._drag_anchor_hash not in order or h not in order:
            return
        i, j = order.index(self._drag_anchor_hash), order.index(h)
        lo, hi = min(i, j), max(i, j)
        in_range = set(order[lo:hi + 1])
        for x in order:
            desired = self._drag_target_state if x in in_range else self._drag_snapshot.get(x, False)
            if (x in self.selected_hashes) != desired:
                self._set_hash_selected(x, desired)
        self._move_focus(h)

    def _flattened_hashes(self):
        return list(self.tree.get_children(""))

    def _select_range(self, anchor_hash, target_hash):
        order = self._flattened_hashes()
        if anchor_hash not in order or target_hash not in order:
            return
        i, j = order.index(anchor_hash), order.index(target_hash)
        lo, hi = min(i, j), max(i, j)
        for h in order[lo:hi + 1]:
            self._set_hash_selected(h, True)

    def _toggle_range(self, anchor_hash, target_hash):
        order = self._flattened_hashes()
        if anchor_hash not in order or target_hash not in order:
            return
        desired = target_hash not in self.selected_hashes
        i, j = order.index(anchor_hash), order.index(target_hash)
        lo, hi = min(i, j), max(i, j)
        for h in order[lo:hi + 1]:
            self._set_hash_selected(h, desired)

    def _move_focus(self, h):
        self.tree.see(h)
        self.tree.focus(h)
        self.tree.selection_set(h)

    def _set_hash_selected(self, h, selected):
        if selected:
            self.selected_hashes.add(h)
        else:
            self.selected_hashes.discard(h)
        if self.tree.exists(h):
            self.tree.set(h, "sel", CHECK_ON if selected else CHECK_OFF)
            self.tree.item(h, tags=("selected",) if selected else ())
        self._update_selection_label()

    def _update_selection_label(self):
        self.selection_label.config(text=f"{len(self.selected_hashes)} selected")

    def _push_selection_undo(self):
        self._selection_undo_stack.append(frozenset(self.selected_hashes))
        if len(self._selection_undo_stack) > 50:
            self._selection_undo_stack.pop(0)

    def on_undo_selection(self, _event=None):
        if not self._selection_undo_stack:
            return "break"
        target = self._selection_undo_stack.pop()
        current = set(self.selected_hashes)
        for h in current - target:
            self._set_hash_selected(h, False)
        for h in target - current:
            self._set_hash_selected(h, True)
        return "break"
