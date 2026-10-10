# passport slot watcher

Runs a headless check of the Ventus booking system for the Embassy of Sweden in
London (**Passansökan**) every ~5 minutes on GitHub Actions, and sends a **push
notification to your phone** (via [ntfy.sh](https://ntfy.sh)) when appointment
slots appear.

Booking page: https://ventus.enalog.se/Booking/Booking/Index/UDDLondon

- It clicks through the consent steps (Avtal → Ärende → Villkorlig avtalssida),
  presses **"Första lediga tid"**, then reads that week plus the next 7 (set
  `WEEKS_TO_SCAN` to change). It never picks a
  time or enters personal data.
- When you get the ping, open the booking page and finish it yourself.

## Setup

1. In the ntfy app, subscribe to a secret, unguessable topic (e.g.
   `passport-alerts-x93kq2`).
2. Add it as repository secret **`NTFY_PASSPORT`**. Optionally add
   **`NTFY_PASSPORT_DEBUG`** for quiet health pings (errors / inconclusive).
3. De-dup state reuses the shared `GIST_PAT` / `GIST_ID` secrets, in its own
   gist file `passport_seen_slots.json` (created automatically on first write).
4. Actions tab → *passport* → **Run workflow** to start the self-loop.

## Run it locally

```sh
cd passport
pip install -r requirements.txt
python -m playwright install chromium
NTFY_TOPIC=your-topic python check_slots.py
```
