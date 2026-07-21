#!/usr/bin/env python3
"""
check_slots.py -- GitHub Actions edition
========================================
A trimmed, cloud-friendly version of the samordningsnummer slot poller. It runs
ONE headless check of the Migrationsverket "Boka tid för besök" booking page for
the Embassy of Sweden in London and, if appointment slots for **ansöka om
samordningsnummer** are available, sends a phone push via ntfy.sh.

Designed to be run on a schedule by GitHub Actions (see
.github/workflows/poll.yml). It does NOT book anything and enters no personal
data -- it only watches and alerts. When you get the ping, open the booking page
on any device and finish the booking yourself.

    Booking page:
      https://www.migrationsverket.se/ansokanbokning/valjtyp?0&enhet=U0586&sprak=sv&callback=https:/www.swedenabroad.se

PHONE PUSH: set the NTFY_TOPIC environment variable (a secret, unguessable
string) and subscribe to that same topic in the ntfy app (https://ntfy.sh) on
your phone. In GitHub Actions this is provided as a repository secret.

    NTFY_TOPIC=my-secret-samordningsnummer-topic-8f3k python check_slots.py

Exit code is always 0 (a "no slots" result is not a failure), so the scheduled
job shows green whether or not slots were found.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #

START_URL = (
    "https://www.migrationsverket.se/ansokanbokning/valjtyp"
    "?0&enhet=U0586&sprak=sv&callback=https:/www.swedenabroad.se"
)

# "Anledning till besöket" -> matched by visible label (survives renumbering).
REASON_LABEL = "samordningsnummer"
REASON_VALUE = "19"  # fallback if the label match fails

# "Antal personer": 1 person = option value "0" (values are offset by one).
NUM_PEOPLE = 1
ANTAL_VALUE = str(NUM_PEOPLE - 1)

# Result page shows this when nothing is bookable.
NO_SLOTS_MARKER = "det finns inga lediga tider"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------- #
# Notification
# --------------------------------------------------------------------------- #

def notify(
    title: str,
    message: str,
    *,
    topic_env: str = "NTFY_TOPIC",
    priority: str = "high",
    tags: str = "calendar",
) -> None:
    """Print to the Actions log + push to ntfy.sh if the chosen topic is set.

    ``topic_env`` selects the channel:
      * ``NTFY_TOPIC``       -- main channel: real slot alerts (high priority).
      * ``NTFY_DEBUG_TOPIC`` -- quiet health channel: inconclusive runs / errors,
        so bot-health noise never lands on the real-alert channel. Optional --
        if the debug topic isn't set, these are logged only, no push.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}\n[{stamp}] {title}\n{message}\n{'='*60}\n", flush=True)
    topic = os.environ.get(topic_env)
    if not topic:
        print(f"({topic_env} not set -- no push sent)", flush=True)
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": tags,
                "Click": START_URL,
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"(ntfy push sent to {topic_env})", flush=True)
    except Exception as e:
        print(f"(ntfy push failed: {e})", flush=True)


def _step(msg: str) -> None:
    print(f"    [{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Start-page helpers
# --------------------------------------------------------------------------- #

def _reason_option_value(page) -> str | None:
    """Value of the reason option whose text contains REASON_LABEL."""
    try:
        opts = page.locator("#viseringstyp option")
        n = opts.count()
        if n == 0:
            return None
        for i in range(n):
            o = opts.nth(i)
            text = (o.text_content() or "").strip().lower()
            val = o.get_attribute("value") or ""
            if val and REASON_LABEL.lower() in text:
                return val
    except Exception:
        pass
    return REASON_VALUE


def _select_reason(page) -> bool:
    val = _reason_option_value(page)
    if not val:
        return False
    try:
        page.locator("#viseringstyp").select_option(val)
        return True
    except Exception as e:
        print(f"  ! reason select_option({val!r}) failed: {e}", flush=True)
        return False


def _fill_start_page(page) -> bool:
    """Fill the start page and click 'Fortsätt'. Returns True on success."""
    _step("waiting for reason dropdown (#viseringstyp)...")
    try:
        page.locator("#viseringstyp").wait_for(state="visible", timeout=20_000)
    except Exception as e:
        print(f"  ! reason dropdown never appeared: {e}", flush=True)
        return False

    # Wait for Wicket's JS so the reason's onchange AJAX actually fires.
    _step("waiting for Wicket JS to initialise...")
    try:
        page.wait_for_function(
            "() => typeof window.Wicket !== 'undefined'", timeout=20_000
        )
    except Exception:
        _step("(Wicket global not detected; continuing anyway)")

    _step("selecting samordningsnummer reason...")
    if not _select_reason(page):
        print("  ! could not select the samordningsnummer reason.", flush=True)
        return False

    # Retry until the AJAX-injected #antalpersoner appears.
    appeared = False
    for attempt in range(1, 6):
        try:
            page.wait_for_selector("#antalpersoner", timeout=6_000)
            appeared = True
            _step(f"Antal personer appeared (attempt {attempt}).")
            break
        except Exception:
            _step(f"Antal personer not up yet (attempt {attempt}); re-firing...")
            try:
                _select_reason(page)
                page.eval_on_selector(
                    "#viseringstyp",
                    "el => el.dispatchEvent(new Event('change', {bubbles: true}))",
                )
            except Exception:
                pass
            time.sleep(1.0)

    if not appeared:
        print(
            "  ! 'Antal personer' never appeared -- the Wicket AJAX did not "
            "complete. Treating as an inconclusive run.",
            flush=True,
        )
        return False

    # Converge on reason + antal + consent (Wicket resets them during re-renders).
    def _val(sel: str) -> str:
        try:
            return page.locator(sel).input_value()
        except Exception:
            return ""

    def _checkbox():
        cb = page.locator("input[name='control.panel:control.checkbox']").first
        if cb.count() == 0:
            cb = page.locator("input[type=checkbox]").first
        return cb

    want_reason = _reason_option_value(page) or REASON_VALUE
    ok = False
    for _ in range(6):
        reason_ok = _val("#viseringstyp") == want_reason
        antal_ok = _val("#antalpersoner") == ANTAL_VALUE
        try:
            cb = _checkbox()
            cb_ok = cb.count() > 0 and cb.is_checked()
        except Exception:
            cb_ok = False

        if reason_ok and antal_ok and cb_ok:
            ok = True
            break
        if not reason_ok:
            try:
                _select_reason(page)
                page.wait_for_selector("#antalpersoner", timeout=15_000)
            except Exception:
                pass
            time.sleep(0.4)
            continue
        if not antal_ok:
            try:
                page.locator("#antalpersoner").select_option(ANTAL_VALUE)
            except Exception:
                pass
            time.sleep(0.4)
            continue
        if not cb_ok:
            try:
                cb = _checkbox()
                if cb.count() and not cb.is_checked():
                    cb.check()
            except Exception:
                pass
            time.sleep(0.2)

    if not ok:
        print("  ! could not lock in reason / antal / consent.", flush=True)
        return False

    print(f"  start page set: reason={_val('#viseringstyp')!r}, "
          f"antal personer={NUM_PEOPLE}, consent ticked.", flush=True)

    _step("clicking Fortsätt...")
    try:
        cont = page.locator("input[name='fortsatt']").first
        if cont.count() == 0:
            cont = page.locator("input[value='Fortsätt'], input[value='Continue']").first
        if cont.count() == 0:
            print("  ! could not find the 'Fortsätt' button.", flush=True)
            return False
        cont.click()
    except Exception as e:
        print(f"  ! could not click 'Fortsätt': {e}", flush=True)
        return False

    try:
        page.wait_for_selector(
            "#fnamn, input[name='spara'], li.feedbackPanelERROR", timeout=20_000
        )
        _step("result page rendered.")
    except Exception:
        _step("result page markers not seen within timeout.")
    time.sleep(0.5)
    return True


# --------------------------------------------------------------------------- #
# Result-page helpers
# --------------------------------------------------------------------------- #

def _on_result_page(page) -> bool:
    try:
        return page.locator(
            "#fnamn, input[name='spara'], [data-id='feedback-wrapper']"
        ).count() > 0
    except Exception:
        return False


def _result_says_no_slots(page) -> bool:
    try:
        if page.locator("li.feedbackPanelERROR", has_text="lediga tider").count() > 0:
            return True
    except Exception:
        pass
    try:
        body = (page.locator("body").inner_text() or "").lower()
    except Exception:
        body = ""
    return NO_SLOTS_MARKER in body


def _find_free_slots(page) -> list[dict]:
    """Positive detection: bookable slots are FullCalendar events with the
    ``ledig`` ("available") class, e.g.

        <a class="fc-time-grid-event fc-event fc-start fc-end ledig" ...>
          <div class="fc-content">
            <div class="fc-time"><span>15:15 - 15:30</span></div>
            <div class="fc-title">1 lediga</div>
          </div>
        </a>

    Events are CSS-positioned (not nested inside a day cell), so the slot's date
    is recovered by matching the event's horizontal centre to the day-header
    column whose bounds contain it (each header carries data-date="YYYY-MM-DD").

    Returns a list of {"key", "date", "time", "label"} dicts. "key" is a stable
    date+time identity used for de-duplication across checks.
    """
    try:
        raw = page.eval_on_selector_all(
            "a.fc-event.ledig",
            """els => els.map(el => {
                const time  = ((el.querySelector('.fc-time') || {}).textContent || '').trim();
                const label = ((el.querySelector('.fc-title') || {}).textContent || '').trim();
                const r = el.getBoundingClientRect();
                const cx = r.left + r.width / 2;
                let date = '';
                document.querySelectorAll(
                    'th.fc-day-header[data-date], td.fc-day[data-date]'
                ).forEach(h => {
                    const hr = h.getBoundingClientRect();
                    if (cx >= hr.left && cx <= hr.right) date = h.getAttribute('data-date');
                });
                return {date, time, label};
            })""",
        )
    except Exception:
        raw = []

    slots: list[dict] = []
    for s in raw or []:
        date = (s.get("date") or "").strip()
        t = (s.get("time") or "").strip()
        slots.append({
            "key": f"{date} {t}".strip(),
            "date": date,
            "time": t,
            "label": (s.get("label") or "").strip(),
        })
    return slots


# --------------------------------------------------------------------------- #
# The check
# --------------------------------------------------------------------------- #

def check_availability() -> dict:
    """Returns {"ok", "available", "detail", "error", "slots"}."""
    result = {"ok": False, "available": False, "detail": "", "error": None,
              "slots": []}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=USER_AGENT, locale="sv-SE")
        page = ctx.new_page()
        page.set_default_timeout(20_000)

        try:
            _step("opening start page...")
            page.goto(START_URL, wait_until="domcontentloaded")
            _step("start page DOM loaded.")

            if not _fill_start_page(page):
                result["error"] = "Could not complete the start page."
                return _finish(browser, result)

            if not _on_result_page(page):
                result["error"] = "Did not reach the 'Boka tid' result page."
                return _finish(browser, result)

            slots = _find_free_slots(page)
            if slots:
                # Positive signal: at least one bookable calendar event.
                result["ok"] = True
                result["available"] = True
                result["slots"] = slots
                lines = [_slot_line(s) for s in slots]
                result["detail"] = (
                    f"{len(slots)} bookable slot(s) found:\n  - "
                    + "\n  - ".join(lines)
                )
            elif _result_says_no_slots(page):
                result["ok"] = True
                result["available"] = False
                result["detail"] = "No free times right now."
            else:
                # No bookable events AND no explicit 'no slots' banner. Ambiguous
                # (site hiccup / layout change) -- do NOT alert, to avoid false
                # alarms. Logged as inconclusive so it's visible in the run log.
                result["ok"] = True
                result["available"] = False
                result["detail"] = (
                    "Inconclusive: no bookable calendar events detected and no "
                    "'no free times' banner either."
                )

        except PWTimeout as e:
            result["error"] = f"Timeout: {e}"
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"

        return _finish(browser, result)


def _finish(browser, result):
    try:
        browser.close()
    except Exception:
        pass
    return result


# --------------------------------------------------------------------------- #
# De-duplication state
# --------------------------------------------------------------------------- #
#
# Only alert for slots we haven't already alerted on. State is a JSON list of
# slot keys from the *previous* check. It lives on the runner's temp dir, so it
# survives across the ~5-min checks WITHIN one self-loop run (same runner), and
# resets on the next hourly run (fresh runner) -- so a still-open slot re-pings
# at most once per hour. Storing only the previous check's keys (not an
# ever-growing history) means a slot that disappears and reappears correctly
# re-alerts.

STATE_FILE = os.environ.get(
    "STATE_FILE",
    os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "seen_slots.json"),
)


def _slot_line(s: dict) -> str:
    base = f"{s['date']} {s['time']}".strip()
    return f"{base} ({s['label']})" if s.get("label") else base


def _load_seen() -> set[str]:
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_seen(keys) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(sorted(keys), f)
    except Exception as e:
        print(f"  (could not write state file {STATE_FILE}: {e})", flush=True)


def main() -> int:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] checking availability...", flush=True)
    res = check_availability()

    if res["error"]:
        # Site hiccup / exception. State is left untouched (we don't know the
        # true slot set), so recovery doesn't trigger spurious re-alerts.
        print(f"  ! error: {res['error']}", flush=True)
        notify(
            "watcher: run error",
            f"The check errored (not a slot signal): {res['error']}",
            topic_env="NTFY_DEBUG_TOPIC",
            priority="low",
            tags="warning",
        )
        return 0

    if res["available"]:
        current = {s["key"] for s in res["slots"]}
        seen = _load_seen()
        new = [s for s in res["slots"] if s["key"] not in seen]
        _save_seen(current)  # remember this check's slots as the new baseline

        if new:
            lines = [_slot_line(s) for s in new]
            notify(
                "Samordningsnummer (London) -- new appointment slot!",
                f"{len(new)} new slot(s):\n  - " + "\n  - ".join(lines)
                + f"\n\nBook here: {START_URL}",
            )
        else:
            print(
                f"  slots available but all already alerted: "
                f"{sorted(current)}",
                flush=True,
            )
    elif "Inconclusive" in res["detail"]:
        # Neither slots nor the 'no free times' banner -- possible layout change.
        # Leave state untouched; quiet ping so a blind bot is noticeable.
        print(f"  ? INCONCLUSIVE: {res['detail']}", flush=True)
        notify(
            "watcher: inconclusive",
            f"{res['detail']} The page layout may have changed -- worth a look.",
            topic_env="NTFY_DEBUG_TOPIC",
            priority="low",
            tags="warning",
        )
    else:
        # Confirmed no slots: clear the baseline so reappearing slots re-alert.
        _save_seen(set())
        print(f"  no slots: {res['detail']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
