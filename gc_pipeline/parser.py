"""Parser for raw GC instrument-export CSVs.

Each file has a fixed-ish metadata header (some lines carry two label/value
pairs), followed by a peak table of variable length terminated by a "Sum" row.
"""

import csv

# Header labels we care about -> field name. Anything else in the header is
# still preserved verbatim in raw_header_text but not promoted to a column.
LABEL_TO_FIELD = {
    "Sample name:": "sample_name",
    "Sample amount:": "sample_amount",
    "Sample type:": "sample_type",
    "Instrument:": "instrument",
    "Injection date:": "injection_date",
    "Acq. method:": "acq_method",
    "Analysis method:": "analysis_method",
    "Signal:": "signal",
    "Last changed:": "last_changed",
}

NUMERIC_FIELDS = {"sample_amount"}


def _clean_cell(cell):
    cell = cell.strip()
    return cell if cell else None


def _parse_number(text):
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_file(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        raw_text = f.read()

    rows = list(csv.reader(raw_text.splitlines()))

    fields = {}
    header_line_count = 0

    for row in rows:
        # The peak-table header row starts the table; stop scanning header lines.
        if row and row[0].strip() == "Name":
            break
        header_line_count += 1

        # Walk cells two at a time: label, value, label, value, ...
        i = 0
        while i < len(row) - 1:
            label = row[i].strip()
            if label in LABEL_TO_FIELD:
                field = LABEL_TO_FIELD[label]
                value = _clean_cell(row[i + 1])
                if field in NUMERIC_FIELDS:
                    value = _parse_number(value)
                fields[field] = value
            i += 2

    raw_header_text = "\n".join(",".join(c for c in r) for r in rows[:header_line_count])

    peaks = []
    for row in rows[header_line_count + 1:]:
        if not row or not any(c.strip() for c in row):
            continue
        name = row[0].strip()
        # Sum row: empty gas name, "Sum" appears in the Area column.
        if not name:
            if any(c.strip() == "Sum" for c in row):
                break
            continue
        rt = _parse_number(row[1]) if len(row) > 1 else None
        rf = _parse_number(row[2]) if len(row) > 2 else None
        area = _parse_number(row[3]) if len(row) > 3 else None
        amount = _parse_number(row[4]) if len(row) > 4 else None
        concentration = _parse_number(row[5]) if len(row) > 5 else None
        peaks.append({
            "gas": name,
            "rt": rt,
            "rf": rf,
            "area": area,
            "amount": amount,
            "concentration": concentration,
        })

    fields["raw_header_text"] = raw_header_text

    # run_number is NOT parsed here - the trailing digit in a sample name isn't a
    # reliable run order (e.g. "Air_01"/"Air_02" is a bottle-instance number, not an
    # injection sequence for some batches). It's instead computed from injection_date
    # by db._recompute_run_numbers(), which is the actual source of truth for order.

    return {"fields": fields, "peaks": peaks}
