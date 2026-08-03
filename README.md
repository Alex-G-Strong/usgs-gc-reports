# USGS GC Reports

A desktop tool for managing gas chromatograph (GC) results in a noble-gas /
geochronology lab: import raw instrument exports, track calibration standards,
build calibration curves, and turn a sample's raw signal into an actual gas
composition — all offline, with your raw data files staying exactly where they
are.

> Looking for the deep technical/architecture writeup instead? See
> [AI_REFERENCE.md](AI_REFERENCE.md).

## Contents

- [What problem this solves](#what-problem-this-solves)
- [Getting started](#getting-started)
- [The big picture: how data flows through the app](#the-big-picture-how-data-flows-through-the-app)
- [Tab by tab](#tab-by-tab)
  - [Selector](#selector)
  - [Load Data](#load-data)
  - [Standards](#standards)
  - [Models](#models)
  - [Analysis](#analysis)
  - [Help](#help)
- [The math](#the-math)
  - [Turning a standard into a calibration point](#turning-a-standard-into-a-calibration-point)
  - [Fitting the curve](#fitting-the-curve)
  - [Turning a sample's signal into a percentage](#turning-a-samples-signal-into-a-percentage)
  - [Averaging duplicate sample runs](#averaging-duplicate-sample-runs)
- [Runs, rounds, and numbering](#runs-rounds-and-numbering)
- [Duplicates and reprocessed files](#duplicates-and-reprocessed-files)
- [Data safety](#data-safety)
- [Requirements](#requirements)

## What problem this solves

The GC produces one CSV per injection. Over a real field season that's hundreds
of files, some of them standards (known gas mixtures at a known pressure), most
of them real samples. To turn a sample's raw peak area into "this bottle is 20.9%
O2," you need to:

1. Know what standard(s) were run and what they actually contain.
2. Know the pressure each standard was loaded at (this is never in the CSV — it's
   a physical number from the lab notebook).
3. Build a calibration curve (peak area vs. known amount) for each gas from those
   standard runs.
4. Apply that curve to a sample's raw peak area, correcting for that sample's own
   pressure.

This app does all four steps, plus the bookkeeping that makes it actually usable
day to day: detecting duplicate/reprocessed files, keeping run numbering sane
across multi-day calibration cycles, flagging when a step is missing, and letting
you export everything back out to Excel/CSV.

## Getting started

```
python main.py
```

Requires Python 3.11+ with `tkinter` (ships with standard Python on Windows),
`matplotlib`, `numpy`, `openpyxl`, and `Pillow`. No internet connection needed —
everything runs locally against a SQLite file (`gc_data.sqlite3`, created
automatically next to `main.py` on first launch).

Point the app at the folder your GC software exports into (Load Data tab →
"Watch folder...") and it checks every 20 seconds for new files, or click
"Ingest now" for a one-off scan.

## The big picture: how data flows through the app

```
Raw CSVs on disk  →  Ingest  →  Load Data tab (review)  →  Selector (browse/select)
                                                                    ↓
                                          Standards ← composition →  ↓
                                                                    ↓
                                                          Models tab (fit curves)
                                                                    ↓
                                                          Analysis tab (apply curves)
                                                                    ↓
                                                        % composition per sample
```

Your raw CSV files are never modified or deleted by this app — the database is
just an index built from them. If you ever need to start over, you can delete
`gc_data.sqlite3` and re-point the app at your export folder; everything rebuilds
from the CSVs.

## Tab by tab

### Selector

The main browser: every run, grouped by calibration round (if assigned) or by
calendar date. Click, shift-click, ctrl-shift-click, or drag to build up a
selection — this selection is what every other tab acts on ("Load selected into
Models," "Enter pressures," the Data Viewer). Right-click a run for quick actions:
edit its pressure, open its file's folder, mark it excluded, set a highlight
color.

A **Data Viewer** pivot table lives alongside the browser — one row per selected
run, one column per field or per-gas measurement, fully reorderable, exportable
to Excel-pasteable text or CSV.

### Load Data

The guided path for getting new files into the database, in three steps:

1. **Folder** — point at (or continuously watch) the folder your GC exports into.
2. **Duplicates** — if any newly-arrived file shares a sample name + injection
   date with something already on file (a byte-identical re-export, or a
   genuinely reprocessed file with corrected peak values), it lands here for you
   to resolve field-by-field before it can proceed. Smart defaults are
   pre-selected (the newest file's peak values, the earliest-entered pressure/
   standard/notes), so most groups just need a glance and a click.
3. **Pressure entry** — enter the pressure (and which standard, if any) each new
   run was loaded at. "Insert runs into database" is the final step — it's what
   actually assigns real, permanent run numbers, so nothing you're still
   reviewing can accidentally shift the numbering of runs you already finished.

### Standards

Define what's actually in each of your reference gas standards: pick a standard,
enter its known percent composition for each gas. You can add a gas the
instrument has never detected on the current channel (e.g. H2S) if you know it's
in the mixture — useful when the detector setup doesn't happen to see everything
a certified standard actually contains. Every field autosaves as you type.

### Models

Where calibration curves get built. Select a batch of runs in the Selector,
click "Load selected into Models," and every standard-linked run with a known
pressure becomes a plotted point (peak area vs. calculated amount) on its gas's
own tab. You can:

- Split points into multiple named series (e.g. if you have two different
  concentration ranges you want fit separately) by selecting and using "Move"/
  "Add."
- Exclude individual points from a fit without deleting any data.
- Double-click a plotted point (or its row in the table below) to jump straight
  into fixing its pressure if something looks off.
- Toggle "force through origin" per series — on by default, since a calibration
  curve should physically read zero signal at zero amount.

An Overview tab shows every gas's fitted line at a glance, with copyable
equations.

### Analysis

Where curves actually get used. Any run linked to the reserved **"Sample"**
standard (meaning: "this is a real sample, not a reference mixture") shows up
here automatically once it's loaded into Models, alongside its pressure. For each
gas, pick which calibration curve applies, and the app computes a percent
composition from the run's raw peak area. Cells outside a curve's calibrated
range are flagged.

Duplicate sample runs (the same bottle injected more than once) can be dragged
together into an averaged group — this creates a real synthetic entry, averaging
the raw measurements *before* the math runs, not after.

The composition breakdown chart shows a stacked bar for whatever's selected, with
a second, zoomed-in panel that starts sized to actually show your trace gases
(see [the math](#the-math) below) instead of squashing everything against the
bottom of a fixed 100% scale.

### Help

In-app documentation for specific known issues (e.g. what to do about a missing
CSV export from the instrument software), each with both a plain-language guide
and a denser technical reference.

## The math

### Turning a standard into a calibration point

A standard's known composition (e.g. "20.9% O2") plus its loaded pressure gives a
calculated **amount**:

```
amount = percent × pressure / 14.65
```

14.65 is a fixed lab convention (roughly standard atmospheric pressure in psi) —
it normalizes different loading pressures onto a common scale so points from
different injections are comparable. This `amount` is the y-axis value; the
run's raw, as-measured peak area is the x-axis value. Every standard run that has
a known pressure and a linked standard with a defined composition for a gas
contributes one such point.

### Fitting the curve

For each gas, a straight line is fit through its points: `amount = slope × area +
intercept`. By default the fit is forced through the origin (zero area should
mean zero amount) — this can be turned off per curve if you have a real reason
to expect a nonzero intercept. At least one point is needed for an origin-forced
fit, two for a free fit.

### Turning a sample's signal into a percentage

This is the *inverse* of the fit, solved for percent using the sample's own raw
peak area and pressure:

```
percent = 14.65 × (slope × area + intercept) / pressure
```

Critically, the sample's raw area is plugged straight into the equation the way
it was fit — the pressure only gets applied at the very last step. This matters
whenever a curve has a nonzero intercept: normalizing the area by pressure
*before* applying the curve would give a different (wrong) answer.

### Averaging duplicate sample runs

If the same physical bottle was injected more than once, dragging those runs
together averages their **raw peak areas and pressures first**, then runs that
single averaged pair through the percent formula above — producing one real
percentage per gas for the group, rather than averaging several independently-
computed percentages (which is not generally the same number, especially with a
nonzero intercept).

## Runs, rounds, and numbering

Each run gets a `run_number` — normally its 1-based rank by injection time within
its own calendar date. Real calibration cycles often span more than one day
(calibrate, run samples, shut down, resume next week), so runs can instead be
grouped into a **round**, numbered either by time across the round's whole span
or by the trailing digit in the sample name — whichever matches how your lab
notebook actually tracks it. A round's numbering can be hand-corrected for one
run, and everything after it shifts to match automatically.

Numbers are only assigned once a run has been reviewed and explicitly "inserted
into the database" from the Load Data tab — a run still under review never has a
number yet, so it can't interleave with (and shift) runs you already finished
reviewing.

## Duplicates and reprocessed files

Two distinct situations, both handled without ever touching your raw files:

- **Byte-identical re-export** (e.g. the GC software re-saved the same file):
  flagged automatically, safe to discard the extra copy — the original raw CSVs
  are just sitting in `ignored_hashes` protection so re-scanning the folder never
  silently recreates a run you already dealt with.
- **Reprocessed file with corrected peaks** (same sample/date, genuinely
  different values — you looked at the chromatogram and re-picked a peak): these
  go to the Duplicates tab for a field-by-field review, since a computer can't
  safely guess which version is right. If you're actively watching a folder
  during a live run session, a reprocessed file is merged in automatically
  (matching the routine, expected pattern of "the software's first guess gets
  corrected minutes later"), with a note logged so it's never silent.

Nothing is ever deleted destructively — see [Data safety](#data-safety).

## Data safety

- Raw CSV files are read-only as far as this app is concerned. They are never
  edited or deleted.
- "Deleting" a run from the database only removes its database row. The file
  stays on disk, and the deletion is remembered (so re-scanning the folder won't
  bring it back by accident) until you explicitly restore it from the Deleted
  Files window.
- The database itself is disposable — it's an index built from your CSVs, not
  the source of record. If it's ever lost or corrupted, re-pointing the app at
  your export folder rebuilds everything except manually-entered data (pressures,
  standards, curve choices), which is why the app treats that entry as worth
  protecting once made.

## Requirements

- Windows (developed/tested there; the underlying stack is cross-platform)
- Python 3.11+
- `matplotlib`, `numpy`, `openpyxl`, `Pillow`

See [AI_REFERENCE.md](AI_REFERENCE.md) for the full module map, database schema,
and implementation-level design conventions.
