"""Shared by the web app and the Telegram bot: turn an uploaded file into a draft."""
import json, datetime as dt
import reader

def parties(c): return [(r[0], r[1]) for r in c.execute("select id, name from clients where kind in ('customer','both')")]
def items(c): return {r[0]: {"hsn": r[1], "unit": r[2], "gst": r[3], "sale": r[4]} for r in c.execute("select name, hsn, unit, gst, sale from items")}

def add_draft(c, data): return c.execute("insert into drafts(created,data) values(?,?)", (dt.datetime.now().strftime("%d %b %H:%M"), json.dumps(data))).lastrowid

def to_draft(c, path, inbox_id):
    """Read a bill file. Returns (draft_id, result), or (None, reason) when it is not a readable supplier bill."""
    get = lambda k: (c.execute("select value from settings where key=?", (k,)).fetchone() or [""])[0]
    try: r = reader.read(path, get("co_gstin"), ours_name=get("co_name"))
    except Exception: return None, "Could not read this file. It stays in the Inbox."
    if not r: return None, "No bill found in this file. It stays in the Inbox."
    if r.get("is_ours"): return None, "This is your own sales invoice, not a supplier bill. It stays in the Inbox."
    r["inbox_id"], r["kind"] = inbox_id, "purchase"
    c.execute("update inbox set status='done' where id=?", (inbox_id,))
    return add_draft(c, r), r
