#!/usr/bin/env python3
"""
Web front end for yt_channel_filter.py.

Watches the YouTube history of one or more kids' accounts and blocks channels
from it. Each kid has their own Chrome profile (and sign-in) under data/<id>/.
A single worker thread owns the browsers and runs jobs one at a time; a poller
queues a quick history check for each signed-in kid every POLL_SECONDS.

Access is protected either by Cloudflare Access (AUTH_MODE=cloudflare, the
app verifies the Access token on every request) or by a password
(AUTH_MODE=password: a sign-in page sets a session cookie, with lockout after
wrong guesses).

Settings come from environment variables or a .env file next to this script:

  AUTH_MODE               "cloudflare" (default) or "password" for home-network use
  APP_PASSWORD            password for AUTH_MODE=password (asked for on the site's sign-in page)
  SESSION_DAYS            how long a sign-in on a phone or computer lasts (default 30)
  LISTEN                  address to listen on (default 127.0.0.1; 0.0.0.0 in password mode)
  CF_ACCESS_TEAM_DOMAIN   e.g. yourteam.cloudflareaccess.com        (cloudflare mode)
  CF_ACCESS_AUD           Application Audience (AUD) tag from Access (cloudflare mode)
  ALLOWED_EMAILS          comma-separated; optional extra check on the signed-in email
  PORT                    default 8765
  YT_HEADLESS             1 = run Chrome without a window (default 0)
  POLL_SECONDS            how often to check each kid's history (default 120)

Start:   python webapp.py
Local test without Cloudflare (never expose this):  python webapp.py --insecure-local
"""

from __future__ import annotations

import argparse
import contextlib
import re
import shutil
import uuid
import hashlib
import hmac
import io
import secrets
import json
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, abort, g, jsonify, make_response, request, send_file

import yt_channel_filter as ytf

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ACCOUNTS_FILE = DATA / "accounts.json"


def load_env_file():
    f = HERE / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_file()

app = Flask(__name__)
INSECURE = False

# --------------------------------------------------------------------------
# Cloudflare Access check
# --------------------------------------------------------------------------

_jwks = None


def verify_access():
    """Abort unless the request has a valid Cloudflare Access JWT."""
    if INSECURE:
        return {"email": "local-test"}
    if auth_mode() == "password":
        return verify_password()
    team = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").replace("https://", "").strip("/")
    aud = os.environ.get("CF_ACCESS_AUD", "").strip()
    if not team or not aud:
        abort(503, "Server is missing CF_ACCESS_TEAM_DOMAIN / CF_ACCESS_AUD settings.")
    token = request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get("CF_Authorization")
    if not token:
        abort(403, "Sign in through Cloudflare Access.")
    import jwt  # PyJWT

    global _jwks
    if _jwks is None:
        _jwks = jwt.PyJWKClient(f"https://{team}/cdn-cgi/access/certs")
    try:
        key = _jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=aud, issuer=f"https://{team}")
    except Exception:
        abort(403, "Invalid Cloudflare Access token.")
    allowed = [e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()]
    if allowed and claims.get("email", "").lower() not in allowed:
        abort(403, "This account is not allowed.")
    return claims


def auth_mode() -> str:
    return os.environ.get("AUTH_MODE", "cloudflare").strip().lower()


# Brute-force protection: 5 wrong passwords from one address locks it out for
# 15 minutes; 30 wrong passwords overall in an hour locks everyone out for 15.
FAIL_PER_IP, FAIL_GLOBAL, WINDOW, LOCKOUT = 5, 30, 3600, 900
_fails: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}
_fail_lock = threading.Lock()


def client_ip() -> str:
    # Behind the Cloudflare Tunnel, requests arrive from localhost and the
    # visitor's address is in CF-Connecting-IP. Only trust it from localhost.
    if request.remote_addr in ("127.0.0.1", "::1") and request.headers.get("CF-Connecting-IP"):
        return request.headers["CF-Connecting-IP"]
    return request.remote_addr or "?"


SESSION_COOKIE = "ytf_session"
_secret_cache: dict[str, bytes] = {}
_secret_lock = threading.Lock()


def session_key() -> bytes:
    """Signing key for sign-in cookies. Mixes a random per-install secret with
    the password, so changing APP_PASSWORD signs everyone out."""
    pw = os.environ.get("APP_PASSWORD", "")
    with _secret_lock:
        return _session_key_locked(pw)


def _session_key_locked(pw: str) -> bytes:
    if pw not in _secret_cache:
        f = DATA / ".session-secret"
        try:
            raw = f.read_text().strip()
        except FileNotFoundError:
            raw = ""
        if len(raw) < 32:
            raw = secrets.token_hex(32)
            DATA.mkdir(exist_ok=True)
            f.write_text(raw)
            os.chmod(f, 0o600)
        _secret_cache.clear()
        _secret_cache[pw] = hashlib.sha256((raw + "\0" + pw).encode()).digest()
    return _secret_cache[pw]


def make_session() -> tuple[str, int]:
    days = max(1, int(os.environ.get("SESSION_DAYS", "30")))
    exp = int(time.time()) + days * 86400
    sig = hmac.new(session_key(), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}", days * 86400


def session_ok(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    want = hmac.new(session_key(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, want)


def locked_out(ip: str) -> bool:
    with _fail_lock:
        return max(_locked_until.get(ip, 0), _locked_until.get("*", 0)) > time.time()


def password_matches(given: str) -> bool:
    """Check a password attempt, counting wrong ones toward the lockout."""
    want = os.environ.get("APP_PASSWORD", "")
    if want and given and hmac.compare_digest(given.encode(), want.encode()):
        return True
    ip, now = client_ip(), time.time()
    with _fail_lock:
        for key in (ip, "*"):
            hist = [t for t in _fails.get(key, []) if t > now - WINDOW] + [now]
            _fails[key] = hist
            if len(hist) >= (FAIL_PER_IP if key == ip else FAIL_GLOBAL):
                _locked_until[key] = now + LOCKOUT
                _fails[key] = []
                print(f"Locked out {key} after repeated wrong passwords", file=sys.stderr)
    time.sleep(1)
    return False


def verify_password():
    if session_ok(request.cookies.get(SESSION_COOKIE)):
        return {"email": "parent"}
    # Still accept HTTP basic auth (any username) for scripts and old bookmarks.
    auth = request.authorization
    if auth and auth.password:
        if locked_out(client_ip()):
            abort(make_response(jsonify(error="Too many wrong passwords. Try again in 15 minutes."), 429))
        if password_matches(auth.password):
            return {"email": "parent"}
    abort(make_response(jsonify(error="signed_out"), 401))


# Pages anyone may load in password mode: the page itself (it holds no data
# and shows the sign-in form) and the sign-in call.
PUBLIC_ENDPOINTS = {"index", "session_login", "session_logout"}


@app.before_request
def _guard():
    # Only accept JSON for changes, so a page on another site can't post forms here.
    if request.method == "POST" and not request.is_json:
        abort(415)
    if auth_mode() == "password" and not INSECURE and request.endpoint in PUBLIC_ENDPOINTS:
        g.user = {"email": None}
        return
    g.user = verify_access()


@app.post("/api/session")
def session_login():
    if auth_mode() != "password" or INSECURE:
        return jsonify(ok=True)
    if locked_out(client_ip()):
        return jsonify(error="Too many wrong passwords. Try again in 15 minutes."), 429
    given = str((request.get_json() or {}).get("password", ""))[:200]
    if not password_matches(given):
        return jsonify(error="That password isn't right."), 401
    token, max_age = make_session()
    resp = jsonify(ok=True)
    https = request.headers.get("X-Forwarded-Proto") == "https" or bool(request.headers.get("CF-Connecting-IP"))
    resp.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=https, samesite="Strict")
    return resp


@app.post("/api/logout")
def session_logout():
    resp = jsonify(ok=True)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.after_request
def _headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    if request.headers.get("X-Forwarded-Proto") == "https" or request.headers.get("CF-Connecting-IP"):
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp



# --------------------------------------------------------------------------
# Kids' accounts. Each has data/<id>/{profile/, feed.json, blocked.json, log.jsonl}
# --------------------------------------------------------------------------

store_lock = threading.RLock()


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def accounts() -> list[dict]:
    with store_lock:
        return read_json(ACCOUNTS_FILE, [])


def save_accounts(accts: list[dict]):
    with store_lock:
        write_json(ACCOUNTS_FILE, accts)


def get_account(aid: str) -> dict:
    for a in accounts():
        if a["id"] == aid:
            return a
    abort(404, "No such account.")


def update_account(aid: str, **changes):
    with store_lock:
        accts = accounts()
        for a in accts:
            if a["id"] == aid:
                a.update(changes)
        save_accounts(accts)


def adir(aid: str) -> Path:
    return DATA / aid


def migrate_single_account():
    """Move a pre-multi-account install (yt-profile/, blocked.json) into data/kid1."""
    if ACCOUNTS_FILE.exists():
        return
    DATA.mkdir(exist_ok=True)
    old_profile = HERE / "yt-profile"
    if old_profile.exists():
        d = adir("kid1")
        d.mkdir(parents=True, exist_ok=True)
        old_profile.rename(d / "profile")
        for name, new in (("blocked.json", "blocked.json"), ("block-log.jsonl", "log.jsonl")):
            if (HERE / name).exists():
                (HERE / name).rename(d / new)
        save_accounts([{"id": "kid1", "name": "Kid 1", "signed_in": None}])
    else:
        save_accounts([])


def feed(aid: str) -> list[dict]:
    with store_lock:
        return read_json(adir(aid) / "feed.json", [])


def blocked(aid: str) -> list[dict]:
    with store_lock:
        return read_json(adir(aid) / "blocked.json", [])


def snapshot(aid: str, what: str) -> dict:
    with store_lock:
        return read_json(adir(aid) / f"{what}.json", {"at": None, "items": []})


def same_channel(url: str | None, name: str | None, ch: ytf.Channel, vid: str | None = None) -> bool:
    """Known channel links decide (two channels can share a name); the name is
    only a fallback when a link is missing, and known video ids cover Shorts."""
    if vid and vid in ch.videos:
        return True
    if url and ch.url:
        return ytf.normalize_channel_path(url).lower() == ch.url.lower()
    return bool(name) and bool(ch.name) and name.strip().lower() == ch.name.strip().lower()


def row_channel(b: dict) -> ytf.Channel:
    return ytf.Channel(name=b.get("name") or "", url=b.get("url") or "", videos=list(b.get("vids") or []))


def find_row(rows: list[dict], ch: ytf.Channel) -> dict | None:
    return next((b for b in rows if ytf.channel_key(b.get("url"), b.get("name")) == ch.key
                 or same_channel(b.get("url"), b.get("name"), ch)), None)


def save_block_result(aid: str, ch: ytf.Channel, steps: dict, results: dict, auto: bool, create: bool = True):
    """Record a channel as blocked, with what each step achieved. With
    create=False (automatic and "run again" clean-ups) a channel that was
    unblocked in the meantime is left off the list."""
    now = time.time()
    with store_lock:
        rows = blocked(aid)
        row = find_row(rows, ch)
        if row is None:
            if not create:
                return
            row = {"name": ch.name, "url": ch.url, "blocked_at": time.strftime("%Y-%m-%d %H:%M"),
                   "blocked_ts": now, "steps": {}}
            rows.append(row)
        row.update(name=ch.name or row.get("name"), url=ch.url or row.get("url"), last_run=now,
                   wanted={k: bool(v) for k, v in steps.items()})
        vids = list(row.get("vids") or [])
        vids += [v for v in ch.videos if v not in vids]
        row["vids"] = vids[-300:]  # lets a rewatched Short (no channel shown) be recognised
        row.setdefault("steps", {})
        for k, v in results.items():
            row["steps"][k] = {**v, "at": now}
        if auto:
            row["auto_runs"] = row.get("auto_runs", 0) + 1
        write_json(adir(aid) / "blocked.json", rows)


# --------------------------------------------------------------------------
# Jobs and the worker thread
# --------------------------------------------------------------------------


class Job:
    def __init__(self, kind: str, account: str | None, params: dict, quiet: bool = False):
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.account, self.params, self.quiet = kind, account, params, quiet
        self.state = "queued"
        self.lines: list[str] = []
        self.progress: dict = {}
        self.cancel = False
        self.created, self.started, self.finished = time.time(), None, None

    def to_dict(self, lines=True):
        d = {"id": self.id, "kind": self.kind, "account": self.account, "state": self.state,
             "label": self.params.get("label", ""), "created": self.created, "auto": bool(self.params.get("auto")),
             "started": self.started, "finished": self.finished, "progress": self.progress}
        if lines:
            d["lines"] = self.lines[-400:]
        return d

    def check_cancel(self):
        if self.cancel:
            raise ytf.Cancelled()


class JobWriter(io.TextIOBase):
    def __init__(self, job: Job):
        self.job, self.buf = job, ""

    def write(self, s):
        sys.__stdout__.write(s)
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.job.lines.append(line)
        return len(s)

    def flush(self):
        if self.buf:
            self.job.lines.append(self.buf)
            self.buf = ""


job_queue: "queue.Queue[Job]" = queue.Queue()
all_jobs: list[Job] = []  # recent jobs, newest last
jobs_lock = threading.Lock()


def submit(kind: str, account: str | None, params: dict | None = None, quiet: bool = False) -> Job:
    with jobs_lock:
        job = Job(kind, account, params or {}, quiet)
        all_jobs.append(job)
        # keep the last 80, but never drop a job that hasn't finished
        old = [j for j in all_jobs[:-80] if j.state in ("queued", "running")]
        all_jobs[:] = old + all_jobs[-80:]
        job_queue.put(job)
    return job


def pending(account: str, kind: str | None = None) -> bool:
    with jobs_lock:
        return any(j.account == account and j.state in ("queued", "running") and (kind is None or j.kind == kind)
                   for j in all_jobs)


def browser_args(aid: str):
    return SimpleNamespace(
        cdp=None,
        profile=str(adir(aid) / "profile"),
        slow_mo=int(os.environ.get("YT_SLOW_MO", "100")),
        headless=os.environ.get("YT_HEADLESS", "0") == "1",
    )


class Browsers:
    """Keeps one Chrome open per kid so history checks are quick."""

    def __init__(self, pw):
        self.pw, self.open = pw, {}

    def page(self, aid: str):
        entry = self.open.get(aid)
        if entry:
            ctx, close = entry
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.evaluate("1")
                return page
            except Exception:
                self.close(aid)
        (adir(aid) / "profile").mkdir(parents=True, exist_ok=True)
        ctx, close = ytf.open_browser(self.pw, browser_args(aid))
        self.open[aid] = (ctx, close)
        return ytf.get_page(ctx)

    def close(self, aid: str):
        entry = self.open.pop(aid, None)
        if entry:
            try:
                entry[1]()
            except Exception:
                pass


def worker():
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browsers = Browsers(pw)
    while True:
        job = job_queue.get()
        if job.cancel:
            job.state, job.finished = "cancelled", time.time()
            continue
        job.state, job.started = "running", time.time()
        w = JobWriter(job)
        try:
            with contextlib.redirect_stdout(w):
                if job.kind == "delete":
                    browsers.close(job.account)
                    run_delete(job.account)
                else:
                    page = browsers.page(job.account)
                    ytf.LOG_FILE = adir(job.account) / "log.jsonl"
                    RUNNERS[job.kind](page, job.account, job.params, job)
            job.state = "done"
        except ytf.Cancelled:
            w.flush()
            job.lines.append("Stopped.")
            job.state = "cancelled"
        except Exception as e:
            w.flush()
            job.lines.append(f"Error: {e.__class__.__name__}: {str(e).splitlines()[0][:300] if str(e) else ''}")
            traceback.print_exc(file=sys.stderr)
            job.state = "failed"
            if job.account and job.kind != "delete":
                browsers.close(job.account)  # start fresh next time
                if job.kind == "poll":
                    update_account(job.account, poll_error=job.lines[-1], last_poll=time.time())
        finally:
            w.flush()
            job.finished = time.time()


def poller():
    interval = max(30, int(os.environ.get("POLL_SECONDS", "120")))
    while True:
        time.sleep(5)
        now = time.time()
        for a in accounts():
            if a.get("signed_in") is False or a.get("paused"):
                continue
            if pending(a["id"]):
                continue
            if a.get("hide_shorts") and a.get("signed_in"):
                since = now - (a.get("shorts_attempt_at") or 0)  # last attempt, even if stopped
                if since > SHORTS_EVERY or (a.get("shorts_pending") and since > SHORTS_SOON):
                    submit("shorts", a["id"], {"label": "Hiding Shorts"}, quiet=True)
                    continue
            if now - (a.get("last_poll") or 0) < interval:
                continue
            submit("poll", a["id"], {"label": "Checking history"}, quiet=True)


# --------------------------------------------------------------------------
# Job runners (run on the worker thread)
# --------------------------------------------------------------------------

SIGNED_OUT_HINT = "a[href*='accounts.google.com/ServiceLogin'], ytd-button-renderer a[href*='ServiceLogin']"


def read_history(page, max_items: int) -> list[dict] | None:
    """Return history items newest first, or None if the account is signed out."""
    ytf.goto(page, ytf.HISTORY_URL)
    if "accounts.google.com" in page.url:
        return None
    page.wait_for_timeout(1000)
    if page.locator("#avatar-btn").count() == 0 and page.locator(SIGNED_OUT_HINT).count() > 0:
        return None
    if max_items > 40:
        ytf.scroll_until_stable(page, ytf.VIDEO_ITEMS, max_items, max_rounds=60)
    out = []
    for it in ytf.extract_items(page)[:max_items]:
        vid = ytf.video_id(it["href"])
        if not vid:
            continue
        out.append({
            "vid": vid,
            "short": "/shorts/" in it["href"],
            "title": it["title"][:200],
            "channel": it["channelName"][:120],
            "url": ytf.normalize_channel_path(it["channelHref"]),
        })
    return out


def merge_feed(aid: str, items: list[dict]) -> list[dict]:
    """Merge freshly read history into the saved feed; return the new items."""
    now = time.time()
    with store_lock:
        old = feed(aid)
        known = {f["vid"]: f for f in old}
        merged, new = [], []
        for it in items:
            prev = known.pop(it["vid"], None)
            if prev:
                prev.update({k: v for k, v in it.items() if v})
                merged.append(prev)
            else:
                it["seen"] = now
                merged.append(it)
                new.append(it)
        merged += [f for f in old if f["vid"] in known]
        write_json(adir(aid) / "feed.json", merged[:600])
    return new


AUTO_STEPS = {"unsubscribe": True, "likes": True, "dislike": False, "dont_recommend": True, "history": True}
AUTO_GAP = 1800       # at most one automatic clean-up per channel every 30 minutes
QUIET_AFTER_RUN = 900  # ignore a channel's videos for 15 minutes after we ran (our own visits)


def protect(aid: str, new_items: list[dict]):
    """A blocked channel showed up in the history again: note it and clean up.

    Sightings that can't be cleaned up straight away (another clean-up is
    running, or this channel was cleaned up in the last 30 minutes) are kept
    as `cleanup_pending` and picked up by a later poll."""
    acct = get_account(aid)
    now = time.time()
    with store_lock:
        rows = blocked(aid)
        changed = False
        for it in new_items:
            for b in rows:
                if not same_channel(it.get("url"), it.get("channel"), row_channel(b), it.get("vid")):
                    continue
                if now - (b.get("last_run") or 0) < QUIET_AFTER_RUN:
                    continue  # most likely our own visit during the clean-up
                b["seen_again"] = b.get("seen_again", 0) + 1
                b["seen_again_at"] = now
                b["cleanup_pending"] = True
                changed = True
        if changed:
            write_json(adir(aid) / "blocked.json", rows)
    if acct.get("auto_cleanup") is False or pending(aid, "block"):
        return
    with store_lock:
        rows = blocked(aid)
        due = [b for b in rows if b.get("cleanup_pending") and now - (b.get("auto_at") or 0) > AUTO_GAP]
        if not due:
            return
        for b in due:
            b["auto_at"] = now
            b["cleanup_pending"] = False
        write_json(adir(aid) / "blocked.json", rows)
    chans = [{"name": b["name"], "url": b["url"]} for b in due]
    names = ", ".join(c["name"] for c in chans[:3])
    print(f"Blocked channel watched again ({names}); cleaning up.")
    submit("block", aid, {"channels": chans, "steps": AUTO_STEPS, "auto": True,
                          "label": f"Cleaning up {names} again"})


def run_poll(page, aid, params, job):
    deep = bool(params.get("deep"))
    items = read_history(page, 300 if deep else 40)
    if items is None:
        update_account(aid, signed_in=False, last_poll=time.time(), poll_error=None)
        print("This account is signed out. Use 'Sign in' to sign it in.")
        return
    first_run = not (adir(aid) / "feed.json").exists()  # a block can empty the feed later
    new = merge_feed(aid, items)
    # Don't flag a whole existing history as new, nor older videos that only
    # appeared because "Load older history" read further back.
    recent = set() if first_run else {it["vid"] for it in items[:40]}
    old = {it["vid"] for it in new if it["vid"] not in recent}
    if old:
        with store_lock:
            f = feed(aid)
            for x in f:
                if x["vid"] in old:
                    x["seen"] = 0
            write_json(adir(aid) / "feed.json", f)
    new = [it for it in new if it["vid"] not in old]
    protect(aid, new)  # also retries clean-ups that had to wait
    if get_account(aid).get("hide_shorts") and any(it.get("short") for it in new):
        print("New Shorts in the history; they'll be removed shortly.")
        update_account(aid, shorts_pending=True)  # the poller runs it once the 30-minute gap allows
    update_account(aid, signed_in=True, last_poll=time.time(), poll_error=None)
    print(f"History checked: {len(items)} videos, {len(new)} new.")


def run_check(page, aid, params, job):
    ok = ytf.is_signed_in(page)
    update_account(aid, signed_in=ok)
    print("Signed in to YouTube." if ok else "Not signed in. Use 'Sign in'.")


def run_snapshot(page, aid, params, job):
    what = params["what"]
    items = ytf.read_subscriptions(page) if what == "subs" else ytf.read_home(page)
    if items is None:
        update_account(aid, signed_in=False)
        print("This account is signed out.")
        return
    with store_lock:
        write_json(adir(aid) / f"{what}.json", {"at": time.time(), "items": items})
    print(f"Found {len(items)} {'subscriptions' if what == 'subs' else 'suggested videos'}.")


def run_block(page, aid, params, job):
    dry = bool(params.get("dry_run"))
    steps = {k: bool(params.get("steps", {}).get(k)) for k in ytf.STEPS}
    record = params.get("record", True)
    auto = bool(params.get("auto"))
    existing_only = auto or bool(params.get("reapply"))  # never re-add a channel that was unblocked
    job.progress = {"phase": "Getting ready", "channels": [], "current": None,
                    "steps": [k for k in ytf.STEPS if steps[k]]}

    known = feed(aid)
    picked: list[ytf.Channel] = []
    for c in params["channels"]:
        job.check_cancel()
        ch = ytf.Channel(name=c.get("name") or "", url=c.get("url") or "", videos=[v for v in [c.get("vid")] if v])
        if not ch.url and ch.videos:
            job.progress["phase"] = "Finding which channel made that video"
            print(f"Looking up the channel of video {ch.videos[0]}...")
            ytf.find_channel_url(page, ch)
            if not ch.url:
                print("  could not find the channel for that video; skipping")
                continue
        for f in known:  # every video from this channel we've seen in their history
            if f["vid"] not in ch.videos and same_channel(f.get("url"), f.get("channel"), ch):
                ch.videos.append(f["vid"])
        row = find_row(blocked(aid), ch)
        for v in (row or {}).get("vids") or []:  # and ones remembered from earlier clean-ups
            if v not in ch.videos:
                ch.videos.append(v)
        picked.append(ch)
    if not picked:
        raise RuntimeError("Couldn't work out which channel that is.")
    job.progress["channels"] = [{"name": c.name, "url": c.url,
                                 "steps": {k: {"state": "waiting"} for k in ytf.STEPS if steps[k]}} for c in picked]
    print(("TEST RUN, nothing will change. " if dry else "") + "Channels: " + ", ".join(c.name for c in picked))

    liked: dict[str, list[str]] = {}
    if steps["likes"]:
        job.progress["phase"] = "Looking through their liked videos"
        liked = ytf.liked_videos_for(page, picked, int(params.get("max_liked", 1000)))
        job.check_cancel()

    for i, ch in enumerate(picked):
        job.progress["current"] = i
        job.progress["phase"] = None
        slot = job.progress["channels"][i]["steps"]

        def report(step, state, **info):
            job.check_cancel()
            slot[step] = {"state": state, **info}
            if state != "running":
                print(f"  {ytf.STEP_NAMES[step]}: {info.get('note') or state}")

        if existing_only and find_row(blocked(aid), ch) is None:
            print(f"\n{ch.name} was unblocked, so it's skipped.")
            for k in slot:
                slot[k] = {"state": "nothing", "note": "Skipped: unblocked"}
            continue
        print(f"\n== {ch.name} ==")
        try:
            results = ytf.block_channel(page, ch, steps, liked_ids=liked.get(ch.key, []), dry=dry,
                                        max_history=int(params.get("max_history", 500)),
                                        dislike_max=int(params.get("dislike_max", 30)), report=report)
        except ytf.Cancelled as e:
            # keep what was already done, so it shows on the Blocked tab and can be run again
            if record and not dry and getattr(e, "results", None):
                save_block_result(aid, ch, steps, e.results, auto, create=not existing_only)
            raise
        ytf.log({"channel": ch.name, "url": ch.url, "dry_run": dry, "via": "auto" if auto else "web",
                 "steps": results})
        if dry:
            continue
        if record:
            save_block_result(aid, ch, steps, results, auto, create=not existing_only)
        with store_lock:
            f = feed(aid)
            for x in f:  # fill in channel details on Shorts we just looked up
                if not x.get("url") and x["vid"] in ch.videos:
                    x["url"], x["channel"] = ch.url, ch.name
            if results.get("history", {}).get("state") in ("done", "nothing"):
                f = [x for x in f if not same_channel(x.get("url"), x.get("channel"), ch)]
            write_json(adir(aid) / "feed.json", f)
            if results.get("unsubscribe", {}).get("state") in ("done", "nothing"):
                subs = snapshot(aid, "subs")
                subs["items"] = [x for x in subs["items"] if not same_channel(x.get("url"), x.get("name"), ch)]
                write_json(adir(aid) / "subs.json", subs)
            if results.get("dont_recommend", {}).get("state") == "done":
                home = snapshot(aid, "home")
                home["items"] = [x for x in home["items"] if not same_channel(x.get("url"), x.get("channel"), ch)]
                write_json(adir(aid) / "home.json", home)
    job.progress["current"] = None
    job.progress["phase"] = "Finished"
    print("\nDone." + (" (test run)" if dry else ""))


SHORTS_EVERY = 6 * 3600     # re-hide the Shorts shelf and clear Shorts from history this often
SHORTS_SOON = 1800          # but not more often than this when new Shorts show up in the history


def run_shorts(page, aid, params, job):
    """Hide the Shorts shelf on the homepage and remove Shorts from the history."""
    # Record the attempt first, so a stopped or failed run isn't retried at once.
    update_account(aid, shorts_attempt_at=time.time(), shorts_pending=False)
    status = {"at": time.time()}
    job.progress = {"phase": "Hiding the Shorts shelf on the homepage"}
    try:
        hidden, found = ytf.hide_shorts_shelf(page)
        if hidden:
            status["shelf"] = {"state": "done", "note": "Shorts shelf hidden on the homepage"}
        elif found:
            status["shelf"] = {"state": "partial", "note": "Found the Shorts shelf but couldn't hide it"}
        else:
            status["shelf"] = {"state": "nothing", "note": "No Shorts shelf on the homepage right now"}
    except ytf.Cancelled:
        raise
    except Exception as e:
        status["shelf"] = {"state": "failed", "note": ytf.describe_error(e)}
    print("Shorts shelf:", status["shelf"]["note"])
    job.check_cancel()
    job.progress = {"phase": "Removing Shorts from their watch history"}
    try:
        removed_ids, missed = ytf.remove_shorts_from_history(
            page, int(params.get("max_history", 300)),
            progress=lambda **kw: (job.check_cancel(), job.progress.update(phase="Removing Shorts from their watch history: " + kw.get("note", ""))))
        n = len(removed_ids)
        if missed:
            status["history"] = {"state": "partial", "n": n, "note": f"Removed {n} Shorts, {missed} couldn't be removed"}
        else:
            status["history"] = {"state": "done" if n else "nothing", "n": n,
                                 "note": f"Removed {n} Shorts from the history" if n else "No Shorts in the history"}
        status["history"]["ids"] = removed_ids  # taken off again before saving
    except ytf.Cancelled:
        raise
    except Exception as e:
        status["history"] = {"state": "failed", "note": ytf.describe_error(e)}
    print("Shorts in history:", status["history"]["note"])
    gone = set(status["history"].pop("ids", []))
    update_account(aid, shorts_status=status)
    if gone:
        with store_lock:
            write_json(adir(aid) / "feed.json", [x for x in feed(aid) if x["vid"] not in gone])


def run_delete(aid):
    with store_lock:
        save_accounts([a for a in accounts() if a["id"] != aid])
        shutil.rmtree(adir(aid), ignore_errors=True)
    print("Account removed.")


# ---- Remote sign-in: the page shows screenshots of the kid's Chrome and sends
# clicks and keystrokes back. Runs on the worker thread like everything else.

login_cmds: "queue.Queue[tuple[str, dict]]" = queue.Queue()
login_state = {"active": False, "account": None, "jpeg": None, "seq": 0}
SIGNIN_URL = ("https://accounts.google.com/ServiceLogin?service=youtube"
              "&continue=https%3A%2F%2Fwww.youtube.com%2Fsignin%3Faction_handle_signin%3Dtrue%26next%3D%252F")


def run_login(page, aid, params, job):
    while not login_cmds.empty():
        login_cmds.get_nowait()
    print("Opening the Google sign-in page.")
    page.goto(SIGNIN_URL, wait_until="domcontentloaded")
    login_state.update(active=True, account=aid)
    deadline = time.time() + 20 * 60
    try:
        while time.time() < deadline:
            try:
                kind, arg = login_cmds.get(timeout=0.5)
            except queue.Empty:
                kind, arg = None, None
            try:
                if kind == "done":
                    break
                if kind == "click":
                    page.mouse.click(float(arg["x"]), float(arg["y"]))
                elif kind == "type":
                    page.keyboard.type(str(arg["text"]), delay=35)
                elif kind == "key":
                    page.keyboard.press(str(arg["key"]))
                elif kind == "scroll":
                    page.mouse.wheel(0, float(arg["dy"]))
                elif kind == "back":
                    page.go_back()
                if kind:
                    page.wait_for_timeout(350)
                shot = page.screenshot(type="jpeg", quality=70)
                if shot != login_state["jpeg"]:
                    login_state["jpeg"] = shot
                    login_state["seq"] += 1
            except Exception as e:  # keep going through navigation hiccups
                print(f"  ({e.__class__.__name__}, retrying)")
                page.wait_for_timeout(500)
        else:
            print("Sign-in window timed out after 20 minutes.")
    finally:
        login_state.update(active=False, account=None, jpeg=None)
    ok = ytf.is_signed_in(page)
    update_account(aid, signed_in=ok, last_poll=0)
    print("Signed in. Their history will appear in a moment." if ok else "Not signed in yet. Try 'Sign in' again.")


RUNNERS = {"poll": run_poll, "check": run_check, "block": run_block, "login": run_login, "snapshot": run_snapshot,
           "shorts": run_shorts}


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/")
def index():
    return send_file(HERE / "static" / "index.html")


@app.get("/api/state")
def state():
    aid = request.args.get("account")
    accts = accounts()
    with jobs_lock:
        active = [j.to_dict(lines=False) for j in all_jobs if j.state in ("queued", "running")]
        recent = [j for j in reversed(all_jobs) if j.state in ("done", "failed", "cancelled")
                  and (not j.quiet or j.state == "failed") and (aid is None or j.account in (aid, None))][:12]
        recent = [j.to_dict(lines=True) for j in recent]
    out = {
        "user": g.user.get("email"),
        "auth": "off" if INSECURE else auth_mode(),
        "accounts": accts,
        "poll_seconds": max(30, int(os.environ.get("POLL_SECONDS", "120"))),
        "login": {"active": login_state["active"], "account": login_state["account"]},
        "active_jobs": active,
        "recent_jobs": recent,
    }
    if aid and any(a["id"] == aid for a in accts):
        out["feed"] = feed(aid)
        out["blocked"] = blocked(aid)
        out["subs"] = snapshot(aid, "subs")
        out["home"] = snapshot(aid, "home")
    return jsonify(out)


@app.post("/api/accounts")
def add_account():
    name = str((request.get_json() or {}).get("name", "")).strip()[:40]
    if not name:
        return jsonify(error="Give the account a name."), 400
    aid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "kid"
    taken = {a["id"] for a in accounts()}
    base, n = aid, 2
    while aid in taken:
        aid, n = f"{base}-{n}", n + 1
    with store_lock:
        save_accounts(accounts() + [{"id": aid, "name": name, "signed_in": None}])
    adir(aid).mkdir(parents=True, exist_ok=True)
    return jsonify(id=aid)


@app.post("/api/accounts/<aid>/rename")
def rename_account(aid):
    get_account(aid)
    name = str((request.get_json() or {}).get("name", "")).strip()[:40]
    if not name:
        abort(400)
    update_account(aid, name=name)
    return jsonify(ok=True)


@app.post("/api/accounts/<aid>/settings")
def account_settings(aid):
    get_account(aid)
    body = request.get_json() or {}
    if "auto_cleanup" in body:
        update_account(aid, auto_cleanup=bool(body["auto_cleanup"]))
    if "hide_shorts" in body:
        on = bool(body["hide_shorts"])
        update_account(aid, hide_shorts=on)
        if on and not pending(aid, "shorts"):
            submit("shorts", aid, {"label": "Hiding Shorts"})
        if not on:  # stop anything already queued or running
            with jobs_lock:
                for j in all_jobs:
                    if j.account == aid and j.kind == "shorts" and j.state in ("queued", "running"):
                        j.cancel = True
                        if j.state == "queued":
                            j.state, j.finished = "cancelled", time.time()
    return jsonify(ok=True)


@app.post("/api/accounts/<aid>/delete")
def delete_account(aid):
    get_account(aid)
    submit("delete", aid, {"label": "Removing account"})
    return jsonify(ok=True)


@app.post("/api/accounts/<aid>/refresh")
def refresh_account(aid):
    a = get_account(aid)
    deep = bool((request.get_json() or {}).get("deep"))
    if not pending(aid, "poll"):
        if a.get("signed_in") is False:
            update_account(aid, signed_in=None)
        submit("poll", aid, {"deep": deep, "label": "Loading older history" if deep else "Checking history"},
               quiet=not deep)
    return jsonify(ok=True)


@app.post("/api/accounts/<aid>/snapshot")
def refresh_snapshot(aid):
    get_account(aid)
    what = (request.get_json() or {}).get("what")
    if what not in ("subs", "home"):
        abort(400)
    with jobs_lock:
        dup = any(j.account == aid and j.kind == "snapshot" and j.params.get("what") == what
                  and j.state in ("queued", "running") for j in all_jobs)
    if not dup:
        submit("snapshot", aid, {"what": what, "label": "Reading subscriptions" if what == "subs"
                                 else "Reading the homepage suggestions"}, quiet=True)
    return jsonify(ok=True)


def clean_channel(c: dict) -> dict:
    return {"name": str(c.get("name", ""))[:120], "url": ytf.normalize_channel_path(str(c.get("url", ""))),
            "vid": re.sub(r"[^A-Za-z0-9_-]", "", str(c.get("vid", "")))[:20]}


@app.post("/api/accounts/<aid>/block")
def block_channels(aid):
    get_account(aid)
    body = request.get_json() or {}
    chans = []
    if body.get("reapply"):
        rows = blocked(aid)
        keys = set(body.get("keys") or [])
        for b in rows:
            if not keys or ytf.channel_key(b.get("url"), b.get("name")) in keys:
                chans.append({"name": b["name"], "url": b["url"], "vid": ""})
    for c in body.get("channels") or []:
        chans.append(clean_channel(c))
    chans = [c for c in chans if c.get("url") or c.get("vid")]
    if not chans:
        return jsonify(error="No channel given."), 400
    want = body.get("steps")
    if not isinstance(want, dict):  # default: everything except thumbs-down
        want = {k: k != "dislike" for k in ytf.STEPS}
    steps = {k: bool(want.get(k)) for k in ytf.STEPS}
    if not any(steps.values()):
        return jsonify(error="Pick at least one thing to do."), 400
    record = body.get("record", True) is not False
    names = ", ".join(c["name"] or "channel" for c in chans[:3]) + ("..." if len(chans) > 3 else "")
    if not record and steps == {k: k == "unsubscribe" for k in ytf.STEPS}:
        label = "Unsubscribing from " + names
    elif body.get("reapply"):
        label = "Running the clean-up again for " + names
    else:
        label = "Blocking " + names
    job = submit("block", aid, {
        "channels": chans, "steps": steps, "record": record, "dry_run": bool(body.get("dry_run")),
        "reapply": bool(body.get("reapply")),
        "dislike_max": max(1, min(100, int(body.get("dislike_max") or 30))),
        "label": ("Test run: " if body.get("dry_run") else "") + label,
    })
    return jsonify(job=job.to_dict(lines=False))


@app.post("/api/jobs/<jid>/cancel")
def cancel_job(jid):
    with jobs_lock:
        job = next((j for j in all_jobs if j.id == jid), None)
        if not job:
            abort(404)
        if job.state == "queued":
            job.cancel = True
            job.state, job.finished = "cancelled", time.time()
        elif job.state == "running" and job.kind in ("block", "shorts"):
            job.cancel = True
    return jsonify(ok=True)


@app.post("/api/accounts/<aid>/forget")
def forget_blocked(aid):
    """Remove channels from the saved blocklist (does not resubscribe)."""
    get_account(aid)
    keys = set((request.get_json() or {}).get("keys") or [])
    with store_lock:
        write_json(adir(aid) / "blocked.json",
                   [b for b in blocked(aid) if ytf.channel_key(b.get("url"), b.get("name")) not in keys])
    return jsonify(ok=True)


@app.post("/api/accounts/<aid>/login")
def login_start(aid):
    get_account(aid)
    if login_state["active"] or pending(aid, "login"):
        return jsonify(error="A sign-in window is already open."), 409
    submit("login", aid, {"label": "Signing in"})
    return jsonify(ok=True)


@app.get("/api/login/screen")
def login_screen():
    if not login_state["active"] or not login_state["jpeg"]:
        return ("", 204)
    if request.args.get("after") == str(login_state["seq"]):
        return ("", 204)
    return app.response_class(login_state["jpeg"], mimetype="image/jpeg",
                              headers={"X-Seq": str(login_state["seq"])})


@app.post("/api/login/input")
def login_input():
    if not login_state["active"]:
        return jsonify(error="The sign-in window is not open."), 409
    body = request.get_json() or {}
    kind = body.get("kind")
    allowed_keys = {"Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp", "Shift+Tab", "Space"}
    if kind not in ("click", "type", "key", "scroll", "back", "done"):
        abort(400)
    if kind == "key" and body.get("key") not in allowed_keys:
        abort(400)
    if kind == "type" and len(str(body.get("text", ""))) > 500:
        abort(400)
    login_cmds.put((kind, body))
    return jsonify(ok=True)


def main():
    global INSECURE
    ap = argparse.ArgumentParser()
    ap.add_argument("--insecure-local", action="store_true", help="Skip the sign-in check (local testing only)")
    a = ap.parse_args()
    INSECURE = a.insecure_local
    mode = auth_mode()
    if INSECURE:
        pass
    elif mode == "password":
        if len(os.environ.get("APP_PASSWORD", "")) < 8:
            sys.exit("Set APP_PASSWORD (8+ characters) in .env first.")
    elif not (os.environ.get("CF_ACCESS_TEAM_DOMAIN") and os.environ.get("CF_ACCESS_AUD")):
        sys.exit("Set CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD in .env, or AUTH_MODE=password (see WEBSITE.md).")
    host = os.environ.get("LISTEN") or ("0.0.0.0" if mode == "password" and not INSECURE else "127.0.0.1")
    migrate_single_account()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    port = int(os.environ.get("PORT", "8765"))
    print(f"Listening on http://{host}:{port}  (auth: {'OFF' if INSECURE else mode})")
    from waitress import serve
    serve(app, host=host, port=port, threads=6)


if __name__ == "__main__":
    main()
