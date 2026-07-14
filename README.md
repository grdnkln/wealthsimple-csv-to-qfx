# WealthSimple CSV → QFX (YNAB4) converter

(written by Claude)

`ws_csv_to_qfx.py` converts WealthSimple chequing-account CSV statements into a
single Quicken **QFX** (OFX 1.02 SGML) file that imports cleanly into
**YNAB4** (You Need A Budget 4).

- Standard library only (no third-party modules). Runs on any Python 3.
- Fully local — no data leaves the machine.
- Merges multiple monthly CSVs into one QFX and removes duplicate rows that
  appear where consecutive statements overlap.

## Usage

```bash
python3 ws_csv_to_qfx.py [--config account.json] IN1.csv [IN2.csv ...] OUT.qfx
```

The **last** positional argument is the output QFX; every argument before it is
an input CSV. Example:

```bash
python3 ws_csv_to_qfx.py *.csv wealthsimple.qfx
```

`--config` is optional and defaults to `account.json` beside the script (then
the current directory).

## Configuration — `account.json`

The QFX needs a few facts that aren't in the CSVs. Fill these in with your real
account details (placeholders ship by default):

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
