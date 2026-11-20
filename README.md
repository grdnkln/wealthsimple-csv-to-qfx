# WealthSimple CSV → QFX (YNAB4) converter


## AI Usage

This entire project was written by Claude, including this README.md

## Introduction

`ws_csv_to_qfx.py` converts WealthSimple chequing-account CSV statements into a
single Quicken **QFX** (OFX 1.02 SGML) file that imports cleanly into
**YNAB4** (You Need A Budget 4).

- Standard library only (no third-party modules). Runs on any Python 3.
- Fully local — no data leaves the machine.
- Merges multiple monthly CSVs into one QFX and removes duplicate rows that
  appear where consecutive statements overlap.

## Getting the CSV files from WealthSimple

The input CSVs are the **monthly statements** WealthSimple generates for your
chequing account. Download them from the **Documents** section:

1. Sign in to WealthSimple (app or [my.wealthsimple.com](https://my.wealthsimple.com)).
2. Open your profile / account menu and go to **Documents**.
3. Select your account from the Filters section on the right
4. Download each monthly statement by clicking the **CSV** button beside each statement

Repeat for each month you want to import, then pass all the CSVs to the script
(see Usage below).

See WealthSimple's help article for the current steps and screenshots:
<https://help.wealthsimple.com/hc/en-ca/articles/360056580894-Find-your-monthly-statements>

## Usage

```bash
python3 ws_csv_to_qfx.py [--config accounts.json] IN1.csv [IN2.csv ...] OUT.qfx
```

The **last** positional argument is the output QFX; every argument before it is
an input CSV. Example:

```bash
python3 ws_csv_to_qfx.py *.csv wealthsimple.qfx
```

`--config` is optional and defaults to `accounts.json` beside the script (then
the current directory).

## Configuration — `accounts.json`

Both converters share one `accounts.json`, with a `wealthsimple` section and a
`canadiantire` section. Copy `accounts.json.example` to `accounts.json` and
fill in your real account details.

The QFX needs a few facts that aren't in the CSVs. Keys in the `wealthsimple`
section:

| key        | meaning                                             |
|------------|-----------------------------------------------------|
| `bankid`   | Bank transit id for the account                     |
| `acctid`   | Account number                                      |
| `accttype` | `CHECKING` (chequing) / `SAVINGS`                   |
| `curdef`   | Default currency, e.g. `CAD`                        |
| `org`      | Financial institution name                          |
| `fid`      | Financial institution id                            |
| `intu_bid` | Intuit bank id                                      |

YNAB4 matches an import to an account partly by `bankid` + `acctid`, so keep
them consistent between imports for the same account.

## What gets mapped

| CSV column    | QFX field                                          |
|---------------|----------------------------------------------------|
| `date`        | `DTPOSTED` (and `DTSTART`/`DTEND`, `LEDGERBAL` date)|
| `amount`      | `TRNAMT`; sign sets `TRNTYPE` (DEBIT/CREDIT)        |
| `description` | `NAME` (the YNAB payee)                             |
| `balance`     | last row → `LEDGERBAL`                              |

Each transaction gets a stable `FITID` (SHA-1 of date+amount+balance+description)
so re-importing the same statement will not create duplicates. The WealthSimple
type code column is not included in the output.

Non-ASCII characters in descriptions (e.g. `®`) are transliterated to plain
ASCII so the OFX 1.02 file stays clean 7-bit.

# Canadian Tire Mastercard PDF → QFX converter

`ct_pdf_to_qfx.py` converts Canadian Tire **Triangle Mastercard** PDF
statements into a single credit-card QFX file for YNAB4.

- Requires the `pdftotext` command from **poppler** (`sudo pacman -S poppler`,
  `sudo apt install poppler-utils`, or `brew install poppler`). No Python
  packages beyond the standard library.
- Imports helpers from `ws_csv_to_qfx.py`, so keep both scripts in the same
  folder.

## Getting the PDF statements

Sign in at [ctfs.com](https://www.ctfs.com), open your Triangle Mastercard
account, go to **Statements**, and download each monthly statement as a PDF.

## Usage

```bash
python3 ct_pdf_to_qfx.py [--config accounts.json] [--posting-date] IN1.pdf [IN2.pdf ...] OUT.qfx
```

Example:

```bash
python3 ct_pdf_to_qfx.py *Triangle-Mastercard.pdf canadian-tire.qfx
```

- Transactions are dated by **transaction date** by default; pass
  `--posting-date` to use the posting date instead.
- Passing the same statement twice is harmless; duplicates are skipped by
  statement date.

## Configuration — `canadiantire` section of `accounts.json`

The values mirror the QFX files Canadian Tire's website exports directly, so
PDF-converted imports land in the same YNAB4 account as direct downloads:

| key        | meaning                                                          |
|------------|------------------------------------------------------------------|
| `acctid`   | The 11-digit reference number printed at the bottom left of page 1 of the statement (e.g. `00041337314`), not the card number |
| `curdef`   | Default currency, `CAD`                                          |
| `intu_bid` | Intuit bank id; Canadian Tire uses `10177`                       |
| `org`, `fid` | Optional. Canadian Tire's own QFX omits the `<FI>` block, so leave these out to match it |

Transaction IDs (`FITID`) cannot be reproduced from the PDF, so they differ
from those in Canadian Tire's direct downloads. Avoid importing the same
period from both sources, or let YNAB4's duplicate matching catch overlaps.

## How it works

- Transactions are read from the **Details of your account summary** section
  (payments, purchases, and any other rows in the same format). The itemized
  "Details of your Canadian Tire store purchases" section is ignored.
- Statement dates have no year; it is taken from the statement date (December
  purchases on a January statement get the previous year).
- Amounts are flipped to the cardholder's view: purchases become negative
  (`DEBIT`), payments positive (`CREDIT`). `LEDGERBAL` is the negative of the
  latest statement's **New Balance**, and `AVAILBAL` is its **Available
  credit**.
- `DTUSER` always holds the transaction date, as in Canadian Tire's own
  exports; `DTPOSTED` holds the transaction date unless `--posting-date` is
  given.
- `$0.00` rows (store purchases paid fully with CT Money or a gift card) are
  skipped.
- **Safety check:** every statement must satisfy
  `previous balance + parsed transactions = new balance`. If any statement
  fails, the script stops without writing output. This guards against
  statement formats not seen yet (returns, fees, interest, foreign currency),
  which have not appeared in the statements used to develop the script.
