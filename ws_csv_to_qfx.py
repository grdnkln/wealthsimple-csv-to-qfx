#!/usr/bin/env python3
"""Convert WealthSimple chequing-account CSV statements into a single Quicken
QFX (OFX 1.02 SGML) file suitable for import into YNAB4.

Standard library only. All processing is local; nothing is sent over a network.

Usage:
    python3 ws_csv_to_qfx.py [--config account.json] IN1.csv [IN2.csv ...] OUT.qfx

The last positional argument is the output QFX path; every preceding positional
argument is an input CSV. Multiple CSVs are merged, sorted by date, and
de-duplicated into one QFX.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation

# Columns expected in the WealthSimple statement CSV.
REQUIRED_COLUMNS = ("date", "transaction", "description", "amount", "balance", "currency")

# Keys we require from account.json.
CONFIG_KEYS = ("bankid", "acctid", "accttype", "curdef", "org", "fid", "intu_bid")


class ConversionError(Exception):
    """Raised for user-facing errors (bad CSV, missing config, etc.)."""


def load_config(path):
    """Load and validate account.json."""
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        raise ConversionError(f"Config file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ConversionError(f"Config file {path} is not valid JSON: {exc}")

    missing = [k for k in CONFIG_KEYS if k not in cfg]
    if missing:
        raise ConversionError(
            f"Config file {path} is missing keys: {', '.join(missing)}"
        )
    return cfg


def sanitize_ascii(text):
    """Reduce arbitrary text to clean 7-bit ASCII for OFX 1.02 SGML output.

    OFX 1.02 SGML also treats '&', '<' and '>' specially, so escape them.
    """
    if text is None:
        text = ""
    # Common symbols that would otherwise be dropped by ASCII folding.
    replacements = {
        "®": "(R)",   # ®
        "™": "(TM)",  # ™
        "©": "(C)",   # ©
        "–": "-",     # en dash
        "—": "-",     # em dash
        "‘": "'", "’": "'",
        "“": '"', "”": '"',
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    # Fold any remaining accented characters to ASCII, drop the rest.
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    # SGML entity escaping.
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.strip()


def parse_row(row, source, lineno):
    """Validate and normalize one CSV row into a transaction dict."""
    for col in REQUIRED_COLUMNS:
        if col not in row:
            raise ConversionError(
                f"{source} line {lineno}: missing column '{col}'"
            )
    raw_date = (row["date"] or "").strip()
    try:
        date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        raise ConversionError(
            f"{source} line {lineno}: bad date '{raw_date}' (expected YYYY-MM-DD)"
        )
    try:
        amount = Decimal((row["amount"] or "").strip())
        balance = Decimal((row["balance"] or "").strip())
    except InvalidOperation:
        raise ConversionError(
            f"{source} line {lineno}: bad amount/balance "
            f"('{row['amount']}' / '{row['balance']}')"
        )
    return {
        "date": date,
        "code": (row["transaction"] or "").strip(),
        "description": (row["description"] or "").strip(),
        "amount": amount,
        "balance": balance,
        "currency": (row["currency"] or "").strip(),
    }


def read_csv_files(paths):
    """Read all CSVs, returning a flat list of transaction dicts in file order."""
    txns = []
    for path in paths:
        try:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                if reader.fieldnames is None:
                    raise ConversionError(f"{path}: file is empty")
                # DictReader.line_num counts the header, so data starts at 2.
                for row in reader:
                    txns.append(parse_row(row, path, reader.line_num))
        except FileNotFoundError:
            raise ConversionError(f"CSV file not found: {path}")
        except UnicodeDecodeError as exc:
            raise ConversionError(f"{path}: could not decode as UTF-8: {exc}")
    return txns


def dedupe(txns):
    """Drop exact-duplicate rows (overlapping statements), preserving order.

    A transaction is keyed by (date, amount, balance, description). The running
    balance makes genuine transactions effectively unique, so equal keys are
    true duplicates.
    """
    seen = set()
    result = []
    for t in txns:
        key = (t["date"], t["amount"], t["balance"], t["description"])
        if key in seen:
            continue
        seen.add(key)
        result.append(t)
    return result


def fitid(t):
    """Deterministic, stable, unique transaction id."""
    basis = "|".join([
        t["date"].strftime("%Y%m%d"),
        format(t["amount"], "f"),
        format(t["balance"], "f"),
        t["description"],
    ])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]


def money(value):
    """Format a Decimal as a signed 2-decimal string."""
    return str(value.quantize(Decimal("0.01")))


def build_qfx(txns, cfg):
    """Build the full OFX 1.02 SGML document as a string."""
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    first = txns[0]
    last = txns[-1]
    dtstart = first["date"].strftime("%Y%m%d")
    dtend = last["date"].strftime("%Y%m%d")

    header = "\r\n".join([
        "OFXHEADER:100",
        "DATA:OFXSGML",
        "VERSION:102",
        "SECURITY:TYPE1",
        "ENCODING:USASCII",
        "CHARSET:1252",
        "COMPRESSION:NONE",
        "OLDFILEUID:NONE",
        "NEWFILEUID:NONE",
        "",
        "",
    ])

    lines = []
    lines.append("<OFX>")
    lines.append("<SIGNONMSGSRSV1>")
    lines.append("<SONRS>")
    lines.append("<STATUS>")
    lines.append("<CODE>0")
    lines.append("<SEVERITY>INFO")
    lines.append("</STATUS>")
    lines.append(f"<DTSERVER>{now}")
    lines.append("<LANGUAGE>ENG")
    lines.append("<FI>")
    lines.append(f"<ORG>{sanitize_ascii(cfg['org'])}")
    lines.append(f"<FID>{sanitize_ascii(str(cfg['fid']))}")
    lines.append("</FI>")
    lines.append(f"<INTU.BID>{sanitize_ascii(str(cfg['intu_bid']))}")
    lines.append("</SONRS>")
    lines.append("</SIGNONMSGSRSV1>")
    lines.append("<BANKMSGSRSV1>")
    lines.append("<STMTTRNRS>")
    lines.append(f"<TRNUID>{now}")
    lines.append("<STATUS>")
    lines.append("<CODE>0")
    lines.append("<SEVERITY>INFO")
    lines.append("</STATUS>")
    lines.append("<STMTRS>")
    lines.append(f"<CURDEF>{sanitize_ascii(str(cfg['curdef']))}")
    lines.append("<BANKACCTFROM>")
    lines.append(f"<BANKID>{sanitize_ascii(str(cfg['bankid']))}")
    lines.append(f"<ACCTID>{sanitize_ascii(str(cfg['acctid']))}")
    lines.append(f"<ACCTTYPE>{sanitize_ascii(str(cfg['accttype']))}")
    lines.append("</BANKACCTFROM>")
    lines.append("<BANKTRANLIST>")
    lines.append(f"<DTSTART>{dtstart}")
    lines.append(f"<DTEND>{dtend}")

    for t in txns:
        trntype = "DEBIT" if t["amount"] < 0 else "CREDIT"
        lines.append("<STMTTRN>")
        lines.append(f"<TRNTYPE>{trntype}")
        lines.append(f"<DTPOSTED>{t['date'].strftime('%Y%m%d')}")
        lines.append(f"<TRNAMT>{money(t['amount'])}")
        lines.append(f"<FITID>{fitid(t)}")
        lines.append(f"<NAME>{sanitize_ascii(t['description'])}")
        lines.append("</STMTTRN>")

    lines.append("</BANKTRANLIST>")
    lines.append("<LEDGERBAL>")
    lines.append(f"<BALAMT>{money(last['balance'])}")
    lines.append(f"<DTASOF>{dtend}")
    lines.append("</LEDGERBAL>")
    lines.append("</STMTRS>")
    lines.append("</STMTTRNRS>")
    lines.append("</BANKMSGSRSV1>")
    lines.append("</OFX>")

    return header + "\r\n".join(lines) + "\r\n"


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Convert WealthSimple CSV statements into one YNAB4-ready QFX file.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to account.json (default: account.json next to this script, "
             "then the current directory).",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="CSV",
        help="One or more input CSV files followed by the output QFX file.",
    )
    args = parser.parse_args(argv)
    if len(args.paths) < 2:
        parser.error("provide at least one input CSV and one output QFX path.")
    return args


def resolve_config_path(explicit):
    if explicit:
        return explicit
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(script_dir, "account.json")
    if os.path.exists(candidate):
        return candidate
    return "account.json"


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    *csv_paths, out_path = args.paths

    try:
        cfg = load_config(resolve_config_path(args.config))
        txns = read_csv_files(csv_paths)
        if not txns:
            raise ConversionError("No transactions found in the provided CSV files.")
        before = len(txns)
        txns = dedupe(txns)
        # Stable sort by date; keeps within-day file order for ties.
        txns.sort(key=lambda t: t["date"])
        qfx = build_qfx(txns, cfg)
        with open(out_path, "w", encoding="ascii", newline="") as fh:
            fh.write(qfx)
    except ConversionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    dropped = before - len(txns)
    note = f" ({dropped} duplicate(s) dropped)" if dropped else ""
    print(f"Wrote {len(txns)} transaction(s){note} to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
