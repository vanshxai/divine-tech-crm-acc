"""Typed command -> draft sales bill. No AI: a plain, predictable format that works offline.

    bill Bhoomi Patel                      or on one line:  bill Bhoomi Patel; Camera 4 x 2500; Labour 500
    2MP Camera 4 x 2500
    Labour 1 x 500 gst 12

Item lines:  <item> <qty> x <rate>  |  <item> <qty> <rate>  |  <item> <rate>  |  <item> <qty>   (rate from the Item Master)
Options at the end of a line:  gst 12   disc 10   (disc can be 10+5). A line starting "note:" becomes the narration."""
import re, difflib

PREFIX = re.compile(r"^\s*(?:bill|invoice)(?:\s+(?:to|for))?\s*[:\-]?\s*", re.I)
OPT = re.compile(r"\b(gst|tax|disc|discount)\s*[:=]?\s*(\d+(?:\.\d+)?(?:\+\d+(?:\.\d+)?)*)\s*%?", re.I)
N = r"(\d+(?:\.\d+)?)"
FORMS = (re.compile(r"^(.+?)[\s,]*" + N + r"\s*[xX*@]\s*" + N + r"\s*$"), re.compile(r"^(.+?)[\s,]+" + N + r"[\s,]+" + N + r"\s*$"), re.compile(r"^(.+?)[\s,]+" + N + r"\s*$"))

def match_name(name, names):
    """Best match of a typed name among known names: exact, then all words present, then close spelling."""
    n = name.lower().strip(); low = {x.lower(): x for x in names}
    if n in low: return low[n]
    words = [w for w in re.split(r"\W+", n) if w]
    both = [x for x in names if words and all(w in x.lower() for w in words)]
    if both: return min(both, key=len)
    close = difflib.get_close_matches(n, list(low), n=1, cutoff=0.72)
    return low[close[0]] if close else None

def parse_quick(text, parties, items):
    """parties: [(id, name)]; items: {name: {hsn, unit, gst, sale}}. Returns the draft dict used by the Sales voucher."""
    segs = [s.strip() for s in re.split(r"[\n;]+", text) if s.strip()]
    out = {"kind": "sale", "customer": "", "party": None, "narration": "", "lines": [], "warnings": []}; typed = ""
    for k, seg in enumerate(segs):
        if k == 0: seg = PREFIX.sub("", seg)
        if re.match(r"(?i)^(note|narration)\s*:", seg): out["narration"] = seg.split(":", 1)[1].strip(); continue
        opts = {m.group(1).lower(): m.group(2) for m in OPT.finditer(seg)}; seg = OPT.sub("", seg).strip(" ,")
        hit = next((m for m in (f.match(seg) for f in FORMS) if m), None)
        if not hit:
            if not typed and not out["lines"]: typed = seg; continue
            out["warnings"].append(f"Could not read the line: {seg}"); continue
        g = hit.groups(); d = g[0].strip(" ,"); q = r = None
        if len(g) == 3: q, r = g[1], g[2]
        mname = match_name(d, list(items))
        if len(g) == 2:                                                        # one number: the rate, or the quantity when the Item Master has a price
            if mname and items[mname].get("sale") not in (None, "", "0"): q = g[1]
            else: q, r = "1", g[1]
        m = items.get(mname) if mname else None
        if not mname: out["warnings"].append(f"'{d}' is not in the Item Master (typed name used).")
        if r is None: r = (m or {}).get("sale") or ""
        if not r or r == "0": out["warnings"].append(f"No rate for '{d}'. Please fill it in.")
        out["lines"].append({"descr": mname or d, "hsn": (m or {}).get("hsn") or "", "unit": (m or {}).get("unit") or "", "qty": q, "rate": r,
                             "disc": opts.get("disc") or opts.get("discount") or "", "gst": opts.get("gst") or opts.get("tax") or (m or {}).get("gst") or "18"})
    pn = match_name(typed, [p[1] for p in parties]) if typed else None
    out["customer"] = pn or typed
    out["party"] = next((p[0] for p in parties if p[1] == pn), None)
    if typed and not pn: out["warnings"].insert(0, f"Customer '{typed}' was not found in Parties. Choose one below.")
    if not typed: out["warnings"].insert(0, "No customer name given. Choose one below.")
    return out
