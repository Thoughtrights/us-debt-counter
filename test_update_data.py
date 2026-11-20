#!/usr/bin/env python3
"""Tests for update_data.py. They never touch the network or the real data files.

    python3 -m unittest test_update_data -v

Each test runs the updater against canned Treasury responses (good, empty, truncated,
wrong) in a temporary folder and checks the one rule that matters: a good data file
is never replaced by a bad one, and a bad file is never created.
"""
import contextlib
import datetime
import io
import json
import pathlib
import tempfile
import unittest

import update_data as ud


# ----------------------------------------------------------------------------- canned Treasury data

def make_daily(days=8300, end_total=40.0e12):
    """Business-day rows from 1993-04-01, growing smoothly, in the API's string format."""
    rows, date, total = [], datetime.date(1993, 4, 1), 4.2e12
    step = (end_total / total) ** (1 / (days - 1))
    while len(rows) < days:
        if date.weekday() < 5:
            has_parts = date >= datetime.date(2001, 1, 2)
            rows.append({
                "record_date": date.isoformat(),
                "tot_pub_debt_out_amt": f"{total:.2f}",
                "debt_held_public_amt": f"{total * 0.8:.2f}" if has_parts else "null",
                "intragov_hold_amt": f"{total * 0.2:.2f}" if has_parts else "null",
            })
            total *= step
        date += datetime.timedelta(days=1)
    return rows


def make_yearly():
    rows, total = [], 71.0e6
    step = (4.06e12 / total) ** (1 / 202)
    for year in range(1790, 1993):
        rows.append({"record_date": f"{year}-06-30", "debt_outstanding_amt": f"{total:.2f}"})
        total *= step
    return rows


def fiscal_json(rows):
    return json.dumps({"data": rows, "meta": {"total-pages": 1, "total-count": len(rows)}})


def make_holders_text(newest="2026-09", months=13, scale=1.0, drop=None, total_fudge=0.0):
    year, mon = int(newest[:4]), int(newest[5:])
    labels = []
    for _ in range(months):
        labels.append(f"{year}-{mon:02d}")
        year, mon = (year - 1, 12) if mon == 1 else (year, mon - 1)
    names = ["Japan", "United Kingdom", "China, Mainland", "Belgium", "Cayman Islands", "Canada", "Luxembourg",
             "Ireland", "France", "Taiwan", "Switzerland", "Singapore"]
    lines = ["Table 5: Major Foreign Holders of Treasury Securities", "Billions of dollars", "",
             "Country\t" + "\t".join(labels)]
    sums = [0.0] * months
    for i, name in enumerate(names):
        values = [round((1000 - 60 * i + m) * scale, 1) for m in range(months)]
        sums = [a + b for a, b in zip(sums, values)]
        lines.append(name + "\t" + "\t".join(str(v) for v in values))
    other = [1800.0 * scale] * months
    rows = {
        "All Other": other,
        "Grand Total": [round(a + b + total_fudge, 1) for a, b in zip(sums, other)],
        "Of Which: Foreign Official": [3600.0 * scale] * months,
        "Of Which: Foreign Official Treasury Bills": [300.0 * scale] * months,
    }
    for name, values in rows.items():
        if name != drop:
            lines.append(name + "\t" + "\t".join(str(v) for v in values))
    lines += ["", "Notes:", "The data in this table are collected primarily from U.S.-based custodians."]
    return "\n".join(lines) + "\n"


DAILY = make_daily()
YEARLY = make_yearly()


class Web:
    """Stands in for update_data.http_get, answering each Treasury URL from a dict."""

    def __init__(self, daily=None, yearly=None, holders=None):
        self.answers = {
            ud.DAILY_URL: fiscal_json(DAILY) if daily is None else daily,
            ud.YEARLY_URL: fiscal_json(YEARLY) if yearly is None else yearly,
            ud.HOLDERS_URL: make_holders_text() if holders is None else holders,
        }

    def __call__(self, url):
        for prefix, answer in self.answers.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError("unexpected URL " + url)


DOWN = ud.DataError("could not download (simulated outage)")


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.saved = (ud.DEBT_FILE, ud.HOLDERS_FILE, ud.http_get)
        ud.DEBT_FILE = self.dir / "debt.js"
        ud.HOLDERS_FILE = self.dir / "holders.js"

    def tearDown(self):
        ud.DEBT_FILE, ud.HOLDERS_FILE, ud.http_get = self.saved
        self.tmp.cleanup()

    def run_update(self, web, *args):
        """Run the updater; return (exit code, everything it printed)."""
        ud.http_get = web
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = ud.main(list(args))
        return code, out.getvalue()

    def good_files(self):
        code, _ = self.run_update(Web())
        self.assertEqual(code, 0)
        return ud.DEBT_FILE.read_bytes(), ud.HOLDERS_FILE.read_bytes()

    def assert_untouched(self, debt_bytes, holders_bytes):
        self.assertEqual(ud.DEBT_FILE.read_bytes(), debt_bytes, "debt.js was changed")
        self.assertEqual(ud.HOLDERS_FILE.read_bytes(), holders_bytes, "holders.js was changed")

    def leftovers(self):
        return sorted(p.name for p in self.dir.iterdir() if p.name.endswith(".tmp"))

    # ------------------------------------------------------------------ the normal case

    def test_good_data_writes_two_valid_files(self):
        code, out = self.run_update(Web())
        self.assertEqual(code, 0, out)
        debt = ud.read_saved(ud.DEBT_FILE, ud.DEBT_VAR)
        ud.validate_debt(debt)
        self.assertEqual(len(debt["daily"]), len(DAILY))
        self.assertEqual(debt["daily"][0], ["1993-04-01", 4.2e12, None, None])
        self.assertEqual(len(debt["yearly"]), len(YEARLY))
        holders = ud.read_saved(ud.HOLDERS_FILE, ud.HOLDERS_VAR)
        ud.validate_holders(holders)
        self.assertEqual(holders["months"][0], "2026-09")
        self.assertEqual(self.leftovers(), [])
        for path in (ud.DEBT_FILE, ud.HOLDERS_FILE):
            self.assertEqual(path.stat().st_mode & 0o777, 0o644, f"{path.name} must be readable by a web server")

    def test_unchanged_data_is_not_rewritten(self):
        before = self.good_files()
        code, out = self.run_update(Web())
        self.assertEqual(code, 0)
        self.assertEqual(out.count("already up to date"), 2, out)
        self.assert_untouched(*before)

    def test_newer_data_replaces_old_and_keeps_a_backup(self):
        old_debt, old_holders = self.good_files()
        code, out = self.run_update(Web(daily=fiscal_json(make_daily(days=8320, end_total=40.2e12)),
                                        holders=make_holders_text(newest="2026-10")))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(ud.read_saved(ud.DEBT_FILE, ud.DEBT_VAR)["daily"]), 8320)
        self.assertEqual(ud.read_saved(ud.HOLDERS_FILE, ud.HOLDERS_VAR)["months"][0], "2026-10")
        self.assertEqual((self.dir / "debt.js.bak").read_bytes(), old_debt)
        self.assertEqual((self.dir / "holders.js.bak").read_bytes(), old_holders)

    # ------------------------------------------------------------------ failures must not damage good files

    def assert_rejected(self, web, files=("debt.js", "holders.js")):
        before = self.good_files()
        code, out = self.run_update(web)
        self.assertEqual(code, 1, out)
        for name in files:
            self.assertIn(f"{name}: NOT UPDATED", out)
        self.assertIn("left untouched", out)
        self.assert_untouched(*before)
        self.assertEqual(self.leftovers(), [])
        return out

    def test_outage_leaves_both_files_alone(self):
        self.assert_rejected(Web(daily=DOWN, holders=DOWN))

    def test_one_file_failing_does_not_block_the_other(self):
        self.good_files()
        holders_before = ud.HOLDERS_FILE.read_bytes()
        code, out = self.run_update(Web(daily=fiscal_json(make_daily(days=8320, end_total=40.2e12)), holders=DOWN))
        self.assertEqual(code, 1)
        self.assertIn("debt.js: updated", out)
        self.assertIn("holders.js: NOT UPDATED", out)
        self.assertEqual(ud.HOLDERS_FILE.read_bytes(), holders_before)

    def test_error_page_instead_of_data(self):
        html = "<html><body><h1>503 Service Unavailable</h1></body></html>"
        out = self.assert_rejected(Web(daily=html, holders=html))
        self.assertIn("did not send JSON", out)
        self.assertIn("no 'Country' header", out)

    def test_truncated_json(self):
        self.assert_rejected(Web(daily=fiscal_json(DAILY)[:5000]), files=("debt.js",))

    def test_empty_data_list(self):
        self.assert_rejected(Web(daily=fiscal_json([])), files=("debt.js",))

    def test_too_few_rows(self):
        out = self.assert_rejected(Web(daily=fiscal_json(DAILY[:300])), files=("debt.js",))
        self.assertIn("only 300 daily figures", out)

    def test_row_count_mismatch(self):
        body = json.dumps({"data": DAILY[:-5], "meta": {"total-pages": 1, "total-count": len(DAILY)}})
        self.assert_rejected(Web(daily=body), files=("debt.js",))

    def test_garbage_values(self):
        for field, value in (("tot_pub_debt_out_amt", "n/a"), ("tot_pub_debt_out_amt", "0"),
                             ("tot_pub_debt_out_amt", "-5"), ("record_date", "2026-13-45"),
                             ("tot_pub_debt_out_amt", "9999999999999999")):
            rows = [dict(r) for r in DAILY]
            rows[-10][field] = value
            with self.subTest(field=field, value=value):
                self.assert_rejected(Web(daily=fiscal_json(rows)), files=("debt.js",))

    def test_dates_out_of_order(self):
        rows = [dict(r) for r in DAILY]
        rows[100], rows[101] = rows[101], rows[100]
        self.assert_rejected(Web(daily=fiscal_json(rows)), files=("debt.js",))

    def test_latest_figure_without_breakdown(self):
        rows = [dict(r) for r in DAILY]
        rows[-1]["debt_held_public_amt"] = "null"
        self.assert_rejected(Web(daily=fiscal_json(rows)), files=("debt.js",))

    def test_refuses_to_go_backwards(self):
        out = self.assert_rejected(Web(daily=fiscal_json(DAILY[:-20]), holders=make_holders_text(newest="2026-07")))
        self.assertIn("earlier than the saved ones", out)
        self.assertIn("older than the saved one", out)

    def test_holders_layout_changes(self):
        cases = {
            "missing grand total": make_holders_text(drop="Grand Total"),
            "rows do not add up": make_holders_text(total_fudge=900.0),
            "implausible total": make_holders_text(scale=0.001),
            "empty table": "Country\t2026-09\t2026-08\n\n",
            "non-numeric cell": make_holders_text().replace("1000.0", "n/a", 1),
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assert_rejected(Web(holders=text), files=("holders.js",))

    # ------------------------------------------------------------------ fallbacks

    def test_yearly_failure_reuses_saved_history(self):
        self.good_files()
        for bad in (DOWN, fiscal_json(YEARLY[:50]), "not json"):
            with self.subTest(bad=str(bad)[:20]):
                code, out = self.run_update(Web(daily=fiscal_json(make_daily(days=8320, end_total=40.2e12)),
                                                yearly=bad), "--only", "debt", "--force")
                self.assertEqual(code, 0, out)
                self.assertIn("saved copy was reused", out)
                debt = ud.read_saved(ud.DEBT_FILE, ud.DEBT_VAR)
                ud.validate_debt(debt)
                self.assertEqual(len(debt["yearly"]), len(YEARLY))

    def test_failure_with_nothing_saved_creates_no_file(self):
        code, out = self.run_update(Web(daily=DOWN, yearly=DOWN, holders="garbage"))
        self.assertEqual(code, 1)
        self.assertIn("no usable debt.js", out)
        self.assertIn("no usable holders.js", out)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [])

    def test_corrupt_existing_file_is_replaced_but_never_backed_up(self):
        ud.DEBT_FILE.write_text("window.DEBT_SNAPSHOT = {\"daily\": [[\"1993-04-01\", 4.2e12", encoding="utf-8")
        ud.HOLDERS_FILE.write_text("", encoding="utf-8")
        code, out = self.run_update(Web())
        self.assertEqual(code, 0, out)
        ud.validate_debt(ud.read_saved(ud.DEBT_FILE, ud.DEBT_VAR))
        ud.validate_holders(ud.read_saved(ud.HOLDERS_FILE, ud.HOLDERS_VAR))
        self.assertFalse((self.dir / "debt.js.bak").exists(), "a corrupt file was kept as the backup")
        self.assertFalse((self.dir / "holders.js.bak").exists(), "an empty file was kept as the backup")

    def test_a_bug_in_one_update_does_not_stop_the_other(self):
        before_debt, _ = self.good_files()

        def explode(url):
            if url.startswith(ud.HOLDERS_URL):
                return make_holders_text(newest="2026-10")
            raise RuntimeError("simulated bug")
        code, out = self.run_update(explode)
        self.assertEqual(code, 1)
        self.assertIn("unexpected error", out)
        self.assertIn("holders.js: updated", out)
        self.assertEqual(ud.DEBT_FILE.read_bytes(), before_debt)

    # ------------------------------------------------------------------ --quiet (scheduled jobs)

    def test_quiet_success_prints_nothing(self):
        code, out = self.run_update(Web(), "--quiet")           # first run: files are written
        self.assertEqual((code, out), (0, ""))
        ud.validate_debt(ud.read_saved(ud.DEBT_FILE, ud.DEBT_VAR))
        code, out = self.run_update(Web(), "-q")                # nothing new
        self.assertEqual((code, out), (0, ""))
        code, out = self.run_update(Web(yearly=DOWN), "-q", "--force")  # a fallback was used, still a success
        self.assertEqual((code, out), (0, ""))
        code, out = self.run_update(Web(), "--check", "--quiet")
        self.assertEqual((code, out), (0, ""))

    def test_quiet_failure_still_reports(self):
        self.good_files()
        code, out = self.run_update(Web(holders=DOWN), "--quiet")
        self.assertEqual(code, 1)
        self.assertEqual(len(out.strip().splitlines()), 1, out)   # one line, for the one file that failed
        self.assertIn("holders.js: NOT UPDATED", out)
        ud.HOLDERS_FILE.write_text("", encoding="utf-8")
        code, out = self.run_update(Web(), "--check", "--quiet")
        self.assertEqual(code, 1)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertIn("holders.js: PROBLEM", out)

    # ------------------------------------------------------------------ --check

    def test_check_reports_good_and_bad_files(self):
        self.good_files()
        code, out = self.run_update(Web(daily=DOWN), "--check")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count(": OK"), 2)
        for damage in ("", "window.DEBT_SNAPSHOT = {};\n", ud.DEBT_FILE.read_text()[:20000]):
            ud.DEBT_FILE.write_text(damage, encoding="utf-8")
            with self.subTest(size=len(damage)):
                code, out = self.run_update(Web(), "--check")
                self.assertEqual(code, 1)
                self.assertIn("debt.js: PROBLEM", out)
                self.assertIn("holders.js: OK", out)
        ud.DEBT_FILE.unlink()
        code, out = self.run_update(Web(), "--check")
        self.assertEqual(code, 1)
        self.assertIn("does not exist", out)


class RetryTest(unittest.TestCase):
    def test_download_retries_with_doubling_delay(self):
        waits, calls = [], []

        def failing_open(request, timeout):
            calls.append(timeout)
            raise OSError("connection reset")
        saved = (ud.urllib.request.urlopen, ud.time.sleep)
        ud.urllib.request.urlopen, ud.time.sleep = failing_open, waits.append
        try:
            with self.assertRaises(ud.DataError) as caught:
                ud.http_get("https://example.invalid/data")
        finally:
            ud.urllib.request.urlopen, ud.time.sleep = saved
        self.assertEqual(len(calls), ud.HTTP_ATTEMPTS)
        self.assertEqual(waits, [2, 4])
        self.assertIn("after 3 tries", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
