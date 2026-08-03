import sys
from pathlib import Path

from gc_pipeline.gui import App

# Always resolve the database next to the running app (the frozen .exe's own
# folder, or this script's folder in dev) rather than whatever the current
# working directory happens to be - a desktop/Start Menu shortcut's "Start in"
# folder shouldn't determine where the database ends up.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

if __name__ == "__main__":
    App(db_path=str(APP_DIR / "gc_data.sqlite3")).mainloop()
