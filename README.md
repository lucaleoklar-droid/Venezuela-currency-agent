# Venezuela Currency Agent

Tracks the Venezuelan **BCV official** vs **parallel** USD exchange rate, scraping
every 30 minutes and publishing the data here automatically.

## Live chart

![BCV vs parallel rate — last 30 days](data/chart.png)

Regenerated every scrape. Direct link:
https://raw.githubusercontent.com/lucaleoklar-droid/Venezuela-currency-agent/master/data/chart.png

## Data tables

GitHub renders these as sortable, searchable tables — just click:

| View | File | Contents |
|------|------|----------|
| **Right now** | [data/current.json](data/current.json) | Latest reading (BCV, parallel, spread) |
| **This week** | [data/recent.csv](data/recent.csv) | Last 7 days of raw 30-min scrapes |
| **Daily trend** | [data/daily.csv](data/daily.csv) | One row per day — avg / min / max |
| **Full history** | [data/archive/](data/archive/) | Complete raw data, one CSV per month |

Column definitions: [data/README.md](data/README.md).

## How it works

- **Sources:** parallel rate from `ve.dolarapi.com` (fallback `dolartoday.com`),
  official rate from `bcv.org.ve`.
- **Storage:** SQLite on the deployed worker; a daily snapshot is committed to
  [`backups/`](backups/).
- **Alerts & forecasts:** a Telegram bot sends daily briefs and momentum/spread
  alerts; a forecaster scores 24h-ahead spread movement (Brier-scored).
- **Spread** = `(parallel - bcv) / bcv x 100`. All timestamps are UTC.

Data is informational only and reflects scraped public sources.
