# YouTube Filter

Block YouTube channels on your child's account properly: not just "Don't recommend", but a full clean-up of history, subscriptions, likes and recommendations, from a simple website the whole family can use.

YouTube has no real "block this channel" button for regular accounts. **YouTube Filter** builds one by doing, in a real signed-in Chrome browser, everything a parent would otherwise click through by hand. It also keeps watching your child's history, so a blocked channel that sneaks back in gets cleaned up again automatically.

> **Status:** early and experimental. YouTube changes its pages often, so some steps may stop working until the page selectors are updated. Every step reports whether it worked, so nothing fails silently.

## Features

- **One-click Block channel.** Each block runs these steps, and you can turn any of them off:
  1. Removes the channel's videos (including Shorts) from the watch history
  2. Unsubscribes
  3. Removes likes from the channel's videos
  4. Gives a thumbs down to the channel's newest videos (30 by default)
  5. Marks "Don't recommend channel"
- **Stays blocked.** The site checks each child's history every couple of minutes. If a blocked channel shows up again, it reruns the clean-up on its own.
- **Several children.** Each child gets a separate tab with their own browser profile and Google sign-in.
- **See what they watch.** Each child has Watched, Channels, Subscriptions, Suggested (their current homepage) and Blocked tabs.
- **Hide Shorts (optional).** Hides the Shorts shelf on the homepage and clears watched Shorts from history, then repeats every 6 hours.
- **Plain-language, phone-friendly UI.** Live progress with a Stop button, a "Needs another try" flag on any step that didn't work, plus Run again, Unblock and a Recent activity log.
- **Command-line tool** for scripted or scheduled use.

## How it works

YouTube's official API can't read watch history or use "Don't recommend channel", so the app drives Google Chrome with [Playwright](https://playwright.dev/) using a saved, signed-in browser profile for each child. Nothing is sent anywhere except YouTube, and all data stays on your own server.

```
phone / laptop ──► website (Flask + Waitress) ──► Chrome (one profile per child) ──► YouTube
                        │
                        └─ data/  sign-ins, history snapshots, blocked channels, activity log
```

## Quick start (website)

The recommended setup is a small Debian 12 container or VM. The full walkthrough is in **[WEBSITE.md](WEBSITE.md)**, covering a Proxmox container, the installer, an optional Cloudflare Tunnel and Cloudflare Access, adding children and automatic updates. In short:

```bash
git clone https://github.com/Joepowles/Youtube-filter.git /root/yt
bash /root/yt/deploy/install.sh        # Chrome, Python env, systemd service, auto-update timer
nano /opt/yt-filter/.env               # set AUTH_MODE and APP_PASSWORD
systemctl restart yt-filter
```

Then open `http://<server-ip>:8765`, sign in with your password, add a child, and press **Sign in to YouTube** to sign in to their Google account in a browser view inside the page.

### Configuration

Settings live in `/opt/yt-filter/.env`. See [.env.example](.env.example).

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUTH_MODE` | `password` in the example | `password` uses the site's own sign-in page. `cloudflare` requires Cloudflare Access. |
| `APP_PASSWORD` | | Family password for password mode (at least 8 characters) |
| `SESSION_DAYS` | `30` | How long a sign-in lasts on each device |
| `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` | | Cloudflare Access settings (cloudflare mode) |
| `ALLOWED_EMAILS` | | Optional list of Cloudflare Access emails that are allowed in |
| `PORT` | `8765` | Port the site listens on |
| `POLL_SECONDS` | `120` | How often each child's history is checked |
| `YT_HEADLESS` | `0` | Run Chrome without a window. Leave this at 0 on a server, because Chrome uses a virtual screen. |

## Command-line tool

`yt_channel_filter.py` works on its own on Windows, macOS or Linux with Python 3.9+ and Google Chrome.

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium                 # fallback if Chrome isn't found

python yt_channel_filter.py login                     # sign in to the child's account once
python yt_channel_filter.py scan                      # list channels in the watch history
python yt_channel_filter.py block --dry-run           # pick channels and preview the changes
python yt_channel_filter.py block --remove-history --dislike
```

`block` shows a numbered list of channels sorted by how often each was watched. Type `1,4,7-9` to pick from the list, or `/minecraft` to filter it. Without the menu:

```bash
python yt_channel_filter.py block --channels "@SomeHandle,Some Channel"
python yt_channel_filter.py block --from blocklist.txt -y    # one name or @handle per line
```

Other options: `--use-saved`, `--dislike-max N`, `--max-history N`, `--max-liked N`, `--slow-mo MS`, `--profile DIR`, and `--cdp URL` to attach to a Chrome you started yourself. That last one helps if Google says "This browser or app may not be secure". Each action is logged to `block-log.jsonl`.

## Security

Anyone who can use the site controls a browser that is signed in to your child's Google account, so treat access accordingly.

- Use a long random password (`openssl rand -base64 18`). Password mode locks out an address after 5 wrong attempts, and locks out everyone after 30 wrong attempts in an hour.
- If you expose the site to the internet, put it behind a Cloudflare Tunnel (no open router ports). Cloudflare Access adds a second lock in front of the password.
- Sign-in cookies are signed with a random per-install secret. Changing `APP_PASSWORD` signs out every device.
- `data/`, `.env` and browser profiles are git-ignored. Never commit or share them: a browser profile is as good as the account's password.

## Limitations

- **Page selectors can break.** The app relies on YouTube's page layout. When a step fails, it shows up as "Needs another try" and the details are in Recent activity.
- **Not a hard block.** A child can still search for a channel. For a supervised (Family Link) account, also block the channel in YouTube's parental controls. This app handles what those controls don't: history, subscriptions, likes and recommendations.
- **Shorts can't be turned off fully.** YouTube offers no off switch, and the hidden shelf comes back after about 30 days, which is why the job repeats.
- **Automation.** Automating a browser may go against YouTube's Terms of Service. Use it on accounts you manage, at your own risk.

## Project layout

```
webapp.py               website and background jobs (history polling, auto clean-up)
yt_channel_filter.py    browser automation and the command-line tool
static/index.html       the web UI
deploy/                 installer, systemd units, auto-update, Cloudflare Tunnel and sign-in helpers
WEBSITE.md              full server setup guide
```

## Disclaimer

This project isn't affiliated with or endorsed by YouTube or Google. YouTube is a trademark of Google LLC.
