"""SYNTHETIC test data. Invented values only; no real records."""

import base64
import csv
import io

# deliberately messy: trailing spaces, mixed case, a quoted comma, missing
# values, a non-numeric amount, impossible dates, two exact duplicate rows
MESSY_ORDERS_CSV = """order_id,customer,amount,order_date,country
1,Alice ,100.50,2026-01-05,IN
2,bob,,2026-01-06,in
3,"Carol, Ltd",75,05/01/2026,US
3,"Carol, Ltd",75,05/01/2026,US
4,,abc,2026-02-30,IN
5,Dävid,200,,
5,Dävid,200,,
6,eve,  300 ,2026-03-01,US
7,Frank,1e3,2026-03-02,uk
8,grace,-5,2026-03-03,IN
9,Heidi,0,2026-13-01,IN
10,Ivan,99.99,2026-03-04,
"""

MESSY_EXPECTED = {
    "row_count": 12,
    "duplicate_rows": 2,
    "nulls": {"order_id": 0, "customer": 1, "amount": 1, "order_date": 2, "country": 3},
    "distinct_order_id": 10,
}


def b64(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


def large_csv(rows: int) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "group", "value", "note"])
    for i in range(rows):
        w.writerow([i, f"g{i % 97}", (i * 7919) % 10007 / 3, f"synthetic row {i}"])
    return out.getvalue()


def xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
