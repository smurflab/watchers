# samordningsnummer slot watcher

Runs a headless check of the Migrationsverket **"Boka tid för besök"** page for
the Embassy of Sweden in London every ~5 minutes on GitHub Actions, and sends a
**push notification to your phone** (via [ntfy.sh](https://ntfy.sh)) the moment
appointment slots for **ansöka om samordningsnummer** appear.

- It only **watches and alerts** — it never books or enters personal data.
- When you get the ping, open the booking page on any device and finish it
  yourself. The notification links straight to the booking page.

## Setup

This watcher lives in the [`watchers`](../README.md) monorepo. To make it run:

1. **Install ntfy on your phone** (App Store / Google Play). Pick a secret,
   unguessable topic name, e.g. `samordning-alerts-7fk29q`, and **subscribe** to
   it. Anyone who knows the topic can push to it, so keep it private.
2. **Add the topic as a repository secret** named **`NTFY_SAMORDNING`**
   (Settings → Secrets and variables → Actions → New repository secret).
3. **Enable Actions** if prompted, then test it: Actions tab →
   *samordningsnummer* → **Run workflow**. The log should end with either
   `no slots: ...` or fire a push to your phone if slots are open.

From then on it runs itself every ~5 minutes.

## Behaviour notes

- **Repeated pings:** each run is independent (no memory between runs), so while
  slots stay open you'll get a ping every ~5 min until you book. Intentional.
- **No auto-booking:** picking a time and entering your details is always done by
  you. This watcher just tells you when to act.

## Run it locally (optional test)

```sh
cd samordningsnummer
pip install -r requirements.txt
python -m playwright install chromium
NTFY_TOPIC=your-topic python check_slots.py
```
