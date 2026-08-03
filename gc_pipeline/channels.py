"""Shared instrument-channel classification (Ar vs He) - used both by the Data
Viewer's channel-header grouping/collapsible gas columns and by the main browser's
gas filter, so the parsing rule lives in exactly one place."""

import re

CHANNEL_TOKEN_RE = re.compile(r"[^A-Za-z0-9]+")

# The gases the Ar channel is known to report, in the order the scientist expects
# them (roughly elution order) - used to order/label gases even before any data
# exists to classify them from (e.g. a freshly-created database).
AR_CHANNEL_GASES = ("He", "H2", "O2", "N2", "CH4")

# The He channel can additionally report these - unlike AR_CHANNEL_GASES this list
# is deliberately not exhaustive (there are "potentially many" hydrocarbons it can
# report that don't have a fixed name ahead of time), but these known ones should
# always be offered as filter options even before any He-channel run has actually
# been ingested, rather than only appearing once real data exists to classify them.
KNOWN_HE_CHANNEL_GASES = ("CO2", "H2S", "CO", "O2+Ar")


def channel_of(acq_method):
    """Which instrument channel a run's acq_method string indicates - "Ar", "He", or
    (if neither token is present) the raw acq_method string itself as a fallback
    identity, so grouping/classification still degrades sensibly."""
    if not acq_method:
        return acq_method or ""
    tokens = {t.lower() for t in CHANNEL_TOKEN_RE.split(acq_method) if t}
    if "ar" in tokens:
        return "Ar"
    if "he" in tokens:
        return "He"
    return acq_method
