"""Validated read/write access to the files the existing sniper already uses:
config.json, customlist.txt and unclaimed.txt. Nothing outside these files is reachable."""
import importlib.util
import json
import os
import random
import re
import string
import threading
import time

SECRET_KEYS = ("webhook_url", "scrapingant_key")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
HOOK_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
MAX_LIST = 5000


def load_sniper_module(root):
    """Import 67.py as a module (it is guarded by __main__) so its own fetch/classify/check are reused."""
    spec = importlib.util.spec_from_file_location("sniper67", os.path.join(root, "67.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:                       # 67.py calls colorama.init(); don't leave our stdout wrapped
        import colorama
        colorama.deinit()
    except Exception:
        pass
    return mod


class ConfigStore:
    def __init__(self, root):
        self.root = root
        self.path = os.path.join(root, "config.json")
        self.lock = threading.Lock()
        self.defaults = dict(load_sniper_module(root).DEFAULTS)

    # -------------------------------------------------------------- config.json
    def _read(self):
        cfg = dict(self.defaults)
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
        except FileNotFoundError:
            pass
        return cfg

    def raw(self):
        with self.lock:
            return self._read()

    def secret_values(self):
        cfg = self.raw()
        return [str(cfg.get(k, "")) for k in SECRET_KEYS] + [os.environ.get("SCRAPINGANT_KEY", "")]

    def public(self):
        """Config for the dashboard. Secret values are never returned, only whether they are set."""
        cfg = self.raw()
        out = {k: v for k, v in cfg.items() if k not in SECRET_KEYS and k in self.defaults}
        for k in SECRET_KEYS:
            out[k] = ""
            out[k + "_set"] = bool(str(cfg.get(k, "")).strip())
        out["scrapingant_key_from_env"] = bool(os.environ.get("SCRAPINGANT_KEY", "").strip())
        return out

    def validate(self, patch):
        if not isinstance(patch, dict):
            return None, ["Body must be a JSON object"]
        errors, clean = [], {}
        unknown = [k for k in patch if k not in self.defaults]
        if unknown:
            errors.append("Unknown setting(s): " + ", ".join(sorted(map(str, unknown))[:5]))

        def num(key, typ, lo, hi):
            v = patch[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or (typ is int and v != int(v)):
                errors.append(f"{key} must be a {'whole ' if typ is int else ''}number")
            elif not lo <= v <= hi:
                errors.append(f"{key} must be between {lo} and {hi}")
            else:
                clean[key] = typ(v)

        def boolean(key):
            if not isinstance(patch[key], bool):
                errors.append(f"{key} must be true or false")
            else:
                clean[key] = patch[key]

        def text(key, pattern=None, maxlen=200):
            v = patch[key]
            if not isinstance(v, str):
                errors.append(f"{key} must be text")
                return
            v = v.strip()
            if len(v) > maxlen or any(c.isspace() for c in v):
                errors.append(f"{key} is invalid")
            elif pattern and v and not pattern.match(v):
                errors.append(f"{key} is invalid")
            else:
                clean[key] = v

        for key in patch:
            if key not in self.defaults:
                continue
            if key == "mode":
                if str(patch[key]).strip().lower() not in ("direct", "scrapingant"):
                    errors.append('mode must be "direct" or "scrapingant"')
                else:
                    clean[key] = str(patch[key]).strip().lower()
            elif key == "letter_count":
                num(key, int, 1, 30)
            elif key == "delay":
                num(key, float, 0, 600)
            elif key == "max_requests":
                num(key, int, 1, 10_000_000)
            elif key in ("use_customlist", "filter_premium", "save_to_file"):
                boolean(key)
            elif key == "webhook_url":
                v = patch[key]
                if not isinstance(v, str) or (v.strip() and not v.strip().startswith(HOOK_PREFIXES)) \
                        or len(v) > 300 or any(c.isspace() for c in v.strip()):
                    errors.append("webhook_url must be a Discord webhook URL or empty")
                else:
                    clean[key] = v.strip()
            elif key == "scrapingant_key":
                text(key, re.compile(r"^[A-Za-z0-9_-]{8,128}$"), 128)
            elif key == "known_taken":
                text(key, NAME_RE, 32)
        return clean, errors

    def update(self, patch):
        clean, errors = self.validate(patch)
        if errors:
            return None, errors
        with self.lock:
            cfg = self._read()
            try:
                with open(self.path, encoding="utf-8") as f:
                    on_disk = json.load(f)
            except (OSError, ValueError):
                on_disk = {}
            on_disk = on_disk if isinstance(on_disk, dict) else {}
            on_disk.update(clean)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(on_disk, f, indent=4)
                f.write("\n")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)
        return sorted(clean), []

    # -------------------------------------------------------------- customlist.txt / unclaimed.txt
    def get_customlist(self):
        try:
            with open(os.path.join(self.root, "customlist.txt"), encoding="utf-8") as f:
                return [l.rstrip("\n") for l in f if l.strip()]
        except FileNotFoundError:
            return []

    def set_customlist(self, names):
        if not isinstance(names, list) or len(names) > MAX_LIST:
            return False, [f"names must be a list of at most {MAX_LIST} entries"]
        out, errors = [], []
        for n in names:
            if not isinstance(n, str):
                errors.append("every entry must be text")
                break
            n = n.strip()
            if not n:
                continue
            if n.startswith("//") and len(n) <= 100 and "\n" not in n:
                out.append(n)
            elif NAME_RE.match(n):
                out.append(n)
            else:
                errors.append(f"Invalid username: {n[:20]}")
                if len(errors) >= 3:
                    break
        if errors:
            return False, errors
        path = os.path.join(self.root, "customlist.txt")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write("\n".join(out) + ("\n" if out else ""))
        os.replace(path + ".tmp", path)
        return True, []

    def get_results(self, limit=300):
        try:
            with open(os.path.join(self.root, "unclaimed.txt"), encoding="utf-8") as f:
                lines = [l.strip() for l in f if NAME_RE.match(l.strip())]
        except FileNotFoundError:
            return []
        seen, out = set(), []
        for n in reversed(lines):
            if n not in seen:
                seen.add(n)
                out.append(n)
            if len(out) >= limit:
                break
        return out


class ConnectionTester:
    """Runs the same self-test 67.py does at start-up, using 67.py's own fetch/classify code."""

    def __init__(self, root, store):
        self.root, self.store = root, store
        self.lock = threading.Lock()
        self.last = 0

    def run(self):
        if time.time() - self.last < 5:
            return {"ok": False, "message": "Please wait a few seconds between tests", "steps": []}, 429
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "message": "A test is already running", "steps": []}, 409
        try:
            self.last = time.time()
            cfg = self.store.raw()
            m = load_sniper_module(self.root)
            m.MODE = str(cfg["mode"]).strip().lower()
            m.API_KEY = str(cfg["scrapingant_key"]).strip() or os.environ.get("SCRAPINGANT_KEY", "").strip()
            if m.MODE == "scrapingant" and not m.API_KEY:
                return {"ok": False, "message": "ScrapingAnt mode needs an API key", "steps": []}, 200
            session = m.new_session()
            steps, t0 = [], time.time()
            fresh = "".join(random.choice(string.ascii_lowercase) for _ in range(24))
            plan = [("Random unregistered name", fresh, m.UNCLAIMED)]
            kt = str(cfg.get("known_taken", "")).strip()
            if kt:
                plan.append((f"Known taken name ({kt})", kt, m.CLAIMED))
            ok = True
            try:
                for label, name, expected in plan:
                    state, status = m.check(session, name, retries=1)
                    good = state == expected
                    ok &= good
                    steps.append({"label": label, "result": state, "http": status, "ok": good})
            except SystemExit:
                return {"ok": False, "message": "ScrapingAnt rejected the request - check the key / credits",
                        "steps": steps, "mode": m.MODE}, 200
            ms = int((time.time() - t0) * 1000)
            if ok:
                msg = "guns.lol reachable and detection works"
            else:
                first = steps[0]["result"] if steps else "error"
                msg = {"blocked": "guns.lol is blocking this server (bot protection)",
                       "ratelimit": "guns.lol is rate limiting this server",
                       "error": "Could not reach guns.lol"}.get(first, "Detection looks unreliable with this mode")
            return {"ok": ok, "message": msg, "steps": steps, "latency_ms": ms, "mode": m.MODE}, 200
        finally:
            self.lock.release()
