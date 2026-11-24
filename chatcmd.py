"""Command chat: a panel on the right where every action is a click or a short line.
Look-ups are instant. Anything that changes the books shows a summary and waits for Confirm.
It reuses the app's own checks (post, reverse, item_vals, gstin_ok), so it cannot do what the forms would refuse."""
import re, json, secrets, datetime as dt, difflib
from decimal import Decimal as D
from flask import request, session, jsonify
import quick

MENU = [("Look up", "Who owes whom?", "કોણે કોને આપવાના?", "send", "due"), ("Look up", "Sales and purchases", "વેચાણ અને ખરીદી", "send", "sales"),
        ("Look up", "Find something", "શોધો", "fill", "find "), ("Look up", "Stock of an item", "માલનો સ્ટોક", "fill", "stock "),
        ("Add", "New bill", "નવું બિલ", "send", "bill"), ("Add", "Add a party", "પાર્ટી ઉમેરો", "send", "add party"), ("Add", "Add an item", "વસ્તુ ઉમેરો", "send", "add item"), ("Add", "Add a job", "કામ ઉમેરો", "send", "add job"),
        ("Money", "Money received", "પૈસા મળ્યા", "send", "receipt"), ("Money", "Pay a supplier", "સપ્લાયરને ચૂકવો", "send", "pay"),
        ("Remove", "Cancel a bill", "બિલ રદ કરો", "send", "cancel bill"), ("Remove", "Remove a draft", "ડ્રાફ્ટ કાઢો", "send", "remove draft"),
        ("Remove", "Remove a party", "પાર્ટી કાઢો", "send", "remove party"), ("Remove", "Remove an item", "વસ્તુ કાઢો", "send", "remove item")]
CATALOG = ["due", "due <party>", "sales", "stock <item>", "find <text>", "open <bill number>", "drafts", "bill <customer>; <item> 4 x 2500; <item> 500", "add party <name> supplier phone 98…",
           "add item <name> price 2500 gst 18 unit Pcs", "add job <customer>: <work> on 30-11-2026", "receipt 5000 from <customer> cash", "pay 5000 to <supplier> bank",
           "cancel bill <bill number>", "remove draft <number>", "remove party <name>", "remove item <name>", "help"]
UNITS = ["Pcs", "Mtr", "Bdl", "Roll", "Set", "Box", "Kg", "Ltr"]
FLOWS = {
    "add party": [("name", "What is the party's name?", "text"), ("kind", "Is it a customer or a supplier?", ["customer", "supplier", "both"]), ("phone", "Phone number? (or Skip)", "opt"), ("gstin", "GSTIN? (or Skip)", "gstin")],
    "add item": [("name", "What is the item's name?", "text"), ("unit", "Which unit?", "unit"), ("sale", "Selling price per unit? (or Skip)", "opt"), ("gst", "GST %?", "gst"), ("hsn", "HSN code? (or Skip)", "opt")],
    "add job": [("party", "For which customer?", "party:customer,both"), ("title", "What is the work?", "text"), ("followup", "Follow up on which date? (or Skip)", "date")],
    "receipt": [("party", "Money received from which customer?", "party:customer,both"), ("amount", "How much (₹)?", "amount"), ("mode", "How was it paid?", ["Cash", "Bank", "UPI"])],
    "pay": [("party", "Which supplier is being paid?", "party:supplier,both"), ("amount", "How much (₹)?", "amount"), ("mode", "How is it paid?", ["Cash", "Bank", "UPI"])],
    "bill": [("party", "Bill for which customer?", "party:customer,both"), ("items", "List the items, like: Camera 4 x 2500; Labour 500", "text")],
    "cancel bill": [("bill", "Which bill number?", "bill")], "remove draft": [("draft", "Which draft number?", "draft")],
    "remove party": [("party", "Which party?", "party:customer,supplier,both")], "remove item": [("item", "Which item?", "item")]}

def register(app, H):
    db, q, one, post, reverse, Oops = H["db"], H["q"], H["one"], H["post"], H["reverse"], H["Oops"]
    rs = lambda p: f"₹{D(p) / 100:,.2f}"
    chip = lambda label, send=None, fill=None, gu="": {"label": label, "gu": gu, "send": send, "fill": fill}
    def out(say=None, card=None, chips=None, link=None, confirm=None): return {"say": say or [], "card": card, "chips": chips or [], "link": link, "confirm": confirm}
    menu_chips = lambda: [dict(chip(l, s if k == "send" else None, s if k == "fill" else None, g), group=grp) for grp, l, g, k, s in MENU]
    cancel = chip("Cancel", "cancel")

    # ---------- finding things by typed name ----------
    def pick_party(raw, kinds):
        if raw.startswith("#") and raw[1:].isdigit():
            r = one("select id,name from clients where id=?", int(raw[1:])); return (dict(r), None) if r else (None, out(["I could not find that party."]))
        rows = q("select id,name from clients where kind in (%s)" % ",".join("?" * len(kinds)), *kinds); n = raw.lower().strip(); words = [w for w in re.split(r"\W+", n) if w]
        exact = [r for r in rows if r["name"].lower() == n] or [r for r in rows if words and all(w in r["name"].lower() for w in words)]
        if len(exact) == 1: return dict(exact[0]), None
        near = exact or [r for r in rows if r["name"].lower() in difflib.get_close_matches(n, [x["name"].lower() for x in rows], n=5, cutoff=0.55)]
        if not near: return None, out([f"I could not find a party called “{raw}”. Type another name, or add it first."], chips=[chip("Add a party", "add party"), cancel])
        return None, out(["Which one?"], chips=[chip(r["name"], f"#{r['id']}") for r in near[:6]] + [cancel])
    def pick_bill(raw):
        raw = raw.strip().lstrip("#")
        r = one("select i.id, i.no, i.total, i.cancelled, c.name, 'invoice' kind from invoices i join clients c on c.id=i.client_id where lower(i.no)=lower(?)", raw) \
            or one("select p.id, p.no, p.total, p.cancelled, c.name, 'purchase' kind from purchases p join clients c on c.id=p.party_id where lower(p.no)=lower(?) or lower(p.sup_no)=lower(?)", raw, raw)
        if r: return dict(r), None
        recent = q("select no from invoices order by id desc limit 4"); return None, out([f"No bill with the number “{raw}”."], chips=[chip(x["no"], x["no"]) for x in recent] + [cancel])

    # ---------- turning an answer into a value ----------
    def resolve(spec, raw):
        if isinstance(spec, list):
            m = next((c for c in spec if c.lower() == raw.strip().lower()), None)
            return (True, m) if m else (False, out(["Please pick one of these."], chips=[chip(c, c) for c in spec] + [cancel]))
        if spec == "gstin":
            g = raw.strip().upper()
            if g in ("", "SKIP"): return True, ""
            return (True, g) if H["gstin_ok"](g) else (False, out(["That GSTIN is not valid. Check every letter and digit, or Skip."], chips=[chip("Skip", "skip"), cancel]))
        if spec == "unit": return (True, H["unit_norm"](raw)) if re.fullmatch(r"[A-Za-z]{1,8}\.?", raw.strip()) else (False, out(["Please type a unit, like Pcs or Mtr."], chips=[chip(c, c) for c in UNITS] + [cancel]))
        if spec == "gst":
            try: ok = 0 <= D(raw.strip().rstrip("%")) <= 28
            except Exception: ok = False
            return (True, raw.strip().rstrip("%")) if ok else (False, out(["GST should be a number from 0 to 28."], chips=[chip(c, c) for c in ("18", "12", "5", "0")] + [cancel]))
        if spec == "text": return (True, raw.strip()) if raw.strip() else (False, out(["Please type something."], chips=[cancel]))
        if spec == "opt": return True, "" if raw.strip().lower() == "skip" else raw.strip()
        if spec == "amount":
            try: v = D(re.sub(r"[₹,\s]|rs\.?", "", raw, flags=re.I))
            except Exception: v = D(0)
            return (True, int(v * 100)) if v > 0 else (False, out(["Please type the amount as a number, like 5000."], chips=[cancel]))
        if spec == "date":
            s = raw.strip().lower()
            if s in ("", "skip"): return True, ""
            if s in ("today", "tomorrow"): return True, str(dt.date.today() + dt.timedelta(days=s == "tomorrow"))
            for f in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
                try: return True, dt.datetime.strptime(s, f).date().isoformat()
                except ValueError: pass
            return False, out(["I could not read that date. Use 30-11-2026, or say today or tomorrow."], chips=[chip("Skip", "skip"), cancel])
        if spec.startswith("party:"): p, r = pick_party(raw, spec[6:].split(",")); return (True, p) if p else (False, r)
        if spec == "bill": b, r = pick_bill(raw); return (True, b) if b else (False, r)
        if spec == "draft":
            r = one("select id, data from drafts where id=? and status='new'", int(raw)) if raw.strip().isdigit() else None
            return (True, {"id": r["id"], "name": json.loads(r["data"]).get("supplier") or json.loads(r["data"]).get("customer") or "draft"}) if r else (False, out(["No waiting draft has that number."], chips=[chip("Open Drafts", "drafts"), cancel]))
        if spec == "item":
            names = [r["name"] for r in q("select name from items")]; m = quick.match_name(raw, names)
            return (True, {"name": m}) if m else (False, out([f"I could not find an item called “{raw}”."], chips=[cancel]))

    def ask(key, prompt, spec):
        chips = [chip(c, c) for c in spec] if isinstance(spec, list) else [chip("Skip", "skip")] if spec in ("opt", "date", "gstin") else [chip(c, c) for c in UNITS] if spec == "unit" else [chip(c, c) for c in ("18", "12", "5", "0")] if spec == "gst" else []
        if spec == "bill": chips = [chip(r["no"], r["no"]) for r in q("select no from invoices order by id desc limit 4")]
        if spec == "date": chips += [chip("Today", "today"), chip("Tomorrow", "tomorrow")]
        return out([prompt], chips=chips + [cancel])

    def fill(cmd, vals):
        def hold(key): session["flow"] = {"cmd": cmd, "vals": vals, "ask": key}
        for key, prompt, spec in FLOWS[cmd]:                                   # check everything already given, in any order, so mistakes show at once
            if key in vals and isinstance(vals[key], str):
                ok, res = resolve(spec, vals[key])
                if not ok: vals.pop(key); hold(key); return res
                vals[key] = res
        if cmd in ("receipt", "pay") and "party" in vals and "amount" in vals:
            got, left, total = alloc(vals["party"]["id"], vals["amount"], cmd)
            if not got: session.pop("flow", None); return out([f"{vals['party']['name']} has no unpaid bills."], chips=menu_chips())
            if left: vals.pop("amount"); hold("amount"); return out([f"That is {rs(left)} more than the unpaid bills ({rs(total)}). Enter an amount up to {rs(total)}."], chips=[cancel])
        for key, prompt, spec in FLOWS[cmd]:
            if key not in vals: hold(key); return ask(key, prompt, spec)
        session.pop("flow", None); return finish(cmd, vals)

    # ---------- plans (what will happen) and executors (doing it) ----------
    def alloc(pid, amount, kind):
        view, col = ("inv_due", "client_id") if kind == "receipt" else ("pur_due", "party_id")
        bills = q(f"select * from {view} where {col}=? and due>0 and cancelled=0 and doctype='tax' order by date, id", pid); left, got = amount, []
        for b in bills:
            if left <= 0: break
            x = min(left, b["due"]); got.append((b, x)); left -= x
        return got, left, sum(b["due"] for b in bills)

    def plan(cmd, v):
        if cmd == "add party":
            g = (v.get("gstin") or "").upper()
            if g and not H["gstin_ok"](g): raise Oops("That GSTIN is not valid. Check every letter and digit.")
            if one("select 1 from clients where lower(name)=lower(?)", v["name"]): raise Oops(f"A party named {v['name']} already exists.")
            return "Add this party?", [["Name", v["name"]], ["Type", v["kind"]], ["Phone", v.get("phone") or "—"], ["GSTIN", g or "—"]]
        if cmd == "add item":
            H["item_vals"](item_form(v)); return "Add this item?", [["Name", v["name"]], ["Unit", v["unit"]], ["Selling price", v.get("sale") or "—"], ["GST %", v["gst"]], ["HSN", v.get("hsn") or "—"]]
        if cmd == "add job": return "Add this job?", [["Customer", v["party"]["name"]], ["Work", v["title"]], ["Follow up", v.get("followup") or "—"]]
        if cmd in ("receipt", "pay"):
            got, left, total = alloc(v["party"]["id"], v["amount"], cmd)
            if not got: raise Oops(f"{v['party']['name']} has no unpaid bills.")
            if left: raise Oops(f"That is {rs(left)} more than the unpaid bills ({rs(total)}). Enter an amount up to {rs(total)}.")
            return ("Record this receipt?" if cmd == "receipt" else "Record this payment?"), [[b["no"] + (" (clears it)" if x == b["due"] else " (part payment)"), rs(x)] for b, x in got] + [["Total", rs(v["amount"])], ["Party", v["party"]["name"]], ["Paid by", v["mode"]]]
        if cmd == "cancel bill":
            b = v["bill"]; tbl, col = ("payments", "invoice_id") if b["kind"] == "invoice" else ("ppayments", "purchase_id")
            if b["cancelled"]: raise Oops("That bill is already cancelled.")
            if one(f"select 1 from {tbl} where {col}=? and cancelled=0", b["id"]): raise Oops("That bill has payments. Undo them first.")
            return "Cancel this bill?", [["Bill", b["no"]], ["Party", b["name"]], ["Total", rs(b["total"])], ["What happens", "A reversing entry is posted. The bill stays visible as Cancelled."]]
        if cmd == "remove draft": return "Remove this draft?", [["Draft", f"#{v['draft']['id']} {v['draft']['name']}"], ["What happens", "It is taken off the list. The original file stays in the Inbox."]]
        if cmd == "remove party":
            p = v["party"]; used = one("select (select count(*) from ledger where client_id=?) + (select count(*) from invoices where client_id=?) + (select count(*) from purchases where party_id=?) + (select count(*) from jobs where client_id=?)", p["id"], p["id"], p["id"], p["id"])[0]
            if used: raise Oops(f"{p['name']} already has bills, jobs or entries, so it cannot be removed. Accounting keeps its history.")
            return "Remove this party?", [["Party", p["name"]], ["What happens", "It is deleted. It has no history."]]
        if cmd == "remove item":
            i = one("select id from items where name=?", v["item"]["name"])["id"]
            if one("select (select count(*) from lines where item_id=?) + (select count(*) from plines where item_id=?)", i, i)[0]: raise Oops(f"{v['item']['name']} is on bills already, so it cannot be removed. Accounting keeps its history.")
            return "Remove this item?", [["Item", v["item"]["name"]], ["What happens", "It is deleted from the Item Master."]]

    def item_form(v): return {"name": v["name"], "unit": v["unit"], "hsn": v.get("hsn") or "", "gst": v["gst"], "sale": v.get("sale") or "", "purch": "", "opening": v.get("opening") or "0"}

    def execute(cmd, v):
        with db() as c:
            if cmd == "add party":
                c.execute("insert into clients(name,phone,gstin,state,address,kind) values(?,?,?,?,?,?)", (v["name"], v.get("phone") or "", (v.get("gstin") or "").upper(), H["OWN_STATE"], "", v["kind"])); return f"Added {v['name']}.", "/clients"
            if cmd == "add item": c.execute("insert into items(name,unit,hsn,gst,sale,purch,opening) values(?,?,?,?,?,?,?)", H["item_vals"](item_form(v))); return f"Added item {v['name']}.", "/items"
            if cmd == "add job": c.execute("insert into jobs(client_id,title,followup) values(?,?,?)", (v["party"]["id"], v["title"], v.get("followup") or None)); return "Job added.", "/jobs"
            if cmd in ("receipt", "pay"):
                got, left, _ = alloc(v["party"]["id"], v["amount"], cmd)
                if left or not got: raise Oops("The amounts have changed. Please start again.")
                acct, day = ("Cash" if v["mode"] == "Cash" else "Bank"), H["today"]()
                for b, x in got:
                    if cmd == "receipt":
                        vch = post(day, [(acct, None, x, 0), ("Debtors", b["client_id"], 0, x)]); c.execute("insert into payments(date,invoice_id,amount,mode,voucher) values(?,?,?,?,?)", (day, b["id"], x, v["mode"], vch))
                    else:
                        vch = post(day, [("Creditors", b["party_id"], x, 0), (acct, None, 0, x)]); c.execute("insert into ppayments(date,purchase_id,amount,mode,voucher) values(?,?,?,?,?)", (day, b["id"], x, v["mode"], vch))
                return f"Recorded {rs(v['amount'])} {'from' if cmd == 'receipt' else 'to'} {v['party']['name']} across {len(got)} bill(s).", f"/client/{v['party']['id']}"
            if cmd == "cancel bill":
                b = v["bill"]; tbl = "invoices" if b["kind"] == "invoice" else "purchases"; row = one(f"select * from {tbl} where id=? and cancelled=0", b["id"])
                if not row: raise Oops("That bill is already cancelled.")
                plan(cmd, v)
                if row["voucher"]: reverse(row["voucher"])
                c.execute(f"update {tbl} set cancelled=1 where id=?", (b["id"],)); return f"Cancelled {b['no']}.", f"/{b['kind']}/{b['id']}"
            if cmd == "remove draft": c.execute("update drafts set status='dismissed' where id=? and status='new'", (v["draft"]["id"],)); return "Draft removed.", "/drafts"
            if cmd == "remove party": plan(cmd, v); c.execute("delete from clients where id=?", (v["party"]["id"],)); return f"Removed {v['party']['name']}.", "/clients"
            if cmd == "remove item": plan(cmd, v); c.execute("delete from items where name=?", (v["item"]["name"],)); return f"Removed {v['item']['name']}.", "/items"

    def finish(cmd, v):
        if cmd == "bill":
            res = quick.parse_quick(v["party"]["name"] + "; " + v["items"], H["intake"].parties(db()), H["intake"].items(db()))
            if not res["lines"]: return out(["I could not read the items. Write them like: Camera 4 x 2500; Labour 500"], chips=[chip("Try again", "bill"), cancel])
            with db() as c: did = H["intake"].add_draft(c, res)
            return out(["Bill draft ready. Open it to check, save and print."] + ["⚠ " + w for w in res["warnings"]], card={"title": f"Bill for {res['customer']}", "rows": [[l["descr"], f"{l['qty']} × {l['rate']}"] for l in res["lines"]]}, link={"label": "Open the bill", "url": f"/invoice/new?draft={did}"})
        try: title, rows = plan(cmd, v)
        except Oops as e: return out([str(e)], chips=[chip("Start again", cmd), cancel])
        token = secrets.token_hex(8)
        with db() as c: c.execute("insert into chat_pending values(?,?,?)", (token, json.dumps({"cmd": cmd, "vals": v}), dt.datetime.now().isoformat()))
        return out(card={"title": title, "rows": rows}, confirm=token)

    # ---------- look-ups ----------
    def r_help(): return out(["What do you want to do? Click one, or type a command. Examples:"] + ["• " + c for c in CATALOG[:12]], chips=menu_chips())
    def r_due(arg):
        if arg:
            p, r = pick_party(arg, ["customer", "supplier", "both"])
            if not p: return r
            rec = q("select no, due from inv_due where client_id=? and due>0 and cancelled=0 order by date", p["id"]); pay = q("select no, due from pur_due where party_id=? and due>0 and cancelled=0 and doctype='tax' order by date", p["id"])
            rows = [[f"They owe us · {b['no']}", rs(b["due"])] for b in rec] + [[f"We owe them · {b['no']}", rs(b["due"])] for b in pay]
            return out(card={"title": p["name"], "rows": rows or [["Nothing pending", ""]]}, chips=[chip("Money received", f"receipt from {p['name']}"), chip("Pay this party", f"pay to {p['name']}"), chip("New bill", "bill")])
        rec = q("select c.name, sum(l.dr-l.cr) b from ledger l join clients c on c.id=l.client_id where l.account='Debtors' group by c.id having b>0 order by b desc limit 8")
        pay = q("select c.name, sum(l.cr-l.dr) b from ledger l join clients c on c.id=l.client_id where l.account='Creditors' group by c.id having b>0 order by b desc limit 8")
        rows = [["Customers owe us", rs(sum(r["b"] for r in rec))]] + [["   " + r["name"], rs(r["b"])] for r in rec] + [["We owe suppliers", rs(sum(r["b"] for r in pay))]] + [["   " + r["name"], rs(r["b"])] for r in pay]
        return out(card={"title": "Outstanding", "rows": rows}, chips=[chip(f"Received from {r['name']}", f"receipt from {r['name']}") for r in rec[:3]] + [chip(f"Pay {r['name']}", f"pay to {r['name']}") for r in pay[:2]])
    def r_sales():
        day, mo = H["today"](), H["today"]()[:7] + "%"
        g = lambda acc, a, b, like, v: one(f"select coalesce(sum({a}-{b}),0) from ledger where account=? and date like ?", acc, v)[0]
        return out(card={"title": "Sales and purchases", "rows": [["Sales today", rs(g("Sales", "cr", "dr", 0, day))], ["Sales this month", rs(g("Sales", "cr", "dr", 0, mo))], ["Purchases this month", rs(g("Purchases", "dr", "cr", 0, mo))]]}, link={"label": "Open the books", "url": "/ledger"})
    def r_stock(arg):
        if not arg: return out(["Which item? Type: stock <item name>"])
        names = [r["name"] for r in q("select name from items")]; m = quick.match_name(arg, names)
        if not m: return out([f"I could not find an item called “{arg}”."])
        r = one(H["STOCK"] + " where i.name=?", m); return out(card={"title": m, "rows": [["In stock", f"{r['stock']:g} {r['unit']}"], ["Selling price", r["sale"] or "—"], ["GST %", r["gst"]], ["HSN", r["hsn"] or "—"]]}, link={"label": "Open Items", "url": "/items"})
    def r_find(arg):
        if not arg: return out(["Type what to find: find <name, phone, GSTIN, item or bill number>"])
        like = f"%{arg}%"; rows = [[f"Party · {r['name']}", r["phone"] or ""] for r in q("select name, phone from clients where name like ? or phone like ? or gstin like ? limit 5", like, like, like)]
        rows += [[f"Item · {r['name']}", r["sale"] or ""] for r in q("select name, sale from items where name like ? or hsn like ? limit 5", like, like)]
        bills = q("select no, total from invoices where no like ? union all select no, total from purchases where no like ? or sup_no like ? limit 5", like, like, like); rows += [[f"Bill · {r['no']}", rs(r["total"])] for r in bills]
        return out(card={"title": f"Found for “{arg}”", "rows": rows or [["Nothing found", ""]]}, chips=[chip(f"Open {r['no']}", f"open {r['no']}") for r in bills[:3]])
    def r_open(arg):
        b, r = pick_bill(arg) if arg else (None, out(["Which bill number?"])); return out([f"{b['no']} · {b['name']} · {rs(b['total'])}"], link={"label": "Open the bill", "url": f"/{b['kind']}/{b['id']}"}) if b else r
    def r_drafts():
        n = one("select count(*) from drafts where status='new'")[0]; return out([f"{n} draft{'s' if n != 1 else ''} waiting for your check."], link={"label": "Open Drafts", "url": "/drafts"})
    def r_quick(text):
        res = quick.parse_quick(text, H["intake"].parties(db()), H["intake"].items(db()))
        if not res["lines"]: return out(["I could not read the items. Write them like: bill Bhoomi Patel; Camera 4 x 2500; Labour 500"])
        with db() as c: did = H["intake"].add_draft(c, res)
        return out(["Bill draft ready. Open it to check, save and print."] + ["⚠ " + w for w in res["warnings"]], card={"title": f"Bill for {res['customer'] or '(choose customer)'}", "rows": [[l["descr"], f"{l['qty']} × {l['rate']}"] for l in res["lines"]]}, link={"label": "Open the bill", "url": f"/invoice/new?draft={did}"})

    # ---------- one-line commands ----------
    def parse_line(s):
        l = s.lower().strip()
        if l in ("help", "?", "menu", "hi", "hello"): return "help", {}
        for w in ("due", "stock", "find", "open", "sales", "drafts"):
            if l == w or l.startswith(w + " "): return w, {"arg": s.strip()[len(w):].strip()}
        if l.startswith("bill ") and (";" in s or "\n" in s): return "quick", {"text": s}
        m = re.match(r"(?i)^(add party|add item|add job|receipt|pay|bill|cancel bill|remove draft|remove party|remove item)\b\s*(.*)$", s.strip())
        if not m: return None
        cmd, rest = m.group(1).lower(), m.group(2).strip(); v = {}
        def grab(key, pat, upper=False):
            nonlocal rest; mm = re.search(pat, rest, re.I)
            if mm: v[key] = mm.group(1).strip().upper() if upper else mm.group(1).strip(); rest = (rest[:mm.start()] + " " + rest[mm.end():]).strip()
        if cmd == "add party":
            grab("phone", r"\bphone\s+([\d+\-\s]{6,})"); grab("gstin", r"\bgstin\s+([0-9A-Za-z]{15})", True); grab("kind", r"\b(customer|supplier|both)\b")
            if "kind" in v: v["kind"] = v["kind"].lower()
            if rest: v["name"] = " ".join(rest.split())
        elif cmd == "add item":
            for key, pat in (("sale", r"\b(?:price|rate)\s+([\d.]+)"), ("gst", r"\bgst\s+([\d.]+)"), ("hsn", r"\bhsn\s+(\d+)"), ("unit", r"\bunit\s+(\w+)"), ("opening", r"\bstock\s+([\d.]+)")): grab(key, pat)
            if "unit" in v: v["unit"] = next((u for u in UNITS if u.lower() == v["unit"].lower()), v["unit"])
            if rest: v["name"] = " ".join(rest.split())
        elif cmd == "add job":
            grab("followup", r"\bon\s+(\S+)\s*$")
            if ":" in rest: v["party"], v["title"] = (x.strip() for x in rest.split(":", 1))
            elif rest: v["party"] = rest
        elif cmd in ("receipt", "pay"):
            grab("mode", r"\b(cash|bank|upi)\s*$")
            if "mode" in v: v["mode"] = {"cash": "Cash", "bank": "Bank", "upi": "UPI"}[v["mode"].lower()]
            grab("party", r"\b(?:from|to)\s+(.+)$"); grab("amount", r"(?:₹|rs\.?)?\s*(\d[\d,]*(?:\.\d+)?)")
        elif cmd == "bill":
            if rest: v["party"] = rest
        elif cmd == "cancel bill":
            if rest: v["bill"] = rest
        elif rest: v[cmd.split()[1]] = rest
        return cmd, v

    def handle(t):
        t = t.strip(); flow = session.get("flow")
        if t.lower() in ("cancel", "stop") and flow: session.pop("flow"); return out(["Cancelled. What next?"], chips=menu_chips())
        if t.lower() == "menu": return r_help()
        p = parse_line(t)
        if p:
            session.pop("flow", None); cmd, v = p
            return {"help": lambda: r_help(), "due": lambda: r_due(v.get("arg", "")), "sales": lambda: r_sales(), "stock": lambda: r_stock(v.get("arg", "")), "find": lambda: r_find(v.get("arg", "")),
                    "open": lambda: r_open(v.get("arg", "")), "drafts": lambda: r_drafts(), "quick": lambda: r_quick(v["text"])}.get(cmd, lambda: fill(cmd, v))()
        if flow:
            key = flow.get("ask") or next(k for k, _, _ in FLOWS[flow["cmd"]] if k not in flow["vals"]); flow["vals"][key] = t; return fill(flow["cmd"], flow["vals"])
        return out(["I did not understand that. Click one of these, or type help."], chips=menu_chips())

    @app.post("/chat")
    def chat():
        try: return jsonify(handle(request.get_json(force=True).get("text", "")))
        except Oops as e: return jsonify(out([str(e)]))
        except (ArithmeticError, ValueError): session.pop("flow", None); return jsonify(out(["Please check the numbers and dates you typed."], chips=menu_chips()))

    @app.post("/chat/confirm")
    def chat_confirm():
        d = request.get_json(force=True); row = one("select * from chat_pending where token=?", d.get("token", ""))
        if not row: return jsonify(out(["That action has expired. Please start again."], chips=menu_chips()))
        with db() as c: c.execute("delete from chat_pending where token=?", (row["token"],))
        if not d.get("yes"): return jsonify(out(["Okay, nothing was changed."], chips=menu_chips()))
        a = json.loads(row["action"])
        try: msg, url = execute(a["cmd"], a["vals"])
        except Oops as e: return jsonify(out([str(e)], chips=menu_chips()))
        except (ArithmeticError, ValueError): return jsonify(out(["Please check the numbers you typed. Nothing was changed."], chips=menu_chips()))
        return jsonify(out(["✓ " + msg], link={"label": "Open", "url": url}, chips=menu_chips()))

    @app.get("/chat/suggest")
    def chat_suggest():
        s = request.args.get("q", "").strip(); l = s.lower(); res = []
        if not s: return jsonify([])
        res += [{"text": c.split(" <")[0].split(" 4 x")[0] + " ", "hint": c} for c in CATALOG if c.lower().startswith(l) and len(l) < 14][:5]
        m = re.search(r"(?i)^(due|find|stock|open|bill|add job|remove party|remove item|cancel bill|receipt\b.*?\bfrom|pay\b.*?\bto)\s+(.*?)(\w{2,})$", s)
        if m:
            like = f"%{m.group(3)}%"; names = [r["name"] for r in q("select name from clients where name like ? limit 4", like)] + [r["name"] for r in q("select name from items where name like ? limit 3", like)]
            res += [{"text": s[:m.start(3)] + n, "hint": "name"} for n in names]
        return jsonify(res[:8])
