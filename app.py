import os, sqlite3, json, re, time, secrets, hmac, mimetypes, datetime as dt
from pathlib import Path
from decimal import Decimal as D, ROUND_HALF_UP
from flask import Flask, Request, request, redirect, render_template_string as R, g, session, Response, send_from_directory, abort
from gujarati import GU
from werkzeug.utils import secure_filename
import intake, quick

OWN_STATE, OWN_CODE, PREFIX = "Gujarat", "24", "DT"   # same state -> CGST+SGST, else IGST
DB = Path(__file__).with_name("books.db")
for _l in (Path(__file__).with_name(".env").read_text().splitlines() if Path(__file__).with_name(".env").exists() else []):
    if "=" in _l and not _l.startswith("#"): os.environ.setdefault(*_l.split("=", 1))
UPLOADS = Path(__file__).with_name("uploads")
PASSWORD, API_KEY, PER = os.environ.get("APP_PASSWORD"), os.environ.get("API_KEY"), 100   # no APP_PASSWORD = open on this computer only

class Req(Request): max_form_parts, max_form_memory_size = 500_000, 100_000_000        # a bill can have thousands of fields

app = Flask(__name__); app.request_class = Req
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.environ.get("HTTPS")), MAX_CONTENT_LENGTH=40 * 1024 * 1024)

SCHEMA = """
create table if not exists clients(id integer primary key, name text not null, phone, gstin, state, address, kind text default 'customer');
create table if not exists items(id integer primary key, name text not null, unit text, hsn text, gst text, sale text, purch text, opening text);
create table if not exists jobs(id integer primary key, client_id int, title text, status text default 'enquiry', followup text);
create table if not exists invoices(id integer primary key, no text unique, date text, client_id int, narration text, taxable int, cgst int, sgst int, igst int, ro int, total int, voucher int, cancelled int default 0, incl int default 0, meta text, due text);
create table if not exists lines(invoice_id int, descr text, qty text, rate text, gst text, hsn text, unit text, item_id int, disc text, taxable int, taxamt int);
create table if not exists payments(id integer primary key, date text, invoice_id int, amount int, mode text, voucher int, cancelled int default 0);
create table if not exists purchases(id integer primary key, no text unique, date text, party_id int, sup_no text, sup_date text, narration text, taxable int, cgst int, sgst int, igst int, ro int, total int, voucher int, draft_id int, cancelled int default 0, incl int default 0, meta text, due text, job_id int, doctype text default 'tax', calc_total int);
create table if not exists plines(purchase_id int, descr text, qty text, rate text, gst text, hsn text, unit text, item_id int, disc text, taxable int, taxamt int);
create table if not exists ppayments(id integer primary key, date text, purchase_id int, amount int, mode text, voucher int, cancelled int default 0);
create table if not exists drafts(id integer primary key, created text, data text, status text default 'new');
create table if not exists settings(key text primary key, value text);
create table if not exists chat_pending(token text primary key, action text, created text);
create table if not exists tg_users(chat_id integer primary key, name text, added text);
create table if not exists inbox(id integer primary key, received text, chat_id int, msg_id int, sender text, kind text, filename text, path text, size int, caption text, uid text, status text default 'new');
create unique index if not exists inbox_uq on inbox(chat_id, msg_id, uid);
create table if not exists ledger(id integer primary key, voucher int, date text, account text, client_id int, dr int default 0, cr int default 0);
create trigger if not exists l_upd before update on ledger begin select raise(abort,'ledger is append-only'); end;
create trigger if not exists l_del before delete on ledger begin select raise(abort,'ledger is append-only'); end;
"""
UPGRADES = ("alter table clients add column kind text default 'customer'", "alter table lines add column hsn text", "alter table lines add column unit text",
            "alter table lines add column item_id int", "alter table plines add column unit text", "alter table plines add column item_id int",
            "alter table invoices add column narration text", "alter table lines add column disc text", "alter table lines add column taxable int",
            "alter table lines add column taxamt int", "alter table plines add column disc text", "alter table plines add column taxable int",
            "alter table plines add column taxamt int", "alter table invoices add column incl int default 0", "alter table invoices add column meta text",
            "alter table invoices add column due text", "alter table purchases add column incl int default 0", "alter table purchases add column meta text",
            "alter table purchases add column due text", "alter table purchases add column job_id int", "alter table purchases add column doctype text default 'tax'",
            "alter table purchases add column calc_total int")
VIEWS = """
drop view if exists inv_due; drop view if exists pur_due;
create view inv_due as select i.id, i.no, i.date, i.client_id, c.name, i.total, i.cancelled, 'tax' as doctype, i.due as due_date,
  i.total - coalesce((select sum(amount) from payments where invoice_id=i.id and cancelled=0),0) as due
  from invoices i join clients c on c.id=i.client_id;
create view pur_due as select p.id, p.no, p.date, p.party_id, c.name, p.sup_no, p.total, p.cancelled, coalesce(p.doctype,'tax') as doctype, p.due as due_date,
  p.total - coalesce((select sum(amount) from ppayments where purchase_id=p.id and cancelled=0),0) as due
  from purchases p join clients c on c.id=p.party_id;
"""
_c = sqlite3.connect(DB); _c.executescript(SCHEMA)
for _s in UPGRADES:
    try: _c.execute(_s)
    except sqlite3.OperationalError: pass
_c.executescript(VIEWS); _c.commit(); _c.close()

# ---------- money: integer paise, Decimal maths, half-up rounding ----------
rnd = lambda x: int(D(x).quantize(D(1), ROUND_HALF_UP))
paise = lambda x: rnd(D(x) * 100)
app.add_template_filter(lambda p: f"{D(p) / 100:,.2f}", "rs")
app.add_template_filter(lambda x: f"{x:g}", "qty")

class Oops(Exception): pass

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def _close(_):
    if c := g.pop("db", None): c.close()

q = lambda sql, *a: db().execute(sql, a).fetchall()
one = lambda sql, *a: db().execute(sql, a).fetchone()

def post(date, rows):
    """Append one balanced voucher. rows = (account, client_id, dr, cr)."""
    dt.date.fromisoformat(date)
    if sum(r[2] for r in rows) != sum(r[3] for r in rows): raise Oops("Entry does not balance")
    v = one("select coalesce(max(voucher),0)+1 from ledger")[0]
    db().executemany("insert into ledger(voucher,date,account,client_id,dr,cr) values(?,?,?,?,?,?)", [(v, date, *r) for r in rows])
    return v

def reverse(voucher):
    rows = q("select account,client_id,dr,cr from ledger where voucher=?", voucher)
    post(str(dt.date.today()), [(a, c, cr, dr) for a, c, dr, cr in rows])

def gstin_ok(x):
    C = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if len(x) != 15 or any(c not in C for c in x): return False
    s = sum((lambda v: v // 36 + v % 36)(C.index(c) * (1 + i % 2)) for i, c in enumerate(x[:14]))
    return C[(36 - s % 36) % 36] == x[14]

def intra(p):
    x = (p["gstin"] or "").strip()
    return x[:2] == OWN_CODE if len(x) == 15 else not (p["state"] or "").strip() or p["state"].strip().lower() == OWN_STATE.lower()

UNITS = {"mtr": "Mtr", "mts": "Mtr", "mt": "Mtr", "meter": "Mtr", "metre": "Mtr", "meters": "Mtr", "m": "Mtr",
         "pcs": "Pcs", "pc": "Pcs", "nos": "Pcs", "no": "Pcs", "piece": "Pcs", "pieces": "Pcs", "bdl": "Bdl", "bundle": "Bdl", "bundles": "Bdl",
         "roll": "Roll", "rolls": "Roll", "rl": "Roll", "kg": "Kg", "kgs": "Kg", "ltr": "Ltr", "lt": "Ltr", "litre": "Ltr", "liter": "Ltr",
         "box": "Box", "boxes": "Box", "set": "Set", "sets": "Set"}
unit_norm = lambda u: UNITS.get(u.strip().lower().rstrip("."), u.strip())      # Mtr / Mts / Meter all become Mtr
META = {"place": "Place of supply", "rcm": "Reverse charge", "transport": "Transport", "vehicle": "Vehicle no.", "eway": "E-way bill no.",
        "challan": "Challan no.", "po": "P.O. / order no.", "shipto": "Ship to"}
meta_of = lambda f: json.dumps({k: f.get(k, "").strip() for k in META if f.get(k, "").strip()})
cfg = lambda k, d="": (one("select value from settings where key=?", k) or [d])[0]

def parse(f):
    out = []
    for d, qty, rate, gst, hsn, unit, disc in zip(*(f.getlist(k) for k in ("d", "q", "r", "g", "h", "u", "x"))):
        d, rate = d.strip(), rate.strip()
        if not d and not rate: continue
        if not rate: raise Oops(f"Rate is missing for {d}")
        out.append((d, qty, rate, gst, hsn.strip(), unit_norm(unit), disc.strip()))
    if not out: raise Oops("Add at least one item")
    return out

def calc(lines, same_state, incl=False):
    """Per-line (taxable, tax) in paise, then totals. Rates may include tax; discount may be like 10+5."""
    rows, groups = [], {}
    for d, qty, rate, g_, _h, _u, disc in lines:
        parts = [p for p in disc.split("+") if p.strip()]
        if not d or D(qty) <= 0 or D(rate) < 0 or not 0 <= D(g_) <= 28 or any(not 0 <= D(p) <= 100 for p in parts): raise Oops("Check item, quantity, rate, discount and GST")
        f = D(1)
        for p in parts: f *= 1 - D(p) / 100
        gross = paise(D(qty) * D(rate) * f)
        t = rnd(D(gross) / (1 + D(g_) / 100)) if incl else gross
        x = gross - t if incl else rnd(D(t) * D(g_) / 100)
        rows.append((t, x)); gg = groups.setdefault(D(g_), [0, 0]); gg[0] += t; gg[1] += x
    taxable, gst = sum(r[0] for r in rows), sum(r[1] for r in rows)
    cg = sum(v[1] // 2 for v in groups.values()) if same_state else 0
    sg = gst - cg if same_state else 0
    ig = 0 if same_state else gst
    exact = taxable + gst; total = (exact + 50) // 100 * 100
    return rows, taxable, cg, sg, ig, total - exact, total

def rate_groups(rows, same_state):
    """[(rate, taxable, cgst, sgst, igst)] from stored per-line amounts, for the printed tax summary."""
    g = {}
    for rate, t, x in rows: a = g.setdefault(D(rate), [0, 0]); a[0] += t; a[1] += x
    return [(r, a[0], a[1] // 2 if same_state else 0, a[1] - a[1] // 2 if same_state else 0, 0 if same_state else a[1]) for r, a in sorted(g.items())]

ONES = "One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen".split()
TENS = "Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()
below100 = lambda n: ONES[n - 1] if n < 20 else TENS[n // 10 - 2] + (" " + ONES[n % 10 - 1] if n % 10 else "")
def words(p):
    r, ps = divmod(p, 100); out = []
    for div, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand")):
        if r >= div: out.append(below100(r // div) + " " + name); r %= div
    if r >= 100: out.append(ONES[r // 100 - 1] + " Hundred" + (" " + below100(r % 100) if r % 100 else ""))
    elif r: out.append(below100(r))
    return "Rupees " + (" ".join(out) or "Zero") + (f" and {below100(ps)} Paise" if ps else "") + " Only"
app.add_template_filter(lambda p: words(int(p)), "words")

def inv_no(date):
    """Next sales invoice number, continuing the firm's own series (e.g. D26-001), restarting each financial year."""
    d = dt.date.fromisoformat(date); pre = f"{cfg('inv_prefix', 'D')}{(d.year - (d.month < 4)) % 100:02d}-"
    used = [int(r[0][len(pre):]) for r in q("select no from invoices where no like ?", pre + "%") if r[0][len(pre):].isdigit()]
    if used: return f"{pre}{max(used) + 1:03d}"
    start = cfg("inv_first_for")
    if start == pre: return f"{pre}{int(cfg('inv_first')):03d}"        # the number set on the Company page, for that financial year
    if start and pre > start: return f"{pre}001"                       # a new financial year restarts at 001
    raise Oops("Set the next invoice number first: Company, Invoice numbering")

def need_company():
    if not (cfg("co_name") and cfg("co_gstin") and cfg("co_addr")): raise Oops("Fill in Company details first (name, address, GSTIN) so the bill is a valid tax invoice")

def vno(pre, table, date):
    d = dt.date.fromisoformat(date); y = d.year - (d.month < 4); p = f"{pre}/{y}-{(y + 1) % 100:02d}/"
    return p + f"{one(f'select count(*) from {table} where no like ?', p + '%')[0] + 1:04d}"

iid = lambda name: (one("select id from items where lower(name)=lower(?)", name) or [None])[0]
STOCK = """select i.*, cast(i.opening as real)
  + coalesce((select sum(cast(qty as real)) from plines p join purchases h on h.id=p.purchase_id where p.item_id=i.id and h.cancelled=0 and coalesce(h.doctype,'tax')='tax'),0)
  - coalesce((select sum(cast(qty as real)) from lines l join invoices h on h.id=l.invoice_id where l.item_id=i.id and h.cancelled=0),0) as stock from items i"""

def parties(kinds, acc, a, b):
    """Parties for a voucher's dropdown, with their balance and GST type so the form can show them."""
    return [dict(r, intra=int(intra(r))) for r in q(f"""select c.*, coalesce((select sum({a}-{b}) from ledger where account='{acc}' and client_id=c.id),0) bal
                                                       from clients c where kind in ({kinds}) order by name""")]

def item_data():
    rows = q("select * from items order by name")
    return rows, {r["name"].lower(): {"hsn": r["hsn"], "unit": r["unit"], "gst": r["gst"], "sale": r["sale"], "buy": r["purch"]} for r in rows}

# ---------- layout (Busy-style groups: Masters / Transactions / Reports) ----------
NAV = [(None, [("/", "Home", "M3 11l9-8 9 8M5 10v10h14V10", "/")]),
       ("Masters", [("/clients", "Parties", "M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2M9 11a4 4 0 100-8 4 4 0 000 8M22 21v-2a4 4 0 00-3-3.9M16 3.1a4 4 0 010 7.8", "/client"),
                    ("/items", "Items", "M21 8l-9-5-9 5v8l9 5 9-5zM3 8l9 5 9-5M12 13v8", "/item"),
                    ("/company", "Company", "M3 21h18M5 21V5l7-2 7 2v16M9 9h2M13 9h2M9 13h2M13 13h2", "/company")]),
       ("Transactions", [("/invoices", "Sales", "M14 3H6v18h12V7zM14 3v4h4M9 12h6M9 16h6", "/invoice"),
                         ("/purchases", "Purchase", "M6 2L3 6v14h18V6l-3-4zM3 6h18M16 10a4 4 0 01-8 0", "/purchase"),
                         ("/receipt", "Receipt", "M17 7L7 17M16 17H7V8", "/receipt"),
                         ("/payment", "Payment", "M7 17L17 7M8 7h9v9", "/payment"),
                         ("/drafts", "Drafts", "M22 12h-6l-2 3h-4l-2-3H2M5.5 5h13L22 12v7H2v-7z", "/draft"),
                         ("/inbox", "Inbox", "M4 4h16v12h-5l-3 3-3-3H4z", "/inbox")]),
       ("Reports", [("/outstanding", "Outstanding", "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01", "/outstanding"),
                    ("/ledger", "Books", "M4 5a2 2 0 012-2h13v16H6a2 2 0 00-2 2zM4 21V5M8 7h7", "/ledger")]),
       ("CRM", [("/jobs", "Jobs", "M3 7h18v13H3zM8 7V4h8v3", "/job")])]

HEAD = """<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Divine Books</title>
<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Noto+Sans+Gujarati:wght@400;500;600&display=swap">
<style>
:root{--bg:#f5f6f8;--card:#fff;--line:#e5e8ee;--ink:#0f1720;--mute:#667085;--acc:#0b6b5f;--acc-ink:#fff;--acc-soft:#e3f3f0;--bad:#b42318;--bad-soft:#fde8e6;--warn:#9a5b00;--warn-soft:#fff1d6;--ok:#157347;--ok-soft:#e2f5ea;--hover:#f1f3f7;--side:#0f1720;--side-ink:#aab3c0}
@media(prefers-color-scheme:dark){:root{--bg:#0b0f14;--card:#141a21;--line:#252d38;--ink:#e8ebf0;--mute:#8d97a5;--acc:#2fbfa9;--acc-ink:#06201b;--acc-soft:#10312c;--bad:#ff8a80;--bad-soft:#3a1714;--warn:#f5c26b;--warn-soft:#38290b;--ok:#6fd49b;--ok-soft:#0f2f1e;--hover:#1a222b;--side:#0a0d11;--side-ink:#8d97a5}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
a{color:var(--acc);text-decoration:none}h1{font-size:24px;letter-spacing:-.02em;margin:0}h2{font-size:15px;margin:0 0 12px}
.app{display:grid;grid-template-columns:224px 1fr;min-height:100vh}
aside{background:var(--side);padding:20px 12px;position:sticky;top:0;height:100vh;overflow-y:auto;display:flex;flex-direction:column;gap:2px}
.brand{color:#fff;font-weight:700;font-size:17px;letter-spacing:-.01em;padding:4px 12px 14px}.brand small{display:block;font-weight:500;font-size:12px;color:var(--side-ink)}
.grp{color:var(--side-ink);font-size:11px;text-transform:uppercase;letter-spacing:.08em;padding:14px 12px 4px;opacity:.7}
aside a{display:flex;gap:12px;align-items:center;color:var(--side-ink);padding:7px 12px;border-radius:8px;font-weight:500}
aside a:hover{background:#ffffff14;color:#fff}aside a.on{background:#ffffff1f;color:#fff}
aside em{margin-left:auto;font-style:normal;background:var(--acc);color:var(--acc-ink);border-radius:99px;font-size:12px;padding:0 8px;font-weight:700}
aside svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}
main{padding:32px 40px 64px;max-width:1120px;width:100%}
.head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:20px;overflow-x:auto}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px}
.stat span{color:var(--mute);font-size:13px;font-weight:500}.stat b{display:block;font-size:26px;letter-spacing:-.02em;margin-top:4px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:20px}
table{width:100%;border-collapse:collapse}th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);font-weight:600;text-align:left;padding:0 10px 10px}
td{padding:12px 10px;border-top:1px solid var(--line)}tbody tr:hover td{background:var(--hover)}.r{text-align:right}.mute{color:var(--mute)}
.btn,button{display:inline-flex;align-items:center;gap:8px;background:var(--acc);color:var(--acc-ink);border:1px solid transparent;border-radius:9px;padding:0 16px;height:40px;font:600 14px Inter,system-ui,sans-serif;cursor:pointer}
.btn:hover,button:hover{filter:brightness(1.08)}.ghost{background:transparent;color:var(--ink);border-color:var(--line)}.danger{background:var(--bad-soft);color:var(--bad)}
input,select,textarea{font:15px Inter,system-ui,sans-serif;height:40px;padding:0 12px;border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--ink);width:100%}
input:focus,select:focus,button:focus-visible,a:focus-visible{outline:2px solid var(--acc);outline-offset:1px}td input{height:36px}
label{font-size:13px;font-weight:500;color:var(--mute);display:block;margin-bottom:4px}.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;align-items:end}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:600;background:var(--hover);color:var(--mute)}
.pill.active,.pill.Due,.pill.new{background:var(--warn-soft);color:var(--warn)}.pill.done,.pill.Paid,.pill.invoiced{background:var(--ok-soft);color:var(--ok)}
.pill.quoted,.pill.Part{background:var(--acc-soft);color:var(--acc)}.pill.Cancelled,.pill.dismissed{background:var(--bad-soft);color:var(--bad)}
.empty{color:var(--mute);padding:8px 0}.sel{width:auto;height:34px}.note{background:var(--warn-soft);color:var(--warn);border-radius:9px;padding:10px 14px;margin-bottom:16px}
.pinfo{margin:8px 0 0;font-size:13px;color:var(--mute);min-height:20px}.ro{padding:9px 12px;border:1px solid var(--line);border-radius:9px;background:var(--hover);height:40px}
.vfoot{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:20px;margin-top:20px}.vfoot table td,.vfoot table th{padding:7px 6px}
.bigtot{font-size:30px;font-weight:700;letter-spacing:-.02em;text-align:right;border-top:2px solid var(--ink);padding-top:10px;margin-top:6px}
.fl{animation:fl 1.6s ease both}@keyframes fl{from{background:var(--acc-soft);box-shadow:0 0 0 2px var(--acc)}to{background:var(--card);box-shadow:none}}
#toast{position:fixed;right:20px;bottom:84px;background:var(--card);border:1px solid var(--acc);border-radius:12px;padding:14px 18px;box-shadow:0 8px 30px #0003;z-index:9}
.gu{display:block;font:500 .95em/1.35 'Noto Sans Gujarati',Inter,sans-serif;text-transform:none;letter-spacing:0;opacity:.9}
h1 .gu{font-size:.52em;margin-top:2px}.btn .gu,button .gu,.pill .gu{display:inline;margin-left:6px}.pill .gu{margin-left:4px}.nogu .gu{display:none!important}textarea{height:auto;min-height:74px;padding:10px 12px;width:100%}
.pill.Proforma{background:var(--warn-soft);color:var(--warn)}
#chatbtn{position:fixed;right:20px;bottom:20px;z-index:15;height:46px;border-radius:23px;box-shadow:0 6px 24px #0004}
#chat{position:fixed;top:0;right:0;bottom:0;width:390px;max-width:100vw;background:var(--card);border-left:1px solid var(--line);display:flex;flex-direction:column;z-index:20;transform:translateX(105%);transition:transform .18s}
.chat-open #chat{transform:none}.chat-open #chatbtn{display:none}.chat-open main{margin-right:400px}
#chat .top{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-bottom:1px solid var(--line)}
#clog{flex:1;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:10px}
.bub{max-width:94%;padding:9px 12px;border-radius:12px;word-break:break-word}.bub.me{align-self:flex-end;background:var(--acc);color:var(--acc-ink)}.bub.bot{background:var(--hover);align-self:flex-start}
.ccard{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-top:8px;background:var(--card)}.ccard b{display:block;margin-bottom:6px}.ccard div{display:flex;justify-content:space-between;gap:12px;padding:2px 0;font-size:14px}.ccard div span:last-child{text-align:right;white-space:nowrap}.ccard div span:first-child{min-width:0}
.cgrp{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--mute);margin:10px 0 4px}.cchips{display:flex;flex-wrap:wrap;gap:6px}
.cchip{height:auto;padding:6px 12px;border-radius:99px;background:var(--card);color:var(--ink);border:1px solid var(--line);font-weight:500;font-size:13px;text-align:left}.cchip:hover{border-color:var(--acc);color:var(--acc);filter:none}
.cchip small,#csug small{display:block;font:500 11px 'Noto Sans Gujarati',Inter,sans-serif;opacity:.8}.cchip:disabled{opacity:.45;cursor:default}
#csug{padding:0 12px}#csug button{width:100%;justify-content:space-between;height:auto;padding:7px 10px;background:transparent;color:var(--ink);border:0;border-bottom:1px solid var(--line);border-radius:0;font-weight:500}
#cform{padding:12px;border-top:1px solid var(--line)}
@media(max-width:1100px){.chat-open main{margin-right:0}}@media(max-width:820px){ #chat{width:100vw} #chatbtn{bottom:76px}}details>summary{list-style:none}details>summary::-webkit-details-marker{display:none}
.inv{--ink:#111;--line:#555;--mute:#444;background:#fff;color:#111;padding:0;font-size:14px}.inv table{margin:0}.inv th{color:#222;font-size:11px}.inv td,.inv th{padding:6px 8px;border-top:1px solid var(--line)}
.inv .box{padding:10px 14px;border-top:1px solid var(--line)}.inv .two{display:grid;grid-template-columns:1fr 1fr}.inv .two>div{padding:10px 14px}.inv .two>div+div{border-left:1px solid var(--line)}
.inv .gu{display:none!important}.inv .kv{display:grid;grid-template-columns:130px 1fr;gap:2px 8px}.inv .ttl{text-align:center;padding:12px 16px 8px}.inv .big{font-size:28px;font-weight:700;line-height:1.2}
@media(max-width:820px){aside .gu{display:none}.app{display:block}aside{position:fixed;top:auto;bottom:0;left:0;right:0;height:auto;flex-direction:row;overflow-x:auto;padding:6px;z-index:5}
.brand,.grp{display:none}aside a{flex-direction:column;gap:2px;font-size:11px;padding:6px 10px}main{padding:20px 16px 96px}.cols,.vfoot{grid-template-columns:1fr}}
@media print{aside,.np,#toast{display:none}.app{display:block}main{padding:0}.card{border:0}}
</style><div class=app><aside><div class=brand>Divine Books<small>Divine Technologies</small></div>
{% for grp, items in nav %}{% if grp %}<div class=grp>{{grp}}</div>{% endif %}{% for h,n,d,m in items %}<a href={{h}} class="{{'on' if (m=='/' and request.path=='/') or (m!='/' and request.path.startswith(m))}}"><svg viewBox="0 0 24 24"><path d="{{d}}"/></svg><span>{{n}}</span>{% if n=='Drafts' and nd %}<em>{{nd}}</em>{% elif n=='Inbox' and ni %}<em>{{ni}}</em>{% endif %}</a>{% endfor %}{% endfor %}<div style="margin-top:auto;padding:12px"><a href=/backup style="padding:0 0 8px;font-size:13px">⬇ Backup</a>{% if pw %}<a href=/logout style="padding:0 0 8px;font-size:13px">Log out</a>{% endif %}<a href=# id=lang style="padding:0;font-size:13px"></a></div></aside><main>
{% macro extras(m, due) %}<details class=card style="margin-top:-8px"><summary style="cursor:pointer;font-weight:600">More details <span class=mute style="font-weight:400">(transport, e-way bill, ship-to…)</span></summary>
<div class=form style="margin-top:14px"><div><label>Place of supply</label><input name=place value="{{m.place or ''}}"></div>
<div><label>Reverse charge</label><select name=rcm><option value="">No<option value=Y {{'selected' if m.rcm=='Y'}}>Yes</select></div>
<div><label>Transport</label><input name=transport value="{{m.transport or ''}}"></div><div><label>Vehicle no.</label><input name=vehicle value="{{m.vehicle or ''}}"></div>
<div><label>E-way bill no.</label><input name=eway value="{{m.eway or ''}}"></div><div><label>Challan no.</label><input name=challan value="{{m.challan or ''}}"></div>
<div><label>P.O. / order no.</label><input name=po value="{{m.po or ''}}"></div><div><label>Due date</label><input name=due type=date value="{{due or ''}}"></div>
<div style="grid-column:1/-1"><label>Ship to (delivery address)</label><input name=shipto value="{{m.shipto or ''}}"></div></div></details>{% endmacro %}
{% macro grid(rows, names, imap, mode, fl, pr) %}<datalist id=itemlist>{% for n in names %}<option value="{{n}}">{% endfor %}</datalist>
<table id=lines><thead><tr><th>Item<th style=width:105px>HSN<th style=width:66px>Qty<th style=width:74px>Unit<th style=width:100px>Rate ₹<th style=width:66px>Disc %<th style=width:58px>GST %<th class=r style=width:100px>Amount<th style=width:40px></tr></thead><tbody>
{% for r in rows %}{% set c = 'fl' if fl and r.descr else '' %}{% set dl = ([loop.index, 12]|min) * 0.15 %}<tr><td><input name=d value="{{r.descr}}" list=itemlist autocomplete=off class="{{c}}" style="animation-delay:{{dl}}s">
<td><input name=h value="{{r.hsn}}" class="{{c}}" style="animation-delay:{{dl+0.05}}s"><td><input name=q value="{{r.qty or 1}}" class="{{c}}" style="animation-delay:{{dl+0.1}}s">
<td><input name=u value="{{r.unit or ''}}" class="{{c}}" style="animation-delay:{{dl+0.15}}s"><td><input name=r value="{{r.rate}}" class="{{c}}" style="animation-delay:{{dl+0.2}}s">
<td><input name=x value="{{r.disc or ''}}" class="{{c}}" placeholder="0" style="animation-delay:{{dl+0.25}}s"><td><input name=g value="{{r.gst or 18}}" class="{{c}}" style="animation-delay:{{dl+0.3}}s">
<td class="r amt"><td><button type=button class=ghost onclick="delRow(this)" tabindex=-1 title="Remove row">×</button></tr>{% endfor %}</tbody></table>
<p><button type=button class=ghost onclick="addRow()">+ Add row</button> <span class=mute id=cnt></span></p></div>
<div class=vfoot><div class=card><h2>GST summary</h2><table><thead><tr><th>Rate<th class=r>Taxable<th class=r>CGST<th class=r>SGST<th class=r>IGST</tr></thead><tbody id=gs></tbody></table></div>
<div class=card><h2>Bill sundry</h2><table><tbody id=sundry></tbody></table><div class=bigtot id=grand>₹0.00</div></div></div>
{% if mode=='buy' %}<div class=card><h2>As printed on the supplier's bill</h2><p class=mute style="margin-top:-6px">Filled from the items. If the paper bill shows different figures, type the paper's figures here.</p>
<div class=form><div><label>Taxable ₹</label><input id=pr_t name=pr_t type=number step=0.01 value="{{pr.t}}"></div><div><label>CGST ₹</label><input id=pr_c name=pr_c type=number step=0.01 value="{{pr.c}}"></div>
<div><label>SGST ₹</label><input id=pr_s name=pr_s type=number step=0.01 value="{{pr.s}}"></div><div><label>IGST ₹</label><input id=pr_i name=pr_i type=number step=0.01 value="{{pr.i}}"></div>
<div><label>Round off ₹</label><input id=pr_r name=pr_r type=number step=0.01 value="{{pr.r}}"></div><div><label>Total ₹</label><input id=pr_g name=pr_g type=number step=0.01 value="{{pr.g}}"></div></div>
<input type=hidden name=touched id=touched value="{{1 if pr.t else ''}}"><p id=diffwarn class=note style="display:none;margin-top:14px"></p>
<label id=ackrow style="display:none;margin-top:10px"><input type=checkbox name=ack value=1 style="width:auto;height:auto;display:inline"> The supplier's figures are right</label></div>{% endif %}
<script>
const ITEMS={{imap|tojson}},MODE="{{mode}}",P=document.getElementById('party'),body=document.querySelector('#lines tbody'),INC=document.getElementById('incl');
const PR=['pr_t','pr_c','pr_s','pr_i','pr_r','pr_g'].map(i=>document.getElementById(i)),TCH=document.getElementById('touched');let CAL=[0,0,0,0,0,0];
const f2=x=>x.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2}),blank=l=>l.forEach((e,k)=>{e.value=k==2?1:k==6?18:'';e.className='';e.style.animation='none'});
function addRow(){const t=body.lastElementChild.cloneNode(true);blank(t.querySelectorAll('input'));t.querySelector('.amt').textContent='';body.append(t);return t}
function delRow(b){const t=b.closest('tr');if(body.rows.length>1)t.remove();else blank(t.querySelectorAll('input'));calc()}
function info(){const o=P.selectedOptions[0];document.getElementById('pinfo').textContent=!o||!o.value?'':(o.dataset.gstin||'No GSTIN')+' · '+(o.dataset.intra=='1'?'Same state: CGST + SGST':'Other state: IGST')+' · '+(MODE=='sale'?'They owe us ₹':'We owe ₹')+f2(o.dataset.bal/100)}
function diff(){if(!PR[0])return;const v=PR.map(e=>Math.round((+e.value||0)*100)),gap=Math.max(Math.abs(v[0]-CAL[0]),Math.abs(v[1]+v[2]+v[3]-CAL[1]-CAL[2]-CAL[3])),w=document.getElementById('diffwarn');
w.style.display=gap>100?'block':'none';document.getElementById('ackrow').style.display=gap>100?'block':'none';w.textContent='The bill figures differ from the items by ₹'+f2(gap/100)+'. Check qty, rate and discount, or tick the box if the paper bill is right.'}
function calc(){const o=P.selectedOptions[0],same=!o||o.dataset.intra!='0',incl=INC.value=='1';let grp={},tx=0,gs=0,k=0;
for(const r of body.rows){const n=r.querySelectorAll('input'),q=+n[2].value||0,rt=+n[4].value||0,g=+n[6].value||0;let fc=1;String(n[5].value||'').split('+').forEach(x=>fc*=1-(+x||0)/100);
const gross=Math.round(q*rt*fc*100),t=incl?Math.round(gross/(1+g/100)):gross,x=incl?gross-t:Math.round(t*g/100);r.querySelector('.amt').textContent=gross?f2((incl?gross:t)/100):'';
if(gross){k++;grp[g]=grp[g]||{x:0,t:0};grp[g].x+=t;grp[g].t+=x;tx+=t;gs+=x}}
let cg=0;const h=Object.keys(grp).sort((a,b)=>a-b).map(k=>{const v=grp[k],c=same?Math.floor(v.t/2):0;cg+=c;return '<tr><td>'+k+'%<td class=r>'+f2(v.x/100)+'<td class=r>'+(same?f2(c/100):'')+'<td class=r>'+(same?f2((v.t-c)/100):'')+'<td class=r>'+(same?'':f2(v.t/100))+'</tr>'}).join('');
const sg=same?gs-cg:0,ig=same?0:gs,ex=tx+gs,tot=Math.round(ex/100)*100,ro=tot-ex;
document.getElementById('gs').innerHTML=h||'<tr><td class=mute>Add items above</tr>';
document.getElementById('sundry').innerHTML='<tr><td>Taxable value<td class=r>'+f2(tx/100)+'</tr>'+(same?'<tr><td>CGST<td class=r>'+f2(cg/100)+'</tr><tr><td>SGST<td class=r>'+f2(sg/100)+'</tr>':'<tr><td>IGST<td class=r>'+f2(ig/100)+'</tr>')+'<tr><td>Round off<td class=r>'+f2(ro/100)+'</tr>';
document.getElementById('grand').textContent='₹'+f2(tot/100);document.getElementById('cnt').textContent=k+' items';CAL=[tx,cg,sg,ig,ro,tot];
if(PR[0]&&!TCH.value)PR.forEach((e,i)=>e.value=(CAL[i]/100).toFixed(2));diff()}
if(PR[0])PR.forEach(e=>e.addEventListener('input',()=>{TCH.value=1;diff()}));
document.addEventListener('input',e=>{if(PR.includes(e.target))return;if(e.target.name=='d'&&e.target.value&&e.target.closest('tr')==body.lastElementChild)addRow();calc()});
P.addEventListener('change',()=>{info();calc()});INC.addEventListener('change',calc);
document.addEventListener('keydown',e=>{const t=e.target;if(e.key!='Enter'||t.tagName!='INPUT'||!t.form||!t.form.classList.contains('v'))return;e.preventDefault();
const all=()=>[...t.form.querySelectorAll('input:not([type=hidden]),select')];let f=all();if(t.closest('#lines')&&f.indexOf(t)==f.length-1){addRow();f=all()}f[f.indexOf(t)+1]?.focus()});
document.addEventListener('change',e=>{const m=ITEMS[(e.target.value||'').toLowerCase()];if(!m||!e.target.name||e.target.name!='d')return;
const n=e.target.closest('tr').querySelectorAll('input');n[1].value=m.hsn||'';n[3].value=m.unit||'';n[4].value=(MODE=='sale'?m.sale:m.buy)||'';n[6].value=m.gst||18;calc()});
info();calc()</script>{% endmacro %}
{% macro bar(qs) %}<form class=np style="display:flex;gap:8px;margin-bottom:16px"><input name=q value="{{qs}}" placeholder="Search…" style="max-width:340px"><button class=ghost>Search</button>{% if qs %}<a class="btn ghost" href="{{request.path}}">Clear</a>{% endif %}</form>{% endmacro %}
{% macro pager(p, more, qs) %}<div class=np style="display:flex;gap:8px;justify-content:center;margin-top:8px">{% if p>1 %}<a class="btn ghost" href="?p={{p-1}}&q={{qs|urlencode}}">← Previous</a>{% endif %}{% if more %}<a class="btn ghost" href="?p={{p+1}}&q={{qs|urlencode}}">Next →</a>{% endif %}</div>{% endmacro %}"""
TAIL = """</main></div><button id=chatbtn class=np>💬 Commands</button>
<div id=chat class=np><div class=top><b>Commands</b><button class=ghost id=cclose type=button style="height:32px">✕</button></div><div id=clog></div><div id=csug></div>
<form id=cform><input id=cin autocomplete=off placeholder="Type a command, or click one above"></form></div>
<div id=toast hidden></div><script>
let last=null,fl=null;
const bd=(h,n)=>{const a=document.querySelector('aside a[href="'+h+'"]');if(!a)return;let em=a.querySelector('em');if(n){if(!em){em=document.createElement('em');a.append(em)}em.textContent=n}else if(em)em.remove()},
toast=h=>{const t=document.getElementById('toast');t.innerHTML=h;t.hidden=false};
async function poll(){try{const r=await(await fetch('/api/drafts/count')).json();bd('/drafts',r.n);bd('/inbox',r.files);
if(last!==null&&r.latest>last){if(location.pathname=='/drafts')location.reload();else toast('📷 New bill received · <a href="/drafts"><b>Check it</b></a>')}
if(fl!==null&&r.flatest>fl){if(location.pathname=='/inbox')location.reload();else toast('📎 New file received · <a href="/inbox"><b>Open</b></a>')}
last=r.latest;fl=r.flatest}catch(e){}}
poll();setInterval(poll,3000)
const L=document.getElementById('lang');function setL(on){document.documentElement.classList.toggle('nogu',!on);L.textContent=on?'🌐 ગુજરાતી ✓':'🌐 Gujarati off'}
let on=true;try{on=localStorage.getItem('gu')!='off'}catch(e){}setL(on);
L.onclick=e=>{e.preventDefault();on=!on;try{localStorage.setItem('gu',on?'on':'off')}catch(e){}setL(on)}</script><script src="/static/chat.js?v=1"></script>"""
def gu_html(h):
    """Add the Gujarati line under every known English label (outside scripts and styles)."""
    def add(m):
        g_ = GU.get(m[2].strip())
        return f"{m[1]}{m[2]}<span class=gu>{g_}</span>" if g_ else m[0]
    opt = lambda m: f"{m[1]}{m[2]} · {GU[m[2].strip()]}" if m[2].strip() in GU else m[0]
    parts = re.split(r"(<script.*?</script>|<style.*?</style>)", h, flags=re.S)
    return "".join(p if p[:7] in ("<script", "<style>") else re.sub(r"(<option[^>]*>)([^<>]+)", opt, re.sub(r"(<(?!option|script|style|title)[a-zA-Z0-9]+[^>]*>)([^<>]+)(?=<)", add, p)) for p in parts)

page = lambda body, **k: gu_html(R(HEAD + body + TAIL, nav=NAV, **k))
today = lambda: str(dt.date.today())
app.context_processor(lambda: {"nd": one("select count(*) from drafts where status='new'")[0], "ni": one("select count(*) from inbox where status='new'")[0], "pw": bool(PASSWORD)})

def bill_state(d): return "Cancelled" if d["cancelled"] else "Proforma" if d["doctype"] == "proforma" else "Paid" if d["due"] <= 0 else "Part" if d["due"] < d["total"] else "Due"

@app.errorhandler(Oops)
def _oops(e): return page("<div class=card><h2>⚠ {{m}}</h2><p class=mute>{{g}}</p><p class=mute>Go back and fix it, then try again.</p></div>", m=str(e), g=GU.get(str(e), "")), 400

@app.errorhandler(ValueError)
@app.errorhandler(ArithmeticError)
def _bad(e): return page("<div class=card><h2>⚠ Please check what you typed</h2><p class=mute>A date, number or required box looks wrong. Go back and fix it.</p></div>"), 400

# ---------- home / parties / items ----------
@app.route("/")
def home():
    s = lambda acc, a, b: one(f"select coalesce(sum({a}-{b}),0) from ledger where account=?", acc)[0]
    m = today()[:7] + "%"
    ms = lambda acc, a, b: one(f"select coalesce(sum({a}-{b}),0) from ledger where account=? and date like ?", acc, m)[0]
    calls = q("select j.*, c.name from jobs j join clients c on c.id=j.client_id where followup<=? and status in ('enquiry','quoted','active') order by followup", today())
    return page("""<div class=head><h1>Home</h1><div><a class=btn href=/invoice/new>+ Sales bill</a> <a class="btn ghost" href=/purchase/new>+ Purchase bill</a></div></div>
<div class=stats><div class=stat><span>Customers owe us</span><b>₹{{rec|rs}}</b></div><div class=stat><span>We owe suppliers</span><b>₹{{pay|rs}}</b></div>
<div class=stat><span>Sales this month</span><b>₹{{sales|rs}}</b></div><div class=stat><span>Purchases this month</span><b>₹{{buys|rs}}</b></div></div>
{% if nd %}<div class=note>📥 {{nd}} bill{{'s' if nd>1}} waiting for your check. <a href=/drafts><b>Open drafts</b></a></div>{% endif %}
<div class=cols><div class=card><h2>Follow up today</h2>{% for j in calls %}<p><b>{{j.name}}</b> · {{j.title}}<br><span class=mute>{{j.followup}}</span> <span class="pill {{j.status}}">{{j.status}}</span></p>{% else %}<p class=empty>Nothing to follow up.</p>{% endfor %}</div>
<div class=card><h2>Unpaid sales bills</h2><table><tbody>{% for d in dues %}<tr><td><a href=/invoice/{{d.id}}>{{d.no}}</a><br><span class=mute>{{d.name}}</span><td class=r><b>₹{{d.due|rs}}</b></tr>{% else %}<tr><td class=empty>All paid.</tr>{% endfor %}</tbody></table></div></div>""",
        rec=s("Debtors", "dr", "cr"), pay=s("Creditors", "cr", "dr"), sales=ms("Sales", "cr", "dr"), buys=ms("Purchases", "dr", "cr"),
        calls=calls, dues=q("select * from inv_due where due>0 and cancelled=0 order by date"))

@app.route("/clients", methods=["GET", "POST"])
def clients():
    if request.method == "POST":
        f = request.form; x = f["gstin"].strip().upper()
        if not f["name"].strip(): raise Oops("Name is needed")
        if x and not gstin_ok(x): raise Oops("GSTIN is not valid. Check every letter and digit.")
        with db() as c: c.execute("insert into clients(name,phone,gstin,state,address,kind) values(?,?,?,?,?,?)",
                                  (f["name"].strip(), f["phone"], x, f["state"].strip(), f["address"], f["kind"]))
        return redirect("/clients")
    a = request.args.get; qs = a("q", "").strip(); p = max(int(a("p", 1)), 1); like = f"%{qs}%"
    rows = q("""select c.*, coalesce((select sum(dr-cr) from ledger where account='Debtors' and client_id=c.id),0) rec,
                coalesce((select sum(cr-dr) from ledger where account='Creditors' and client_id=c.id),0) pay from clients c
                where c.name like ? or c.phone like ? or c.gstin like ? order by c.name limit ? offset ?""", like, like, like, PER + 1, (p - 1) * PER)
    more, rows = len(rows) > PER, rows[:PER]
    return page("""<div class=head><h1>Parties</h1></div>{{bar(qs)}}<div class=card><table><thead><tr><th>Name<th>Type<th>Phone<th>GSTIN<th class=r>They owe us<th class=r>We owe them</tr></thead><tbody>
{% for c in rows %}<tr><td><a href=/client/{{c.id}}><b>{{c.name}}</b></a><td class=mute>{{c.kind}}<td>{{c.phone}}<td class=mute>{{c.gstin}}<td class=r>₹{{c.rec|rs}}<td class=r>₹{{c.pay|rs}}</tr>
{% else %}<tr><td class=empty colspan=6>No parties yet. Add the first one below.</tr>{% endfor %}</tbody></table></div>{{pager(p, more, qs)}}
<div class=card style="margin-top:20px"><h2>Add party</h2><form method=post class=form><div><label>Name</label><input name=name value="{{a('name','')}}" required></div>
<div><label>Type</label><select name=kind><option value=customer>Customer<option value=supplier {{'selected' if a('kind')=='supplier'}}>Supplier<option value=both>Both</select></div>
<div><label>Phone</label><input name=phone></div><div><label>GSTIN</label><input name=gstin value="{{a('gstin','')}}"></div>
<div><label>State</label><input name=state value="{{own}}"></div><div><label>Address</label><input name=address></div><button>Save party</button></form></div>""",
        rows=rows, own=OWN_STATE, a=a, qs=qs, p=p, more=more)

@app.route("/client/<int:i>")
def client(i):
    c = one("select * from clients where id=?", i)
    bal = lambda acc, a, b: one(f"select coalesce(sum({a}-{b}),0) from ledger where account=? and client_id=?", acc, i)[0]
    return page("""<div class=head><div><h1>{{c.name}}</h1><span class=mute>{{c.kind}} · {{c.phone}} · {{c.gstin or 'No GSTIN'}} · {{c.state}}</span></div><div>
<a class=btn href="/invoice/new?client={{c.id}}">+ Sales bill</a> <a class="btn ghost" href="/purchase/new?party={{c.id}}">+ Purchase bill</a> <a class="btn ghost" href="/client/{{c.id}}/edit">Edit</a></div></div>
<div class=stats><div class=stat><span>Owes us</span><b>₹{{rec|rs}}</b></div><div class=stat><span>We owe</span><b>₹{{pay|rs}}</b></div></div>
<div class=cols><div class=card><h2>Jobs</h2>{% for j in jobs %}<p>{{j.title}} <span class="pill {{j.status}}">{{j.status}}</span></p>{% else %}<p class=empty>No jobs.</p>{% endfor %}</div>
<div class=card><h2>Bills</h2><table><tbody>{% for d in invs %}<tr><td><a href=/invoice/{{d.id}}>{{d.no}}</a><br><span class=mute>Sales · {{d.date}}</span><td class=r>₹{{d.total|rs}}<td class=r><span class="pill {{st(d)}}">{{st(d)}}</span></tr>{% endfor %}
{% for d in pur %}<tr><td><a href=/purchase/{{d.id}}>{{d.no}}</a><br><span class=mute>Purchase · {{d.date}}</span><td class=r>₹{{d.total|rs}}<td class=r><span class="pill {{st(d)}}">{{st(d)}}</span></tr>{% endfor %}</tbody></table></div></div>""",
        c=c, jobs=q("select * from jobs where client_id=?", i), invs=q("select * from inv_due where client_id=? order by date desc", i),
        pur=q("select * from pur_due where party_id=? order by date desc", i), st=bill_state, rec=bal("Debtors", "dr", "cr"), pay=bal("Creditors", "cr", "dr"))

def item_vals(f, me=0):
    n = f["name"].strip()
    if not n: raise Oops("Item name is needed")
    if (e := iid(n)) and e != me: raise Oops("An item with this name already exists")
    if not 0 <= D(f["gst"]) <= 28 or min(D(f["sale"] or 0), D(f["purch"] or 0), D(f["opening"] or 0)) < 0: raise Oops("Check GST, prices and opening stock")
    return n, unit_norm(f["unit"]) or "Pcs", f["hsn"].strip(), f["gst"], f["sale"] or "0", f["purch"] or "0", f["opening"] or "0"

ITEM_FORM = """<div><label>Name</label><input name=name value="{{i.name}}" required></div><div><label>Unit</label><input name=unit value="{{i.unit or 'Pcs'}}" list=units><datalist id=units><option>Pcs<option>Mtr<option>Bdl<option>Roll<option>Set<option>Box<option>Kg<option>Ltr</datalist></div>
<div><label>HSN / SAC</label><input name=hsn value="{{i.hsn or ''}}"></div><div><label>GST %</label><input name=gst type=number step=0.01 value="{{i.gst or 18}}"></div>
<div><label>Sale price ₹</label><input name=sale type=number step=0.01 value="{{i.sale or ''}}"></div><div><label>Purchase price ₹</label><input name=purch type=number step=0.01 value="{{i.purch or ''}}"></div>
<div><label>Opening stock</label><input name=opening type=number step=any value="{{i.opening or 0}}"></div>"""

@app.route("/items", methods=["GET", "POST"])
def items():
    if request.method == "POST":
        with db() as c: c.execute("insert into items(name,unit,hsn,gst,sale,purch,opening) values(?,?,?,?,?,?,?)", item_vals(request.form))
        return redirect("/items")
    qs = request.args.get("q", "").strip(); p = max(int(request.args.get("p", 1)), 1); like = f"%{qs}%"
    rows = q(STOCK + " where i.name like ? or i.hsn like ? order by i.name limit ? offset ?", like, like, PER + 1, (p - 1) * PER)
    more, rows = len(rows) > PER, rows[:PER]
    return page("""<div class=head><h1>Items</h1></div>{{bar(qs)}}<div class=card><table><thead><tr><th>Name<th>Unit<th>HSN<th class=r>GST %<th class=r>Sale ₹<th class=r>Purchase ₹<th class=r>In stock<th></tr></thead><tbody>
{% for i in rows %}<tr><td><b>{{i.name}}</b><td>{{i.unit}}<td class=mute>{{i.hsn}}<td class=r>{{i.gst}}<td class=r>{{i.sale}}<td class=r>{{i.purch}}<td class=r><b>{{i.stock|qty}}</b><td class=r><a href=/item/{{i.id}}/edit>Edit</a></tr>
{% else %}<tr><td class=empty colspan=8>No items yet. Add the first one below.</tr>{% endfor %}</tbody></table></div>{{pager(p, more, qs)}}
<div class=card style="margin-top:20px"><h2>Add item</h2><form method=post class=form>""" + ITEM_FORM + """<button>Save item</button></form></div>""", rows=rows, qs=qs, p=p, more=more, i={})

@app.route("/item/<int:i>/edit", methods=["GET", "POST"])
def item_edit(i):
    if request.method == "POST":
        with db() as c: c.execute("update items set name=?,unit=?,hsn=?,gst=?,sale=?,purch=?,opening=? where id=?", (*item_vals(request.form, i), i))
        return redirect("/items")
    return page("""<div class=head><h1>Edit item</h1></div><div class=card><form method=post class=form>""" + ITEM_FORM + """<button>Save item</button> <a class="btn ghost" href=/items>Cancel</a></form>
<p class=mute>Changing a price or name only affects new bills. Old bills stay as they were.</p></div>""", i=one("select * from items where id=?", i))

@app.route("/client/<int:i>/edit", methods=["GET", "POST"])
def client_edit(i):
    if request.method == "POST":
        f = request.form; x = f["gstin"].strip().upper()
        if not f["name"].strip(): raise Oops("Name is needed")
        if x and not gstin_ok(x): raise Oops("GSTIN is not valid. Check every letter and digit.")
        with db() as c: c.execute("update clients set name=?,phone=?,gstin=?,state=?,address=?,kind=? where id=?", (f["name"].strip(), f["phone"], x, f["state"].strip(), f["address"], f["kind"], i))
        return redirect(f"/client/{i}")
    return page("""<div class=head><h1>Edit party</h1></div><div class=card><form method=post class=form><div><label>Name</label><input name=name value="{{c.name}}" required></div>
<div><label>Type</label><select name=kind>{% for k in ['customer','supplier','both'] %}<option value={{k}} {{'selected' if k==c.kind}}>{{k|capitalize}}{% endfor %}</select></div>
<div><label>Phone</label><input name=phone value="{{c.phone or ''}}"></div><div><label>GSTIN</label><input name=gstin value="{{c.gstin or ''}}"></div>
<div><label>State</label><input name=state value="{{c.state or ''}}"></div><div><label>Address</label><input name=address value="{{c.address or ''}}"></div>
<button>Save party</button> <a class="btn ghost" href=/client/{{c.id}}>Cancel</a></form><p class=mute>Changes apply to new bills. Old bills keep the amounts and taxes they were made with.</p></div>""", c=one("select * from clients where id=?", i))

# ---------- jobs (CRM, basic) ----------
@app.route("/jobs", methods=["GET", "POST"])
def jobs():
    if request.method == "POST":
        f = request.form
        with db() as c: c.execute("insert into jobs(client_id,title,followup) values(?,?,?)", (f["client"], f["title"], f["followup"] or None))
        return redirect("/jobs")
    return page("""<div class=head><h1>Jobs</h1></div><div class=card><table><thead><tr><th>Client<th>Work<th>Follow up<th>Status</tr></thead><tbody>
{% for j in rows %}<tr><td><b>{{j.name}}</b><td>{{j.title}}<td class=mute>{{j.followup or '—'}}<td>
{% if j.status=='invoiced' %}<span class="pill invoiced">invoiced</span>{% else %}<form method=post action=/job/{{j.id}}><select class=sel name=s onchange=this.form.submit()>
{% for s in ['enquiry','quoted','active','done'] %}<option {{'selected' if s==j.status}}>{{s}}</option>{% endfor %}</select></form>{% endif %}</tr>
{% else %}<tr><td class=empty colspan=4>No jobs yet.</tr>{% endfor %}</tbody></table></div>
<div class=card><h2>Add job</h2><form method=post class=form><div><label>Client</label><select name=client>{% for c in cs %}<option value={{c.id}}>{{c.name}}</option>{% endfor %}</select></div>
<div><label>Work</label><input name=title placeholder="e.g. 8 camera installation" required></div><div><label>Follow up on</label><input name=followup type=date></div><button>Save job</button></form></div>""",
        rows=q("select j.*, c.name from jobs j join clients c on c.id=j.client_id order by j.status in ('done','invoiced'), j.followup"), cs=q("select * from clients order by name"))

@app.post("/job/<int:i>")
def job_status(i):
    with db() as c: c.execute("update jobs set status=? where id=? and status!='invoiced'", (request.form["s"], i))
    return redirect("/jobs")

# ---------- sales ----------
@app.route("/invoices")
def invoices():
    qs = request.args.get("q", "").strip(); p = max(int(request.args.get("p", 1)), 1); like = f"%{qs}%"
    rows = q("select * from inv_due where no like ? or name like ? order by date desc, id desc limit ? offset ?", like, like, PER + 1, (p - 1) * PER)
    more, rows = len(rows) > PER, rows[:PER]
    return page("""<div class=head><h1>Sales</h1><div><a class="btn ghost" href=/quick>⚡ Quick bill</a> <a class=btn href=/invoice/new>+ New sales bill</a></div></div>{{bar(qs)}}<div class=card><table><thead><tr><th>Number<th>Date<th>Customer<th class=r>Total<th class=r>Due<th>Status</tr></thead><tbody>
{% for d in rows %}<tr><td><a href=/invoice/{{d.id}}><b>{{d.no}}</b></a><td class=mute>{{d.date}}<td>{{d.name}}<td class=r>₹{{d.total|rs}}<td class=r>{{(0 if d.cancelled else d.due)|rs}}<td><span class="pill {{st(d)}}">{{st(d)}}</span></tr>
{% else %}<tr><td class=empty colspan=6>No sales bills yet.</tr>{% endfor %}</tbody></table></div>{{pager(p, more, qs)}}""", rows=rows, st=bill_state, qs=qs, p=p, more=more)

@app.route("/invoice/new", methods=["GET", "POST"])
def invoice_new():
    if request.method == "GET":
        rows, imap = item_data(); warn = ""
        try: vn = inv_no(today()); need_company()
        except Oops as e: vn, warn = "—", str(e)
        pre = {}
        if dd := request.args.get("draft"):
            dr = one("select * from drafts where id=? and status='new'", dd)
            if not dr: raise Oops("This draft is already used or removed")
            pre = json.loads(dr["data"])
        srows = pre.get("lines") or []; srows = srows + [{}] * max(6 - len(srows), 1)
        return page("""<div class=head><h1>Sales voucher</h1></div>{% if warn %}<div class=note>⚠ {{warn}}. <a href=/company><b>Open Company details</b></a></div>{% endif %}
{% if pre.customer is defined %}<div class=note>⚡ Draft from your command. Check it, then save.</div>{% for w in pre.warnings or [] %}<div class=note>⚠ {{w}}</div>{% endfor %}{% endif %}
<form method=post class=v><input type=hidden name=draft value="{{request.args.get('draft','')}}"><div class=card><div class=form>
<div><label>Series</label><div class=ro>Main</div></div><div><label>Voucher no.</label><div class=ro>{{vn}}</div></div><div><label>Date</label><input name=date type=date value={{d}}></div>
<div><label>Customer</label><select name=client id=party required><option value="">Choose…</option>{% for c in cs %}<option value={{c.id}} data-intra={{c.intra}} data-bal={{c.bal}} data-gstin="{{c.gstin}}" {{'selected' if c.id|string==sel}}>{{c.name}}</option>{% endfor %}</select></div>
<div><label>Job (optional)</label><select name=job><option value="">None</option>{% for j in js %}<option value={{j.id}}>{{j.name}}: {{j.title}}</option>{% endfor %}</select></div>
<div><label>Rates are</label><select name=incl id=incl><option value=0>Without tax<option value=1>Including tax</select></div>
<div><label>Narration</label><input name=narration value="{{pre.narration or ''}}"></div></div><p class=pinfo id=pinfo></p></div>
{{extras({}, '')}}<div class=card>{{grid(srows, names, items, 'sale', pre.customer, {})}}<button>Save sales bill</button></form>""",
            cs=parties("'customer','both'", "Debtors", "dr", "cr"), sel=str(pre.get("party") or request.args.get("client", "")), pre=pre, srows=srows, d=today(), vn=vn, warn=warn, names=[r["name"] for r in rows], items=imap,
            js=q("select j.*, c.name from jobs j join clients c on c.id=j.client_id where status!='invoiced'"))
    f = request.form; need_company(); lines = parse(f); incl = f.get("incl") == "1"
    cl = one("select * from clients where id=?", f["client"])
    rows, taxable, cg, sg, ig, ro, total = calc(lines, intra(cl), incl)
    with db() as c:
        no = inv_no(f["date"])
        v = post(f["date"], [("Debtors", cl["id"], total, 0), ("Sales", None, 0, taxable)]
                 + [(a, None, 0, x) for a, x in (("CGST", cg), ("SGST", sg), ("IGST", ig)) if x]
                 + ([("Round off", None, max(-ro, 0), max(ro, 0))] if ro else []))
        i = c.execute("insert into invoices(no,date,client_id,narration,taxable,cgst,sgst,igst,ro,total,voucher,incl,meta,due) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (no, f["date"], cl["id"], f["narration"], taxable, cg, sg, ig, ro, total, v, int(incl), meta_of(f), f.get("due") or None)).lastrowid
        c.executemany("insert into lines(invoice_id,descr,qty,rate,gst,hsn,unit,item_id,disc,taxable,taxamt) values(?,?,?,?,?,?,?,?,?,?,?)",
                      [(i, d, qy, r, gs, h, u, iid(d), dc, t, x) for (d, qy, r, gs, h, u, dc), (t, x) in zip(lines, rows)])
        if f["job"]: c.execute("update jobs set status='invoiced' where id=?", (f["job"],))
        if f.get("draft"): c.execute("update drafts set status='done' where id=?", (f["draft"],))
    return redirect(f"/invoice/{i}")

def doc_lines(rows): return [(l["descr"], l["hsn"], l["qty"], l["unit"], l["rate"], l["disc"], l["gst"], l["taxable"] if l["taxable"] is not None else paise(D(l["qty"]) * D(l["rate"]))) for l in rows]

TAXINV = """<div class="card inv"><div class=box style="display:flex;justify-content:space-between;border-top:0"><b>GSTIN : {{co.gstin}}</b><i>Original Copy</i></div>
<div class=ttl><div style="font-size:12px;letter-spacing:.12em"><u>TAX INVOICE</u></div><div class=big>{{co.name}}</div><div>{{co.addr}}</div><div>PAN : {{co.pan}}{% if co.phone %} · Tel : {{co.phone}}{% endif %}</div><i>{{co.email}}</i></div>
<div class="two" style="border-top:1px solid var(--line)"><div><b>Party Details :</b><br><b>{{v.name}}</b><br>{{v.address}}<br>{{v.state}}<br>GSTIN / UIN : {{v.gstin or '—'}}{% if v.phone %}<br>Mobile : {{v.phone}}{% endif %}</div>
<div><div class=kv><span>Invoice No.</span><b>: {{v.no}}</b><span>Dated</span><span>: {{v.date_fmt}}</span><span>Place of Supply</span><span>: {{meta.place or place}}</span><span>Reverse Charge</span><span>: {{meta.rcm or 'N'}}</span>
<span>Transport</span><span>: {{meta.transport or ''}}</span><span>Vehicle No.</span><span>: {{meta.vehicle or ''}}</span><span>E-Way Bill No.</span><span>: {{meta.eway or ''}}</span>{% if meta.po %}<span>Order No.</span><span>: {{meta.po}}</span>{% endif %}{% if v.due %}<span>Due Date</span><span>: {{v.due}}</span>{% endif %}</div></div></div>
{% if meta.shipto %}<div class=box><b>Shipped to :</b> {{meta.shipto}}</div>{% endif %}{% if v.narration %}<div class=box>{{v.narration}}</div>{% endif %}
<table><thead><tr><th>S.N.<th>Description of Goods<th>HSN/SAC<th class=r>Qty.<th>Unit<th class=r>Price{{' (incl. tax)' if v.incl}}<th class=r>Disc %<th class=r>Amount (₹)</tr></thead><tbody>
{% for d,h,qy,u,r,dc,gs,a in ls %}<tr><td>{{loop.index}}.<td>{{d}}<td>{{h or ''}}<td class=r>{{qy}}<td>{{u or ''}}<td class=r>{{r}}<td class=r>{{dc or ''}}<td class=r>{{a|rs}}</tr>{% endfor %}</tbody></table>
<div class=box style="display:flex;justify-content:flex-end"><table style="width:340px"><tbody>
<tr><td style="border:0">Taxable value<td class=r style="border:0">{{v.taxable|rs}}
{% for rt,t,c,s,i in grp %}{% if c %}<tr><td style="border:0">Add : CGST @ {{'%.2f'|format(rt/2)}} %<td class=r style="border:0">{{c|rs}}<tr><td style="border:0">Add : SGST @ {{'%.2f'|format(rt/2)}} %<td class=r style="border:0">{{s|rs}}{% elif i %}<tr><td style="border:0">Add : IGST @ {{'%.2f'|format(rt)}} %<td class=r style="border:0">{{i|rs}}{% endif %}{% endfor %}
{% if v.ro %}<tr><td style="border:0">Round off<td class=r style="border:0">{{v.ro|rs}}{% endif %}
<tr><td><b>Grand Total ₹</b><td class=r><b>{{v.total|rs}}</b></tbody></table></div>
<div class=box><table style="width:auto"><thead><tr><th>Tax Rate<th class=r>Taxable Amt.<th class=r>CGST Amt.<th class=r>SGST Amt.<th class=r>IGST Amt.<th class=r>Total Tax</tr></thead><tbody>
{% for rt,t,c,s,i in grp %}<tr><td>{{rt}}%<td class=r>{{t|rs}}<td class=r>{{c|rs}}<td class=r>{{s|rs}}<td class=r>{{i|rs}}<td class=r>{{(c+s+i)|rs}}</tr>{% endfor %}</tbody></table></div>
<div class=box><b>{{v.total|words}}</b></div>
<div class=box style="white-space:pre-line"><b>Bank Details :</b> {{co.bank}}</div>
<div class="two"><div><b>Terms &amp; Conditions</b><div style="white-space:pre-line;font-size:12px">{{co.terms}}</div></div><div style="min-height:110px"><span class=mute>Receiver's Signature :</span><div style="text-align:right;margin-top:56px"><b>For {{co.name}}</b><br>Authorised Signatory</div></div></div></div>"""

@app.route("/invoice/<int:i>")
def invoice(i):
    v = dict(one("select i.*, c.name, c.gstin, c.address, c.state, c.phone from invoices i join clients c on c.id=i.client_id where i.id=?", i))
    raw = q("select descr,hsn,qty,unit,rate,disc,gst,taxable,taxamt from lines where invoice_id=?", i)
    ls = doc_lines(raw); same = intra(v); v["date_fmt"] = dt.date.fromisoformat(v["date"]).strftime("%d-%m-%Y")
    grp = rate_groups([(l["gst"], l["taxable"] if l["taxable"] is not None else paise(D(l["qty"]) * D(l["rate"])), l["taxamt"] or 0) for l in raw], same)
    if raw and raw[0]["taxable"] is None: grp = [(D(18), v["taxable"], v["cgst"], v["sgst"], v["igst"])]        # bills made before per-line tax was stored
    pays = q("select * from payments where invoice_id=? order by id", i); paid = sum(p["amount"] for p in pays if not p["cancelled"])
    co = {k[3:]: cfg(k) for k in ("co_name", "co_addr", "co_gstin", "co_pan", "co_phone", "co_email", "co_bank", "co_terms")}
    return page("""<div class="head np"><h1>{{v.no}} {% if v.cancelled %}<span class="pill Cancelled">Cancelled</span>{% elif v.total-paid<=0 %}<span class="pill Paid">Paid</span>{% endif %}</h1><div>
{% if not v.cancelled %}<a class=btn href="/receipt?invoice={{v.id}}">Record receipt</a> {% endif %}<button class=ghost onclick=print()>Print</button></div></div>""" + TAXINV + """
<div class="card np"><h2>Receipts · Due ₹{{(v.total-paid)|rs}}</h2>{% for p in pays %}<form method=post action=/receipt/{{p.id}}/cancel style="display:flex;gap:12px;align-items:center;justify-content:space-between;padding:6px 0">
<span>{{p.date}} · {{p.mode}} · ₹{{p.amount|rs}} {% if p.cancelled %}<span class="pill Cancelled">undone</span>{% endif %}</span>{% if not p.cancelled %}<button class="ghost danger">Undo</button>{% endif %}</form>{% else %}<p class=empty>No receipts yet.</p>{% endfor %}</div>
{% if not v.cancelled %}<form class=np method=post action=/invoice/{{v.id}}/cancel onsubmit="return confirm('Cancel this bill? A reversing entry will be posted.')"><button class=danger>Cancel bill</button></form>{% endif %}""",
        v=v, ls=ls, pays=pays, paid=paid, co=co, grp=grp, meta=json.loads(v["meta"] or "{}"), place=f"{v['state'] or OWN_STATE} ({(v['gstin'] or OWN_CODE)[:2]})")

@app.post("/invoice/<int:i>/cancel")
def invoice_cancel(i):
    with db() as c:
        v = one("select * from invoices where id=? and cancelled=0", i)
        if not v: raise Oops("Already cancelled")
        if one("select 1 from payments where invoice_id=? and cancelled=0", i): raise Oops("Undo its receipts first")
        reverse(v["voucher"]); c.execute("update invoices set cancelled=1 where id=?", (i,))
    return redirect(f"/invoice/{i}")

# ---------- purchase (+ drafts from photo automation) ----------
@app.route("/purchases")
def purchases():
    qs = request.args.get("q", "").strip(); p = max(int(request.args.get("p", 1)), 1); like = f"%{qs}%"
    rows = q("select * from pur_due where no like ? or name like ? or sup_no like ? order by date desc, id desc limit ? offset ?", like, like, like, PER + 1, (p - 1) * PER)
    more, rows = len(rows) > PER, rows[:PER]
    return page("""<div class=head><h1>Purchase</h1><a class=btn href=/purchase/new>+ New purchase bill</a></div>{{bar(qs)}}<div class=card><table><thead><tr><th>Number<th>Date<th>Supplier<th>Supplier bill<th class=r>Total<th class=r>To pay<th>Status</tr></thead><tbody>
{% for d in rows %}<tr><td><a href=/purchase/{{d.id}}><b>{{d.no}}</b></a><td class=mute>{{d.date}}<td>{{d.name}}<td class=mute>{{d.sup_no}}<td class=r>₹{{d.total|rs}}<td class=r>{{(0 if d.cancelled or d.doctype=='proforma' else d.due)|rs}}<td><span class="pill {{st(d)}}">{{st(d)}}</span></tr>
{% else %}<tr><td class=empty colspan=7>No purchase bills yet.</tr>{% endfor %}</tbody></table></div>{{pager(p, more, qs)}}""", rows=rows, st=bill_state, qs=qs, p=p, more=more)

def post_purchase(date, party, taxable, cg, sg, ig, ro, total):
    return post(date, [("Purchases", None, taxable, 0)] + [(a, None, x, 0) for a, x in (("Input CGST", cg), ("Input SGST", sg), ("Input IGST", ig)) if x]
                + ([("Round off", None, max(ro, 0), max(-ro, 0))] if ro else []) + [("Creditors", party, 0, total)])

def printed(f, p, taxable, cg, sg, ig):
    """Use the supplier's own printed figures (what the tax credit depends on); the item maths only checks them."""
    t, c_, s_, i_, r_, g_ = (paise(f[k] or 0) for k in ("pr_t", "pr_c", "pr_s", "pr_i", "pr_r", "pr_g"))
    if t + c_ + s_ + i_ + r_ != g_: raise Oops("The figures on the bill do not add up (taxable + taxes + round-off must equal the total)")
    if (intra(p) and i_) or (not intra(p) and (c_ or s_)): raise Oops("The tax type does not match the supplier's state (CGST+SGST for same state, IGST for other states)")
    gap = max(abs(t - taxable), abs(c_ + s_ + i_ - (cg + sg + ig)))
    if gap > 100 and not f.get("ack"): raise Oops(f"The bill's figures differ from the items by ₹{gap / 100:,.2f}. If the paper bill is right, tick the box and save again")
    return t, c_, s_, i_, r_, g_

@app.route("/purchase/new", methods=["GET", "POST"])
def purchase_new():
    if request.method == "GET":
        pre, miss = {}, None
        if d := request.args.get("draft"):
            dr = one("select * from drafts where id=? and status='new'", d)
            if not dr: raise Oops("This draft is already used or removed")
            pre = json.loads(dr["data"])
            m = one("select id from clients where (gstin!='' and gstin=?) or lower(name)=lower(?)", (pre.get("gstin") or "").upper(), pre.get("supplier") or "")
            pre["party"] = m["id"] if m else ""; miss = None if m else pre
        else: pre["party"] = request.args.get("party", "")
        src = pre.get("inbox_id"); sr = one("select path from inbox where id=?", src) if src else None
        src_pdf = bool(sr and (sr[0] or "").lower().endswith(".pdf"))
        rows = pre.get("lines") or []; rows = rows + [{}] * max(6 - len(rows), 1)
        irows, imap = item_data()
        return page("""<div class=head><h1>Purchase voucher</h1></div>
{% if miss %}<div class=note>📷 Read from photo. Supplier <b>{{miss.supplier}}</b> {{miss.gstin}} is not in Parties. <a href="/clients?name={{(miss.supplier or '')|urlencode}}&gstin={{(miss.gstin or '')|urlencode}}&kind=supplier"><b>Add this supplier first</b></a>, then reopen this draft.</div>
{% elif pre.supplier %}<div class=note>📷 Read from the bill. Check every box against the original below, then save.</div>{% endif %}
{% for w in pre.warnings or [] %}<div class=note>⚠ {{w}}</div>{% endfor %}
{% if src %}<div class=card style="padding:0;overflow:hidden"><div class=mute style="padding:10px 16px">Original bill</div>{% if src_pdf %}<iframe src="/inbox/file/{{src}}" style="width:100%;height:560px;border:0"></iframe>{% else %}<img src="/inbox/file/{{src}}" style="width:100%;display:block">{% endif %}</div>{% endif %}
<form method=post class=v><input type=hidden name=draft value="{{request.args.get('draft','')}}"><div class=card><div class=form>
<div><label>Series</label><div class=ro>Main</div></div><div><label>Voucher no.</label><div class=ro>{{vn}}</div></div><div><label>Date</label><input name=date type=date value={{d}}></div>
<div><label>Supplier</label><select name=party id=party required><option value="">Choose…</option>{% for c in cs %}<option value={{c.id}} data-intra={{c.intra}} data-bal={{c.bal}} data-gstin="{{c.gstin}}" {{'selected' if c.id|string==pre.party|string}}>{{c.name}}</option>{% endfor %}</select></div>
<div><label>Supplier's bill no.</label><input name=sup_no value="{{pre.sup_no or ''}}" class="{{'fl' if pre.supplier}}" required></div>
<div><label>Supplier's bill date</label><input name=sup_date type=date value="{{pre.sup_date or d}}" class="{{'fl' if pre.supplier}}" required></div>
<div><label>Document type</label><select name=doctype><option value=tax>Tax invoice<option value=proforma {{'selected' if pre.doctype=='proforma'}}>Proforma (not in books)</select></div>
<div><label>Rates are</label><select name=incl id=incl><option value=0>Without tax<option value=1 {{'selected' if pre.incl}}>Including tax</select></div>
<div><label>Job (optional)</label><select name=job><option value="">None</option>{% for j in js %}<option value={{j.id}}>{{j.name}}: {{j.title}}</option>{% endfor %}</select></div>
<div><label>Narration</label><input name=narration></div></div><p class=pinfo id=pinfo></p></div>
{{extras(pre.meta or {}, '')}}<div class=card>{{grid(rows, names, items, 'buy', pre.supplier, pre.printed or {})}}<button>✓ Verify & save</button></form>""",
            cs=parties("'supplier','both'", "Creditors", "cr", "dr"), pre=pre, miss=miss, rows=rows, d=today(), vn=vno("PU", "purchases", today()), names=[r["name"] for r in irows], items=imap, src=src, src_pdf=src_pdf,
            js=q("select j.*, c.name from jobs j join clients c on c.id=j.client_id order by j.id desc limit 200"))
    f = request.form; lines = parse(f); incl = f.get("incl") == "1"; proforma = f.get("doctype") == "proforma"
    p = one("select * from clients where id=?", f["party"]); sup_no = f["sup_no"].strip()
    dt.date.fromisoformat(f["sup_date"])
    if not sup_no: raise Oops("Supplier's bill number is needed")
    if one("select 1 from purchases where party_id=? and lower(sup_no)=lower(?) and cancelled=0 and coalesce(doctype,'tax')=?", p["id"], sup_no, "proforma" if proforma else "tax"): raise Oops("This supplier bill is already entered")
    rows, taxable, cg, sg, ig, ro, total = calc(lines, intra(p), incl); calc_total = total
    if f.get("touched"): taxable, cg, sg, ig, ro, total = printed(f, p, taxable, cg, sg, ig)
    with db() as c:
        no = vno("PF" if proforma else "PU", "purchases", f["date"])
        v = None if proforma else post_purchase(f["date"], p["id"], taxable, cg, sg, ig, ro, total)
        i = c.execute("insert into purchases(no,date,party_id,sup_no,sup_date,narration,taxable,cgst,sgst,igst,ro,total,voucher,draft_id,incl,meta,due,job_id,doctype,calc_total) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (no, f["date"], p["id"], sup_no, f["sup_date"], f["narration"], taxable, cg, sg, ig, ro, total, v, f["draft"] or None, int(incl), meta_of(f), f.get("due") or None, f["job"] or None, "proforma" if proforma else "tax", calc_total)).lastrowid
        c.executemany("insert into plines(purchase_id,descr,qty,rate,gst,hsn,unit,item_id,disc,taxable,taxamt) values(?,?,?,?,?,?,?,?,?,?,?)",
                      [(i, d, qy, r, gs, h, u, iid(d), dc, t, x) for (d, qy, r, gs, h, u, dc), (t, x) in zip(lines, rows)])
        if f["draft"]: c.execute("update drafts set status='done' where id=?", (f["draft"],))
    return redirect(f"/purchase/{i}")

@app.route("/purchase/<int:i>")
def purchase(i):
    v = one("select p.*, c.name, c.gstin, c.address from purchases p join clients c on c.id=p.party_id where p.id=?", i)
    ls = doc_lines(q("select descr,hsn,qty,unit,rate,disc,gst,taxable from plines where purchase_id=?", i))
    pays = q("select * from ppayments where purchase_id=? order by id", i); paid = sum(p["amount"] for p in pays if not p["cancelled"])
    job = one("select title from jobs where id=?", v["job_id"]) if v["job_id"] else None
    dr = one("select data from drafts where id=?", v["draft_id"]) if v["draft_id"] else None; src = json.loads(dr[0]).get("inbox_id") if dr else None
    return page("""<div class="head np"><h1>{{v.no}} {% if v.cancelled %}<span class="pill Cancelled">Cancelled</span>{% elif v.doctype=='proforma' %}<span class="pill Proforma">Proforma</span>{% elif v.total-paid<=0 %}<span class="pill Paid">Paid</span>{% endif %}</h1><div>
{% if not v.cancelled and v.doctype!='proforma' %}<a class=btn href="/payment?purchase={{v.id}}">Pay supplier</a> {% elif not v.cancelled %}<a class=btn href="/purchase/{{v.id}}/post">Post to books</a> {% endif %}<button class=ghost onclick=print()>Print</button></div></div>
{% if v.doctype=='proforma' %}<div class=note>This is a proforma. It is not in your books or stock yet. When the real tax invoice arrives, press <b>Post to books</b>.</div>{% endif %}
<div class=card><div style="display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:20px"><div><h2>Supplier</h2><b>{{v.name}}</b><br><span class=mute>{{v.gstin}}<br>{{v.address}}</span></div>
<div class=r><h2>Purchase bill</h2><b>{{v.no}}</b><br><span class=mute>{{v.date}}<br>Supplier bill {{v.sup_no}} · {{v.sup_date}}{% if v.due %}<br>Due {{v.due}}{% endif %}{% if job %}<br>Job: {{job.title}}{% endif %}{% if src %}<br><a href=/inbox/file/{{src}} target=_blank>View original bill</a>{% endif %}</span></div></div>
<table><thead><tr><th>Item<th>HSN<th class=r>Qty<th class=r>Rate{{' (incl. tax)' if v.incl}}<th class=r>Disc %<th class=r>GST %<th class=r>Amount</tr></thead><tbody>
{% for d,h,qy,u,r,dc,gs,a in ls %}<tr><td>{{d}}<td class=mute>{{h or ''}}<td class=r>{{qy}} {{u or ''}}<td class=r>{{r}}<td class=r>{{dc or ''}}<td class=r>{{gs}}<td class=r>{{a|rs}}</tr>{% endfor %}
<tr><td colspan=6 class=r>Taxable value<td class=r>{{v.taxable|rs}}</tr>
{% for n,x in (('CGST',v.cgst),('SGST',v.sgst),('IGST',v.igst)) if x %}<tr><td colspan=6 class=r>{{n}}<td class=r>{{x|rs}}</tr>{% endfor %}
{% if v.ro %}<tr><td colspan=6 class=r>Round off<td class=r>{{v.ro|rs}}</tr>{% endif %}
<tr><td colspan=6 class=r><b>Total (as printed by supplier)</b><td class=r><b>₹{{v.total|rs}}</b></tr></tbody></table>
{% if v.calc_total and v.calc_total!=v.total %}<p class=mute>Items add up to ₹{{v.calc_total|rs}}; the supplier's printed total is used.</p>{% endif %}
{% for k,x in meta.items() %}<p class=mute style="margin:4px 0">{{labels[k]}}: {{x}}</p>{% endfor %}{% if v.narration %}<p class=mute>Note: {{v.narration}}</p>{% endif %}</div>
<div class="card np"><h2>Payments · To pay ₹{{(0 if v.doctype=='proforma' else v.total-paid)|rs}}</h2>{% for p in pays %}<form method=post action=/payment/{{p.id}}/cancel style="display:flex;gap:12px;align-items:center;justify-content:space-between;padding:6px 0">
<span>{{p.date}} · {{p.mode}} · ₹{{p.amount|rs}} {% if p.cancelled %}<span class="pill Cancelled">undone</span>{% endif %}</span>{% if not p.cancelled %}<button class="ghost danger">Undo</button>{% endif %}</form>{% else %}<p class=empty>No payments yet.</p>{% endfor %}</div>
{% if not v.cancelled %}<form class=np method=post action=/purchase/{{v.id}}/cancel onsubmit="return confirm('Cancel this bill? A reversing entry will be posted.')"><button class=danger>Cancel bill</button></form>{% endif %}""",
        v=v, ls=ls, pays=pays, paid=paid, job=job, src=src, meta=json.loads(v["meta"] or "{}"), labels=META)

@app.route("/purchase/<int:i>/post", methods=["GET", "POST"])
def purchase_post(i):
    v = one("select * from purchases where id=? and doctype='proforma' and cancelled=0", i) or abort(404)
    if request.method == "GET":
        return page("""<div class=head><h1>Post to books</h1></div><div class=card><p class=mute>Enter the number and date of the real tax invoice. The amounts from the proforma will be used.</p><form method=post class=form>
<div><label>Supplier's bill no.</label><input name=sup_no required></div><div><label>Supplier's bill date</label><input name=sup_date type=date value={{d}} required></div><div><label>Entry date</label><input name=date type=date value={{d}}></div><button>Post to books</button></form></div>""", d=today())
    f = request.form; sup_no = f["sup_no"].strip(); dt.date.fromisoformat(f["sup_date"])
    if not sup_no: raise Oops("Supplier's bill number is needed")
    if one("select 1 from purchases where party_id=? and lower(sup_no)=lower(?) and cancelled=0 and coalesce(doctype,'tax')='tax'", v["party_id"], sup_no): raise Oops("This supplier bill is already entered")
    with db() as c:
        no = vno("PU", "purchases", f["date"])
        vch = post_purchase(f["date"], v["party_id"], v["taxable"], v["cgst"], v["sgst"], v["igst"], v["ro"], v["total"])
        c.execute("update purchases set doctype='tax', voucher=?, no=?, sup_no=?, sup_date=?, date=? where id=?", (vch, no, sup_no, f["sup_date"], f["date"], i))
    return redirect(f"/purchase/{i}")

@app.post("/purchase/<int:i>/cancel")
def purchase_cancel(i):
    with db() as c:
        v = one("select * from purchases where id=? and cancelled=0", i)
        if not v: raise Oops("Already cancelled")
        if one("select 1 from ppayments where purchase_id=? and cancelled=0", i): raise Oops("Undo its payments first")
        v["voucher"] and reverse(v["voucher"]); c.execute("update purchases set cancelled=1 where id=?", (i,))
    return redirect(f"/purchase/{i}")

@app.route("/drafts")
def drafts():
    rows = [(r, json.loads(r["data"])) for r in q("select * from drafts where status='new' order by id desc")]
    return page("""<div class=head><h1>Drafts</h1><form method=post action=/drafts/sample><button class=ghost>+ Add sample draft</button></form></div>
<div class=card><h2>Upload a bill</h2><p class=mute>A PDF or photo of a supplier bill. It is read on this computer, never sent anywhere, and opens as a draft for you to check.</p>
<form method=post action=/drafts/upload enctype=multipart/form-data class=form><div><input type=file name=f accept=".pdf,.jpg,.jpeg,.png,.webp" required></div><button>Read this bill</button></form>
<p style="margin-top:14px"><a class="btn ghost" href=/quick>⚡ Quick bill (type a command)</a></p></div>
<div class=card><p class=mute>Bills from Telegram and uploads wait here. Open one, check it against the paper, and save. Nothing reaches the books until you save.</p><table><thead><tr><th>Received<th>Party<th>Bill no.<th>Items<th><th></tr></thead><tbody>
{% for r,d in rows %}<tr><td class=mute>{{r.created}}<td><b>{{d.supplier or d.customer or '—'}}</b>{% if d.kind=='sale' %} <span class="pill active">sale</span>{% endif %}<td>{{d.sup_no or ''}}<td>{{d.lines|length}}<td>{% if d.warnings %}<span class="pill Due">⚠ {{d.warnings|length}}</span>{% endif %}
<td class=r><a class=btn href="{{'/invoice/new' if d.kind=='sale' else '/purchase/new'}}?draft={{r.id}}">Check</a>
<form method=post action=/draft/{{r.id}}/dismiss style=display:inline><button class="ghost danger">Remove</button></form></tr>{% else %}<tr><td class=empty colspan=6>No drafts waiting.</tr>{% endfor %}</tbody></table></div>""", rows=rows)

def add_draft(data):
    with db() as c: return c.execute("insert into drafts(created,data) values(?,?)", (dt.datetime.now().strftime("%d %b %H:%M"), json.dumps(data))).lastrowid

@app.post("/api/draft")                     # the Telegram bot / photo reader posts here
def api_draft(): return {"id": add_draft(request.get_json(force=True))}

@app.get("/api/drafts/count")               # the open web page asks this every few seconds
def api_count(): return {"n": one("select count(*) from drafts where status='new'")[0], "latest": one("select coalesce(max(id),0) from drafts")[0],
                         "files": one("select count(*) from inbox where status='new'")[0], "flatest": one("select coalesce(max(id),0) from inbox")[0]}

@app.post("/drafts/sample")
def draft_sample():
    add_draft({"supplier": "Sample Electronics (example)", "gstin": "", "sup_no": "SE/1042", "sup_date": today(),
               "lines": [{"descr": "2MP Dome Camera", "hsn": "85258900", "unit": "Pcs", "qty": "4", "rate": "1850", "gst": "18"},
                         {"descr": "Cat6 Cable 305m", "hsn": "85444299", "unit": "Roll", "qty": "1", "rate": "6200", "gst": "18"}]})
    return redirect("/drafts")

@app.post("/draft/<int:i>/dismiss")
def draft_dismiss(i):
    with db() as c: c.execute("update drafts set status='dismissed' where id=? and status='new'", (i,))
    return redirect("/drafts")

@app.post("/drafts/upload")
def drafts_upload():
    f = request.files.get("f")
    if not f or not f.filename: raise Oops("Choose a file first")
    name = secure_filename(f.filename)
    if name.rsplit(".", 1)[-1].lower() not in ("pdf", "jpg", "jpeg", "png", "webp"): raise Oops("Only PDF or photo files (jpg, png) can be read")
    rel = f"{dt.date.today():%Y-%m}/web_{int(time.time())}_{name}"; (UPLOADS / rel).parent.mkdir(parents=True, exist_ok=True); f.save(UPLOADS / rel)
    with db() as c:
        row = c.execute("insert into inbox(received,chat_id,msg_id,sender,kind,filename,path,size,caption,uid) values(?,?,?,?,?,?,?,?,?,?)",
                        (dt.datetime.now().strftime("%d %b %H:%M"), 0, int(time.time() * 1000), "Web upload", "document", name, rel, (UPLOADS / rel).stat().st_size, "", rel)).lastrowid
        did, res = intake.to_draft(c, UPLOADS / rel, row)
    if not did: raise Oops(res)
    return redirect(f"/purchase/new?draft={did}")

@app.route("/quick", methods=["GET", "POST"])
def quick_bill():
    if request.method == "POST":
        res = quick.parse_quick(request.form["text"], intake.parties(db()), intake.items(db()))
        if not res["lines"]: raise Oops("I could not find any item lines. Write one item per line, like: Camera 4 x 2500")
        with db() as c: did = intake.add_draft(c, res)
        return redirect(f"/invoice/new?draft={did}")
    return page("""<div class=head><h1>Quick bill</h1></div><div class=card><p class=mute>Type the bill in plain words. First line the customer, then one item per line: <b>item qty x rate</b>. The Sales voucher opens filled in, ready to check, save and print.</p>
<form method=post><textarea name=text rows=8 required placeholder="Bhoomi Patel&#10;2MP Camera 4 x 2500&#10;Labour 1 x 500 gst 12"></textarea><p><button>Create the draft</button></p></form>
<p class=mute>Items in your Item Master fill HSN, unit and GST by themselves, and supply the price if you leave it out. The same text works in your Telegram bot when it starts with the word <b>bill</b>.</p></div>""")

# ---------- receipt (money in) and payment (money out) ----------
def money(kind, ptbl, col, ours):
    """Shared screen for Receipt (customers) and Payment (suppliers)."""
    view, label = ("inv_due", "Receipt") if kind == "in" else ("pur_due", "Payment")
    if request.method == "GET":
        ds = q(f"select * from {view} where due>0 and cancelled=0 and doctype='tax' order by coalesce(due_date, date)")
        sel = request.args.get("invoice" if kind == "in" else "purchase", "")
        return page("""<div class=head><h1>{{label}}</h1></div><div class=card><h2>{{'Money received from a customer' if kind=='in' else 'Money paid to a supplier'}}</h2>{% if ds %}<form method=post class=form>
<div><label>Bill</label><select name=bill>{% for d in ds %}<option value={{d.id}} {{'selected' if d.id|string==sel}}>{{d.no}} · {{d.name}} · due ₹{{d.due|rs}}</option>{% endfor %}</select></div>
<div><label>Amount ₹</label><input name=amount type=number step=0.01 required></div><div><label>Date</label><input name=date type=date value={{d}}></div>
<div><label>{{'Received by' if kind=='in' else 'Paid by'}}</label><select name=mode><option>Cash<option>Bank<option>UPI</select></div><button>Save</button></form>{% else %}<p class=empty>No unpaid bills.</p>{% endif %}</div>""",
            ds=ds, sel=sel, d=today(), label=label, kind=kind)
    f = request.form
    with db() as c:
        b = one(f"select * from {view} where id=? and cancelled=0 and doctype='tax'", f["bill"]); amt = paise(f["amount"])
        if not b or not 0 < amt <= b["due"]: raise Oops("Amount must be above 0 and not more than the due")
        cash = ("Cash" if f["mode"] == "Cash" else "Bank", None)
        v = post(f["date"], [(*cash, amt, 0), ("Debtors", b[col], 0, amt)] if kind == "in" else [("Creditors", b[col], amt, 0), (*cash, 0, amt)])
        c.execute(f"insert into {ptbl}(date,{ours},amount,mode,voucher) values(?,?,?,?,?)", (f["date"], b["id"], amt, f["mode"], v))
    return redirect(f"/{'invoice' if kind == 'in' else 'purchase'}/{b['id']}")

@app.route("/receipt", methods=["GET", "POST"])
def receipt(): return money("in", "payments", "client_id", "invoice_id")

@app.route("/payment", methods=["GET", "POST"])
def payment(): return money("out", "ppayments", "party_id", "purchase_id")

def undo(tbl, bill_col, back):
    with db() as c:
        p = one(f"select * from {tbl} where id=? and cancelled=0", request.view_args["i"])
        if not p: raise Oops("Already undone")
        reverse(p["voucher"]); c.execute(f"update {tbl} set cancelled=1 where id=?", (p["id"],))
    return redirect(f"/{back}/{p[bill_col]}")

@app.post("/receipt/<int:i>/cancel")
def receipt_cancel(i): return undo("payments", "invoice_id", "invoice")

@app.post("/payment/<int:i>/cancel")
def payment_cancel(i): return undo("ppayments", "purchase_id", "purchase")

# ---------- reports ----------
@app.route("/outstanding")
def outstanding():
    return page("""<div class=head><h1>Outstanding</h1></div><div class=cols><div class=card><h2>To receive from customers · ₹{{(rec|sum(attribute='due'))|rs}}</h2><table><tbody>
{% for r in rec %}<tr><td><a href=/invoice/{{r.id}}>{{r.no}}</a><br><span class=mute>{{r.name}} · {{r.date}}{% if r.due_date %} · due {{r.due_date}}{% endif %}</span><td class=r><b>₹{{r.due|rs}}</b></tr>{% else %}<tr><td class=empty>Nothing pending.</tr>{% endfor %}</tbody></table></div>
<div class=card><h2>To pay suppliers · ₹{{(pay|sum(attribute='due'))|rs}}</h2><table><tbody>
{% for r in pay %}<tr><td><a href=/purchase/{{r.id}}>{{r.no}}</a><br><span class=mute>{{r.name}} · {{r.date}}{% if r.due_date %} · due {{r.due_date}}{% endif %}</span><td class=r><b>₹{{r.due|rs}}</b></tr>{% else %}<tr><td class=empty>Nothing pending.</tr>{% endfor %}</tbody></table></div></div>""",
        rec=q("select * from inv_due where due>0 and cancelled=0 order by coalesce(due_date, date)"), pay=q("select * from pur_due where due>0 and cancelled=0 and doctype='tax' order by coalesce(due_date, date)"))

@app.route("/ledger")
def ledger():
    tb = q("select account, sum(dr) dr, sum(cr) cr from ledger group by account order by account")
    return page("""<div class=head><h1>Books</h1></div><div class=card><h2>Trial balance</h2><table><thead><tr><th>Account<th class=r>Debit ₹<th class=r>Credit ₹</tr></thead><tbody>
{% for t in tb %}<tr><td>{{t.account}}<td class=r>{{t.dr|rs}}<td class=r>{{t.cr|rs}}</tr>{% else %}<tr><td class=empty colspan=3>No entries yet.</tr>{% endfor %}
{% if tb %}<tr><td><b>Total</b><td class=r><b>{{tb|sum(attribute='dr')|rs}}</b><td class=r><b>{{tb|sum(attribute='cr')|rs}}</b></tr>{% endif %}</tbody></table></div>
<div class=card><h2>Latest entries</h2><table><thead><tr><th>Entry<th>Date<th>Account<th class=r>Debit<th class=r>Credit</tr></thead><tbody>
{% for e in es %}<tr><td class=mute>#{{e.voucher}}<td class=mute>{{e.date}}<td>{{e.account}}<td class=r>{{e.dr|rs if e.dr}}<td class=r>{{e.cr|rs if e.cr}}</tr>{% endfor %}</tbody></table></div>""",
        tb=tb, es=q("select * from ledger order by id desc limit 60"))

# ---------- company details: printed on every tax invoice ----------
CO = ("co_name", "co_addr", "co_gstin", "co_pan", "co_phone", "co_email", "co_bank", "co_terms", "inv_prefix", "inv_first")

@app.route("/company", methods=["GET", "POST"])
def company():
    if request.method == "POST":
        f = request.form; x = f["co_gstin"].strip().upper()
        if x and not gstin_ok(x): raise Oops("GSTIN is not valid. Check every letter and digit.")
        if f["inv_first"].strip() and (not f["inv_first"].strip().isdigit() or int(f["inv_first"]) < 1): raise Oops("The next invoice number must be a whole number, 1 or more")
        if not re.fullmatch(r"[A-Za-z0-9]{0,6}", f["inv_prefix"].strip()): raise Oops("Invoice prefix can have only letters and digits")
        with db() as c:
            for k in CO: c.execute("insert or replace into settings values(?,?)", (k, (x if k == "co_gstin" else f[k]).strip()))
            if f["inv_first"].strip():
                t = dt.date.today(); c.execute("insert or replace into settings values('inv_first_for',?)", (f"{f['inv_prefix'].strip()}{(t.year - (t.month < 4)) % 100:02d}-",))
        return redirect("/company")
    return page("""<div class=head><h1>Company details</h1></div>{% if saved %}<div class=note>Printed on every tax invoice. Check each box carefully.</div>{% endif %}
<form method=post><div class=card><h2>Printed on invoices</h2><div class=form><div><label>Company name</label><input name=co_name value="{{c.co_name}}" required></div>
<div><label>GSTIN</label><input name=co_gstin value="{{c.co_gstin}}"></div><div><label>PAN</label><input name=co_pan value="{{c.co_pan}}"></div>
<div><label>Phone</label><input name=co_phone value="{{c.co_phone}}"></div><div><label>Email</label><input name=co_email value="{{c.co_email}}"></div>
<div style="grid-column:1/-1"><label>Address</label><input name=co_addr value="{{c.co_addr}}" required></div>
<div style="grid-column:1/-1"><label>Bank details</label><textarea name=co_bank>{{c.co_bank}}</textarea></div>
<div style="grid-column:1/-1"><label>Terms & conditions</label><textarea name=co_terms>{{c.co_terms}}</textarea></div></div></div>
<div class=card><h2>Invoice numbering</h2><p class=mute>Bills are numbered like <b>{{c.inv_prefix or 'D'}}26-001</b>: prefix, year the financial year starts, then a counter that restarts every April. Enter the number your <b>next</b> bill should get, so it continues from your last one.</p>
<div class=form><div><label>Prefix</label><input name=inv_prefix value="{{c.inv_prefix or 'D'}}"></div><div><label>Next invoice number</label><input name=inv_first type=number min=1 value="{{c.inv_first}}"></div></div></div>
<button>Save company details</button></form>""", c={k: cfg(k) for k in CO}, saved=not cfg("co_name"))

# ---------- inbox: everything received on Telegram ----------
def pair_code():
    if r := one("select value from settings where key='pair_code'"): return r[0]
    code = f"{secrets.randbelow(10**6):06d}"
    with db() as c: c.execute("insert or replace into settings values('pair_code',?)", (code,))
    return code

@app.route("/inbox")
def inbox():
    return page("""<div class=head><h1>Inbox</h1></div><div class=card><h2>Connect Telegram</h2><p>Open your bot in Telegram and send: <b>/start {{code}}</b></p>
<p class=mute>Connected: {% for u in users %}{{u.name}}{{', ' if not loop.last}}{% else %}nobody yet{% endfor %}</p></div>
<div class=card><table><thead><tr><th>Received<th>From<th>File<th>Note<th>Status<th></tr></thead><tbody>
{% for r in rows %}<tr><td class=mute>{{r.received}}<td>{{r.sender}}<td>{% if r.path %}{% if r.kind=='photo' %}<a href=/inbox/file/{{r.id}} target=_blank><img src=/inbox/file/{{r.id}} style="height:56px;border-radius:6px"></a>{% else %}<a href=/inbox/file/{{r.id}} target=_blank>{{r.filename}}</a>{% endif %} <span class=mute>{{(r.size/1024)|round|int}} KB</span>{% else %}<span class=mute>text</span>{% endif %}
<td>{{r.caption or ''}}<td><span class="pill {{r.status}}">{{r.status}}</span><td class=r>{% if r.status=='new' %}<form method=post action=/inbox/{{r.id}}/done style=display:inline><button class=ghost>Mark done</button></form>{% endif %}</tr>
{% else %}<tr><td class=empty colspan=6>Nothing received yet.</tr>{% endfor %}</tbody></table></div>""",
        code=pair_code(), users=q("select * from tg_users"), rows=q("select * from inbox order by id desc limit 200"))

@app.route("/inbox/file/<int:i>")
def inbox_file(i):
    r = one("select path from inbox where id=? and path is not null", i) or abort(404)
    safe = mimetypes.guess_type(r[0])[0] in ("image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf")   # anything else downloads, never runs
    resp = send_from_directory(UPLOADS, r[0], as_attachment=not safe); resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

@app.post("/inbox/<int:i>/done")
def inbox_done(i):
    with db() as c: c.execute("update inbox set status='done' where id=?", (i,))
    return redirect("/inbox")

@app.route("/backup")
def backup():
    mem = sqlite3.connect(":memory:"); db().backup(mem)
    return Response(mem.serialize(), mimetype="application/octet-stream", headers={"Content-Disposition": f"attachment; filename=divine-books-{today()}.db"})

# ---------- login (only when APP_PASSWORD is set, e.g. on the internet) ----------
@app.before_request
def gate():
    if not PASSWORD or session.get("ok") or request.endpoint in ("login", "static"): return
    if API_KEY and request.path.startswith("/api/") and hmac.compare_digest(request.headers.get("X-Api-Key", ""), API_KEY): return
    return redirect("/login")

@app.route("/login", methods=["GET", "POST"])
def login():
    bad = False
    if request.method == "POST":
        if PASSWORD and hmac.compare_digest(request.form["password"], PASSWORD): session["ok"] = True; return redirect("/")
        time.sleep(1); bad = True
    return page("""<style>aside{display:none}.app{display:block}</style><div class=card style="max-width:380px;margin:80px auto"><h1>Divine Books</h1>
<form method=post class=form style="margin-top:16px"><div><label>Password</label><input name=password type=password autofocus required></div><button>Log in</button></form>{% if bad %}<p class=note style="margin-top:16px">Wrong password</p>{% endif %}</div>""", bad=bad)

@app.route("/logout")
def logout():
    session.clear(); return redirect("/login")

import chatcmd; chatcmd.register(app, globals())

if __name__ == "__main__":
    app.run(port=5050, debug=True)
