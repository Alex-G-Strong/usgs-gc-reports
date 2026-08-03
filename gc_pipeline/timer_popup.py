"""A stacked countdown timer (run a sequence of named timer blocks back-to-back,
each ending in an alarm) - opened as a popup from the main app, shared across every
tab. Adapted from the user's standalone stack_timer_gui.py (originally a tk.Tk root
window) into a tk.Toplevel so it can live alongside the rest of the app instead of
needing its own separate mainloop."""

import json
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, simpledialog

try:
    import winsound
except ImportError:
    winsound = None

LIBRARY_PATH = Path(__file__).resolve().parent.parent / "timer_library.json"


class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tipwindow = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, event=None):
        if self.tipwindow or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 2
        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(tw, text=self.text, background="#ffffe0", relief=tk.SOLID, borderwidth=1,
                          font=("tahoma", "8", "normal"))
        label.pack(ipadx=4, ipady=2)

    def hide(self, event=None):
        if self.tipwindow:
            self.tipwindow.destroy()
            self.tipwindow = None


@dataclass
class TimerBlock:
    name: str
    duration_seconds: int

    def format_duration(self):
        minutes = self.duration_seconds // 60
        seconds = self.duration_seconds % 60
        return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def parse_duration(text):
    text = text.strip().lower()
    if not text:
        raise ValueError("Duration is required.")
    if text.endswith("m"):
        return int(text[:-1]) * 60
    if text.endswith("s"):
        return int(text[:-1])
    if ":" in text:
        parts = text.split(":")
        minutes = int(parts[0])
        seconds = int(parts[1]) if len(parts) > 1 else 0
        return minutes * 60 + seconds
    return int(text)


class StackTimerWindow(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Stacked Timer")
        self.resizable(False, False)
        self.blocks: list[TimerBlock] = []
        self.running = False
        self.paused = False
        self.current_index = 0
        self.current_remaining = 0
        self.highlight_index = None
        self.library_path = LIBRARY_PATH
        self.saved_timers: dict = {}
        # The name this stack was last loaded from (or saved as) in the library -
        # None means "not tied to any library entry yet" (fresh/never-saved), so
        # saving prompts fresh rather than silently renaming something unrelated.
        self._loaded_library_name = None
        self._load_library()
        self._build_ui()

    def _build_ui(self):
        frame = tk.Frame(self, padx=10, pady=10)
        frame.pack(fill=tk.BOTH, expand=True)

        # Editable header for the currently-loaded (or about to be saved) sequence's
        # name - loading a saved timer fills this in, and editing it here renames
        # that library entry on the next save rather than requiring a separate
        # rename flow inside the library dialog.
        name_frame = tk.Frame(frame)
        name_frame.grid(row=0, column=0, columnspan=2, pady=(0, 8), sticky="ew")
        name_frame.grid_columnconfigure(1, weight=1)
        tk.Label(name_frame, text="Sequence name:").grid(row=0, column=0, sticky="w")
        self.sequence_name_var = tk.StringVar()
        self.sequence_name_entry = tk.Entry(name_frame, textvariable=self.sequence_name_var)
        self.sequence_name_entry.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        library_frame = tk.Frame(frame)
        library_frame.grid(row=1, column=0, columnspan=2, pady=(0, 0), sticky="ew")

        load_button = tk.Button(library_frame, text="Load From Library", command=self._open_library_dialog)
        load_button.pack(side=tk.LEFT, expand=True, fill=tk.X)

        save_button = tk.Button(library_frame, text="Save Current to Library", command=self._prompt_save_timer)
        save_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(10, 0))

        separator_top = tk.Frame(frame, height=2, bd=1, relief=tk.SUNKEN)
        separator_top.grid(row=2, column=0, columnspan=2, pady=(10, 10), sticky="ew")

        control_frame = tk.Frame(frame)
        control_frame.grid(row=3, column=0, columnspan=2, sticky="ew")
        control_frame.grid_columnconfigure(0, weight=1)
        control_frame.grid_columnconfigure(1, weight=0)

        timer_fields = tk.Frame(control_frame)
        timer_fields.grid(row=0, column=0, sticky="nsew")
        timer_fields.grid_columnconfigure(1, weight=1)

        tk.Label(timer_fields, text="Timer Label:").grid(row=0, column=0, sticky="w")
        self.label_entry = tk.Entry(timer_fields, width=12)
        self.label_entry.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        self.label_entry.insert(0, "Alarm")

        tk.Label(timer_fields, text="Duration:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.duration_entry = tk.Entry(timer_fields, width=12)
        self.duration_entry.grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=(4, 0))
        self.duration_entry.insert(0, "30s")

        tk.Label(timer_fields, text="Sound:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.sound_var = tk.StringVar(value="Click")
        sound_options = ["Click", "Beep", "None"]
        self.sound_menu = tk.OptionMenu(timer_fields, self.sound_var, *sound_options)
        self.sound_menu.config(width=12)
        self.sound_menu.grid(row=2, column=1, sticky="ew", padx=(5, 0), pady=(4, 0))

        add_button = tk.Button(control_frame, text="Add Block", command=self.add_block, width=10)
        add_button.grid(row=0, column=1, rowspan=3, padx=(10, 0), sticky="nsew")

        separator_mid = tk.Frame(frame, height=2, bd=1, relief=tk.SUNKEN)
        separator_mid.grid(row=6, column=0, columnspan=2, pady=(10, 10), sticky="ew")

        buttons_frame = tk.Frame(frame)
        buttons_frame.grid(row=7, column=0, columnspan=2, pady=(0, 0), sticky="ew")

        self.edit_button = tk.Button(buttons_frame, text="Edit Block", command=self.edit_block, state=tk.DISABLED)
        self.edit_button.pack(side=tk.LEFT, expand=True, fill=tk.X)

        self.delete_button = tk.Button(
            buttons_frame, text="Delete Block", command=self.delete_block, state=tk.DISABLED
        )
        self.delete_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(10, 0))

        self.up_button = tk.Button(
            buttons_frame, text="Move Up", command=lambda: self.move_selected_block(-1), state=tk.DISABLED
        )
        self.up_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(10, 0))

        self.down_button = tk.Button(
            buttons_frame, text="Move Down", command=lambda: self.move_selected_block(1), state=tk.DISABLED
        )
        self.down_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(10, 0))

        clear_button = tk.Button(buttons_frame, text="Clear Stack", command=self.clear_blocks)
        clear_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(10, 0))

        separator_edit = tk.Frame(frame, height=2, bd=1, relief=tk.SUNKEN)
        separator_edit.grid(row=8, column=0, columnspan=2, pady=(10, 10), sticky="ew")

        self.blocks_listbox = tk.Listbox(frame, width=50, height=10)
        self.blocks_listbox.grid(row=9, column=0, columnspan=3, pady=(0, 10), sticky="nsew")
        self.blocks_listbox.bind("<<ListboxSelect>>", self._on_select)
        self.blocks_listbox.bind("<ButtonPress-1>", self._on_drag_start)
        self.blocks_listbox.bind("<B1-Motion>", self._on_drag_motion)
        self.blocks_listbox.bind("<ButtonRelease-1>", self._on_drag_end)

        self.status_label = tk.Label(frame, text="Ready", anchor="w")
        self.status_label.grid(row=10, column=0, columnspan=3, pady=(10, 0), sticky="ew")

        separator = tk.Frame(frame, height=2, bd=1, relief=tk.SUNKEN)
        separator.grid(row=11, column=0, columnspan=3, pady=(12, 0), sticky="ew")

        playback_frame = tk.Frame(frame)
        playback_frame.grid(row=12, column=0, columnspan=3, pady=(10, 0), sticky="ew")

        self.start_button = tk.Button(playback_frame, text="▶", command=self.start_timer, width=6)
        self.start_button.pack(side=tk.LEFT, expand=True, fill=tk.X)
        ToolTip(self.start_button, "Start")

        self.play_button = tk.Button(
            playback_frame, text="⏸", command=self.pause_resume_timer, state=tk.DISABLED, width=6
        )
        self.play_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(10, 0))
        ToolTip(self.play_button, "Pause / Resume")

        self.stop_button = tk.Button(playback_frame, text="🔄", command=self.stop_timer, state=tk.DISABLED, width=6)
        self.stop_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(10, 0))
        ToolTip(self.stop_button, "Reset")

        frame.grid_columnconfigure(1, weight=1)

    def _refresh_listbox(self):
        self.blocks_listbox.delete(0, tk.END)
        for block in self.blocks:
            self.blocks_listbox.insert(tk.END, f"{block.name} — {block.format_duration()}")
        self._apply_highlight()

    def _apply_highlight(self):
        for i in range(self.blocks_listbox.size()):
            self.blocks_listbox.itemconfig(i, bg="white")
        if self.highlight_index is not None and 0 <= self.highlight_index < self.blocks_listbox.size():
            self.blocks_listbox.itemconfig(self.highlight_index, bg="light green")

    def _highlight_current_block(self, index):
        self.highlight_index = index
        self._apply_highlight()

    def _clear_highlight(self):
        self.highlight_index = None
        self._apply_highlight()

    def _selected_index(self):
        selection = self.blocks_listbox.curselection()
        return selection[0] if selection else None

    def _load_library(self):
        if not self.library_path.exists():
            self.saved_timers = {}
            return
        try:
            with self.library_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, dict):
                    self.saved_timers = {
                        name: sequence for name, sequence in data.items()
                        if isinstance(name, str) and isinstance(sequence, list)
                    }
                else:
                    self.saved_timers = {}
        except (json.JSONDecodeError, OSError):
            self.saved_timers = {}

    def _save_library(self):
        try:
            with self.library_path.open("w", encoding="utf-8") as handle:
                json.dump(self.saved_timers, handle, indent=2)
        except OSError as exc:
            messagebox.showerror("Save failed", f"Could not save library: {exc}")

    def _get_block_sequence_data(self):
        return [{"name": block.name, "duration_seconds": block.duration_seconds} for block in self.blocks]

    def _set_block_sequence_data(self, sequence):
        self.blocks = [TimerBlock(name=item["name"], duration_seconds=int(item["duration_seconds"])) for item in
                        sequence]
        self._refresh_listbox()

    def _prompt_save_timer(self):
        if not self.blocks:
            messagebox.showinfo("No blocks", "Add at least one timer block before saving.", parent=self)
            return
        # The header name field is the source of truth now, not a separate prompt -
        # editing it after loading a saved sequence is how you rename it.
        name = self.sequence_name_var.get().strip()
        if not name:
            name = simpledialog.askstring(
                "Save Timer Sequence", "Enter a name for this timer sequence:", parent=self
            )
            if not name:
                return
            name = name.strip()
            if not name:
                messagebox.showerror("Invalid name", "Timer name cannot be empty.", parent=self)
                return

        renaming_from = (
            self._loaded_library_name
            if self._loaded_library_name is not None and self._loaded_library_name != name
            else None
        )
        if renaming_from is not None:
            if name in self.saved_timers and not messagebox.askyesno(
                "Overwrite existing",
                f"'{name}' already exists in the library. Renaming '{renaming_from}' to '{name}' will "
                "overwrite it. Continue?",
                parent=self,
            ):
                return
            self.saved_timers.pop(renaming_from, None)
        elif name in self.saved_timers:
            if not messagebox.askyesno(
                "Overwrite existing", f"A timer named '{name}' already exists. Overwrite?", parent=self
            ):
                return

        self.saved_timers[name] = self._get_block_sequence_data()
        self._save_library()
        self.sequence_name_var.set(name)
        self._loaded_library_name = name
        if renaming_from is not None:
            self.status_label.config(text=f"Renamed '{renaming_from}' to '{name}' and saved.")
        else:
            self.status_label.config(text=f"Saved '{name}' to library.")

    def _open_library_dialog(self):
        dialog = tk.Toplevel(self)
        dialog.title("Load Library Timer")
        dialog.resizable(False, False)

        tk.Label(dialog, text="Saved timers:").pack(anchor="w", padx=10, pady=(10, 0))
        listbox = tk.Listbox(dialog, width=40, height=10)
        listbox.pack(padx=10, pady=(0, 10), fill=tk.BOTH)

        for name in sorted(self.saved_timers.keys()):
            listbox.insert(tk.END, name)

        def load_selected():
            selection = listbox.curselection()
            if not selection:
                messagebox.showinfo("Select timer", "Select a saved timer to load.", parent=dialog)
                return
            name = listbox.get(selection[0])
            sequence = self.saved_timers.get(name)
            if sequence is None:
                messagebox.showerror("Error", "Selected timer could not be loaded.", parent=dialog)
                return
            self._set_block_sequence_data(sequence)
            self.sequence_name_var.set(name)
            self._loaded_library_name = name
            self._clear_template_fields()
            self.status_label.config(text=f"Loaded '{name}' from library.")
            dialog.destroy()

        def delete_selected():
            selection = listbox.curselection()
            if not selection:
                messagebox.showinfo("Select timer", "Select a saved timer to delete.", parent=dialog)
                return
            name = listbox.get(selection[0])
            if not messagebox.askyesno("Confirm delete", f"Delete '{name}' from the library?", parent=dialog):
                return
            self.saved_timers.pop(name, None)
            self._save_library()
            if self._loaded_library_name == name:
                # Its old name no longer refers to anything - next save should
                # treat it as unattached (prompt fresh, or save under the still-
                # editable header name as a brand new entry) rather than silently
                # trying to "rename" an entry that isn't there anymore.
                self._loaded_library_name = None
            self.status_label.config(text=f"Deleted '{name}' from library.")
            listbox.delete(selection[0])

        load_button = tk.Button(dialog, text="Load", command=load_selected)
        load_button.pack(side=tk.LEFT, padx=10, pady=(0, 10), expand=True, fill=tk.X)

        delete_button = tk.Button(dialog, text="Delete", command=delete_selected)
        delete_button.pack(side=tk.LEFT, padx=10, pady=(0, 10), expand=True, fill=tk.X)

        close_button = tk.Button(dialog, text="Close", command=dialog.destroy)
        close_button.pack(side=tk.LEFT, padx=10, pady=(0, 10), expand=True, fill=tk.X)

    def _on_select(self, event):
        index = self._selected_index()
        state = tk.NORMAL if index is not None else tk.DISABLED
        self.edit_button.config(state=state)
        self.delete_button.config(state=state)
        self.up_button.config(state=state)
        self.down_button.config(state=state)

    def _on_drag_start(self, event):
        self._drag_index = self.blocks_listbox.nearest(event.y)

    def _on_drag_motion(self, event):
        if getattr(self, "_drag_index", None) is None:
            return
        current_index = self.blocks_listbox.nearest(event.y)
        if current_index != self._drag_index and 0 <= current_index < len(self.blocks):
            self.blocks[self._drag_index], self.blocks[current_index] = (
                self.blocks[current_index], self.blocks[self._drag_index]
            )
            self._refresh_listbox()
            self.blocks_listbox.selection_clear(0, tk.END)
            self.blocks_listbox.selection_set(current_index)
            self._drag_index = current_index

    def _on_drag_end(self, event):
        self._drag_index = None

    def _move_block(self, src, dst):
        self.blocks.insert(dst, self.blocks.pop(src))
        self._refresh_listbox()
        self.blocks_listbox.selection_clear(0, tk.END)
        self.blocks_listbox.selection_set(dst)

    def move_selected_block(self, direction):
        index = self._selected_index()
        if index is None:
            return
        dest = index + direction
        if 0 <= dest < len(self.blocks):
            self._move_block(index, dest)

    def edit_block(self):
        index = self._selected_index()
        if index is None:
            return
        block = self.blocks[index]

        edit_window = tk.Toplevel(self)
        edit_window.title("Edit Timer Block")
        edit_window.resizable(False, False)

        tk.Label(edit_window, text="Label:").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 0))
        label_entry = tk.Entry(edit_window, width=30)
        label_entry.grid(row=0, column=1, padx=10, pady=(10, 0))
        label_entry.insert(0, block.name)

        tk.Label(edit_window, text="Duration:").grid(row=1, column=0, sticky="w", padx=10, pady=(10, 0))
        duration_entry = tk.Entry(edit_window, width=30)
        duration_entry.grid(row=1, column=1, padx=10, pady=(10, 0))
        duration_entry.insert(0, f"{block.duration_seconds // 60}:{block.duration_seconds % 60:02d}")

        def save_edit():
            try:
                duration = parse_duration(duration_entry.get().strip())
                if duration <= 0:
                    raise ValueError("Duration must be greater than zero.")
            except ValueError as exc:
                messagebox.showerror("Invalid duration", str(exc), parent=edit_window)
                return
            block.name = label_entry.get().strip() or "Alarm"
            block.duration_seconds = duration
            self._refresh_listbox()
            self.blocks_listbox.selection_clear(0, tk.END)
            self.blocks_listbox.selection_set(index)
            self.status_label.config(text=f"Updated block: {block.name} — {block.format_duration()}")
            edit_window.destroy()

        save_button = tk.Button(edit_window, text="Save", command=save_edit)
        save_button.grid(row=2, column=0, columnspan=2, pady=10)

    def delete_block(self):
        index = self._selected_index()
        if index is None:
            return
        self.blocks.pop(index)
        self._refresh_listbox()
        self.status_label.config(text="Deleted selected block.")
        self._on_select(None)

    def _clear_template_fields(self):
        """Resets the Add Block template (label + duration) to empty, as if the
        block that was just written got sent off into the stack and the form is
        ready for the next one - called after a block is added, and after loading a
        different sequence in, so stale text from before never lingers."""
        self.label_entry.delete(0, tk.END)
        self.duration_entry.delete(0, tk.END)

    def add_block(self):
        if self.running:
            return
        label = self.label_entry.get().strip() or "Alarm"
        duration_text = self.duration_entry.get().strip()
        try:
            duration = parse_duration(duration_text)
            if duration <= 0:
                raise ValueError("Duration must be greater than zero.")
        except ValueError as exc:
            messagebox.showerror("Invalid duration", str(exc))
            return

        block = TimerBlock(name=label, duration_seconds=duration)
        self.blocks.append(block)
        self.blocks_listbox.insert(tk.END, f"{label} — {block.format_duration()}")
        self.status_label.config(text=f"Added block: {label} — {block.format_duration()}")
        self._clear_template_fields()

    def clear_blocks(self):
        if self.running:
            return
        self.blocks.clear()
        self.blocks_listbox.delete(0, tk.END)
        self.sequence_name_var.set("")
        self._loaded_library_name = None
        self.status_label.config(text="Cleared timer stack.")

    def start_timer(self):
        if self.running:
            return
        if not self.blocks:
            messagebox.showinfo("No blocks", "Add at least one timer block first.")
            return
        self.running = True
        self.paused = False
        self.current_index = 0
        self.current_remaining = self.blocks[0].duration_seconds
        self.start_button.config(state=tk.DISABLED)
        self.play_button.config(state=tk.NORMAL, text="⏸")
        self.stop_button.config(state=tk.NORMAL)
        self.status_label.config(text="Starting timer stack...")
        thread = threading.Thread(target=self._run_blocks, daemon=True)
        thread.start()

    def stop_timer(self):
        if not self.running:
            return
        self.running = False
        self.paused = False
        self.status_label.config(text="Timer reset.")
        self.play_button.config(state=tk.DISABLED, text="⏸")
        self.stop_button.config(state=tk.DISABLED)
        self.start_button.config(state=tk.NORMAL)
        self._clear_highlight()

    def pause_resume_timer(self):
        if not self.running:
            return
        self.paused = not self.paused
        if self.paused:
            self.play_button.config(text="▶")
            self.status_label.config(text="Timer paused.")
        else:
            self.play_button.config(text="⏸")
            self.status_label.config(text="Resuming timer...")

    def _run_blocks(self):
        while self.current_index < len(self.blocks) and self.running:
            block = self.blocks[self.current_index]
            self._highlight_current_block(self.current_index)
            self._set_status(f"Running: {block.name} ({block.format_duration()})")
            while self.current_remaining >= 0 and self.running:
                if self.paused:
                    time.sleep(0.2)
                    continue
                self._update_countdown(block.name, self.current_remaining)
                time.sleep(1)
                self.current_remaining -= 1
            if not self.running:
                break
            self._play_alarm(block.name)
            self.current_index += 1
            if self.current_index < len(self.blocks):
                self.current_remaining = self.blocks[self.current_index].duration_seconds
        self._clear_highlight()
        self._finish_run()

    def _set_status(self, text):
        self.after(0, lambda: self.status_label.config(text=text))

    def _update_countdown(self, name, remaining):
        mins, secs = divmod(remaining, 60)
        countdown_text = f"{mins:02d}:{secs:02d}"
        self.after(0, lambda: self.status_label.config(text=f"{name} — {countdown_text}"))

    def _play_alarm(self, name):
        self.after(0, lambda: self.status_label.config(text=f"Alarm: {name}"))
        sound_choice = self.sound_var.get()
        if sound_choice == "Click":
            if winsound:
                winsound.Beep(1200, 40)
            else:
                self.after(0, self.bell)
        elif sound_choice == "Beep":
            if winsound:
                winsound.Beep(440, 200)
            else:
                self.after(0, self.bell)
        time.sleep(0.2)

    def _finish_run(self):
        self.after(0, self._reset_ui)

    def _reset_ui(self):
        self.running = False
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.status_label.config(text="All timer blocks complete.")
