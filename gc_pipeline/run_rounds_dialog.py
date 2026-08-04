"""A small dialog for assigning a set of runs (picked in the Selector tab) to a
"round" - a user-defined span of one or more calendar days covering one calibration
cycle (calibrate, run samples, shut down). Rounds are what run_number is computed
within once a run is assigned to one, instead of pure per-calendar-date ranking."""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from gc_pipeline import db

NEW_ROUND_SENTINEL = "+ New round..."


class RoundAssignDialog(tk.Toplevel):
    def __init__(self, app, run_ids, preselect_round_id=None, on_done=None):
        super().__init__(app)
        self.app = app
        self.conn = app.conn
        self.run_ids = list(run_ids)
        # Pre-highlights whichever round the caller already knows these runs belong
        # to (e.g. the Pressure entry wizard's currently-shown round), rather than
        # always defaulting to "+ New round..." - still just a starting point, the
        # user can pick a different existing round or create a new one instead.
        self.preselect_round_id = preselect_round_id
        self._on_done = on_done

        self.title("Assign to round")
        self.geometry("360x420")
        self.minsize(320, 360)

        self._build()
        self._load_rounds()

    def _build(self):
        ttk.Label(
            self, text=f"{len(self.run_ids)} run(s) selected",
            foreground="#666666",
        ).pack(anchor="w", padx=8, pady=(8, 4))

        self.round_listbox = tk.Listbox(self, exportselection=False)
        self.round_listbox.pack(fill="both", expand=True, padx=8)
        self.round_listbox.bind("<<ListboxSelect>>", self._on_selection_changed)
        self.round_listbox.bind("<Button-3>", self._on_round_right_click)

        # side="bottom" pins this to the bottom of the window regardless of pack
        # order - new_round_frame (below, packed with the default side="top") stays
        # above it even though it's toggled via pack()/pack_forget() afterward,
        # which would otherwise re-append it below anything already packed.
        btn_bar = ttk.Frame(self)
        btn_bar.pack(fill="x", padx=8, pady=8, side="bottom")
        ttk.Button(btn_bar, text="Cancel", command=self._on_cancel).pack(side="right")
        ttk.Button(btn_bar, text="Assign", command=self.on_assign).pack(side="right", padx=(0, 6))
        # Bulk removal, not just bulk assignment - clears round_id (back to plain
        # per-date numbering) for every one of self.run_ids currently in a round,
        # regardless of which round(s) they're each in. Left-aligned, separate
        # from the Assign/Cancel pair, since it's a different kind of action (not
        # "pick a round then commit" - it just runs immediately).
        ttk.Button(btn_bar, text="Unassign from round", command=self.on_unassign).pack(side="left")
        ttk.Button(btn_bar, text="Rename...", command=self.on_rename).pack(side="left", padx=(6, 0))

        self.new_round_frame = ttk.Frame(self)
        ttk.Label(self.new_round_frame, text="New round name:").pack(anchor="w")
        self._auto_name = db.compute_round_auto_name(self.conn, self.run_ids)
        self.name_var = tk.StringVar(value=self._auto_name)
        ttk.Entry(self.new_round_frame, textvariable=self.name_var).pack(fill="x")

        ttk.Label(self.new_round_frame, text="Number runs by:").pack(anchor="w", pady=(8, 0))
        self.mode_var = tk.StringVar(value="time")
        ttk.Radiobutton(
            self.new_round_frame, text="Time (sequential from the round's start)",
            variable=self.mode_var, value="time",
        ).pack(anchor="w")
        ttk.Radiobutton(
            self.new_round_frame, text="Sample ID's trailing number",
            variable=self.mode_var, value="sample_id",
        ).pack(anchor="w")

    def _load_rounds(self):
        # Newest first, by the actual injection dates of each round's runs - not
        # alphabetically by name, which stops sorting chronologically the moment a
        # round gets renamed away from its date-based auto-name.
        latest_dates = db.get_round_latest_dates(self.conn)
        self._rounds = sorted(
            db.list_run_rounds(self.conn), key=lambda r: latest_dates.get(r["round_id"], ""), reverse=True
        )
        self.round_listbox.delete(0, "end")
        for round_row in self._rounds:
            self.round_listbox.insert("end", round_row["name"])
        self.round_listbox.insert("end", NEW_ROUND_SENTINEL)
        preselect_idx = next(
            (i for i, r in enumerate(self._rounds) if r["round_id"] == self.preselect_round_id),
            len(self._rounds),  # falls back to "+ New round..." if not found/not given
        )
        self.round_listbox.selection_set(preselect_idx)
        self.round_listbox.see(preselect_idx)
        self._on_selection_changed()

    def _on_selection_changed(self, _event=None):
        selection = self.round_listbox.curselection()
        is_new = not selection or selection[0] == len(self._rounds)
        if is_new:
            self.new_round_frame.pack(fill="x", padx=8, pady=(8, 0))
        else:
            self.new_round_frame.pack_forget()

    def on_assign(self):
        selection = self.round_listbox.curselection()
        if not selection or selection[0] == len(self._rounds):
            name = self.name_var.get().strip()
            if not name:
                messagebox.showwarning("Name required", "Enter a name for the new round.")
                return
            if any(r["name"] == name for r in self._rounds):
                messagebox.showwarning("Already exists", f'A round named "{name}" already exists.')
                return
            # If the user left the auto-filled name as-is, this round keeps
            # auto-updating its name as more runs are added to it later; typing
            # anything else opts it out of that (see rename_run_round).
            name_is_auto = name == self._auto_name
            db.create_run_round(
                self.conn, name, self.run_ids, numbering_mode=self.mode_var.get(), name_is_auto=name_is_auto
            )
        else:
            round_row = self._rounds[selection[0]]
            db.add_runs_to_round(self.conn, round_row["round_id"], self.run_ids)
        self.app.notify_data_changed()
        self.app.refresh()
        self.destroy()
        if self._on_done is not None:
            self._on_done()

    def on_unassign(self):
        placeholders = ",".join("?" for _ in self.run_ids)
        rows = self.conn.execute(
            f"SELECT run_id, round_id FROM runs WHERE run_id IN ({placeholders}) AND round_id IS NOT NULL",
            self.run_ids,
        ).fetchall()
        if not rows:
            messagebox.showinfo(
                "Nothing to unassign", "None of the selected run(s) are currently assigned to a round."
            )
            return
        # A selection can span more than one round at once - group by each run's
        # *current* round_id (remove_runs_from_round is itself scoped to one
        # round) rather than assuming every selected run shares the same one.
        by_round = {}
        for r in rows:
            by_round.setdefault(r["round_id"], []).append(r["run_id"])
        for round_id, run_ids in by_round.items():
            db.remove_runs_from_round(self.conn, round_id, run_ids)
        self.app.notify_data_changed()
        self.app.refresh()
        self.destroy()
        if self._on_done is not None:
            self._on_done()

    def on_rename(self):
        selection = self.round_listbox.curselection()
        if not selection or selection[0] == len(self._rounds):
            messagebox.showinfo("Nothing to rename", "Select an existing round first.")
            return
        self._rename_round(self._rounds[selection[0]])

    def _on_round_right_click(self, event):
        idx = self.round_listbox.nearest(event.y)
        if idx < 0 or idx >= len(self._rounds):
            return  # the "+ New round..." sentinel row, or an empty list - nothing to rename
        self.round_listbox.selection_clear(0, "end")
        self.round_listbox.selection_set(idx)
        self._on_selection_changed()
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Rename...", command=lambda: self._rename_round(self._rounds[idx]))
        menu.tk_popup(event.x_root, event.y_root)

    def _rename_round(self, round_row):
        new_name = simpledialog.askstring(
            "Rename round", "Round name:", initialvalue=round_row["name"], parent=self
        )
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name:
            messagebox.showwarning("Name required", "Enter a name for the round.")
            return
        if new_name != round_row["name"] and any(r["name"] == new_name for r in self._rounds):
            messagebox.showwarning("Already exists", f'A round named "{new_name}" already exists.')
            return
        db.rename_run_round(self.conn, round_row["round_id"], new_name)
        self.app.notify_data_changed()
        self.app.refresh()
        # Reload with the just-renamed round preselected, rather than whatever
        # preselect_round_id this dialog was originally constructed with - keeps
        # the rename visible/selected right where the user was looking.
        self.preselect_round_id = round_row["round_id"]
        self._load_rounds()

    def _on_cancel(self):
        self.destroy()
        if self._on_done is not None:
            self._on_done()


class RoundPickerDialog(tk.Toplevel):
    """Pick one existing round, with no "create new" option - used by the watch-
    folder's "auto-assign newly ingested runs to a round" mode, where there's no
    concrete set of runs yet to derive an auto-name from."""

    def __init__(self, app, on_pick, title="Choose a round"):
        super().__init__(app)
        self.app = app
        self.conn = app.conn
        self.on_pick = on_pick

        self.title(title)
        self.geometry("320x360")
        self.minsize(280, 300)

        self._rounds = db.list_run_rounds(self.conn)

        ttk.Label(
            self, text="Newly-ingested runs will be added to whichever round you pick here.",
            foreground="#666666", wraplength=290, justify="left",
        ).pack(anchor="w", padx=8, pady=(8, 4))

        if not self._rounds:
            ttk.Label(
                self, text="No rounds exist yet - create one first via \"Assign to round...\".",
                foreground="#a00", wraplength=290, justify="left",
            ).pack(anchor="w", padx=8, pady=8)

        self.round_listbox = tk.Listbox(self, exportselection=False)
        self.round_listbox.pack(fill="both", expand=True, padx=8)
        for round_row in self._rounds:
            self.round_listbox.insert("end", round_row["name"])
        if self._rounds:
            self.round_listbox.selection_set(0)

        btn_bar = ttk.Frame(self)
        btn_bar.pack(fill="x", padx=8, pady=8, side="bottom")
        ttk.Button(btn_bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btn_bar, text="OK", command=self._on_ok).pack(side="right", padx=(0, 6))

    def _on_ok(self):
        selection = self.round_listbox.curselection()
        if not selection:
            messagebox.showwarning("Nothing chosen", "Select a round first.")
            return
        round_row = self._rounds[selection[0]]
        self.destroy()
        self.on_pick(round_row["round_id"], round_row["name"])
