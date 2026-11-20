# US National Debt Counter

A single web page that shows a live estimate of the US national debt, built from US Treasury data. It needs no server-side code and no build step.

## What the page shows

- **A ticking counter.** Treasury publishes one official figure per business day. The page starts from the latest one and adds the average growth of the past year, so the number moves between official updates. It is an estimate and is labeled as one.
- **Your share.** A switch divides the debt by the US population, with an option for household size.
- **A debt chart** covering the past 10, 20, 30, 40 or 50 years, or everything since 1790. Presidential terms are shaded, with the change in debt during each term. Hover or tap for the date, amount and president.
- **Who holds the debt.** The split between foreign holders, US investors and the Federal Reserve, and US government accounts, followed by a country-by-country breakdown.

## Where the data comes from

| Data | Source | How the page gets it |
|---|---|---|
| Daily debt since April 1993 | [Debt to the Penny](https://fiscaldata.treasury.gov/datasets/debt-to-the-penny/) | Saved copy in `debt.js`, then the latest figures live from Treasury's API |
| Yearly debt, 1790–1992 | [Historical Debt Outstanding](https://fiscaldata.treasury.gov/datasets/historical-debt-outstanding/) | Saved copy in `debt.js` |
| Foreign holders by country | [Treasury International Capital, Major Foreign Holders](https://home.treasury.gov/data/treasury-international-capital-tic-system) | Saved copy in `holders.js` only |

The foreign-holders table cannot be fetched from a web page, because Treasury's server does not allow it. That is why it is only ever read from the saved file.

## Files

| File | Purpose |
|---|---|
| `index.html` | The page: markup, styles and script in one file |
| `debt.js` | Saved debt figures, written by `update_data.py` |
| `holders.js` | Saved foreign-holders table, written by `update_data.py` |
| `update_data.py` | Refreshes the two data files |
| `test_update_data.py` | Tests for the update script |

## Viewing it

Open `index.html` in a browser, or serve the folder:

```bash
python3 -m http.server 8000
```

Then visit <http://localhost:8000>.

To host it, upload `index.html`, `debt.js` and `holders.js` to any static web host. The page loads its fonts from Google Fonts, so they need an internet connection to appear.

## How the page loads data

1. It draws immediately from the saved figures in `debt.js`.
2. It asks Treasury for the most recent 45 days plus anything newer, and merges that in.
3. It checks again every hour while the page stays open.

If Treasury does not start answering within 15 seconds, or the download takes longer than 60 seconds, the request is abandoned. The page then retries after 2 seconds, doubling the wait each time up to 5 minutes. Meanwhile it keeps showing the figures it has and says so.

The page checks both data files before using them. A missing or damaged `debt.js` is ignored and everything is fetched live. A missing or damaged `holders.js` hides the "Who holds the debt" section. Neither stops the rest of the page from working.

## Updating the saved data

```bash
python3 update_data.py            # refresh both files
python3 update_data.py --check    # only verify the files already on disk
python3 update_data.py --quiet    # print nothing unless something went wrong
```

The script uses only the Python standard library. It has been run on Python 3.9 and 3.13.

Treasury posts a new foreign-holders table around the 18th of each month, covering the month that ended about six weeks earlier. Running the script daily is fine: it only rewrites a file when the data has changed.

For a scheduled job, use `--quiet`. A successful run is silent and exits 0. A failure prints one line per affected file and exits 1.

```
0 23 * * * /usr/bin/python3 /path/to/update_data.py --quiet
```

The job rewrites `debt.js` and `holders.js` in place, so on a server those files will differ from the copies in this repository.

### How the script protects the data files

A bad data file would spoil the page for every visitor, so:

- Each file is handled on its own. A failure with one never touches the other.
- Downloads are retried, and new data must pass validation before anything is written.
- New data is refused if it ends earlier, or holds fewer figures, than what is already saved.
- A file is written under a temporary name, read back and validated, then swapped into place in one step.
- The previous good file is kept as `debt.js.bak` or `holders.js.bak`.
- On any failure the existing file is left exactly as it was.

## Tests

```bash
python3 -m unittest test_update_data
```

The tests never touch the network or the real data files. They run the update script against good, empty, truncated and wrong responses in a temporary folder.

## Things to know

- **The counter is an estimate.** The official figure, with its date, is shown beside it.
- **The population is a fixed number.** "Your share" divides by about 343 million, set as `US_POPULATION` in `index.html`. It is an approximation and does not update itself.
- **"Your share" for past years uses today's population,** so earlier years understate what each person's share was at the time.
- **Before April 1993 there is one figure per year,** so that part of the chart is smoother, and per-term changes there are estimated between yearly figures.
- **Foreign holdings are recorded where the securities are held in custody,** which is not always who owns them. Financial centers such as Belgium, Luxembourg, Ireland and the Cayman Islands look larger than their own investors' holdings.
