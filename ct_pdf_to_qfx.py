#!/usr/bin/env python3
"""Convert Canadian Tire Triangle Mastercard PDF statements into a single
Quicken QFX (OFX 1.02 SGML) credit-card file suitable for import into YNAB4.

Text is extracted with the `pdftotext` command-line tool (poppler-utils).
Otherwise standard library only; all processing is local.

Usage:
    python3 ct_pdf_to_qfx.py [--config accounts.json] [--posting-date] \
        IN1.pdf [IN2.pdf ...] OUT.qfx

The last positional argument is the output QFX path; every preceding positional
argument is an input PDF statement. Each statement is reconciled against its
printed balances before anything is written.
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime
from decimal import Decimal

from ws_csv_to_qfx import (
    CONFIG_FILENAME, ConversionError, load_config, money, sanitize_ascii,
)

CONFIG_SECTION = "canadiantire"
# org and fid are optional: Canadian Tire's own QFX downloads omit the FI block.
CONFIG_KEYS = ("acctid", "curdef", "intu_bid")

LONG_DATE = r"[A-Z][a-z]+ \d{1,2}, \d{4}"
AMOUNT = r"-?\$?[\d,]+\.\d{2}"

STATEMENT_DATE_RE = re.compile(rf"Statement date:\s+({LONG_DATE})")
PERIOD_RE = re.compile(rf"For the period:\s+({LONG_DATE}) to ({LONG_DATE})")
PREV_BALANCE_RE = re.compile(rf"Balance from your last statement\s+({AMOUNT})")
NEW_BALANCE_RE = re.compile(rf"Your New Balance\s+({AMOUNT})")
AVAILABLE_CREDIT_RE = re.compile(rf"Available credit\s+({AMOUNT})")

# A transaction row in the account-summary detail section. Text from the
# right-hand messages column may follow the amount on the same line.
TXN_RE = re.compile(
    r"^\s{1,6}([A-Z][a-z]{2} \d{2})\s+([A-Z][a-z]{2} \d{2})\s+"
    r"(\S.*?)\s{2,}(-?[\d,]+\.\d{2})(?:\s|$)"
)

SECTION_START = "Details of your account summary"
SECTION_END_MARKERS = (
    "Other details about your account",
    "Details of your interest charges",
    "Details of your Canadian Tire store purchases",
)


def pdf_to_text(path):
    """Extract layout-preserving text from a PDF using pdftotext."""
    if not os.path.exists(path):
        raise ConversionError(f"PDF file not found: {path}")
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", path, "-"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        raise ConversionError(
            "pdftotext not found. Install poppler-utils "
            "(e.g. 'sudo pacman -S poppler' or 'sudo apt install poppler-utils')."
        )
    if result.returncode != 0:
        raise ConversionError(f"{path}: pdftotext failed: {result.stderr.strip()}")
    return result.stdout


def parse_amount(text):
    return Decimal(text.replace("$", "").replace(",", ""))


def parse_long_date(text):
    return datetime.strptime(text, "%B %d, %Y").date()


def infer_date(short, statement_date):
    """Turn 'Dec 13' into a date, using the statement date to pick the year.

    Statements never span more than about a month, so a month later than the
    statement month must belong to the previous year.
    """
    parsed = datetime.strptime(f"{short} {statement_date.year}", "%b %d %Y").date()
    if parsed.month > statement_date.month:
        parsed = parsed.replace(year=statement_date.year - 1)
    return parsed


def search_required(regex, text, label, source):
    m = regex.search(text)
    if not m:
        raise ConversionError(f"{source}: could not find {label}")
    return m


def detail_section(lines):
    """Return the lines of the account-summary detail section, or [] if the
    statement has no such section (no activity that period)."""
    start = next((i for i, l in enumerate(lines) if l.startswith(SECTION_START)), None)
    if start is None:
        return []
    end = next(
        (i for i in range(start + 1, len(lines))
         if any(marker in lines[i] for marker in SECTION_END_MARKERS)),
        len(lines),
    )
    return lines[start:end]


def parse_statement(path, use_posting_date):
    """Parse one PDF statement into metadata plus a list of transactions."""
    text = pdf_to_text(path)

    statement_date = parse_long_date(
        search_required(STATEMENT_DATE_RE, text, "statement date", path).group(1)
    )
    period = search_required(PERIOD_RE, text, "statement period", path)
    period_start = parse_long_date(period.group(1))
    period_end = parse_long_date(period.group(2))
    prev_balance = parse_amount(
        search_required(PREV_BALANCE_RE, text, "previous balance", path).group(1)
    )
    new_balance = parse_amount(
        search_required(NEW_BALANCE_RE, text, "new balance", path).group(1)
    )
    available = AVAILABLE_CREDIT_RE.search(text)

    txns = []
    for index, line in enumerate(detail_section(text.splitlines())):
        m = TXN_RE.match(line)
        if not m:
            continue
        txn_short, post_short, description, amount = m.groups()
        txn_date = infer_date(txn_short, statement_date)
        txns.append({
            "index": index,
            "date": infer_date(post_short, statement_date) if use_posting_date
                    else txn_date,
            "txn_date": txn_date,
            "description": description.strip(),
            "amount": parse_amount(amount),
        })

    total = sum((t["amount"] for t in txns), Decimal("0"))
    if prev_balance + total != new_balance:
        raise ConversionError(
            f"{path}: transactions do not reconcile with the statement "
            f"(previous balance {prev_balance} + parsed transactions {total} "
            f"!= new balance {new_balance}). The statement may contain a "
            f"format this script does not understand yet."
        )

    return {
        "path": path,
        "statement_date": statement_date,
        "period_start": period_start,
        "period_end": period_end,
        "new_balance": new_balance,
        "available_credit": parse_amount(available.group(1)) if available else None,
        # Purchases paid entirely by CT Money or gift card show as 0.00.
        "txns": [t for t in txns if t["amount"] != 0],
    }


def fitid(statement, t):
    """Deterministic, stable, unique transaction id."""
    basis = "|".join([
        statement["statement_date"].strftime("%Y%m%d"),
        str(t["index"]),
        t["date"].strftime("%Y%m%d"),
        format(t["amount"], "f"),
        t["description"],
    ])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]


def build_qfx(statements, cfg):
    """Build the full OFX 1.02 SGML credit-card document as a string."""
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    latest = statements[-1]

    # Statement amounts are from the bank's view (purchases positive); QFX
    # credit-card amounts are from the cardholder's view (purchases negative).
    entries = [
        (t["date"], -t["amount"], fitid(s, t), t["description"], t["txn_date"])
        for s in statements for t in s["txns"]
    ]
    entries.sort(key=lambda e: e[0])

    # A purchase can predate its statement period (bought before, posted after).
    dates = [e[0] for e in entries]
    dtstart = min([s["period_start"] for s in statements] + dates).strftime("%Y%m%d")
    dtend = max([s["period_end"] for s in statements] + dates).strftime("%Y%m%d")

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
    if cfg.get("org") or cfg.get("fid"):
        lines.append("<FI>")
        lines.append(f"<ORG>{sanitize_ascii(str(cfg.get('org', '')))}")
        lines.append(f"<FID>{sanitize_ascii(str(cfg.get('fid', '')))}")
        lines.append("</FI>")
    lines.append(f"<INTU.BID>{sanitize_ascii(str(cfg['intu_bid']))}")
    lines.append("</SONRS>")
    lines.append("</SIGNONMSGSRSV1>")
    lines.append("<CREDITCARDMSGSRSV1>")
    lines.append("<CCSTMTTRNRS>")
    lines.append(f"<TRNUID>{now}")
    lines.append("<STATUS>")
    lines.append("<CODE>0")
    lines.append("<SEVERITY>INFO")
    lines.append("</STATUS>")
    lines.append("<CCSTMTRS>")
    lines.append(f"<CURDEF>{sanitize_ascii(str(cfg['curdef']))}")
    lines.append("<CCACCTFROM>")
    lines.append(f"<ACCTID>{sanitize_ascii(str(cfg['acctid']))}")
    lines.append("</CCACCTFROM>")
    lines.append("<BANKTRANLIST>")
    lines.append(f"<DTSTART>{dtstart}")
    lines.append(f"<DTEND>{dtend}")

    for date, amount, txn_id, description, txn_date in entries:
        trntype = "DEBIT" if amount < 0 else "CREDIT"
        lines.append("<STMTTRN>")
        lines.append(f"<TRNTYPE>{trntype}")
        lines.append(f"<DTPOSTED>{date.strftime('%Y%m%d')}")
        lines.append(f"<DTUSER>{txn_date.strftime('%Y%m%d')}")
        lines.append(f"<TRNAMT>{money(amount)}")
        lines.append(f"<FITID>{txn_id}")
        lines.append(f"<NAME>{sanitize_ascii(description)}")
        lines.append("</STMTTRN>")

    lines.append("</BANKTRANLIST>")
    lines.append("<LEDGERBAL>")
    lines.append(f"<BALAMT>{money(-latest['new_balance'])}")
    lines.append(f"<DTASOF>{latest['statement_date'].strftime('%Y%m%d')}")
    lines.append("</LEDGERBAL>")
    if latest["available_credit"] is not None:
        lines.append("<AVAILBAL>")
        lines.append(f"<BALAMT>{money(latest['available_credit'])}")
        lines.append(f"<DTASOF>{latest['statement_date'].strftime('%Y%m%d')}")
        lines.append("</AVAILBAL>")
    lines.append("</CCSTMTRS>")
    lines.append("</CCSTMTTRNRS>")
    lines.append("</CREDITCARDMSGSRSV1>")
    lines.append("</OFX>")

    return header + "\r\n".join(lines) + "\r\n", len(entries)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Convert Canadian Tire Triangle Mastercard PDF statements "
                    "into one YNAB4-ready QFX file.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"Path to {CONFIG_FILENAME} (default: {CONFIG_FILENAME} next to "
             f"this script, then the current directory).",
    )
    parser.add_argument(
        "--posting-date",
        action="store_true",
        help="Use the posting date instead of the transaction date.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="PDF",
        help="One or more input PDF statements followed by the output QFX file.",
    )
    args = parser.parse_args(argv)
    if len(args.paths) < 2:
        parser.error("provide at least one input PDF and one output QFX path.")
    return args


def resolve_config_path(explicit):
    if explicit:
        return explicit
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(script_dir, CONFIG_FILENAME)
    if os.path.exists(candidate):
        return candidate
    return CONFIG_FILENAME


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    *pdf_paths, out_path = args.paths

    try:
        cfg = load_config(resolve_config_path(args.config),
                          CONFIG_SECTION, CONFIG_KEYS)
        statements = {}
        duplicates = 0
        for path in pdf_paths:
            statement = parse_statement(path, args.posting_date)
            if statement["statement_date"] in statements:
                duplicates += 1
                continue
            statements[statement["statement_date"]] = statement
        ordered = [statements[d] for d in sorted(statements)]
        qfx, count = build_qfx(ordered, cfg)
        with open(out_path, "w", encoding="ascii", newline="") as fh:
            fh.write(qfx)
    except ConversionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    note = f" ({duplicates} duplicate statement(s) skipped)" if duplicates else ""
    print(f"Wrote {count} transaction(s) from {len(ordered)} statement(s){note} "
          f"to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
