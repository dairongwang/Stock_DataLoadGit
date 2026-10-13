"""
Generate test files in different FILE FORMATS and COLUMN SEQUENCES from
samples/NASDAQ_20250123.txt (the default format).

    python samples/make_samples.py

Every file carries exactly the same 4092 rows, so converting any of them
must yield a file identical to NASDAQ_20250123.txt.

  NASDAQ_20250123_reordered.txt   text, same header style, columns re-sequenced
  NASDAQ_20250123_broker.csv      ';' delimited, other header names, ISO dates, extra column
  NASDAQ_20250123.csv             CSV, header "Symbol,Open,High,Low,Close,Volume,Date"
  NASDAQ_20250123.tsv             tab delimited text, header "ticker close vol date open high low"
  NASDAQ_20250123_noheader.txt    '|' delimited, NO header, sequence date|ticker|open|high|low|close|vol
                                  (needs a custom format -> shipped in formats.json as "pipe_noheader")
  NASDAQ_20250123.xlsx            Excel, sheet "Prices", a title row above the header,
                                  numeric cells, header "Trade Date,Ticker,Volume,Open,High,Low,Close"
  NASDAQ_20250123.json            JSON document with nested records under "data"
  NASDAQ_20250123.xml             XML with <quote symbol=".." date=".."><open>..</open>...</quote>
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
SRC = HERE / "NASDAQ_20250123.txt"

# index of each default column in the source: ticker,date,open,high,low,close,vol
T, D, O, H, L, C, V = range(7)


def num(s: str):
    """Text -> int/float exactly as a spreadsheet/JSON producer would store it."""
    return int(s) if s.lstrip("-").isdigit() else float(s)


def iso(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def main() -> None:
    with open(SRC, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    data = rows[1:]
    made = []

    # 1) different column sequence only (text)
    out = HERE / "NASDAQ_20250123_reordered.txt"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["<date>", "<ticker>", "<vol>", "<close>", "<open>", "<high>", "<low>"])
        for r in data:
            w.writerow([r[D], r[T], r[V], r[C], r[O], r[H], r[L]])
    made.append(out)

    # 2) ';' delimited, other names, ISO dates, extra column
    out = HERE / "NASDAQ_20250123_broker.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";", lineterminator="\n")
        w.writerow(["Symbol", "Trade Date", "Volume", "Close", "Open", "High", "Low", "Currency"])
        for r in data:
            w.writerow([r[T], iso(r[D]), r[V], r[C], r[O], r[H], r[L], "USD"])
    made.append(out)

    # 3) plain CSV with a different sequence + names
    out = HERE / "NASDAQ_20250123.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\r\n")          # Windows line endings, as Excel "Save as CSV" does
        w.writerow(["Symbol", "Open", "High", "Low", "Close", "Volume", "Date"])
        for r in data:
            w.writerow([r[T], r[O], r[H], r[L], r[C], r[V], r[D]])
    made.append(out)

    # 4) tab delimited text
    out = HERE / "NASDAQ_20250123.tsv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["ticker", "close", "vol", "date", "open", "high", "low"])
        for r in data:
            w.writerow([r[T], r[C], r[V], r[D], r[O], r[H], r[L]])
    made.append(out)

    # 5) pipe delimited, no header
    out = HERE / "NASDAQ_20250123_noheader.txt"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        for r in data:
            fh.write("|".join([r[D], r[T], r[O], r[H], r[L], r[C], r[V]]) + "\n")
    made.append(out)

    # 6) Excel
    try:
        import openpyxl
    except ImportError:
        print("openpyxl not installed - skipping NASDAQ_20250123.xlsx")
    else:
        out = HERE / "NASDAQ_20250123.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Prices"
        ws.append(["NASDAQ daily prices 2025-01-23"])          # title row -> skip_rows = 1
        ws.append(["Trade Date", "Ticker", "Volume", "Open", "High", "Low", "Close"])
        for r in data:
            ws.append([num(r[D]), r[T], num(r[V]), num(r[O]), num(r[H]), num(r[L]), num(r[C])])
        notes = wb.create_sheet("Notes")
        notes.append(["Source", "stooq"])
        wb.save(out)
        made.append(out)

    # 7) JSON: nested document
    out = HERE / "NASDAQ_20250123.json"
    doc = {
        "market": "NASDAQ",
        "asOf": "2025-01-23",
        "data": [
            {
                "symbol": r[T],
                "date": iso(r[D]),
                "prices": {"open": num(r[O]), "high": num(r[H]), "low": num(r[L]), "close": num(r[C])},
                "volume": num(r[V]),
            }
            for r in data
        ],
    }
    out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    made.append(out)

    # 8) XML: attributes + child elements
    out = HERE / "NASDAQ_20250123.xml"
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<quotes market="NASDAQ">\n')
        for r in data:
            fh.write(f'  <quote symbol="{escape(r[T])}" date="{r[D]}">'
                     f'<open>{r[O]}</open><high>{r[H]}</high><low>{r[L]}</low>'
                     f'<close>{r[C]}</close><volume>{r[V]}</volume></quote>\n')
        fh.write("</quotes>\n")
    made.append(out)

    for p in made:
        print(f"wrote {p.name:<32} {p.stat().st_size:>9,d} bytes")


if __name__ == "__main__":
    main()
