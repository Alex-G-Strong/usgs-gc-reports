"""A window for defining known standard gas mixtures and their compositions - the
reference data needed to turn a standard run's pressure into an actual calibration
point (a future standards-curve view will combine this with pressure_wizard data)."""

import tkinter as tk
from tkinter import ttk, messagebox

from gc_pipeline import db

AUTOSAVE_DEBOUNCE_MS = 800


class StandardsWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.conn = app.conn
        self.selected_standard_id = None
        self.value_vars = {}   # gas -> StringVar - always a percent value
        self._autosave_after_ids = {}       # gas -> after_id, for debounced per-field autosave
        self._description_autosave_id = None
        # Gases added via "+ Add gas..." this window session that haven't actually
        # been saved (with a real value, for any standard) yet - list_composition_
        # gas_names() only knows about ones already persisted, so a just-added,
        # not-yet-filled-in row needs this to keep showing up while you switch
        # between standards looking for the right one to put a value on.
        self._extra_gas_names = set()

        self.title("Standards")
        self.geometry("760x420")
        self.minsize(680, 360)

        self._build()
        self._load_standards()

    def _build(self):
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=6, pady=6)

        # -- left: list of standards ---------------------------------------
        left = ttk.Frame(paned)
        paned.add(left, weight=1)

        btn_bar = ttk.Frame(left)
        btn_bar.pack(fill="x", pady=(0, 4))
        ttk.Button(btn_bar, text="New", command=self.on_new_standard).pack(side="left")
        ttk.Button(btn_bar, text="Rename", command=self.on_rename_standard).pack(side="left", padx=4)
        ttk.Button(btn_bar, text="Delete", command=self.on_delete_standard).pack(side="left")

        self.standards_tree = ttk.Treeview(
            left, columns=("name", "description"), show="headings", selectmode="browse"
        )
        self.standards_tree.heading("name", text="name")
        self.standards_tree.heading("description", text="description")
        self.standards_tree.column("name", width=120, anchor="w")
        self.standards_tree.column("description", width=160, anchor="w")
        self.standards_tree.tag_configure("sample-marker", foreground="#888888")
        self.standards_tree.pack(fill="both", expand=True)
        self.standards_tree.bind("<<TreeviewSelect>>", self.on_select_standard)

        # -- right: composition form -----------------------------------------
        right = ttk.Frame(paned)
        paned.add(right, weight=2)

        # A dropdown alternative to clicking a row in the tree on the left - handy
        # once there are more than a handful of standards. Stays in sync both ways:
        # picking here selects the matching tree row, and selecting a tree row
        # updates this to match.
        picker_bar = ttk.Frame(right)
        picker_bar.pack(fill="x", pady=(0, 6))
        ttk.Label(picker_bar, text="Standard:").pack(side="left")
        self.standard_picker_var = tk.StringVar()
        self.standard_picker = ttk.Combobox(
            picker_bar, textvariable=self.standard_picker_var, state="readonly", width=24
        )
        self.standard_picker.pack(side="left", padx=(6, 0))
        self.standard_picker.bind("<<ComboboxSelected>>", self.on_pick_standard)

        self.composition_label = ttk.Label(right, text="Select a standard", font=("", 10, "bold"))
        self.composition_label.pack(anchor="w", pady=(0, 6))

        desc_frame = ttk.Frame(right)
        desc_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(desc_frame, text="Description:").pack(side="left")
        self.description_var = tk.StringVar()
        self.description_entry = ttk.Entry(desc_frame, textvariable=self.description_var)
        self.description_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.description_entry.bind("<FocusOut>", lambda _e: self._autosave_description(strict=True))
        self.description_entry.bind("<KeyRelease>", self._schedule_autosave_description)

        # Save bar and hint are packed to the bottom FIRST, so they always keep a
        # reserved slot and stay visible - the gas-row list (below) can be longer
        # than the window and scrolls internally instead of pushing these off-screen.
        hint = ttk.Label(
            right,
            text="Every field autosaves as you edit it (or when you tab/click away) - no "
                 "explicit save is needed. Leave a gas blank if it isn't part of this "
                 "standard. Values are percent composition (e.g. enter 20.9 for 20.9%). "
                 "Use \"+ Add gas...\" for a component the instrument has never actually "
                 "detected/reported a peak for (e.g. H2S) but that belongs in this "
                 "standard's known composition.",
            foreground="#666666", wraplength=380, justify="left",
        )
        hint.pack(side="bottom", anchor="w", pady=(0, 6))

        save_bar = ttk.Frame(right)
        save_bar.pack(side="bottom", anchor="w", pady=10, fill="x")
        ttk.Button(save_bar, text="Save now", command=self.on_save_composition).pack(side="left")
        self.save_status_label = ttk.Label(save_bar, text="", foreground="#2a7a2a")
        self.save_status_label.pack(side="left", padx=(10, 0))

        # -- scrollable gas-row list -------------------------------------------
        form_container = ttk.Frame(right)
        form_container.pack(side="top", fill="both", expand=True)

        self.form_canvas = tk.Canvas(form_container, highlightthickness=0)
        form_scrollbar = ttk.Scrollbar(form_container, orient="vertical", command=self.form_canvas.yview)
        self.form_canvas.configure(yscrollcommand=form_scrollbar.set)
        self.form_canvas.pack(side="left", fill="both", expand=True)
        form_scrollbar.pack(side="right", fill="y")

        self.form_frame = ttk.Frame(self.form_canvas)
        self._form_frame_window = self.form_canvas.create_window((0, 0), window=self.form_frame, anchor="nw")
        self.form_frame.bind(
            "<Configure>",
            lambda _e: self.form_canvas.configure(scrollregion=self.form_canvas.bbox("all")),
        )
        self.form_canvas.bind(
            "<Configure>",
            lambda e: self.form_canvas.itemconfig(self._form_frame_window, width=e.width),
        )

        def _on_mousewheel(event):
            self.form_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self.form_canvas.bind("<Enter>", lambda _e: self.form_canvas.bind_all("<MouseWheel>", _on_mousewheel))
        self.form_canvas.bind("<Leave>", lambda _e: self.form_canvas.unbind_all("<MouseWheel>"))

        self.value_entries = {}  # gas -> Entry widget, so on_add_gas can focus the new one
        self._rebuild_gas_rows()

    # -- standards list ----------------------------------------------------
    def _load_standards(self, select_id=None):
        self.standards_tree.delete(*self.standards_tree.get_children())
        for s in db.list_standards(self.conn):
            tags = ("sample-marker",) if s["name"].lower() == db.SAMPLE_STANDARD_NAME.lower() else ()
            self.standards_tree.insert(
                "", "end", iid=str(s["standard_id"]),
                values=(s["name"], s["description"] or ""),
                tags=tags,
            )
        self.standard_picker.configure(values=[s["name"] for s in db.list_standards(self.conn)])
        if select_id is not None and self.standards_tree.exists(str(select_id)):
            self.standards_tree.selection_set(str(select_id))
            self.standards_tree.focus(str(select_id))
        self._refresh_composition_form()

    def on_select_standard(self, _event=None):
        self._refresh_composition_form()

    def on_pick_standard(self, _event=None):
        name = self.standard_picker_var.get()
        match = next((s for s in db.list_standards(self.conn) if s["name"] == name), None)
        if match is None:
            return
        iid = str(match["standard_id"])
        self.standards_tree.selection_set(iid)
        self.standards_tree.focus(iid)
        self.standards_tree.see(iid)
        self._refresh_composition_form()

    def _current_standard_id(self):
        sel = self.standards_tree.selection()
        return int(sel[0]) if sel else None

    def _refresh_composition_form(self):
        # Cancel any pending debounced autosave from whatever standard was selected
        # before - otherwise a timer left over from mid-typing on the old standard
        # could fire against the newly-selected one instead.
        for after_id in self._autosave_after_ids.values():
            self.after_cancel(after_id)
        self._autosave_after_ids = {}
        if self._description_autosave_id is not None:
            self.after_cancel(self._description_autosave_id)
            self._description_autosave_id = None

        standard_id = self._current_standard_id()
        self.selected_standard_id = standard_id
        self.save_status_label.config(text="")
        for var in self.value_vars.values():
            var.set("")

        if standard_id is None:
            self.composition_label.config(text="Select a standard")
            self.description_var.set("")
            self.standard_picker_var.set("")
            return

        name = self.standards_tree.set(str(standard_id), "name")
        self.composition_label.config(text=f"Composition: {name}")
        self.standard_picker_var.set(name)
        self.description_var.set(self.standards_tree.set(str(standard_id), "description"))
        for row in db.get_standard_compositions(self.conn, standard_id):
            if row["gas"] in self.value_vars:
                self.value_vars[row["gas"]].set(str(row["value"]))

    # -- standard CRUD -------------------------------------------------------
    def on_new_standard(self):
        name = _prompt_text(self, "New standard", "Name:")
        if not name:
            return
        try:
            standard_id = db.create_standard(self.conn, name)
        except Exception as exc:
            messagebox.showerror("Could not create standard", str(exc))
            return
        self._load_standards(select_id=standard_id)

    def on_rename_standard(self):
        standard_id = self._current_standard_id()
        if standard_id is None:
            messagebox.showwarning("Nothing selected", "No standard is selected.")
            return
        current_name = self.standards_tree.set(str(standard_id), "name")
        name = _prompt_text(self, "Rename standard", "Name:", initial=current_name)
        if not name:
            return
        current_desc = self.standards_tree.set(str(standard_id), "description")
        db.rename_standard(self.conn, standard_id, name, current_desc)
        self._load_standards(select_id=standard_id)
        self.app.notify_data_changed()

    def on_delete_standard(self):
        standard_id = self._current_standard_id()
        if standard_id is None:
            messagebox.showwarning("Nothing selected", "No standard is selected.")
            return
        name = self.standards_tree.set(str(standard_id), "name")
        if not messagebox.askyesno("Delete standard", f'Delete standard "{name}"? This cannot be undone.'):
            return
        db.delete_standard(self.conn, standard_id)
        self._load_standards()
        self.app.notify_data_changed()

    # -- gas rows (including manually-added ones) ------------------------------
    def _all_known_gas_names(self):
        """Every gas the form should offer a row for - the instrument-reported list
        (list_gases, from actual peak data) plus anything already recorded in some
        standard's composition, which includes gases added via "+ Add gas..." even
        if the instrument has never reported a peak for them."""
        return sorted(
            set(db.list_gases(self.conn)) | set(db.list_composition_gas_names(self.conn)) | self._extra_gas_names
        )

    def _rebuild_gas_rows(self):
        """Rebuilds the whole gas-row form from scratch - needed after "+ Add gas..."
        since that changes the row set itself, not just a value within it."""
        for child in self.form_frame.winfo_children():
            child.destroy()
        self.value_vars = {}
        self.value_entries = {}
        gases = self._all_known_gas_names()
        # Wide enough for the longest gas name currently in play (some, like
        # "C2H6-Meth channel", badly overrun a fixed narrow width and get clipped)
        # - grows automatically as longer names get added instead of needing a
        # hardcoded guess.
        name_width = max((len(g) for g in gases), default=10)
        for row, gas in enumerate(gases):
            ttk.Label(self.form_frame, text=gas, width=name_width).grid(row=row, column=0, sticky="w", pady=2)
            value_var = tk.StringVar()
            self.value_vars[gas] = value_var
            entry = ttk.Entry(self.form_frame, textvariable=value_var, width=10)
            entry.grid(row=row, column=1, padx=(4, 10))
            entry.bind("<FocusOut>", lambda _e, g=gas: self._autosave_gas(g, strict=True))
            entry.bind("<KeyRelease>", lambda _e, g=gas: self._schedule_autosave_gas(g))
            self.value_entries[gas] = entry
            ttk.Label(self.form_frame, text="%").grid(row=row, column=2, sticky="w")
        ttk.Button(self.form_frame, text="+ Add gas...", command=self.on_add_gas).grid(
            row=len(gases), column=0, columnspan=3, sticky="w", pady=(8, 0)
        )

    def on_add_gas(self):
        """Records a gas the instrument has never reported a peak for but that a
        standard's real, known composition (e.g. its certificate of analysis)
        includes - e.g. H2S on a channel that doesn't detect it. The row is purely
        additive/in-memory until a value is actually typed in for some standard
        (which autosaves normally); an added-but-never-filled-in row just quietly
        drops out again next time the form rebuilds, same as leaving any other gas
        blank on purpose."""
        name = _prompt_text(self, "Add gas", "Gas name (matched exactly, including case):")
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if name in self._all_known_gas_names():
            messagebox.showinfo("Already tracked", f'"{name}" is already a tracked gas.')
            return
        selected = self.selected_standard_id
        self._extra_gas_names.add(name)
        self._rebuild_gas_rows()
        if selected is not None:
            self.standards_tree.selection_set(str(selected))
        self._refresh_composition_form()
        entry = self.value_entries.get(name)
        if entry is not None:
            entry.focus_set()

    # -- composition (autosave) -----------------------------------------------
    def _schedule_autosave_gas(self, gas):
        prev = self._autosave_after_ids.get(gas)
        if prev is not None:
            self.after_cancel(prev)
        self._autosave_after_ids[gas] = self.after(
            AUTOSAVE_DEBOUNCE_MS, lambda: self._autosave_gas(gas, strict=False)
        )

    def _autosave_gas(self, gas, strict):
        self._autosave_after_ids.pop(gas, None)
        if self.selected_standard_id is None:
            return
        raw = self.value_vars[gas].get().strip()
        if not raw:
            db.delete_standard_composition(self.conn, self.selected_standard_id, gas)
            self._flash_saved()
            return
        try:
            value = float(raw)
        except ValueError:
            # Live-typing autosave (strict=False) silently leaves whatever was
            # already saved untouched until the value becomes parseable again (e.g.
            # mid-typing "-" or "1."); FocusOut (strict=True) flags a genuine mistake.
            if strict:
                messagebox.showerror("Invalid value", f"'{raw}' is not a number ({gas}).")
            return
        db.upsert_standard_composition(self.conn, self.selected_standard_id, gas, value)
        self._flash_saved()

    def _schedule_autosave_description(self, _event=None):
        if self._description_autosave_id is not None:
            self.after_cancel(self._description_autosave_id)
        self._description_autosave_id = self.after(AUTOSAVE_DEBOUNCE_MS, lambda: self._autosave_description(False))

    def _autosave_description(self, strict):
        self._description_autosave_id = None
        if self.selected_standard_id is None:
            return
        name = self.standards_tree.set(str(self.selected_standard_id), "name")
        description = self.description_var.get().strip()
        db.rename_standard(self.conn, self.selected_standard_id, name, description)
        self.standards_tree.set(str(self.selected_standard_id), "description", description)
        self._flash_saved()

    def _flash_saved(self):
        self.save_status_label.config(text="Saved.")
        self.after(2000, lambda: self.save_status_label.config(text=""))

    def on_save_composition(self):
        """The "Save now" button - forces an immediate, strict (error-on-invalid)
        save of every field right away, rather than waiting for the autosave
        debounce or a focus change."""
        if self.selected_standard_id is None:
            messagebox.showwarning("Nothing selected", "No standard is selected.")
            return
        for gas in self.value_vars:
            self._autosave_gas(gas, strict=True)
        self._autosave_description(strict=True)


def _prompt_text(parent, title, label, initial=""):
    """A minimal modal text-entry dialog - simpler than pulling in tkinter.simpledialog
    for just one string field, and keeps styling consistent with the rest of the app."""
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent)
    dialog.grab_set()

    ttk.Label(dialog, text=label).pack(padx=10, pady=(10, 2))
    var = tk.StringVar(value=initial)
    entry = ttk.Entry(dialog, textvariable=var, width=30)
    entry.pack(padx=10, pady=(0, 10))
    entry.focus_set()

    result = {"value": None}

    def on_ok(_event=None):
        result["value"] = var.get().strip()
        dialog.destroy()

    def on_cancel(_event=None):
        dialog.destroy()

    btn_bar = ttk.Frame(dialog)
    btn_bar.pack(pady=(0, 10))
    ttk.Button(btn_bar, text="OK", command=on_ok).pack(side="left", padx=4)
    ttk.Button(btn_bar, text="Cancel", command=on_cancel).pack(side="left")
    entry.bind("<Return>", on_ok)
    entry.bind("<Escape>", on_cancel)

    dialog.wait_window()
    return result["value"]
