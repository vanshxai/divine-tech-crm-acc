"""Telegram bot: saves every file, photo or text an approved person sends into uploads/ and the Inbox page.
Run:  python3 bot.py        (token comes from .env, never from code)"""
import os, re, ssl, json, time, sqlite3, secrets, datetime as dt, urllib.request, urllib.parse
from pathlib import Path
from app import DB, UPLOADS
try:
    import certifi; CTX = ssl.create_default_context(cafile=certifi.where())     # this Mac's Python ships without root certificates
except ImportError: CTX = ssl.create_default_context()

API, LIMIT = "https://api.telegram.org", 20 * 1024 * 1024          # Telegram bots can download files up to 20 MB
NAMES = {"voice": "voice.ogg", "video": "video.mp4", "video_note": "note.mp4", "audio": "audio.mp3", "animation": "clip.mp4", "document": "file"}
now = lambda: dt.datetime.now().strftime("%d %b %H:%M")

def token():
    t = os.environ.get("TELEGRAM_BOT_TOKEN")
    env = Path(__file__).with_name(".env")
    if not t and env.exists():
        t = next((l.split("=", 1)[1].strip() for l in env.read_text().splitlines() if l.startswith("TELEGRAM_BOT_TOKEN=")), None)
    return t or exit("Put TELEGRAM_BOT_TOKEN=... in the .env file")

class Tg:
    def __init__(self, tok): self.tok = tok
    def call(self, method, **p):
        return json.load(urllib.request.urlopen(f"{API}/bot{self.tok}/{method}", urllib.parse.urlencode(p).encode(), timeout=70, context=CTX))["result"]
    def say(self, chat, text):
        try: self.call("sendMessage", chat_id=chat, text=text)
        except Exception as e: print("could not reply:", type(e).__name__)
    def file(self, file_id):
        return urllib.request.urlopen(f"{API}/file/bot{self.tok}/{self.call('getFile', file_id=file_id)['file_path']}", timeout=120, context=CTX).read()

def attachment(m):
    """(file_id, unique_id, name, size, kind) of whatever the message carries, or None."""
    if m.get("photo"):
        p = m["photo"][-1]; return p["file_id"], p["file_unique_id"], "photo.jpg", p.get("file_size", 0), "photo"
    for k in ("document", "video", "audio", "voice", "video_note", "animation"):
        if d := m.get(k): return d["file_id"], d["file_unique_id"], d.get("file_name") or NAMES[k], d.get("file_size", 0), k

def handle(u, tg, c):
    m = u.get("message") or u.get("channel_post")
    if not m: return
    chat, mid, text = m["chat"]["id"], m["message_id"], (m.get("text") or "").strip()
    who = " ".join(filter(None, [m.get("from", {}).get("first_name"), m.get("from", {}).get("last_name")])) or str(chat)
    paired = c.execute("select 1 from tg_users where chat_id=?", (chat,)).fetchone()
    if text.startswith(("/start", "/pair")):
        code = (text.split() + [""])[1]
        real = (c.execute("select value from settings where key='pair_code'").fetchone() or [""])[0]
        if code and real and secrets.compare_digest(code, real):
            c.execute("insert or replace into tg_users(chat_id,name,added) values(?,?,?)", (chat, who, now()))
            c.execute("delete from settings where key='pair_code'")            # one-time code
            return tg.say(chat, "✅ Connected. Send bills, photos, PDFs or Excel files here and they will be saved.")
        return tg.say(chat, "✅ You are connected. Send files here." if paired else "Send /start followed by the 6-digit code shown on the Inbox page of Divine Books.")
    if not paired: return tg.say(chat, "Not connected. Send /start and the 6-digit code from the Inbox page.")
    a = attachment(m)
    if a and a[3] > LIMIT: return tg.say(chat, "⚠ This file is bigger than 20 MB, which Telegram bots cannot download. Please send a smaller file.")
    if not a and not text: return tg.say(chat, "I can save files, photos and text messages.")
    fid, uid, name, size, kind = a or (None, "text", "", 0, "text")
    if c.execute("select 1 from inbox where chat_id=? and msg_id=? and uid=?", (chat, mid, uid)).fetchone(): return
    rel = None
    if fid:
        data = tg.file(fid); name = re.sub(r"[^\w.\- ]", "_", name)[:120] or "file"
        rel = f"{dt.date.today():%Y-%m}/{mid}_{uid}_{name}"
        (UPLOADS / rel).parent.mkdir(parents=True, exist_ok=True); (UPLOADS / rel).write_bytes(data); size = len(data)
    c.execute("insert into inbox(received,chat_id,msg_id,sender,kind,filename,path,size,caption,uid) values(?,?,?,?,?,?,?,?,?,?)",
              (now(), chat, mid, who, kind, name, rel, size, m.get("caption") or text, uid))
    pending = c.execute("select count(*) from inbox where status='new'").fetchone()[0]
    tg.say(chat, f"✅ Saved. {pending} item{'s' if pending != 1 else ''} waiting in the Inbox.")

def main():
    tg = Tg(token()); print("Bot running as @" + tg.call("getMe")["username"], flush=True)
    try: tg.call("deleteWebhook")                  # long polling and webhooks cannot both be on
    except Exception: pass
    off = 0
    while True:
        try:
            for u in tg.call("getUpdates", offset=off, timeout=50):
                off = u["update_id"] + 1
                c = sqlite3.connect(DB, timeout=30)
                try:
                    with c: handle(u, tg, c)
                except Exception as e:
                    print("failed:", type(e).__name__, e, flush=True)
                    if chat := (u.get("message") or {}).get("chat", {}).get("id"): tg.say(chat, "⚠ Could not save that. Please send it again.")
                finally: c.close()
        except Exception as e:
            print("network:", type(e).__name__, flush=True); time.sleep(5)

if __name__ == "__main__": main()
