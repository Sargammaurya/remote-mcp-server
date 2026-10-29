"""
Bounded value summaries: what values a text column really holds, and how often.

Live runs (2026-10-23) showed plans that "normalised country codes" but left
'USA', 'U.S.' and 'India' behind: inspection reported how MANY distinct values
a column had, never WHICH, so the model could not see the variants it had to
map. These summaries show them, within limits:

  * only text columns with at most MAX_DISTINCT distinct values (categories,
    codes, labels) — free text and identifiers are never listed;
  * at most TOP_K values per column, most frequent first, each cut to MAX_LEN
    characters, with exact counts (NULLs are counted separately, not listed);
  * a column whose NAME suggests personal data, or whose values LOOK like
    emails, phone or card numbers, is masked: its distinct count is given,
    its values are not.

The same rules serve inspect_dataset (before preparation) and export_asset
(after), so a client can check its claims about the result against them.
"""

from __future__ import annotations

import re

MAX_DISTINCT = 50
TOP_K = 20
MAX_LEN = 64

_SENSITIVE_NAME = re.compile(
    r"(?i)(e-?mail|phone|mobile|\btel\b|address|street|zip|postcode|postal|ssn|social|passport|"
    r"national.?id|aadhaar|pan\b|tax.?id|iban|account|card|cvv|password|secret|token|\bdob\b|"
    r"birth|salary|\bip\b|ip_address|first.?name|last.?name|full.?name|surname|(?<![a-z])name(?![a-z])|"
    # people: masked unless the name says it is a category of them
    r"(?:customer|client|patient|employee|person|contact)(?!_?(?:type|segment|tier|group|category|"
    r"class|status|plan|region|country|count|id)))")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", re.I)
# a run of digits with phone/card separators, anywhere in the text
_DIGIT_RUN = re.compile(r"\+?\d[\d ().-]{7,}\d")


def _looks_personal(text: str) -> bool:
    if _EMAIL.search(text):
        return True
    return any(sum(ch.isdigit() for ch in run) >= 9 for run in _DIGIT_RUN.findall(text))


def is_sensitive_name(column: str) -> bool:
    return bool(_SENSITIVE_NAME.search(str(column)))


def summarize(column: str, distinct: int, top: list[tuple[object, int]]) -> dict:
    """`top`: (value, count) most frequent first (NULLs excluded) — at most TOP_K + 1 entries."""
    if is_sensitive_name(column):
        return {"distinct": distinct, "values_hidden": "masked: the column name suggests personal data"}
    if distinct > MAX_DISTINCT:
        return {"distinct": distinct, "values_hidden": f"more than {MAX_DISTINCT} distinct values"}
    shown = []
    for value, count in top[:TOP_K]:
        text = "" if value is None else str(value)
        if _looks_personal(text):
            return {"distinct": distinct, "values_hidden": "masked: values look like personal data"}
        shown.append({"value": text if len(text) <= MAX_LEN else text[:MAX_LEN] + "…", "count": int(count)})
    out = {"distinct": distinct, "values": shown}
    if distinct > len(shown):
        out["values_not_shown"] = distinct - len(shown)
    return out


def top_values_sql(source: str, columns: list[tuple[int, str]], quote) -> str:
    """One query: the TOP_K + 1 most frequent non-NULL values of each listed column.

    `columns`: (index, name). Returns rows (c, v, n) where c is the index.

    The source is read ONCE: with one UNION ALL part per column reading `source`
    directly, a read_csv source was re-read and re-sniffed per column — 59 text
    columns of a 34 MB, 323-column CSV took 231 s (vs 4.3 s), past every timeout.
    """
    parts = [
        f"SELECT {i} AS c, CAST({quote(name)} AS VARCHAR) AS v, count(*) AS n FROM src "
        f"WHERE {quote(name)} IS NOT NULL GROUP BY 2"
        for i, name in columns
    ]
    body = " UNION ALL ".join(parts)
    needed = ", ".join(quote(name) for _, name in columns)
    return (f"WITH src AS MATERIALIZED (SELECT {needed} FROM {source}), t AS MATERIALIZED ({body}) "
            f"SELECT c, v, n FROM t "
            f"QUALIFY row_number() OVER (PARTITION BY c ORDER BY n DESC, v) <= {TOP_K + 1} ORDER BY c, n DESC, v")
