"""Tkinter desktop app: ingest, browse/filter, and export GC runs."""

import datetime
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, font as tkfont

from gc_pipeline import db, export
from gc_pipeline.ingest import ingest as run_ingest
from gc_pipeline.pressure_wizard import PressureEntryWindow
from gc_pipeline.deleted_files import DeletedFilesWindow
from gc_pipeline.standards import StandardsWindow
from gc_pipeline.data_viewer import DataViewerPanel
from gc_pipeline.models_panel import ModelsPanel
from gc_pipeline.analysis_panel import AnalysisPanel
from gc_pipeline.run_rounds_dialog import RoundAssignDialog, RoundPickerDialog
from gc_pipeline.highlight_swatches_dialog import HighlightSwatchesWindow
from gc_pipeline.timer_popup import StackTimerWindow
from gc_pipeline.duplicates_wizard import DuplicatesWizardWindow
from gc_pipeline.load_data_panel import LoadDataPanel
from gc_pipeline.help_panel import HelpPanel
from gc_pipeline.widgets import (
    ActionsPopup, DropdownChecklist, GasChannelFilter, bind_fast_hscroll, bind_tooltip,
    bind_treeview_heading_tooltip, justify_columns,
)

ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"

# Shared status-banner colors: green = "stuff to review, nothing wrong",
# orange = "duplicates need a decision", red = "couldn't even ingest this".
STATUS_GREEN = "#1e7d32"
STATUS_ORANGE = "#a06000"
STATUS_RED = "#b3261e"
STATUS_NEUTRAL = "#666666"

COLUMNS = ("sel", "highlight", "sample_name", "injection_date", "run_number", "acq_method",
           "analysis_method", "duplicate_of", "excluded")

COLUMN_FULL_LABELS = {
    "sel": "Selected", "highlight": "Highlight", "sample_name": "Sample name",
    "injection_date": "Injection date", "run_number": "Run number",
    "acq_method": "Acquisition method", "analysis_method": "Analysis method",
    "duplicate_of": "Duplicate of", "excluded": "Excluded",
}

CHECK_ON = "☑"   # checked box glyph
CHECK_OFF = "☐"  # unchecked box glyph

SHIFT_MASK = 0x0001
CTRL_MASK = 0x0004

FILTER_DEBOUNCE_MS = 250  # delay before a text-filter keystroke triggers a live re-query
POLL_INTERVAL_MS = 20_000  # how often the watched folder is auto-rechecked for new files

NOTES_MIN_LINES = 1
NOTES_MAX_LINES = 8


def _wrapped_line_count(font_obj, line, width_px):
    """How many display lines `line` (no newlines) takes when word-wrapped at
    width_px, per the given font's metrics - a pure text-metrics computation (no
    dependency on the widget actually being drawn on screen)."""
    if not line:
        return 1
    words = line.split(" ")
    count = 1
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if not current or font_obj.measure(candidate) <= width_px:
            current = candidate
        else:
            count += 1
            current = word
    return count


class App(tk.Tk):
    def __init__(self, db_path="gc_data.sqlite3"):
        super().__init__()
        self.db_path = db_path
        self.conn = db.connect(db_path)
        self._watched_folder = db.get_setting(self.conn, "watched_folder")
        self._watch_paused = False
        # "Watch for rest of day + assign to round..." mode: while set, every newly-
        # ingested run during a poll pass is auto-added to this round, until local
        # midnight (self._watch_and_assign_until) passes, at which point it clears
        # itself and watching auto-pauses.
        self._watch_and_assign_round_id = None
        self._watch_and_assign_round_name = None
        self._watch_and_assign_until = None
        # Running history of what's actually landed in the database, newest first -
        # distinct from _watch_second_line (which only ever reflects the *last*
        # poll), so a scientist coming back after being away can see everything that
        # was added while they weren't watching, not just the most recent check.
        self._ingest_history = []
        # Last-poll-only facts (not persisted in the DB, unlike the pending-review/
        # duplicate counts) - used purely to color/flag the toolbar indicator and the
        # Folder tab's warning banner red when the most recent pass hit something
        # that isn't a normal "stuff to review" situation.
        self._last_ingest_errors = 0
        self._last_missing_export = 0
        self._last_missing_export_paths = []
        # run_ids that a pressure-entry session just flagged as "standard linked,
        # no pressure entered" - populated/cleared per-run by PressureEntryPanel as
        # it saves/closes, scoped to only what that session actually touched. Drives
        # the orange toolbar warning label below.
        self._pressure_missing_for_standard_run_ids = set()
        # Unlike "N new runs" (which naturally stops repeating once those files are
        # ingested and become "skipped" next poll), a persistent problem - parse
        # errors, a missing CSV export - gets rediscovered on every single poll for
        # as long as it stays unfixed, since ingest() has to rescan the same broken
        # files every time. Logging that to the history list on every 20-second poll
        # would drown out everything else, so each category is only actually logged
        # again once a day, or once its count has grown by 10+ since it was last
        # logged, or immediately the first time it reappears after being cleared.
        self._persistent_issue_log_state = {
            "errors": {"last_at": None, "last_count": 0},
            "missing_export": {"last_at": None, "last_count": 0},
        }
        self.title("GC Report Browser")
        # Opens at a size wide enough that the Selector's filter bar isn't clipped by
        # the Data Viewer pane's fixed opening width, but minsize is kept well below
        # half of a typical 1920px-wide display - a minsize near/above half the
        # screen width blocks (or badly clips) Windows 11's Snap/split-screen, since
        # Tk won't let the window shrink smaller than minsize to fit a snap region.
        # Below this size some content scrolls/clips rather than staying fully
        # visible, which is an acceptable tradeoff for snap support to work at all.
        self.geometry("1650x760")
        self.minsize(900, 600)
        if ICON_PATH.exists():
            self.iconbitmap(str(ICON_PATH))

        self.selected_run_ids = set()
        self._base_run_tags = {}     # run_id -> tags tuple, excluding "selected"
        self._anchor_run_id = None   # last explicitly (de)selected run; start of a Shift-range
        self._focus_run_id = None    # keyboard cursor position
        self._filter_after_id = None

        # Sorts the runs *within* each date group only - date groups themselves
        # always stay in date order and never mix together (see refresh()).
        self._tree_sort_column = "injection_date"
        self._tree_sort_reverse = False
        # True = newest folder on top (the default), False = oldest on top.
        self._tree_group_reverse = True
        self._highlight_overlays = {}  # run_id -> tk.Label chip for its highlight column

        # Live drag state: while dragging, every row is either inside the current
        # [anchor, cursor] range (forced to _drag_target_state) or restored to whatever
        # it was in _drag_snapshot before the drag began - this is what lets dragging
        # back over already-painted rows undo them.
        self._drag_anchor_run_id = None
        self._drag_target_state = None
        self._drag_snapshot = {}

        self._selection_undo_stack = []
        self._detail_run_id = None  # run currently shown in the Notes box + raw inspector

        # Packed before the notebook (and with side="bottom") so it reserves its own
        # strip at the bottom of the window regardless of which tab is active -
        # "shared across tabs" per the user's ask, rather than living inside any one
        # tab's own toolbar.
        bottom_bar = ttk.Frame(self)
        bottom_bar.pack(side="bottom", fill="x")
        ttk.Separator(bottom_bar, orient="horizontal").pack(fill="x")
        ttk.Button(bottom_bar, text="Timer...", command=self.on_open_timer).pack(
            side="right", padx=6, pady=4
        )
        self._timer_window = None

        # The four top-level tabs (Load Data/Selector/Models/Analysis) are the
        # highest-level navigation in the whole app - a bigger, bolder tab label
        # keeps them from visually blending in with the many smaller buttons/sub-tabs
        # underneath, per the user's "they get lost" report. Scoped to its own style
        # name so it doesn't also inflate the Load Data tab's own inner sub-tab strip
        # or the Models tab's per-gas sub-tabs, which should stay their normal size.
        style = ttk.Style(self)
        style.configure("TopLevel.TNotebook.Tab", font=("", 10, "bold"), padding=(10, 4))
        self.notebook = ttk.Notebook(self, style="TopLevel.TNotebook")
        self.notebook.pack(fill="both", expand=True)

        self.selector_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.selector_tab, text="Selector")
        self._build_toolbar(self.selector_tab)
        self._build_table(self.selector_tab)

        self.models_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.models_tab, text="Models")
        self.models_panel = ModelsPanel(self.models_tab, self)
        self.models_panel.pack(fill="both", expand=True)

        self.analysis_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.analysis_tab, text="Analysis")
        self.analysis_panel = AnalysisPanel(self.analysis_tab, self)
        self.analysis_panel.pack(fill="both", expand=True)

        self.help_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.help_tab, text="Help")
        self.help_panel = HelpPanel(self.help_tab, self)
        self.help_panel.pack(fill="both", expand=True)

        # Built last (needs self.analysis_panel etc. to already exist - its embedded
        # panels touch those at runtime, not construction time, but this ordering
        # keeps that assumption trivially true) then moved to the front - the tab
        # order should read "Load Data, Selector, Models, Analysis" left to right,
        # but the app still opens on Selector (see notebook.select below).
        self.load_tab = ttk.Frame(self.notebook)
        self.load_data_panel = LoadDataPanel(self.load_tab, self)
        self.load_data_panel.pack(fill="both", expand=True)
        self.notebook.add(self.load_tab, text="Load Data")
        self.notebook.insert(0, self.load_tab)
        self.notebook.select(self.selector_tab)

        # A ttk.Treeview's cell overlays (out-of-range shading, highlight chips) are
        # placed via bbox(), which comes back empty while the widget is unmapped -
        # true whenever its tab isn't the one currently showing. Any render that
        # happened while the Analysis tab was hidden needs a follow-up pass once it's
        # actually visible, or those overlays silently never appear.
        self.notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

        # Set the initial sash positions now that geometry has been computed, so the
        # side panel opens at roughly a fixed width and the inspector at a fixed
        # height instead of ttk's default 50/50 split.
        self.update_idletasks()
        self._paned.sash_place(0, self._paned.winfo_width() - self._side_panel_width, 0)
        self._vpaned.sashpos(0, self._vpaned.winfo_height() - self._inspector_height)

        self.refresh()
        self.update_pressure_button()
        if self._watched_folder:
            # Picks up anything dropped into the folder while the app was closed,
            # before settling into the recurring poll below.
            self._run_ingest_pass(manual=False)
        self.after(POLL_INTERVAL_MS, self._poll_tick)

    # -- layout -----------------------------------------------------
    def _build_toolbar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=8, pady=6)

        # The folder/watch/ingest controls used to live here - they've moved to the
        # Load Data tab's "Folder" sub-tab (see load_data_panel.py), reached via the
        # "Load Data..." button below, so this toolbar only needs a launcher plus a
        # compact glance at whether anything's currently mid-review.
        ttk.Button(bar, text="Load Data...", command=self.on_open_load_tab).pack(side="left")
        # Clickable, and split by what it actually routes to: reviewing plain
        # pending runs (green) belongs on the Pressure entry sub-tab, duplicate
        # groups (orange) belong on the Duplicates sub-tab - clicking either jumps
        # straight there rather than making the user go find the right sub-tab
        # themselves. A third (red) label flags the rarer "couldn't even ingest"
        # case - parse failures or a missing CSV export - and points at Help.
        self.load_pending_review_label = ttk.Label(bar, text="", foreground=STATUS_GREEN, cursor="hand2")
        self.load_pending_review_label.pack(side="left", padx=(6, 0))
        self.load_pending_review_label.bind("<Button-1>", lambda _e: self.on_open_pressure_review())

        self.load_pending_duplicates_label = ttk.Label(bar, text="", foreground=STATUS_ORANGE, cursor="hand2")
        self.load_pending_duplicates_label.pack(side="left", padx=(6, 0))
        self.load_pending_duplicates_label.bind("<Button-1>", lambda _e: self.on_open_duplicates_tab())

        self.load_pending_errors_label = ttk.Label(bar, text="", foreground=STATUS_RED, cursor="hand2")
        self.load_pending_errors_label.pack(side="left", padx=(6, 0))
        self.load_pending_errors_label.bind("<Button-1>", lambda _e: self.on_open_load_folder_tab())

        # Flags runs that were just edited in a pressure-entry session and ended up
        # with a standard linked but no pressure entered - scoped to only the runs
        # actually touched in that session (not a database-wide scan), so this never
        # resurfaces old, unrelated gaps. Clicking it reopens exactly those runs in
        # the pressure editor so they can be fixed immediately.
        self.load_pending_pressure_warning_label = ttk.Label(bar, text="", foreground=STATUS_ORANGE, cursor="hand2")
        self.load_pending_pressure_warning_label.pack(side="left", padx=(6, 0))
        self.load_pending_pressure_warning_label.bind(
            "<Button-1>", lambda _e: self.on_open_pressure_warning_review()
        )

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12)

        ttk.Button(bar, text="Enter pressures...", command=self.on_enter_pressures).pack(side="left", padx=6)
        ttk.Button(bar, text="Standards...", command=self.on_show_standards).pack(side="left", padx=6)
        # A bit larger than the surrounding buttons - it's the main way into the
        # Models/Analysis tabs from here, matched to the same font size as the
        # top-level tab labels so it doesn't read as just another toolbar button.
        action_style = ttk.Style(self)
        action_style.configure("BigAction.TButton", font=("", 10, "bold"), padding=(10, 4))
        ttk.Button(
            bar, text="Load selected into Models", style="BigAction.TButton", command=self.on_load_into_models
        ).pack(side="left", padx=6)
        self.selection_label = ttk.Label(bar, text="0 runs selected")
        self.selection_label.pack(side="left", padx=10)

        ttk.Separator(parent, orient="horizontal").pack(fill="x")

    def _build_filters(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=4, pady=(0, 4))

        ttk.Label(bar, text="Sample name:").pack(side="left")
        self.sample_name_var = tk.StringVar()
        self.sample_name_var.trace_add("write", self._schedule_live_filter)
        ttk.Entry(bar, textvariable=self.sample_name_var, width=14).pack(side="left", padx=(2, 8))

        self.gas_filter = GasChannelFilter(bar, "Gas", self.refresh)
        self.gas_filter.pack(side="left", padx=(0, 6))
        self.gas_filter.set_gases(db.list_gases(self.conn), db.get_gas_channels(self.conn))

        self.standard_filter = DropdownChecklist(bar, "Standard", self._load_standard_options(), self.refresh)
        self.standard_filter.pack(side="left", padx=(0, 6))

        self.date_from_enabled = tk.BooleanVar(value=False)
        self.date_to_enabled = tk.BooleanVar(value=False)
        self.date_from_var = tk.StringVar()
        self.date_to_var = tk.StringVar()
        self.dates_popup = ActionsPopup(bar, "Dates", self._build_dates_popup)
        self.dates_popup.pack(side="left", padx=(0, 6))

        self.show_duplicates_var = tk.BooleanVar(value=False)
        self.show_excluded_var = tk.BooleanVar(value=True)
        # Stacked together since they're both "go deal with duplicates" actions -
        # the wizard shortcut sits directly above the popup rather than off in the
        # selection-actions row, so they read as one unit.
        dup_stack = ttk.Frame(bar)
        dup_stack.pack(side="left", padx=(0, 6))
        ttk.Button(dup_stack, text="Duplicates wizard...", command=self.on_open_duplicates_tab).pack(
            fill="x", pady=(0, 2)
        )
        self.duplicates_popup = ActionsPopup(dup_stack, "Duplicates", self._build_duplicates_popup)
        self.duplicates_popup.pack(fill="x")

    def _build_selection_actions_row(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(bar, text="Assign to round...", command=self.on_assign_to_round).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(
            bar, text="Add to loaded selection", command=self.on_add_to_loaded_selection
        ).pack(side="left", padx=(0, 6))

    def _build_dates_popup(self, body):
        date_options = self._load_date_options()

        from_row = ttk.Frame(body)
        from_row.pack(fill="x", pady=2)
        ttk.Checkbutton(from_row, variable=self.date_from_enabled, command=self.refresh).pack(side="left")
        ttk.Label(from_row, text="From:").pack(side="left")
        self.date_from_combo = ttk.Combobox(
            from_row, textvariable=self.date_from_var, width=10, state="readonly", values=date_options
        )
        self.date_from_combo.pack(side="left", padx=(4, 0))
        self.date_from_combo.bind("<<ComboboxSelected>>", self._on_date_from_changed)

        to_row = ttk.Frame(body)
        to_row.pack(fill="x", pady=2)
        ttk.Checkbutton(to_row, variable=self.date_to_enabled, command=self.refresh).pack(side="left")
        ttk.Label(to_row, text="To:").pack(side="left", padx=(8, 0))
        self.date_to_combo = ttk.Combobox(
            to_row, textvariable=self.date_to_var, width=10, state="readonly", values=date_options
        )
        self.date_to_combo.pack(side="left", padx=(4, 0))
        self.date_to_combo.bind("<<ComboboxSelected>>", self._on_date_to_changed)

    def _build_duplicates_popup(self, body):
        ttk.Checkbutton(body, text="Show duplicates", variable=self.show_duplicates_var,
                         command=self.refresh).pack(anchor="w", pady=2)
        ttk.Checkbutton(body, text="Show excluded", variable=self.show_excluded_var,
                         command=self.refresh).pack(anchor="w", pady=2)
        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=6)
        ttk.Button(body, text="Exclude all duplicates",
                   command=self._popup_action(self.duplicates_popup, self.on_exclude_all_duplicates)).pack(
            fill="x", pady=2
        )
        ttk.Button(body, text="Open as a separate window...",
                   command=self._popup_action(self.duplicates_popup, self.on_open_duplicates_wizard)).pack(
            fill="x", pady=2
        )

    def _popup_action(self, popup, fn):
        """Wrap a popup button's command so clicking it also closes that popup -
        otherwise it'd sit open over whatever dialog fn() opens."""
        def _run():
            popup.close_popup()
            fn()
        return _run

    def _load_gas_options(self):
        return db.list_gases(self.conn)

    def _load_date_options(self):
        rows = self.conn.execute(
            "SELECT DISTINCT substr(injection_date, 1, 10) AS d FROM runs "
            "WHERE injection_date IS NOT NULL ORDER BY d DESC"
        ).fetchall()
        return [r["d"] for r in rows]

    def _load_standard_options(self):
        names = sorted({name for name in db.get_run_standards(self.conn).values() if name})
        return names + ["(none)"]

    def _on_date_from_changed(self, _event):
        self.date_from_enabled.set(True)
        self.refresh()

    def _on_date_to_changed(self, _event):
        self.date_to_enabled.set(True)
        self.refresh()

    def _schedule_live_filter(self, *_args):
        if self._filter_after_id is not None:
            self.after_cancel(self._filter_after_id)
        self._filter_after_id = self.after(FILTER_DEBOUNCE_MS, self._run_live_filter)

    def _run_live_filter(self):
        self._filter_after_id = None
        self.refresh()

    def _build_table(self, parent):
        # Plain tk.PanedWindow (not ttk) here specifically so the sash has a real,
        # generously-sized grab area (sashwidth) - ttk's themed sash is only a few
        # pixels wide with no way to widen its hit area portably.
        paned = tk.PanedWindow(
            parent, orient="horizontal", sashwidth=8, sashrelief="raised", sashpad=1,
            showhandle=False, bd=0,
        )
        paned.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.Frame(paned)
        paned.add(left, minsize=200, stretch="always")

        title_row = ttk.Frame(left)
        title_row.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(title_row, text="Data selector", font=("", 10, "bold")).pack(side="left")
        ttk.Button(title_row, text="Clear selection", command=self.on_clear_selection).pack(
            side="left", padx=(10, 4)
        )
        ttk.Button(title_row, text="Delete selected", command=self.on_delete_selected).pack(
            side="left", padx=4
        )
        ttk.Button(title_row, text="Deleted files...", command=self.on_show_deleted_files).pack(
            side="left", padx=4
        )
        ttk.Button(title_row, text="Justify columns", command=self.on_justify_columns).pack(
            side="left", padx=4
        )
        ttk.Button(title_row, text="Clear filters", command=self.on_clear_filters).pack(
            side="left", padx=4
        )
        ttk.Button(title_row, text="Copy table", command=self.on_copy_selector_table).pack(
            side="left", padx=4
        )
        ttk.Button(title_row, text="Export as CSV...", command=self.on_export_selector_csv).pack(
            side="left", padx=4
        )

        ttk.Separator(left, orient="horizontal").pack(fill="x", padx=4, pady=(0, 4))

        self._build_selection_actions_row(left)
        self._build_filters(left)

        # The left side is itself split vertically: the run tree + always-visible
        # Notes box on top, a collapsible raw-header/peaks inspector below it - the
        # sash between them is the "drag bar" that brings the inspector back up.
        vpaned = ttk.PanedWindow(left, orient="vertical")
        vpaned.pack(fill="both", expand=True)

        tree_section = ttk.Frame(vpaned)
        vpaned.add(tree_section, weight=4)

        tree_frame = ttk.Frame(tree_section)
        tree_frame.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(tree_frame, columns=COLUMNS, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Round", command=self._on_group_sort_click, anchor="w")
        self.tree.column("#0", width=130, stretch=False, anchor="w")
        for col in COLUMNS:
            # Every column is sortable for consistency, even the ones with little
            # practical value (sel, excluded) - no need to special-case those.
            self.tree.heading(col, command=lambda c=col: self._on_tree_sort_click(c), anchor="w")
            if col == "sel":
                width, stretch = 40, False
            elif col == "highlight":
                width, stretch = 26, False
            elif col in ("run_number", "injection_date"):
                width, stretch = 70, False
            else:
                width, stretch = 140, True
            self.tree.column(col, width=width, stretch=stretch, anchor="w")
        self.tree.tag_configure("duplicate", background="#fff2cc")
        self.tree.tag_configure("excluded", foreground="#999999")
        self.tree.tag_configure("date", background="#e8eef7")
        # Placed last wherever it's applied so it wins over duplicate/excluded shading -
        # a checked row should always be visually obvious.
        self.tree.tag_configure("selected", background="#bdddff")
        # Several columns are narrower than their real name (e.g. "acq_method",
        # "analysis_method", "duplicate_of" all get clipped at a practical width) -
        # a hover tooltip spells the full name back out instead of guessing from a
        # truncated fragment.
        bind_treeview_heading_tooltip(
            self.tree,
            lambda col_id: "Round" if col_id == "#0" else COLUMN_FULL_LABELS.get(
                COLUMNS[int(col_id.lstrip("#")) - 1], None
            ) if col_id.lstrip("#").isdigit() else None,
        )

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._on_tree_vscroll)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self._on_tree_hscroll)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        bind_fast_hscroll(self.tree)
        self.tree.bind("<Configure>", lambda _e: self._update_highlight_overlays(), add="+")
        self.tree.bind(
            "<MouseWheel>", lambda _e: self.after(1, self._update_highlight_overlays), add="+"
        )
        self.tree.bind(
            "<Shift-MouseWheel>", lambda _e: self.after(1, self._update_highlight_overlays), add="+"
        )
        # Collapsing/expanding a date/round folder (the disclosure triangle) changes
        # which rows are actually visible on screen without going through refresh()
        # or a scroll/resize event - without this, a highlight chip belonging to a
        # row inside the collapsed group keeps floating in its last on-screen spot
        # instead of disappearing, since .place() doesn't auto-hide just because the
        # tree row it was covering collapsed underneath it. after(1, ...) so this
        # runs once Tk has actually finished collapsing/expanding, not before.
        self.tree.bind("<<TreeviewOpen>>", lambda _e: self.after(1, self._update_highlight_overlays), add="+")
        self.tree.bind("<<TreeviewClose>>", lambda _e: self.after(1, self._update_highlight_overlays), add="+")

        self.tree.bind("<ButtonPress-1>", self.on_tree_press)
        self.tree.bind("<B1-Motion>", self.on_tree_drag)
        self.tree.bind("<Double-1>", self.on_row_double_click)
        self.tree.bind("<Button-3>", self.on_row_right_click)

        # Shift held is read from event.state inside the handlers, so binding the bare
        # key (not "<Shift-Up>" etc. separately) is enough to catch both cases.
        self.tree.bind("<Up>", self.on_key_up)
        self.tree.bind("<Down>", self.on_key_down)
        self.tree.bind("<Prior>", self.on_key_pageup)
        self.tree.bind("<Next>", self.on_key_pagedown)
        self.tree.bind("<space>", self.on_key_space)
        self.tree.bind("<Control-z>", self.on_undo_selection)

        self._context_menu = tk.Menu(self, tearoff=0)

        self._build_notes_bar(tree_section)

        inspector_section = ttk.Frame(vpaned)
        vpaned.add(inspector_section, weight=1)

        self._inspector_collapsed = False
        handle_bar = ttk.Frame(inspector_section, height=18)
        handle_bar.pack(fill="x", side="top")
        handle_bar.pack_propagate(False)

        # Plain, non-clickable label - just says what's below, isn't the control.
        ttk.Label(handle_bar, text="Raw inspector", foreground="#999999").pack(
            side="left", padx=(6, 0)
        )

        # The actual toggle: a small icon-only affordance (not a text button) - a
        # rounded pill with a direction arrow, loosely modeled on the collapse tab
        # on the edge of LibreOffice/OpenOffice's sidebar. Only this element gets the
        # hand cursor; the label above stays plain since it isn't clickable itself.
        # Provisional look - likely to change once we settle on something better.
        bg = ttk.Style().lookup("TFrame", "background") or self.cget("bg")
        pill = tk.Canvas(handle_bar, width=26, height=14, highlightthickness=0, bg=bg, cursor="hand2")
        pill.pack(side="right", padx=(0, 8))
        pill.bind("<Button-1>", self._toggle_inspector)
        self._inspector_pill = pill
        self._draw_inspector_pill()

        self.detail = tk.Text(inspector_section, height=10, wrap="none", state="disabled")
        self.detail.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self._vpaned = vpaned
        self._inspector_height = 160  # approx. height to open at
        self._handle_bar_height = 18

        self.data_viewer = DataViewerPanel(paned, self)
        paned.add(self.data_viewer, minsize=200, stretch="always")

        self._paned = paned
        self._side_panel_width = 840  # approx. width to open at - wide enough for a pivot table

    def _toggle_inspector(self, _event=None):
        self.update_idletasks()
        if self._inspector_collapsed:
            self._vpaned.sashpos(0, self._vpaned.winfo_height() - self._inspector_height)
            self._inspector_collapsed = False
        else:
            # Collapse to just the handle bar's height, not all the way to 0 - so it
            # stays visible/clickable to bring the inspector back up.
            self._vpaned.sashpos(0, self._vpaned.winfo_height() - self._handle_bar_height)
            self._inspector_collapsed = True
        self._draw_inspector_pill()

    def _draw_inspector_pill(self):
        pill = self._inspector_pill
        pill.delete("all")
        w, h = 26, 14
        r = h / 2
        # Rounded pill: two end-caps plus a filling rectangle.
        pill.create_oval(0, 0, h, h, fill="#bbbbbb", outline="")
        pill.create_oval(w - h, 0, w, h, fill="#bbbbbb", outline="")
        pill.create_rectangle(r, 0, w - r, h, fill="#bbbbbb", outline="")
        arrow = "▾" if not self._inspector_collapsed else "▴"
        pill.create_text(w / 2, h / 2, text=arrow, fill="#555555", font=("", 7))

    def _build_notes_bar(self, parent):
        notes_bar = ttk.Frame(parent)
        notes_bar.pack(side="bottom", fill="x", padx=4, pady=(4, 4))

        ttk.Label(notes_bar, text="Notes:").pack(side="left", anchor="n", pady=(3, 0))

        text_frame = ttk.Frame(notes_bar)
        text_frame.pack(side="left", fill="both", expand=True, padx=(6, 6))
        # relief="flat" + a light highlight border (rather than the default heavy
        # black solid relief) so this plain tk.Text reads like every other themed
        # input box instead of standing out.
        self.notes_text = tk.Text(
            text_frame, height=NOTES_MIN_LINES, wrap="word", relief="flat",
            highlightthickness=1, highlightbackground="#a0a0a0", highlightcolor="#a0a0a0",
        )
        self.notes_text.pack(fill="both", expand=True)
        self.notes_text.bind("<<Modified>>", self._on_notes_text_modified)

        btn_col = ttk.Frame(notes_bar)
        btn_col.pack(side="left", anchor="n")
        ttk.Button(btn_col, text="Save note", command=self.on_save_notes).pack()
        self.notes_status_label = ttk.Label(btn_col, text="", foreground="#2a7a2a")
        self.notes_status_label.pack()

    def _on_notes_text_modified(self, _event=None):
        if not self.notes_text.edit_modified():
            return
        self.notes_text.edit_modified(False)
        self._resize_notes_text()

    def _resize_notes_text(self):
        """Grow/shrink the notes box to fit its current content (clamped between
        NOTES_MIN_LINES and NOTES_MAX_LINES), nudging the sash between the tree
        section and the inspector so the inspector - not the tree - absorbs the
        size change.

        Line count is computed manually via font.measure() rather than Text's own
        `count ... displaylines` - the latter only reflects real wrapping once the
        widget has actually been drawn on screen by a window manager, which makes it
        unreliable to rely on right after a fast sequence of edits/window switches."""
        self.notes_text.update_idletasks()
        width_px = max(self.notes_text.winfo_width() - 8, 20)  # minus ~padding/border
        font_obj = tkfont.Font(font=self.notes_text.cget("font"))
        text = self.notes_text.get("1.0", "end-1c")
        wrapped_lines = sum(_wrapped_line_count(font_obj, para, width_px) for para in text.split("\n"))
        needed = min(max(wrapped_lines, NOTES_MIN_LINES), NOTES_MAX_LINES)
        current = int(self.notes_text.cget("height"))
        if needed == current:
            return
        # Apply the height change and the compensating sash move together, before
        # any forced reflow - doing an update_idletasks() between the two would let
        # the tree visibly shrink for one frame and then jump back, since pack would
        # reclaim the tree's space before the sash had a chance to grow to compensate.
        line_px = font_obj.metrics("linespace")
        self.notes_text.configure(height=needed)
        try:
            pos = self._vpaned.sashpos(0)
            self._vpaned.sashpos(0, pos + (needed - current) * line_px)
        except tk.TclError:
            pass  # sash not laid out yet (e.g. during initial construction)

    # -- data ---------------------------------------------------------
    def current_filters(self):
        selected_gases = self.gas_filter.selected()
        # Compare against the filter widget's own full option set (real + baseline
        # He-channel gases it always offers), not just db.list_gases() - otherwise
        # "every gas checked" would never register as true once the widget started
        # offering gases beyond what's actually been ingested, silently engaging a
        # gas filter (and hiding no-peak "blank" runs) even by default.
        all_checked = set(selected_gases) == set(self.gas_filter.vars.keys())
        return {
            "sample_name": self.sample_name_var.get().strip() or None,
            "gases": None if all_checked else selected_gases,
            "date_from": self.date_from_var.get() if self.date_from_enabled.get() else None,
            # injection_date is a full timestamp - a bare "YYYY-MM-DD" upper bound
            # would exclude every run on that day (their timestamps sort *after*
            # the bare date string), so extend it to the end of that day.
            "date_to": f"{self.date_to_var.get()}T23:59:59" if self.date_to_enabled.get() else None,
            "show_duplicates": self.show_duplicates_var.get(),
            "show_excluded": self.show_excluded_var.get(),
        }

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.update_load_pending_indicator()

        # Refreshed on every call (cheap - small tables) so newly-ingested dates/
        # gases, or a standard newly linked via the pressure wizard, show up in
        # these dropdowns without needing a dedicated hook at every call site that
        # could change them - set_gases preserves existing check-state for gases
        # that persist, and the date comboboxes just get a fresh values list.
        self.gas_filter.set_gases(db.list_gases(self.conn), db.get_gas_channels(self.conn))
        # The date comboboxes only exist while the Dates popup is actually open -
        # its content is rebuilt fresh (and destroyed on close) every time, like
        # every ActionsPopup - so there may be nothing to refresh values against.
        if hasattr(self, "date_from_combo") and self.date_from_combo.winfo_exists():
            date_options = self._load_date_options()
            self.date_from_combo.configure(values=date_options)
            self.date_to_combo.configure(values=date_options)
        self.standard_filter.set_options(self._load_standard_options())

        runs = db.query_runs(self.conn, **self.current_filters())
        run_standards = db.get_run_standards(self.conn)
        allowed_standards = set(self.standard_filter.selected())
        runs = [
            r for r in runs
            if (run_standards.get(r["run_id"]) or "(none)") in allowed_standards
        ]

        self._run_ids_in_view = []
        self._base_run_tags = {}

        # Group by round when a run has one assigned (a round can span multiple
        # calendar days on purpose - collapsing by date would split it back apart);
        # a run with no round falls back to the old per-date grouping, same as
        # before rounds existed.
        groups = {}
        for r in runs:
            if r["round_id"] is not None:
                group_key = ("round", r["round_id"])
            else:
                group_key = ("date", (r["injection_date"] or "")[:10] or "Unknown date")
            groups.setdefault(group_key, []).append(r)

        round_names = {rr["round_id"]: rr["name"] for rr in db.list_run_rounds(self.conn)}

        def group_sort_value(key):
            # Comparable across both kinds of group, so date groups and round
            # groups interleave in chronological order rather than every round
            # group being sorted separately from every date group.
            return min((r["injection_date"] or "") for r in groups[key])

        for group_key in sorted(groups, key=group_sort_value, reverse=self._tree_group_reverse):
            kind, ident = group_key
            # Sort only within this group - groups are never mixed together, each
            # stays a self-contained block.
            rows = sorted(groups[group_key], key=self._tree_sort_key, reverse=self._tree_sort_reverse)
            if kind == "round":
                group_iid = f"group-round-{ident}"
                label = round_names.get(ident, f"Round {ident}")
            else:
                group_iid = f"group-date-{ident}"
                label = ident
            all_selected = all(r["run_id"] in self.selected_run_ids for r in rows)
            self.tree.insert(
                "", "end", iid=group_iid,
                text=f"{label}  ({len(rows)} runs)",
                values=(CHECK_ON if all_selected else CHECK_OFF, "", "", "", "", "", "", "", ""),
                open=True, tags=("date",),
            )
            for r in rows:
                base_tags = ["run"]
                if r["duplicate_of"] is not None:
                    base_tags.append("duplicate")
                if r["excluded"]:
                    base_tags.append("excluded")
                self._base_run_tags[r["run_id"]] = tuple(base_tags)

                run_iid = f"run-{r['run_id']}"
                selected = r["run_id"] in self.selected_run_ids
                glyph = CHECK_ON if selected else CHECK_OFF
                tags = base_tags + ["selected"] if selected else base_tags
                self.tree.insert(
                    group_iid, "end", iid=run_iid,
                    values=(glyph, "", r["sample_name"], r["injection_date"],
                            r["run_number"] if r["run_number"] is not None else "",
                            r["acq_method"], r["analysis_method"],
                            r["duplicate_of"] or "", "yes" if r["excluded"] else ""),
                    tags=tags,
                )
                self._run_ids_in_view.append(r["run_id"])

        self._update_tree_headings()
        self._update_selection_label()
        self._update_highlight_overlays()

    def _on_tree_vscroll(self, *args):
        self.tree.yview(*args)
        self._update_highlight_overlays()

    def _on_tree_hscroll(self, *args):
        self.tree.xview(*args)
        self._update_highlight_overlays()

    def _update_highlight_overlays(self):
        """(Re)places a small colored-chip label over every currently-visible run's
        "highlight" cell - the only way to color part of a ttk.Treeview row, since
        the widget has no native per-cell background (same technique the Analysis
        tab's out-of-range cells use)."""
        for label in self._highlight_overlays.values():
            label.destroy()
        self._highlight_overlays = {}
        if not hasattr(self, "tree"):
            return
        self.tree.update_idletasks()
        colors = db.get_run_highlight_colors(self.conn)
        if not colors:
            return
        for group_iid in self.tree.get_children(""):
            for run_iid in self.tree.get_children(group_iid):
                run_id = int(run_iid.split("-", 1)[1])
                color = colors.get(run_id)
                if not color:
                    continue
                bbox = self.tree.bbox(run_iid, "highlight")
                if not bbox:
                    continue
                x, y, w, h = bbox
                label = tk.Label(self.tree, bg=color, cursor="hand2")
                label.place(x=x, y=y, width=w, height=h)
                label.bind(
                    "<Button-1>",
                    lambda e, rid=run_id: self._open_highlight_picker(rid, e.x_root, e.y_root),
                )
                self._highlight_overlays[run_id] = label

    def _tree_sort_key(self, row):
        col = self._tree_sort_column
        if col == "sel":
            return row["run_id"] in self.selected_run_ids
        if col in ("run_number", "duplicate_of"):
            val = row[col]
            return (val is None, val or 0)
        if col == "excluded":
            return row["excluded"]
        return row[col] or ""

    def _on_tree_sort_click(self, col):
        if self._tree_sort_column == col:
            self._tree_sort_reverse = not self._tree_sort_reverse
        else:
            self._tree_sort_column = col
            self._tree_sort_reverse = False
        self.refresh()

    def _on_group_sort_click(self):
        self._tree_group_reverse = not self._tree_group_reverse
        self.refresh()

    def on_justify_columns(self):
        justify_columns(self.tree, list(COLUMNS))

    def _column_name_at(self, event_x):
        """Resolves identify_column()'s "#N" back to a real column name by asking
        identify_column itself rather than assuming a fixed index - a hardcoded
        "#N" guess (this tree's own COLUMNS index) was found to disagree with what
        identify_column actually reports for indented child rows, which silently
        broke a hardcoded check here before."""
        col_id = self.tree.identify_column(event_x)
        try:
            return COLUMNS[int(col_id.lstrip("#")) - 1]
        except (ValueError, IndexError):
            return None

    def _selector_table_rows(self):
        """Mirrors exactly what's on screen right now (folder/round header rows and
        all - not just checked/filtered-out rows), same shape used by DataViewerPanel's
        own copy/export."""
        header = ["Round"] + list(COLUMNS)
        rows = [header]
        for top_iid in self.tree.get_children(""):
            rows.append([self.tree.item(top_iid, "text")] + list(self.tree.item(top_iid, "values")))
            for child_iid in self.tree.get_children(top_iid):
                rows.append([self.tree.item(child_iid, "text")] + list(self.tree.item(child_iid, "values")))
        return rows

    def on_copy_selector_table(self):
        rows = self._selector_table_rows()
        if len(rows) <= 1:
            return
        self.clipboard_clear()
        self.clipboard_append(export.rows_to_tsv(rows))

    def on_export_selector_csv(self):
        rows = self._selector_table_rows()
        if len(rows) <= 1:
            messagebox.showwarning("Nothing to export", "The table is empty.")
            return
        out_path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV file", "*.csv")])
        if not out_path:
            return
        export.write_csv(rows, out_path)
        messagebox.showinfo("Export complete", f"Wrote {out_path}")

    def _update_tree_headings(self):
        for col in COLUMNS:
            label = col
            if col == self._tree_sort_column:
                label += " ▼" if self._tree_sort_reverse else " ▲"
            self.tree.heading(col, text=label)
        group_label = "Round"
        group_label += " ▼" if self._tree_group_reverse else " ▲"
        self.tree.heading("#0", text=group_label)

    # -- selection ("scratchpad") --------------------------------------
    def on_tree_press(self, event):
        # Dragging a column-header border to resize it must never be mistaken for a
        # row drag-select - without this, the pointer drifting even slightly over a
        # row while still resizing (holding the mouse button down) could leave a
        # stale drag-select anchor around, which on_tree_drag then acted on -
        # scrolling to and selecting rows the user never meant to touch.
        if self.tree.identify_region(event.x, event.y) == "separator":
            self._drag_anchor_run_id = None
            return
        # Let the expand/collapse arrow behave normally; don't treat it as a selection click.
        if self.tree.identify_element(event.x, event.y) == "Treeitem.indicator":
            self._drag_anchor_run_id = None
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            self._drag_anchor_run_id = None
            return
        if iid.startswith("group-"):
            # ttk's native expand/collapse triangle is a tiny hitbox (already handled
            # above via the Treeitem.indicator check) - enlarge it to a full square as
            # tall as the row itself at the left edge, so clicking anywhere in that
            # square toggles expand/collapse instead of requiring the pixel-precise
            # triangle. Clicks further right than that square still bulk-select the
            # group, unchanged.
            bbox = self.tree.bbox(iid)
            row_height = bbox[3] if bbox else 20
            if event.x <= row_height:
                self.tree.item(iid, open=not self.tree.item(iid, "open"))
                self._drag_anchor_run_id = None
                return
            self._push_selection_undo()
            self._toggle_group(iid)
            self._drag_anchor_run_id = None  # bulk group toggle isn't a drag anchor
            children = self.tree.get_children(iid)
            if children:
                rid = int(children[0].split("-", 1)[1])
                self._anchor_run_id = rid
                self._focus_run_id = rid
            return
        if not iid.startswith("run-"):
            return

        run_id = int(iid.split("-", 1)[1])
        # A blank "highlight" cell (no chip - see _update_highlight_overlays for the
        # colored-chip case, which binds its own click) has no overlay widget to
        # intercept the click, so it's handled here instead - clicking anywhere in
        # that column, chip or not, opens the same swatch picker rather than
        # toggling the row's selection.
        if self._column_name_at(event.x) == "highlight":
            self._open_highlight_picker(run_id, event.x_root, event.y_root)
            self._drag_anchor_run_id = None
            return
        shift = bool(event.state & SHIFT_MASK)
        ctrl = bool(event.state & CTRL_MASK)
        self._push_selection_undo()
        if shift and ctrl and self._anchor_run_id is not None:
            # Ctrl+Shift: range toggle - if the row you clicked was already selected,
            # the whole range gets deselected; otherwise the whole range gets selected.
            self._toggle_range(self._anchor_run_id, run_id)
            self._drag_anchor_run_id = None
        elif shift and self._anchor_run_id is not None:
            # Plain Shift: range-select, additive, doesn't clear anything outside the range.
            self._select_range(self._anchor_run_id, run_id)
            self._drag_anchor_run_id = None
        else:
            # Plain click or Ctrl+click: toggle just this row (Ctrl needs no special
            # handling since that's already the default behavior here). Also snapshot
            # everything's current state so a drag from here can restore rows the
            # cursor passes back out of.
            self._drag_snapshot = {rid: (rid in self.selected_run_ids) for rid in self._flattened_run_ids()}
            was_selected = run_id in self.selected_run_ids
            self._toggle_run(run_id)
            self._anchor_run_id = run_id
            self._drag_anchor_run_id = run_id
            self._drag_target_state = not was_selected
        self._focus_run_id = run_id
        self._move_focus(run_id)

    def on_tree_drag(self, event):
        if self._drag_anchor_run_id is None:
            return
        if self.tree.identify_region(event.x, event.y) == "separator":
            return
        iid = self.tree.identify_row(event.y)
        if not iid or not iid.startswith("run-"):
            return
        run_id = int(iid.split("-", 1)[1])
        order = self._flattened_run_ids()
        if self._drag_anchor_run_id not in order or run_id not in order:
            return

        i, j = order.index(self._drag_anchor_run_id), order.index(run_id)
        lo, hi = min(i, j), max(i, j)
        in_range = set(order[lo:hi + 1])
        # Every row is either in the live range (forced to the target state) or restored
        # to its pre-drag snapshot - this is what makes dragging back out un-select rows
        # that were only ever "accidentally" swept in.
        changed = []
        for rid in order:
            desired = self._drag_target_state if rid in in_range else self._drag_snapshot.get(rid, False)
            if (rid in self.selected_run_ids) != desired:
                self._set_run_selected_quiet(rid, desired)
                changed.append(rid)
        if changed:
            self._sync_group_glyphs_for(changed)
            self._update_selection_label()
        self._focus_run_id = run_id
        self._move_focus(run_id)

    def _flattened_run_ids(self):
        result = []
        for group_iid in self.tree.get_children(""):
            for run_iid in self.tree.get_children(group_iid):
                result.append(int(run_iid.split("-", 1)[1]))
        return result

    def _select_range(self, anchor_run_id, target_run_id):
        order = self._flattened_run_ids()
        if anchor_run_id not in order or target_run_id not in order:
            return
        i, j = order.index(anchor_run_id), order.index(target_run_id)
        lo, hi = min(i, j), max(i, j)
        rids = order[lo:hi + 1]
        for rid in rids:
            self._set_run_selected_quiet(rid, True)
        self._sync_group_glyphs_for(rids)
        self._update_selection_label()

    def _toggle_range(self, anchor_run_id, target_run_id):
        order = self._flattened_run_ids()
        if anchor_run_id not in order or target_run_id not in order:
            return
        desired = target_run_id not in self.selected_run_ids
        i, j = order.index(anchor_run_id), order.index(target_run_id)
        lo, hi = min(i, j), max(i, j)
        rids = order[lo:hi + 1]
        for rid in rids:
            self._set_run_selected_quiet(rid, desired)
        self._sync_group_glyphs_for(rids)
        self._update_selection_label()

    def _move_focus(self, run_id):
        run_iid = f"run-{run_id}"
        self.tree.see(run_iid)
        self.tree.focus(run_iid)
        self.tree.selection_set(run_iid)

    def _toggle_run(self, run_id):
        self._set_run_selected(run_id, run_id not in self.selected_run_ids)

    def _apply_row_tags(self, run_id):
        base = self._base_run_tags.get(run_id, ("run",))
        tags = base + ("selected",) if run_id in self.selected_run_ids else base
        self.tree.item(f"run-{run_id}", tags=tags)

    def _set_run_selected(self, run_id, selected):
        self._set_run_selected_quiet(run_id, selected)
        group_iid = self.tree.parent(f"run-{run_id}")
        if group_iid:
            self._sync_group_glyph(group_iid)
        self._update_selection_label()

    def _set_run_selected_quiet(self, run_id, selected):
        """Same as _set_run_selected but skips the group-glyph resync and the
        selection-label/Data Viewer refresh - a caller toggling a whole batch of rows
        (group-select, range-select, drag-select, undo) must call
        _sync_group_glyphs_for(...) and _update_selection_label() exactly once after
        the whole batch instead of once per row. Per-row group-glyph resync is O(n)
        over the group each time (O(n^2) for a batch of n), and _update_selection_label
        triggers two DB queries plus a full Data Viewer re-render - doing that once per
        row is what made selecting a large round very slow."""
        if selected:
            self.selected_run_ids.add(run_id)
        else:
            self.selected_run_ids.discard(run_id)
        run_iid = f"run-{run_id}"
        self.tree.set(run_iid, "sel", CHECK_ON if selected else CHECK_OFF)
        self._apply_row_tags(run_id)

    def _sync_group_glyphs_for(self, run_ids):
        groups = set()
        for run_id in run_ids:
            group_iid = self.tree.parent(f"run-{run_id}")
            if group_iid:
                groups.add(group_iid)
        for group_iid in groups:
            self._sync_group_glyph(group_iid)

    def _toggle_group(self, group_iid):
        child_iids = self.tree.get_children(group_iid)
        run_ids = [int(c.split("-", 1)[1]) for c in child_iids]
        select_all = not all(rid in self.selected_run_ids for rid in run_ids)
        for rid in run_ids:
            self._set_run_selected_quiet(rid, select_all)
        self._sync_group_glyph(group_iid)
        self._update_selection_label()

    def _sync_group_glyph(self, group_iid):
        child_iids = self.tree.get_children(group_iid)
        run_ids = [int(c.split("-", 1)[1]) for c in child_iids]
        all_selected = bool(run_ids) and all(rid in self.selected_run_ids for rid in run_ids)
        self.tree.set(group_iid, "sel", CHECK_ON if all_selected else CHECK_OFF)

    def _update_selection_label(self):
        self.selection_label.config(text=f"{len(self.selected_run_ids)} runs selected")
        self.data_viewer.refresh(self.selected_run_ids)

    # -- keyboard navigation ---------------------------------------------
    def on_key_up(self, event):
        self._navigate(-1, shift=bool(event.state & SHIFT_MASK), ctrl=bool(event.state & CTRL_MASK))
        return "break"

    def on_key_down(self, event):
        self._navigate(1, shift=bool(event.state & SHIFT_MASK), ctrl=bool(event.state & CTRL_MASK))
        return "break"

    def on_key_pageup(self, event):
        self._navigate_page(-1, shift=bool(event.state & SHIFT_MASK), ctrl=bool(event.state & CTRL_MASK))
        return "break"

    def on_key_pagedown(self, event):
        self._navigate_page(1, shift=bool(event.state & SHIFT_MASK), ctrl=bool(event.state & CTRL_MASK))
        return "break"

    def on_key_space(self, event):
        if self._focus_run_id is not None:
            self._push_selection_undo()
            self._toggle_run(self._focus_run_id)
            self._anchor_run_id = self._focus_run_id
        return "break"

    def _navigate(self, delta, shift, ctrl):
        order = self._flattened_run_ids()
        if not order:
            return
        current = self._focus_run_id if self._focus_run_id in order else order[0]
        idx = order.index(current)
        new_run_id = order[max(0, min(len(order) - 1, idx + delta))]
        self._apply_navigation(current, new_run_id, shift, ctrl)

    def _navigate_page(self, delta, shift, ctrl):
        order = self._flattened_run_ids()
        if not order:
            return
        current = self._focus_run_id if self._focus_run_id in order else order[0]
        dates = self.tree.get_children("")
        cur_date = self.tree.parent(f"run-{current}")
        if cur_date not in dates:
            return
        date_idx = max(0, min(len(dates) - 1, dates.index(cur_date) + delta))
        children = self.tree.get_children(dates[date_idx])
        if not children:
            return
        # Landing on the far edge of the target date (bottom when paging down, top when
        # paging up) so a held Shift sweeps in every run of that whole date group.
        new_iid = children[-1] if delta > 0 else children[0]
        new_run_id = int(new_iid.split("-", 1)[1])
        self._apply_navigation(current, new_run_id, shift, ctrl)

    def _apply_navigation(self, current, new_run_id, shift, ctrl):
        if shift and ctrl:
            if self._anchor_run_id is None:
                self._anchor_run_id = current
            self._push_selection_undo()
            self._toggle_range(self._anchor_run_id, new_run_id)
        elif shift:
            if self._anchor_run_id is None:
                self._anchor_run_id = current
            self._push_selection_undo()
            self._select_range(self._anchor_run_id, new_run_id)
        else:
            self._anchor_run_id = new_run_id
        self._focus_run_id = new_run_id
        self._move_focus(new_run_id)

    def on_clear_selection(self):
        self._push_selection_undo()
        self.selected_run_ids.clear()
        self.refresh()

    def on_delete_selected(self):
        if not self.selected_run_ids:
            messagebox.showwarning("Nothing selected", "No runs are selected.")
            return

        run_ids = list(self.selected_run_ids)
        placeholders = ",".join("?" for _ in run_ids)
        non_duplicate_count = self.conn.execute(
            f"SELECT COUNT(*) FROM runs WHERE run_id IN ({placeholders}) AND duplicate_of IS NULL",
            run_ids,
        ).fetchone()[0]

        message = (
            f"Delete {len(run_ids)} run(s) from the database? The raw CSVs on disk are never touched, "
            'and each entry can be recovered from "Deleted files..." (Restore, then re-ingest) - but it '
            "won't come back on its own."
        )
        if non_duplicate_count:
            message += (
                f"\n\n{non_duplicate_count} of these are NOT flagged as duplicates - "
                f"they are the only copy of that data on record."
            )
        if not messagebox.askyesno("Delete selected runs", message):
            return

        deleted = db.delete_runs(self.conn, run_ids)
        self.selected_run_ids.clear()
        self.refresh()
        self.update_pressure_button()
        messagebox.showinfo(
            "Deleted", f'Deleted {deleted} run(s). Recoverable from "Deleted files..." if needed.'
        )

    def on_show_deleted_files(self):
        DeletedFilesWindow(self)

    def on_show_standards(self):
        StandardsWindow(self)

    def on_load_into_models(self):
        if not self.selected_run_ids:
            messagebox.showwarning("Nothing selected", "No runs are selected.")
            return
        self.models_panel.load_runs(self.selected_run_ids)
        self.analysis_panel.load_runs(self.selected_run_ids)
        self.notebook.select(self.models_tab)

    def on_add_to_loaded_selection(self):
        """Like "Load selected into Models", but merges the current checkbox
        selection into whatever's already loaded instead of replacing it - for
        adding a few more runs without having to re-check everything from scratch."""
        if not self.selected_run_ids:
            messagebox.showwarning("Nothing selected", "No runs are selected.")
            return
        merged = set(self.models_panel._run_ids) | set(self.selected_run_ids)
        self.models_panel.load_runs(merged)
        self.analysis_panel.load_runs(merged)
        self.notebook.select(self.models_tab)

    def on_assign_to_round(self):
        if not self.selected_run_ids:
            messagebox.showwarning("Nothing selected", "No runs are selected.")
            return
        RoundAssignDialog(self, self.selected_run_ids)

    # -- selection undo ---------------------------------------------------
    def _push_selection_undo(self):
        self._selection_undo_stack.append(frozenset(self.selected_run_ids))
        if len(self._selection_undo_stack) > 50:
            self._selection_undo_stack.pop(0)

    def on_undo_selection(self, event=None):
        if not self._selection_undo_stack:
            return "break"
        target = self._selection_undo_stack.pop()
        current = set(self.selected_run_ids)
        removed = current - target
        added = target - current
        for rid in removed:
            self._set_run_selected_quiet(rid, False)
        for rid in added:
            self._set_run_selected_quiet(rid, True)
        changed = removed | added
        if changed:
            self._sync_group_glyphs_for(changed)
            self._update_selection_label()
        return "break"

    def on_clear_filters(self):
        self.sample_name_var.set("")
        self.gas_filter.select_all()
        self.date_from_enabled.set(False)
        self.date_to_enabled.set(False)
        self.standard_filter.select_all()
        self.show_duplicates_var.set(False)
        self.show_excluded_var.set(True)
        self.refresh()

    # -- actions --------------------------------------------------------
    def on_choose_watch_folder(self):
        folder = filedialog.askdirectory(title="Select folder to watch for new raw CSVs", initialdir="RAWs")
        if not folder:
            return
        self._watched_folder = folder
        db.set_setting(self.conn, "watched_folder", folder)
        self._watch_paused = False
        self.watch_toggle_button.config(text="Pause watching")
        self._run_ingest_pass(manual=True)

    def on_ingest_now(self):
        self._run_ingest_pass(manual=True)

    def on_toggle_watch_pause(self):
        self._watch_paused = not self._watch_paused
        self.watch_toggle_button.config(text="Resume watching" if self._watch_paused else "Pause watching")
        self._update_watch_status(None, manual=False)

    def on_watch_and_assign_to_round(self):
        """Starts (or re-targets) watching, auto-assigning every newly-ingested run
        to a chosen existing round until local midnight, at which point it clears
        itself and watching auto-pauses (see _poll_tick)."""
        RoundPickerDialog(self, self._on_watch_and_assign_round_picked)

    def _on_watch_and_assign_round_picked(self, round_id, round_name):
        self._watch_and_assign_round_id = round_id
        self._watch_and_assign_round_name = round_name
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        self._watch_and_assign_until = datetime.datetime.combine(tomorrow, datetime.time())
        self._watch_paused = False
        self.watch_toggle_button.config(text="Pause watching")
        if not self._watched_folder:
            self.on_choose_watch_folder()
        else:
            self._run_ingest_pass(manual=True)

    def _poll_tick(self):
        if (
            self._watch_and_assign_until is not None
            and datetime.datetime.now() >= self._watch_and_assign_until
        ):
            self._watch_and_assign_round_id = None
            self._watch_and_assign_round_name = None
            self._watch_and_assign_until = None
            self._watch_paused = True
            self.watch_toggle_button.config(text="Resume watching")
            self._update_watch_status(None, manual=False)
        if not self._watch_paused and self._watched_folder:
            self._run_ingest_pass(manual=False)
        self.after(POLL_INTERVAL_MS, self._poll_tick)

    def _run_ingest_pass(self, manual):
        """Runs one ingest pass against the watched folder. ingest.py opens and
        cleanly closes its own separate sqlite3 connection every call - self.conn is
        never closed/reopened here (unlike the old one-shot on_ingest), since every
        child window (PressureEntryWindow, StandardsWindow, ModelsPanel,
        DataViewerPanel) holds a direct reference to it captured at construction
        time; reassigning it on a timer would silently break any of those still
        open. A plain SELECT on self.conn after another connection's commit already
        sees the latest data, so there's nothing to refresh on the connection itself.

        Same-identity groups (ingest.py detects, never merges - see
        summary["same_identity_groups"]) are handled differently depending on mode:
        live watch + round-assign is "the scientist is mid-run", where a reprocessed
        file turning up is the routine case (could happen on every run), so it's
        auto-merged immediately using the Repair tool's own defaults - no popup, no
        queueing, just a log line (see _update_watch_status). Every other ingestion
        path leaves same-identity groups alone entirely; they queue up on the Load
        Data tab's Repair sub-tab instead, since outside a live session there's no
        strong signal a mismatch is "the same run correcting itself" rather than a
        genuine conflict worth a human's look."""
        folder = self._watched_folder
        if not folder:
            if manual:
                self.on_choose_watch_folder()
            return
        summary = run_ingest(folder, self.db_path)
        live_round_mode = self._watch_and_assign_round_id is not None
        finalize_ids = list(summary["new_run_ids"])
        merge_log_lines = []

        if summary["same_identity_groups"]:
            if live_round_mode:
                loser_to_target = {}
                for group_run_ids in summary["same_identity_groups"]:
                    target_run_id = group_run_ids[0]
                    sample_row = self.conn.execute(
                        "SELECT sample_name FROM runs WHERE run_id = ?", (target_run_id,)
                    ).fetchone()
                    field_matrix = db.get_group_field_matrix(self.conn, group_run_ids)
                    selections = db.default_field_selections(field_matrix, group_run_ids)
                    db.merge_same_identity_group(self.conn, target_run_id, group_run_ids, selections)
                    for rid in group_run_ids:
                        if rid != target_run_id:
                            loser_to_target[rid] = target_run_id
                    sample_name = sample_row["sample_name"] if sample_row else "a run"
                    merge_log_lines.append(f"Auto-updated {sample_name} from a reprocessed file.")
                mapped_ids = [loser_to_target.get(rid, rid) for rid in finalize_ids]
                seen = set()
                finalize_ids = [rid for rid in mapped_ids if not (rid in seen or seen.add(rid))]
            else:
                grouped_ids = {rid for group in summary["same_identity_groups"] for rid in group}
                finalize_ids = [rid for rid in finalize_ids if rid not in grouped_ids]

        if live_round_mode:
            db.add_runs_to_round(self.conn, self._watch_and_assign_round_id, finalize_ids)
            db.finalize_runs(self.conn, finalize_ids)

        if summary["new"] or summary["duplicates"] or manual:
            self.refresh()
            self.update_pressure_button()
        self.update_load_pending_indicator()
        self._update_watch_status(summary, manual, merge_log_lines)
        if live_round_mode and finalize_ids:
            PressureEntryWindow(self, run_ids=finalize_ids, missing_only=True)

    def _should_log_persistent_issue(self, category, count):
        state = self._persistent_issue_log_state[category]
        if not count:
            state["last_at"] = None
            state["last_count"] = 0
            return False
        now = datetime.datetime.now()
        should_log = (
            state["last_at"] is None
            or count >= state["last_count"] + 10
            or now - state["last_at"] >= datetime.timedelta(days=1)
        )
        if should_log:
            state["last_at"] = now
            state["last_count"] = count
        return should_log

    def _update_watch_status(self, summary, manual, merge_log_lines=None):
        if summary is not None:
            self._last_ingest_errors = summary["errors"]
            self._last_missing_export = summary["missing_export"]
            self._last_missing_export_paths = summary["missing_export_paths"]
        if not self._watched_folder:
            self._watch_second_line = ""
        else:
            prefix = "Paused - " if self._watch_paused else ""
            if summary is None:
                self._watch_second_line = f"{prefix}not checked yet"
            else:
                now = datetime.datetime.now().strftime("%H:%M:%S")
                self._watch_second_line = (
                    f"{prefix}last checked {now}: "
                    f"{summary['new']} new, {summary['duplicates']} dup, "
                    f"{summary['needs_repair']} pending review, {summary['errors']} errors"
                )
                if summary["missing_export"]:
                    self._watch_second_line += (
                        f", {summary['missing_export']} run folder(s) missing their CSV export - see Help"
                    )
            if self._watch_and_assign_round_id is not None:
                self._watch_second_line += (
                    f" - auto-assigning new runs to \"{self._watch_and_assign_round_name}\" until midnight"
                )
            if merge_log_lines:
                self._watch_second_line += "\n" + "\n".join(merge_log_lines)
        if summary is not None:
            # Always call both (never short-circuited) so a cleared issue's state
            # resets even on a poll that has nothing else to report.
            log_errors = self._should_log_persistent_issue("errors", summary["errors"])
            log_missing_export = self._should_log_persistent_issue("missing_export", summary["missing_export"])
            if summary["new"] or summary["duplicates"] or summary["needs_repair"] or log_errors or log_missing_export:
                self._record_ingest_history(summary, merge_log_lines, log_errors, log_missing_export)
        self._refresh_watch_status_display()
        self._refresh_auto_versioning_caption()
        self.update_load_pending_indicator()
        self._refresh_missing_export_banner()
        if manual and summary is not None:
            # Silent auto-polls never interrupt with a modal - only an explicit
            # "Watch folder..."/"Ingest now" click gets the same confirmation the
            # old one-shot "Ingest folder..." button always gave.
            needs_repair_line = (
                f"Pending review (Duplicates tab): {summary['needs_repair']}\n"
                if summary["needs_repair"] and self._watch_and_assign_round_id is None else ""
            )
            missing_export_line = (
                f"Missing CSV export (see Help tab): {summary['missing_export']}\n"
                if summary["missing_export"] else ""
            )
            messagebox.showinfo(
                "Ingest complete",
                f"New runs: {summary['new']}\n"
                f"Flagged as duplicates: {summary['duplicates']}\n"
                f"{needs_repair_line}"
                f"Skipped (already ingested): {summary['skipped']}\n"
                f"Previously deleted, skipped: {summary['ignored']}\n"
                f"Failed to parse: {summary['errors']}\n"
                f"{missing_export_line}",
            )

    def _record_ingest_history(self, summary, merge_log_lines, log_errors, log_missing_export):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = []
        if summary["new"]:
            parts.append(f"{summary['new']} new")
        if summary["duplicates"]:
            parts.append(f"{summary['duplicates']} dup")
        if summary["needs_repair"]:
            parts.append(f"{summary['needs_repair']} pending review")
        # errors/missing_export are re-detected on every single poll for as long as
        # they stay unfixed (unlike the other counts above, which naturally stop
        # repeating once handled) - only actually add a line for them when the
        # once-a-day/every-10-more throttle in _should_log_persistent_issue says so,
        # or this list would fill up with the same message every 20 seconds.
        if log_errors:
            parts.append(f"{summary['errors']} errors")
        if log_missing_export:
            parts.append(f"{summary['missing_export']} missing CSV export")
        if not parts:
            return
        line = f"{now}  -  {', '.join(parts)}"
        if merge_log_lines:
            line += "  (" + "; ".join(merge_log_lines) + ")"
        self._ingest_history.insert(0, line)
        del self._ingest_history[200:]
        self._refresh_ingest_history_display()

    def _refresh_ingest_history_display(self):
        widget = getattr(self, "ingest_history_text", None)
        if widget is None:
            return
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", "\n".join(self._ingest_history))
        widget.config(state="disabled")

    def _refresh_auto_versioning_caption(self):
        label = getattr(self, "auto_versioning_label", None)
        if label is None:
            return
        if self._watch_and_assign_round_id is not None:
            label.config(
                text="Auto-versioning is on: a new file matching an existing run's sample and date in this "
                     "round will automatically replace its peak data with the newest version. Pressure/"
                     "standard/round stay as already entered."
            )
        else:
            label.config(text="")

    def _refresh_missing_export_banner(self):
        text_widget = getattr(self, "missing_export_text", None)
        if text_widget is None:
            return
        text_widget.config(state="normal")
        text_widget.delete("1.0", "end")
        if self._last_missing_export:
            paths = self._last_missing_export_paths
            shown = paths[:8]
            more = len(paths) - len(shown)
            text_widget.insert(
                "end",
                f"{self._last_missing_export} run folder(s) have raw acquisition files but no exported CSV - "
                "the Agilent method never produced a peak-table export for these. Right-click a file name "
                "below to open its folder; click anywhere else here for how to fix it.\n",
                ("intro",),
            )
            for i, path in enumerate(shown):
                text_widget.insert("end", path, (f"path-{i}", "path"))
                text_widget.insert("end", "\n", ("intro",))
            if more > 0:
                text_widget.insert("end", f"(+{more} more)\n", ("intro",))
            text_widget.config(height=min(len(shown) + 2, 9))
        else:
            text_widget.config(height=1)
        text_widget.config(state="disabled")

    def _refresh_watch_status_display(self, _event=None):
        """Renders the folder line right-justified against the label's own current
        width: the tail (the folder that actually matters) always stays intact,
        filled outward with as much of the parent path as fits, with the front
        elided ("...") once it no longer fits - re-run on every resize (bound to
        <Configure> on the label) so it keeps tracking the available width."""
        if not self._watched_folder:
            self.watch_status_label.config(text="Not watching a folder yet.")
            return
        width_px = max(self.watch_status_label.winfo_width(), 20)
        folder_line = self._elide_path_left(self._watched_folder, width_px)
        self.watch_status_label.config(text=f"{folder_line}\n{self._watch_second_line}")

    @staticmethod
    def _elide_path_left(path, max_width_px):
        font_obj = tkfont.nametofont("TkDefaultFont")
        if font_obj.measure(path) <= max_width_px:
            return path
        for cut in range(1, len(path)):
            candidate = "..." + path[cut:]
            if font_obj.measure(candidate) <= max_width_px:
                return candidate
        return "..."

    def on_enter_pressures(self):
        # Scoped to just whatever's checked in the Selector, by default - the
        # wizard's own Round filter (unchecked by default in this case) lets the
        # user widen the view to other unentered runs from there if they want more
        # than just the current selection.
        if self.selected_run_ids:
            PressureEntryWindow(
                self, run_ids=None, missing_only=False, preselected_run_ids=list(self.selected_run_ids)
            )
        else:
            PressureEntryWindow(self, run_ids=None, missing_only=True)

    def on_edit_selected_pressure(self, fallback_run_id=None):
        """Right-click "Edit selected run(s) in pressure entry..." - operates on
        whatever's checked in the Selector's own selection scratchpad; falls back to
        just the row that was right-clicked if nothing's checked, so it's still
        useful for a single quick edit without pre-checking first."""
        run_ids = list(self.selected_run_ids) if self.selected_run_ids else (
            [fallback_run_id] if fallback_run_id is not None else []
        )
        if not run_ids:
            messagebox.showwarning("Nothing selected", "No runs are selected.")
            return
        PressureEntryWindow(self, run_ids=None, missing_only=False, preselected_run_ids=run_ids)

    def update_pressure_button(self):
        # The "Enter pressures..." button no longer changes color when runs are
        # missing a pressure - kept as a callable no-op since many call sites
        # (ingest, merges, save) still call this after an action that could change
        # the missing-pressure count, and removing the method would mean editing
        # every one of them too.
        pass

    def _is_excluded(self, run_id):
        row = self.conn.execute("SELECT excluded FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return bool(row["excluded"]) if row else False

    def on_row_double_click(self, event):
        sel = self.tree.selection()
        if not sel or not sel[0].startswith("run-"):
            return
        run_id = int(sel[0].split("-", 1)[1])
        # run_number is deliberately double-click-only to edit (same convention as
        # the pressure wizard) - everywhere else on the row, double-click opens the
        # detail/inspector as it always has.
        if self._column_name_at(event.x) == "run_number":
            self._start_run_number_edit(sel[0], run_id)
            return
        self.show_run_detail(run_id)

    def _start_run_number_edit(self, row_iid, run_id):
        row = self.conn.execute(
            "SELECT round_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None or row["round_id"] is None:
            messagebox.showwarning(
                "No round assigned",
                "This run isn't part of a round yet - assign it to one first "
                "(\"Assign to round...\") before editing its run number by hand.",
            )
            return
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
            if not entry.winfo_exists():
                return
            raw = var.get().strip()
            old = self.tree.set(row_iid, "run_number")
            entry.destroy()
            if raw == old:
                return
            try:
                new_number = int(raw)
            except ValueError:
                messagebox.showerror("Invalid value", f"'{raw}' is not a whole number.")
                return
            # Same cascading-shift logic as the pressure wizard's run_number editor -
            # every other run in the round numbered higher than this run's old
            # number shifts by the same delta.
            db.set_run_number_manual(self.conn, run_id, new_number)
            self.refresh()

        def cancel(_event=None):
            if entry.winfo_exists():
                entry.destroy()

        entry.bind("<Return>", commit)
        entry.bind("<Escape>", cancel)
        entry.bind("<FocusOut>", commit)

    def show_run_detail(self, run_id):
        run = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            return
        self._detail_run_id = run_id
        peaks = db.get_peaks_for_run(self.conn, run_id)

        self.notes_text.delete("1.0", "end")
        self.notes_text.insert("1.0", run["notes"] or "")
        self.notes_text.edit_modified(False)
        self._resize_notes_text()
        self.notes_status_label.config(text="")

        # Read-only display, not an editor - briefly re-enable just to repopulate it.
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", "--- Raw header ---\n")
        self.detail.insert("end", run["raw_header_text"] + "\n\n")
        self.detail.insert("end", "--- Peaks ---\n")
        self.detail.insert("end", "gas\trt\trf\tarea\tamount\tconcentration\n")
        for p in peaks:
            self.detail.insert(
                "end", f"{p['gas']}\t{p['rt']}\t{p['rf']}\t{p['area']}\t{p['amount']}\t{p['concentration']}\n"
            )
        self.detail.configure(state="disabled")

    def on_save_notes(self):
        if self._detail_run_id is None:
            return
        new = self.notes_text.get("1.0", "end-1c").strip()
        db.set_run_notes(self.conn, self._detail_run_id, new)
        self.notes_status_label.config(text="Saved.")
        self.after(2000, lambda: self.notes_status_label.config(text=""))
        self.data_viewer.sync_notes_cell(self._detail_run_id, new)

    def sync_notes_display(self, run_id, new_text):
        """Called by DataViewerPanel when a run's notes cell is edited directly in
        the pivot table - keep the standalone Notes box in sync if it's currently
        showing that same run."""
        if self._detail_run_id != run_id:
            return
        self.notes_text.delete("1.0", "end")
        self.notes_text.insert("1.0", new_text)
        self.notes_text.edit_modified(False)
        self._resize_notes_text()

    def remove_from_selection(self, run_id):
        """Called by DataViewerPanel's per-row "x" - deselect a run directly from the
        Data Viewer without needing to go find and uncheck it in the main tree.
        Goes through the same undo-tracked path as unchecking it there."""
        self._push_selection_undo()
        self._set_run_selected(run_id, False)

    def remove_from_selection_many(self, run_ids):
        """Bulk version of remove_from_selection, used by the Data Viewer's
        drag-select + "Remove from selection" button - batched the same way
        group/range-select already are (see _set_run_selected_quiet) so removing
        many rows at once doesn't re-run the Data Viewer's own full refresh once
        per row."""
        run_ids = [rid for rid in run_ids if rid in self.selected_run_ids]
        if not run_ids:
            return
        self._push_selection_undo()
        for rid in run_ids:
            self._set_run_selected_quiet(rid, False)
        self._sync_group_glyphs_for(run_ids)
        self._update_selection_label()

    def set_selected_run_ids(self, run_ids):
        """Replace the current selection outright with exactly this set of runs -
        used by the Data Viewer's "Load Models/Analysis selection" button to pull
        back whatever's currently loaded in the Models/Analysis tabs without having
        to go re-find and re-check each run by hand in the main tree."""
        self._push_selection_undo()
        self.selected_run_ids = set(run_ids)
        self.refresh()  # refresh() already re-syncs the selection label/Data Viewer at the end

    def notify_data_changed(self):
        """Call after any edit that could affect what the Data Viewer is showing for
        the currently selected runs (pressure/standard/notes edits, excluded/duplicate
        toggles, deletions, ingests...) - cheap, since it only re-queries whatever's
        currently selected rather than polling on a timer."""
        self.data_viewer.refresh(self.selected_run_ids)

    def on_open_timer(self):
        if self._timer_window is not None and self._timer_window.winfo_exists():
            self._timer_window.deiconify()
            self._timer_window.lift()
            self._timer_window.focus_set()
            return
        self._timer_window = StackTimerWindow(self)

    def _on_notebook_tab_changed(self, _event=None):
        if self.notebook.select() == str(self.analysis_tab):
            self.analysis_panel._update_overlays()
            self.analysis_panel.on_justify_columns()

    def on_row_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid or not iid.startswith("run-"):
            return
        self.tree.selection_set(iid)
        run_id = int(iid.split("-", 1)[1])

        menu = self._context_menu
        menu.delete(0, "end")
        menu.add_command(
            label="Edit selected run(s) in pressure entry...",
            command=lambda: self.on_edit_selected_pressure(run_id),
        )
        menu.add_command(label="Toggle excluded", command=self.on_toggle_excluded)
        if self._is_duplicate(run_id):
            menu.add_command(label="Not actually a duplicate", command=self.on_clear_duplicate)

        highlight_menu = self._build_highlight_menu(menu, run_id)
        menu.add_cascade(label="Set highlight", menu=highlight_menu)

        menu.add_separator()
        menu.add_command(
            label="Open file location", command=lambda: self.on_open_file_location(run_id)
        )

        menu.tk_popup(event.x_root, event.y_root)

    def on_open_file_location(self, run_id):
        row = self.conn.execute("SELECT source_file FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None or not row["source_file"]:
            messagebox.showwarning("No file on record", "This run has no source file path on record.")
            return
        if not self._watched_folder:
            messagebox.showwarning(
                "No watched folder set",
                "No watched folder is configured, so the raw file's location can't be resolved. Set one "
                'via "Watch folder..." on the Load Data tab\'s Folder sub-tab.',
            )
            return
        # source_file is stored relative to whatever folder it was ingested from -
        # in every current ingestion path (watch/poll, "Ingest now", Load Data tab)
        # that's always the currently-configured watched folder, so this is a safe
        # (not guaranteed-universal) resolution rather than a stored absolute path.
        abs_path = Path(self._watched_folder) / row["source_file"]
        if not abs_path.exists():
            messagebox.showwarning(
                "File not found",
                f"Expected to find it at:\n{abs_path}\n\nbut nothing is there - it may have been moved, "
                "renamed, or ingested from a different folder than the one currently watched.",
            )
            return
        # explorer.exe often reports a non-zero exit code even on a normal, working
        # "select this file" launch - not a real failure worth surfacing.
        subprocess.Popen(["explorer", f"/select,{abs_path}"])

    def _build_highlight_menu(self, parent_menu, run_id):
        """Shared by the row right-click cascade and the direct click-on-the-
        "highlight"-column popup - one place building the swatch list so both stay
        in sync."""
        highlight_menu = tk.Menu(parent_menu, tearoff=0)
        highlight_menu.add_command(label="(none)", command=lambda: self._set_run_highlight(run_id, None))
        swatches = db.list_highlight_swatches(self.conn)
        if swatches:
            highlight_menu.add_separator()
            for swatch in swatches:
                label = swatch["label"] or swatch["color"]
                highlight_menu.add_command(
                    label=label, background=swatch["color"],
                    command=lambda sid=swatch["swatch_id"]: self._set_run_highlight(run_id, sid),
                )
        highlight_menu.add_separator()
        highlight_menu.add_command(label="Edit swatches...", command=self.on_edit_highlight_swatches)
        return highlight_menu

    def _open_highlight_picker(self, run_id, x_root, y_root):
        """Clicking directly on a run's "highlight" cell opens the same swatch
        picker as the right-click "Set highlight" cascade, without needing to
        right-click first - as a standalone popup menu rather than a cascade,
        since there's no parent context menu here."""
        menu = self._build_highlight_menu(self, run_id)
        menu.tk_popup(x_root, y_root)

    def _set_run_highlight(self, run_id, swatch_id):
        db.set_run_highlight(self.conn, run_id, swatch_id)
        self._update_highlight_overlays()
        self.notify_data_changed()
        self.analysis_panel._update_highlight_overlays()

    def on_edit_highlight_swatches(self):
        def on_change():
            self._update_highlight_overlays()
            self.data_viewer._update_highlight_overlays()
            self.analysis_panel._update_highlight_overlays()
        HighlightSwatchesWindow(self, on_change=on_change)

    def on_toggle_excluded(self):
        sel = self.tree.selection()
        if not sel or not sel[0].startswith("run-"):
            return
        run_id = int(sel[0].split("-", 1)[1])
        new_state = not self._is_excluded(run_id)
        db.set_excluded(self.conn, run_id, new_state)
        self.refresh()

    def on_clear_duplicate(self):
        sel = self.tree.selection()
        if not sel or not sel[0].startswith("run-"):
            return
        run_id = int(sel[0].split("-", 1)[1])
        db.clear_duplicate(self.conn, run_id)
        self.refresh()

    def on_exclude_all_duplicates(self):
        count = db.exclude_all_duplicates(self.conn)
        messagebox.showinfo("Duplicates excluded", f"Marked {count} duplicate run(s) as excluded.")
        self.refresh()

    def on_open_duplicates_wizard(self):
        DuplicatesWizardWindow(self)

    def on_open_load_tab(self):
        self.notebook.select(self.load_tab)

    def on_open_duplicates_tab(self):
        self.notebook.select(self.load_tab)
        self.load_data_panel.open_duplicates_tab()

    def on_open_pressure_review(self):
        """Plain pending-review runs go straight to Pressure entry - never a stop
        at the Duplicates sub-tab, which is reserved for runs actually blocked by a
        same-identity group (see on_open_duplicates_tab)."""
        self.notebook.select(self.load_tab)
        self.load_data_panel.notebook.select(self.load_data_panel.pressure_tab)
        self.load_data_panel._rebuild_pressure_panel()

    def on_open_pressure_warning_review(self):
        """Reopens exactly the run(s) an earlier pressure-entry session flagged as
        "standard linked, no pressure entered" - not a database-wide missing-pressure
        scan, just the specific runs the warning is actually about."""
        run_ids = sorted(self._pressure_missing_for_standard_run_ids)
        if not run_ids:
            return
        PressureEntryWindow(self, run_ids=run_ids, missing_only=False)

    def on_open_help_tab(self):
        self.notebook.select(self.help_tab)

    def on_open_load_folder_tab(self):
        self.notebook.select(self.load_tab)
        self.load_data_panel.notebook.select(self.load_data_panel.folder_tab)

    def on_open_missing_export_folder(self, rel_path):
        if not self._watched_folder:
            messagebox.showwarning(
                "No watched folder set",
                "No watched folder is configured, so this can't be resolved to a real path.",
            )
            return
        abs_path = Path(self._watched_folder) / rel_path
        if not abs_path.exists():
            messagebox.showwarning(
                "Folder not found",
                f"Expected to find it at:\n{abs_path}\n\nbut nothing is there.",
            )
            return
        subprocess.Popen(["explorer", str(abs_path)])

    def update_load_pending_indicator(self):
        if not hasattr(self, "load_pending_review_label"):
            return
        n = db.count_pending_load_runs(self.conn)
        n_dup = len(db.find_same_identity_groups(self.conn))
        self.load_pending_review_label.config(
            text=f"{n} run(s) awaiting review" if n else ""
        )
        self.load_pending_duplicates_label.config(
            text=f"{n_dup} duplicate group(s) pending" if n_dup else ""
        )
        error_bits = []
        if self._last_ingest_errors:
            error_bits.append(f"{self._last_ingest_errors} failed to parse")
        if self._last_missing_export:
            error_bits.append(f"{self._last_missing_export} missing CSV export")
        self.load_pending_errors_label.config(
            text=(", ".join(error_bits) + " (see Help)") if error_bits else ""
        )
        n_pressure_warn = len(self._pressure_missing_for_standard_run_ids)
        self.load_pending_pressure_warning_label.config(
            text=(
                f"{n_pressure_warn} run(s) missing pressure for a linked standard"
                if n_pressure_warn else ""
            )
        )

    def _is_duplicate(self, run_id):
        row = self.conn.execute("SELECT duplicate_of FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return bool(row and row["duplicate_of"] is not None)
