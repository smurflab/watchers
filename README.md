# watchers

A collection of small, independent **watchers** — scripts that run on a schedule
via GitHub Actions and send a **phone push** (via free [ntfy.sh](https://ntfy.sh))
when something they're watching changes. They only watch and alert; they never
book, buy, or submit anything.

## Layout

```
watchers/
├── <watcher-name>/            ← one folder per watcher (code + requirements.txt)
│   ├── check_*.py
│   └── requirements.txt
└── .github/workflows/
    └── <watcher-name>.yml     ← one workflow per watcher (its own cron schedule)
```

Each watcher is self-contained: its own folder, its own workflow, its own ntfy
topic. They share nothing except this repo.

## Cost

Keep this repo **public** — GitHub Actions minutes are then free and unlimited,
so the schedules cost nothing. Nothing sensitive lives in the code; each ntfy
topic is stored as an encrypted **repository secret**.

## ntfy topics — one secret per watcher

So you know *which* watcher pinged you, each watcher uses its own ntfy topic,
stored as its own repository secret. Naming convention: `NTFY_<WATCHER>`.

| Watcher            | Workflow                | Secret name       |
| ------------------ | ----------------------- | ----------------- |
| `samordningsnummer`| `samordningsnummer.yml` | `NTFY_SAMORDNING` |

Set secrets under **Settings → Secrets and variables → Actions → New repository
secret**. On your phone, subscribe to each topic in the ntfy app.

## De-dup state (Gist KV) — alert once, ever

Each self-loop run only lives ~5h before handing off to a fresh runner, whose
disk is empty. To avoid re-alerting on a still-open slot after every handoff,
de-dup state is stored in a **GitHub Gist** — a mutable file that outlives the
runner. This activates when **both** secrets are set (otherwise it falls back to
a per-run temp file, which re-pings once per handoff):

| Secret     | What it is                                                          |
| ---------- | ------------------------------------------------------------------- |
| `GIST_PAT` | A **classic** PAT with the `gist` scope.                            |
| `GIST_ID`  | Id of a gist containing a file named `samordning_seen_slots.json`.  |

Each check does one `GET` (read state) and one `PATCH` (write state). Any API
error is logged and the run continues on the temp-file behavior.

## Adding a new watcher

1. `mkdir <name>` and add `check_*.py` + `requirements.txt`.
2. Copy `.github/workflows/samordningsnummer.yml` to
   `.github/workflows/<name>.yml`; update `name`, `concurrency.group`,
   `defaults.run.working-directory: <name>`, the cron, and the
   `NTFY_TOPIC` secret reference (`NTFY_<WATCHER>`).
3. Add the `NTFY_<WATCHER>` repository secret and subscribe to the topic on your
   phone.
4. Commit & push. Trigger a manual run from the Actions tab to test.

## Notes / gotchas

- **Schedule delays:** GitHub's cron is best-effort; runs can be a few minutes
  late when GitHub is busy.
- **Auto-disable after 60 days:** GitHub pauses scheduled workflows if the repo
  has no activity for 60 days. Any small commit re-enables them.
- **Repeated pings:** with the Gist KV state configured (see above) each slot
  pings exactly once, ever. Without it (temp-file fallback), a still-open slot
  re-pings once per ~5h self-loop handoff.

See each watcher's own `README.md` for what it watches.
