# Divine Books

Accounting and billing for Divine Technologies. Flask + SQLite, with a Telegram bot that collects bills and files.

```bash
pip install -r requirements.txt
python3 app.py        # web app on http://127.0.0.1:5050
python3 bot.py        # Telegram bot (needs TELEGRAM_BOT_TOKEN in .env)
```

Settings go in a `.env` file that is never committed:

```
TELEGRAM_BOT_TOKEN=...
APP_PASSWORD=...      # turns on the login page; always set this on the internet
API_KEY=...           # lets the bot post drafts when APP_PASSWORD is set
HTTPS=1               # when served over https
```

Data lives in `books.db` and `uploads/` (both git-ignored). Use the Backup link in the sidebar to download a copy.

Not ready for serverless hosting such as Vercel: the database is a local file and the bot is a long-running process. See PLAN.md.
