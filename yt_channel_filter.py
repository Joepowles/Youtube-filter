#!/usr/bin/env python3
"""
YouTube channel filter
======================

Drives a real Chrome window that is signed in to your child's YouTube account.

  1. Scans the account's watch history and lists every channel in it.
  2. Lets you pick channels to block.
  3. For each picked channel:
       - unsubscribes,
       - removes the like from every liked video by that channel,
       - clicks "Don't recommend channel" on one of its videos in a feed,
       - optionally gives a thumbs down to its newest videos,
       - optionally removes its videos from the watch history.

YouTube's official API does not expose watch history or "Don't recommend
channel", so this works through the website, the same way you would by hand.
YouTube changes its page layout from time to time; if a step stops working,
the script reports it and carries on with the next step.

Commands
--------
  python yt_channel_filter.py login          # open Chrome, sign in once
  python yt_channel_filter.py scan           # list channels in watch history
  python yt_channel_filter.py block          # pick channels and block them
  python yt_channel_filter.py block --channels "@SomeChannel,Other Channel"
  python yt_channel_filter.py block --from blocklist.txt
  python yt_channel_filter.py block --dry-run   # show what would happen

Run any command with --help for its options.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote_plus

try:
    from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PWTimeout
except ImportError:  # pragma: no cover
    sys.exit("Playwright is not installed. Run:  pip install playwright  (see README.md)")

HERE = Path(__file__).resolve().parent
DEFAULT_PROFILE = HERE / "yt-profile"
CHANNELS_FILE = HERE / "channels.json"
LOG_FILE = HERE / "block-log.jsonl"

YT = "https://www.youtube.com"
HISTORY_URL = f"{YT}/feed/history"
LIKED_URL = f"{YT}/playlist?list=LL"

# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------


@dataclass
class Channel:
    name: str
    url: str  # canonical path such as /@handle or /channel/UC...
    videos: list[str] = field(default_factory=list)  # watch-history video ids

    @property
    def key(self) -> str:
        return channel_key(self.url, self.name)


def channel_key(url: str | None, name: str | None) -> str:
    if url:
        return normalize_channel_path(url).lower()
    return "name:" + (name or "").strip().lower()


def normalize_channel_path(href: str) -> str:
    """Turn any channel link into a stable path like /@handle or /channel/UC..."""
    if not href:
        return ""
    path = urlparse(href).path if href.startswith("http") else href.split("?")[0]
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""
    if parts[0].startswith("@"):
        return "/" + parts[0]
    if parts[0] in ("channel", "c", "user") and len(parts) > 1:
        return f"/{parts[0]}/{parts[1]}"
    return "/" + parts[0]


def video_id(href: str) -> str | None:
    if not href:
        return None
    if "/shorts/" in href:
        return href.split("/shorts/")[1].split("?")[0].split("/")[0]
    q = parse_qs(urlparse(href if href.startswith("http") else YT + href).query)
    return q.get("v", [None])[0]


# --------------------------------------------------------------------------
# Browser
# --------------------------------------------------------------------------


def open_browser(p, args) -> tuple[BrowserContext, callable]:
    """Return (context, close_fn). Uses a saved Chrome profile, or an existing
    Chrome reached over the DevTools port when --cdp is given."""
    if args.cdp:
        browser = p.chromium.connect_over_cdp(args.cdp)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        return ctx, lambda: None  # leave the user's Chrome running

    Path(args.profile).mkdir(parents=True, exist_ok=True)
    launch = dict(
        user_data_dir=str(args.profile),
        headless=bool(getattr(args, "headless", False)),
        viewport={"width": 1280, "height": 900},
        # Google refuses sign-in from browsers that announce automation.
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled"],
        slow_mo=args.slow_mo,
    )
    try:
        ctx = p.chromium.launch_persistent_context(channel="chrome", **launch)
    except Exception as e:  # Chrome not installed: fall back to bundled Chromium
        print(f"(Google Chrome not found, using Playwright's Chromium: {e.__class__.__name__})")
        ctx = p.chromium.launch_persistent_context(**launch)
    return ctx, ctx.close


def get_page(ctx: BrowserContext) -> Page:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(15000)
    return page


def goto(page: Page, url: str):
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    dismiss_consent(page)


def dismiss_consent(page: Page):
    # EU/UK cookie consent screen
    for label in ("Reject all", "Accept all"):
        btn = page.get_by_role("button", name=label)
        if btn.count() and btn.first.is_visible():
            btn.first.click()
            page.wait_for_timeout(1500)
            return


def is_signed_in(page: Page) -> bool:
    goto(page, YT)
    return page.locator("button#avatar-btn, #avatar-btn").count() > 0


def scroll_until_stable(page: Page, item_selector: str, max_items: int, max_rounds: int = 200):
    last, still = -1, 0
    for _ in range(max_rounds):
        n = page.locator(item_selector).count()
        if n >= max_items:
            break
        if n == last:
            still += 1
            if still >= 4:
                break
        else:
            still = 0
        last = n
        page.mouse.wheel(0, 6000)
        page.keyboard.press("End")
        page.wait_for_timeout(1200)


# One generic extractor for history, liked playlist, home feed and sidebars.
VIDEO_ITEMS = (
    "ytd-video-renderer, ytd-playlist-video-renderer, ytd-rich-item-renderer, "
    "ytd-compact-video-renderer, yt-lockup-view-model, ytd-grid-video-renderer, "
    "ytd-reel-item-renderer, ytm-shorts-lockup-view-model, ytm-shorts-lockup-view-model-v2"
)

EXTRACT_JS = r"""
(sel) => {
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(sel)) {
    // skip outer wrappers that contain another matching item
    if (el.querySelector(sel)) continue;
    const vlink = el.querySelector('a[href*="/watch?v="], a[href*="/shorts/"]');
    if (!vlink) continue;
    const href = vlink.getAttribute('href');
    if (seen.has(href)) continue;
    seen.add(href);
    let ch = el.querySelector(
      'ytd-channel-name a, a[href^="/@"], a[href^="/channel/"], a[href^="/c/"], a[href^="/user/"]');
    let chName = ch ? ch.textContent.trim() : '';
    let chHref = ch ? ch.getAttribute('href') : '';
    if (!chName) {
      const n = el.querySelector('ytd-channel-name #text, .yt-content-metadata-view-model-wiz__metadata-text, yt-content-metadata-view-model span');
      chName = n ? n.textContent.trim() : '';
    }
    const t = el.querySelector('#video-title, h3, [title]');
    out.push({
      href,
      title: t ? (t.getAttribute('title') || t.textContent || '').trim() : '',
      channelName: chName,
      channelHref: chHref || '',
      idx: Array.prototype.indexOf.call(document.querySelectorAll(sel), el),
    });
  }
  return out;
}
"""


def extract_items(page: Page) -> list[dict]:
    return page.evaluate(EXTRACT_JS, VIDEO_ITEMS)


def item_matches(item: dict, ch: Channel) -> bool:
    """True if a page item (video or Short) belongs to the channel.

    When both sides have a channel link, the link decides: two different
    channels can share a display name. The name is only used when one side has
    no link, and known video ids cover Shorts, which show no channel at all in
    the history page."""
    if ch.videos and video_id(item.get("href", "")) in ch.videos:
        return True
    if item.get("channelHref") and ch.url:
        return normalize_channel_path(item["channelHref"]).lower() == ch.url.lower()
    name = (item.get("channelName") or "").strip().lower()
    return bool(name) and bool(ch.name) and name in (ch.name.strip().lower(), ch.name.strip().lower().lstrip("@"))


# --------------------------------------------------------------------------
# Menus and clicks (text-based so they survive layout changes)
# --------------------------------------------------------------------------

MENU_ITEM_SELECTORS = (
    "tp-yt-paper-item, ytd-menu-service-item-renderer, yt-list-item-view-model, "
    "ytd-menu-navigation-item-renderer, [role='menuitem'], [role='option']"
)


def click_menu_option(page: Page, text_re: re.Pattern, timeout_ms: int = 4000) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        items = page.locator(MENU_ITEM_SELECTORS).filter(has_text=text_re)
        for i in range(items.count()):
            it = items.nth(i)
            if it.is_visible():
                it.click()
                page.wait_for_timeout(800)
                return True
        page.wait_for_timeout(250)
    page.keyboard.press("Escape")
    return False


def open_item_menu(page: Page, index: int) -> bool:
    item = page.locator(VIDEO_ITEMS).nth(index)
    item.scroll_into_view_if_needed()
    item.hover()
    page.wait_for_timeout(300)
    btn = item.locator(
        "button[aria-label='Action menu'], button[aria-label='More actions'], "
        "ytd-menu-renderer yt-icon-button button, ytd-menu-renderer button, "
        "button-view-model button[aria-label*='ore']"
    )
    for i in range(btn.count()):
        b = btn.nth(i)
        if b.is_visible():
            b.click()
            page.wait_for_timeout(700)
            return True
    return False


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------


def scan_history(page: Page, max_items: int) -> list[Channel]:
    print(f"Reading watch history (up to {max_items} videos)...")
    goto(page, HISTORY_URL)
    if "accounts.google.com" in page.url:
        sys.exit("Not signed in. Run:  python yt_channel_filter.py login")
    scroll_until_stable(page, VIDEO_ITEMS, max_items)
    items = extract_items(page)
    channels: dict[str, Channel] = {}
    skipped = 0
    for it in items:
        if not (it["channelName"] or it["channelHref"]):
            skipped += 1  # mostly Shorts shelves, which show no channel
            continue
        ch = Channel(name=it["channelName"] or it["channelHref"], url=normalize_channel_path(it["channelHref"]))
        ch = channels.setdefault(ch.key, ch)
        vid = video_id(it["href"])
        if vid and vid not in ch.videos:
            ch.videos.append(vid)
    print(f"Found {len(items)} videos from {len(channels)} channels"
          + (f" ({skipped} items without a channel, usually Shorts, were skipped)" if skipped else ""))
    result = sorted(channels.values(), key=lambda c: (-len(c.videos), c.name.lower()))
    CHANNELS_FILE.write_text(json.dumps([asdict(c) for c in result], indent=2))
    print(f"Saved to {CHANNELS_FILE.name}")
    return result


def print_channels(channels: list[Channel]):
    w = len(str(len(channels)))
    for i, c in enumerate(channels, 1):
        print(f"  {str(i).rjust(w)}. {c.name}  ({len(c.videos)} watched)  {c.url}")


def parse_selection(text: str, n: int) -> list[int]:
    picked: list[int] = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            rng = range(min(a, b), max(a, b) + 1)
        elif part.isdigit():
            rng = [int(part)]
        else:
            raise ValueError(f"Not a number or range: {part!r}")
        for k in rng:
            if not 1 <= k <= n:
                raise ValueError(f"{k} is out of range 1-{n}")
            if k - 1 not in picked:
                picked.append(k - 1)
    return picked


def choose_interactively(channels: list[Channel]) -> list[Channel]:
    shown = channels
    while True:
        print()
        print_channels(shown)
        print("\nType numbers to block (e.g. 1,4,7-9), '/text' to filter the list, "
              "'*' to show all again, or press Enter to cancel.")
        ans = input("> ").strip()
        if not ans:
            return []
        if ans == "*":
            shown = channels
            continue
        if ans.startswith("/"):
            q = ans[1:].strip().lower()
            shown = [c for c in channels if q in c.name.lower() or q in c.url.lower()] or channels
            continue
        try:
            return [shown[i] for i in parse_selection(ans, len(shown))]
        except ValueError as e:
            print(f"  {e}")


def resolve_named(channels: list[Channel], names: list[str]) -> list[Channel]:
    out = []
    for raw in names:
        q = raw.strip()
        if not q or q.startswith("#"):
            continue
        ql = q.lower()
        hit = next((c for c in channels if c.name.lower() == ql or c.url.lower() in (ql, "/" + ql)), None)
        if hit is None:
            # Not in history: still allowed, identified by handle or URL.
            path = normalize_channel_path(q if ("/" in q or q.startswith("http")) else ("/" + q if q.startswith("@") else ""))
            hit = Channel(name=q, url=path)
            print(f"  note: {q!r} was not in the scanned history; acting on it anyway")
        out.append(hit)
    return out


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


def channel_of_video(page: Page, vid: str) -> tuple[str, str] | None:
    """Open a video (or Short) and return (channel name, channel path)."""
    goto(page, f"{YT}/watch?v={vid}")
    page.keyboard.press("k")
    sel = ("ytd-video-owner-renderer ytd-channel-name a, ytd-video-owner-renderer a[href^='/@'], "
           "ytd-video-owner-renderer a[href^='/channel/'], "
           "ytd-reel-player-overlay-renderer a[href^='/@'], reel-player-header-renderer a[href^='/@'], "
           "ytd-reel-video-renderer[is-active] a[href^='/@'], yt-reel-channel-bar-view-model a[href^='/@']")
    try:
        page.locator(sel).first.wait_for(state="attached", timeout=8000)
    except PWTimeout:
        return None
    links = page.locator(sel)
    for i in range(links.count()):
        a = links.nth(i)
        href = a.get_attribute("href") or ""
        name = (a.text_content() or "").strip()
        if href:
            return (name or href.strip("/"), normalize_channel_path(href))
    return None


def find_channel_url(page: Page, ch: Channel) -> str | None:
    if ch.url:
        return YT + ch.url
    if ch.videos:
        found = channel_of_video(page, ch.videos[0])
        if found:
            if not ch.name or ch.name == ch.videos[0]:
                ch.name = found[0]
            ch.url = found[1]
            return YT + ch.url
    return None


def unsubscribe(page: Page, ch: Channel, dry: bool) -> str:
    url = find_channel_url(page, ch)
    if not url:
        return "skipped (could not find the channel page)"
    goto(page, url)
    header = page.locator("#page-header, #channel-header, ytd-c4-tabbed-header-renderer, yt-page-header-renderer").first
    scope = header if header.count() else page
    subscribed = scope.get_by_role("button", name=re.compile(r"^(Subscribed|Unsubscribe)", re.I))
    if subscribed.count() == 0:
        subscribed = scope.locator("button").filter(has_text=re.compile(r"^\s*Subscribed\s*$", re.I))
    if subscribed.count() == 0 or not subscribed.first.is_visible():
        return "not subscribed"
    if dry:
        return "would unsubscribe"
    subscribed.first.click()
    page.wait_for_timeout(800)
    # New layout opens a menu (notifications / Unsubscribe); old layout goes straight to the dialog.
    click_menu_option(page, re.compile(r"^\s*Unsubscribe\s*$", re.I), timeout_ms=2500)
    confirm = page.locator(
        "yt-confirm-dialog-renderer #confirm-button button, yt-confirm-dialog-renderer #confirm-button, "
        "tp-yt-paper-dialog button"
    ).filter(has_text=re.compile("Unsubscribe", re.I))
    try:
        confirm.first.wait_for(state="visible", timeout=4000)
        confirm.first.click()
    except PWTimeout:
        return "FAILED (no confirm dialog; YouTube layout may have changed)"
    page.wait_for_timeout(1500)
    return "unsubscribed"


def liked_videos_for(page: Page, chans: list[Channel], max_items: int) -> dict[str, list[str]]:
    print(f"Reading liked videos (up to {max_items})...")
    goto(page, LIKED_URL)
    scroll_until_stable(page, VIDEO_ITEMS, max_items)
    items = extract_items(page)
    out: dict[str, list[str]] = {c.key: [] for c in chans}
    for it in items:
        for c in chans:
            if item_matches(it, c):
                vid = video_id(it["href"])
                if vid:
                    out[c.key].append(vid)
    print(f"  {len(items)} liked videos checked")
    return out


def unlike_video(page: Page, vid: str, dry: bool) -> str:
    goto(page, f"{YT}/watch?v={vid}")
    page.keyboard.press("k")  # pause playback (harmless if already paused)
    like = page.locator(
        "like-button-view-model button, #segmented-like-button button, "
        "ytd-toggle-button-renderer#like-button button"
    ).first
    try:
        like.wait_for(state="visible", timeout=10000)
    except PWTimeout:
        return "FAILED (like button not found)"
    if like.get_attribute("aria-pressed") != "true":
        return "not liked"
    if dry:
        return "would unlike"
    like.click()
    page.wait_for_timeout(1200)
    return "unliked" if like.get_attribute("aria-pressed") != "true" else "FAILED (still liked)"


DONT_RECOMMEND = re.compile(r"Don.?t recommend (this )?channel", re.I)


def try_dont_recommend_on(page: Page, ch: Channel, dry: bool) -> bool | None:
    """Look for one of the channel's videos on the current page and use its menu.
    Returns True on success, None if no matching video is on the page."""
    for _ in range(4):
        items = extract_items(page)
        for it in items:
            if item_matches(it, ch):
                if dry:
                    return True
                if open_item_menu(page, it["idx"]) and click_menu_option(page, DONT_RECOMMEND):
                    page.wait_for_timeout(1000)
                    return True
                page.keyboard.press("Escape")
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1200)
    return None


def dont_recommend(page: Page, ch: Channel, dry: bool) -> str:
    places = [("home feed", YT)]
    places += [("next to its video", f"{YT}/watch?v={v}") for v in ch.videos[:3]]
    places.append(("search results", f"{YT}/results?search_query={quote_plus(ch.name)}"))
    for label, url in places:
        goto(page, url)
        if "/watch" in url:
            page.keyboard.press("k")
        if try_dont_recommend_on(page, ch, dry):
            return ("would mark" if dry else "marked") + f" Don't recommend (via {label})"
    return ("skipped: none of its videos showed up with a \"Don't recommend channel\" option. "
            "Unsubscribing and unliking still reduce recommendations; rerun later to retry.")


def _remove_history_item(page: Page, index: int) -> bool:
    el = page.locator(VIDEO_ITEMS).nth(index)
    el.scroll_into_view_if_needed()
    el.hover()
    x = el.locator("button[aria-label*='Remove from' i]")
    if x.count() and x.first.is_visible():
        x.first.click()
        page.wait_for_timeout(900)
        return True
    if open_item_menu(page, index) and click_menu_option(page, re.compile(r"Remove from (watch )?history", re.I)):
        page.wait_for_timeout(900)
        return True
    page.keyboard.press("Escape")
    return False


def remove_channel_history(page: Page, ch: Channel, max_items: int, dry: bool, progress=None) -> tuple[int, int]:
    """Remove every video (and known Short) of one channel from watch history.

    First searches the history for the channel's name, which finds old videos
    quickly, then reads through the latest `max_items` videos to catch the
    rest. Returns (removed, could_not_remove); in a dry run "removed" counts
    what would be removed."""
    progress = progress or (lambda **kw: None)
    done: set[str] = set()
    removed = missed = 0

    def sweep(url: str, depth: int):
        nonlocal removed, missed
        goto(page, url)
        if "accounts.google.com" in page.url:
            raise RuntimeError("signed out of YouTube")
        scroll_until_stable(page, VIDEO_ITEMS, depth, max_rounds=80)
        failed: set[str] = set()  # retried by the next sweep, not this one
        for _ in range(3000):
            target = next((it for it in extract_items(page)
                           if (video_id(it["href"]) or it["href"]) not in done | failed and item_matches(it, ch)), None)
            if not target:
                break
            key = video_id(target["href"]) or target["href"]
            if dry or _remove_history_item(page, target["idx"]):
                done.add(key)
                removed += 1
                progress(n=removed, note=f"{removed} removed so far")
            else:
                failed.add(key)
                if len(failed) >= 3:
                    break  # the remove button isn't where we expect; stop trying on this page
        missed = len(failed)

    name = (ch.name or "").strip().lstrip("@")
    if name:
        progress(n=removed, note="Searching the history for this channel")
        sweep(f"{HISTORY_URL}?query={quote_plus(name)}", 300)
    progress(n=removed, note="Reading through the rest of the history")
    sweep(HISTORY_URL, max_items)  # its failures are the ones that count
    return removed, missed


def remove_from_history(page: Page, chans: list[Channel], max_items: int, dry: bool) -> dict[str, int]:
    """Older entry point kept for scripts: removes several channels' videos."""
    return {c.key: remove_channel_history(page, c, max_items, dry)[0] for c in chans}


DISLIKE_BUTTON = ("dislike-button-view-model button, #segmented-dislike-button button, "
                  "ytd-toggle-button-renderer#dislike-button button")


def channel_recent_videos(page: Page, ch: Channel, limit: int) -> list[str]:
    """Ids of the channel's newest regular videos, from its Videos tab."""
    url = find_channel_url(page, ch)
    if not url:
        return []
    goto(page, url.rstrip("/") + "/videos")
    scroll_until_stable(page, VIDEO_ITEMS, limit, max_rounds=15)
    out = []
    for it in extract_items(page):
        vid = video_id(it["href"]) if "/watch" in it["href"] else None
        if vid and vid not in out:
            out.append(vid)
    return out[:limit]


def dislike_video(page: Page, vid: str, dry: bool) -> str:
    goto(page, f"{YT}/watch?v={vid}")
    page.keyboard.press("k")
    btn = page.locator(DISLIKE_BUTTON).first
    try:
        btn.wait_for(state="visible", timeout=10000)
    except PWTimeout:
        return "FAILED (thumbs-down button not found)"
    if btn.get_attribute("aria-pressed") == "true":
        return "already"
    if dry:
        return "would dislike"
    btn.click()
    page.wait_for_timeout(1000)
    return "disliked" if btn.get_attribute("aria-pressed") == "true" else "FAILED (did not stick)"


SUBS_URL = f"{YT}/feed/channels"

SUBS_JS = r"""
() => {
  const out = [], seen = new Set();
  const rows = document.querySelectorAll('ytd-channel-renderer, yt-lockup-view-model, ytd-grid-channel-renderer');
  for (const el of rows) {
    const a = el.querySelector('a#main-link, a[href^="/@"], a[href^="/channel/"], a[href^="/c/"], a[href^="/user/"]');
    if (!a) continue;
    const href = a.getAttribute('href');
    if (!href || seen.has(href)) continue;
    seen.add(href);
    const n = el.querySelector('#channel-title #text, ytd-channel-name #text, #text, h3, [title]');
    const img = el.querySelector('img');
    out.push({ href, name: n ? (n.getAttribute('title') || n.textContent || '').trim() : '',
               avatar: img ? (img.getAttribute('src') || '') : '' });
  }
  return out;
}
"""


def read_subscriptions(page: Page, max_items: int = 500) -> list[dict] | None:
    """Channels the account is subscribed to, or None if signed out."""
    goto(page, SUBS_URL)
    if "accounts.google.com" in page.url:
        return None
    scroll_until_stable(page, "ytd-channel-renderer, yt-lockup-view-model, ytd-grid-channel-renderer",
                        max_items, max_rounds=40)
    out = []
    for it in page.evaluate(SUBS_JS):
        path = normalize_channel_path(it["href"])
        if path:
            avatar = it["avatar"] if it["avatar"].startswith("https://") else ""
            out.append({"name": (it["name"] or path.strip("/"))[:120], "url": path, "avatar": avatar[:400]})
    return out


def read_home(page: Page, max_items: int = 60) -> list[dict] | None:
    """What YouTube is suggesting on the homepage right now, or None if signed out."""
    goto(page, YT)
    if "accounts.google.com" in page.url or (
            page.locator("#avatar-btn").count() == 0 and page.locator("a[href*='ServiceLogin']").count() > 0):
        return None
    scroll_until_stable(page, VIDEO_ITEMS, max_items, max_rounds=4)
    out, seen = [], set()
    for it in extract_items(page):
        vid = video_id(it["href"])
        if not vid or vid in seen:
            continue
        seen.add(vid)
        out.append({"vid": vid, "short": "/shorts/" in it["href"], "title": it["title"][:200],
                    "channel": it["channelName"][:120], "url": normalize_channel_path(it["channelHref"])})
    return out[:max_items]


# --------------------------------------------------------------------------
# Shorts. YouTube has no "turn off Shorts" setting, so this does the two
# things it does allow: hide the Shorts shelf on the homepage ("Not
# interested", which YouTube undoes after about 30 days) and remove watched
# Shorts from the history so they stop feeding recommendations.
# --------------------------------------------------------------------------

SHORTS_SHELF = ("ytd-rich-shelf-renderer[is-shorts], ytd-reel-shelf-renderer, "
                "ytd-rich-section-renderer:has(ytm-shorts-lockup-view-model), "
                "ytd-rich-section-renderer:has(ytm-shorts-lockup-view-model-v2)")
SHELF_DISMISS = ("button[aria-label*='Not interested' i], button[aria-label*='Close' i], "
                 "button[aria-label*='Dismiss' i], #dismiss-button button")


def hide_shorts_shelf(page: Page, dry: bool = False) -> tuple[int, int]:
    """Click "Not interested" on Shorts shelves on the homepage.
    Returns (shelves hidden, shelves found)."""
    goto(page, YT)
    hidden = found = 0
    for _ in range(4):  # shelves can appear further down the feed
        shelves = page.locator(SHORTS_SHELF)
        for i in range(shelves.count()):
            shelf = shelves.nth(i)
            if not shelf.is_visible():
                continue
            found += 1
            if dry:
                hidden += 1
                continue
            shelf.scroll_into_view_if_needed()
            shelf.hover()
            btn = shelf.locator(SHELF_DISMISS)
            clicked = False
            for j in range(btn.count()):
                if btn.nth(j).is_visible():
                    btn.nth(j).click()
                    clicked = True
                    break
            if not clicked:  # newer layout: the shelf's own menu has "Not interested"
                menu = shelf.locator("button[aria-label*='More' i], button[aria-label*='Action menu' i]").first
                if menu.count() and menu.is_visible():
                    menu.click()
                    page.wait_for_timeout(600)
                    clicked = click_menu_option(page, re.compile(r"Not interested", re.I))
            if clicked:
                hidden += 1
                page.wait_for_timeout(1200)
        if found:
            break
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1200)
    return hidden, found


def remove_shorts_from_history(page: Page, max_items: int, dry: bool = False, progress=None) -> tuple[list[str], int]:
    """Remove watched Shorts from the history. Returns (ids removed, how many
    couldn't be removed). `progress` is called before each removal, so it can
    also be used to stop the job."""
    progress = progress or (lambda **kw: None)
    goto(page, HISTORY_URL)
    if "accounts.google.com" in page.url:
        raise RuntimeError("signed out of YouTube")
    scroll_until_stable(page, VIDEO_ITEMS, max_items, max_rounds=60)
    done: set[str] = set()
    failed: set[str] = set()
    removed: list[str] = []
    for _ in range(2000):
        progress(n=len(removed), note=f"{len(removed)} Shorts removed so far")
        target = next((it for it in extract_items(page)
                       if "/shorts/" in it["href"] and it["href"] not in done | failed), None)
        if not target:
            break
        if dry or _remove_history_item(page, target["idx"]):
            done.add(target["href"])
            vid = video_id(target["href"])
            if vid:
                removed.append(vid)
        else:
            failed.add(target["href"])
            if len(failed) >= 3:
                break
    return removed, len(failed)


# --------------------------------------------------------------------------
# Block one channel: every step, with progress reports
# --------------------------------------------------------------------------

# Order matters: liking, disliking and "Don't recommend" open video pages,
# which puts those videos back into the watch history, so history goes last.
STEPS = ["unsubscribe", "likes", "dislike", "dont_recommend", "history"]
STEP_NAMES = {"unsubscribe": "unsubscribe", "likes": "remove likes", "dislike": "thumbs down",
              "dont_recommend": "don't recommend channel", "history": "remove from watch history"}


class Cancelled(Exception):
    pass


def block_channel(page: Page, ch: Channel, steps: dict, *, liked_ids: list[str] | None = None,
                  dry: bool = False, max_history: int = 500, dislike_max: int = 30, report=None) -> dict:
    """Run the chosen steps for one channel.

    `steps` maps step names in STEPS to True/False. `report(step, **info)` is
    called as each step starts, progresses and ends; info has `state`
    (running, done, nothing, partial, failed) and optionally `n`, `total`,
    `note`. Returns {step: final info}."""
    report = report or (lambda step, **kw: None)
    results: dict[str, dict] = {}

    def do_unsubscribe():
        r = unsubscribe(page, ch, dry)
        if r in ("unsubscribed", "would unsubscribe"):
            return {"state": "done", "note": "Unsubscribed" + (" (test)" if dry else "")}
        if r == "not subscribed":
            return {"state": "nothing", "note": "Wasn't subscribed"}
        return {"state": "failed", "note": r}

    def do_likes():
        ids = liked_ids or []
        if not ids:
            return {"state": "nothing", "n": 0, "note": "No liked videos from this channel"}
        ok = bad = 0
        for i, vid in enumerate(ids, 1):
            report("likes", state="running", n=ok, total=len(ids), note=f"Video {i} of {len(ids)}")
            r = safe(lambda: unlike_video(page, vid, dry))
            if r.startswith("FAILED"):
                bad += 1
            elif r != "not liked":
                ok += 1
        st = "done" if not bad else ("partial" if ok else "failed")
        return {"state": st, "n": ok, "total": len(ids),
                "note": f"Removed {ok} like{'s' if ok != 1 else ''}" + (f", {bad} could not be changed" if bad else "")}

    def do_dislike():
        report("dislike", state="running", note="Finding their newest videos")
        vids = channel_recent_videos(page, ch, dislike_max)
        if not vids:  # Videos tab unreadable: fall back to ones we saw them watch
            vids = ch.videos[:dislike_max]
        if not vids:
            return {"state": "failed", "note": "Couldn't find any of their videos"}
        ok = bad = 0
        for i, vid in enumerate(vids, 1):
            report("dislike", state="running", n=ok, total=len(vids), note=f"Video {i} of {len(vids)}")
            r = safe(lambda: dislike_video(page, vid, dry))
            if r.startswith("FAILED"):
                bad += 1
            else:
                ok += 1
        st = "done" if not bad else ("partial" if ok else "failed")
        return {"state": st, "n": ok, "total": len(vids),
                "note": f"Thumbs down on {ok} video{'s' if ok != 1 else ''}" + (f", {bad} failed" if bad else "")}

    def do_dont_recommend():
        report("dont_recommend", state="running", note="Looking for one of their videos on the homepage")
        r = dont_recommend(page, ch, dry)
        if r.startswith(("marked", "would mark")):
            return {"state": "done", "note": "Marked \"Don't recommend channel\""}
        if r.startswith("skipped"):
            return {"state": "partial", "note": "None of their videos came up to mark. Run again later to retry."}
        return {"state": "failed", "note": r}

    def do_history():
        n, missed = remove_channel_history(page, ch, max_history, dry,
                                           progress=lambda **kw: report("history", state="running", **kw))
        if missed:
            return {"state": "partial" if n else "failed", "n": n,
                    "note": f"Removed {n}, but {missed} couldn't be removed. Run again to retry."}
        if n == 0:
            return {"state": "nothing", "n": 0, "note": "None of their videos were in the history"}
        return {"state": "done", "n": n, "note": f"Removed {n} video{'s' if n != 1 else ''} from the history"}

    fns = {"unsubscribe": do_unsubscribe, "likes": do_likes, "dislike": do_dislike,
           "dont_recommend": do_dont_recommend, "history": do_history}
    try:
        for step in STEPS:
            if not steps.get(step):
                continue
            report(step, state="running")
            try:
                res = fns[step]()
            except Cancelled:
                results[step] = {"state": "partial", "note": "Stopped part-way"}
                raise
            except Exception as e:
                res = {"state": "failed", "note": describe_error(e)}
            results[step] = res
            report(step, **res)
    except Cancelled as e:
        e.results = results  # what was done before the stop, for the caller to save
        raise
    return results


def log(entry: dict):
    entry["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_login(args):
    with sync_playwright() as p:
        ctx, close = open_browser(p, args)
        page = get_page(ctx)
        goto(page, YT)
        print("\nA Chrome window is open. Sign in to your child's YouTube account there\n"
              "(avatar at the top right should show their account), then come back here.")
        input("Press Enter when signed in... ")
        ok = is_signed_in(page)
        close()
    print("Signed in. The session is saved in", args.profile if not args.cdp else "your Chrome")
    if not ok:
        print("Warning: I could not see an account avatar. Run `login` again if later steps fail.")


def load_or_scan(page: Page, args) -> list[Channel]:
    if args.use_saved and CHANNELS_FILE.exists():
        data = json.loads(CHANNELS_FILE.read_text())
        return [Channel(**d) for d in data]
    return scan_history(page, args.max_history)


def cmd_scan(args):
    with sync_playwright() as p:
        ctx, close = open_browser(p, args)
        page = get_page(ctx)
        chans = scan_history(page, args.max_history)
        close()
    print()
    print_channels(chans)


def cmd_block(args):
    with sync_playwright() as p:
        ctx, close = open_browser(p, args)
        page = get_page(ctx)
        try:
            chans_all = load_or_scan(page, args)
            if args.channels:
                picked = resolve_named(chans_all, args.channels.split(","))
            elif args.from_file:
                picked = resolve_named(chans_all, Path(args.from_file).read_text().splitlines())
            else:
                picked = choose_interactively(chans_all)
            if not picked:
                print("Nothing selected.")
                return

            print("\nChannels to block:")
            for c in picked:
                print(f"  - {c.name} {c.url}")
            steps = {"unsubscribe": True, "likes": True, "dislike": args.dislike,
                     "dont_recommend": True, "history": args.remove_history}
            print("Steps:", ", ".join(STEP_NAMES[k] for k in STEPS if steps[k]),
                  "(DRY RUN, nothing will change)" if args.dry_run else "")
            if not args.yes and input("Go ahead? [y/N] ").strip().lower() != "y":
                print("Cancelled.")
                return

            liked = liked_videos_for(page, picked, args.max_liked)

            def report(step, state, note="", **kw):
                if state != "running" or note:
                    print(f"  {STEP_NAMES[step]}: {state}{' - ' + note if note else ''}")

            for c in picked:
                print(f"\n== {c.name} ==")
                res = block_channel(page, c, steps, liked_ids=liked.get(c.key, []), dry=args.dry_run,
                                    max_history=args.max_history, dislike_max=args.dislike_max, report=report)
                log({"channel": c.name, "url": c.url, "dry_run": args.dry_run, "steps": res})
            print(f"\nDone. A record of every action is in {LOG_FILE.name}.")
        finally:
            close()


def describe_error(e: Exception) -> str:
    msg = str(e).splitlines()[0][:120] if str(e) else ""
    return f"FAILED ({e.__class__.__name__}: {msg})"


def safe(fn):
    try:
        return fn()
    except Cancelled:
        raise
    except Exception as e:
        return describe_error(e)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Block YouTube channels found in watch history.")
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE), help="Chrome profile folder that keeps the sign-in")
    ap.add_argument("--cdp", help="Use an already-open Chrome instead, e.g. http://localhost:9222 (see README)")
    ap.add_argument("--slow-mo", type=int, default=150, help="ms pause between browser actions (default 150)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="Open Chrome so you can sign in once").set_defaults(fn=cmd_login)

    s = sub.add_parser("scan", help="List channels in watch history")
    s.add_argument("--max-history", type=int, default=500)
    s.set_defaults(fn=cmd_scan)

    b = sub.add_parser("block", help="Pick channels and block them")
    b.add_argument("--channels", help='Comma-separated names or handles, e.g. "@Foo,Bar Channel"')
    b.add_argument("--from", dest="from_file", help="Text file with one channel name or handle per line")
    b.add_argument("--use-saved", action="store_true", help="Use channels.json from the last scan instead of rescanning")
    b.add_argument("--remove-history", action="store_true", help="Also delete their videos from watch history")
    b.add_argument("--dislike", action="store_true", help="Also give a thumbs down to the channel's newest videos")
    b.add_argument("--dislike-max", type=int, default=30, help="How many of their newest videos to thumbs down")
    b.add_argument("--max-history", type=int, default=500)
    b.add_argument("--max-liked", type=int, default=1000)
    b.add_argument("--dry-run", action="store_true", help="Show what would change without changing anything")
    b.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation")
    b.set_defaults(fn=cmd_block)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
