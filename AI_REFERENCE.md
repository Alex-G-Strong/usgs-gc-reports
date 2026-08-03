# AI Reference — USGS GC Reports

Technical reference for an AI assistant picking up this codebase cold. Detailed,
not verbose — assumes you can read code; this fills in the *why* and the *how it
all connects* that isn't obvious from any single file.

## What this is

An offline Windows desktop app (Python 3.11, Tkinter/ttk, SQLite, matplotlib,
numpy, openpyxl, Pillow) for a USGS geochronology/noble-gas lab. It ingests raw
CSV exports from a gas chromatograph (GC), lets a scientist review and annotate
them, builds standards-calibration curves (peak area → known gas %), and applies
those curves to sample runs to get a bottle's actual gas composition.

**Core invariant: raw CSVs are the permanent source of truth. The SQLite database
(`gc_data.sqlite3`) is a rebuildable index.** Nothing in the app ever mutates a raw
CSV. Deleting a run from the DB (`db.delete_runs`) never touches the file on disk —
it only removes the row and records the content hash in `ignored_hashes` so a
re-ingest doesn't silently recreate it. Restoring is `db.unignore_hashes` +
re-ingest, not an undo of a DB write.

## Entry point / process model

`main.py` → `gc_pipeline.gui.App(db_path="gc_data.sqlite3").mainloop()`. Single
process, single SQLite connection (`App.conn`) held for the process lifetime and
passed down to every child window/panel — they all read/write through it directly
rather than owning separate connections. The one exception is `ingest.py::ingest()`,
which opens and cleanly closes its **own** connection every call (so the 20-second
folder-watch poll never touches `App.conn`, and child windows holding a reference
to it are never invalidated mid-poll).

`db.connect(db_path)` runs `SCHEMA` (idempotent `CREATE TABLE IF NOT EXISTS`) then
`_migrate(conn)`, which does incremental `ALTER TABLE ... ADD COLUMN` for every
column introduced after the schema's initial version, keyed on `PRAGMA table_info`
checks — safe to run against an already-populated production database on every
launch. **Never drops or recreates a table with real data in it.**

## File layout

```
main.py                          entry point
gc_pipeline/
  db.py            (~1850 ln)    schema, ALL SQL, no Tkinter
  parser.py                      raw CSV -> {fields, peaks}
  ingest.py                      folder scan -> db.insert_run calls, detection only
  channels.py                    acq_method -> "Ar"/"He"/raw-fallback classification
  calibration.py                 pure math: candidates, fit_linear, compute_percent
  export.py                      xlsx (openpyxl) + generic TSV/CSV table writers
  widgets.py                     shared Tk widgets (DropdownChecklist, tooltips, popups)
  gui.py           (~2000 ln)    App(tk.Tk) - top-level window, Selector tab, toolbar
  load_data_panel.py             "Load Data" tab: Folder / Duplicates / Pressure entry
  duplicates_wizard.py           same-identity-group resolution UI
  pressure_wizard.py (~1330 ln)  pressure/standard/notes entry, embeddable + standalone
  standards.py                   standard composition editor (% per gas)
  run_rounds_dialog.py           assign runs to a multi-day "round"
  data_viewer.py                 pivot-table view of the current selection
  models_panel.py (~1600 ln)     Models tab: per-gas calibration regression + chart
  analysis_panel.py (~1550 ln)   Analysis tab: apply curves to samples, composition chart
  deleted_files.py               view/restore permanently-deleted runs
  highlight_swatches_dialog.py   manage the row/cell highlight color palette
  help_panel.py / help_renderer.py   in-app HTML-subset doc viewer (Help tab)
  help_docs/*.html               the actual help content (human + AI variants)
  timer_popup.py                 standalone stacked countdown timer (Selector toolbar)
```

Every "window" class (`PressureEntryWindow`, `StandardsWindow`, `DeletedFilesWindow`,
`RoundAssignDialog`, `HighlightSwatchesWindow`) is a `tk.Toplevel`, non-modal (no
`grab_set`), constructed with `(app, ...)` and reading `app.conn` directly. Several
have a "panel" variant that's a plain `ttk.Frame` embeddable elsewhere
(`PressureEntryPanel` is embedded both in a standalone `PressureEntryWindow` *and*
directly in the Load Data tab's 3rd sub-tab — see the `auto_close_on_save` gotcha
below).

## Database schema (`db.py::SCHEMA` + `_migrate`)

```
runs (run_id PK, source_file, content_hash, sample_name, sample_type, sample_amount,
      instrument, injection_date, acq_method, analysis_method, signal, last_changed,
      raw_header_text, duplicate_of -> runs, excluded, ingested_at, run_number,
      notes, is_synthetic_group, round_id -> run_rounds, highlight_swatch_id -> highlight_swatches,
      load_finalized,
      UNIQUE(source_file, content_hash))
peaks (peak_id PK, run_id -> runs, gas, rt, rf, area, amount, concentration)
standards (standard_id PK, name UNIQUE, description, created_at)
standard_compositions (standard_id, gas, value [percent], units, PK(standard_id, gas))
standard_references (run_id PK -> runs, pressure, entered_at, standard_id -> standards,
                      pressure_not_recorded)
ignored_hashes (content_hash PK, ignored_at, sample_name, source_file, run_number)
calibration_series (series_id PK, gas, name, slope, intercept, r_squared,
                     force_through_origin, created_at, updated_at)
calibration_series_points (series_id, run_id, gas, excluded, override_area,
                            override_amount, PK(series_id, run_id))
app_settings (key PK, value)                          -- e.g. watched folder path
sample_group_members (group_run_id -> runs, member_run_id -> runs)  -- averaged groups
analysis_series_selection (run_id, gas, series_id -> calibration_series, PK(run_id, gas))
run_rounds (round_id PK, name, numbering_mode ['time'|'sample_id'], name_is_auto, created_at)
highlight_swatches (swatch_id PK, color, label, position)
cell_highlights (run_id, gas, swatch_id -> highlight_swatches, PK(run_id, gas))
```

Key relational facts:
- A run's peak data is `peaks` rows keyed by `(run_id, gas)` — `gas` is a **raw
  string from the instrument export**, not a normalized enum (see Gas naming below).
- `standard_references.pressure` is nullable — a run can be standard-linked before
  a pressure exists (the pressure wizard lets both be entered independently, in
  either order).
- `runs.load_finalized` (default 1 for pre-existing rows, 0 for freshly-ingested
  ones) gates whether a run participates in real `run_number` assignment. See
  "Load Data flow" below — this is the mechanism that keeps a mid-review batch from
  perturbing already-reviewed numbering.
- `runs.is_synthetic_group = 1` marks a row that isn't backed by a real raw CSV at
  all — an averaged group created by dragging duplicate sample runs together in the
  Analysis tab (`db.create_sample_group`). Its `peaks`/`standard_references` rows
  are computed means of its `sample_group_members`, recomputed on any membership
  change (`db.recompute_sample_group`).

## Data flow, end to end

### 1. Ingest (`ingest.py::ingest(folder_path, db_path)`)

Recursively scans `folder_path` for `*.csv`, hashes each file (sha256), and for
each one not already known by `(source_file, content_hash)`:
- Skip if `content_hash` is in `ignored_hashes` (a human deleted this exact content
  on purpose — don't resurrect it).
- `parser.parse_file(path)` → `{fields, peaks}`.
- `db.insert_run(...)`, flagging `duplicate_of` if the content hash matches an
  *existing* run (byte-identical duplicate).

Also detects (never merges) **same-identity groups**: runs sharing
`(sample_name, injection_date)` but *not necessarily* the same bytes — a strict
superset of the hash-duplicate check, catching a reprocessed export whose peak
values changed after the scientist re-picked peaks in the Agilent software. This
is reported in the summary (`needs_repair`, `same_identity_groups`) for the caller
to route to `db.merge_same_identity_group` (via the Duplicates wizard) — `ingest()`
itself never merges anything.

Also flags `.sirslt` run folders with acquisition files but **no exported CSV** at
all (`missing_export_count`) — an Agilent-side export step that wasn't run, not a
parse error. Surfaced via the Folder tab's red banner + Help doc, not silently
dropped.

`gui.py` polls this every 20s (`POLL_INTERVAL_MS`) against a folder path persisted
in `app_settings`. Two distinct post-ingest behaviors depending on mode
(`App._run_ingest_pass`):
- **Live watch + round-assign active** (`_watch_and_assign_round_id` set — the
  scientist is mid-run, watching in real time): a same-identity group is
  auto-merged immediately via `default_field_selections` (this is the *expected*
  case here — the GC software's first-guess peaks, corrected minutes later by a
  re-export), finalized (`db.finalize_runs`), numbered immediately, and the
  pressure-entry popup auto-opens scoped to just the new run(s).
- **Everything else** (plain watch, "Ingest now", batch backfill): new runs land
  with `load_finalized = 0` and **no** `run_number` yet. Same-identity groups are
  left untouched for the Duplicates wizard to resolve by hand. Nothing is
  auto-numbered — see Load Data flow.

### 2. Load Data tab (`load_data_panel.py`)

Three sub-tabs, meant to be walked in order for a fresh batch:
1. **Folder** — point at / watch a folder; shows recent-activity history and the
   missing-export/parse-error banners.
2. **Duplicates** (`duplicates_wizard.py`) — every `find_same_identity_groups()`
   entry, rendered as one flat table with divider rows between groups. Each column
   (pressure, standard, round, run_number, notes, highlight, and one per gas —
   `rt`/`rf`/`area`/`amount`/`concentration` move together as a unit) is
   pre-selected via `db.default_field_selections` (newest run wins for peak
   values, earliest-entered wins for human-entered fields) and overridable by
   clicking a different cell. "Merge selection" → `db.merge_same_identity_group`.
3. **Pressure entry** — an embedded `PressureEntryPanel` (`show_save_button=False`
   — its own Save button is disabled; the tab's "Insert runs into database" button
   drives both) scoped to `db.list_pending_load_runs()` minus anything still stuck
   in an unresolved duplicate group. "Insert runs into database" confirms, then
   calls `db.finalize_runs(run_ids)` — sets `load_finalized = 1`, recomputes
   `run_number` (round-aware) for exactly this batch.

**Why this exists**: `run_number` used to be assigned at ingest time, which meant
a batch still under review could get interleaved with (and renumbered by) an
unrelated backfill happening concurrently. Gating on `load_finalized` makes
numbering a deliberate, explicit step (`db.finalize_runs`) instead of an ingest
side effect.

### 3. Selector tab (`gui.py::App`, the main window)

A `ttk.Treeview` grouped by round (if `round_id` set) or by calendar date
(fallback), with a persistent multi-select "scratchpad" (`App.selected_run_ids:
set[int]`) — click/shift-click/ctrl-shift-click/drag, with its own 50-entry undo
stack (`_push_selection_undo`/`on_undo_selection`, Ctrl+Z). This selection is the
shared substrate every other tab/panel reads from — "Load selected into Models",
the Data Viewer, "Enter pressures" all operate on `App.selected_run_ids`.
`App.set_selected_run_ids(run_ids)` is the one place that mutates it and fires
`Data Viewer.refresh()` + `update_load_pending_indicator()` as side effects.

Right-click menu: toggle excluded/duplicate flags, edit in pressure entry
(scoped-selection-or-fallback-to-clicked-row pattern, reused everywhere a
right-click menu needs to act on "the checked group, or just this one"), open file
location, per-run highlight color.

### 4. `run_number` semantics (`db.py`)

Two independent numbering regimes, chosen per-run by whether `round_id` is set:
- **No round** (`recompute_run_numbers`): 1-based rank by `injection_date` *within
  its own calendar date*. Only touches `round_id IS NULL AND load_finalized = 1`
  rows. Called after ingest, after `delete_runs`, after `finalize_runs`.
- **Round-assigned** (`recompute_round_numbers`): 1-based rank across the round's
  *entire* multi-day span, by either `injection_date` (`numbering_mode='time'`) or
  the sample name's trailing `_(\d+)$` digit (`'sample_id'`) — a round exists
  specifically because a calibration cycle can span a midnight/multi-day gap,
  which per-date numbering would silently mis-rank.
- **Manual override** (`db.set_run_number_manual`): a pure numeric-threshold
  cascade — `delta = new - old`; every other run in the round with
  `run_number > old` shifts by `delta`. Can leave gaps (not a reflow); overwritten
  wholesale the next time the round's membership or mode changes (documented
  trade-off, not a bug).

### 5. Pressure/standard entry (`pressure_wizard.py`)

`PressureEntryPanel` — a `ttk.Treeview` with **write-through autosave**: every
field (pressure, standard link, notes) commits to the DB immediately on a
successful parse/selection, not batched until Save. `self.pending: dict[run_id,
str]` only ever holds a value that failed to parse yet (so it isn't lost, and
`on_save()`'s final validation pass catches it). Typing `NR` sets
`pressure_not_recorded`; `NULL` explicitly clears both the value and that flag.

Standard-linking uses a custom floating prefix-match Entry+Listbox (not a native
`ttk.Combobox` — an earlier version's use of `ttk::combobox::Post` caused
intermittent freezes). Picking/creating a standard writes via `db.set_run_standard`
immediately; a brand-new standard opens an inline composition mini-editor before
advancing.

Ctrl+V (`on_paste`) splits clipboard text on newlines (Excel/CSV single-column-copy
shape) and fills the pressure column downward from the selected row — this is
the paste-from-spreadsheet path, already built, not something to re-add.

`PressureEntryWindow` (the standalone `Toplevel` wrapper) defaults
`auto_close_on_save=True` — Save closes the window once the write actually commits
(never on a validation failure). **`PressureEntryPanel`'s own default stays
`False`** — it's also embedded directly in the Load Data tab's 3rd sub-tab, where
`winfo_toplevel()` resolves to the *main app window*, so blindly defaulting True
there would destroy the whole app on Save. Only flip the Window-level default,
never the Panel-level one.

Validation (`_validation_issues`/`_warn_validation_issues`): "standard linked but
no pressure" is scoped to `self._touched_run_ids` (only rows actually edited in
*this* session), and surfaces as a passive orange toolbar label on the main
`App` (`load_pending_pressure_warning_label` / `App._pressure_missing_for_standard_run_ids`)
rather than a blocking dialog — clicking it reopens exactly those run(s).

### 6. Standards (`standards.py`)

One row per gas (`_all_known_gas_names()` = union of `db.list_gases` [ever seen in
real peak data] + `db.list_composition_gas_names` [already in some standard's
composition] + session-local `_extra_gas_names` [just added via "+ Add gas..." but
not yet saved anywhere]) — lets a standard's known composition include a gas the
instrument channel in use has never actually reported (e.g. H2S on a channel that
doesn't detect it). Every field autosaves (debounced `<KeyRelease>` + immediate
`<FocusOut>`); "Save now" forces an immediate strict pass. Values are always
percent (fractions were retired — `_migrate` converts any legacy `units='fraction'`
rows in place, `×100`).

`db.SAMPLE_STANDARD_NAME = "Sample"` is a reserved pseudo-standard: linking a run
to it marks "this is a real sample, not a standard" (distinct from leaving it
unlinked = "not yet decided"). It's a normal `standards` row, always left with an
empty composition — which is what naturally excludes it from every calibration
regression (`build_calibration_candidates` only produces a point where the linked
standard has a composition entry for that gas) with zero special-casing.

### 7. Data Viewer (`data_viewer.py`)

A pivot table: one row per run in the current selection, one column per field or
`gas:<gas>:<metric>` (metric ∈ amount/rt/rf/concentration/area). Columns are
add/removable via a `DropdownChecklist` or a stationary header click (drag =
reorder, click = remove — disambiguated by a 5px movement threshold, mirrored in
several other panels as `DRAG_THRESHOLD_PX`). Has its own row-selection scratchpad
(`_table_selection`, independent of `App.selected_run_ids`) driving "Remove from
selection" and a right-click → "Edit N run(s) in pressure entry..." (same
scoped-selection-or-clicked-row pattern as the Selector).

### 8. Models tab (`models_panel.py`) — the calibration regression

**`calibration.build_calibration_candidates(conn, run_ids)`** is the single source
of truth for what counts as a calibration point: for every run that is
standard-linked, has a pressure, and whose linked standard has a composition entry
for a given gas — `amount = compute_amount(percent, pressure) = percent * pressure
/ 14.65` (`ATMOSPHERIC_PRESSURE = 14.65`, a fixed lab constant) is the y-value;
the run's raw `peaks.area` for that gas is the x-value. A run missing any of those
three conditions contributes **no point** for that gas — never a broken/BD point.

One `GasCalibrationTab` per gas, each with its own `calibration_series` (a named,
independently-fittable subset of that gas's candidate points — a point can belong
to more than one series). New series default `force_through_origin=True`
(`db.create_calibration_series`) — a calibration curve should read 0 amount at 0
signal, which is overwhelmingly the physically-correct default; the per-series
"Zero" checkbox opts out. `calibration.fit_linear(points, through_origin)` uses
`numpy.polyfit` (or a closed-form through-origin slope) — needs ≥2 points normally,
≥1 if forced through the origin. Every re-render recomputes and
`db.save_calibration_fit`s if the fit actually changed — there's no separate "Save"
action, matching the write-through-autosave convention used throughout.

Double-clicking a sample — either its row in the points table, or its plotted
point on the chart (`_find_point_near`, nearest-by-pixel-distance across all
tracked scatter artists, since a y-intercept marker at x=0 can sit close enough to
a real point to both register as a hit) — opens that single run in the pressure
entry wizard (`ModelsPanel.edit_run`), then reloads Models on close, since a
pressure correction changes `amount` and needs to ripple back into the fit.

### 9. Analysis tab (`analysis_panel.py`) — applying curves to samples

Auto-populated (no separate load step) whenever "Load selected into Models" is
clicked: whatever's `Sample`-linked with a pressure among the loaded selection
shows up here too (`AnalysisPanel.load_runs`, called right after
`ModelsPanel.load_runs`).

**`calibration.compute_percent(area, pressure, slope, intercept) = 14.65 *
(slope*area + intercept) / pressure`** — the *inverse* of the fit: plug the
sample's own raw area into the gas's *selected* curve's equation exactly as fit,
then normalize by the sample's own pressure. (Not "normalize area first" — that
would be silently wrong whenever a curve has a nonzero intercept.) Each
`(run_id, gas)` cell picks its own curve independently
(`analysis_series_selection` table); out-of-range flagging compares the sample's
raw area against `calibration.get_series_area_bounds(series_id)` (min/max area
among that series' own non-excluded fitted points).

**Duplicate averaging**: dragging one sample row onto another (Illustrator-layers-
style; native `ttk.Treeview` parent/child rows) creates a real, persisted
synthetic `runs` row (`is_synthetic_group=1`) via `db.create_sample_group` — raw
peak areas and pressure are averaged *before* curve-inversion (one % per gas for
the whole group), not an average of independently-computed percentages, and
membership changes trigger `db.recompute_sample_group` to keep it live.

Composition chart: a stacked bar per selected row, one segment per gas
(`_gas_color`, see Gas colors below), unmeasured remainder as an outlined blank
segment, segment text color auto-switches white/black by relative luminance so it
stays legible regardless of gas color. The zoomed-in panel's y-axis defaults to
**1.2× the largest gas percentage currently selected**
(`_default_zoom_ceiling`) — recomputed only on a genuinely new row selection
(`_on_selection_changed`), never on an unrelated re-render (a curve refit
elsewhere, the drag-to-group gesture itself), so a manual zoom drag isn't yanked
out from under the user.

## Gas naming — the recurring gotcha

`peaks.gas` is a **raw string straight from the instrument export**, not a
normalized identifier. The same physical gas can appear under multiple distinct
strings depending on which detector/channel produced it — e.g. `"CH4"` (Ar
channel), `"CH4 - HC_channel"` and `"CH4 - Meth_channel"` (two different FID paths
on the He channel's `He_FrFID_BackTCD_Aux2FID.amx` acquisition method). Confirmed
via direct query: **no single run ever reports more than one CH4 variant
simultaneously** — which string appears is determined entirely by `acq_method`,
not redundant measurement.

Anywhere code needs to treat "CH4" and "CH4 - Meth_channel" as the same underlying
gas (currently: `analysis_panel.py`'s color lookup), extract the leading
alphanumeric token (`_base_gas_name`, `re.match(r"[A-Za-z0-9]+", gas)`) rather than
comparing the raw string. Most of the app deliberately does **not** collapse these
variants — `calibration_series` and `standard_compositions` are keyed on the exact
raw string, since each variant is fit as its own independent calibration curve.

`channels.py::channel_of(acq_method)` classifies a run's *acquisition method*
(not a gas name) into `"Ar"` / `"He"` / raw-fallback by tokenizing on non-
alphanumeric separators — used for the Data Viewer's collapsible channel-grouped
columns and the gas filter's channel-aware relevance check.

## UI conventions worth knowing before touching any panel

- **Treeview has no native per-cell styling.** Every custom cell color/highlight
  (row-highlight swatches, duplicate-wizard's picked-cell overlay, out-of-range
  flags) is a `tk.Frame`/`tk.Label` placed via `.place()` over `tree.bbox(iid,
  col)`, repositioned on `<Configure>`/scroll.
- **Header click vs. drag** (Data Viewer, Analysis results table): a stationary
  click removes/toggles a column; real mouse movement before release reorders it
  instead. Disambiguated by a small pixel-movement threshold, not by timing.
- **Region disambiguation**: `tree.identify_region(x, y)` returning `"separator"`
  must be checked *first* in any custom press/drag handler on a Treeview — several
  real bugs (unintended row selection while resizing a column border) came from
  skipping this check.
- **Write-through autosave, not batched Save**: pressure/standard/notes fields,
  Standards' composition values, calibration fit recomputation — all commit
  immediately on a valid edit. `pending`/undo-stack patterns exist only to handle
  a not-yet-valid in-progress edit, not as a staging area requiring an explicit
  Save.
- **Selection scratchpads are per-panel**, not shared, except `App.selected_run_ids`
  itself (the Selector's own) which everything else reads from. The Data Viewer,
  each `GasCalibrationTab`'s points table, and the Duplicates wizard each maintain
  their own independent click/shift-click/ctrl-shift-click/drag selection set with
  the same interaction pattern copy-pasted (not shared as a mixin/helper) —
  intentional low-risk duplication rather than a shared abstraction refactored
  under working code.
- **Right-click "act on selection, or just this row" pattern**: repeated verbatim
  across the Selector, Data Viewer, and Models points table —
  `run_ids = list(scratchpad) if scratchpad else [clicked_run_id]`.

## Testing

No formal test suite ships in the repo. Development this far used disposable
scratch scripts (plain Python, not pytest) run against scratch copies of the DB in
a temp directory, exercising the real Tkinter widgets headlessly (`app.update()`
after each simulated event) — never against the live `gc_data.sqlite3`. If adding
tests going forward, that's the established pattern: a throwaway script per
feature, asserting against real `db.py` state and real widget state
(`tree.bbox`, `tree.set`, etc.), not mocks.
