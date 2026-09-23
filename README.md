# Odds Decision Hub

A self-hosted, **read-only** dashboard that compares DraftKings prices with a no-vig "fair" price taken from sharp
bookmakers and tells you, at a glance, which DraftKings bets have positive expected value (+EV), how much to stake,
the worst price still worth taking, and how urgent it is.

Odds come from **[The Odds API](https://the-odds-api.com)**, a licensed odds data provider. **You place every bet
yourself** in the DraftKings app. The software never logs in anywhere, never automates a sportsbook, and never
contacts a sportsbook's website or API.

> Betting involves risk of loss. +EV means "profitable on average over many bets", not "this bet wins". Only bet
> where sports betting is legal for you and you are of legal age (21+ in most US states). If gambling stops being fun,
> call or text **1-800-GAMBLER**.

---

## Contents

1. [What it does and does not do](#what-it-does-and-does-not-do)
2. [How the model works](#how-the-model-works)
3. [The Odds API: plans and credit math](#the-odds-api-plans-and-credit-math)
4. [Quick start (demo, no API key)](#quick-start-demo-no-api-key)
5. [Real run on your own computer](#real-run-on-your-own-computer)
6. [Deploy on a cheap Ubuntu VPS](#deploy-on-a-cheap-ubuntu-vps)
7. [Reach the dashboard safely](#reach-the-dashboard-safely)
8. [Phone alerts](#phone-alerts)
9. [Dashboard walkthrough](#dashboard-walkthrough)
10. [Settings reference](#settings-reference)
11. [Limitations (read this)](#limitations-read-this)
12. [Troubleshooting](#troubleshooting)
13. [Development](#development)
14. [Responsible gambling](#responsible-gambling)

---

## What it does and does not do

**It does**

- Poll The Odds API for DraftKings plus a set of other books (Pinnacle, BetOnline, LowVig, FanDuel, BetMGM, ...)
  on moneylines, spreads and totals, for the leagues you enable.
- Work out a fair (no-vig) win probability for every outcome from the sharpest book available.
- Flag DraftKings prices whose expected value clears your threshold, with a verdict (**BET NOW / BET / WATCH**),
  urgency, suggested stake, "take at X or better" price and plain-English reasons.
- Push the board to your browser in real time and, optionally, send phone alerts (ntfy, Discord, Telegram).
- Keep a bet journal: you log what you actually placed, settle it later, and see profit, ROI and closing line value.
- Spend your Odds API credits carefully so the monthly quota lasts until it resets.

**It does not**

- Place bets, log in to DraftKings or any other sportsbook, fill bet slips, or scrape any sportsbook site.
  The only outbound connections are The Odds API and the alert webhooks you configure. The "Open in DraftKings"
  button is an ordinary link that *you* click.
- Guarantee profit. It estimates edges; results have large variance and prices move.
- Cover player props, parlays / same-game parlays, futures or live micro-markets. Main-line moneyline, spread and
  total only (alternate lines are off by default).
- Check whether betting is legal where you are. That is your responsibility.

```
The Odds API ──HTTPS──> poll scheduler ──> market store ──> engine: fair price, EV, stake, urgency
 (licensed feed)       (credit budget)                        │
                                                              ├──> web server ──SSE──> your browser (dashboard)
                                                              ├──> optional alerts: ntfy / Discord / Telegram
                                                              └──> ./data: settings.json + bet journal

You ──> DraftKings app (you decide and place every bet yourself)
```

---

## How the model works

### 1. Fair price (no-vig)

Every sportsbook price contains a margin (the "vig"): a two-way market at -110 / -110 implies 52.4% + 52.4% =
104.8%, not 100%. Removing that margin from a **sharp** book (one that takes large bets from professionals and
moves quickly) gives a good estimate of the true probability.

- **Reference book.** `SHARP_BOOKS` in priority order (default Pinnacle, then BetOnline, then LowVig). The first
  one with a complete, not-suspended, fresh market (all outcomes present, seen within the last 45 s for live games
  or 15 min before the game) is used.
- **Consensus fallback.** If no sharp book qualifies, the de-vigged probabilities of every other usable book
  (at least `MIN_CONSENSUS_BOOKS`, default 3) are averaged. DraftKings is never used as a reference for itself.
- **De-vig method.** The default `worst` computes four standard methods (multiplicative, additive, power, Shin)
  and, for each outcome, keeps the **lowest** probability. An edge therefore has to survive every method.

Example, Pinnacle favorite -200 (1.50) / underdog +175 (2.75):

| Method         | Favorite | Underdog |
|----------------|---------:|---------:|
| Multiplicative | 64.71%   | 35.29%   |
| Additive       | 65.15%   | 34.85%   |
| Power          | 65.37%   | 34.63%   |
| Shin           | 65.15%   | 34.85%   |
| **Worst case** | **64.71%** | **34.63%** |

Under `worst` the underdog's fair price is +189 (1 / 0.3463), so DraftKings has to offer better than +189 on the
underdog before it even breaks even.

Markets are compared only at the **same line**: DraftKings Celtics -3.5 is compared with Pinnacle Celtics -3.5 /
Knicks +3.5, never with -4. Soccer moneylines are three-way (home / draw / away).

### 2. Expected value (EV)

```
EV = p × d − 1        p = fair win probability, d = DraftKings decimal odds
```

Fair 50% and DraftKings +110 (decimal 2.10): EV = 0.50 × 2.10 − 1 = **+5.0%**, that is +$5 per $100 staked on
average.

Prices are ignored when the DraftKings quote is older than 45 s (live) / 10 min (pre-game), when it is longer than
+1000 (`MAX_DECIMAL_ODDS`), or when the EV is above +25% (`MAX_PLAUSIBLE_EV`; almost always bad data or a mismatched
line).

### 3. Confidence (0 to 1)

| Factor | Effect |
|---|---|
| Reference = Pinnacle | 0.90 |
| Reference = another sharp book | 0.80 |
| Reference = consensus of *n* books | min(0.85, 0.55 + 0.05 × n) |
| Live game | × 0.85 |
| Age of the reference price | × (1 − 0.3 × age / max age) |
| Longshot (longer than +300) | × 0.85 |
| Pre-game, DraftKings repriced *after* the reference last moved | × 0.85 |

The result is clamped to 0.1 – 1 and shown as a meter on each card. It scales the stake and gates the verdict.

### 4. Verdicts

| Verdict | Meaning |
|---|---|
| **BET NOW** | EV ≥ your live/pre-game minimum, confidence ≥ 0.5, **and** time matters: the game is live, or it is a stale line (below). Open DraftKings now, check the price, place it or skip it. |
| **BET** | Same thresholds, pre-game, no stale line. Worth taking; usually minutes to hours of shelf life. |
| **WATCH** | Some edge (≥ `WATCH_EV`, default +0.5%) but below your minimum, confidence under 0.5, or a live price DraftKings moved *after* the sharp line (the feed may be lagging). Not a bet. |

Defaults: minimum EV **+2% pre-game**, **+3% live** (live data is noisier, so it must clear a higher bar).

### 5. Stale lines ("steam")

When a single sharp book is the reference, the engine checks whether that book's implied probability for the pick
rose by at least `STALE_MOVE_PROB` (2 percentage points) within the last `STALE_WINDOW_SEC` (120 s) while
DraftKings has not changed its price since. Example reason: *"Pinnacle moved +105 → −120 in the last 70s;
DraftKings still +110"*. These are the edges most likely to disappear, so they get extra urgency.

### 6. Urgency

```
score = 45 if live
      + 30 if stale line
      + min(25, EV × 500)                 (+5% EV or more adds the full 25)
      + 15 / 8 / 3 if a pre-game start is within 15 min / 1 h / 3 h
WATCH picks are capped at 40; score clamped to 0–100.

critical ≥ 75     high ≥ 55     medium ≥ 30     low < 30
```

Examples: live + stale + 5% EV = 45 + 30 + 25 = 100 (critical). Live, 4% EV = 45 + 20 = 65 (high). Pre-game, 5%
EV, starting in 50 min = 25 + 8 = 33 (medium).

Each pick also gets a rough shelf life shown as "act within ~N s": live stale line 20 s, other live 45 s, pre-game
stale line 2 min, pre-game starting within the hour 5 min, otherwise 15 min. It is an estimate, not a promise.

### 7. Stake sizing (fractional Kelly)

```
full Kelly f* = (p × d − 1) / (d − 1)
stake         = bankroll × f* × KELLY_MULTIPLIER × confidence
then capped by: bankroll × MAX_STAKE_PCT, MAX_STAKE_ABS, and today's remaining exposure
                (bankroll × MAX_DAILY_EXPOSURE_PCT − what you already logged today)
rounded down to whole dollars; below $1 → $0. WATCH picks always show $0.
```

Example: bankroll $1,000, p = 0.50, DraftKings +110 (2.10), confidence 0.88:
f* = (1.05 − 1) / 1.10 = 4.55%; × 0.25 (quarter Kelly) × 0.88 = 1.00% → **$10**. Caps: 2% of bankroll = $20,
$100 absolute, $150 daily exposure, none binding.

"Today" is the calendar day in `TZ`. Only bets you log with **I placed it** count toward the daily exposure.

### 8. "Take at X or better"

The worst DraftKings price at which the pick still clears your minimum EV:

```
minimum decimal = (1 + min EV) / p      → shown in American odds, rounded toward the better price
```

With p = 0.50 and a +2% pre-game minimum: 1.02 / 0.50 = 2.04 → **take at +104 or better**. If DraftKings now
shows +110 or +105, take it; at +100, skip. "Better" means a bigger payout: +120 is better than +110, and −105 is
better than −115. The same pick live (+3% minimum) needs +106 or better.

### 9. Arbs (optional, `SHOW_ARBS`)

If the DraftKings price on one side plus the best price at *another sportsbook* on the other side(s) implies less
than 100% in total (Σ 1/d < 1), both bets together lock in a profit whatever happens. The Arbs tab shows each leg,
its book and a stake split (to the cent) that equalizes the payout (minimum 0.5% profit). The second leg must be
placed at the other sportsbook, by you. The reference books in `SHARP_BOOKS` (Pinnacle, BetOnline, LowVig by default)
are only used for pricing, never as an arb leg: they are offshore books, and with their thin margin every +EV
DraftKings price would also show up as a "DraftKings vs Pinnacle arb". Books watch for arbitrage betting and limit
accounts that do it.

### 10. Correlation

If one event produces several picks, the weaker ones say *"Correlated with <pick> — pick one"*. Betting both sides
of related markets on the same game multiplies risk rather than edge.

---

## The Odds API: plans and credit math

### Sign up

1. Go to [the-odds-api.com](https://the-odds-api.com) and pick a plan. The key is emailed to you.
2. Put it in `.env` as `ODDS_API_KEY=...`.
3. Set `ODDS_API_MONTHLY_CREDITS` to your plan size and `ODDS_API_RESET_DAY` to the day of the month your quota
   resets (see your account page).

At the time of writing the plans were 500 (free), 20K, 100K, 5M and 15M credits per month; check the site for
current plans and prices. **The free 500 is only enough to try it; 20K is the practical minimum; 100K is where
live betting starts to make sense.**

### What a call costs

Only the odds endpoint costs credits. Learning which games exist and when they start uses the free events endpoint
(0 credits), so leagues with nothing on never spend anything.

```
credits per odds call = markets × ceil(bookmakers / 10)

defaults: 3 markets (h2h, spreads, totals) × ceil(10 books / 10) = 3 × 1 = 3 credits per call
          an 11th bookmaker:                                        3 × 2 = 6 credits per call
          moneyline only (ODDS_API_MARKETS=h2h):                    1 × 1 = 1 credit per call
```

One call returns every game in one league (all games starting within `ODDS_API_PREMATCH_HORIZON_HOURS`, default
24 h, plus live ones). The startup log line and the health drawer show your actual cost per call and the credits
remaining.

### How the scheduler spends credits

```
credits per hour = (credits remaining − ODDS_API_RESERVE_CREDITS) / hours until the reset day
```

It is recomputed after every call from the `x-requests-remaining` header, so it self-corrects: credits not spent
during quiet hours (nothing live, nothing starting within the pre-match horizon) are spread over the rest of the
cycle, and it never plans to spend more than you have. When only the reserve is left, polling stops until the reset.

That hourly budget is split between the **active** leagues (live, or with a game starting within the horizon) by
weight: **live 4, starting within 2 h 2, other pre-game 1**. Each league's poll interval is

```
interval = 3600 × cost per call / (credits per hour × weight / total weight)
           limited to ≥ 20 s live (ODDS_API_MIN_INTERVAL_LIVE_SEC), ≥ 180 s pre-game (…_PREMATCH_SEC),
           and ≤ 1800 s (ODDS_API_MAX_INTERVAL_SEC) unless the budget is too tight to allow even that
```

**20K plan**, start of a 30-day cycle: (20,000 − 200) / 720 h = **27.5 credits/hour**. At 3 credits per call that is
27.5 / 3 = 9.2 calls per hour, so a single live league is refreshed every 3600 × 3 / 27.5 = **393 s (about every
6.5–7 minutes)**.

**100K plan**: (100,000 − 200) / 720 h = **138.6 credits/hour** = 46 calls per hour, so a single live league is
refreshed every 3600 × 3 / 138.6 = **78 s**.

**Free 500**: (500 − 200) / 720 h = 0.42 credits/hour, one call every 7.2 hours. Testing only (lower the reserve).

Intervals you get with the defaults (computed with the real scheduler at the start of a 30-day cycle):

| Situation | 20K plan (27.5 credits/h) | 100K plan (138.6 credits/h) |
|---|---|---|
| 1 league live, nothing else | 393 s (6.5 min) | 78 s |
| 2 leagues live | 786 s (13 min) each | 156 s (2.6 min) each |
| 3 leagues live | 1,179 s (20 min) each | 234 s (3.9 min) each |
| 1 live + 1 starting within 2 h | 590 s live / 1,179 s other | 117 s live / 234 s other |
| 2 live + 2 with games later (weights 4+4+1+1) | 982 s live / 3,928 s others | 195 s live / 780 s others |
| 1 pre-game league only | 393 s | 180 s (pre-game floor) |

Worked row 5 on the 20K plan: total weight 10, a live league gets 27.5 × 4 / 10 = 11 credits/h, so 3600 × 3 / 11 =
982 s; a later league gets 2.75 credits/h, so 3,928 s (the budget wins over the 1,800 s target).

Polling one live league at the 20 s floor costs 3 × 180 calls = 540 credits per hour. Only the multi-million
plans reach the floor for several leagues at once.

**Poll interval vs. price freshness.** Prices are only trusted for a limited time after each poll: 45 s for live
games, 10 min (DraftKings) / 15 min (reference book) before the game. On a small plan that means live picks show up
in short bursts right after each refresh of that league and then drop off until the next one, which is deliberate:
a six-minute-old live price is not something to bet on. Likewise, a pre-game league polled less often than every
10 minutes has gaps where its picks are hidden. Fewer leagues or a bigger plan closes those gaps.

**Stretching a small plan**

- Enable only the leagues you actually bet (Settings → Leagues, or `LEAGUES`).
- Lower `ODDS_API_PREMATCH_HORIZON_HOURS` (for example 6) so tomorrow's games stop taking a share.
- Drop markets you do not bet (`ODDS_API_MARKETS=h2h,spreads` = 2 credits per call).
- Keep the bookmaker list at 10 or fewer.
- Do not use the same key in another app: its usage comes out of the same quota (the scheduler adapts, but you get
  fewer polls).

---

## Quick start (demo, no API key)

Requires Node.js 22.12 or newer.

```bash
npm ci && npm run demo
```

Open **http://localhost:8080**. Demo mode simulates a few leagues with moving prices, stale DraftKings lines and
occasional arbs so you can learn the dashboard. **Demo prices are fake: never bet from demo mode.**

With Docker instead: `cp .env.example .env`, set `DEMO_MODE=true`, then `docker compose up -d --build`.

---

## Real run on your own computer

```bash
cp .env.example .env        # set ODDS_API_KEY, BANKROLL, LEAGUES, TZ, ...
npm ci
npm run build
npm start                   # reads .env from the current directory
```

Open **http://localhost:8080**. By default it listens on 127.0.0.1 only. Settings and your bet journal are stored
in `./data`.

For a 24/7 setup (so live alerts reach your phone while your laptop sleeps) use a small VPS.

---

## Deploy on a cheap Ubuntu VPS

Any provider's smallest plan is enough: 1 vCPU, 1 GB RAM, 10 GB disk, Ubuntu 24.04 LTS (22.04 works too). The
container is limited to 512 MB. Where the server is located does not matter for the odds; where **you** are when
you place a bet does matter legally.

The commands assume a user with `sudo`.

### 1. Update the server and turn on the firewall

```bash
sudo apt update && sudo apt upgrade -y
sudo ufw allow OpenSSH
sudo ufw enable
timedatectl                 # "System clock synchronized: yes" (prices are time-stamped; the clock must be right)
```

Note: ports published by Docker bypass ufw. That is why `docker-compose.yml` publishes the dashboard on
`127.0.0.1` only (see [Reach the dashboard safely](#reach-the-dashboard-safely)).

### 2. Install Docker Engine and the Compose plugin

Either Docker's convenience script:

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
```

or Docker's apt repository:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Then make Docker start on boot and check it works:

```bash
sudo systemctl enable --now docker
sudo docker run --rm hello-world
docker compose version
```

Optional, to run `docker` without `sudo`: `sudo usermod -aG docker $USER`, then log out and back in. (Membership of
the `docker` group is equivalent to root on that machine.) The rest of this guide omits `sudo` for `docker`
commands; add it if you skipped this step.

### 3. Get the code

```bash
cd ~
git clone https://github.com/YOUR_ACCOUNT/YOUR_REPO.git odds-hub
cd odds-hub
```

For a private repository use a GitHub deploy key or personal access token, or copy the folder from your computer
instead: `rsync -av --exclude node_modules --exclude dist --exclude data ./ you@YOUR_VPS_IP:~/odds-hub/`.

### 4. Configure

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Set at least:

| Variable | What to put |
|---|---|
| `ODDS_API_KEY` | your key |
| `ODDS_API_MONTHLY_CREDITS`, `ODDS_API_RESET_DAY` | your plan size and reset day |
| `LEAGUES` | only what you bet, e.g. `NFL,NBA` |
| `BANKROLL` | your betting bankroll in dollars |
| `TZ` | your time zone, e.g. `America/Chicago` (defines "today" for the daily limit) |
| `DASHBOARD_PASSWORD` | a long random password: `openssl rand -base64 24` |
| `BOOK_STATE` | the state you bet from, e.g. `nj` (better DraftKings links) |
| `NTFY_URL` / `DISCORD_WEBHOOK_URL` / `TELEGRAM_*` | optional phone alerts |

`HOST`, `PORT` and `DATA_DIR` in `.env` are ignored under Docker Compose; `docker-compose.yml` sets them.

### 5. Give the container its data folder

The container runs as the unprivileged user `node` (uid 1000) and stores `settings.json` and the bet journal in
`./data`:

```bash
mkdir -p data
sudo chown -R 1000:1000 data
```

Skip this and the app cannot save anything (`EACCES: permission denied` in the logs).

### 6. Build and start

```bash
docker compose up -d --build
```

The first build takes a minute or two. If it gets killed on a 1 GB server, add swap and retry:

```bash
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 7. Watch the logs

```bash
docker compose logs -f          # Ctrl+C stops following; the service keeps running
```

The startup banner shows the mode, leagues, books, credits per odds call, the dashboard URL and whether a password
is set. Inside Docker the app listens on 0.0.0.0 (so Docker can forward the port); the host side is still
127.0.0.1 only. If you left `DASHBOARD_PASSWORD` empty, the banner warns about exactly that.

### 8. Verify

```bash
docker compose ps                                   # STATUS should become "Up ... (healthy)" within ~1 minute
curl -fsS http://127.0.0.1:8080/healthz             # {"ok":true,"uptimeSec":42}
curl -fsS -u admin:'YOUR_PASSWORD' http://127.0.0.1:8080/api/state | head -c 300; echo
```

`/healthz` is the only page that works without the password; it reveals nothing but "ok" and the uptime.

### 9. Auto-start after a reboot

Already configured: `restart: always` in `docker-compose.yml` plus `sudo systemctl enable docker` from step 2. Test
it once:

```bash
sudo reboot
# reconnect, then:
cd ~/odds-hub && docker compose ps
```

### 10. Updating

```bash
cd ~/odds-hub
git pull
docker compose up -d --build
docker image prune -f           # remove the old image layers
```

Your settings and bets in `./data` are untouched. To roll back, `git checkout <previous commit>` and run
`docker compose up -d --build` again.

After editing `.env`, run `docker compose up -d` (it recreates the container with the new values).
`docker compose restart` does **not** re-read `.env`.

### 11. Backups

Everything worth keeping is in `./data` (`settings.json` and the bet journal `bets.jsonl`) plus your `.env` (secrets;
store that copy somewhere private).

```bash
mkdir -p ~/backups
tar czf ~/backups/odds-hub-data-$(date +%F).tar.gz -C ~/odds-hub data
```

Nightly at 04:17 with 30 days kept (`crontab -e`; `%` must be escaped in crontab):

```
17 4 * * * tar czf $HOME/backups/odds-hub-data-$(date +\%F).tar.gz -C $HOME/odds-hub data && find $HOME/backups -name 'odds-hub-data-*.tar.gz' -mtime +30 -delete
```

Copy backups off the server now and then: `scp 'you@YOUR_VPS_IP:backups/odds-hub-data-*.tar.gz' .`

Restore:

```bash
cd ~/odds-hub
docker compose down
tar xzf ~/backups/odds-hub-data-2026-09-01.tar.gz -C ~/odds-hub
sudo chown -R 1000:1000 data
docker compose up -d
```

---

## Reach the dashboard safely

The dashboard shows your bankroll and bets, and whoever reaches it can change settings and log bets. Keep it
private. `docker-compose.yml` publishes it on the server's `127.0.0.1:8080` only, so by default nothing on the
internet can reach it. Pick one of the options below.

**Never** change the port mapping to `"8080:8080"` or `"0.0.0.0:8080:8080"`, and never expose port 8080 without a
password: Docker-published ports go around ufw, so the dashboard would be open to the whole internet. Never send
the password over plain `http://` across the internet (HTTP Basic auth is readable on the wire), and never use
`tailscale funnel` for it (that makes it public).

### Option A: SSH tunnel (recommended, nothing to install)

From your laptop:

```bash
ssh -N -L 8080:127.0.0.1:8080 you@YOUR_VPS_IP
```

Leave it running and open **http://localhost:8080**. The traffic travels inside SSH. If port 8080 is busy on your
laptop use `-L 9080:127.0.0.1:8080` and open http://localhost:9080. To make it a one-word command, add to
`~/.ssh/config`:

```
Host odds
    HostName YOUR_VPS_IP
    User you
    LocalForward 8080 127.0.0.1:8080
```

then run `ssh -N odds`. SSH apps with port forwarding work on phones too, but Tailscale is easier there.

### Option B: Tailscale (private network, easiest on a phone)

On the VPS:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                  # open the printed link and sign in
sudo tailscale serve --bg 8080     # HTTPS inside your tailnet -> 127.0.0.1:8080
```

Install the Tailscale app on your phone and laptop, sign in to the same account, and open
`https://<vps-name>.<your-tailnet>.ts.net`. Only your own devices can reach it. (If `serve` asks you to enable
HTTPS certificates or MagicDNS, follow its link.) Stop sharing with `sudo tailscale serve reset`. Setting
`DASHBOARD_PASSWORD` as well is still a good idea.

### Option C: Caddy reverse proxy with automatic HTTPS + DASHBOARD_PASSWORD

Use this if you want a normal `https://` address. You need a domain name.

1. Set a strong `DASHBOARD_PASSWORD` in `.env` (`openssl rand -base64 24`) and apply it with `docker compose up -d`.
   **Do not skip this step: with a reverse proxy the dashboard is on the public internet.**
2. Point a DNS **A** record, e.g. `odds.example.com`, at your VPS IP.
3. Install Caddy: on Ubuntu 24.04 `sudo apt install -y caddy`, otherwise use the official apt repository from
   caddyserver.com/docs/install.
4. Replace `/etc/caddy/Caddyfile` with:

   ```
   odds.example.com {
       reverse_proxy 127.0.0.1:8080
   }
   ```

5. Open the web ports and reload:

   ```bash
   sudo ufw allow 80/tcp
   sudo ufw allow 443/tcp
   sudo systemctl reload caddy
   ```

Caddy obtains and renews a Let's Encrypt certificate automatically, redirects HTTP to HTTPS, streams the live
updates without buffering, and passes the `Host` header through unchanged (the dashboard refuses writes whose
`Origin` does not match `Host`). Your browser will ask for `DASHBOARD_USER` / `DASHBOARD_PASSWORD`.

If you use another proxy (nginx, Traefik), keep the `Host` header and disable response buffering for
`/api/stream`.

---

## Phone alerts

Alerts fire when a **new** BET NOW / BET pick reaches `NOTIFY_MIN_URGENCY` (default `critical`; `high` or `medium`
send more). Each pick alerts at most once per `NOTIFY_COOLDOWN_SEC` (300 s), and at most 5 messages go out per engine
tick. Demo mode never sends alerts (its prices are fake). Example message:

```
LIVE BET NOW · NBA · Celtics -3.5 @ +110 (take ≥ +104) · EV +5.2% · stake $38 · Knicks @ Celtics
```

Configure any combination in `.env`, then `docker compose up -d`:

- **ntfy** (simplest, free): install the ntfy app, subscribe to a long random topic such as `odds-hub-7f3kq9x2m1`,
  and set `NTFY_URL=https://ntfy.sh/odds-hub-7f3kq9x2m1`. Critical alerts are sent at max priority; tapping one
  opens the DraftKings link when available. Anyone who knows the topic name can read it, so make it unguessable
  (or use your own ntfy server).
- **Discord**: Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL →
  `DISCORD_WEBHOOK_URL=...`.
- **Telegram**: create a bot with @BotFather (gives the token), send your bot any message, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}`. Set both `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID`.

Webhook URLs and bot tokens are secrets; the app never writes them to its logs.

The dashboard itself can also beep and show browser notifications while it is open (click **Alerts** once to
allow it). Browser notifications need HTTPS or `localhost` (the SSH tunnel counts as localhost), and iPhone
Safari does not show them for ordinary tabs, so use ntfy or Telegram for your phone.

---

## Dashboard walkthrough

**Top bar**: live-connection state (live / reconnecting), data source status and credits remaining, age of the
last update, number of live events, bankroll, how much you can still stake today, and buttons for Alerts,
Settings, System health and the light/dark theme.

**Opportunities tab**

- **LIVE — ACT NOW**: live BET NOW / BET cards, most urgent first. Critical cards are red with a pulsing border and
  a countdown bar ("act within ~20 s").
- **Pre-game**: BET cards for games that have not started.
- **Watch list** (collapsed by default): WATCH picks, near-misses worth keeping an eye on. Not bets.
- Cards that disappear from the feed turn grey, say "Price moved — gone" and fade out.

**Each card** shows the verdict badge, urgency, league, teams, start time or LIVE with the score, the pick in large
type, the DraftKings price in American odds with **take at ≥ X**, the fair price, EV %, the suggested stake in
dollars and % of bankroll, a confidence meter, the reference source and how old the prices are, a stale-line flag
and the reasons. Buttons:

- **Open in DraftKings**: deep link when the feed provides one (set `BOOK_STATE`), otherwise the DraftKings home
  page. Find the same market, check the price against "take at", then decide.
- **Copy pick**: copies the pick text.
- **I placed it**: log the bet with the stake and odds you actually got (both editable). This feeds the daily
  exposure limit and the My bets stats.

**Filters**: league chips, live-only, minimum EV slider and show-watch-list toggle; remembered in your browser.

**Arbs tab**: DraftKings-vs-other-book pairs, both legs with book, price and stake.

**My bets tab**: totals (bets, pending, staked, profit, ROI, average EV at placement, average CLV) and your bets
with **Won / Lost / Push / Void** buttons to settle them. For pre-game bets the app records the fair price when the
game starts (the "closing line"); CLV = closing fair probability × your decimal odds − 1. Consistently positive CLV
is the best early evidence that you are beating the market, long before profit is statistically meaningful.

**Settings**: the runtime settings below; invalid values are rejected with a message. **System health**:
status of each data source, credits used/remaining, events and quotes tracked, memory and engine timing.

When nothing shows, the page says why: no API key yet, or simply no edges right now (the normal state most of the
time).

---

## Settings reference

### Runtime settings (dashboard Settings; `.env` provides the starting values)

Once you save in the dashboard, values are stored in `data/settings.json` and take precedence over `.env`. Delete
that file (and restart) to go back to the `.env` values. The dashboard shows percentages; `.env` uses fractions.

| Dashboard field | `.env` variable | Default | Meaning |
|---|---|---|---|
| Bankroll | `BANKROLL` | 1000 | Dollars set aside for betting |
| Kelly multiplier | `KELLY_MULTIPLIER` | 0.25 | Fraction of full Kelly (0.01–1) |
| Max stake per bet (%) | `MAX_STAKE_PCT` | 0.02 | Cap per bet as a share of bankroll |
| Max stake per bet ($) | `MAX_STAKE_ABS` | 100 | Cap per bet in dollars |
| Daily exposure limit | `MAX_DAILY_EXPOSURE_PCT` | 0.15 | Total you may stake per day (in `TZ`) |
| Min EV pre-game | `MIN_EV_PREMATCH` | 0.02 | EV needed for BET before the game (dashboard range 0–50%) |
| Min EV live | `MIN_EV_LIVE` | 0.03 | EV needed for BET NOW in-game (0–50%) |
| Watch list from | `WATCH_EV` | 0.005 | Lowest EV shown at all (as WATCH); must be ≤ both minimums |
| Leagues | `LEAGUES` | NFL,NCAAF,NBA,NCAAB,WNBA,MLB,NHL,EPL,UCL,MLS | Leagues priced (and polled) |
| Show arbs | `SHOW_ARBS` | true | Compute DraftKings-vs-other-book arbs |

### Environment-only settings

`.env.example` documents every variable in detail; the essentials:

| Variable | Default | Meaning |
|---|---|---|
| `ODDS_API_KEY` | (empty) | Odds API key; empty = no live odds |
| `DEMO_MODE` | false | Simulated data, no credits |
| `ODDS_API_MONTHLY_CREDITS` / `ODDS_API_RESET_DAY` / `ODDS_API_RESERVE_CREDITS` | 20000 / 1 / 200 | Budget inputs |
| `ODDS_API_BOOKS` | 10 books incl. pinnacle, draftkings | Books requested (DraftKings always added) |
| `SHARP_BOOKS` | pinnacle,betonlineag,lowvig | Reference books in priority order |
| `ODDS_API_MARKETS` | h2h,spreads,totals | Markets requested |
| `ODDS_API_SPORTS` | (empty) | Override/add sport keys, e.g. `KBO:baseball_kbo` or `UFC:none` |
| `ODDS_API_MIN_INTERVAL_LIVE_SEC` / `…_PREMATCH_SEC` / `ODDS_API_MAX_INTERVAL_SEC` | 20 / 180 / 1800 | Poll interval limits |
| `ODDS_API_PREMATCH_HORIZON_HOURS` | 24 | Only games starting within this window spend credits |
| `ODDS_API_INCLUDE_LINKS` / `BOOK_STATE` | true / (empty) | DraftKings deep links and the state to fill in |
| `ODDS_API_TIMEOUT_MS` / `ODDS_API_BASE_URL` | 15000 / https://api.the-odds-api.com | HTTP details |
| `DEVIG_METHOD` | worst | worst, multiplicative, additive, power, shin |
| `MIN_CONSENSUS_BOOKS` | 3 | Books needed for a consensus fair price |
| `LIVE_MAX_SHARP_AGE_SEC` / `PREMATCH_MAX_SHARP_AGE_SEC` | 45 / 900 | Oldest trusted reference price |
| `LIVE_MAX_DK_AGE_SEC` / `PREMATCH_MAX_DK_AGE_SEC` | 45 / 600 | Oldest trusted DraftKings price |
| `STALE_MOVE_PROB` / `STALE_WINDOW_SEC` | 0.02 / 120 | Stale-line detection |
| `MAX_PLAUSIBLE_EV` | 0.25 | Hide edges above this (bad data) |
| `MAX_DECIMAL_ODDS` | 11 | Ignore prices longer than +1000 |
| `INCLUDE_ALT_LINES` | false | Evaluate alternate lines too |
| `GONE_RETENTION_SEC` | 90 | How long vanished picks stay visible |
| `HOST` / `PORT` | 127.0.0.1 / 8080 | Listen address (Docker overrides to 0.0.0.0:8080 inside the container) |
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD` | admin / (empty) | HTTP Basic auth; empty password = no auth |
| `DATA_DIR` | ./data | Settings and bet journal (Docker: /app/data ← ./data) |
| `TZ` | America/New_York | Defines "today" for the daily limit |
| `LOG_LEVEL` | info | debug, info, warn, error |
| `DISCORD_WEBHOOK_URL`, `NTFY_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | (empty) | Alert channels |
| `NOTIFY_MIN_URGENCY` / `NOTIFY_COOLDOWN_SEC` | critical / 300 | Alert threshold and per-pick cooldown |

An invalid value (for example a typo in `LEAGUES`) stops the app at startup with a message naming the variable.

---

## Limitations (read this)

- **Data latency.** The Odds API is an aggregator: its prices trail the books by roughly 20–60 seconds, and each
  league is only refreshed every poll interval on top of that (see the table above). A live "edge" is often just a
  price the feed has not updated yet. That is why live picks need a higher EV, get lower confidence, expire fast,
  and are forced to WATCH when DraftKings moved after the sharp line. Treat live picks as noisy.
- **Prices move before you click.** Always compare the price in the DraftKings app with **take at X or better**. If
  it is worse, skip it. If the market is suspended or the bet slip changes the odds, skip it.
- **The fair price is an estimate.** It is only as good as the reference book. Consensus prices (no sharp book
  available) are weaker, and small leagues and longshots are less efficient.
- **Variance is large.** A +3% edge still loses nearly half its bets at even money. It takes hundreds of bets before
  profit says much; closing line value tells you more, sooner. Expect losing weeks.
- **Accounts get limited.** Sportsbooks, DraftKings included, restrict stakes on (or close) accounts that
  consistently beat their lines or bet arbitrage. Plan for your limits to shrink if this works.
- **Legality and age.** Only bet where sports betting is legal for you, from a location where your sportsbook is
  licensed, and only if you are of legal age (21+ in most US states). This software does not check.
- **Not advice.** This is a decision-support tool, not financial or betting advice. Only stake money you can
  afford to lose.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Container keeps restarting | `docker compose logs --tail 100`. Usually a config error (the message names the variable) or `EACCES` on `/app/data` (run `sudo chown -R 1000:1000 data`). |
| Edited `.env` but nothing changed | Run `docker compose up -d` (recreates the container). `restart` does not re-read `.env`. Runtime settings saved in the dashboard override `.env`: change them there or delete `data/settings.json`. |
| "No ODDS_API_KEY" / source disabled | Set `ODDS_API_KEY` and `docker compose up -d`. |
| The Odds API "down", invalid key | Check the key on your account page; after a 401 the app pauses that source for 30 minutes. Fix the key and recreate the container. |
| Credits run out / "quota" | The scheduler stops when only the reserve is left. Wait for the reset day, upgrade the plan, or cut leagues/markets. Make sure `ODDS_API_RESET_DAY` matches your account. |
| A league never shows anything | Out of season: no games means no polling (and no credits spent). If the API rejects the sport key as unknown or unavailable, the league is skipped for 6 hours at a time; check `ODDS_API_SPORTS`, Settings → Leagues and System health. |
| Nothing on the board | Usually normal: real edges are rare and short-lived. Open the Watch list and System health (events and quotes tracked). |
| Every live pick is WATCH | Expected when the feed is lagging or DraftKings moved after the sharp line. |
| Stake shows $0 | WATCH verdict, today's exposure limit used up, or the Kelly stake is under $1. |
| "Open in DraftKings" opens the home page | The feed had no deep link for that market, or `BOOK_STATE` is empty. |
| Password prompt keeps coming back | Wrong user/password. If the password contains `$`, `#` or spaces, wrap it in single quotes in `.env`. |
| `cross-origin request rejected` (403) behind a proxy | The proxy rewrites the `Host` header. Pass it through unchanged (Caddy and `tailscale serve` do by default). |
| Dashboard stuck on "reconnecting" behind a proxy | The proxy buffers the event stream. Disable buffering for `/api/stream` (nginx honors the `X-Accel-Buffering: no` header the app sends). |
| Daily limit resets at the wrong hour | Set `TZ` in `.env`, then `docker compose up -d`. |
| Port 8080 already used on the server | Change only the host side: `"127.0.0.1:9080:8080"`. |
| Health check failing | `docker inspect --format '{{json .State.Health}}' odds-hub` and the logs. |
| Build killed on a small server | Add swap (step 6 of the VPS guide). |

---

## Development

```bash
npm ci
npm run dev          # watch mode with live odds (.env)
npm run demo         # simulated data
npm test             # vitest, no network access needed
npm run typecheck    # strict TypeScript incl. tests
npm run build        # compile to dist/
```

Layout: `src/index.ts` (startup, poll loop, engine tick, shutdown), `src/sources` (Odds API client and parser,
credit scheduler, demo feed), `src/engine` (de-vig, fair price, Kelly, opportunities, market store), `src/server`
(HTTP/SSE server, settings, bet journal, notifier), `src/util` (odds math, HTTP, retry, logging), `public/`
(dashboard: plain HTML/CSS/JS, no external resources), `test/` (vitest). Node built-ins only at runtime; no
third-party runtime dependencies.

---

## Responsible gambling

- Set a budget (the bankroll here) and a daily limit you are comfortable losing, and stick to them.
- Never chase losses or raise stakes to "get it back".
- DraftKings and other sportsbooks offer deposit limits, time-outs and self-exclusion in their account settings.
- Help is free and confidential: call or text **1-800-GAMBLER** (1-800-426-2537), or visit the National Council on
  Problem Gambling at ncpgambling.org.
