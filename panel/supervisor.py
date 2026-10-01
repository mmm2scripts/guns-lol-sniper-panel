"""
Supervisor for the existing 67.py checker.

The original script is NOT re-implemented: it is launched as a child process (SNIPER_EVENTS=1 makes it
print machine-readable events next to its normal output). This module turns that output into
structured logs + counters and keeps everything in memory / on disk so the dashboard can disconnect and
reconnect at any time without affecting the running sniper.
"""
import collections
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
WEBHOOK_RE = re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\S+", re.I)
RESULT_LINE_RE = re.compile(r"^https?://\S+/\S+ - (?:\S+ )*?(?:unclaimed|claimed|blocked|ratelimit|error|Skipping)")
EVENT_PREFIX = "@@EVENT "
MAX_LINE = 600
MAX_LOGS = 5000
LOG_FILE_LIMIT = 4 * 1024 * 1024

END_REASONS = {
    "max_requests": "Reached max_requests - stopped to protect your quota",
    "customlist_done": "Custom list finished",
    "interrupted": "Stopped",
    "self_test_failed": "Start-up self-test failed - guns.lol did not give a usable answer",
}


def now():
    return time.time()


class Supervisor:
    def __init__(self, root, data_dir, script="67.py", secrets_fn=None):
        self.root = root
        self.data_dir = data_dir
        self.script = script
        self.secrets_fn = secrets_fn or (lambda: [])
        os.makedirs(data_dir, exist_ok=True)
        self.log_path = os.path.join(data_dir, "logs.jsonl")
        self.state_path = os.path.join(data_dir, "state.json")

        self.cond = threading.Condition()
        self.lock = threading.RLock()
        self.logs = collections.deque(maxlen=MAX_LOGS)
        self.next_id = 1
        self.epoch = 1                 # bumped when logs are cleared so clients reset their view

        self.proc = None
        self.state = "stopped"         # stopped | starting | running | stopping | error
        self.stop_requested = False
        self.started_at = None
        self.stats = self._blank_stats()
        self.current_username = None
        self.activity = "Idle"
        self.last_activity = None
        self.last_result = None
        self.guns = {"status": "unknown", "detail": "Not checked yet", "at": None}
        self.last_exit = None
        self.end_reason = None
        self.run_config = None
        self.desired = "stopped"
        self._last_save = 0
        self._load()

    # ------------------------------------------------------------------ persistence
    @staticmethod
    def _blank_stats():
        return {"attempts": 0, "available": 0, "taken": 0, "failed": 0}

    def _load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            self.stats.update({k: int(st.get("stats", {}).get(k, 0)) for k in self.stats})
            self.last_activity = st.get("last_activity")
            self.last_result = st.get("last_result")
            self.last_exit = st.get("last_exit")
            self.desired = st.get("desired", "stopped")
            self.guns = st.get("guns", self.guns)
            self.current_username = st.get("current_username")
        except (OSError, ValueError):
            pass
        try:
            with open(self.log_path, encoding="utf-8") as f:
                for line in f.readlines()[-MAX_LOGS:]:
                    try:
                        e = json.loads(line)
                        self.logs.append(e)
                        self.next_id = max(self.next_id, e["id"] + 1)
                    except (ValueError, KeyError):
                        continue
        except OSError:
            pass

    def _save_state(self, force=False):
        t = now()
        if not force and t - self._last_save < 5:
            return
        self._last_save = t
        data = {"stats": self.stats, "last_activity": self.last_activity, "last_result": self.last_result,
                "last_exit": self.last_exit, "desired": self.desired, "guns": self.guns,
                "current_username": self.current_username}
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # ------------------------------------------------------------------ logging
    def redact(self, text):
        text = ANSI_RE.sub("", text)
        text = WEBHOOK_RE.sub("[webhook hidden]", text)
        for s in self.secrets_fn():
            if s and len(s) >= 4:
                text = text.replace(s, "***")
        return text[:MAX_LINE]

    def log(self, level, event, msg):
        msg = self.redact(str(msg))
        with self.cond:
            entry = {"id": self.next_id, "ts": round(now(), 3), "level": level, "event": event, "msg": msg}
            self.next_id += 1
            self.logs.append(entry)
            self.last_activity = entry["ts"]
            try:
                if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > LOG_FILE_LIMIT:
                    keep = list(self.logs)[-1000:]
                    with open(self.log_path, "w", encoding="utf-8") as f:
                        for e in keep:
                            f.write(json.dumps(e) + "\n")
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError:
                pass
            self.cond.notify_all()
        return entry

    def get_logs(self, since=0, limit=300, wait=0):
        """Entries with id > since. since=0 -> the most recent `limit`. Long-polls up to `wait` seconds."""
        deadline = now() + wait
        with self.cond:
            while True:
                last_id = self.next_id - 1
                if since > last_id:      # client is ahead of us (e.g. state reset)
                    since = 0
                fresh = [e for e in self.logs if e["id"] > since]
                if fresh or wait <= 0 or now() >= deadline:
                    break
                self.cond.wait(timeout=max(0.1, min(deadline - now(), 25)))
            if since == 0 and len(fresh) > limit:
                fresh = fresh[-limit:]
            elif len(fresh) > limit:
                fresh = fresh[:limit]
            return {"entries": fresh, "last_id": self.next_id - 1, "epoch": self.epoch}

    def clear_logs(self):
        with self.cond:
            self.logs.clear()
            self.epoch += 1
            try:
                open(self.log_path, "w").close()
            except OSError:
                pass
            self.cond.notify_all()
        self.log("info", "info", "Logs cleared")

    # ------------------------------------------------------------------ process control
    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, resumed=False):
        with self.lock:
            if self.is_running():
                return False, "Sniper is already running"
            self.stop_requested = False
            self.state = "starting"
            self.stats = self._blank_stats()
            self.current_username = None
            self.activity = "Starting"
            self.started_at = now()
            self.last_exit = None
            self.end_reason = None
            self.desired = "running"
            self._save_state(force=True)
            self.log("info", "starting", "Resuming sniper after server restart" if resumed else "Starting sniper")
            env = dict(os.environ)
            env.update({"SNIPER_EVENTS": "1", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"})
            kwargs = {}
            if os.name == "posix":
                # make sure SIGINT (our graceful stop) is not inherited as "ignored"
                kwargs["preexec_fn"] = lambda: signal.signal(signal.SIGINT, signal.SIG_DFL)
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, "-u", self.script], cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", bufsize=1, **kwargs)
            except OSError as e:
                self.state = "error"
                self.desired = "stopped"
                self.activity = "Failed to start"
                self.log("error", "error", f"Could not launch sniper: {e}")
                return False, "Could not launch sniper"
            threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
            return True, "Starting"

    def stop(self, wait=False, keep_desired=False):
        with self.lock:
            proc = self.proc
            if not self.is_running():
                if not keep_desired:
                    self.desired = "stopped"
                    self._save_state(force=True)
                return False, "Sniper is not running"
            self.stop_requested = True
            self.state = "stopping"
            self.activity = "Stopping"
            if not keep_desired:
                self.desired = "stopped"
                self._save_state(force=True)
        self.log("info", "stopping", "Stopping sniper")
        try:
            if os.name == "posix":
                proc.send_signal(signal.SIGINT)
            else:
                proc.terminate()
        except OSError:
            pass
        t = threading.Thread(target=self._escalate, args=(proc,), daemon=True)
        t.start()
        if wait:
            t.join(timeout=20)
        return True, "Stopping"

    def _escalate(self, proc):
        for sig_name, delay in (("terminate", 8), ("kill", 4)):
            try:
                proc.wait(timeout=delay)
                return
            except subprocess.TimeoutExpired:
                try:
                    getattr(proc, sig_name)()
                except OSError:
                    pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    def restart(self):
        def work():
            self.log("info", "restarting", "Restarting sniper")
            if self.is_running():
                self.stop(wait=True)
                t0 = now()
                while self.is_running() and now() - t0 < 10:
                    time.sleep(0.2)
            time.sleep(0.5)
            self.start()
        threading.Thread(target=work, daemon=True).start()
        return True, "Restarting"

    def shutdown(self):
        """Server is exiting: stop the child but remember that it was meant to be running."""
        if self.is_running():
            self.stop(wait=True, keep_desired=True)

    # ------------------------------------------------------------------ output parsing
    def _reader(self, proc):
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if line.strip():
                    try:
                        self._handle_line(line)
                    except Exception as e:  # never let a parsing bug kill the reader
                        self.log("error", "error", f"Log parser error: {e}")
        finally:
            code = proc.wait()
            self._finished(proc, code)

    def _handle_line(self, line):
        if line.startswith(EVENT_PREFIX):
            try:
                self._handle_event(json.loads(line[len(EVENT_PREFIX):]))
            except ValueError:
                pass
            return
        clean = ANSI_RE.sub("", line).strip()
        if RESULT_LINE_RE.match(clean):
            return   # covered by structured result events
        low = clean.lower()
        if "rate limited" in low:
            self.guns = {"status": "rate_limited", "detail": clean, "at": now()}
            self.activity = "Rate limited - waiting"
            self.log("warning", "ratelimit", clean)
        elif "blocked by bot protection" in low:
            self.guns = {"status": "blocked", "detail": "Blocked by bot protection", "at": now()}
            self.activity = "Backing off (blocked)"
            self.log("warning", "blocked", clean)
        elif any(w in low for w in ("rejected the request", "traceback", "error", "could not", "didn't give", "aborting")):
            self.log("error", "error", clean)
        elif "self-test" in low or "mode:" in low or "config:" in low:
            self.log("info", "info", clean)
        else:
            self.log("info", "info", clean)

    def _handle_event(self, ev):
        kind = ev.get("event")
        with self.lock:
            if kind == "start":
                src = f"custom list ({ev.get('customlist_size')} names)" if ev.get("customlist") \
                    else f"random {ev.get('letter_count')}-char names"
                self.log("info", "starting", f"Mode {ev.get('mode')} | {src} | delay {ev.get('delay')}s | "
                                             f"max {ev.get('max_requests')} requests")
                self.run_config = ev
                self.activity = "Running self-test"
            elif kind == "connected":
                self.state = "running"
                self.activity = "Connected - scanning"
                self.guns = {"status": "connected", "detail": "Self-test passed", "at": now()}
                self.log("success", "connected", "Connected to guns.lol - self-test passed")
            elif kind == "disconnected":
                self.guns = {"status": "disconnected", "detail": ev.get("reason", ""), "at": now()}
                self.log("error", "disconnected", "Disconnected - could not get a usable answer from guns.lol")
            elif kind == "checking":
                self.current_username = ev.get("name")
                self.activity = f"Checking {ev.get('name')}"
                self.log("info", "checking",
                         f"Attempt #{self.stats['attempts'] + 1}: checking username {ev.get('name')}")
            elif kind == "result":
                self._result(ev)
            elif kind == "end":
                reason = ev.get("reason", "")
                self.end_reason = reason
                self.log("info" if reason != "self_test_failed" else "error", "end", END_REASONS.get(reason, f"Sniper finished ({reason})"))
            self._save_state()

    def _result(self, ev):
        name, state, status = ev.get("name"), ev.get("state"), ev.get("status")
        self.stats["attempts"] += 1
        n = self.stats["attempts"]
        self.last_result = {"name": name, "state": state, "at": now()}
        if state == "unclaimed":
            self.stats["available"] += 1
            self.guns = {"status": "connected", "detail": "Last check OK", "at": now()}
            self.log("success", "available", f"#{n} {name} - username available (HTTP {status})")
            self.log("success", "success", f"Success: {name} is free")
        elif state == "claimed":
            self.stats["taken"] += 1
            self.guns = {"status": "connected", "detail": "Last check OK", "at": now()}
            self.log("info", "unavailable", f"#{n} {name} - username unavailable (HTTP {status})")
        else:
            self.stats["failed"] += 1
            label = {"blocked": "blocked", "ratelimit": "rate limited", "error": "request failed"}.get(state, state)
            self.guns = {"status": {"blocked": "blocked", "ratelimit": "rate_limited"}.get(state, "error"),
                         "detail": f"Last check: {label}", "at": now()}
            self.log("warning" if state != "error" else "error", "error",
                     f"#{n} {name} - check failed: {label} (HTTP {status})")

    def _finished(self, proc, code):
        with self.lock:
            if proc is not self.proc:
                return
            requested = self.stop_requested
            self.current_username = None
            self.activity = "Idle"
            if self.end_reason == "self_test_failed" and not requested:
                self.state = "error"
                reason = "self_test_failed"
                self.log("error", "disconnected", "Disconnected - fix the connection problem, then start again")
            elif requested or code == 0:
                self.state = "stopped"
                reason = "stopped" if requested else "finished"
                self.log("info", "disconnected", "Disconnected - sniper stopped" if requested
                         else "Disconnected - sniper finished")
            else:
                self.state = "error"
                reason = "crashed"
                self.log("error", "error", f"Sniper exited unexpectedly (exit code {code})")
                self.log("error", "disconnected", "Disconnected - sniper process ended")
            self.guns = {"status": "disconnected", "detail": "Sniper not running", "at": now()}
            self.last_exit = {"reason": reason, "code": code, "at": now()}
            self.started_at = None
            if reason in ("finished", "crashed", "self_test_failed"):
                self.desired = "stopped"   # don't auto-resume something that ended by itself
            self._save_state(force=True)
            self.proc = None

    # ------------------------------------------------------------------ status
    def set_guns_status(self, status, detail):
        with self.lock:
            self.guns = {"status": status, "detail": detail, "at": now()}
            self._save_state(force=True)

    def status(self):
        with self.lock:
            running = self.is_running()
            uptime = int(now() - self.started_at) if running and self.started_at else 0
            return {
                "sniper": {
                    "state": self.state,
                    "running": running,
                    "started_at": self.started_at,
                    "uptime_seconds": uptime,
                    "mode": (self.run_config or {}).get("mode"),
                    "last_exit": self.last_exit,
                },
                "guns": dict(self.guns),
                "current_username": self.current_username,
                "activity": self.activity,
                "stats": dict(self.stats),
                "last_activity": self.last_activity,
                "last_result": self.last_result,
            }
