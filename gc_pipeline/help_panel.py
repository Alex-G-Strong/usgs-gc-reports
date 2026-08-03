"""The Help tab: renders in-house documentation directly in the app (a small
HTML subset - headings, bold/italic, <mark> highlighting, lists, local images -
via help_renderer.py), with an "Open in browser" button on every doc for full-
fidelity viewing of anything the mini-renderer can't handle. Each topic ships
two documents: a short human-readable guide, and a separate, denser technical
reference meant for an AI assistant to read in some future session rather than
for a person to read start-to-end - detailed, not verbose."""

import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from gc_pipeline.help_renderer import render_html_file

DOCS_DIR = Path(__file__).resolve().parent / "help_docs"

# (topic label, human-guide filename, AI-reference filename)
TOPICS = [
    (
        "Missing CSV export (Agilent method)",
        "agilent_export_fix.html",
        "agilent_export_fix_ai.html",
    ),
]


class HelpPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._image_refs = []
        self._current_path = None
        self._build()

    def _build(self):
        left = ttk.Frame(self)
        left.pack(side="left", fill="y", padx=(8, 4), pady=8)
        ttk.Label(left, text="Topics", font=("", 10, "bold")).pack(anchor="w", pady=(0, 4))
        for label, human_file, ai_file in TOPICS:
            box = ttk.LabelFrame(left, text=label)
            box.pack(fill="x", pady=(0, 8))
            ttk.Button(box, text="View guide", command=lambda f=human_file: self.show_doc(f)).pack(
                fill="x", padx=4, pady=(4, 2)
            )
            ttk.Button(
                box, text="View technical (AI) reference", command=lambda f=ai_file: self.show_doc(f)
            ).pack(fill="x", padx=4, pady=(0, 4))

        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True, padx=(4, 8), pady=8)

        toolbar = ttk.Frame(right)
        toolbar.pack(fill="x")
        self.title_label = ttk.Label(toolbar, text="Select a topic on the left.", font=("", 11, "bold"))
        self.title_label.pack(side="left")
        self.browser_button = ttk.Button(
            toolbar, text="Open in browser", command=self._open_in_browser, state="disabled"
        )
        self.browser_button.pack(side="right")

        text_frame = ttk.Frame(right)
        text_frame.pack(fill="both", expand=True, pady=(6, 0))
        scroll = ttk.Scrollbar(text_frame, orient="vertical")
        self.text = tk.Text(
            text_frame, wrap="word", state="disabled", relief="flat", padx=10, pady=8,
            yscrollcommand=scroll.set,
        )
        scroll.config(command=self.text.yview)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        if TOPICS:
            self.show_doc(TOPICS[0][1])

    def show_doc(self, filename):
        path = DOCS_DIR / filename
        self._current_path = path
        self.title_label.config(text=filename)
        self.browser_button.config(state="normal")
        if not path.exists():
            self.text.config(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", f"{filename} hasn't been written yet.\n\nExpected at:\n{path}")
            self.text.config(state="disabled")
            self._image_refs = []
            return
        self._image_refs = render_html_file(self.text, path)

    def _open_in_browser(self):
        if self._current_path is not None and self._current_path.exists():
            webbrowser.open(self._current_path.resolve().as_uri())
