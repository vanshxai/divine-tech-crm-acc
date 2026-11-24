import os, sqlite3, json, re, time, secrets, hmac, mimetypes, datetime as dt
from pathlib import Path
from decimal import Decimal as D, ROUND_HALF_UP
from flask import Flask, Request, request, redirect, render_template_string as R, g, session, Response, send_from_directory, abort
from gujarati import GU

OWN_STATE, OWN_CODE, PREFIX = "Gujarat", "24", "DT"   # same state -> CGST+SGST, else IGST
DB = Path(__file__).with_name("books.db")
for _l in (Path(__file__).with_name(".env").read_text().splitlines() if Path(__file__).with_name(".env").exists() else []):
    if "=" in _l and not _l.startswith("#"): os.environ.setdefault(*_l.split("=", 1))
UPLOADS = Path(__file__).with_name("uploads")
PASSWORD, API_KEY, PER = os.environ.get("APP_PASSWORD"), os.environ.get("API_KEY"), 100   # no APP_PASSWORD = open on this computer only

class Req(Request): max_form_parts, max_form_memory_size = 500_000, 100_000_000        # a bill can have thousands of fields

app = Flask(__name__); app.request_class = Req
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.environ.get("HTTPS")))

SCHEMA = """
create table if not exists clients(id integer primary key, name text not null, phone, gstin, state, address, kind text default 'customer');
create table if not exists items(id integer primary key, name text not null, unit text, hsn text, gst text, sale text, purch text, opening text);
create table if not exists jobs(id integer primary key, client_id int, title text, status text default 'enquiry', followup text);
create table if not exists invoices(id integer primary key, no text unique, date text, client_id int, narration text, taxable int, cgst int, sgst int, igst int, ro int, total int, voucher int, cancelled int default 0);
create table if not exists lines(invoice_id int, descr text, qty text, rate text, gst text, hsn text, unit text, item_id int);
create table if not exists payments(id integer primary key, date text, invoice_id int, amount int, mode text, voucher int, cancelled int default 0);
create table if not exists purchases(id integer primary key, no text unique, date text, party_id int, sup_no text, sup_date text, narration text, taxable int, cgst int, sgst int, igst int, ro int, total int, voucher int, draft_id int, cancelled int default 0);
create table if not exists plines(purchase_id int, descr text, qty text, rate text, gst text, hsn text, unit text, item_id int);
create table if not exists ppayments(id integer primary key, date text, purchase_id int, amount int, mode text, voucher int, cancelled int default 0);
create table if not exists drafts(id integer primary key, created text, data text, status text default 'new');
create table if not exists settings(key text primary key, value text);
create table if not exists tg_users(chat_id integer primary key, name text, added text);
create table if not exists inbox(id integer primary key, received text, chat_id int, msg_id int, sender text, kind text, filename text, path text, size int, caption text, uid text, status text default 'new');
create unique index if not exists inbox_uq on inbox(chat_id, msg_id, uid);
create table if not exists ledger(id integer primary key, voucher int, date text, account text, client_id int, dr int default 0, cr int default 0);
create trigger if not exists l_upd before update on ledger begin select raise(abort,'ledger is append-only'); end;
create trigger if not exists l_del before delete on ledger begin select raise(abort,'ledger is append-only'); end;
drop view if exists inv_due; drop view if exists pur_due;
create view inv_due as select i.id, i.no, i.date, i.client_id, c.name, i.total, i.cancelled,
  i.total - coalesce((select sum(amount) from payments where invoice_id=i.id and cancelled=0),0) as due
  from invoices i join clients c on c.id=i.client_id;
create view pur_due as select p.id, p.no, p.date, p.party_id, c.name, p.sup_no, p.total, p.cancelled,
  p.total - coalesce((select sum(amount) from ppayments where purchase_id=p.id and cancelled=0),0) as due
  from purchases p join clients c on c.id=p.party_id;
"""
_c = sqlite3.connect(DB); _c.executescript(SCHEMA)
for _s in ("alter table clients add column kind text default 'customer'", "alter table lines add column hsn text", "alter table lines add column unit text",
           "alter table lines add column item_id int", "alter table plines add column unit text", "alter table plines add column item_id int",
           "alter table invoices add column narration text"):
    try: _c.execute(_s)
    except sqlite3.OperationalError: pass
_c.commit(); _c.close()

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

def parse(f):
    out = []
    for d, qty, rate, gst, hsn, unit in zip(*(f.getlist(k) for k in ("d", "q", "r", "g", "h", "u"))):
        d, rate = d.strip(), rate.strip()
        if not d and not rate: continue
        if not rate: raise Oops(f"Rate is missing for {d}")
        out.append((d, qty, rate, gst, hsn.strip(), unit.strip()))
    if not out: raise Oops("Add at least one item")
    return out

def tax(lines, same_state):
    taxable = gst = 0
    for d, qty, rate, g_, *_ in lines:
        if not d or D(qty) <= 0 or D(rate) < 0 or not 0 <= D(g_) <= 28: raise Oops("Check item, quantity, rate and GST")
        t = paise(D(qty) * D(rate)); taxable += t; gst += rnd(D(t) * D(g_) / 100)
    cg, sg, ig = (gst // 2, gst - gst // 2, 0) if same_state else (0, 0, gst)
    exact = taxable + gst; total = (exact + 50) // 100 * 100
    return taxable, cg, sg, ig, total - exact, total

def vno(pre, table, date):
    d = dt.date.fromisoformat(date); y = d.year - (d.month < 4); p = f"{pre}/{y}-{(y + 1) % 100:02d}/"
    return p + f"{one(f'select count(*) from {table} where no like ?', p + '%')[0] + 1:04d}"

iid = lambda name: (one("select id from items where lower(name)=lower(?)", name) or [None])[0]
STOCK = """select i.*, cast(i.opening as real)
  + coalesce((select sum(cast(qty as real)) from plines p join purchases h on h.id=p.purchase_id where p.item_id=i.id and h.cancelled=0),0)
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
                    ("/items", "Items", "M21 8l-9-5-9 5v8l9 5 9-5zM3 8l9 5 9-5M12 13v8", "/item")]),
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
input,select{font:15px Inter,system-ui,sans-serif;height:40px;padding:0 12px;border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--ink);width:100%}
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
#toast{position:fixed;right:20px;bottom:20px;background:var(--card);border:1px solid var(--acc);border-radius:12px;padding:14px 18px;box-shadow:0 8px 30px #0003;z-index:9}
.gu{display:block;font:500 .95em/1.35 'Noto Sans Gujarati',Inter,sans-serif;text-transform:none;letter-spacing:0;opacity:.9}
h1 .gu{font-size:.52em;margin-top:2px}.btn .gu,button .gu,.pill .gu{display:inline;margin-left:6px}.pill .gu{margin-left:4px}.nogu .gu{display:none!important}
@media(max-width:820px){aside .gu{display:none}.app{display:block}aside{position:fixed;top:auto;bottom:0;left:0;right:0;height:auto;flex-direction:row;overflow-x:auto;padding:6px;z-index:5}
.brand,.grp{display:none}aside a{flex-direction:column;gap:2px;font-size:11px;padding:6px 10px}main{padding:20px 16px 96px}.cols,.vfoot{grid-template-columns:1fr}}
@media print{aside,.np,#toast{display:none}.app{display:block}main{padding:0}.card{border:0}}
</style><div class=app><aside><div class=brand>Divine Books<small>Divine Technologies</small></div>
{% for grp, items in nav %}{% if grp %}<div class=grp>{{grp}}</div>{% endif %}{% for h,n,d,m in items %}<a href={{h}} class="{{'on' if (m=='/' and request.path=='/') or (m!='/' and request.path.startswith(m))}}"><svg viewBox="0 0 24 24"><path d="{{d}}"/></svg><span>{{n}}</span>{% if n=='Drafts' and nd %}<em>{{nd}}</em>{% elif n=='Inbox' and ni %}<em>{{ni}}</em>{% endif %}</a>{% endfor %}{% endfor %}<div style="margin-top:auto;padding:12px"><a href=/backup style="padding:0 0 8px;font-size:13px">⬇ Backup</a>{% if pw %}<a href=/logout style="padding:0 0 8px;font-size:13px">Log out</a>{% endif %}<a href=# id=lang style="padding:0;font-size:13px"></a></div></aside><main>
{% macro grid(rows, names, imap, mode, fl) %}<datalist id=itemlist>{% for n in names %}<option value="{{n}}">{% endfor %}</datalist>
<table id=lines><thead><tr><th>Item<th style=width:110px>HSN<th style=width:70px>Qty<th style=width:80px>Unit<th style=width:105px>Rate ₹<th style=width:62px>GST %<th class=r style=width:105px>Amount<th style=width:44px></tr></thead><tbody>
{% for r in rows %}{% set c = 'fl' if fl and r.descr else '' %}{% set dl = ([loop.index, 12]|min) * 0.15 %}<tr><td><input name=d value="{{r.descr}}" list=itemlist autocomplete=off class="{{c}}" style="animation-delay:{{dl}}s">
<td><input name=h value="{{r.hsn}}" class="{{c}}" style="animation-delay:{{dl+0.05}}s"><td><input name=q value="{{r.qty or 1}}" class="{{c}}" style="animation-delay:{{dl+0.1}}s">
<td><input name=u value="{{r.unit or ''}}" class="{{c}}" style="animation-delay:{{dl+0.15}}s"><td><input name=r value="{{r.rate}}" class="{{c}}" style="animation-delay:{{dl+0.2}}s">
<td><input name=g value="{{r.gst or 18}}" class="{{c}}" style="animation-delay:{{dl+0.25}}s"><td class="r amt"><td><button type=button class=ghost onclick="delRow(this)" tabindex=-1 title="Remove row">×</button></tr>{% endfor %}</tbody></table>
<p><button type=button class=ghost onclick="addRow()">+ Add row</button> <span class=mute id=cnt></span></p></div>
<div class=vfoot><div class=card><h2>GST summary</h2><table><thead><tr><th>Rate<th class=r>Taxable<th class=r>CGST<th class=r>SGST<th class=r>IGST</tr></thead><tbody id=gs></tbody></table></div>
<div class=card><h2>Bill sundry</h2><table><tbody id=sundry></tbody></table><div class=bigtot id=grand>₹0.00</div></div></div>
<script>
const ITEMS={{imap|tojson}},MODE="{{mode}}",P=document.getElementById('party'),body=document.querySelector('#lines tbody');
const f2=x=>x.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2}),blank=l=>l.forEach((e,k)=>{e.value=k==2?1:k==5?18:'';e.className='';e.style.animation='none'});
function addRow(){const t=body.lastElementChild.cloneNode(true);blank(t.querySelectorAll('input'));t.querySelector('.amt').textContent='';body.append(t);return t}
function delRow(b){const t=b.closest('tr');if(body.rows.length>1)t.remove();else blank(t.querySelectorAll('input'));calc()}
function info(){const o=P.selectedOptions[0];document.getElementById('pinfo').textContent=!o||!o.value?'':(o.dataset.gstin||'No GSTIN')+' · '+(o.dataset.intra=='1'?'Same state: CGST + SGST':'Other state: IGST')+' · '+(MODE=='sale'?'They owe us ₹':'We owe ₹')+f2(o.dataset.bal/100)}
function calc(){const o=P.selectedOptions[0],same=!o||o.dataset.intra!='0';let grp={},tx=0,gs=0,k=0;
for(const r of body.rows){const n=r.querySelectorAll('input'),a=Math.round((+n[2].value||0)*(+n[4].value||0)*100)/100;r.querySelector('.amt').textContent=a?f2(a):'';
if(a){k++;const rt=+n[5].value||0,t=Math.round(a*rt)/100;grp[rt]=grp[rt]||{x:0,t:0};grp[rt].x+=a;grp[rt].t+=t;tx+=a;gs+=t}}
const h=Object.keys(grp).sort((a,b)=>a-b).map(k=>{const v=grp[k],c=same?f2(v.t/2):'',i=same?'':f2(v.t);return '<tr><td>'+k+'%<td class=r>'+f2(v.x)+'<td class=r>'+c+'<td class=r>'+c+'<td class=r>'+i+'</tr>'}).join('');
document.getElementById('gs').innerHTML=h||'<tr><td class=mute>Add items above</tr>';const ex=tx+gs,tot=Math.round(ex);
document.getElementById('sundry').innerHTML='<tr><td>Taxable value<td class=r>'+f2(tx)+'</tr>'+(same?'<tr><td>CGST<td class=r>'+f2(gs/2)+'</tr><tr><td>SGST<td class=r>'+f2(gs/2)+'</tr>':'<tr><td>IGST<td class=r>'+f2(gs)+'</tr>')+'<tr><td>Round off<td class=r>'+f2(tot-ex)+'</tr>';
document.getElementById('grand').textContent='₹'+f2(tot);document.getElementById('cnt').textContent=k+' items'}
document.addEventListener('input',e=>{if(e.target.name=='d'&&e.target.value&&e.target.closest('tr')==body.lastElementChild)addRow();calc()});
P.addEventListener('change',()=>{info();calc()});
document.addEventListener('keydown',e=>{const t=e.target;if(e.key!='Enter'||t.tagName!='INPUT'||!t.form||!t.form.classList.contains('v'))return;e.preventDefault();
const all=()=>[...t.form.querySelectorAll('input:not([type=hidden]),select')];let f=all();if(t.closest('#lines')&&f.indexOf(t)==f.length-1){addRow();f=all()}f[f.indexOf(t)+1]?.focus()});
document.addEventListener('change',e=>{const m=ITEMS[(e.target.value||'').toLowerCase()];if(!m||!e.target.name||e.target.name!='d')return;
const n=e.target.closest('tr').querySelectorAll('input');n[1].value=m.hsn||'';n[3].value=m.unit||'';n[4].value=(MODE=='sale'?m.sale:m.buy)||'';n[5].value=m.gst||18;calc()});
info();calc()</script>{% endmacro %}
{% macro bar(qs) %}<form class=np style="display:flex;gap:8px;margin-bottom:16px"><input name=q value="{{qs}}" placeholder="Search…" style="max-width:340px"><button class=ghost>Search</button>{% if qs %}<a class="btn ghost" href="{{request.path}}">Clear</a>{% endif %}</form>{% endmacro %}
{% macro pager(p, more, qs) %}<div class=np style="display:flex;gap:8px;justify-content:center;margin-top:8px">{% if p>1 %}<a class="btn ghost" href="?p={{p-1}}&q={{qs|urlencode}}">← Previous</a>{% endif %}{% if more %}<a class="btn ghost" href="?p={{p+1}}&q={{qs|urlencode}}">Next →</a>{% endif %}</div>{% endmacro %}"""
TAIL = """</main></div><div id=toast hidden></div><script>
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
L.onclick=e=>{e.preventDefault();on=!on;try{localStorage.setItem('gu',on?'on':'off')}catch(e){}setL(on)}</script>"""
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

def bill_state(d): return "Cancelled" if d["cancelled"] else "Paid" if d["due"] <= 0 else "Part" if d["due"] < d["total"] else "Due"

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
    return n, f["unit"].strip() or "Pcs", f["hsn"].strip(), f["gst"], f["sale"] or "0", f["purch"] or "0", f["opening"] or "0"

ITEM_FORM = """<div><label>Name</label><input name=name value="{{i.name}}" required></div><div><label>Unit</label><input name=unit value="{{i.unit or 'Pcs'}}" list=units><datalist id=units><option>Pcs<option>Nos<option>Mtr<option>Box<option>Set<option>Roll<option>Kg<option>Ltr</datalist></div>
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
    return page("""<div class=head><h1>Sales</h1><a class=btn href=/invoice/new>+ New sales bill</a></div>{{bar(qs)}}<div class=card><table><thead><tr><th>Number<th>Date<th>Customer<th class=r>Total<th class=r>Due<th>Status</tr></thead><tbody>
{% for d in rows %}<tr><td><a href=/invoice/{{d.id}}><b>{{d.no}}</b></a><td class=mute>{{d.date}}<td>{{d.name}}<td class=r>₹{{d.total|rs}}<td class=r>{{(0 if d.cancelled else d.due)|rs}}<td><span class="pill {{st(d)}}">{{st(d)}}</span></tr>
{% else %}<tr><td class=empty colspan=6>No sales bills yet.</tr>{% endfor %}</tbody></table></div>{{pager(p, more, qs)}}""", rows=rows, st=bill_state, qs=qs, p=p, more=more)

@app.route("/invoice/new", methods=["GET", "POST"])
def invoice_new():
    if request.method == "GET":
        rows, imap = item_data()
        return page("""<div class=head><h1>Sales voucher</h1></div><form method=post class=v><div class=card><div class=form>
<div><label>Series</label><div class=ro>Main</div></div><div><label>Voucher no.</label><div class=ro>{{vn}}</div></div><div><label>Date</label><input name=date type=date value={{d}}></div>
<div><label>Customer</label><select name=client id=party required><option value="">Choose…</option>{% for c in cs %}<option value={{c.id}} data-intra={{c.intra}} data-bal={{c.bal}} data-gstin="{{c.gstin}}" {{'selected' if c.id|string==sel}}>{{c.name}}</option>{% endfor %}</select></div>
<div><label>Job (optional)</label><select name=job><option value="">None</option>{% for j in js %}<option value={{j.id}}>{{j.name}}: {{j.title}}</option>{% endfor %}</select></div>
<div><label>Narration</label><input name=narration></div></div><p class=pinfo id=pinfo></p></div>
<div class=card>{{grid([{}] * 6, names, items, 'sale', false)}}<button>Save sales bill</button></form>""",
            cs=parties("'customer','both'", "Debtors", "dr", "cr"), sel=request.args.get("client", ""), d=today(), vn=vno(PREFIX, "invoices", today()), names=[r["name"] for r in rows], items=imap,
            js=q("select j.*, c.name from jobs j join clients c on c.id=j.client_id where status!='invoiced'"))
    f = request.form; lines = parse(f)
    cl = one("select * from clients where id=?", f["client"])
    taxable, cg, sg, ig, ro, total = tax(lines, intra(cl))
    with db() as c:
        no = vno(PREFIX, "invoices", f["date"])
        v = post(f["date"], [("Debtors", cl["id"], total, 0), ("Sales", None, 0, taxable)]
                 + [(a, None, 0, x) for a, x in (("CGST", cg), ("SGST", sg), ("IGST", ig)) if x]
                 + ([("Round off", None, max(-ro, 0), max(ro, 0))] if ro else []))
        i = c.execute("insert into invoices(no,date,client_id,narration,taxable,cgst,sgst,igst,ro,total,voucher) values(?,?,?,?,?,?,?,?,?,?,?)",
                      (no, f["date"], cl["id"], f["narration"], taxable, cg, sg, ig, ro, total, v)).lastrowid
        c.executemany("insert into lines(invoice_id,descr,qty,rate,gst,hsn,unit,item_id) values(?,?,?,?,?,?,?,?)", [(i, d, qy, r, gs, h, u, iid(d)) for d, qy, r, gs, h, u in lines])
        if f["job"]: c.execute("update jobs set status='invoiced' where id=?", (f["job"],))
    return redirect(f"/invoice/{i}")

DOC = """<div class=card><div style="display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:20px"><div><h2>{{who}}</h2><b>{{v.name}}</b><br><span class=mute>{{v.gstin}}<br>{{v.address}}</span></div>
<div class=r><h2>{{title}}</h2><b>{{v.no}}</b><br><span class=mute>{{v.date}}</span>{% if v.sup_no %}<br><span class=mute>Supplier bill {{v.sup_no}} · {{v.sup_date}}</span>{% endif %}</div></div>
<table><thead><tr><th>Item<th>HSN<th class=r>Qty<th class=r>Rate<th class=r>GST %<th class=r>Amount</tr></thead><tbody>
{% for d,h,qy,u,r,gs,a in ls %}<tr><td>{{d}}<td class=mute>{{h or ''}}<td class=r>{{qy}} {{u or ''}}<td class=r>{{r}}<td class=r>{{gs}}<td class=r>{{a|rs}}</tr>{% endfor %}
<tr><td colspan=5 class=r>Taxable value<td class=r>{{v.taxable|rs}}</tr>
{% for n,x in (('CGST',v.cgst),('SGST',v.sgst),('IGST',v.igst)) if x %}<tr><td colspan=5 class=r>{{n}}<td class=r>{{x|rs}}</tr>{% endfor %}
{% if v.ro %}<tr><td colspan=5 class=r>Round off<td class=r>{{v.ro|rs}}</tr>{% endif %}
<tr><td colspan=5 class=r><b>Total</b><td class=r><b>₹{{v.total|rs}}</b></tr></tbody></table>{% if v.narration %}<p class=mute>Note: {{v.narration}}</p>{% endif %}</div>"""

def doc_lines(rows): return [(l["descr"], l["hsn"], l["qty"], l["unit"], l["rate"], l["gst"], paise(D(l["qty"]) * D(l["rate"]))) for l in rows]

@app.route("/invoice/<int:i>")
def invoice(i):
    v = one("select i.*, c.name, c.gstin, c.address from invoices i join clients c on c.id=i.client_id where i.id=?", i)
    ls = doc_lines(q("select descr,hsn,qty,unit,rate,gst from lines where invoice_id=?", i))
    pays = q("select * from payments where invoice_id=? order by id", i); paid = sum(p["amount"] for p in pays if not p["cancelled"])
    return page("""<div class="head np"><h1>{{v.no}} {% if v.cancelled %}<span class="pill Cancelled">Cancelled</span>{% elif v.total-paid<=0 %}<span class="pill Paid">Paid</span>{% endif %}</h1><div>
{% if not v.cancelled %}<a class=btn href="/receipt?invoice={{v.id}}">Record receipt</a> {% endif %}<button class=ghost onclick=print()>Print</button></div></div>""" + DOC + """
<div class="card np"><h2>Receipts · Due ₹{{(v.total-paid)|rs}}</h2>{% for p in pays %}<form method=post action=/receipt/{{p.id}}/cancel style="display:flex;gap:12px;align-items:center;justify-content:space-between;padding:6px 0">
<span>{{p.date}} · {{p.mode}} · ₹{{p.amount|rs}} {% if p.cancelled %}<span class="pill Cancelled">undone</span>{% endif %}</span>{% if not p.cancelled %}<button class="ghost danger">Undo</button>{% endif %}</form>{% else %}<p class=empty>No receipts yet.</p>{% endfor %}</div>
{% if not v.cancelled %}<form class=np method=post action=/invoice/{{v.id}}/cancel onsubmit="return confirm('Cancel this bill? A reversing entry will be posted.')"><button class=danger>Cancel bill</button></form>{% endif %}""",
        v=v, ls=ls, pays=pays, paid=paid, who="Bill to", title="Tax invoice")

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
{% for d in rows %}<tr><td><a href=/purchase/{{d.id}}><b>{{d.no}}</b></a><td class=mute>{{d.date}}<td>{{d.name}}<td class=mute>{{d.sup_no}}<td class=r>₹{{d.total|rs}}<td class=r>{{(0 if d.cancelled else d.due)|rs}}<td><span class="pill {{st(d)}}">{{st(d)}}</span></tr>
{% else %}<tr><td class=empty colspan=7>No purchase bills yet.</tr>{% endfor %}</tbody></table></div>{{pager(p, more, qs)}}""", rows=rows, st=bill_state, qs=qs, p=p, more=more)

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
        rows = pre.get("lines") or []; rows = rows + [{}] * max(6 - len(rows), 1)
        irows, imap = item_data()
        return page("""<div class=head><h1>Purchase voucher</h1></div>
{% if miss %}<div class=note>📷 Read from photo. Supplier <b>{{miss.supplier}}</b> {{miss.gstin}} is not in Parties. <a href="/clients?name={{(miss.supplier or '')|urlencode}}&gstin={{(miss.gstin or '')|urlencode}}&kind=supplier"><b>Add this supplier first</b></a>, then reopen this draft.</div>
{% elif pre.supplier %}<div class=note>📷 Read from photo. Check every box against the paper bill, then save.</div>{% endif %}
<form method=post class=v><input type=hidden name=draft value="{{request.args.get('draft','')}}"><div class=card><div class=form>
<div><label>Series</label><div class=ro>Main</div></div><div><label>Voucher no.</label><div class=ro>{{vn}}</div></div><div><label>Date</label><input name=date type=date value={{d}}></div>
<div><label>Supplier</label><select name=party id=party required><option value="">Choose…</option>{% for c in cs %}<option value={{c.id}} data-intra={{c.intra}} data-bal={{c.bal}} data-gstin="{{c.gstin}}" {{'selected' if c.id|string==pre.party|string}}>{{c.name}}</option>{% endfor %}</select></div>
<div><label>Supplier's bill no.</label><input name=sup_no value="{{pre.sup_no or ''}}" class="{{'fl' if pre.supplier}}" required></div>
<div><label>Supplier's bill date</label><input name=sup_date type=date value="{{pre.sup_date or d}}" class="{{'fl' if pre.supplier}}" required></div>
<div><label>Narration</label><input name=narration></div></div><p class=pinfo id=pinfo></p></div>
<div class=card>{{grid(rows, names, items, 'buy', pre.supplier)}}<button>✓ Verify & save</button></form>""",
            cs=parties("'supplier','both'", "Creditors", "cr", "dr"), pre=pre, miss=miss, rows=rows, d=today(), vn=vno("PU", "purchases", today()), names=[r["name"] for r in irows], items=imap)
    f = request.form; lines = parse(f)
    p = one("select * from clients where id=?", f["party"]); sup_no = f["sup_no"].strip()
    dt.date.fromisoformat(f["sup_date"])
    if not sup_no: raise Oops("Supplier's bill number is needed")
    if one("select 1 from purchases where party_id=? and lower(sup_no)=lower(?) and cancelled=0", p["id"], sup_no): raise Oops("This supplier bill is already entered")
    taxable, cg, sg, ig, ro, total = tax(lines, intra(p))
    with db() as c:
        no = vno("PU", "purchases", f["date"])
        v = post(f["date"], [("Purchases", None, taxable, 0)] + [(a, None, x, 0) for a, x in (("Input CGST", cg), ("Input SGST", sg), ("Input IGST", ig)) if x]
                 + ([("Round off", None, max(ro, 0), max(-ro, 0))] if ro else []) + [("Creditors", p["id"], 0, total)])
        i = c.execute("insert into purchases(no,date,party_id,sup_no,sup_date,narration,taxable,cgst,sgst,igst,ro,total,voucher,draft_id) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (no, f["date"], p["id"], sup_no, f["sup_date"], f["narration"], taxable, cg, sg, ig, ro, total, v, f["draft"] or None)).lastrowid
        c.executemany("insert into plines(purchase_id,descr,qty,rate,gst,hsn,unit,item_id) values(?,?,?,?,?,?,?,?)", [(i, d, qy, r, gs, h, u, iid(d)) for d, qy, r, gs, h, u in lines])
        if f["draft"]: c.execute("update drafts set status='done' where id=?", (f["draft"],))
    return redirect(f"/purchase/{i}")

@app.route("/purchase/<int:i>")
def purchase(i):
    v = one("select p.*, c.name, c.gstin, c.address from purchases p join clients c on c.id=p.party_id where p.id=?", i)
    ls = doc_lines(q("select descr,hsn,qty,unit,rate,gst from plines where purchase_id=?", i))
    pays = q("select * from ppayments where purchase_id=? order by id", i); paid = sum(p["amount"] for p in pays if not p["cancelled"])
    return page("""<div class="head np"><h1>{{v.no}} {% if v.cancelled %}<span class="pill Cancelled">Cancelled</span>{% elif v.total-paid<=0 %}<span class="pill Paid">Paid</span>{% endif %}</h1><div>
{% if not v.cancelled %}<a class=btn href="/payment?purchase={{v.id}}">Pay supplier</a> {% endif %}<button class=ghost onclick=print()>Print</button></div></div>""" + DOC + """
<div class="card np"><h2>Payments · To pay ₹{{(v.total-paid)|rs}}</h2>{% for p in pays %}<form method=post action=/payment/{{p.id}}/cancel style="display:flex;gap:12px;align-items:center;justify-content:space-between;padding:6px 0">
<span>{{p.date}} · {{p.mode}} · ₹{{p.amount|rs}} {% if p.cancelled %}<span class="pill Cancelled">undone</span>{% endif %}</span>{% if not p.cancelled %}<button class="ghost danger">Undo</button>{% endif %}</form>{% else %}<p class=empty>No payments yet.</p>{% endfor %}</div>
{% if not v.cancelled %}<form class=np method=post action=/purchase/{{v.id}}/cancel onsubmit="return confirm('Cancel this bill? A reversing entry will be posted.')"><button class=danger>Cancel bill</button></form>{% endif %}""",
        v=v, ls=ls, pays=pays, paid=paid, who="Supplier", title="Purchase bill")

@app.post("/purchase/<int:i>/cancel")
def purchase_cancel(i):
    with db() as c:
        v = one("select * from purchases where id=? and cancelled=0", i)
        if not v: raise Oops("Already cancelled")
        if one("select 1 from ppayments where purchase_id=? and cancelled=0", i): raise Oops("Undo its payments first")
        reverse(v["voucher"]); c.execute("update purchases set cancelled=1 where id=?", (i,))
    return redirect(f"/purchase/{i}")

@app.route("/drafts")
def drafts():
    rows = [(r, json.loads(r["data"])) for r in q("select * from drafts where status='new' order by id desc")]
    return page("""<div class=head><h1>Drafts</h1><form method=post action=/drafts/sample><button class=ghost>+ Add sample draft</button></form></div>
<div class=card><p class=mute>Bills read from photos appear here by themselves. Open one, check it against the paper, and save. Nothing reaches the books until you save.</p><table><thead><tr><th>Received<th>Supplier<th>Bill no.<th>Items<th></tr></thead><tbody>
{% for r,d in rows %}<tr><td class=mute>{{r.created}}<td><b>{{d.supplier}}</b><td>{{d.sup_no}}<td>{{d.lines|length}}<td class=r><a class=btn href="/purchase/new?draft={{r.id}}">Check</a>
<form method=post action=/draft/{{r.id}}/dismiss style=display:inline><button class="ghost danger">Remove</button></form></tr>{% else %}<tr><td class=empty colspan=5>No drafts waiting.</tr>{% endfor %}</tbody></table></div>""", rows=rows)

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

# ---------- receipt (money in) and payment (money out) ----------
def money(kind, ptbl, col, ours):
    """Shared screen for Receipt (customers) and Payment (suppliers)."""
    view, label = ("inv_due", "Receipt") if kind == "in" else ("pur_due", "Payment")
    if request.method == "GET":
        ds = q(f"select * from {view} where due>0 and cancelled=0 order by date")
        sel = request.args.get("invoice" if kind == "in" else "purchase", "")
        return page("""<div class=head><h1>{{label}}</h1></div><div class=card><h2>{{'Money received from a customer' if kind=='in' else 'Money paid to a supplier'}}</h2>{% if ds %}<form method=post class=form>
<div><label>Bill</label><select name=bill>{% for d in ds %}<option value={{d.id}} {{'selected' if d.id|string==sel}}>{{d.no}} · {{d.name}} · due ₹{{d.due|rs}}</option>{% endfor %}</select></div>
<div><label>Amount ₹</label><input name=amount type=number step=0.01 required></div><div><label>Date</label><input name=date type=date value={{d}}></div>
<div><label>{{'Received by' if kind=='in' else 'Paid by'}}</label><select name=mode><option>Cash<option>Bank<option>UPI</select></div><button>Save</button></form>{% else %}<p class=empty>No unpaid bills.</p>{% endif %}</div>""",
            ds=ds, sel=sel, d=today(), label=label, kind=kind)
    f = request.form
    with db() as c:
        b = one(f"select * from {view} where id=? and cancelled=0", f["bill"]); amt = paise(f["amount"])
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
{% for r in rec %}<tr><td><a href=/invoice/{{r.id}}>{{r.no}}</a><br><span class=mute>{{r.name}} · {{r.date}}</span><td class=r><b>₹{{r.due|rs}}</b></tr>{% else %}<tr><td class=empty>Nothing pending.</tr>{% endfor %}</tbody></table></div>
<div class=card><h2>To pay suppliers · ₹{{(pay|sum(attribute='due'))|rs}}</h2><table><tbody>
{% for r in pay %}<tr><td><a href=/purchase/{{r.id}}>{{r.no}}</a><br><span class=mute>{{r.name}} · {{r.date}}</span><td class=r><b>₹{{r.due|rs}}</b></tr>{% else %}<tr><td class=empty>Nothing pending.</tr>{% endfor %}</tbody></table></div></div>""",
        rec=q("select * from inv_due where due>0 and cancelled=0 order by date"), pay=q("select * from pur_due where due>0 and cancelled=0 order by date"))

@app.route("/ledger")
def ledger():
    tb = q("select account, sum(dr) dr, sum(cr) cr from ledger group by account order by account")
    return page("""<div class=head><h1>Books</h1></div><div class=card><h2>Trial balance</h2><table><thead><tr><th>Account<th class=r>Debit ₹<th class=r>Credit ₹</tr></thead><tbody>
{% for t in tb %}<tr><td>{{t.account}}<td class=r>{{t.dr|rs}}<td class=r>{{t.cr|rs}}</tr>{% else %}<tr><td class=empty colspan=3>No entries yet.</tr>{% endfor %}
{% if tb %}<tr><td><b>Total</b><td class=r><b>{{tb|sum(attribute='dr')|rs}}</b><td class=r><b>{{tb|sum(attribute='cr')|rs}}</b></tr>{% endif %}</tbody></table></div>
<div class=card><h2>Latest entries</h2><table><thead><tr><th>Entry<th>Date<th>Account<th class=r>Debit<th class=r>Credit</tr></thead><tbody>
{% for e in es %}<tr><td class=mute>#{{e.voucher}}<td class=mute>{{e.date}}<td>{{e.account}}<td class=r>{{e.dr|rs if e.dr}}<td class=r>{{e.cr|rs if e.cr}}</tr>{% endfor %}</tbody></table></div>""",
        tb=tb, es=q("select * from ledger order by id desc limit 60"))

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

if __name__ == "__main__":
    app.run(port=5050, debug=True)
