"""Reads a supplier bill (PDF or photo) into the draft that fills the purchase form.
Runs only on this computer: pdftotext for PDFs, tesseract for photos. Nothing is sent anywhere.
A row is recognised by arithmetic (qty x rate [- discount] = amount), so it does not depend on one supplier's layout.
Every result is only a draft: totals are cross-checked and any doubt is listed in "warnings"."""
import re, subprocess, tempfile, datetime as dt
from decimal import Decimal as D, ROUND_HALF_UP
from pathlib import Path

GSTIN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
NUM = re.compile(r"(?<![\d.,])\d[\d,]*(?:\.\d+)?")
DATE = re.compile(r"\b(\d{1,2})[-/. ]+(\d{1,2}|[A-Za-z]{3})[-/. ]+(\d{4}|\d{2})\b")
MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
STD = [D(x) for x in ("0", "0.25", "3", "5", "12", "18", "28")]
GENERIC = re.compile(r"^(tax invoice|invoice|proforma invoice|original|original copy|duplicate|gst tax invoice|bill of supply)$", re.I)
num = lambda s: D(s.replace(",", ""))
rnd2 = lambda x: D(x).quantize(D("0.01"), ROUND_HALF_UP)
fmt = lambda d: format(D(d).normalize(), "f") if D(d) != 0 else "0"
run = lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=180).stdout

def gstin_ok(x):
    C = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if len(x) != 15 or any(c not in C for c in x): return False
    s = sum((lambda v: v // 36 + v % 36)(C.index(c) * (1 + i % 2)) for i, c in enumerate(x[:14]))
    return C[(36 - s % 36) % 36] == x[14]

def ocr(img, psm=6):
    """Photo to text. Coloured print (orange, blue) is turned dark and small photos are enlarged first, so tesseract can see them."""
    from PIL import Image, ImageChops, ImageOps
    r, g, b = Image.open(img).convert("RGB").split()
    gray = ImageOps.autocontrast(ImageChops.darker(ImageChops.darker(r, g), b), cutoff=1)
    if gray.width < 2200: gray = gray.resize((2200, int(gray.height * 2200 / gray.width)), Image.LANCZOS)
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        gray.save(f.name); return run(["tesseract", f.name, "stdout", "--psm", str(psm)])

def text_of(path, psm=6):
    p = Path(path); ext = p.suffix.lower()
    if ext == ".pdf":
        t = run(["pdftotext", "-layout", str(p), "-"])
        if len(re.findall(r"[A-Za-z]{3,}", t)) > 25: return t, "pdf"
        with tempfile.TemporaryDirectory() as d:                               # a scanned PDF is only pictures: read them like photos
            run(["pdftoppm", "-r", "200", "-png", str(p), f"{d}/pg"])
            return "\n".join(ocr(f, psm) for f in sorted(Path(d).glob("pg*.png"))), "photo"
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"): return ocr(p, psm), "photo"
    return "", ""

def nums(s): return [num(m.group()) for m in NUM.finditer(s)]

def to_date(m):
    d, mo, y = m.groups(); mo = MONTHS.get(mo[:3].lower()) if not mo.isdigit() else int(mo); y = int(y) + (2000 if len(y) == 2 else 0)
    try: return dt.date(y, mo, int(d)).isoformat()
    except (ValueError, TypeError): return None

def below(lines, i, col):
    """Text sitting under a label (bills often put the value on the next line, same column)."""
    for l in lines[i + 1:i + 4]:
        seg = l[max(col - 2, 0):].strip()
        if seg: return seg
    return ""

def field(lines, label):
    """Value of a labelled field: on the same line after the label, or just underneath it."""
    for i, l in enumerate(lines):
        m = re.search(label, l)
        if not m: continue
        v = re.match(r"[\s:|\-]*([A-Za-z0-9]+(?:\s*[/\-]\s*[A-Za-z0-9]+)*)", l[m.end():])
        if v and not re.match(r"(?i)(dated?|date|transport|delivery|e-?way|buyer|station|vehicle|mode|other|challan|ref)", v.group(1)): return v.group(1).strip()
        b = below(lines, i, m.start())
        if b: return b.split("  ")[0].strip()
    return ""

def solve(N):
    """Find (qty, rate, discount, amount) among the numbers of a row so that qty x rate x (1 - disc) = amount."""
    if len(N) > 1 and N[0] == 0: return D(1), N[1], None, N[1]                 # "delivery charges" written with qty 0
    n = len(N)
    for i in range(n):
        if N[i] <= 0: continue
        for j in range(i + 1, n):
            for k in [None] + [k for k in range(j + 1, n) if 0 < N[k] < 100]:
                f = 1 if k is None else 1 - N[k] / 100
                for a in range(j + 1, n):
                    if a != k and abs(N[i] * N[j] * f - N[a]) <= max(D("0.05"), N[a] * D("0.0005")): return N[i], N[j], (None if k is None else N[k]), N[a]
    return None

def parse_row(l):
    m = re.match(r"^\s*(\d{1,3})\.?\s+(\S.*)$", l)
    if not m: return None
    rest = m.group(2); cand = None
    for ln in (8, 6, 4):                                                       # HSN (8) / SAC (6) / short HSN (4): standalone digits followed by a space
        c = re.search(r"(?<![\w.,/-])(\d{%d})(?![\w.,/%%-])(?=\s)" % ln, rest)
        if c: cand = c; break
    if not cand: return None
    descr, tail = rest[:cand.start()].strip(), rest[cand.end():]
    pcts = [num(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", re.sub(r"\d+(?:\.\d+)?(?:\+\d+(?:\.\d+)?)+\s*%", "", tail))]
    tail = re.sub(r"\d+(?:\.\d+)?(?:\+\d+(?:\.\d+)?)*\s*%", " ", tail)
    unit = next((u.rstrip(".") for u in re.findall(r"\b([A-Za-z]{1,8}\.?)(?=\s|$)", tail) if u.lower().rstrip(".") not in ("per", "rs", "inr", "nos20")), "")
    N = nums(tail)
    if not N: return None
    sol = solve(N); approx = sol is None
    if approx: sol = (N[0], N[1] if len(N) > 1 else N[0], None, N[-1])
    q, r, d, a = sol
    taxable = q * r * (1 - (d or D(0)) / 100) if not approx else q * r
    g = next((p for p in pcts if p in STD and p > 0), None)
    if g is None:                                                              # tax columns: "% amount % amount" after the amount
        pos = [x for x in range(len(N)) if N[x] == a][-1:] or [0]
        comp = [N[x] for x in range(pos[0] + 1, len(N) - 1) if N[x] in STD and N[x] > 0 and abs(N[x + 1] - taxable * N[x] / 100) <= D("0.03")]
        g = sum(comp) if comp else None
    return dict(descr=descr, hsn=cand.group(1), unit=unit, qty=q, rate=r, disc=d, gst=g, taxable=rnd2(taxable), approx=approx)

def charge_row(l):
    m = re.match(r"^\s*([A-Za-z][A-Za-z .\-/]{2,40}?)\s{2,}(\d{4,6})\s+(\d+(?:\.\d+)?)\s*%\s+([\d,]+\.\d+)\s*$", l)
    if m: return dict(descr=m.group(1).strip(), hsn=m.group(2), unit="", qty=D(1), rate=num(m.group(4)), disc=None, gst=num(m.group(3)), taxable=num(m.group(4)), approx=False)

def read(path, ours="", psm=6, ours_name=""):
    text, how = text_of(path, psm)
    if not text.strip(): return None
    if how == "photo": text = re.sub(r"[|\[\]]", " ", text)                    # table rules and stray brackets that OCR reads as characters
    lines = [l.rstrip() for l in text.splitlines()]; flat = "\n".join(lines); warn = []
    gst = GSTIN.findall(flat); sup_g = next((g for g in gst if g != ours), "")
    # ---- header ----
    foot = [m.group(1).strip(" ,.") for l in lines for m in [re.match(r"^.*?\b[Ff][Oo][Rr],?\s+([A-Z][A-Za-z0-9 .&'-]{3,45}?)\s*$", l)] if m and not re.search(r"(?i)stamp|signator", m.group(1))]
    name = (foot[-1] if foot else next((l.strip() for l in lines[:12] if l.strip() and not GENERIC.match(l.strip()) and len(l.strip()) > 3), "")).title() if (foot or lines) else ""
    mine = ours_name.split()[0].lower() if ours_name.strip() else ""             # the "For <name>" signature says who issued the bill
    if (foot and mine and mine in foot[-1].lower()) or (not foot and ours and gst and gst[0] == ours and any(g != ours for g in gst)): return {"is_ours": True}
    no = re.sub(r"\s*([/\-])\s*", r"\1", field(lines, r"(?:Invoice|Proforma|Bill|Voucher|Tax Invoice)\s*No\.?"))
    d = None
    for i, l in enumerate(lines):
        m = re.search(r"(?:Invoice\s+)?Dated?\b\s*[:\-]*", l)
        if m:
            cand = DATE.search(l[m.end():]) or DATE.search(below(lines, i, m.start())); d = to_date(cand) if cand else None
            if d: break
    if not d: d = next((to_date(m) for m in DATE.finditer("\n".join(lines[:40])) if to_date(m)), None)
    doctype = "proforma" if re.search(r"(?i)proforma", "\n".join(lines[:14])) else "tax"
    # ---- totals ----
    def sum_of(word):
        return sum((nums(l)[-1] for l in lines if re.search(word, l, re.I) and nums(l) and not re.search(r"(?i)\bamt\b|rate\b.*taxable", l)), D(0))
    cg, sg, ig = sum_of(r"\bc\.?gst\b"), sum_of(r"\bs\.?gst\b(?!/)"), sum_of(r"\bi\.?gst\b")
    ro = D(0)
    for l in lines:
        m = re.search(r"(?i)round(?:ed|ing)?\s*off[^0-9\-(]*([-(]?\s*[\d,]+\.\d+)", l)
        if m: v = num(re.sub(r"[-()\s]", "", m.group(1))); ro = -v if re.search(r"[-(]", m.group(1)) else v
    strong = r"(?i)grand\s*total|invoice\s*amt|total\s*rs|net\s*payable|amount\s*payable"
    tot = [max(nums(l)) if re.search(strong, l) else nums(l)[-1] for l in lines if nums(l) and re.search(strong + r"|^\s*total\b|\btotal\b.*[₹`]", l)]
    grand = max((t for t in tot if t < D(10) ** 9), default=None)
    if not (cg or sg or ig):                                                   # tax columns without labels: find a row where taxable + cgst + sgst = total
        for l in reversed(lines):
            body = re.sub(r"(?i)^\s*total:?", "", l); v = nums(body)
            if len(v) >= 4 and not re.search(r"[A-Za-z]{3,}", body):           # a totals row: taxable, cgst, sgst, then total (or total tax)
                hit = next(((v[y], v[y + 1]) for y in range(1, len(v) - 1) if v[y] == v[y + 1] and (abs(v[y - 1] + 2 * v[y] - v[-1]) <= D("0.03") or abs(2 * v[y] - v[-1]) <= D("0.03"))), None)
                if hit: cg, sg = hit; break
    # ---- item rows ----
    rows, i = [], 0
    while i < len(lines):
        r = parse_row(lines[i]) or charge_row(lines[i])
        if r:
            j = i + 1
            while j < len(lines) and j < i + 6 and lines[j].strip() and not re.match(r"^\s*\d{1,3}\.?\s+\S", lines[j]) and not charge_row(lines[j]) \
                    and not re.search(r"(?i)cgst|sgst|igst|round|total|freight|amount|grand", lines[j]) and len(re.findall(r"[A-Za-z]", lines[j])) >= 2 and len(re.findall(r"\d+\.\d{2}", lines[j])) < 2:
                r["descr"] += " " + lines[j].strip(); j += 1
            rows.append(r); i = j
        else: i += 1
    if not rows: warn.append("Could not find the item table. Please type the items from the bill.")
    taxsum = cg + sg + ig
    taxable_p = (grand - taxsum - ro) if grand is not None and taxsum else None
    rate_doc = None
    if taxable_p and taxable_p > 0 and taxsum:
        eff = taxsum / taxable_p * 100; rate_doc = min(STD, key=lambda s: abs(s - eff))
    for r in rows:
        if r["gst"] is None: r["gst"] = rate_doc if rate_doc is not None else D(18); r["guess"] = rate_doc is None
    # ---- self check ----
    if rows and grand is not None:
        items_tax = sum((rnd2(r["taxable"] * r["gst"] / 100) for r in rows), D(0)); items_exact = sum((r["taxable"] for r in rows), D(0)) + items_tax
        if abs(items_exact - (grand - ro)) > D("1.00"): warn.append(f"The items add up to ₹{items_exact:,.2f} but the bill total is ₹{grand - ro:,.2f} before round-off. Check every row against the paper.")
    if grand is None: warn.append("Could not find the bill total.")
    if any(r.get("approx") for r in rows): warn.append("A row's amount includes tax, so its taxable value is approximate. The bill's own totals are filled in below.")
    if any(r.get("guess") for r in rows): warn.append("GST rate was not printed; 18% assumed. Please check.")
    if sup_g and not gstin_ok(sup_g): warn.append(f"Supplier GSTIN {sup_g} fails the check-digit test. Check the bill.")
    if ours and ours not in gst: warn.append("This bill does not carry our GSTIN. It may not be for us.")
    if how == "photo": warn.append("Read from a photo. Check every figure carefully.")
    if not no: warn.append("Bill number not found.")
    if not d: warn.append("Bill date not found.")
    printed = None
    if taxable_p is not None and taxsum:
        printed = {"t": fmt(taxable_p), "c": fmt(cg), "s": fmt(sg), "i": fmt(ig), "r": fmt(ro), "g": fmt(grand)}
    if how == "photo" and not rows and psm == 6:                                # second try with another page layout mode
        alt = read(path, ours, 4, ours_name)
        if alt and alt.get("lines"): return alt
    if not rows and grand is None: return None
    return {"supplier": name, "gstin": sup_g, "sup_no": no, "sup_date": d, "doctype": doctype, "incl": 0, "printed": printed, "warnings": warn, "how": how,
            "lines": [{"descr": r["descr"], "hsn": r["hsn"], "unit": r["unit"], "qty": fmt(r["qty"]), "rate": fmt(r["rate"]), "disc": fmt(r["disc"]) if r["disc"] else "", "gst": fmt(r["gst"])} for r in rows]}

if __name__ == "__main__":
    import sys, json
    print(json.dumps(read(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "", ours_name=sys.argv[3] if len(sys.argv) > 3 else ""), indent=1, ensure_ascii=False))
