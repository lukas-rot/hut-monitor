#!/usr/bin/env python3
"""
Saturday availability monitor for hut-reservation.org.

Strategy:
- Open the booking wizard in headless Chromium via Playwright.
- Intercept the XHR/fetch the SPA makes to fetch availability data
  (this site's calendar is API-driven, so the JSON comes through
  network responses, not the rendered DOM).
- Parse the JSON, find Saturdays within the horizon window that
  have free beds, diff against last run's state, email anything new.

Why intercept the API response instead of scraping the DOM:
- Robust to UI changes (CSS classes, ARIA labels, etc.)
- Single network call vs walking the calendar month-by-month
- Same data the wizard uses, so no false negatives

Env vars (set as GitHub Actions secrets):
  HUT_ID            default "150"
  HORIZON_DAYS      default "365"
  SMTP_HOST         e.g. "smtp.gmail.com"
  SMTP_PORT         default "587"
  SMTP_USER         your gmail address
  SMTP_PASS         gmail app password (NOT your account password)
  EMAIL_TO          where alerts go
  EMAIL_FROM        defaults to SMTP_USER
  MIN_BEDS          default "1" - only alert if >= this many beds
"""
import json
import os
import smtplib
import sys
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Ensure the stdout fallback (when SMTP env vars are unset) doesn't crash
# on a Windows console that defaults to cp1252 and can't encode the emoji
# in the alert subject.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HUT_ID = os.environ.get("HUT_ID", "150")
HUT_URL = f"https://www.hut-reservation.org/reservation/book-hut/{HUT_ID}/wizard"
HORIZON_DAYS = int(os.environ.get("HORIZON_DAYS", "365"))
MIN_BEDS = int(os.environ.get("MIN_BEDS", "1"))

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER or "")
EMAIL_TO = os.environ.get("EMAIL_TO")

STATE_FILE = Path("state.json")


def log(*args):
    print("[monitor]", *args, flush=True)


def load_state() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        return set()


def save_state(s: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(s), indent=2))


def send_email(subject: str, body: str) -> None:
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, EMAIL_TO]):
        log("SMTP env vars not fully set; printing instead:")
        log(subject)
        log(body)
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    log(f"Email sent to {EMAIL_TO}: {subject}")


def extract_saturdays_from_payload(payload) -> dict[str, int]:
    """
    Walk an arbitrary JSON payload returned by the wizard and pull
    out anything that looks like {date: YYYY-MM-DD, freeBeds: N}.

    The hut-reservation.org API has been observed to return shapes
    like:
        [{"date":"2026-06-20","freeBedsPerCategory":{"M":12,"SL":4},
          "hutStatus":"SERVICED", "totalSleepingPlaces":100, ...}, ...]
    or
        [{"date":"2026-06-20","freeBeds":16,...}, ...]

    This walker is lenient: it finds dicts with a "date" key and a
    numeric "freeBeds*" key (or sums dict-of-ints under one).
    """
    out: dict[str, int] = {}

    def numeric_free_beds(d: dict) -> int | None:
        for key in ("freeBeds", "totalFreeBeds", "free", "available"):
            v = d.get(key)
            if isinstance(v, int):
                return v
        # dict-of-categories fallback
        for key, v in d.items():
            if "free" in key.lower() and isinstance(v, dict):
                total = sum(int(x) for x in v.values() if isinstance(x, (int, float)))
                return total
        return None

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            date_str = node.get("date") or node.get("day")
            if isinstance(date_str, str) and len(date_str) >= 10:
                try:
                    d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
                    if d.weekday() == 5:  # Saturday
                        beds = numeric_free_beds(node)
                        if beds is not None:
                            out[d.isoformat()] = max(out.get(d.isoformat(), 0), beds)
                except ValueError:
                    pass
            for v in node.values():
                walk(v)

    walk(payload)
    return out


def find_available_saturdays() -> dict[str, int]:
    """Returns {ISO date string: free beds count} for Saturdays w/ beds >= MIN_BEDS."""
    today = date.today()
    horizon_end = today + timedelta(days=HORIZON_DAYS)
    log(f"Scanning hut {HUT_ID} from {today} to {horizon_end}")

    captured: dict[str, int] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="de-DE",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        def on_response(resp):
            url = resp.url
            if "hut-reservation.org" not in url:
                return
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype:
                return
            try:
                data = resp.json()
            except Exception:
                return
            found = extract_saturdays_from_payload(data)
            if found:
                log(f"  captured {len(found)} Saturdays from {url[:120]}")
                for k, v in found.items():
                    captured[k] = max(captured.get(k, 0), v)

        page.on("response", on_response)

        try:
            page.goto(HUT_URL, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightTimeout:
            log("page load timed out, continuing with whatever we got")

        # The wizard only fires getHutAvailability when the date picker
        # opens, so click the toggle. The response covers ~2 years in one
        # payload, so no pagination loop is needed.
        try:
            page.locator("mat-datepicker-toggle button").first.click(timeout=10000)
        except Exception as e:
            log(f"could not open date picker: {e}")

        page.wait_for_timeout(5000)

        browser.close()

    # Filter to horizon and MIN_BEDS
    result = {
        d: beds
        for d, beds in captured.items()
        if beds >= MIN_BEDS
        and today.isoformat() <= d <= horizon_end.isoformat()
    }
    log(f"Found {len(result)} Saturdays with >= {MIN_BEDS} bed(s) available")
    return result


def main():
    previous_dates = load_state()
    current = find_available_saturdays()
    current_dates = set(current.keys())

    new_openings = current_dates - previous_dates
    closed = previous_dates - current_dates

    if new_openings:
        lines = [
            f"  {d}  ({current[d]} bed(s))"
            for d in sorted(new_openings)
        ]
        body = (
            f"New Saturday availability detected at hut {HUT_ID}:\n\n"
            + "\n".join(lines)
            + f"\n\nBook: {HUT_URL}\n"
            + f"\nCurrently visible Saturday openings: {len(current_dates)}"
        )
        subject = (
            f"🏔️ {len(new_openings)} new Saturday opening"
            f"{'s' if len(new_openings) != 1 else ''} - hut {HUT_ID}"
        )
        send_email(subject, body)
    else:
        log("No new openings.")

    if closed:
        log(f"{len(closed)} previously-open dates are no longer available: "
            f"{sorted(closed)}")

    save_state(current_dates)
    log("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        # Optionally email yourself on hard failure so silent breakage
        # doesn't go unnoticed.
        try:
            send_email(
                f"[hut-monitor] ERROR for hut {HUT_ID}",
                f"Run failed:\n\n{e!r}\n\nCheck the GitHub Actions logs.",
            )
        except Exception:
            pass
        sys.exit(1)
