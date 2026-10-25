#!/usr/bin/env python3
"""
check_slots.py -- passport (Ventus) edition
===========================================
Runs ONE headless check of the Ventus booking system for the Embassy of Sweden
in London ("Passansökan") and, if appointment slots are available, sends a phone
push via ntfy.sh.

Designed to be run on a schedule by GitHub Actions (see
.github/workflows/passport.yml). It does NOT book anything and enters no personal
data -- it only ticks the consent boxes needed to reach the timetable, presses
"Första lediga tid", and reads the result. When you get the ping, open the
booking page on any device and finish the booking yourself.

    Booking page:
      https://ventus.enalog.se/Booking/Booking/Index/UDDLondon

Flow: Start -> Avtal (consent + antal personer) -> Ärendekategorier
(Passansökan) -> Villkorlig avtalssida (consent) -> Välj tid.

PHONE PUSH: set the NTFY_TOPIC environment variable (a secret, unguessable
string) and subscribe to that same topic in the ntfy app (https://ntfy.sh) on
your phone. In GitHub Actions this is provided as a repository secret.

    NTFY_TOPIC=my-secret-passport-topic-8f3k python check_slots.py

Exit code is always 0 (a "no slots" result is not a failure), so the scheduled
job shows green whether or not slots were found.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #

START_URL = "https://ventus.enalog.se/Booking/Booking/Index/UDDLondon"

# "Ärende" radio -> matched by visible label (survives id changes).
SERVICE_LABEL = "Passansökan"

# Weeks to scan, starting at the week "Första lediga tid" jumps to. Each extra
# week is one "Nästa vecka" page load.
WEEKS_TO_SCAN = int(os.environ.get("WEEKS_TO_SCAN", "8"))

# Timetable shows this when nothing is bookable.
NO_SLOTS_MARKER = "inga lediga tider kunde hittas"

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
) -> bool:
    """Print to the Actions log + push to ntfy.sh if the chosen topic is set.

    ``topic_env`` selects the channel:
      * ``NTFY_TOPIC``       -- main channel: real slot alerts (high priority).
      * ``NTFY_DEBUG_TOPIC`` -- quiet health channel: inconclusive runs / errors.
        Optional -- if unset, these are logged only, no push.

    Returns True only if the push was accepted by ntfy.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}\n[{stamp}] {title}\n{message}\n{'='*60}\n", flush=True)
    topic = os.environ.get(topic_env)
    if not topic:
        print(f"({topic_env} not set -- no push sent)", flush=True)
        return False
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
        return True
    except Exception as e:
        print(f"(ntfy push failed: {e})", flush=True)
        return False


def _step(msg: str) -> None:
    print(f"    [{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _breadcrumb(page) -> str:
    try:
        return (page.locator(".breadcrumb").first.inner_text() or "").strip()
    except Exception:
        return ""


def _click_next(page) -> None:
    page.locator("input[type=submit][name=Next]").first.click()
    page.wait_for_load_state("domcontentloaded")


# --------------------------------------------------------------------------- #
# Wizard
# --------------------------------------------------------------------------- #

def _walk_to_timetable(page) -> str | None:
    """Click through the wizard up to 'Välj tid'. Returns an error or None."""
    _step("opening start page...")
    page.goto(START_URL, wait_until="domcontentloaded")
    page.locator("input[name=StartNextButton]").click()
    page.wait_for_load_state("domcontentloaded")

    # Each remaining step is recognised by what it asks for, not by position,
    # so an added/removed step doesn't break the walk.
    for _ in range(6):
        if page.locator("input[name=TimeSearchFirstAvailableButton]").count():
            _step("reached 'Välj tid'.")
            return None

        if page.locator("#AcceptInformationStorage").count():
            _step("Avtal: accepting storage consent, 1 person...")
            page.locator("#AcceptInformationStorage").check()
            try:
                page.locator("#NumberOfPeople").select_option("1")
            except Exception:
                pass
        elif page.locator("input[type=radio][name*=ServiceCategoryId]").count():
            _step(f"Ärendekategorier: choosing {SERVICE_LABEL}...")
            radio = page.get_by_label(SERVICE_LABEL, exact=False).first
            if radio.count() == 0:
                radio = page.locator("input[type=radio][name*=ServiceCategoryId]").first
            radio.check()
        elif page.locator("input[name=AcceptConditionalAgreement][type=checkbox]").count():
            _step("Villkorlig avtalssida: ticking 'Jag har läst informationen'...")
            page.locator("input[name=AcceptConditionalAgreement][type=checkbox]").check()
        else:
            return f"Unrecognised wizard step: {_breadcrumb(page)!r}"

        _click_next(page)

    return f"Never reached 'Välj tid' (stuck at {_breadcrumb(page)!r})."


# --------------------------------------------------------------------------- #
# Result-page helpers
# --------------------------------------------------------------------------- #

def _find_free_slots(page) -> list[dict]:
    """Positive detection: bookable slots are clickable timetable cells that
    carry their exact start time, e.g.

        <div data-function="timeTableCell" data-fromdatetime="2027-04-22 09:00:00"
             aria-label="2027-04-22 09:00:00" ...>

    (The cell's text also contains a <script>/<noscript> block, so it is not
    used.) Returns a list of {"key", "date", "time"} dicts, where "key" is
    "YYYY-MM-DD HH:MM", a stable identity used for de-duplication.
    """
    try:
        raw = page.eval_on_selector_all(
            "[data-function=timeTableCell]",
            "els => els.map(el => el.getAttribute('data-fromdatetime')"
            " || el.getAttribute('aria-label') || '')",
        )
    except Exception:
        raw = []

    slots: list[dict] = []
    for value in raw or []:
        parts = (value or "").strip().split()
        if len(parts) < 2:
            continue
        date, t = parts[0], parts[1][:5]
        slots.append({"key": f"{date} {t}", "date": date, "time": t})
    return slots


def _scan_weeks(page) -> list[dict]:
    """Collect slots from the current week plus the following weeks, up to
    WEEKS_TO_SCAN in total. Slots are de-duplicated by key and returned in
    date/time order.

    Each step sets the date picker to the 'Nästa vecka' date and presses
    'Sök tid' (clicking the link itself does not run a search). A week past the
    end of the booking window renders no timetable at all; that ends the scan.
    """
    found: dict[str, dict] = {}
    for week in range(1, WEEKS_TO_SCAN + 1):
        week_slots = _find_free_slots(page)
        for s in week_slots:
            found.setdefault(s["key"], s)
        _step(f"week {week}/{WEEKS_TO_SCAN} ({_week_label(page)}): "
              f"{len(week_slots)} slot(s).")
        if week == WEEKS_TO_SCAN:
            break
        next_date = page.locator("#nextweek").get_attribute("data-date") \
            if page.locator("#nextweek").count() else None
        if not next_date:
            _step("no 'Nästa vecka' date; stopping scan.")
            break
        try:
            page.locator("#datepicker").evaluate("(el, d) => { el.value = d; }", next_date)
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.locator("input[name=TimeSearchButton]").click()
        except Exception as e:
            _step(f"could not load next week ({e}); stopping scan.")
            break
        if page.locator("table.timetable").count() == 0:
            _step(f"no timetable for week of {next_date} -- end of the booking "
                  "window; stopping scan.")
            break
    return sorted(found.values(), key=lambda s: s["key"])


def _week_label(page) -> str:
    try:
        ids = page.eval_on_selector_all(
            "table.timetable thead th[id]", "els => els.map(e => e.id)"
        )
        return f"{ids[0]}..{ids[-1]}" if ids else "?"
    except Exception:
        return "?"


def _result_says_no_slots(page) -> bool:
    try:
        body = (page.locator("body").inner_text() or "").lower()
    except Exception:
        body = ""
    return NO_SLOTS_MARKER in body


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
            err = _walk_to_timetable(page)
            if err:
                result["error"] = err
                return _finish(browser, result)

            _step("clicking 'Första lediga tid'...")
            page.locator("input[name=TimeSearchFirstAvailableButton]").click()
            page.wait_for_load_state("domcontentloaded")
            try:
                page.locator("table.timetable").wait_for(timeout=20_000)
            except Exception:
                _step("timetable not seen within timeout.")

            no_slots_banner = _result_says_no_slots(page)
            slots = _scan_weeks(page)
            if slots:
                result["ok"] = True
                result["available"] = True
                result["slots"] = slots
                lines = [_slot_line(s) for s in slots]
                result["detail"] = (
                    f"{len(slots)} bookable slot(s) found:\n  - "
                    + "\n  - ".join(lines)
                )
            elif no_slots_banner:
                result["ok"] = True
                result["available"] = False
                result["detail"] = "No free times right now."
            else:
                # Neither bookable cells nor the 'no slots' text. Ambiguous (site
                # hiccup / layout change) -- do NOT alert, to avoid false alarms.
                result["ok"] = True
                result["available"] = False
                result["detail"] = (
                    "Inconclusive: no bookable timetable cells detected and no "
                    "'Inga lediga tider' text either."
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
# Same scheme as the samordningsnummer watcher: store the previous check's slot
# keys, alert only on keys not in it. Backend is a GitHub Gist when GIST_PAT and
# GIST_ID are set (survives self-loop handoffs), else a file in the runner temp
# dir. Uses its own file inside the shared gist so watchers don't collide.

STATE_FILE = os.environ.get(
    "STATE_FILE",
    os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "passport_seen_slots.json"),
)
GIST_FILENAME = "passport_seen_slots.json"


def _slot_line(s: dict) -> str:
    return f"{s['date']} {s['time']}".strip()


def _gist_creds() -> tuple[str, str] | None:
    """Return (pat, gist_id) if Gist-backed state is configured, else None."""
    pat = os.environ.get("GIST_PAT")
    gist_id = os.environ.get("GIST_ID")
    if pat and gist_id:
        return pat, gist_id
    return None


def _gist_load(pat: str, gist_id: str) -> set[str]:
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "passport-watcher",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        gist = json.loads(resp.read().decode("utf-8"))
    entry = (gist.get("files") or {}).get(GIST_FILENAME)
    if not entry:
        return set()
    content = (entry.get("content") or "").strip()
    if not content:
        return set()
    data = json.loads(content)
    return set(data) if isinstance(data, list) else set()


def _gist_save(pat: str, gist_id: str, keys) -> None:
    body = json.dumps(
        {"files": {GIST_FILENAME: {"content": json.dumps(sorted(keys))}}}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        data=body,
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "passport-watcher",
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    urllib.request.urlopen(req, timeout=15).read()


def _load_seen() -> set[str]:
    creds = _gist_creds()
    if creds:
        try:
            return _gist_load(*creds)
        except Exception as e:
            print(f"  (could not read state from gist: {e})", flush=True)
            return set()
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_seen(keys) -> None:
    creds = _gist_creds()
    if creds:
        try:
            _gist_save(*creds, keys)
        except Exception as e:
            print(f"  (could not write state to gist: {e})", flush=True)
        return
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(sorted(keys), f)
    except Exception as e:
        print(f"  (could not write state file {STATE_FILE}: {e})", flush=True)


def main() -> int:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] checking availability...", flush=True)
    res = check_availability()

    if res["error"]:
        print(f"  ! error: {res['error']}", flush=True)
        notify(
            "passport watcher: run error",
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

        if new:
            lines = [_slot_line(s) for s in new]
            shown = lines[:20]
            more = f"\n  ... and {len(lines) - 20} more" if len(lines) > 20 else ""
            sent = notify(
                "Passport (London) -- new appointment slot!",
                f"{len(new)} new slot(s):\n  - " + "\n  - ".join(shown) + more
                + f"\n\nBook here: {START_URL}",
            )
            # Only count slots as alerted once the push actually went out, so a
            # failed push is retried on the next check instead of lost.
            _save_seen(current if sent else current & seen)
        else:
            _save_seen(current)
            print(
                f"  slots available but all already alerted: "
                f"{sorted(current)}",
                flush=True,
            )
    elif "Inconclusive" in res["detail"]:
        print(f"  ? INCONCLUSIVE: {res['detail']}", flush=True)
        notify(
            "passport watcher: inconclusive",
            f"{res['detail']} The page layout may have changed -- worth a look.",
            topic_env="NTFY_DEBUG_TOPIC",
            priority="low",
            tags="warning",
        )
    else:
        _save_seen(set())
        print(f"  no slots: {res['detail']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
