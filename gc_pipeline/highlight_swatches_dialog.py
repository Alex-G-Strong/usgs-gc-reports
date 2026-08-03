"""A small manager window for the row-highlight color palette - a fixed set of
user-editable swatches (add/recolor/remove/relabel) that any run can be tagged
with from the Selector tab's right-click menu. The tag then shows up as a colored
chip wherever that run appears (Selector, Data Viewer, Analysis's Results table)."""

import tkinter as tk
from tkinter import ttk, colorchooser, messagebox

from gc_pipeline import db

DEFAULT_NEW_COLOR = "#ffd166"


class HighlightSwatchesWindow(tk.Toplevel):
    def __init__(self, app, on_change=None):
        super().__init__(app)
        self.app = app
        self.conn = app.conn
        self.on_change = on_change

        self.title("Highlight swatches")
        self.geometry("340x360")
        self.minsize(300, 280)

        self._build()
        self._load()

    def _build(self):
        ttk.Label(
            self, text="These colors are available wherever a run can be highlighted "
                       "(right-click a row in the Selector tab).",
            foreground="#666666", wraplength=310, justify="left",
        ).pack(anchor="w", padx=8, pady=(8, 4))

        self.rows_frame = ttk.Frame(self)
        self.rows_frame.pack(fill="both", expand=True, padx=8)

        ttk.Button(self, text="+ Add swatch", command=self.on_add).pack(anchor="w", padx=8, pady=8)

    def _load(self):
        for child in self.rows_frame.winfo_children():
            child.destroy()
        swatches = db.list_highlight_swatches(self.conn)
        if not swatches:
            ttk.Label(self.rows_frame, text="No swatches yet.", foreground="#666666").pack(anchor="w", pady=4)
        for swatch in swatches:
            self._build_row(swatch)

    def _build_row(self, swatch):
        row = ttk.Frame(self.rows_frame)
        row.pack(fill="x", pady=2)

        swatch_id = swatch["swatch_id"]
        swab = tk.Label(row, text="  ", bg=swatch["color"], relief="solid", borderwidth=1, width=3)
        swab.pack(side="left")
        swab.bind("<Button-1>", lambda _e, sid=swatch_id, c=swatch["color"]: self.on_recolor(sid, c))

        name_var = tk.StringVar(value=swatch["label"] or "")
        entry = ttk.Entry(row, textvariable=name_var)
        entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        entry.bind(
            "<FocusOut>", lambda _e, sid=swatch_id, v=name_var: db.update_highlight_swatch(
                self.conn, sid, label=v.get().strip()
            )
        )
        entry.bind("<Return>", lambda _e, sid=swatch_id, v=name_var: db.update_highlight_swatch(
            self.conn, sid, label=v.get().strip()
        ))

        ttk.Button(row, text="Remove", command=lambda sid=swatch_id: self.on_remove(sid)).pack(side="right")

    def on_add(self):
        swatch_id = db.create_highlight_swatch(self.conn, DEFAULT_NEW_COLOR, "")
        self._load()
        self._notify()
        self.on_recolor(swatch_id, DEFAULT_NEW_COLOR)

    def on_recolor(self, swatch_id, current_color):
        picked = colorchooser.askcolor(color=current_color, parent=self, title="Swatch color")
        if not picked or not picked[1]:
            return
        db.update_highlight_swatch(self.conn, swatch_id, color=picked[1])
        self._load()
        self._notify()

    def on_remove(self, swatch_id):
        if not messagebox.askyesno(
            "Remove swatch", "Remove this swatch? Any runs currently using it lose the highlight."
        ):
            return
        db.delete_highlight_swatch(self.conn, swatch_id)
        self._load()
        self._notify()

    def _notify(self):
        if self.on_change is not None:
            self.on_change()
