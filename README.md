# Yashu Bot — Telegram Course-Selling Bot

A Telegram bot for selling video courses ("stuffs") with aiogram 3.x on Python 3.11.
It has a dual-backend database: local SQLite by default, MongoDB Atlas when `DATABASE_URL`
starts with `mongodb+srv`.

## Features

- **Stuffs catalog** — browse all courses, open one, play lessons in order.
- **Showcase images** — one cover photo per stuff, shown first on the stuff page.
- **Demo lessons** — mark any lesson as free preview; everyone can watch demos without paying.
- **Proofs gallery** — per-stuff delivery screenshots that buyers can flip through.
- **VIEW DEMO / BUY DIRECTLY** — main menu buttons: quick demo access and multi-select checkout.
- **Manual payments + screenshot approval** — crypto wallet / QR methods, buyer presses
  "I have paid", uploads a payment screenshot, admin approves or rejects it in the log group;
  content is delivered instantly after approval.
- **Telegram Stars** — in-bot currency checkout, optionally with a **buyer proof screenshot**
  (the buyer opens the configured receiver account, presses START and sends a screenshot here).
- **Wallet balance** — admins can top up user balances; users pay from balance with instant delivery.
- **Admin panel via `/admin` only** — everything is managed from an inline panel in Telegram.
- **Clone bots via `/clone TOKEN`** — spin up a clone from the main bot: same clone
  (copies all stuffs/categories) or new empty database.
- **Broadcasts** — send text, photo, video, GIF or sticker (as-is, caption kept) to every
  user with preview and confirmation; pick the audience: everyone, buyers, non-buyers,
  a segment of a stuff, or send it *from a clone bot*.
- **User manager** — look up a user by ID, ban/unban, add/remove balance.
- **Stats / VPS panel** — users, revenue, payment requests, plus live CPU/RAM/disk/uptime.
- **Log group** — payment screenshots and transaction notices go to a group/channel you choose.
- **Dashboard** — one-screen KPIs (users, revenue, orders, pending claims) plus a live
  activity feed of recent events.
- **Pending claims inbox** — every payment claim in one place: screenshot, amount, item and
  one-tap approve / reject, no log-group digging needed.
- **Analytics** — funnel, today / 7-day / 30-day revenue, best sellers, coupon performance.
- **Global search** — find stuffs, users, orders, clones and coupons from one search bar.
- **Offer timers** — schedule a feature toggle or a price change (with optional price
  restore) for a future date/time; flash-sale style promos run themselves.
- **Media library** — reusable photo/video files: name them once, drop them into broadcasts
  or start messages.
- **Bulk ops** — select many stuffs and change price, category, archive or delete them at once.
- **Stuffs controls** — archive/unarchive and pin/unpin straight from the stuffs list.
- **Admin roles** — `full` / `support` / `content` roles: support handles users, tickets and
  audit; content manages stuffs, media and start/help text; full admins do the rest.
- **CRM** — per-user notes and tags on the user card, plus their last orders.
- **Balance confirmations** — adding or removing wallet balance asks for an explicit
  confirmation step (ON/OFF-able).
- **Clone dashboard** — per-clone stats (users, orders, earnings), network totals across
  all clones, and broadcast from any clone with one tap.
- **Backup & restore** — download a fresh backup (SQLite file / JSON export) and restore a
  previous one from the panel (SQLite restores apply on restart).
- **Ticket SLA** — support tickets are sorted by age with a priority hint; canned replies
  let you answer common questions in one tap.
- **Big-sale alert** — admins get pinged whenever an order above a configured amount is paid.

### Togglable features (admin panel → Features)

Every feature below has its own **ON/OFF button** in the panel's *Features* page, so you
can switch them on or off without touching the code:

- **Discount coupons** — create codes (`SAVE10` …) with percent off and max uses; buyers
  apply the code at checkout (balance, manual and Stars payments all honour it).
- **Referral rewards** — share `/start ref_<id>` links; the inviter gets a bonus (configurable)
  automatically on the referred user's first approved purchase.
- **Force join** — buyers must join your channel (`@username` or chat id) before paying.
- **Daily check-in** — users claim a small bonus once per day (bonus configurable).
- **Wishlist** — users save stuffs for later with a Wishlist + button on the stuff page.
- **Purchase history** — users see their approved orders from the menu.
- **Reviews** — users rate stuffs 1–5 stars with text; you approve them in the panel and
  approved reviews show on the stuff page.
- **Categories** — tag stuffs with a category (per-stuff *Category* button); the catalog
  groups and prefixes them when the feature is on.
- **Welcome back** — returning users get a list of stuffs added since their last `/start`.
- **FAQ auto-reply** — unknown texts are matched against your question/answer pairs.
- **Auto backup** — daily database backup (SQLite file / JSON export for MongoDB) posted
  to the log group at the configured time.
- **Daily report** — orders, revenue and user counts posted to the log group daily.
- **Scheduled broadcasts** — queue a message for a specific date/time (server time).
- **Maintenance mode** — non-admins only see a maintenance notice (default OFF).
- **Resume position** — paid viewers jump back to the lesson they stopped at.
- **Leaderboard** — top buyers by revenue, shown from the Features page.
- **Support inbox** — users open tickets from the menu; reply/close them from the panel.
- **Audit log** — important admin actions (creates, deletes, approvals, toggles) are logged.
- **Stats graph** — 7-day order bars added to the Stats page.
- **CSV export** — download the user list as a CSV file.
- **Gift codes** — create wallet voucher codes (`BONUS50` …) in the panel; users redeem
  them from the menu and the balance is added instantly.
- **Catalog search** — users type a keyword and get matching stuffs (name/description/category).
- **Cashback** — buyers get a configurable % of each approved order back in their wallet.
- **Purchase receipts** — buyers receive a receipt message (order id, item, amount, date)
  after every approval.
- **Auto-approve** — payment claims up to a configured amount are approved and delivered
  instantly (default OFF; set the max on the Features page).
- **Anti-flood** — users sending messages too fast get a gentle warning (default OFF).
- **Auto-reject old claims** — pending payment claims older than 48h are rejected and the
  user is notified (default OFF).
- **Access validity** — per-stuff day limit: buyers keep access only for N days after
  purchase (set it with the **Validity** button on the stuff page; countdown shows on the
  stuff page; 0 = forever).
- **Dashboard** — the one-screen overview (default ON).
- **Pending inbox** — the all-claims page (default ON).
- **Analytics** — funnel / revenue / coupon stats (default ON).
- **Global search** — the admin search bar (default ON).
- **Offer timers** — scheduled feature/price changes (default ON).
- **Audience segments** — buyer/non-buyer targeting for broadcasts (default ON).
- **Ticket SLA** — age-based ticket sorting and priorities (default ON).
- **Big-sale alert** — admin ping on large orders (default ON).
- **Media library** — reusable broadcast media (default ON).
- **Bulk ops** — multi-stuff edits (default ON).
- **Stuffs controls** — archive/pin buttons (default ON).
- **Backup & restore** — the Backup panel page (default ON).
- **Admin roles** — role assignment (default ON).
- **CRM** — user notes and tags (default ON).
- **Balance confirm** — extra step on balance changes (default ON).
- **Clone dashboard** — per-clone stats page (default ON).

Amount settings (referral bonus, check-in bonus, force-join channel, backup/report times,
cashback %, auto-approve max) are edited from buttons on the same Features page.

## Requirements

- Python 3.11 or newer
- pip
- A bot token from [@BotFather](https://t.me/BotFather)

## Run locally

```bash
pip install -r requirements.txt
```

Create a file named `.env` next to `config.py` (or copy the example):

```bash
copy .env.example .env
```

`.env` variables (see `config.py`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | yes | — | Bot token from @BotFather, format `123456:ABC-DEF...` |
| `ADMIN_ID` | yes | — | Your numeric Telegram user id (comma/semicolon-separated for several) |
| `DATABASE_URL` | no | `sqlite:///course_bot.db` | `sqlite:///course_bot.db` or `mongodb+srv://user:pass@cluster0.mongodb.net/course_bot` |
| `MONGO_DB_NAME` | no | derived | Not read from `.env` — taken from the `DATABASE_URL` path (`course_bot` if empty) |

There is no `LOG_LEVEL` variable; logging is fixed to `INFO` in `main.py`.

Example `.env`:

```env
BOT_TOKEN=123456:ABC-DEF123456-abc123ABC_abc
ADMIN_ID=123456789
DATABASE_URL=sqlite:///course_bot.db
```

Start the bot:

```bash
python main.py
```

Stop with `Ctrl+C`. The SQLite file `course_bot.db` is created automatically on first run.

## Moving your existing database to a server

Your data (stuffs, users, purchases, settings) lives in `course_bot.db`. Database files
are **not** part of the GitHub repo (`.gitignore`), so a fresh `git clone` on a new
machine starts with an empty database. Move the file yourself:

**Option A - keep using the file (simplest):**

1. Upload `course_bot.db` to the server next to `main.py`:

   ```bash
   scp course_bot.db root@YOUR_SERVER:~/YASHU-BOT/
   ```

   (on Windows use WinSCP or any SFTP client instead of `scp`)
2. In `.env` keep: `DATABASE_URL=sqlite:///course_bot.db`
3. Restart the bot - all your old data is there.

**Option B - import into MongoDB (when `.env` has a `mongodb+srv://...` URL):**

1. Upload `course_bot.db` to the server as above.
2. Make sure `.env` contains your MongoDB `DATABASE_URL`.
3. Run once:

   ```bash
   python tools/import_sqlite.py course_bot.db
   ```

   Old ids are remapped automatically: purchases, balances, pending payment
   requests, methods and settings all keep working. The command is safe to re-run
   (nothing is imported twice).

## Hosting

For every host: set the environment variables below (never commit `.env`).

| Variable | Example |
|---|---|
| `BOT_TOKEN` | `123456:ABC-DEF123456-abc123ABC_abc` |
| `ADMIN_ID` | `123456789` |
| `DATABASE_URL` (local file, default) | `sqlite:///course_bot.db` |
| `DATABASE_URL` (hosting, recommended) | `mongodb+srv://user:pass@cluster0.mongodb.net/course_bot` |

SQLite needs a persistent disk, so on ephemeral hosts (Railway, Heroku, Render, EC2 spot)
use MongoDB Atlas.

### Generic VPS (Ubuntu)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
git clone https://github.com/<you>/<repo>.git /opt/yashu-bot
cd /opt/yashu-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python main.py
```

systemd service — save as `/etc/systemd/system/yashu-bot.service`:

```ini
[Unit]
Description=Yashu Bot (Telegram course seller)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/yashu-bot
EnvironmentFile=/opt/yashu-bot/.env
ExecStart=/opt/yashu-bot/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now yashu-bot
sudo systemctl status yashu-bot
journalctl -u yashu-bot -f
```

pm2 alternative:

```bash
sudo apt install -y npm
sudo npm install -g pm2
cd /opt/yashu-bot
pm2 start main.py --name yashu-bot --interpreter python3
pm2 save
pm2 startup
pm2 logs yashu-bot
```

### AWS EC2

Launch an Ubuntu instance, then SSH in and run the same commands as the generic VPS section
(`apt install`, `git clone`, `venv`, `pip install -r requirements.txt`, systemd).

Security group: allow **outbound all traffic** (the bot only calls the Telegram API).
No inbound port is needed because the bot uses long polling; you can leave inbound closed
except SSH (port 22) from your IP.

```bash
ssh -i your-key.pem ubuntu@<EC2-PUBLIC-IP>
```

### Railway

CLI path:

```bash
npm install -g @railway/cli
railway login
railway init
railway up
railway variables set BOT_TOKEN=123456:ABC-DEF123456-abc123ABC_abc
railway variables set ADMIN_ID=123456789
railway variables set DATABASE_URL="mongodb+srv://user:pass@cluster0.mongodb.net/course_bot"
railway service status
```

Or import the GitHub repository in the Railway dashboard (New Project -> GitHub repo) and set
the same variables under *Variables*. The bundled `railway.json` pins the build
(Dockerfile) and the start command (`python main.py`), so no dashboard config is required.

### Heroku

```bash
heroku create yashu-bot
heroku buildpacks:set heroku/python
heroku config:set BOT_TOKEN=123456:ABC-DEF123456-abc123ABC_abc
heroku config:set ADMIN_ID=123456789
heroku config:set DATABASE_URL="mongodb+srv://user:pass@cluster0.mongodb.net/course_bot"
git push heroku main
heroku ps
heroku logs --tail
```

The bundled `app.json` also enables one-click deploy from the Heroku dashboard with the same
three config vars. Dyno type: `heroku ps:scale worker=1` (the `Procfile` defines a `worker`,
not a `web` process — this is a polling bot and serves no HTTP traffic).

### Render

Create a service from the GitHub repo (**New -> Web Worker** or **Web Service**):

- Environment: `Python 3.11`
- Build command: `pip install -r requirements.txt`
- Start command: `python main.py`

Add the environment variables (`BOT_TOKEN`, `ADMIN_ID`, `DATABASE_URL`) under *Environment*.
A Background Worker avoids the "no open port" health-check issue; if you pick a Web Service,
disable health checks. Render's disk is ephemeral — use `DATABASE_URL=mongodb+srv://...`.

### Docker

Build:

```bash
docker build -t yashu-bot .
```

Run with the `.env` file mounted/loaded (create an empty DB file first if you want SQLite
persistence):

```bash
touch course_bot.db
docker run -d --name yashu-bot --restart unless-stopped --env-file .env -v "%cd%/course_bot.db:/app/course_bot.db" yashu-bot
```

On Linux/macOS use `"$(pwd)/course_bot.db:/app/course_bot.db"` instead of `"%cd%/..."`.
Logs:

```bash
docker logs -f yashu-bot
```

## Admin usage

The admin panel opens **only** for configured admin IDs (`ADMIN_ID` in `.env` plus any admins
added inside the panel). Everyone else gets no response from `/admin`.

### /admin panel sections

- **Add stuff** — *Upload from ZIP* or *Direct video forwarding* (send videos/photos one by one).
- **Stuffs** — open a stuff: add videos, demo list (tap to toggle demo/paid), rename, price,
  showcase image, proofs, **category**, **validity (days)**, preview, delete,
  **Receive direct** (all media sent into the chat)
  and **Download ZIP** (one archive with `info.txt`, videos, demo files, cover and proofs).
  Archive / unarchive and pin / unpin from the list; **Bulk mode** selects many stuffs
  to change price/category, archive, restore or delete together.
- **Start message** — set text / photo / video, preview, clear media.
- **Help text** — edit the text users see when they press the **Help** button (HTML supported).
- **Admins** — add by forwarded message or numeric id, remove (env admins cannot be removed);
  assign **full / support / content** roles when the Roles feature is ON.
- **Payment methods** — crypto address or QR (UPI/INR) methods, Telegram Stars with
  *Star receiver @username*, activate/deactivate/delete.
- **Protection** — send content with forwarding/cloud-save disabled.
- **Log group** — forward a message from your group/channel (bot must be admin there).
- **Users** — user manager: card with ban/unban, add/remove balance (confirmation when the
  Balance confirm feature is ON), **notes and tags** (CRM), last orders, quick jumps.
- **Broadcast** — pick an audience (everyone / buyers / non-buyers / a stuff's buyers /
  clone-specific), send **text, photo, video, GIF or sticker** as-is with preview and confirm;
  **Broadcast from clone** on the clone card sends it through that bot.
- **Stats** — users, stuffs, methods, requests, revenue, wallet balances, uptime
  (plus a 7-day order graph when the graph feature is ON).
- **VPS** — CPU, RAM, disk, process usage, bot/system uptime, active database backend.
- **Clone bots** — list every clone with running/stopped status; start, stop or remove them
  (removal asks for confirmation). **Clone dashboard**: per-clone users, orders, earnings,
  network totals and broadcast-from-clone.
- **Features** — 44 ON/OFF toggles (the 28 from earlier batches plus dashboard, pending
  inbox, analytics, global search, offer timers, audience segments, ticket SLA, big-sale
  alert, media library, bulk ops, stuffs controls, backup & restore, admin roles, CRM,
  balance confirm, clone dashboard) plus the buttons for referral bonus, check-in bonus,
  force-join channel, backup time, report time, cashback %, auto-approve max, big-sale
  threshold, Leaderboard, Audit log and CSV export.
- **Dashboard** — KPIs (users, revenue, orders, pending claims) + activity feed.
- **Pending** — inbox of payment claims with screenshot, approve/reject per claim.
- **Analytics** — funnel, today/7d/30d revenue, best sellers, coupon performance, order graph.
- **Search** — one bar for stuffs, users, orders, clones, coupons.
- **Coupons** — add a coupon (code, percent, max uses), toggle it on/off, delete it.
- **Gift codes** — wallet voucher codes: add (amount + code or random), disable, delete.
- **Reviews** — approve or delete user reviews.
- **Support** — open tickets: age/priority sorting, view, reply, one-tap **canned replies**
  (add/delete), close.
- **Schedule** — pending scheduled broadcasts: add or delete.
- **Timers** — offer timers: schedule a feature toggle or a price change (+ optional restore
  timer) for a date/time, delete pending ones.
- **Media** — media library: add named photo/video media, preview or delete, reuse in
  broadcasts and start messages.
- **Bulk ops** — on the Stuffs page: select stuffs, then change price/category, archive,
  restore or delete them together.
- **Backup** — download a fresh backup or upload a previous backup file to restore
  (SQLite: applies on the next restart).
- **FAQ** — question/answer pairs for the auto-reply feature: add or delete.
- **Audit log** — the recent admin activity trail.

Payment screenshots arrive in the log group with **Approve & unlock** / **Reject** buttons;
approving delivers the media to the buyer immediately.

### /clone BOT_TOKEN

```
/clone 123456:ABC-DEF123456-abc123ABC_abc
```

Then choose the mode:

- **Same clone** — the clone bot starts with a copy of all stuffs/categories from the main bot.
- **New database** — the clone bot starts empty; you add stuffs yourself.

The clone runs inside the same process as the main bot: same admin IDs, same users and
balance, but its own stuffs, payment methods and settings (scoped automatically in the
same database). Removing a clone from the panel stops it and deletes only its own data.
The message containing the clone bot token is deleted automatically right after you send it.

### ZIP upload format

Send one `.zip` (max 50 MB) in *Add stuff -> Upload from ZIP*:

```
info.txt
video_1.mp4
video_1.txt
video_2.mp4
demo_1.mp4
cover.jpg
proof_1.jpg
proof_2.jpg
```

- `info.txt` — one `key: value` per line:
  `name:`, `description:`, `price:` (e.g. `199`), optional `demo: true` (makes every media file
  a demo). `#` lines are comments.
- `video_1.mp4` + optional `video_1.txt` — the sidecar text becomes the description; a first
  line starting with `#` becomes the lesson title.
- **`demo*` prefix** (e.g. `demo_1.mp4`) — file is a free demo lesson.
- **`cover*` prefix photo** (or `showcase*`) — the showcase image of the stuff.
- **`proof*` prefix photos** (or `banner*`) — proof screenshots for the proofs gallery.
- Videos: `mp4, mov, mkv, webm, avi, m4v`; images: `jpg, jpeg, png, webp, gif`.
- Files are ordered naturally by number (`video_1`, `video_2`, …).

## Backup

With the default SQLite backend, back up the single file `course_bot.db` (copy it while the
bot is stopped, or periodically). With MongoDB Atlas use Atlas snapshots or `mongodump`.

From the admin panel (**Backup**) you can also download a fresh backup at any moment, or
upload an earlier backup file to restore it: MongoDB restores apply immediately, SQLite
restores apply on the next bot restart.
