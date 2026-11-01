# Running the filter as a website on a Proxmox host

The website runs in a small Debian container on a Proxmox host, is published at your own address (for example **https://yt.example.com**) through a Cloudflare Tunnel, and sits behind Cloudflare Access so only your email can sign in. No ports are opened on your router.

```
browser ──► Cloudflare Access (sign-in) ──► Cloudflare Tunnel ──► container on a Proxmox host
                                                                   ├─ cloudflared
                                                                   └─ web app (127.0.0.1:8765) ─► Chrome ─► YouTube
```

The app also checks the Cloudflare Access token on every request itself, so it refuses anything that didn't come through Access.

## Home network only (no Cloudflare)

To use the site only from home at http://<container-ip>:8765, protected by a password:

```bash
systemctl disable --now cloudflared          # take the site off the internet
nano /opt/yt-filter/.env                     # set these two lines:
#   AUTH_MODE=password
#   APP_PASSWORD=<a password of 8+ characters>
systemctl restart yt-filter
```

The site shows its own sign-in page asking for that password. Steps 3 and 4 below are then not needed. To go back to Cloudflare later, set `AUTH_MODE=cloudflare`, fill in the Access values, and run `systemctl enable --now cloudflared`.

## Public at yt.example.com with a password only (no Cloudflare Access)

Same as the home-network setup above, but with the tunnel left on. Cloudflare provides HTTPS; the app asks for the password, locks an address out for 15 minutes after 5 wrong tries, and locks everyone out for 15 minutes after 30 wrong tries in an hour.

```bash
openssl rand -base64 18                      # a strong password; save it in your password manager
nano /opt/yt-filter/.env                     # AUTH_MODE=password and APP_PASSWORD=<that password>
systemctl restart yt-filter
systemctl enable --now cloudflared
```

Anyone with the password controls a browser signed in to your child's Google account, so use a long random password. Adding Cloudflare Access later (step 4) puts a second lock in front of it.

## 1. Create a container on a Proxmox host (about 5 minutes)

Don't install it on the Proxmox host itself. In the Proxmox web UI (https://<proxmox-ip>:8006):

1. Download the Debian 12 template if you don't have it: **local storage > CT Templates > Templates > debian-12-standard**.
2. **Create CT**: hostname `yt-filter`, a root password, Debian 12 template, 10 GB disk, 2 cores, 3072 MB memory, network DHCP (or a static IP). Leave "Unprivileged container" and "Nesting" on.
3. Start it and note its IP address (shown under **Summary** or with `ip a` in its console).

## 2. Install the app (about 10 minutes)

In the container, clone the repo and run the installer:

```bash
apt-get update && apt-get install -y git
git clone https://github.com/Joepowles/Youtube-filter.git /root/yt
bash /root/yt/deploy/install.sh
```

Cloning (rather than copying the files over) is what turns on automatic updates, see below.

The installer adds Google Chrome, cloudflared, Python packages, a `ytfilter` user, and a `yt-filter` system service. Re-run it any time to update the app.

## 3. Cloudflare Tunnel (about 5 minutes)

**Quickest:** in the container run `HOSTNAME_FQDN=yt.example.com bash /root/yt/deploy/tunnel.sh` (with your own address). It prints a link; open it, sign in to Cloudflare and pick your domain. The script then creates the tunnel, the DNS record for `yt.example.com`, and the service.

**Or by hand in the dashboard:**

`example.com` must already use Cloudflare for DNS. In the Cloudflare dashboard, open **Zero Trust**, then:

1. **Networks > Tunnels > Create a tunnel**, choose **Cloudflared**, name it `yt-filter`.
2. On the install step, pick **Debian** and copy the command that looks like `sudo cloudflared service install eyJ...`. Run it in the container (cloudflared is already installed, so skip the other lines).
3. Add a **public hostname**: subdomain `yt`, domain `example.com`, service type `HTTP`, URL `localhost:8765`. Save.

## 4. Cloudflare Access (about 5 minutes)

Still in **Zero Trust**:

1. **Access > Applications > Add an application > Self-hosted**.
2. Name `YouTube filter`, domain `yt.example.com`, session duration as you like (24 hours is fine).
3. Add a policy: action **Allow**, include **Emails**, and enter your email address. Save.
4. Login method: **One-time PIN** works with no setup (Cloudflare emails you a code). You can add Google as a login method instead under **Settings > Authentication**.
5. Open the application again and copy its **Application Audience (AUD) Tag**. Your **team domain** is under **Settings > Custom Pages** (it looks like `something.cloudflareaccess.com`).

Put both into the app's settings in the container:

```bash
nano /opt/yt-filter/.env
#   CF_ACCESS_TEAM_DOMAIN=something.cloudflareaccess.com
#   CF_ACCESS_AUD=<the AUD tag>
#   ALLOWED_EMAILS=<your email>        # optional second check
systemctl restart yt-filter
```

Cloudflare's menu names shift now and then; if a label differs, the step is in the same area.

## 5. Add your kids

Open the site and click **+ Add kid**. Give the account a name, and a sign-in window opens showing that kid's own browser on the server. Tap in it to pick fields and buttons, type into the box below it and press **Send + Enter**, and tap **Done** once YouTube shows their account. Repeat for each kid; each one gets a separate browser and sign-in.

(Terminal fallback: `ssh -L 6080:localhost:6080 root@<container-ip>`, then `/opt/yt-filter/login.sh <account-id>` and open http://localhost:6080/vnc.html.)

Each signed-in kid keeps a Chrome running on the server, roughly 300-500 MB of memory each, so give the container about 1 GB per kid plus 1 GB.

## 6. Use it

The site is written so anyone in the family can use it. Open it, enter the family password once (it remembers the phone or computer for 30 days, `SESSION_DAYS` in `.env`), and pick a child at the top.

- **Watched**: their watch history, newest first, checked every 2 minutes (`POLL_SECONDS`). New videos are marked **NEW**. **Look further back** reads older history.
- **Channels**: the same history grouped by channel, most-watched first.
- **Subscriptions**: what they're subscribed to, with **Unsubscribe** (just that) or **Block channel**.
- **Suggested**: what YouTube is showing on their homepage right now.
- **Blocked**: every blocked channel and what was done for it, with **Run again** and **Unblock**.

**Block channel** opens a short checklist (all ticked by default, and the choice is remembered):

1. Remove every video from the channel in the watch history (it searches the history for the channel, then reads through the latest 500 videos).
2. Unsubscribe.
3. Remove likes on the channel's videos.
4. Thumbs down the channel's 30 newest videos.
5. Mark "Don't recommend channel" on one of its videos on the homepage (or next to its videos, or in search).

A card at the top shows each step as it happens, with a **Stop** button. Steps that open video pages run first and the history clean-up runs last, so the clean-up also removes the visits the tool made.

**Keep blocked channels cleaned up** (on by default, in each child's **Settings**): when a blocked channel shows up in their history again, the site notes it on the Blocked tab and reruns the clean-up (without thumbs down), at most once every 30 minutes per channel.

**Hide Shorts** (off by default, in each child's **Settings**): YouTube has no setting to turn Shorts off, so this clicks "Not interested" on the Shorts shelf on their homepage and removes watched Shorts from their history. YouTube brings the shelf back after about 30 days, so it repeats every 6 hours, and sooner when new Shorts show up in the history. Shorts are still reachable from the Shorts button or search.

**Recent activity** shows what ran and the full technical log for each task, which is what to send if something says it failed.

## Automatic updates

`install.sh` also sets up a timer that checks GitHub every 5 minutes. When there are new commits it pulls them into `/root/yt` and reinstalls, which restarts the site for a few seconds. See what it did with `journalctl -u yt-filter-update -n 30`. Turn it off with `systemctl disable --now yt-filter-update.timer`.

## Troubleshooting

- Service logs: `journalctl -u yt-filter -f`. Tunnel logs: `journalctl -u cloudflared -f`.
- The site says "Sign in through Cloudflare Access" or "Invalid token": the Access application or the `.env` values don't match. Check the AUD tag and team domain.
- A step says `FAILED (...)`: YouTube changed its page. Send me the Activity output.
