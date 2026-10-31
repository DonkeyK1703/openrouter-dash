# Break-even throughput

For the top 5 open-weight models on OpenRouter, per tier (standard / priority / flex / batch): how many
output tokens one GPU must produce per hour to break even at today's market prices. Enter GPU $/hr on the page.

- Local: `python3 app.py` → http://127.0.0.1:8787 (key in `.env` as `OPENROUTER_API_KEY=...`)
- Hosted: `.github/workflows/poll.yml` runs `app.py --once` every 30 min and deploys `public/` to GitHub Pages.
  Needs repo secret `OPENROUTER_API_KEY` and Pages source set to "GitHub Actions".
- Traces: every API call is a line in `traces.jsonl` (last 48h). Per-provider snapshots are kept as Actions artifacts for 90 days.
- Test: `python3 test_app.py`

Data: Source: OpenRouter (openrouter.ai/rankings). Licensed under CC BY 4.0.
