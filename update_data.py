#!/usr/bin/env python3
"""Refresh the saved data files that index.html loads.

  debt.js     Daily "Debt to the Penny" figures since April 1993 plus the yearly
              history back to 1790. The page draws from this copy straight away and
              falls back to it whenever the live Treasury request is slow or fails.
  holders.js  Treasury's monthly "Major Foreign Holders of Treasury Securities" table
              for the "Who holds the debt" section (a web page cannot fetch it live).

Run it about once a month, after Treasury posts the holders table around the 18th.
Running it more often is fine and keeps the saved debt figures fresher.

    python3 update_data.py            # refresh both files
    python3 update_data.py --check    # only verify the files already on disk
    python3 update_data.py --quiet    # print nothing unless something went wrong

For a scheduled job use --quiet: a successful run is silent and exits 0, so cron has
nothing to log or mail. A failure prints one line per affected file and exits 1.

    0 23 * * * /usr/bin/python3 /path/to/update_data.py --quiet

Safety rules, because a bad data file would spoil the page for every visitor:

  * Each file is handled on its own; a failure in one never touches the other.
  * Downloads are retried, and new data must pass validation before anything is written.
  * New data may never be older or smaller than what is already saved.
  * A file is written under a temporary name, read back and validated, and only then
    swapped into place in one step. The previous good file is kept as <name>.bak.
  * On any failure the existing file is left exactly as it was, and the script exits
    with status 1 so a scheduled job notices.

Uses only the Python standard library.
"""
import argparse
import datetime
import json
import math
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DEBT_FILE = HERE / "debt.js"
HOLDERS_FILE = HERE / "holders.js"
DEBT_VAR = "DEBT_SNAPSHOT"
HOLDERS_VAR = "FOREIGN_HOLDERS"

FISCAL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/accounting/od/"
DAILY_URL = FISCAL + "debt_to_penny"
YEARLY_URL = FISCAL + "debt_outstanding"
HOLDERS_URL = "https://ticdata.treasury.gov/resource-center/data-chart-center/tic/Documents/slt_table5.txt"
USER_AGENT = "Mozilla/5.0 (us-debt-counter update script)"

DAILY_START = "1993-04-01"   # first day of Debt to the Penny; earlier years come from the yearly table
MIN_DAILY_ROWS = 8000        # there were about 8,400 business days of data by late 2026
MIN_YEARLY_ROWS = 200        # 1790 through 1992
MAX_DAILY_CHANGE = 0.06      # the debt has never moved anywhere near 6% in a day
STALE_WARNING_DAYS = 10
MIN_COUNTRIES = 10

HTTP_TIMEOUT = 30            # seconds per request
HTTP_ATTEMPTS = 3
HTTP_FIRST_DELAY = 2         # seconds; doubles after each failed try


class DataError(Exception):
    """Downloaded or saved data is missing, malformed or implausible."""


# ----------------------------------------------------------------------------- downloading

def http_get(url):
    """Return the body of `url` as text, retrying with a doubling delay."""
    delay, last_error = HTTP_FIRST_DELAY, None
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                text = response.read().decode("utf-8", errors="replace")
            if not text.strip():
                raise DataError("the response was empty")
            return text
        except (urllib.error.URLError, OSError, DataError) as err:  # includes HTTP errors and timeouts
            last_error = err
            if attempt < HTTP_ATTEMPTS:
                time.sleep(delay)
                delay *= 2
    host = urllib.parse.urlsplit(url).netloc
    raise DataError(f"could not download from {host} after {HTTP_ATTEMPTS} tries ({last_error})")


def fetch_fiscal(url, fields, date_filter):
    """Fetch every page of a Treasury Fiscal Data query and return the raw rows."""
    rows, page, pages, expected = [], 1, 1, None
    while page <= pages:
        query = urllib.parse.urlencode({
            "fields": ",".join(fields),
            "filter": date_filter,
            "sort": "record_date",
            "page[size]": "10000",
            "page[number]": str(page),
        })
        text = http_get(url + "?" + query)
        try:
            payload = json.loads(text)
        except ValueError:
            raise DataError(f"Treasury did not send JSON (response starts {text[:60]!r})")
        if (not isinstance(payload, dict) or not isinstance(payload.get("data"), list)
                or not isinstance(payload.get("meta"), dict)):
            raise DataError("Treasury's response does not have the expected data/meta layout")
        rows.extend(payload["data"])
        try:
            pages = int(payload["meta"].get("total-pages") or 1)
        except (TypeError, ValueError):
            raise DataError("Treasury's response has an unreadable page count")
        if pages > 50:
            raise DataError(f"Treasury reports an implausible {pages} pages of data")
        expected = payload["meta"].get("total-count")
        page += 1
    if isinstance(expected, int) and expected != len(rows):
        raise DataError(f"Treasury said {expected} rows but sent {len(rows)}")
    return rows


def number(value, what):
    """Convert an API value to float; Treasury writes missing values as the string "null"."""
    if value is None or value in ("", "null"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise DataError(f"{what} is not a number: {value!r}")
    if not math.isfinite(result):
        raise DataError(f"{what} is not a finite number: {value!r}")
    return result


def download_daily():
    raw = fetch_fiscal(
        DAILY_URL,
        ["record_date", "tot_pub_debt_out_amt", "debt_held_public_amt", "intragov_hold_amt"],
        "record_date:gte:" + DAILY_START,
    )
    rows = []
    for r in raw:
        if not isinstance(r, dict):
            raise DataError("a daily row is not an object")
        date = r.get("record_date")
        rows.append([
            date,
            number(r.get("tot_pub_debt_out_amt"), f"total debt on {date}"),
            number(r.get("debt_held_public_amt"), f"debt held by the public on {date}"),
            number(r.get("intragov_hold_amt"), f"intragovernmental holdings on {date}"),
        ])
    return rows


def download_yearly():
    raw = fetch_fiscal(YEARLY_URL, ["record_date", "debt_outstanding_amt"], "record_date:lt:" + DAILY_START)
    rows = []
    for r in raw:
        if not isinstance(r, dict):
            raise DataError("a yearly row is not an object")
        date = r.get("record_date")
        rows.append([date, number(r.get("debt_outstanding_amt"), f"debt outstanding on {date}")])
    return rows


def download_holders():
    return parse_holders(http_get(HOLDERS_URL))


def parse_holders(text):
    """Parse Treasury's tab-separated Major Foreign Holders table."""
    lines = [line.rstrip("\r\n").split("\t") for line in text.splitlines()]
    header_at = next((i for i, cells in enumerate(lines) if cells and cells[0].strip() == "Country"), None)
    if header_at is None:
        raise DataError("the holders table has no 'Country' header row (the file layout may have changed)")
    months = [c.strip() for c in lines[header_at][1:] if c.strip()]

    countries, extras = [], {}
    for cells in lines[header_at + 1:]:
        name = cells[0].strip() if cells else ""
        if not name:
            break  # a blank line ends the table
        try:
            values = [float(c.replace(",", "")) for c in cells[1:1 + len(months)]]
        except ValueError:
            raise DataError(f"the holders row for {name!r} has a value that is not a number")
        if len(values) != len(months):
            raise DataError(f"the holders row for {name!r} has {len(values)} values for {len(months)} months")
        key = {
            "All Other": "allOther",
            "Grand Total": "grandTotal",
            "Of Which: Foreign Official": "foreignOfficial",
        }.get(name)
        if key:
            extras[key] = values
        elif not name.startswith("Of Which"):
            countries.append({"name": name, "values": values})
    return {"months": months, "countries": countries, **extras}


# ----------------------------------------------------------------------------- validation

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def check_date(value, what):
    if not isinstance(value, str) or not ISO_DATE.match(value):
        raise DataError(f"{what} is not a YYYY-MM-DD date: {value!r}")
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise DataError(f"{what} is not a real date: {value!r}")


def is_amount(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_daily(daily):
    if not isinstance(daily, list):
        raise DataError("the daily figures are missing")
    if len(daily) < MIN_DAILY_ROWS:
        raise DataError(f"only {len(daily)} daily figures (expected at least {MIN_DAILY_ROWS:,})")
    previous_date, previous_total = None, None
    for row in daily:
        if not isinstance(row, list) or len(row) != 4:
            raise DataError(f"a daily row does not have 4 values: {row!r}")
        date, total, public, intragov = row
        check_date(date, "a daily date")
        if previous_date is not None and date <= previous_date:
            raise DataError(f"daily dates are out of order or repeated at {date}")
        if not is_amount(total) or total <= 0:
            raise DataError(f"the total debt on {date} is not a positive number: {total!r}")
        for label, part in (("debt held by the public", public), ("intragovernmental holdings", intragov)):
            if part is not None and (not is_amount(part) or part < 0):
                raise DataError(f"{label} on {date} is not a valid amount: {part!r}")
        if previous_total is not None and abs(total / previous_total - 1) > MAX_DAILY_CHANGE:
            raise DataError(f"the debt jumps implausibly between {previous_date} and {date} "
                            f"({previous_total:,.0f} to {total:,.0f})")
        previous_date, previous_total = date, total
    if daily[0][0] != DAILY_START:
        raise DataError(f"daily figures start on {daily[0][0]}, not {DAILY_START}")
    date, total, public, intragov = daily[-1]
    if public is None or intragov is None:
        raise DataError(f"the latest figure ({date}) has no public / intragovernmental breakdown")
    if abs(public + intragov - total) > 0.005 * total:
        raise DataError(f"on {date} the public and intragovernmental parts do not add up to the total")


def validate_yearly(yearly, first_daily_total):
    if not isinstance(yearly, list):
        raise DataError("the yearly history is missing")
    if len(yearly) < MIN_YEARLY_ROWS:
        raise DataError(f"only {len(yearly)} yearly figures (expected at least {MIN_YEARLY_ROWS})")
    previous_date = None
    for row in yearly:
        if not isinstance(row, list) or len(row) != 2:
            raise DataError(f"a yearly row does not have 2 values: {row!r}")
        date, total = row
        check_date(date, "a yearly date")
        if previous_date is not None and date <= previous_date:
            raise DataError(f"yearly dates are out of order or repeated at {date}")
        if date >= DAILY_START:
            raise DataError(f"the yearly history runs past {DAILY_START} ({date})")
        if not is_amount(total) or total <= 0:
            raise DataError(f"the debt outstanding on {date} is not a positive number: {total!r}")
        previous_date = date
    if not yearly[0][0].startswith("1790"):
        raise DataError(f"the yearly history starts in {yearly[0][0][:4]}, not 1790")
    ratio = yearly[-1][1] / first_daily_total
    if not 0.75 <= ratio <= 1.25:
        raise DataError("the yearly history does not join up with the daily figures")


def validate_debt(data, previous=None):
    """Raise DataError unless `data` is a complete, plausible debt snapshot.

    With `previous` (the snapshot already saved), also refuse to go backwards.
    """
    if not isinstance(data, dict):
        raise DataError("the debt data is not an object")
    validate_daily(data.get("daily"))
    validate_yearly(data.get("yearly"), data["daily"][0][1])
    if not isinstance(data.get("generated"), str) or not data["generated"]:
        raise DataError("the debt data has no 'generated' timestamp")
    if previous is not None:
        if data["daily"][-1][0] < previous["daily"][-1][0]:
            raise DataError(f"the new figures end on {data['daily'][-1][0]}, earlier than the saved ones "
                            f"({previous['daily'][-1][0]})")
        if len(data["daily"]) < len(previous["daily"]):
            raise DataError(f"the new data has fewer daily figures ({len(data['daily']):,}) than the saved "
                            f"data ({len(previous['daily']):,})")


def validate_holders(data, previous=None):
    """Raise DataError unless `data` is a complete, plausible foreign-holders table."""
    if not isinstance(data, dict):
        raise DataError("the holders data is not an object")
    months = data.get("months")
    if not isinstance(months, list) or len(months) < 2:
        raise DataError("the holders table has fewer than 2 months")
    for i, month in enumerate(months):
        if not isinstance(month, str) or not ISO_MONTH.match(month):
            raise DataError(f"a holders month is not YYYY-MM: {month!r}")
        if i:
            year, mon = int(months[i - 1][:4]), int(months[i - 1][5:])
            expected = f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"
            if month != expected:
                raise DataError(f"holders months are not consecutive, newest first ({months[i - 1]} then {month})")

    def check_series(values, what, positive):
        if not isinstance(values, list) or len(values) != len(months):
            raise DataError(f"{what} does not have one value per month")
        for value in values:
            if not is_amount(value) or value < 0 or (positive and value <= 0):
                raise DataError(f"{what} has an invalid value: {value!r}")

    countries = data.get("countries")
    if not isinstance(countries, list) or len(countries) < MIN_COUNTRIES:
        count = len(countries) if isinstance(countries, list) else 0
        raise DataError(f"the holders table lists only {count} countries (expected at least {MIN_COUNTRIES})")
    names = set()
    for country in countries:
        if not isinstance(country, dict) or not isinstance(country.get("name"), str) or not country["name"].strip():
            raise DataError("a holders country has no name")
        if country["name"] in names:
            raise DataError(f"the holders table lists {country['name']} twice")
        names.add(country["name"])
        check_series(country.get("values"), f"the holders row for {country['name']}", positive=False)
    for key, label in (("allOther", "the 'All Other' row"), ("grandTotal", "the 'Grand Total' row")):
        if key not in data:
            raise DataError(f"the holders table is missing {label}")
        check_series(data[key], label, positive=True)
    for i, month in enumerate(months):
        listed = sum(c["values"][i] for c in countries) + data["allOther"][i]
        if abs(listed - data["grandTotal"][i]) > 0.01 * data["grandTotal"][i]:
            raise DataError(f"for {month} the rows add up to {listed:,.1f} but the grand total is "
                            f"{data['grandTotal'][i]:,.1f}")
    if not 1000 <= data["grandTotal"][0] <= 100000:  # billions of dollars
        raise DataError(f"the foreign holdings total ({data['grandTotal'][0]:,.1f} billion) is not plausible")
    if "foreignOfficial" in data:
        check_series(data["foreignOfficial"], "the 'Foreign Official' row", positive=True)
        if data["foreignOfficial"][0] > data["grandTotal"][0]:
            raise DataError("foreign official holdings exceed the grand total")
    check_date(data.get("retrieved"), "the holders 'retrieved' date")
    if previous is not None and months[0] < previous["months"][0]:
        raise DataError(f"the new table's latest month ({months[0]}) is older than the saved one "
                        f"({previous['months'][0]})")


# ----------------------------------------------------------------------------- reading and writing

def parse_js(text, var, name):
    """Extract the JSON value assigned to window.<var> in one of our data files."""
    marker = f"window.{var} ="
    at = text.find(marker)
    if at < 0:
        raise DataError(f"{name} does not define {var} (the file may be empty or truncated)")
    body = text[at + len(marker):].strip()
    if body.endswith(";"):
        body = body[:-1]
    try:
        return json.loads(body)
    except ValueError as err:
        raise DataError(f"{name} is not readable ({err})")


def read_saved(path, var):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DataError(f"{path.name} does not exist")
    except (OSError, UnicodeDecodeError) as err:
        raise DataError(f"{path.name} could not be read ({err})")
    return parse_js(text, var, path.name)


def load_valid(path, var, validate):
    """Return (data, None) if the file on disk is good, else (None, reason)."""
    try:
        data = read_saved(path, var)
        validate(data)
        return data, None
    except DataError as err:
        return None, str(err)


def debt_js(data):
    """One figure per line, so the file stays readable and diffs stay small."""
    def block(rows):
        return ",\n".join(json.dumps(row, separators=(",", ":")) for row in rows)
    return (
        "// Generated by update_data.py from US Treasury Fiscal Data. Do not edit by hand.\n"
        "// daily rows: [date, total debt, debt held by the public, intragovernmental holdings], in dollars.\n"
        "// yearly rows: [date, debt outstanding], in dollars.\n"
        f"window.{DEBT_VAR} = {{\n"
        f"\"generated\": {json.dumps(data['generated'])},\n"
        f"\"source\": {json.dumps(data['source'])},\n"
        f"\"daily\": [\n{block(data['daily'])}\n],\n"
        f"\"yearly\": [\n{block(data['yearly'])}\n]\n"
        "};\n"
    )


def holders_js(data):
    return (
        "// Generated by update_data.py from US Treasury TIC data. Do not edit by hand.\n"
        f"window.{HOLDERS_VAR} = " + json.dumps(data, indent=1) + ";\n"
    )


def install(path, var, text, validate):
    """Replace `path` with `text` safely.

    The text is validated before and after it reaches the disk, the previous good file
    is kept as <name>.bak, and the swap is a single atomic rename, so the page can never
    be served a half-written or invalid file.
    """
    validate(parse_js(text, var, path.name))  # exactly what we are about to write
    had_good_file = load_valid(path, var, validate)[0] is not None
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        validate(read_saved(pathlib.Path(tmp_name), var))  # exactly what landed on disk
        os.chmod(tmp_name, 0o644)  # temporary files start out private; a web server must be able to read this
        if had_good_file:
            shutil.copy2(str(path), str(path) + ".bak")
        os.replace(tmp_name, str(path))
        tmp_name = None
    except OSError as err:
        raise DataError(f"could not write {path.name} ({err})")
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


# ----------------------------------------------------------------------------- the two updates

def update_debt(force=False):
    previous, _ = load_valid(DEBT_FILE, DEBT_VAR, validate_debt)
    notes = []

    daily = download_daily()
    validate_daily(daily)

    # The yearly history (1790-1992) never changes, so a saved copy is a safe stand-in
    # if that download fails or comes back wrong.
    try:
        yearly = download_yearly()
        validate_yearly(yearly, daily[0][1])
    except DataError as err:
        if previous is None:
            raise
        yearly = previous["yearly"]
        notes.append(f"the yearly history could not be refreshed ({err}), so the saved copy was reused")

    data = {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "https://fiscaldata.treasury.gov/datasets/debt-to-the-penny/",
        "daily": daily,
        "yearly": yearly,
    }
    validate_debt(data, previous)

    latest = daily[-1][0]
    age = (datetime.date.today() - datetime.date.fromisoformat(latest)).days
    if age > STALE_WARNING_DAYS:
        notes.append(f"warning: Treasury's latest figure is {age} days old")
    summary = f"{len(daily):,} daily figures through {latest}, {len(yearly)} yearly figures"

    if previous is not None and not force and previous["daily"] == daily and previous["yearly"] == yearly:
        return "already up to date (" + summary + ")", notes
    install(DEBT_FILE, DEBT_VAR, debt_js(data), validate_debt)
    return "updated (" + summary + ")", notes


def update_holders(force=False):
    previous, _ = load_valid(HOLDERS_FILE, HOLDERS_VAR, validate_holders)
    table = download_holders()
    data = {
        "source": HOLDERS_URL,
        "unit": "billions of US dollars",
        "retrieved": datetime.date.today().isoformat(),
        **table,
    }
    validate_holders(data, previous)
    summary = (f"{len(data['countries'])} countries, latest month {data['months'][0]}, "
               f"total ${data['grandTotal'][0]:,.1f}B")

    def content(d):
        return {k: v for k, v in d.items() if k != "retrieved"}
    if previous is not None and not force and content(previous) == content(data):
        return "already up to date (" + summary + ")", []
    install(HOLDERS_FILE, HOLDERS_VAR, holders_js(data), validate_holders)
    return "updated (" + summary + ")", []


TASK_NAMES = ["debt", "holders"]


def tasks():
    """name -> (file, variable, validator, updater, what the page does without the file)."""
    return {
        "debt": (DEBT_FILE, DEBT_VAR, validate_debt, update_debt,
                 "the page will rely on live Treasury data only"),
        "holders": (HOLDERS_FILE, HOLDERS_VAR, validate_holders, update_holders,
                    "the page will hide the 'Who holds the debt' section"),
    }


def describe_saved(name):
    """One sentence on what is on disk right now, for failure messages."""
    path, var, validate, _, without = tasks()[name]
    data, problem = load_valid(path, var, validate)
    if data is None:
        return f"There is no usable {path.name} on disk ({problem}); {without}."
    through = data["daily"][-1][0] if name == "debt" else data["months"][0]
    return f"The existing {path.name} (figures through {through}) was left untouched."


def check(quiet=False):
    ok = True
    for name, (path, var, validate, _, without) in tasks().items():
        data, problem = load_valid(path, var, validate)
        if data is None:
            ok = False
            print(f"{path.name}: PROBLEM - {problem}; {without}.", file=sys.stderr)
        elif quiet:
            continue
        elif name == "debt":
            print(f"{path.name}: OK ({len(data['daily']):,} daily figures through {data['daily'][-1][0]}, "
                  f"{len(data['yearly'])} yearly figures, saved {data['generated']})")
        else:
            print(f"{path.name}: OK ({len(data['countries'])} countries, latest month {data['months'][0]}, "
                  f"retrieved {data['retrieved']})")
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Refresh the saved Treasury data files used by index.html.")
    parser.add_argument("--check", action="store_true", help="only verify the files already on disk")
    parser.add_argument("--only", choices=TASK_NAMES, help="refresh just one of the files")
    parser.add_argument("--force", action="store_true", help="rewrite a file even if its data has not changed")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="print nothing unless something went wrong (for cron and other scheduled jobs)")
    args = parser.parse_args(argv)
    if args.check:
        return check(quiet=args.quiet)

    ok = True
    for name in ([args.only] if args.only else TASK_NAMES):
        path, _, _, update, _ = tasks()[name]
        try:
            message, notes = update(force=args.force)
            if not args.quiet:
                print(f"{path.name}: {message}")
                for note in notes:
                    print(f"  note: {note}")
        except DataError as err:
            ok = False
            print(f"{path.name}: NOT UPDATED - {err}. {describe_saved(name)}", file=sys.stderr)
        except Exception as err:  # a bug here must not stop the other file from being handled
            ok = False
            print(f"{path.name}: NOT UPDATED - unexpected error ({type(err).__name__}: {err}). "
                  f"{describe_saved(name)}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
