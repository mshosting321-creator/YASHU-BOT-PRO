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
- **Broadcasts** — send an HTML message to every user with preview and confirmation.
- **User manager** — look up a user by ID, ban/unban, add/remove balance.
- **Stats / VPS panel** — users, revenue, payment requests, plus live CPU/RAM/disk/uptime.
- **Log group** — payment screenshots and transaction notices go to a group/channel you choose.

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
  showcase image, proofs, preview, delete, **Receive direct** (all media sent into the chat)
  and **Download ZIP** (one archive with `info.txt`, videos, demo files, cover and proofs).
- **Start message** — set text / photo / video, preview, clear media.
- **Help text** — edit the text users see when they press the **Help** button (HTML supported).
- **Admins** — add by forwarded message or numeric id, remove (env admins cannot be removed).
- **Payment methods** — crypto address or QR (UPI/INR) methods, Telegram Stars with
  *Star receiver @username*, activate/deactivate/delete.
- **Protection** — send content with forwarding/cloud-save disabled.
- **Log group** — forward a message from your group/channel (bot must be admin there).
- **Users** — user manager: card with ban/unban and add/remove balance.
- **Broadcast** — send HTML text to all users with preview and confirm.
- **Stats** — users, stuffs, methods, requests, revenue, wallet balances, uptime.
- **VPS** — CPU, RAM, disk, process usage, bot/system uptime, active database backend.
- **Clone bots** — list every clone with running/stopped status; start, stop or remove them.

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
