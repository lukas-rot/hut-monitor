# Hut Saturday Monitor

Polls `hut-reservation.org` hourly via GitHub Actions and emails you whenever a Saturday opens up at hut 150 (or whichever hut you configure) within the next 365 days.

## How it works

1. Headless Chromium opens the booking wizard.
2. The SPA fires its normal availability API calls. The script intercepts those JSON responses (no need to know the exact endpoint or selectors).
3. Saturdays with available beds are diffed against the last run's state file (`state.json`, committed back to the repo).
4. New openings → email via SMTP.

Runs every hour. Uses ~3 GitHub Actions minutes per run → ~2,200 min/month on private repos. **Make the repo public** to get unlimited Actions minutes, or accept the cost (~$3-4/mo over the free tier on private).

## One-shot setup with Claude Code

Paste this into Claude Code (`npx @anthropic-ai/claude-code` from this directory):

```
Set up this hut-monitor as a new private GitHub repo under my account, push everything,
then configure the following Actions secrets using `gh secret set`:

  SMTP_HOST = smtp.gmail.com
  SMTP_PORT = 587
  SMTP_USER = <ask me for my Gmail address>
  SMTP_PASS = <ask me for my Gmail app password - link me to https://myaccount.google.com/apppasswords first>
  EMAIL_TO  = <ask me which address should receive alerts>

Then trigger the workflow once with `gh workflow run check.yml`, wait for it to finish,
and show me the run log. If it failed, diagnose. If it succeeded, tell me how many
Saturday openings it currently sees and confirm I should expect emails only on changes.
```

That's it. Claude Code handles repo creation, push, secrets, first run, debugging.

## Manual setup (if you'd rather)

1. Create a new private GitHub repo.
2. Drop all these files in, commit, push.
3. Create a Gmail app password: https://myaccount.google.com/apppasswords (you need 2FA enabled on the account first).
4. In your repo: Settings → Secrets and variables → Actions → New repository secret. Add:
   - `SMTP_HOST` = `smtp.gmail.com`
   - `SMTP_PORT` = `587`
   - `SMTP_USER` = your Gmail address
   - `SMTP_PASS` = the app password (16 chars, no spaces)
   - `EMAIL_TO` = where alerts should go
5. Actions tab → "Hut Saturday Monitor" → "Run workflow" to test.

## Configuration

Edit `.github/workflows/check.yml` to change:
- `HUT_ID` — currently `150`. Change to any other hut on hut-reservation.org.
- `HORIZON_DAYS` — how far ahead to scan. Default 365.
- `MIN_BEDS` — only alert when this many beds are free on a Saturday. Default 1.
- The cron schedule — currently hourly. For tighter polling around booking-release windows (DAV typically releases inventory 30 days out at midnight), add a second cron line.

## Caveats

- The script intercepts whatever JSON the wizard fetches. If the API shape changes, the lenient parser in `extract_saturdays_from_payload` may need a tweak. The first run log will show how many Saturdays were captured — if zero, run `monitor.py` locally and inspect the network responses.
- Polling is hourly. Don't crank it lower than every 15 minutes. The site has rate limiting and a ToS that frowns on automated access. Personal use only.
- Cancellations often happen 1-3 days before the date. Inventory releases happen 30 days out for many DAV huts, usually at midnight Munich time. Adjust cron accordingly if you want to catch release moments fast.
