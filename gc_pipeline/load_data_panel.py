"""The "Load Data" tab: walks the scientist through bringing new GC output into the
database - point at (or watch) a folder, resolve any duplicates the new files turn
out to be against what's already on file (the "Next" button consolidates whatever's
left before moving on), then enter pressures/standards for whatever's left and flag
any issues, and finally "Save" to fold the reviewed runs into the real run
numbering. Runs sit here unnumbered (see db.list_pending_load_runs /
db.finalize_runs) so a fresh ingest never perturbs the numbers of runs that have
already been reviewed."""

import tkinter as tk
from tkinter import ttk, messagebox

from gc_pipeline import db
from gc_pipeline.duplicates_wizard import DuplicatesWizardPanel
from gc_pipeline.pressure_wizard import PressureEntryPanel
from gc_pipeline.widgets import bind_tooltip


class LoadDataPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.conn = app.conn
        self.pressure_panel = None
        self._build()

    def _build(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)

        self.folder_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.folder_tab, text="1. Folder")
        self._build_folder_tab(self.folder_tab)

        self.duplicates_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.duplicates_tab, text="2. Duplicates")
        self.duplicates_panel = DuplicatesWizardPanel(
            self.duplicates_tab, self.app, on_next=self.on_duplicates_next
        )
        self.duplicates_panel.pack(fill="both", expand=True)

        self.pressure_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.pressure_tab, text="3. Pressure entry")
        self._build_pressure_tab(self.pressure_tab)

        self.notebook.bind("<<NotebookTabChanged>>", self._on_subtab_changed)

    # -- sub-tab 1: point at / watch a folder ------------------------------------
    def _build_folder_tab(self, parent):
        # Right-aligned "move forward in the stack" button, same spot/style as the
        # other two sub-tabs' own primary action - routes straight past an empty
        # Duplicates tab to Pressure entry, same smart routing the toolbar's own
        # "Load Data..." button uses (see App.on_open_load_tab). Right-aligned so
        # it reads as "the thing you reach for once you're done here," matching
        # the left-to-right flow across the three sub-tabs themselves.
        nav_bar = ttk.Frame(parent)
        nav_bar.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Button(
            nav_bar, text="Next →", style="BigAction.TButton", command=self.app.on_open_load_tab
        ).pack(side="right")

        ttk.Label(
            parent,
            text="Point the app at the folder the GC exports into (or leave it watched, and it'll pick up "
                 "new files on its own). New files land here first, unnumbered, until they've been checked "
                 "for duplicates and given a pressure/standard on the next two tabs.",
            foreground="#666666", wraplength=900, justify="left",
        ).pack(fill="x", padx=8, pady=(8, 6))

        # A fixed width (wider than the button row's own natural width, via
        # pack_propagate(False) so it doesn't shrink back to fit them) gives the
        # folder-path status label real room without drifting around as the window
        # is resized - same layout trick this used when it lived in the toolbar.
        ingest_frame = ttk.Frame(parent, width=460, height=64)
        ingest_frame.pack(anchor="w", padx=8)
        ingest_frame.pack_propagate(False)

        btn_row = ttk.Frame(ingest_frame)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Watch folder...", command=self.app.on_choose_watch_folder).pack(side="left")
        ttk.Button(btn_row, text="Ingest now", command=self.app.on_ingest_now).pack(side="left", padx=6)
        # These live as attributes on the App (not on this panel) since
        # _update_watch_status/_refresh_watch_status_display/on_toggle_watch_pause
        # are App methods that already reference self.watch_toggle_button etc. -
        # only *where* these widgets are built moved, not who owns/updates them.
        self.app.watch_toggle_button = ttk.Button(
            btn_row, text="Pause watching", command=self.app.on_toggle_watch_pause
        )
        self.app.watch_toggle_button.pack(side="left")
        self.app.watch_and_assign_button = ttk.Button(
            btn_row, text="Watch folder for rest of day and assign to round",
            command=self.app.on_watch_and_assign_to_round,
        )
        self.app.watch_and_assign_button.pack(side="left", padx=(6, 0))

        self.app.watch_status_label = ttk.Label(ingest_frame, text="", foreground="#666666", justify="left")
        self.app.watch_status_label.pack(fill="x", anchor="w", pady=(4, 0))
        bind_tooltip(self.app.watch_status_label, lambda: self.app._watched_folder or "")
        self.app.watch_status_label.bind("<Configure>", self.app._refresh_watch_status_display)

        # Only shown while live watch+round-assign mode is actually active - this is
        # the one case where a reprocessed file is merged automatically rather than
        # going through the Duplicates tab (see _run_ingest_pass), so it's worth
        # calling out explicitly whenever it's in effect rather than leaving it a
        # surprise.
        self.app.auto_versioning_label = ttk.Label(
            parent,
            text="",
            foreground="#8a6d00", wraplength=900, justify="left",
        )
        self.app.auto_versioning_label.pack(fill="x", padx=8, pady=(4, 0))

        # Flags run folders that have raw acquisition files but no exported CSV at
        # all - not a parse error (there's nothing to parse), but the instrument
        # software never produced the peak table. A tk.Text (not a plain Label) so
        # each listed file name can be its own clickable region - right-click a
        # file name to jump straight into its folder; left-click anywhere else in
        # the message goes to Help.
        self.app.missing_export_text = tk.Text(
            parent, height=1, wrap="word", state="disabled", relief="flat",
            borderwidth=0, highlightthickness=0, cursor="hand2", padx=8, pady=2,
        )
        self.app.missing_export_text.pack(fill="x", padx=8, pady=(4, 0))
        self.app.missing_export_text.tag_configure("intro", foreground="#b3261e")
        self.app.missing_export_text.tag_configure(
            "path", foreground="#b3261e", underline=True, font=("", 9, "bold")
        )

        def _tags_at(event):
            index = self.app.missing_export_text.index(f"@{event.x},{event.y}")
            return self.app.missing_export_text.tag_names(index)

        def _on_click(event):
            if any(t.startswith("path-") for t in _tags_at(event)):
                return  # left-click on a file name does nothing - right-click opens its folder
            self.app.on_open_help_tab()

        def _on_right_click(event):
            path_tag = next((t for t in _tags_at(event) if t.startswith("path-")), None)
            if path_tag is None:
                return
            idx = int(path_tag.split("-", 1)[1])
            paths = self.app._last_missing_export_paths
            if idx >= len(paths):
                return
            rel_path = paths[idx]
            menu = tk.Menu(self.app, tearoff=0)
            menu.add_command(
                label="Open containing folder",
                command=lambda: self.app.on_open_missing_export_folder(rel_path),
            )
            menu.tk_popup(event.x_root, event.y_root)

        self.app.missing_export_text.bind("<Button-1>", _on_click)
        self.app.missing_export_text.bind("<Button-3>", _on_right_click)

        # A running history of what's actually landed in the database, newest first -
        # unlike the status label above (which only ever shows the *last* poll's
        # result), this accumulates across every poll so a scientist coming back
        # after being away can see everything that happened while unattended.
        ttk.Label(parent, text="Recent activity:", foreground="#666666").pack(
            fill="x", padx=8, pady=(10, 2)
        )
        history_frame = ttk.Frame(parent)
        history_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        history_scroll = ttk.Scrollbar(history_frame, orient="vertical")
        self.app.ingest_history_text = tk.Text(
            history_frame, height=8, wrap="none", state="disabled",
            foreground="#444444", relief="flat", background=self.app.cget("background"),
            yscrollcommand=history_scroll.set,
        )
        history_scroll.config(command=self.app.ingest_history_text.yview)
        self.app.ingest_history_text.pack(side="left", fill="both", expand=True)
        history_scroll.pack(side="right", fill="y")

        self.app._watch_second_line = ""
        self.app._update_watch_status(None, manual=False)
        self.app._refresh_ingest_history_display()

    # -- sub-tab 3: pressure/standard entry for whatever's still pending --------
    def _build_pressure_tab(self, parent):
        # Right-aligned "move forward in the stack" button, same spot/style as
        # the other two sub-tabs - this is the actual exit point of the whole
        # Load Data flow (it's the same action as the button at the bottom, just
        # placed where the rest of the workflow's own "next" buttons live, so
        # there's always a consistent place to look for "what do I click now").
        top_bar = ttk.Frame(parent)
        top_bar.pack(fill="x", padx=8, pady=(8, 0))
        top_finish_button = ttk.Button(
            top_bar, text="Insert runs into database →", style="BigAction.TButton",
            command=self.on_finish_loading,
        )
        top_finish_button.pack(side="right")
        bind_tooltip(
            top_finish_button,
            "Saves any pending pressure/standard edits, assigns real run numbers, and permanently adds "
            "everything currently pending to the database, then takes you to the Selector tab. You'll be "
            "asked to confirm exactly what's about to be added first.",
        )

        ttk.Label(
            parent,
            text='Runs waiting to be reviewed - enter pressures/standards, flag issues with a highlight '
                 '(click the "Highlight" column), or type NR in the pressure cell if it was genuinely never '
                 "recorded. Nothing here has a run number yet - that happens once you click Save below.",
            foreground="#666666", wraplength=900, justify="left",
        ).pack(fill="x", padx=8, pady=(8, 4))

        self.pressure_panel_frame = ttk.Frame(parent)
        self.pressure_panel_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        finish_bar = ttk.Frame(parent)
        finish_bar.pack(fill="x", padx=8, pady=(0, 8))
        save_button = ttk.Button(
            finish_bar, text="Insert runs into database", command=self.on_finish_loading
        )
        save_button.pack(side="left")
        bind_tooltip(
            save_button,
            "Saves any pending pressure/standard edits, assigns real run numbers, and permanently adds "
            "everything currently pending to the database - this is the last step of loading new data in. "
            "You'll be asked to confirm exactly what's about to be added first.",
        )
        self.finish_status_label = ttk.Label(finish_bar, text="", foreground="#666666")
        self.finish_status_label.pack(side="left", padx=(8, 0))

        self._rebuild_pressure_panel()

    def _rebuild_pressure_panel(self):
        if self.pressure_panel is not None:
            self.pressure_panel.destroy()
        all_pending = db.list_pending_load_runs(self.conn)
        grouped_ids = {
            run_id for run_ids in db.find_same_identity_groups(self.conn).values() for run_id in run_ids
        }
        pending_ids = [r["run_id"] for r in all_pending if r["run_id"] not in grouped_ids]
        self.pressure_panel = PressureEntryPanel(
            self.pressure_panel_frame, self.app, run_ids=pending_ids, missing_only=False,
            show_save_button=False,
        )
        self.pressure_panel.pack(fill="both", expand=True)
        n = len(pending_ids)
        blocked_count = len(all_pending) - n
        # A run hidden here because it's stuck in an unresolved duplicate group
        # must never look identical to "genuinely nothing left" - that's exactly
        # what read as "I already dealt with this" when it hadn't actually been
        # resolved on the Duplicates tab yet.
        if n:
            text = f"{n} run(s) awaiting review"
            if blocked_count:
                text += f" ({blocked_count} more blocked by unresolved duplicate groups - see the Duplicates tab)"
        elif blocked_count:
            text = (
                f"Nothing to review here yet - {blocked_count} run(s) are blocked by unresolved duplicate "
                'groups. Resolve them on the "2. Duplicates" tab first.'
            )
        else:
            text = "Nothing pending - all caught up."
        self.finish_status_label.config(text=text)

    def on_duplicates_next(self):
        self._rebuild_pressure_panel()
        self.notebook.select(self.pressure_tab)

    def on_finish_loading(self):
        pending_ids = [r["run_id"] for r in db.list_pending_load_runs(self.conn)]
        if pending_ids:
            grouped_ids = {
                run_id for run_ids in db.find_same_identity_groups(self.conn).values() for run_id in run_ids
            }
            blocked_ids = [rid for rid in pending_ids if rid in grouped_ids]
            finalize_ids = [rid for rid in pending_ids if rid not in grouped_ids]
            if blocked_ids:
                messagebox.showwarning(
                    "Resolve duplicates first",
                    f"{len(blocked_ids)} run(s) are part of a same-identity group (a reprocessed file, or "
                    'a rediscovered duplicate) and need to be resolved on the "2. Duplicates" tab before '
                    "they can be saved.",
                )
            if finalize_ids:
                # Explicit confirm before the permanent step - assigning real run
                # numbers is exactly the point where this data stops being "still
                # under review" and becomes part of the record, so the user should
                # see precisely what's about to happen before it does.
                names_preview = ", ".join(
                    r["sample_name"] for r in db.list_pending_load_runs(self.conn)
                    if r["run_id"] in finalize_ids
                )
                if len(names_preview) > 200:
                    names_preview = names_preview[:200] + "..."
                if not messagebox.askyesno(
                    "Insert into database?",
                    f"Ready to permanently add {len(finalize_ids)} run(s) to the database and assign them "
                    f"real run numbers:\n\n{names_preview}\n\nThis is the last step - continue?",
                ):
                    return
                if self.pressure_panel is not None and self.pressure_panel.pending:
                    self.pressure_panel.on_save()
                db.finalize_runs(self.conn, finalize_ids)
                self.app.refresh()
                self.app.notify_data_changed()
                self.app.update_pressure_button()
                self.refresh_all()
                messagebox.showinfo(
                    "Added to database",
                    f"{len(finalize_ids)} run(s) have been added to the database with real run numbers.",
                )
        # "Insert runs into database" is the exit point of the Load Data flow -
        # always lands on the Selector tab, whether or not there was actually
        # anything to finalize (e.g. everything was already saved, or nothing was
        # pending at all).
        self.app.notebook.select(self.app.selector_tab)

    # -- keeping both later sub-tabs in sync with the rest of the app -----------
    def _on_subtab_changed(self, _event=None):
        current = self.notebook.select()
        if current == str(self.duplicates_tab):
            self.duplicates_panel.refresh()
        elif current == str(self.pressure_tab):
            self._rebuild_pressure_panel()

    def refresh_all(self):
        self.duplicates_panel.refresh()
        self._rebuild_pressure_panel()

    def open_duplicates_tab(self):
        self.notebook.select(self.duplicates_tab)
        self.duplicates_panel.refresh()
