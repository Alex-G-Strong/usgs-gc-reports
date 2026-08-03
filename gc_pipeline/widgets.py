"""Small reusable Tkinter widgets shared across the app's windows."""

import tkinter as tk
from tkinter import ttk

from gc_pipeline.channels import AR_CHANNEL_GASES, KNOWN_HE_CHANNEL_GASES

MAX_POPUP_ITEMS = 15  # beyond this many options, the popup scrolls instead of growing

FAST_HSCROLL_UNITS = 6  # units per wheel notch - Tk's default (~1) feels sluggish on wide tables


def bind_fast_hscroll(tree, units=FAST_HSCROLL_UNITS):
    """Shift+MouseWheel and Alt+MouseWheel both scroll horizontally (Windows uses
    Shift+wheel by convention; Alt+wheel is offered as an alternative since it doesn't
    require reaching across the keyboard). Tk's default step per notch is small enough
    to feel unresponsive on a wide table - move several units per notch instead.
    Shared so every table in the app scrolls at the same, deliberately-snappier speed."""
    def _on_wheel(event):
        tree.xview_scroll(int(-units * (event.delta / 120)), "units")
        return "break"
    tree.bind("<Shift-MouseWheel>", _on_wheel)
    tree.bind("<Alt-MouseWheel>", _on_wheel)


def justify_columns(tree, column_ids, min_width=30):
    """Proportionally resize `column_ids` to fill (or, if they're wider than
    available, shrink to fit within) the tree's visible width - a one-shot manual
    action (the "Justify columns" button), not a persistent auto-resize, so columns
    don't keep changing size every time one gets added/removed elsewhere. Shrinking
    never takes a column below `min_width`, so headers stay legible even when the
    whole table has to squeeze into a narrow panel."""
    if not column_ids:
        return
    tree.update_idletasks()
    visible_width = tree.winfo_width()
    fixed_width = 0
    if "tree" in str(tree.cget("show")):
        fixed_width += tree.column("#0", "width")
    for c in tree["columns"]:
        if c not in column_ids:
            fixed_width += tree.column(c, "width")
    justify_total = sum(tree.column(c, "width") for c in column_ids)
    available = visible_width - fixed_width
    if justify_total <= 0:
        return
    if available > justify_total:
        extra = available - justify_total
        for c in column_ids:
            w = tree.column(c, "width")
            share = extra * (w / justify_total)
            tree.column(c, width=int(w + share))
    elif available < justify_total:
        deficit = justify_total - available
        shrinkable = {c: max(tree.column(c, "width") - min_width, 0) for c in column_ids}
        total_shrinkable = sum(shrinkable.values())
        if total_shrinkable <= 0:
            return
        deficit = min(deficit, total_shrinkable)
        for c in column_ids:
            if shrinkable[c] <= 0:
                continue
            share = deficit * (shrinkable[c] / total_shrinkable)
            w = tree.column(c, "width")
            tree.column(c, width=int(w - share))


def bind_tooltip(widget, text_getter, wraplength=None):
    """Shows a small borderless tooltip near the cursor after a brief hover delay -
    text_getter may be a plain string or a zero-arg callable (re-evaluated on every
    hover), so the tooltip can reflect state that changes after binding, like a
    status label whose underlying value updates over time. wraplength wraps longer
    text onto multiple lines instead of rendering one very wide unbroken line."""
    state = {"after_id": None, "tip": None}

    def get_text():
        return text_getter() if callable(text_getter) else text_getter

    def cancel_pending():
        if state["after_id"] is not None:
            widget.after_cancel(state["after_id"])
            state["after_id"] = None

    def hide(_event=None):
        cancel_pending()
        if state["tip"] is not None:
            state["tip"].destroy()
            state["tip"] = None

    def show():
        state["after_id"] = None
        text = get_text()
        if not text:
            return
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        x = widget.winfo_rootx()
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        tip.geometry(f"+{x}+{y}")
        ttk.Label(
            tip, text=text, background="#ffffe0", relief="solid", borderwidth=1, padding=4,
            wraplength=wraplength, justify="left",
        ).pack()
        state["tip"] = tip

    def on_enter(_event=None):
        cancel_pending()
        state["after_id"] = widget.after(500, show)

    widget.bind("<Enter>", on_enter, add="+")
    widget.bind("<Leave>", hide, add="+")
    widget.bind("<Button-1>", hide, add="+")


def bind_treeview_heading_tooltip(tree, label_getter):
    """Hovering a ttk.Treeview column heading shows its full label as a tooltip -
    handy when a column is narrower than its name (common once several columns are
    packed into one table) and the heading text just clips instead of wrapping.
    label_getter(col_id) -> display text, where col_id is "#0" for the tree column
    or "#N" for a data column; return None/"" to show nothing for that one."""
    state = {"after_id": None, "tip": None, "col": None}

    def cancel_pending():
        if state["after_id"] is not None:
            tree.after_cancel(state["after_id"])
            state["after_id"] = None

    def hide(_event=None):
        cancel_pending()
        if state["tip"] is not None:
            state["tip"].destroy()
            state["tip"] = None
        state["col"] = None

    def show(col_id, x_root, y_root):
        state["after_id"] = None
        text = label_getter(col_id)
        if not text:
            return
        tip = tk.Toplevel(tree)
        tip.wm_overrideredirect(True)
        tip.geometry(f"+{x_root}+{y_root + 16}")
        ttk.Label(
            tip, text=text, background="#ffffe0", relief="solid", borderwidth=1, padding=4
        ).pack()
        state["tip"] = tip

    def on_motion(event):
        if tree.identify_region(event.x, event.y) != "heading":
            hide()
            return
        col_id = tree.identify_column(event.x)
        if col_id == state["col"]:
            return
        hide()
        state["col"] = col_id
        state["after_id"] = tree.after(500, lambda: show(col_id, event.x_root, event.y_root))

    tree.bind("<Motion>", on_motion, add="+")
    tree.bind("<Leave>", hide, add="+")
    tree.bind("<Button-1>", lambda _e: hide(), add="+")


def _is_descendant(widget, ancestor):
    w = widget
    while w is not None:
        if w == ancestor:
            return True
        w = w.master
    return False


class _PopupButton(ttk.Button):
    """Base for a button that opens a borderless popup below itself when clicked -
    closes on an outside click or Escape. Subclasses implement `_build_popup(popup)`
    to fill it with content, and may override `_on_popup_closed()` to clear their
    own popup-scoped widget refs.

    Uses a plain Toplevel (not a tk.Menu) so it can hold arbitrary content and stay
    open across multiple interactions - a native Menu dismisses after every click,
    which makes multi-step interaction (checking several boxes, clicking a button)
    painfully slow."""

    def __init__(self, parent, text=""):
        super().__init__(parent, text=text, command=self._toggle_popup)
        self._popup = None
        self._popup_frontmost = True  # tracked so the poll only acts on state changes
        # Bound once, permanently - _on_outside_click no-ops whenever the popup isn't
        # open. Binding via bind_all is necessarily global (Tk offers no way to scope
        # a <Button-1> catch-all to "this window"), and add="+" lets it coexist with
        # any other window's own bind_all("<Button-1>") handlers without clobbering
        # them. Never unbound: unbind_all(sequence) has no funcid-scoped form in
        # tkinter and would strip every other bind_all("<Button-1>") handler in the
        # app, not just this one.
        self.bind_all("<Button-1>", self._on_outside_click, add="+")

    def _toggle_popup(self):
        if self._popup is not None:
            self._close_popup()
        else:
            self._open_popup()

    def _open_popup(self):
        popup = tk.Toplevel(self)
        popup.wm_overrideredirect(True)
        # Deliberately NOT -topmost: it should stay open across an Alt+Tab away from
        # this app (rather than auto-closing, which is what it used to do), but sit
        # *behind* whatever the user switched to, not floating on top of it.
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height()
        popup.geometry(f"+{x}+{y}")

        self._popup = popup
        self._popup_frontmost = True
        self._build_popup(popup)

        popup.bind("<Escape>", lambda _e: self._close_popup())

        # Override-redirect windows aren't managed by the window manager, so nothing
        # automatically lowers this popup behind another application's window when
        # the user Alt+Tabs away, or raises it back when they return - do that
        # ourselves. Polled rather than event-driven since neither <FocusOut> nor
        # <Deactivate> proved reliable for "did the whole app lose focus" across
        # platforms/window managers; focus_displayof() (None only when focus is on a
        # *different* application) is cheap enough to check on a 300ms timer.
        self._poll_focus_state()

    def _build_popup(self, popup):
        """Subclasses override: populate `popup` with whatever content they need."""
        raise NotImplementedError

    def _on_popup_closed(self):
        """Optional hook for subclasses to clear their own popup-scoped widget refs."""

    def _poll_focus_state(self):
        if self._popup is None:
            return
        app_focused = self.focus_displayof() is not None
        if app_focused != self._popup_frontmost:
            if app_focused:
                self._popup.lift()
            else:
                self._popup.lower()
            self._popup_frontmost = app_focused
        self._popup.after(300, self._poll_focus_state)

    def _on_outside_click(self, event):
        if not self.winfo_exists() or self._popup is None:
            return
        widget = event.widget
        if widget is self or _is_descendant(widget, self._popup):
            return
        self._close_popup()

    def _close_popup(self):
        if self._popup is None:
            return
        self._popup.destroy()
        self._popup = None
        self._on_popup_closed()

    def close_popup(self):
        """Public alias - lets a caller close this popup from the outside, e.g. an
        ActionsPopup action that should dismiss the popup after running."""
        self._close_popup()


class DropdownChecklist(_PopupButton):
    """A dropdown checklist for filters whose option list can grow over time - unlike
    GasFilterBar's always-visible inline chips (fine for a short, fixed list like the
    5 gases), a growing list of date folders is better tucked behind a dropdown."""

    def __init__(self, parent, label, options, on_change):
        self.label = label
        self.on_change = on_change
        self.vars = {}
        self._popup_frame = None
        self._popup_canvas = None
        self._popup_vsb = None
        self._popup_inner_window = None
        super().__init__(parent)
        self.set_options(options)

    def set_options(self, options):
        old_state = {opt: var.get() for opt, var in self.vars.items()}
        self.vars = {}
        for opt in options:
            var = tk.BooleanVar(value=old_state.get(opt, True))
            self.vars[opt] = var
        self._update_label()
        if self._popup is not None:
            self._rebuild_popup_contents()

    def selected(self):
        return [o for o, v in self.vars.items() if v.get()]

    def set_checked(self, option, checked):
        """Programmatic sync (e.g. after a column is removed by some other gesture,
        like a header click) - the popup's Checkbutton already tracks the same
        BooleanVar live, so flipping it plus refreshing the button label is enough;
        no need to rebuild the popup even if it's currently open."""
        if option in self.vars:
            self.vars[option].set(checked)
            self._update_label()

    def select_all(self):
        for v in self.vars.values():
            v.set(True)
        self._on_change()

    def _build_popup(self, popup):
        border = ttk.Frame(popup, relief="solid", borderwidth=1)
        border.pack()

        # A Canvas + inner Frame (rather than packing checkbuttons straight into
        # `border`) so a long option list can scroll instead of growing the popup
        # off the bottom of the screen - capped at MAX_POPUP_ITEMS rows tall.
        canvas = tk.Canvas(border, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(border, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(canvas)
        inner_window = canvas.create_window((0, 0), window=inner, anchor="nw")

        self._popup_canvas = canvas
        self._popup_vsb = vsb
        self._popup_frame = inner
        self._popup_inner_window = inner_window

        self._rebuild_popup_contents()

    def _on_popup_closed(self):
        self._popup_frame = None
        self._popup_canvas = None
        self._popup_vsb = None
        self._popup_inner_window = None

    def _rebuild_popup_contents(self):
        for child in self._popup_frame.winfo_children():
            child.destroy()
        for opt, var in self.vars.items():
            cb = ttk.Checkbutton(
                self._popup_frame, text=opt, variable=var, command=self._on_change
            )
            cb.pack(anchor="w", padx=4, pady=1)
            cb.bind("<MouseWheel>", self._on_popup_mousewheel)

        if self._popup is None:
            return
        self._popup_frame.update_idletasks()
        children = self._popup_frame.winfo_children()
        req_width = self._popup_frame.winfo_reqwidth()
        item_count = len(children)
        # Measured from the inner frame's own total height, not extrapolated from a
        # single child's winfo_reqheight() - that undercounts by however much pack()
        # padding (pady=1 below) adds around each row, which isn't part of a
        # checkbutton's own requested size. That shortfall compounds across every
        # row, and since it silently shrinks the canvas below the real content
        # height with no scrollbar to reach the difference (only shown once
        # item_count > MAX_POPUP_ITEMS), the last row(s) were clipped with no way
        # to see them even for short lists.
        total_height = self._popup_frame.winfo_reqheight()
        item_height = (total_height / item_count) if item_count else 20
        visible_count = min(item_count, MAX_POPUP_ITEMS) or 1
        self._popup_canvas.configure(width=req_width, height=round(visible_count * item_height))
        self._popup_canvas.itemconfigure(self._popup_inner_window, width=req_width)
        self._popup_canvas.configure(scrollregion=(0, 0, req_width, total_height))
        self._popup_canvas.bind("<MouseWheel>", self._on_popup_mousewheel)
        # The checkbuttons are packed anchor="w" (not fill="x"), so there's blank
        # space around/between them that belongs to this inner Frame, not to the
        # canvas or any checkbutton - without this, the wheel only worked when the
        # cursor was exactly over a checkbutton's own text.
        self._popup_frame.bind("<MouseWheel>", self._on_popup_mousewheel)

        if item_count > MAX_POPUP_ITEMS:
            self._popup_vsb.pack(side="right", fill="y")
        else:
            self._popup_vsb.pack_forget()

    def _on_popup_mousewheel(self, event):
        self._popup_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_change(self):
        self._update_label()
        self.on_change()

    def _update_label(self):
        selected = [o for o, v in self.vars.items() if v.get()]
        if not self.vars or len(selected) == len(self.vars):
            self.config(text=f"{self.label}: All ▾")
        elif not selected:
            self.config(text=f"{self.label}: none ▾")
        else:
            self.config(text=f"{self.label}: {len(selected)} selected ▾")


class GasChannelFilter(_PopupButton):
    """A gas filter combining every known gas (both instrument channels) behind one
    dropdown: an All/None toggle at the top, then the gases grouped under an "Ar
    channel" / "He channel" heading apiece (gases that aren't cleanly one or the
    other - e.g. N2, seen on both - are listed under a final "Other" heading)."""

    def __init__(self, parent, label, on_change):
        self.label = label
        self.on_change = on_change
        self.vars = {}  # gas -> BooleanVar
        self._groups = []  # [(heading, [gas, ...]), ...] - display order
        self._popup_frame = None
        super().__init__(parent)
        self.set_gases([], {})

    def set_gases(self, gases, gas_channels):
        """gases: every known gas name. gas_channels: {gas: {channel, ...}} from
        db.get_gas_channels - used to decide which heading each gas sorts under;
        a gas's own checked/unchecked state is preserved across calls.

        Always includes the full known Ar/He gas lists (channels.AR_CHANNEL_GASES /
        KNOWN_HE_CHANNEL_GASES) even if `gases` doesn't - so e.g. the He-channel
        gases show up as filterable options before any He-channel run has ever
        actually been ingested, not only once real data exists to classify them."""
        old_state = {gas: var.get() for gas, var in self.vars.items()}
        all_gases = list(dict.fromkeys(list(gases) + list(AR_CHANNEL_GASES) + list(KNOWN_HE_CHANNEL_GASES)))
        ar, he, other = [], [], []
        for gas in all_gases:
            observed_channels = gas_channels.get(gas) or set()
            if observed_channels == {"Ar"}:
                ar.append(gas)
            elif observed_channels == {"He"}:
                he.append(gas)
            elif observed_channels:
                other.append(gas)  # actually seen on both (or an unrecognized) channel
            elif gas in AR_CHANNEL_GASES:
                ar.append(gas)  # never ingested yet - fall back to the known baseline
            elif gas in KNOWN_HE_CHANNEL_GASES:
                he.append(gas)
            else:
                other.append(gas)
        self._groups = [g for g in (("Ar channel", ar), ("He channel", he), ("Other", other)) if g[1]]

        self.vars = {}
        for _, gas_list in self._groups:
            for gas in gas_list:
                self.vars[gas] = tk.BooleanVar(value=old_state.get(gas, True))

        self._update_label()
        if self._popup is not None:
            self._rebuild_popup_contents()

    def selected(self):
        return [g for g, v in self.vars.items() if v.get()]

    def select_all(self):
        for v in self.vars.values():
            v.set(True)
        self._on_change()

    def _build_popup(self, popup):
        border = ttk.Frame(popup, relief="solid", borderwidth=1, padding=6)
        border.pack()
        self._popup_frame = border
        self._rebuild_popup_contents()

    def _on_popup_closed(self):
        self._popup_frame = None

    def _all_selected(self):
        return bool(self.vars) and all(v.get() for v in self.vars.values())

    def _on_toggle_all(self):
        target = not self._all_selected()
        for v in self.vars.values():
            v.set(target)
        self._on_change()
        if self._popup is not None:
            self._rebuild_popup_contents()

    def _on_toggle_group(self, gas_list, group_var):
        # ttk.Checkbutton has already flipped group_var by the time this command
        # fires, so its current value *is* the target state to apply to the group.
        target = group_var.get()
        for gas in gas_list:
            self.vars[gas].set(target)
        self._on_change()
        if self._popup is not None:
            self._rebuild_popup_contents()

    def _rebuild_popup_contents(self):
        ttk.Style().configure("Bold.TCheckbutton", font=("", 9, "bold"))

        for child in self._popup_frame.winfo_children():
            child.destroy()

        toggle_row = ttk.Frame(self._popup_frame)
        toggle_row.pack(fill="x", pady=(0, 6))
        ttk.Button(
            toggle_row, text="None" if self._all_selected() else "All", width=6,
            command=self._on_toggle_all,
        ).pack(side="left")

        for heading, gas_list in self._groups:
            group_var = tk.BooleanVar(value=all(self.vars[g].get() for g in gas_list))
            ttk.Checkbutton(
                self._popup_frame, text=heading, variable=group_var,
                command=lambda gl=gas_list, v=group_var: self._on_toggle_group(gl, v),
                style="Bold.TCheckbutton",
            ).pack(anchor="w", pady=(4, 0))
            for gas in gas_list:
                ttk.Checkbutton(
                    self._popup_frame, text=gas, variable=self.vars[gas], command=self._on_change
                ).pack(anchor="w", padx=(18, 0))

    def _on_change(self):
        self._update_label()
        self.on_change()

    def _update_label(self):
        selected = [g for g, v in self.vars.items() if v.get()]
        if not self.vars or len(selected) == len(self.vars):
            self.config(text=f"{self.label}: All ▾")
        elif not selected:
            self.config(text=f"{self.label}: none ▾")
        else:
            self.config(text=f"{self.label}: {len(selected)} selected ▾")


class ActionsPopup(_PopupButton):
    """A button that opens a popup holding a handful of related toggles/buttons
    (not a checkbox-per-option list like DropdownChecklist) - used to group several
    small, related controls (e.g. duplicate/excluded toggles plus the delete
    actions) behind one button instead of spreading them across the toolbar."""

    def __init__(self, parent, label, build_content):
        self.label = label
        self.build_content = build_content
        super().__init__(parent, text=label)

    def _build_popup(self, popup):
        border = ttk.Frame(popup, relief="solid", borderwidth=1, padding=8)
        border.pack()
        self.build_content(border)
