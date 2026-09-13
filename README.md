# Official company news monitor

Checks the seven official company newsrooms in `sources.json`, detects links not seen on a previous run, and prints a compact report. It stores only public article URLs and titles.

## Run locally

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python news_monitor.py --bootstrap
python news_monitor.py
```

Use `python news_monitor.py --format json` if another tool will consume the results. The state file is intentionally ignored by Git for local use.

## Daily automation

The included GitHub Actions workflow runs daily at 08:00 UTC. Push this folder to a GitHub repository and enable Actions; it commits `state.json` and `latest-news.md` after every check. Change the cron expression in `.github/workflows/daily-news.yml` to adjust the time.

## Reliability

The sources are official newsrooms, including the missing Adyen and Checkout.com URLs. RSS is not required: this monitor reads each newsroom directly. Validate its first output; page redesigns may require a source-specific extractor.
