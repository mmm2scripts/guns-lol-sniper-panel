# Guns.lol User Sniper - Control Panel

```
iPhone dashboard  ->  HTTPS + API key  ->  server.py (API)  ->  67.py (your existing sniper)  ->  guns.lol
```

Your original `67.py` is **unchanged in behaviour**. `server.py` launches it as a background process on the
server and wraps it in a small authenticated API. The sniper keeps running when the iPhone app is closed;
reopen the app and it fetches the current status and recent logs from the server.

> **What "sniping" means here.** `67.py` is a username *availability checker*: it requests
> `https://guns.lol/<name>`, decides whether the name is free, saves free names to `unclaimed.txt` and
> (optionally) pings your Discord webhook. It does not register names itself, so the dashboard maps its
> terms like this: **Attempts** = names checked, **Snipes** = free names found, **Failed** = checks that were
> blocked / rate-limited / errored, **Taken** = names that were already claimed.

> **Rotate your secrets.** The archive you gave me contained a real Discord webhook URL and ScrapingAnt key
> inside `config.json`. I blanked both in this copy so they don't end up on GitHub. If that archive was shared
> anywhere, regenerate them (Discord channel -> Integrations -> Webhooks; ScrapingAnt dashboard) and enter the
> new values in the app's **Settings** (they are write-only there).

---

## 1. Install

Requires Python 3.9+ on the server. There are **no new dependencies**: the API server uses only the Python
standard library; `requirements.txt` is still just `requests` and `colorama` for `67.py`.

```bash
unzip guns-lol-sniper-panel-server.zip -d /opt/guns-lol-sniper-panel
cd /opt/guns-lol-sniper-panel
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 2. Configure

Everything `67.py` already supports is editable from the app's **Settings** tab (or by editing `config.json`):

| Setting | Meaning |
|---|---|
| `mode` | `direct` (free, plain requests) or `scrapingant` (hosted browser, needs a key) |
| `scrapingant_key` | ScrapingAnt key (write-only in the app). `SCRAPINGANT_KEY` env var also works |
| `letter_count` | Length of random names |
| `delay` | Seconds between checks |
| `max_requests` | Safety stop (protects your ScrapingAnt quota) |
| `use_customlist` | Check names from `customlist.txt` instead of random ones (editable in the app) |
| `filter_premium` | Skip names starting/ending with `.` `_` `-` |
| `save_to_file` | Append free names to `unclaimed.txt` |
| `webhook_url` | Discord webhook for "available" alerts (write-only in the app) |
| `known_taken` | A name you know is claimed, for the full start-up self-test |

Settings are applied the next time the sniper starts (the app offers to restart it after you save).

Server options go in `.env` (copy `.env.example`) or real environment variables:
`HOST`, `PORT`, `PANEL_API_KEY`, `TRUST_PROXY`, `TLS_CERT`, `TLS_KEY`, `CORS_ORIGIN`, `AUTO_RESUME`.

## 3. Start the server

```bash
./start.sh                 # creates the venv, installs requirements, starts the panel
# or:  ./venv/bin/python server.py
```

To keep it alive across reboots/crashes, use systemd: copy `deploy/guns-sniper-panel.service` to
`/etc/systemd/system/`, adjust the user/paths, then `sudo systemctl enable --now guns-sniper-panel`.
(`screen`, `tmux` or `pm2 start server.py --interpreter ./venv/bin/python` also work.)

If the server itself restarts while the sniper was running, the panel **resumes it automatically**
(`AUTO_RESUME=0` disables that). A sniper that finished on its own, crashed, or failed its self-test is not
auto-restarted.

### HTTPS (required)

The dashboard sends your API key on every request, so it must travel over HTTPS. The server binds to
`127.0.0.1:8787` by default; put a reverse proxy in front:

- **Caddy** (easiest, automatic certificates): see `deploy/Caddyfile`. Add `TRUST_PROXY=1` to `.env`.
- **nginx + certbot**: see `deploy/nginx.conf`.
- **No proxy**: set `TLS_CERT` and `TLS_KEY` to your certificate files and `HOST=0.0.0.0`.

The app refuses plain `http://` server addresses, except for localhost / LAN addresses (for testing).

## 4. The API key

On first start the server prints a random key **once**:

```
FIRST RUN - your API key (shown once, only a hash is stored):
    Y5TH6X247XIa49x-Lxbo8KACjLMmAnX3w903HWAvrLY
```

Only a SHA-256 hash is stored (`data/api_key.sha256`, mode 600). Lost it or want to rotate it:
`python server.py --new-key` (the old key stops working). Alternatively set your own 24+ character key with
`PANEL_API_KEY` in `.env`. Requests use `Authorization: Bearer <key>`; keys are compared in constant time,
and an IP is locked out for 5 minutes after 8 wrong attempts.

## 5. Connect the iPhone dashboard

**Option A - the iOS app (unsigned IPA).** This repo builds it for you on GitHub (I can't compile iOS apps in
my Linux environment, so the IPA has to be produced by GitHub's macOS runner):

1. Create a GitHub repository and push this whole project (the `.gitignore` already keeps secrets out).
2. Open the repo's **Actions** tab -> **Build unsigned IPA** -> **Run workflow**.
3. When it finishes (~5 min), download the **GunsSniper-unsigned-ipa** artifact -> `GunsSniper-unsigned.ipa`.
   (Pushing a tag like `v1.0` also attaches it to a GitHub Release.)
4. The IPA is **unsigned**, so it must be signed when installed. Use a sideloading tool such as AltStore,
   SideStore or Sideloadly with your Apple ID (free Apple IDs need re-signing every 7 days).
5. Open the app, enter `https://your-domain` and the API key, tap **Connect**. The URL and key are stored in
   the iOS Keychain, never in the app's code.

Building locally on a Mac instead: `brew install xcodegen && ./ios/build-unsigned-ipa.sh`.

**Option B - no app at all.** Open `https://your-domain/` in Safari, connect, then Share -> **Add to Home
Screen** for a full-screen app. (In Safari the key is kept in the browser's local storage rather than the
Keychain.)

## 6. How the existing sniper works

1. `server.py` starts `python -u 67.py` in this folder with `SNIPER_EVENTS=1`.
2. `67.py` reads `config.json` / `customlist.txt`, runs its start-up self-test (a random 24-character name must
   come back *unclaimed*; `known_taken` must come back *claimed*), then loops: pick a name -> request
   `guns.lol/<name>` -> classify as `unclaimed` / `claimed` / `blocked` / `ratelimit` / `error`.
3. Free names are appended to `unclaimed.txt` and sent to your Discord webhook (as before).
4. The only changes to `67.py` are ~19 added lines that print machine-readable `@@EVENT` lines when
   `SNIPER_EVENTS=1` (start, connected, checking, result, end). With the variable unset it behaves exactly as
   before, so `python 67.py` and `python 67.py --probe NAME` still work standalone.
5. `panel/supervisor.py` turns that output into timestamped logs and counters. Logs (last 5000) and counters are
   persisted in `data/`, so they survive dashboard disconnects and server restarts.
6. **Stop** sends the sniper a graceful interrupt (Ctrl+C equivalent) and force-kills only if it hasn't exited
   after ~12 s.

Log events shown live in the app: Starting, Connected, Checking username (= attempt made), Username available,
Username unavailable, Success, Error / blocked / rate-limited, Disconnected. The Logs tab updates by long-polling
the server (no manual refresh) and pauses while the app is in the background.

## 7. API

All `/api/*` endpoints require `Authorization: Bearer <API key>` and return JSON. Errors look like
`{"error": "..."}`.

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Sniper state, uptime, current username, activity, counters, guns.lol status, server info |
| GET | `/api/config` | Current settings (secret values are never returned; only `webhook_url_set` / `scrapingant_key_set`) |
| PUT | `/api/config` | Update settings. Send only changed keys; unknown keys and invalid values -> `400` |
| GET | `/api/logs?since=<id>&limit=<n>&wait=<s>` | Log entries after `since`; `wait` (max 25) long-polls for live updates |
| POST | `/api/logs/clear` | Clear the log history |
| POST | `/api/sniper/start` | Start (409 if already running) |
| POST | `/api/sniper/stop` | Graceful stop (409 if not running) |
| POST | `/api/sniper/restart` | Stop, then start |
| POST | `/api/test-connection` | Runs the same self-test as start-up against guns.lol (uses ~20 ScrapingAnt credits in that mode) |
| GET / PUT | `/api/customlist` | Read / replace `customlist.txt` (`{"names": [...]}`) |
| GET | `/api/results` | Free names found so far (`unclaimed.txt`, newest first) |

```bash
curl -H "Authorization: Bearer $KEY" https://sniper.example.com/api/status
curl -X POST -H "Authorization: Bearer $KEY" https://sniper.example.com/api/sniper/start
curl -X PUT  -H "Authorization: Bearer $KEY" -d '{"delay": 1.5}' https://sniper.example.com/api/config
```

### Security notes

- API key checked on every `/api/*` request (constant-time), with brute-force lockout; only its hash is stored.
- Secrets (`webhook_url`, `scrapingant_key`, the API key, any Discord webhook URL) are redacted from every log
  line and are never returned by `/api/config`, `/api/status` or `/api/logs`.
- Config input is validated (types, ranges, Discord-only webhook URLs, username patterns). Request bodies are
  capped at 256 KB.
- No filesystem access is exposed: only five fixed static files are served (no directory listing, no
  traversal); the only files the API reads/writes are `config.json`, `customlist.txt` and `unclaimed.txt`.
- Static dashboard files are public (they contain no secrets); the API is not.

## Project layout

```
67.py                     your sniper (19 lines added for the panel events)
config.json  customlist.txt  unclaimed.txt  requirements.txt  install.bat      (yours)
server.py                 API + dashboard server
panel/supervisor.py       runs 67.py, parses events, logs, counters
panel/config_store.py     validated config / list / results access + connection test (reuses 67.py's own code)
web/                      the dashboard (also bundled inside the iOS app)
ios/  .github/workflows/  iOS app source + unsigned-IPA build
deploy/                   systemd, Caddy, nginx examples
README.original.md        your original README
```
