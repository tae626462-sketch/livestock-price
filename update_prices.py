#!/usr/bin/env python3
"""
VET VISTA — กระดานราคาปศุสัตว์ : อัปเดต prices.json อัตโนมัติ (รันโดย GitHub Actions ทุกวัน)

หลักการ
- แต่ละแหล่งข้อมูลแยกกัน ถ้าแหล่งไหนเปิดไม่ได้หรืออ่านไม่ออก จะคงราคาเดิมไว้ ไม่เดาราคา
- ราคาที่ไม่ได้อัปเดตเกิน 10 วัน แอพจะซ่อนเอง
- อ่านหน้าเว็บตรงก่อน ถ้าไม่ได้จะลองผ่าน r.jina.ai (ตัวแปลงหน้าเว็บเป็นข้อความ)
- ผลแต่ละแหล่งบันทึกไว้ใน prices.json → "status" เพื่อดูว่าแหล่งไหนทำงาน
"""
import json, re, sys, time, datetime as dt
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "prices.json"
TODAY = (dt.datetime.utcnow() + dt.timedelta(hours=7)).date()   # เวลาไทย
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36",
      "Accept-Language": "th-TH,th;q=0.9,en;q=0.8"}

TH_MONTHS = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4, "พฤษภาคม": 5, "มิถุนายน": 6,
    "กรกฎาคม": 7, "สิงหาคม": 8, "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
    "ม.ค.": 1, "ก.พ.": 2, "มี.ค.": 3, "เม.ย.": 4, "พ.ค.": 5, "มิ.ย.": 6,
    "ก.ค.": 7, "ส.ค.": 8, "ก.ย.": 9, "ต.ค.": 10, "พ.ย.": 11, "ธ.ค.": 12,
}
MONTH_RE = "|".join(sorted((re.escape(m) for m in TH_MONTHS), key=len, reverse=True))

status = {}


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, flush=True)


def fetch(url, tries=2):
    """คืน (html, text) ; html อาจเป็น None ถ้าได้มาจาก jina"""
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=40)
            if r.ok and len(r.text) > 500:
                r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
                html = r.text
                return html, html_to_text(html)
        except requests.RequestException as e:
            log("  fetch error", url, e)
        time.sleep(2)
    try:
        r = requests.get("https://r.jina.ai/" + url, headers={**UA, "X-Return-Format": "text"}, timeout=60)
        if r.ok and len(r.text) > 200:
            log("  (ผ่าน jina)", url)
            return None, norm(r.text)
    except requests.RequestException as e:
        log("  jina error", url, e)
    return None, ""


def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    return norm(soup.get_text("\n"))


def norm(t):
    t = t.replace("\xa0", " ").replace("​", "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t


def num(s):
    return float(str(s).replace(",", ""))


def th_date(d, m, y):
    y = int(y)
    if y > 2400:
        y -= 543
    return dt.date(y, TH_MONTHS[m], int(d)).isoformat()


def find_th_dates(text):
    out = []
    for m in re.finditer(rf"(\d{{1,2}})\s*({MONTH_RE})\s*(\d{{4}})", text):
        try:
            out.append(th_date(*m.groups()))
        except Exception:
            pass
    return out


def item(data, iid):
    return next((i for i in data["items"] if i["id"] == iid), None)


def set_price(data, iid, price, date, **extra):
    """อัปเดตราคา 1 รายการ คืน True ถ้ามีการเปลี่ยน"""
    it = item(data, iid)
    if it is None or price is None or not date:
        return False
    if it.get("date") and date < it["date"]:
        return False            # ไม่เอาข้อมูลเก่ากว่าที่มี
    hist = [p for p in (it.get("hist") or []) if p[0] != date]
    if it.get("price") is not None and it.get("date") and not hist:
        hist = [[it["date"], it["price"]]] if it["date"] != date else []
    hist.append([date, round(price, 2)])
    it["hist"] = hist[-12:]
    changed = it.get("price") != round(price, 2) or it.get("date") != date
    it["price"] = round(price, 2)
    it["date"] = date
    it.pop("local", None)
    it.update(extra)
    return changed


# ---------------------------------------------------------------- 1) CPF Feed (ลูกสุกร ลูกไก่ วัตถุดิบ) — ประกาศวันจันทร์
def cpffeed_rows(url):
    html, text = fetch(url)
    rows = []   # (name, week, date, price, pm)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        last = ""
        for tr in soup.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            joined = " | ".join(cells)
            dm = re.search(r"(20\d\d-\d\d-\d\d)", joined)
            if not dm:
                continue
            name = cells[0] if cells[0] and not re.fullmatch(r"[\d.,\-\s]*", cells[0]) else last
            last = name or last
            nums = [c for c in cells if re.fullmatch(r"-?[\d,]+(\.\d+)?", c)]
            # ลำดับคอลัมน์: Week | Date | Price | (+/-)
            after = cells[cells.index(next(c for c in cells if dm.group(1) in c)) + 1:]
            vals = [c for c in after if re.fullmatch(r"-?[\d,]+(\.\d+)?", c)]
            if not vals:
                continue
            wk = next((int(c) for c in nums if c.isdigit() and int(c) <= 53), None)
            rows.append((last, wk, dm.group(1), num(vals[0]), num(vals[1]) if len(vals) > 1 else 0.0))
    if not rows and text:
        # สำรอง: อ่านจากข้อความ  "ชื่อ | 42 | 2026-10-12 | 1500.00 | 60.00 | บาท/ตัว"
        last = ""
        for line in text.split("\n"):
            m = re.search(r"^(.*?)\s*\|?\s*(\d{1,2})\s*\|?\s*(20\d\d-\d\d-\d\d)\s*\|?\s*([\d,]+\.\d+)\s*\|?\s*(-?[\d,]+\.\d+)?", line)
            if m:
                nm = m.group(1).strip(" |") or last
                last = nm
                rows.append((nm, int(m.group(2)), m.group(3), num(m.group(4)), num(m.group(5) or 0)))
    return rows


def key(s):
    return re.sub(r"[\s.()]", "", s or "").lower()


def latest(rows, pred):
    cand = [r for r in rows if pred(key(r[0]))]
    return max(cand, key=lambda r: r[2]) if cand else None


def update_cpffeed(data):
    n = 0
    rows = cpffeed_rows("https://www.cpffeed.com/animal-price/")
    picks = {
        "cp_s": lambda k: "ขุนเล็ก" in k and "w" not in k and "prrs" not in k,
        "cp_w": lambda k: "ขุนเล็กw" in k and "หย่านม" in k and "prrs" not in k,
        "chick_b": lambda k: k == "ลูกไก่เนื้อ",
        "chick_l": lambda k: k.startswith("ลูกไก่ไข่"),
    }
    for iid, pred in picks.items():
        r = latest(rows, pred)
        if r:
            extra = {"week": r[1]} if r[1] else {}
            if iid.startswith("cp_"):
                extra["pm"] = r[4]
            n += set_price(data, iid, r[3], r[2], **extra)
    status["cpffeed_animal"] = {"ok": bool(rows), "rows": len(rows), "at": TODAY.isoformat()}

    rows = cpffeed_rows("https://www.cpffeed.com/price-update/")
    picks = {
        "corn": lambda k: k.startswith("ข้าวโพด"),
        "sbm": lambda k: k.startswith("กากถั่วเหลือง"),
        "fm_general": lambda k: k == "ปลาป่น",
        "rice": lambda k: k.startswith("ปลายข้าว"),
        "bran_ext": lambda k: k == "รำสกัด",
        "bran": lambda k: k == "รำขาว",
    }
    for iid, pred in picks.items():
        r = latest(rows, pred)
        if r:
            n += set_price(data, iid, r[3], r[2], **({"week": r[1]} if r[1] else {}))
    status["cpffeed_raw"] = {"ok": bool(rows), "rows": len(rows), "at": TODAY.isoformat()}
    return n


# ---------------------------------------------------------------- 2) สุกรขุนรายภาค (สมาคมผู้เลี้ยงสุกร ผ่าน pasusart.com) — ทุกวันพระ
REGIONS = [
    ("ตะวันตก", r"(?:ภาค)?ตะวันตก"),
    ("ตะวันออก", r"(?:ภาค)?ตะวันออก(?!เฉียง)"),
    ("อีสาน", r"(?:ภาค)?(?:อีสาน|ตะวันออกเฉียงเหนือ)"),
    ("เหนือ", r"(?:ภาค)?(?<!เฉียง)เหนือ"),
    ("ใต้", r"(?:ภาค)?ใต้"),
]
VAL = r"\s*[:：\-–|]*\s*(\d{2,3}(?:\.\d+)?(?:\s*[-–]\s*\d{2,3}(?:\.\d+)?)?|[^\n|]{0,12}?ยืน[^\n|]{0,8}|[^\n|]{0,6}(?:ขึ้น|ลง)\s*\d+\s*บาท)"


def update_pig(data):
    html, text = fetch("https://pasusart.com/")
    url = None
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            tx = a.get_text(" ", strip=True)
            if "สุกรมีชีวิตหน้าฟาร์ม" in tx and re.search(r"\d", tx):
                url = a["href"]
                break
    if not url and text:
        m = re.search(r"\((https?://pasusart\.com/[^)\s]*)\)", text)
        url = m.group(1) if m else None
    if not url:
        status["pig"] = {"ok": False, "why": "ไม่พบโพสต์ราคาสุกร", "at": TODAY.isoformat()}
        return 0
    _, t = fetch(url)
    dates = find_th_dates(t[:3000]) or find_th_dates(t)
    if not dates:
        status["pig"] = {"ok": False, "why": "ไม่พบวันที่", "url": url}
        return 0
    date = max(d for d in dates if d <= TODAY.isoformat()) if any(d <= TODAY.isoformat() for d in dates) else dates[0]
    pig = data["pig"]
    if pig.get("date") and date <= pig["date"]:
        status["pig"] = {"ok": True, "note": "ยังไม่มีรอบใหม่", "latest": date, "at": TODAY.isoformat()}
        return 0
    old = {r["r"]: r["cur"] for r in pig["rows"]}
    st = t.find("สัปดาห์ที่")
    seg = t[st: st + 3000] if st >= 0 else t
    new = {}
    for name, rx in REGIONS:
        m = re.search(rx + VAL, seg)
        if not m:
            continue
        v = m.group(1).strip()
        if re.search(r"\d{2}", v) and "บาท" not in v:
            new[name] = re.sub(r"\s*[-–]\s*", "-", v)
        else:
            prev = old.get(name, "")
            mm = re.search(r"(ขึ้น|ลง)\s*(\d+)", v)
            pr = re.match(r"(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))?$", prev or "")
            if mm and pr:   # "ขึ้น 2 บาท" จากฐานเดิม
                d = float(mm.group(2)) * (1 if mm.group(1) == "ขึ้น" else -1)
                lo = float(pr.group(1)) + d
                hi = float(pr.group(2)) + d if pr.group(2) else None
                new[name] = f"{lo:g}-{hi:g}" if hi else f"{lo:g}"
            else:          # ยืนราคา → ใช้ตัวเลขเดิม
                new[name] = prev if re.search(r"\d", prev or "") else "ยืนราคา"
    if len(new) < 3:
        status["pig"] = {"ok": False, "why": "อ่านราคาได้ไม่ครบ", "url": url, "got": new}
        return 0
    wk = re.search(r"สัปดาห์ที่\s*(\d+)\s*/\s*(\d{4})", t)
    for r in pig["rows"]:
        r["prev"] = r["cur"]
        r["cur"] = new.get(r["r"], r["cur"])
    pig["prevDate"], pig["date"] = pig.get("date"), date
    d = dt.date.fromisoformat(date)
    pig["week"] = (f"สัปดาห์ที่ {wk.group(1)}/{wk.group(2)} " if wk else "") + f"(วันพระ {d.day} {list(TH_MONTHS)[d.month + 11]})"
    pig["src"] = "pig_auto"
    pig["url"] = url
    status["pig"] = {"ok": True, "date": date, "url": url, "at": TODAY.isoformat()}
    return 1


# ---------------------------------------------------------------- 3) สรุปภาวะสินค้าเกษตร CPF รายสัปดาห์ (กรุงเทพธุรกิจ) — ไก่เนื้อ ไข่ไก่ ปลาป่นแยกเกรด
def latest_cpf_weekly():
    tag = "https://www.bangkokbiznews.com/tags/%E0%B9%80%E0%B8%88%E0%B8%A3%E0%B8%B4%E0%B8%8D%E0%B9%82%E0%B8%A0%E0%B8%84%E0%B8%A0%E0%B8%B1%E0%B8%93%E0%B8%91%E0%B9%8C%E0%B8%AD%E0%B8%B2%E0%B8%AB%E0%B8%B2%E0%B8%A3"
    html, text = fetch(tag)
    links = re.findall(r"(?:https://www\.bangkokbiznews\.com)?(/business/(?:[a-z\-]+/)*\d{6,8})", (html or "") + text)
    seen, best = [], None
    for l in links:
        if l not in seen:
            seen.append(l)
    for l in sorted(seen, key=lambda x: int(re.search(r"(\d+)$", x).group(1)), reverse=True)[:8]:
        _, t = fetch("https://www.bangkokbiznews.com" + l)
        m = re.search(rf"สรุปภาวะสินค้าเกษตรประจำสัปดาห์\s*วันที่\s*([\d\s\-–]+?)\s*({MONTH_RE})?\s*[\-–]?\s*(\d{{1,2}})?\s*({MONTH_RE})\s*(\d{{4}})", t)
        if m:
            end_day = m.group(3) or re.findall(r"\d+", m.group(1))[-1]
            best = (th_date(end_day, m.group(4), m.group(5)), t, "https://www.bangkokbiznews.com" + l)
            break
    return best


def section(t, name):
    m = re.search(rf"{name}\s*:\s*ราคา", t)
    if not m:
        return ""
    nxt = re.search(r"\n\s*(ข้าวโพด|กากถั่วเหลือง|ปลาป่น|ข้าว|สุกร|ไก่เนื้อ|ไข่ไก่)\s*:\s*ราคา", t[m.end():])
    return t[m.start(): m.end() + (nxt.start() if nxt else 2500)]


def update_cpf_weekly(data):
    got = latest_cpf_weekly()
    if not got:
        status["cpf_weekly"] = {"ok": False, "why": "ไม่พบบทความสรุปรายสัปดาห์", "at": TODAY.isoformat()}
        return 0
    wdate, t, url = got
    n = 0
    s = section(t, "ไก่เนื้อ")
    if s:
        body = s.split("ลูกไก่เนื้อ")[0]
        vals = re.findall(r"กิโลกรัมละ\s*([\d.]+)", body)
        ds = find_th_dates(body)
        if vals:
            n += set_price(data, "broiler", num(vals[-1]), ds[-1] if ds else wdate)
    s = section(t, "ไข่ไก่")
    if s:
        vals = re.findall(r"ฟองละ\s*([\d.]+)", s.split("แนวโน้ม")[0])
        if vals:
            n += set_price(data, "egg", num(vals[-1]), wdate)
    s = section(t, "ปลาป่น")
    if s:
        vals = re.findall(r"กิโลกรัมละ\s*([\d.]+)", s.split("แนวโน้ม")[0])
        if len(vals) == 5:
            for iid, v in zip(["fm_shrimp", "fm1_60", "fm1_lt", "fm2_60", "fm2_56"], vals):
                n += set_price(data, iid, num(v), wdate)
    status["cpf_weekly"] = {"ok": True, "date": wdate, "url": url, "at": TODAY.isoformat()}
    return n


# ---------------------------------------------------------------- 4) มันสำปะหลัง (สมาคมโรงงานผู้ผลิตมันสำปะหลังภาคตะวันออกเฉียงเหนือ)
def update_cassava(data):
    _, t = fetch("https://www.nettathai.org/2012-02-06-06-49-09.html")
    heads = list(re.finditer(rf"ราคารับซื้อหัวมันสด\s*ณ\s*โรงแป้ง\s*จังหวัดนครราชสีมา\s*วันที่\s*(\d{{1,2}})\s*({MONTH_RE})\s*(\d{{4}})", t))
    if not heads:
        status["cassava"] = {"ok": False, "why": "ไม่พบตารางราคา", "at": TODAY.isoformat()}
        return 0
    best = max(heads, key=lambda m: th_date(*m.groups()))
    date = th_date(*best.groups())
    after = t[best.end():]
    nxt = re.search(r"ราคารับซื้อหัวมันสด", after)
    block = after[: nxt.start() if nxt else 3000]
    toks = re.findall(r"(\d\.\d{2})(?:\s*-\s*(\d\.\d{2}))?", block)
    vals = [(float(a), float(b) if b else float(a)) for a, b in toks]
    pct25 = vals[1::2]          # คอลัมน์ 30% , 25% สลับกัน
    n = 0
    if len(pct25) >= 5:
        mids = [(a + b) / 2 for a, b in pct25]
        lo, hi = min(a for a, _ in pct25), max(b for _, b in pct25)
        n += set_price(data, "cassava", sum(mids) / len(mids), date,
                       spec=f"เชื้อแป้ง 25% เฉลี่ย {len(pct25)} อำเภอ จ.นครราชสีมา ({lo:.2f}–{hi:.2f})")
    m = re.search(r"มันเส้น[\s\S]{0,400}?(\d\.\d{2})\s*-\s*(\d\.\d{2})", block)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        n += set_price(data, "chip", (a + b) / 2, date, spec=f"ณ โกดังผู้ส่งออก จ.พระนครศรีอยุธยา ({a:.2f}–{b:.2f})")
    status["cassava"] = {"ok": len(pct25) >= 5, "date": date, "at": TODAY.isoformat()}
    return n


# ---------------------------------------------------------------- 5) ราคาเนื้อสัตว์ แม็คโคร / โลตัส (หน้าเว็บขายออนไลน์)
STORE_URLS = {
    # (กลุ่ม, รายการ): (แม็คโคร, โลตัส)
    ("หมู", "สะโพก"): ("https://www.makro.pro/th/p/831499-800259117952024", "https://www.lotuss.com/th/product/pork-ham-kg-72566043"),
    ("หมู", "ไหล่"): ("https://www.makro.pro/en/p/75608-7606384197827", None),
    ("หมู", "สันนอก"): ("https://www.makro.pro/th/p/3uc97Gi-291270989495295", "https://www.lotuss.com/th/product/pork-loin-72566086"),
    ("หมู", "สันคอ"): ("https://www.makro.pro/p/0qzy-jy-7606384689347", "https://www.lotuss.com/th/product/pork-butt-kg-72566051"),
    ("หมู", "สามชั้น"): ("https://www.makro.pro/th/p/76843-7606380298435", "https://www.lotuss.com/th/product/pork-belly-72566078"),
    ("หมู", "หมูบด"): ("https://www.makro.pro/th/p/824264-7606382592195", "https://www.lotuss.com/th/product/minced-pork-70754896"),
    ("หมู", "สันใน"): (None, "https://www.lotuss.com/th/product/pork-tender-loin-72566108"),
    ("ไก่", "อกไก่ติดหนัง"): ("https://www.makro.pro/en/p/235457-7606382067907", "https://www.lotuss.com/th/product/chicken-breast-5691346"),
    ("ไก่", "สะโพกไก่"): ("https://www.makro.pro/en/p/32955-7606381641923", "https://www.lotuss.com/th/product/chicken-thigh-5691419"),
    ("ไก่", "น่องไก่"): ("https://www.makro.pro/p/jwlwtnj-7606380822723", "https://www.lotuss.com/th/product/chicken-drumstick-5691362"),
    ("ไก่", "น่องติดสะโพกเนื้อล้วน"): ("https://www.makro.pro/en/p/843460-15166591218934", None),
    ("วัว", "เนื้อสะโพกวัว"): (None, "https://www.lotuss.com/th/product/75591303"),
    ("วัว", "เนื้อใบพายวัว"): (None, "https://www.lotuss.com/th/product/75591310"),
    ("ปลา", "ปลานิล"): ("https://www.makro.pro/en/p/146263-187607668532060", None),
    ("ปลา", "ปลาดุกหั่นชิ้น"): ("https://www.makro.pro/th/p/231291-7325028778179", None),
    ("ปลา", "ปลาทับทิม"): (None, "https://www.lotuss.com/th/product/75619331"),
}


def store_price(url):
    html, text = fetch(url, tries=1)
    src = html or text
    if not src:
        return None
    for rx in [r'"price"\s*:\s*"?(\d{2,4}(?:\.\d{1,2})?)"?',
               r'product:price:amount"\s*content="(\d{2,4}(?:\.\d{1,2})?)"',
               r'"(?:sellingPrice|finalPrice|salePrice|regularPrice)"\s*:\s*"?(\d{2,4}(?:\.\d{1,2})?)']:
        m = re.search(rx, src)
        if m and 20 <= float(m.group(1)) <= 2000:
            return float(m.group(1))
    m = re.search(r"฿\s*([\d,]{2,5}(?:\.\d{1,2})?)", text)
    if m and 20 <= num(m.group(1)) <= 2000:
        return num(m.group(1))
    return None


def update_retail(data):
    R = data["retail"]
    rows = {(g["g"], r["cut"]): r for g in R["groups"] for r in g["rows"]}
    ok = fail = 0
    for k, (mk, lt) in STORE_URLS.items():
        r = rows.get(k)
        if not r:
            continue
        for url, f, df, pf in ((mk, "makro", "md", "pmakro"), (lt, "lotus", "ld", "plotus")):
            if not url:
                continue
            p = store_price(url)
            time.sleep(1.5)
            if p is None:
                fail += 1
                log("  ไม่ได้ราคา", k, f)
                continue
            ok += 1
            if r.get(f) is not None and r.get(f) != p:
                r[pf] = r[f]
            r[f], r[df] = p, TODAY.isoformat()
    if ok:
        R["date"] = TODAY.isoformat()
    status["retail"] = {"ok": ok > 0, "updated": ok, "failed": fail, "at": TODAY.isoformat()}
    return ok


# ---------------------------------------------------------------- main
def main():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    for name, fn in [("CPF Feed", update_cpffeed), ("สุกรขุนรายภาค", update_pig),
                     ("CPF รายสัปดาห์", update_cpf_weekly), ("มันสำปะหลัง", update_cassava),
                     ("แม็คโคร/โลตัส", update_retail)]:
        log(f"== {name}")
        try:
            log("   เปลี่ยน", fn(data), "รายการ")
        except Exception as e:     # แหล่งเดียวพัง ไม่ให้กระทบแหล่งอื่น
            log("   ERROR", repr(e))
            status[name] = {"ok": False, "why": repr(e)[:200], "at": TODAY.isoformat()}
    changed = json.dumps({k: v for k, v in data.items() if k != "status"}, ensure_ascii=False, sort_keys=True) != \
        json.dumps({k: v for k, v in json.loads(before).items() if k != "status"}, ensure_ascii=False, sort_keys=True)
    if changed:
        dates = [i["date"] for i in data["items"] if i.get("date")] + [data["pig"].get("date") or "", data["retail"].get("date") or ""]
        data["updated"] = max(d for d in dates if d <= TODAY.isoformat())
    data["status"] = {**data.get("status", {}), **status, "_last_run": TODAY.isoformat()}
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log("\nสถานะ:", json.dumps(status, ensure_ascii=False, indent=1))
    log("มีราคาใหม่" if changed else "ไม่มีราคาใหม่")


if __name__ == "__main__":
    sys.exit(main())
