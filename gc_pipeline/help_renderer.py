"""Minimal HTML-subset renderer: parses the handful of tags the Help docs
actually use (headings, paragraphs, bold/italic, <mark> highlighting, list/
blockquote indentation, local images) into a plain tk.Text widget. This is
deliberately not a general HTML engine - anything outside that tag set is just
skipped rather than crashing, since "Open in browser" (real HTML, real CSS) is
always available on the same panel as the full-fidelity fallback."""

import html.parser
from pathlib import Path
from tkinter import font as tkfont

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None

BLOCK_TAGS = ("h1", "h2", "h3", "p", "li", "blockquote")


class _HTMLToText(html.parser.HTMLParser):
    def __init__(self, text_widget, base_dir):
        super().__init__(convert_charrefs=True)
        self.text = text_widget
        self.base_dir = Path(base_dir)
        self._tag_stack = []
        self.images = []  # caller must keep a reference or Tk garbage-collects them

    def _current_tags(self):
        tags = []
        for t in self._tag_stack:
            if t in ("b", "strong"):
                tags.append("bold")
            elif t in ("i", "em"):
                tags.append("italic")
            elif t == "mark":
                tags.append("highlight")
            elif t in ("h1", "h2", "h3"):
                tags.append(t)
            elif t in ("li", "blockquote"):
                tags.append("indent")
        return tuple(dict.fromkeys(tags))

    def _at_line_start(self):
        return self.text.index("end-1c") == "1.0"

    def handle_starttag(self, tag, attrs):
        if tag in BLOCK_TAGS and not self._at_line_start():
            self.text.insert("end", "\n")
        if tag == "li":
            self.text.insert("end", "• ", self._current_tags() + ("indent",))
        if tag == "br":
            self.text.insert("end", "\n")
        if tag == "img":
            self._insert_image(dict(attrs).get("src"))
        self._tag_stack.append(tag)

    def handle_endtag(self, tag):
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
        if tag in BLOCK_TAGS:
            self.text.insert("end", "\n")

    def handle_data(self, data):
        if any(t in ("head", "title", "style", "script") for t in self._tag_stack):
            return
        text = " ".join(data.split())
        if not text:
            return
        self.text.insert("end", text + " ", self._current_tags())

    def _insert_image(self, src):
        if not src or Image is None:
            return
        path = self.base_dir / src
        if not path.exists():
            return
        try:
            img = Image.open(path)
            img.thumbnail((760, 760))
            photo = ImageTk.PhotoImage(img)
        except Exception:
            return
        self.images.append(photo)
        self.text.image_create("end", image=photo)


def configure_tags(text_widget):
    family = tkfont.nametofont("TkDefaultFont").actual("family")
    text_widget.tag_configure("h1", font=(family, 16, "bold"), spacing3=8)
    text_widget.tag_configure("h2", font=(family, 13, "bold"), spacing3=6)
    text_widget.tag_configure("h3", font=(family, 11, "bold"), spacing3=4)
    text_widget.tag_configure("bold", font=(family, 10, "bold"))
    text_widget.tag_configure("italic", font=(family, 10, "italic"))
    text_widget.tag_configure("highlight", background="#fff3a0")
    text_widget.tag_configure("indent", lmargin1=24, lmargin2=24)


def render_html_file(text_widget, html_path):
    """Clears text_widget and renders html_path into it. Returns the list of
    PhotoImage objects the caller must hold a reference to (Tk drops images with
    no live Python reference, even ones already inserted into a Text widget)."""
    html_path = Path(html_path)
    text_widget.config(state="normal")
    text_widget.delete("1.0", "end")
    configure_tags(text_widget)
    parser = _HTMLToText(text_widget, html_path.parent)
    parser.feed(html_path.read_text(encoding="utf-8"))
    text_widget.config(state="disabled")
    return parser.images
