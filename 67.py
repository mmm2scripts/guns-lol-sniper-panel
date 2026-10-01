"""
guns.lol username checker - uses ScrapingAnt's hosted Chrome (free tier). Nothing to install locally.
Uses the same config.json / customlist.txt / unclaimed.txt as 67.py.

Usage:
    python 67.py            # normal run
    python 67.py --probe NAME [NAME ...]   # print raw signals for names (to verify detection)
"""
import json
import os
import random
import string
import sys
import time

import requests
from colorama import Fore, init

init(autoreset=True)

BASE = "https://guns.lol/"
API = "https://api.scrapingant.com/v2/general"
API_KEY = ""
MODE = "direct"
CALLS = 0
LAST = (0, "")
CONFIG_FILE = "config.json"
DEFAULTS = {
    "letter_count": 5, "delay": 0, "use_customlist": False, "filter_premium": True,
    "save_to_file": True, "webhook_url": "",
    "known_taken": "",   # optional: a username you KNOW is claimed, used for the startup self-test
    "mode": "direct",       # "direct" = free plain requests; "scrapingant" = hosted browser (needs key)
    "scrapingant_key": "",  # free key from https://scrapingant.com (or set env var SCRAPINGANT_KEY)
    "max_requests": 900,    # safety stop; each check costs ~10 of your 10,000 free monthly credits
}

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

UNCLAIMED, CLAIMED, BLOCKED, RATELIMIT, ERROR = "unclaimed", "claimed", "blocked", "ratelimit", "error"

# Control-panel integration: when SNIPER_EVENTS=1 (set by server.py) the script also prints
# machine-readable "@@EVENT {...}" lines. With the variable unset, behaviour is exactly as before.
EVENTS = os.environ.get("SNIPER_EVENTS") == "1"


def emit(event, **data):
    if EVENTS:
        print("@@EVENT " + json.dumps({"event": event, **data}), flush=True)


def new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(UA),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def page_title(body):
    low = (body or "").lower()
    i, j = low.find("<title"), low.find("</title>")
    if i == -1 or j == -1:
        return ""
    return body[low.find(">", i) + 1:j].strip()


def classify(status, body):
    """Map a response to a state. Anything ambiguous is BLOCKED/ERROR, never CLAIMED."""
    low = (body or "").lower()
    title = page_title(body).lower()
    challenge = any(t in title for t in ("just a moment", "attention required", "checking your browser",
                                         "verify you are human", "access denied"))
    if status == 429:
        return RATELIMIT
    if status in (403, 503) or challenge:
        return BLOCKED  # real bot-wall / challenge page
    if status == 404:
        return UNCLAIMED
    if status >= 500 or status == 0:
        return ERROR
    if "username not found" in low or "bulunamad" in low:
        return UNCLAIMED
    if status == 200:
        return CLAIMED
    return ERROR


def fetch_api(session, name, timeout=90):
    """Load the profile page in ScrapingAnt's hosted Chrome. Returns (http-like status, rendered html, retry_after)."""
    global CALLS
    CALLS += 1
    try:
        r = session.get(API, params={"url": BASE + name, "browser": "true"},
                        headers={"x-api-key": API_KEY}, timeout=timeout)
    except requests.RequestException as e:
        return 0, str(e), None
    code = r.status_code
    if code in (400, 401, 403):
        sys.exit(f"{Fore.RED}ScrapingAnt rejected the request (HTTP {code}) - check scrapingant_key / credits.\n{r.text[:200]}{Fore.RESET}")
    if code == 423:
        return 403, r.text, None            # target's anti-bot detected the scraper
    if code in (409, 429):
        return 429, r.text, r.headers.get("Retry-After")
    if code in (200, 404):
        return code, r.text, None           # 404 = target page not found
    return 0, r.text, None                  # 422 / 5xx etc.


def fetch_direct(session, name, timeout=15):
    global CALLS
    CALLS += 1
    try:
        r = session.get(BASE + name, timeout=timeout, allow_redirects=True)
        return r.status_code, r.text, r.headers.get("Retry-After")
    except requests.RequestException as e:
        return 0, str(e), None


def fetch(session, name):
    return fetch_api(session, name) if MODE == "scrapingant" else fetch_direct(session, name)


def check(session, name, retries=2):
    wait = 3
    for _ in range(retries):
        status, body, retry_after = fetch(session, name)
        global LAST
        LAST = (status, body)
        state = classify(status, body)
        if state == RATELIMIT:
            try:
                wait = max(wait, int(retry_after))
            except (TypeError, ValueError):
                pass
            print(f"{Fore.YELLOW}Rate limited - sleeping {wait}s{Fore.RESET}")
            time.sleep(wait)
            wait = min(wait * 2, 120)
            continue
        if state == ERROR:
            time.sleep(1.5)
            continue
        return state, status
    return ERROR, 0


def random_name(n, filter_premium):
    base = string.ascii_lowercase + string.digits
    allc = base + "._"
    if filter_premium:
        if n == 1:
            return random.choice(base)
        return random.choice(base) + "".join(random.choice(allc) for _ in range(n - 2)) + random.choice(base)
    return "".join(random.choice(allc) for _ in range(n))


def diagnose():
    status, body = LAST
    snippet = " ".join(body.split())[:300]
    print(f"{Fore.YELLOW}--- last response ---\nHTTP {status} | length {len(body)} | title: {page_title(body)!r}\n"
          f"start of page: {snippet}\n---------------------{Fore.RESET}")


def self_test(session, known_taken):
    """Verify the HTTP response actually distinguishes free vs taken names before burning requests."""
    fresh = "".join(random.choice(string.ascii_lowercase) for _ in range(24))
    state, status = check(session, fresh)
    print(f"{Fore.CYAN}Self-test: random 24-char name -> {state} (HTTP {status}){Fore.RESET}")
    if state in (BLOCKED, ERROR, RATELIMIT):
        print(f"{Fore.RED}guns.lol didn't give a usable answer ({state}).{Fore.RESET}")
        diagnose()
        if MODE == "direct":
            print(f"{Fore.YELLOW}Try \"mode\": \"scrapingant\" in config.json.{Fore.RESET}")
        return False
    if state != UNCLAIMED:
        print(f"{Fore.RED}A name that can't exist was reported as taken, so 'username not found' is probably "
              f"not visible in the page the API returned. Detection would be unreliable; aborting. "
              f"Run --probe to see the raw response.{Fore.RESET}")
        diagnose()
        if MODE == "direct":
            print(f"{Fore.YELLOW}Try \"mode\": \"scrapingant\" in config.json (renders JavaScript).{Fore.RESET}")
        return False
    if known_taken:
        state2, status2 = check(session, known_taken)
        print(f"{Fore.CYAN}Self-test: known_taken '{known_taken}' -> {state2} (HTTP {status2}){Fore.RESET}")
        if state2 != CLAIMED:
            print(f"{Fore.RED}Known-taken name wasn't classified as claimed; aborting.{Fore.RESET}")
            return False
    else:
        print(f"{Fore.YELLOW}Tip: set \"known_taken\" in config.json to a name you know is claimed for a full self-test.{Fore.RESET}")
    return True


def probe(names):
    global API_KEY, MODE
    cfg = load_config()
    MODE = str(cfg["mode"]).strip().lower()
    API_KEY = str(cfg["scrapingant_key"]).strip() or os.environ.get("SCRAPINGANT_KEY", "").strip()
    s = new_session()
    for n in names:
        status, body, _ = fetch(s, n)
        low = body.lower()
        title = body[low.find("<title>") + 7: low.find("</title>")] if "<title>" in low else ""
        print(f"{n}: HTTP {status} | len={len(body)} | title={title!r} | "
              f"'username not found' in body={'username not found' in low} | -> {classify(status, body)}")
        print("   start:", " ".join(body.split())[:200])


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(f"{Fore.RED}Could not read {CONFIG_FILE}: {e}{Fore.RESET}")
    return cfg


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--probe":
        return probe(sys.argv[2:])

    global API_KEY, MODE
    cfg = load_config()
    MODE = str(cfg["mode"]).strip().lower()
    if MODE not in ("direct", "scrapingant"):
        sys.exit(f"{Fore.RED}mode must be \"direct\" or \"scrapingant\"{Fore.RESET}")
    API_KEY = str(cfg["scrapingant_key"]).strip() or os.environ.get("SCRAPINGANT_KEY", "").strip()
    if MODE == "scrapingant" and not API_KEY:
        sys.exit(f"{Fore.RED}No API key. Create a free account at https://scrapingant.com and put the key in "
                 f"config.json as \"scrapingant_key\" (or set env var SCRAPINGANT_KEY).{Fore.RESET}")
    max_requests = int(cfg["max_requests"])
    n = int(cfg["letter_count"])
    delay = float(cfg["delay"])
    filt = bool(cfg["filter_premium"])
    save = bool(cfg["save_to_file"])
    hook = str(cfg["webhook_url"]).strip() or None
    if hook and not hook.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
        sys.exit(f"{Fore.RED}webhook_url must be a Discord webhook URL or empty{Fore.RESET}")

    names = None
    if cfg["use_customlist"]:
        try:
            with open("customlist.txt", encoding="utf-8") as f:
                names = [l.strip() for l in f if l.strip() and not l.strip().startswith("//")] or None
        except FileNotFoundError:
            pass
    print(f"{Fore.CYAN}Mode: {MODE}{Fore.RESET}")
    print(f"{Fore.CYAN}Config: length={n}, delay={delay}, customlist={'on' if names else 'off'}, "
          f"filter_premium={filt}, save_to_file={save}, webhook={'on' if hook else 'off'}{Fore.RESET}")

    emit("start", mode=MODE, letter_count=n, delay=delay, customlist=bool(names), customlist_size=len(names or []),
         filter_premium=filt, save_to_file=save, webhook=bool(hook), max_requests=max_requests)
    session = new_session()
    if not self_test(session, str(cfg["known_taken"]).strip()):
        emit("disconnected", reason="self_test_failed")
        emit("end", reason="self_test_failed")
        return
    emit("connected")

    i = 0
    try:
        while True:
            if CALLS >= max_requests:
                print(f"{Fore.YELLOW}Reached max_requests ({max_requests}) - stopping to protect your free credits. "
                      f"Raise it in config.json if you want to continue.{Fore.RESET}")
                emit("end", reason="max_requests")
                break
            if names is not None:
                if i >= len(names):
                    print(f"{Fore.CYAN}Customlist check completed.{Fore.RESET}")
                    emit("end", reason="customlist_done")
                    break
                name = names[i]
                i += 1
                if filt and (name[0] in "._-" or name[-1] in "._-"):
                    print(f"{BASE}{name} - {Fore.YELLOW}Skipping (Premium Alias){Fore.RESET}")
                    continue
            else:
                name = random_name(n, filt)

            emit("checking", name=name)
            state, status = check(session, name)
            emit("result", name=name, state=state, status=status)
            color = {UNCLAIMED: Fore.GREEN, CLAIMED: Fore.RED}.get(state, Fore.YELLOW)
            print(f"{BASE}{name} - {color}{state}{Fore.RESET} (HTTP {status})")

            if state == UNCLAIMED:
                if save:
                    with open("unclaimed.txt", "a", encoding="utf-8") as f:
                        f.write(name + "\n")
                if hook:
                    try:
                        requests.post(hook, timeout=10, json={"embeds": [{
                            "title": f"Available: {name} (https://guns.lol/{name})", "color": 0x9B59B6}]})
                    except requests.RequestException:
                        pass
            elif state == BLOCKED:
                print(f"{Fore.RED}Blocked by bot protection - backing off 30s{Fore.RESET}")
                time.sleep(30)

            time.sleep(delay + random.uniform(0.2, 0.6))
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Program terminated.{Fore.RESET}")
        emit("end", reason="interrupted")


if __name__ == "__main__":
    main()
