"""Universalna LPZ network -> Google My Maps CSVs + static web map.

Usage:
  python generate.py --region lviv                      # CSVs for Lviv oblast (My Maps geocodes addresses)
  python generate.py --fetch --region lviv --geocode --site
  python generate.py --fetch --region all --geocode --site
  python generate.py --region filled --cached --site    # rebuild every region already on the web map

Data source: https://universalna.com/lpz_all.html (the iframe behind
https://universalna.com/health-insurance/clinics/). `--fetch` downloads it to
data/lpz_all.html; otherwise data/lpz_all.html or ./data.json is used, and if
neither exists it is downloaded automatically.

Geocoding uses OpenStreetMap Nominatim (free, 1 req/s) unless the
GOOGLE_MAPS_API_KEY env var is set. Results are cached in data/geocache.json,
so re-runs only geocode new/changed addresses.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(ROOT, "out", "mymaps")
SITE_DATA_DIR = os.path.join(ROOT, "site", "data")
SOURCE_URL = "https://universalna.com/lpz_all.html"
LIVE_FILE = os.path.join(DATA_DIR, "lpz_all.html")
GEOCACHE_FILE = os.path.join(DATA_DIR, "geocache.json")
USER_AGENT = "insurance-maps-generator/1.0 (personal map of insurer clinic network)"
MYMAPS_LAYER_LIMIT = 2000  # My Maps: max rows per imported layer
SITE_MIN_DONE = 0.95  # a new region appears on the web map once this share of addresses is geocoded

# Oblast (as written in the source, normalized) -> region slug
REGIONS = {
    "Вінницька обл.": "vinnytsia",
    "Волинська обл.": "volyn",
    "Дніпропетровська обл.": "dnipro",
    "Донецька обл.": "donetsk",
    "Житомирська обл.": "zhytomyr",
    "Закарпатська обл.": "zakarpattia",
    "Запорізька обл.": "zaporizhzhia",
    "Івано-Франківська обл.": "ivano-frankivsk",
    "Київська обл.": "kyiv",
    "Кіровоградська обл.": "kropyvnytskyi",
    "Львівська обл.": "lviv",
    "Миколаївська обл.": "mykolaiv",
    "Одеська обл.": "odesa",
    "Полтавська обл.": "poltava",
    "Рівненська обл.": "rivne",
    "Сумська обл.": "sumy",
    "Тернопільська обл.": "ternopil",
    "Харківська обл.": "kharkiv",
    "Херсонська обл.": "kherson",
    "Хмельницька обл.": "khmelnytskyi",
    "Черкаська обл.": "cherkasy",
    "Чернівецька обл.": "chernivtsi",
    "Чернігівська обл.": "chernihiv",
}
OBLAST_TYPOS = {"Кировоградська обл.": "Кіровоградська обл.", "Черновицька обл.": "Чернівецька обл."}

CLINIC_SERVICES = [
    "Швидка допомога", "Амбулаторія", "Стаціонар", "Денний стаціонар", "УЗД", "КТ", "МРТ",
    "Лабораторна діагностика", "Виклик лікаря", "Фізіотерапія/Масаж", "Лікарняний лист",
    "Стоматологія", "Медогляд", "Профогляд", "Педіатр", "Виклик педіатра", "Стаціонар дитячий",
    "Вакцинація", "Ведення вагітності", "Пологи", "Оптика", "Оздоровлення/реабілітація",
    "Онлайн консультація",
]
CATEGORY_LABELS = {"1+": "1+", "1": "1", "2": "2", "3": "3", "4": "4", "0": "0 (стоматологія/оптика)"}
DATASET_KEYS = {"c": "clinics", "clinics": "clinics", "l": "labs", "labs": "labs",
                "p": "pharmacies", "pharmacies": "pharmacies", "w": "wellbeing", "well-being": "wellbeing"}


# ---------------------------------------------------------------- loading

def fetch_live():
    os.makedirs(DATA_DIR, exist_ok=True)
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
    with open(LIVE_FILE, "wb") as f:
        f.write(body)
    print("Downloaded %s (%d KB)" % (SOURCE_URL, len(body) // 1024))


def load_raw(path):
    """Parse either the live page (`var DATA = {c:[...],...}`) or the saved JS-ish data.json."""
    text = open(path, encoding="utf-8-sig").read()
    m = re.search(r"var\s+DATA\s*=\s*", text)
    start = m.end() if m else text.index("{")
    text = text[start:]
    # Quote bare object keys: {c:[[  ,clinics:[[  ,well-being:[[
    text = re.sub(r'([{,]\s*)([A-Za-z_-]+)(\s*:\s*\[)', r'\1"\2"\3', text)
    obj, _ = json.JSONDecoder().raw_decode(text)
    return {DATASET_KEYS[k]: v for k, v in obj.items() if k in DATASET_KEYS}


def clean(s):
    s = re.sub(r"(?<=[а-яА-ЯіІїЇєЄґҐ])[iI]|[iI](?=[а-яА-ЯіІїЇєЄґҐ])",
               lambda m: "і" if m.group() == "i" else "І", s or "")  # Latin i typed in Cyrillic words
    return re.sub(r"\s+", " ", s.strip())


def norm_oblast(s):
    s = clean(s)
    s = s[:1].upper() + s[1:]
    return OBLAST_TYPOS.get(s, s)


def split_addresses(addr):
    """'Львів, вул. А, 102/Львів, вул. Б, 73' -> two addresses; keeps '22/17А' and '(2/3)' intact."""
    parts = re.split(r"/(?=\s*[А-ЯІЇЄҐA-Z][^,/]*,)", addr)
    return [clean(p) for p in parts if clean(p)]


def to_records(raw):
    recs = []
    for kind, rows in raw.items():
        for r in rows:
            oblast = norm_oblast(r[4])
            base = {
                "kind": kind,
                "id": "%s-%s" % (kind, r[0]),
                "name": clean(r[1]),
                "city": clean(r[3]),
                "oblast": oblast,
                "region": REGIONS.get(oblast),
                "ownership": clean(r[5]),
                "category": clean(r[6]),
            }
            if kind == "clinics":
                base["services"] = [s for s, v in zip(CLINIC_SERVICES, r[7:30]) if v == "+"]
                base["notes"] = "" if r[30] == "-" else clean(r[30])
                base["franchise"] = r[31] == "+"
            elif kind == "labs":
                base["services"] = ["Лабораторія"] if r[7] == "+" else []
                base["notes"] = "" if r[8] == "-" else clean(r[8])
            elif kind == "pharmacies":
                base["services"] = []
                base["network"] = clean(r[9])
                base["notes"] = ""
            else:
                base["services"] = []
                base["notes"] = "" if r[9] == "-" else clean(r[9])
            addrs = split_addresses(r[2])
            for i, a in enumerate(addrs):
                rec = dict(base, address=a)
                if len(addrs) > 1:
                    rec["id"] = "%s.%d" % (base["id"], i + 1)
                recs.append(rec)
    return recs


# ---------------------------------------------------------------- geocoding

ABBR = [
    (r"\bвул\.\s*", "вулиця "), (r"\bпр-т\.?\s*", "проспект "), (r"\bпросп\.\s*", "проспект "),
    (r"\bпр\.\s*", "проспект "), (r"\bпл\.\s*", "площа "), (r"\bбул\.\s*", "бульвар "),
    (r"\bб-р\.?\s*", "бульвар "), (r"\bпров\.\s*", "провулок "), (r"\bнаб\.\s*", "набережна "),
    (r"\bш\.\s*", "шосе "),
]


STREET_WORDS = r"(вулиця|проспект|площа|бульвар|провулок|набережна|шосе|майдан|узвіз|тракт|алея|дорога)"


def clean_address(rec):
    """Drop office/room details and normalize spacing; make sure the city is in the string."""
    a = rec["address"]
    a = re.sub(r"\([^)]*\)", "", a)
    a = re.sub(r",?\s*(офіс|оф|прим|кв|каб|поверх|пов)\.?\s*[\w/-]+\s*$", "", a, flags=re.I)
    a = re.sub(r",\s*(?=\S)", ", ", a)
    a = re.sub(r"\b(вул|пр|просп|пл|бул|пров)\.(?=\S)", r"\1. ", a)
    a = clean(a).strip(" ,")
    if rec["city"] and rec["city"].lower() not in a.lower():
        a = "%s, %s" % (rec["city"], a)
    return a


def geo_query(rec):
    """Full address string for a geocoder (and the My Maps 'Адреса для мапи' column)."""
    oblast = "" if rec["city"] == "Київ" else rec["oblast"].replace("обл.", "область")
    return ", ".join(p for p in [clean_address(rec), oblast, "Україна"] if p)


def expand(q):
    for pat, rep in ABBR:
        q = re.sub(pat, rep, q, flags=re.I)
    return clean(q)


def parse_address(rec):
    """-> (city, street, house_number) for structured geocoding; street/number may be ''."""
    a = re.sub(r"\s+селищ\.?(?=,|$)", "", clean_address(rec))  # 'Рудне селищ.'
    parts = [re.sub(r"^(с\.\s*м\.\s*т|смт|м|с|сел|селище|селищ)(\.\s*|\s+)", "", p.strip(), flags=re.I)
             for p in a.split(",")]
    parts = [p for p in parts if p]
    city = rec["city"]
    if parts and not re.search(r"\d", parts[0]) and not re.search(STREET_WORDS, expand(parts[0]), re.I):
        city = parts.pop(0)  # address's own town name (may be an older/variant name)
    parts = [expand(p) for p in parts]
    number, street = "", ""
    for i, p in enumerate(parts):
        m = re.match(r"^(буд\.?\s*)?(№\s*)?(\d+\s?[А-Яа-яЄєІіЇїA-Za-z]?)\b", p)
        if m and not number:
            number = m.group(3).replace(" ", "")
            if not street and i > 0:
                street = parts[i - 1]
    for p in parts:
        if re.search(STREET_WORDS, p, re.I):
            street = p
            break
    if not street and parts:
        street = parts[0]
    # 'Стрий вулиця Січових Стрільців 12/4' style: number glued to the street
    m = re.search(r"\s(\d+[А-Яа-яЄєІіЇїA-Za-z]?)(?:[/-]\S*)?$", street)
    if m:
        number = number or m.group(1)
        street = street[:m.start()]
    # 'Франка І. вулиця' -> 'вулиця Франка'; drop initials, which OSM spells out
    m = re.match(r"^(.*?)\s+%s$" % STREET_WORDS, street, re.I)
    if m:
        street = "%s %s" % (m.group(2), m.group(1))
    street = re.sub(r"^%s\s+(?=%s)" % (re.escape(city), STREET_WORDS), "", street, flags=re.I)
    street = clean(re.sub(r"\b[А-ЯІЇЄҐA-Z][а-яіїєґ]{0,2}\.\s*", "", street))
    if re.match(r"^\d", street):
        street = ""
    return city, street, number


def http_json(url, headers=None):
    """GET JSON; on 429/5xx back off (10s, 30s, 90s, 270s) before giving up."""
    req = urllib.request.Request(url, headers=dict({"User-Agent": USER_AGENT}, **(headers or {})))
    for wait in (10, 30, 90, 270, None):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if wait is None or not (e.code == 429 or e.code >= 500):
                raise
            print("  geocoder busy (HTTP %d), waiting %ds" % (e.code, wait))
            time.sleep(wait)


PRECISION_RANK = {"house": 0, "street": 1, "city": 2}


def outliers(recs, geocoder):
    """Records whose cached point is far from their town - usually the same street name matched in
    another town. Town position = median of its 3+ points, else the geocoded town centre.
    Radius scales with the town's own spread (4x the median distance, min 4 km), so Kyiv or
    Kryvyi Rih aren't flagged for being big. Points already re-checked once (4th cache field)
    are skipped, so they aren't re-geocoded on every run."""
    def km(a, b, c, d):
        return ((a - c) ** 2 + ((b - d) * 0.65) ** 2) ** 0.5 * 111

    by_city = {}
    for r in recs:
        g = geocoder.cache.get(geo_query(r))
        if g and g[2] != "city":
            by_city.setdefault(r["city"].lower(), []).append((r, g))
    bad = []
    for items in by_city.values():
        if len(items) >= 3:
            mla = sorted(g[0] for _, g in items)[len(items) // 2]
            mln = sorted(g[1] for _, g in items)[len(items) // 2]
            dists = sorted(km(g[0], g[1], mla, mln) for _, g in items)
            radius = max(4.0, 4 * dists[len(dists) // 2])
        else:
            centre = geocoder.town_centre(items[0][0])
            if not centre:
                continue
            mla, mln = centre
            radius = 4.0
        bad += [r for r, g in items if len(g) < 4 and km(g[0], g[1], mla, mln) > radius]
    return bad


def place_ok(rec, ad):
    """The hit's settlement must match the record's town. Only settlement-level keys: hromadas and
    raions are named after towns too (Самбірський район), so they would match villages around it."""
    names = {parse_address(rec)[0], rec["city"]}
    hay = " ".join(ad.get(k, "") for k in ("city", "town", "village", "hamlet", "suburb", "city_district",
                                           "borough", "quarter", "neighbourhood")).lower()
    return any(n and n.lower()[:5] in hay for n in names)


def oblast_ok(rec, state):
    """Reject hits outside the record's oblast (e.g. Миколаїв in Lviv obl. vs Mykolaiv city)."""
    stem = rec["oblast"][:4].lower()
    return stem in (state or "").lower()


class Geocoder:
    def __init__(self, offline=False, deadline=None):
        self.offline = offline  # only use cached coordinates, never call a geocoder or write the cache
        self.deadline = deadline  # epoch seconds; after it, behave as offline (CI job time limit)
        self.errors = 0  # consecutive lookup failures
        self.rechecking = set()  # queries evicted as outliers; their new result gets marked
        self.cache = {}
        if os.path.exists(GEOCACHE_FILE):
            self.cache = json.load(open(GEOCACHE_FILE, encoding="utf-8"))
        self.google_key = os.environ.get("GOOGLE_MAPS_API_KEY")
        self.last = 0.0
        self.dirty = 0

    def save(self):
        if self.offline:
            return
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = GEOCACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=0, sort_keys=True)
        os.replace(tmp, GEOCACHE_FILE)
        self.dirty = 0

    def _throttle(self, interval):
        wait = self.last + interval - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()

    def _nominatim(self, rec, **params):
        """params: q=... (free-form) or street=/city= (structured). -> [lat, lng, precision] or None"""
        self._throttle(1.1)
        params.update({"format": "jsonv2", "addressdetails": 1, "limit": 3, "countrycodes": "ua",
                       "accept-language": "uk"})
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
        for hit in http_json(url):
            ad = hit.get("address", {})
            state = ad.get("state")
            if state and not oblast_ok(rec, state):
                continue
            prec = "house" if ad.get("house_number") else (
                "street" if hit.get("addresstype") in ("road", "square") or ad.get("road") else "city")
            # same street name in another town of the oblast; hits without an oblast (OSM has none
            # around Lutsk) can only be checked by town name
            if (prec != "city" or not state) and not place_ok(rec, ad):
                continue
            return [float(hit["lat"]), float(hit["lon"]), prec]
        return None

    def town_centre(self, rec):
        city = parse_address(rec)[0]
        key = "town:%s|%s" % (city, rec["oblast"])
        if key not in self.cache and not self.offline and not self.google_key:
            try:
                hit = self._nominatim(rec, city=city, state=rec["oblast"].replace("обл.", "область"))
            except Exception as e:
                print("  ! town lookup error for %s: %s" % (city, e))
                return None
            self.cache[key] = hit[:2] if hit else None
        return self.cache.get(key)

    def _nominatim_best(self, rec):
        city, street, number = parse_address(rec)
        attempts = []
        if street and number:
            attempts.append({"street": "%s %s" % (number, street), "city": city})
        attempts.append({"q": expand(geo_query(rec))})
        if street:
            attempts.append({"street": street, "city": city})
        best = None
        for params in attempts:
            hit = self._nominatim(rec, **params)
            if hit and (best is None or PRECISION_RANK[hit[2]] < PRECISION_RANK[best[2]]):
                best = hit
            if best and best[2] == "house":
                return best
        if best and best[2] == "street":
            return best
        return self._nominatim(rec, city=city, state=rec["oblast"].replace("обл.", "область")) or best

    def _google(self, q, rec):
        self._throttle(0.05)
        url = "https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode(
            {"address": q, "region": "ua", "language": "uk", "key": self.google_key})
        res = http_json(url)
        for hit in res.get("results", []):
            states = [c["long_name"] for c in hit["address_components"]
                      if "administrative_area_level_1" in c["types"]]
            if oblast_ok(rec, states[0] if states else ""):
                loc = hit["geometry"]["location"]
                types = set(hit["types"])
                if types & {"street_address", "premise", "subpremise", "establishment"}:
                    prec = "house"
                elif "route" in types:
                    prec = "street"
                else:
                    prec = "city"
                return [loc["lat"], loc["lng"], prec]
        return None

    def geocode(self, rec):
        q = geo_query(rec)
        if not self.offline and self.deadline and time.time() > self.deadline:
            print("  time budget reached: saving and continuing with cached coordinates only")
            self.save()
            self.offline = True
        if q in self.cache or self.offline:
            return self.cache.get(q)
        try:
            result = self._google(q, rec) if self.google_key else self._nominatim_best(rec)
        except Exception as e:  # network hiccup: don't cache, try next run
            print("  ! geocode error for %r: %s" % (q, e))
            self.errors += 1
            if self.errors >= 5:
                print("  ! geocoder keeps failing: saving and continuing with cached coordinates only")
                self.save()
                self.offline = True
            return None
        self.errors = 0
        if result and q in self.rechecking:
            result = list(result[:3]) + [1]  # mark: outlier re-checked, keep whatever it is now
        self.cache[q] = result
        self.dirty += 1
        if self.dirty >= 25:
            self.save()
        return result


# ---------------------------------------------------------------- outputs

def gmaps_link(rec):
    if rec.get("lat") is not None:
        return "https://www.google.com/maps/search/?api=1&query=%s,%s" % (rec["lat"], rec["lng"])
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(geo_query(rec))


def write_csv(path, rows, fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def csv_rows(recs):
    rows = []
    for r in recs:
        row = {
            "Назва": r["name"],
        }
        if r["kind"] != "pharmacies":
            row["Категорія"] = CATEGORY_LABELS.get(r["category"], r["category"])
        row.update({
            "Адреса": r["address"],
            "Місто": r["city"],
            "Адреса для мапи": geo_query(r),
            "Власність": r["ownership"],
        })
        if r["kind"] == "clinics":
            row["Послуги"] = ", ".join(r["services"])
            row["Франшиза"] = "так" if r.get("franchise") else "ні"
        if r["kind"] == "pharmacies":
            row["Мережа"] = r.get("network", "")
        row["Примітки"] = r.get("notes", "")
        row["Latitude"] = r.get("lat", "")
        row["Longitude"] = r.get("lng", "")
        row["Точність"] = r.get("precision", "")
        row["Google Maps"] = gmaps_link(r)
        rows.append(row)
    return rows


def export_mymaps(slug, recs):
    out = os.path.join(OUT_DIR, slug)
    files = []
    for kind in ("clinics", "labs", "pharmacies", "wellbeing"):
        part = [r for r in recs if r["kind"] == kind]
        if not part:
            continue
        if kind == "clinics":
            order = {"1+": 0, "1": 1, "2": 2, "3": 3, "4": 4, "0": 5}
            part.sort(key=lambda r: (order.get(r["category"], 9), r["city"], r["name"]))
        rows = csv_rows(part)
        fields = list(rows[0].keys())
        chunks = [rows[i:i + MYMAPS_LAYER_LIMIT] for i in range(0, len(rows), MYMAPS_LAYER_LIMIT)]
        for i, chunk in enumerate(chunks):
            name = kind + ("_%d" % (i + 1) if len(chunks) > 1 else "") + ".csv"
            write_csv(os.path.join(out, name), chunk, fields)
            files.append("%s (%d)" % (name, len(chunk)))
    print("  %s -> out/mymaps/%s/: %s" % (slug, slug, ", ".join(files)))


def export_site(slug, oblast, recs):
    os.makedirs(SITE_DATA_DIR, exist_ok=True)
    points = []
    for r in recs:
        if r.get("lat") is None:
            continue
        p = {"k": r["kind"][0], "n": r["name"], "a": r["address"], "c": r["city"], "cat": r["category"],
             "o": r["ownership"], "la": round(r["lat"], 6), "ln": round(r["lng"], 6), "pr": r["precision"]}
        if r["services"]:
            p["s"] = r["services"]
        if r.get("notes"):
            p["no"] = r["notes"]
        if r.get("franchise"):
            p["f"] = 1
        if r.get("network"):
            p["net"] = r["network"]
        points.append(p)
    with open(os.path.join(SITE_DATA_DIR, slug + ".js"), "w", encoding="utf-8") as f:
        f.write("window.LPZ_LOAD(%s,%s);\n" % (json.dumps(slug), json.dumps(points, ensure_ascii=False, separators=(",", ":"))))
    # region index: every slug that has a data file
    index = []
    alphabet = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
    uk_key = lambda kv: [alphabet.find(ch) for ch in kv[0].lower()]  # Unicode order puts І before А
    for ob, s in sorted(REGIONS.items(), key=uk_key):
        if os.path.exists(os.path.join(SITE_DATA_DIR, s + ".js")):
            index.append({"slug": s, "name": ob.replace(" обл.", "")})
    with open(os.path.join(SITE_DATA_DIR, "regions.js"), "w", encoding="utf-8") as f:
        f.write("window.LPZ_REGIONS=%s;\n" % json.dumps(index, ensure_ascii=False))
    print("  %s -> site/data/%s.js (%d points)" % (slug, slug, len(points)))


def export_seo(site_url):
    """robots.txt + sitemap.xml need the absolute public URL (the GitHub workflow passes it)."""
    u = urllib.parse.urlsplit(site_url)
    base = urllib.parse.urlunsplit((u.scheme, u.netloc.lower(), u.path.rstrip("/") + "/", "", ""))
    with open(os.path.join(ROOT, "site", "robots.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("User-agent: *\nAllow: /\n\nSitemap: %ssitemap.xml\n" % base)
    with open(os.path.join(ROOT, "site", "sitemap.xml"), "w", encoding="utf-8", newline="\n") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                "  <url><loc>%s</loc><lastmod>%s</lastmod><changefreq>weekly</changefreq></url>\n"
                "</urlset>\n" % (base, time.strftime("%Y-%m-%d", time.gmtime())))
    print("  site/robots.txt + site/sitemap.xml for %s" % base)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true", help="download fresh data from universalna.com")
    ap.add_argument("--input", help="data file (default: data/lpz_all.html, else ./data.json)")
    ap.add_argument("--region", nargs="+", default=["lviv"],
                    help="region slugs, 'all', or 'filled' = regions already on the web map (default: lviv)")
    ap.add_argument("--print-regions", action="store_true",
                    help="print the slugs --region resolves to and exit (used by the GitHub workflow)")
    ap.add_argument("--kinds", nargs="+", default=["clinics", "labs", "pharmacies", "wellbeing"],
                    help="which datasets to include")
    ap.add_argument("--geocode", action="store_true", help="add coordinates (cached in data/geocache.json)")
    ap.add_argument("--retry-missing", action="store_true",
                    help="with --geocode: retry addresses that previously could not be found")
    ap.add_argument("--cached", action="store_true",
                    help="use only coordinates already in data/geocache.json (no network lookups)")
    ap.add_argument("--max-minutes", type=float,
                    help="stop new geocoder lookups after this many minutes (progress is kept in the cache)")
    ap.add_argument("--site", action="store_true", help="write site/data/<region>.js for the web map (needs --geocode or --cached)")
    ap.add_argument("--site-url", default=os.environ.get("SITE_URL"),
                    help="public URL of the site; with --site also writes robots.txt + sitemap.xml (env SITE_URL)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    all_slugs = sorted(set(REGIONS.values()))
    slugs = []
    for s in args.region:
        if s == "all":
            slugs += all_slugs
        elif s == "filled":
            slugs += [x for x in all_slugs if os.path.exists(os.path.join(SITE_DATA_DIR, x + ".js"))]
        elif s in all_slugs:
            slugs.append(s)
        else:
            ap.error("unknown region %r; choose from: all, filled, %s" % (s, ", ".join(all_slugs)))
    slugs = sorted(set(slugs))
    if args.print_regions:
        print(" ".join(slugs))
        return

    path = args.input or (LIVE_FILE if os.path.exists(LIVE_FILE) else os.path.join(ROOT, "data.json"))
    if args.fetch or (not args.input and not os.path.exists(path)):
        fetch_live()
        path = args.input or LIVE_FILE
    recs = [r for r in to_records(load_raw(path)) if r["kind"] in args.kinds]
    print("Loaded %d locations from %s" % (len(recs), os.path.relpath(path, ROOT)))
    unknown = sorted({r["oblast"] for r in recs if not r["region"]})
    if unknown:
        print("Skipped (no region): %s" % ", ".join(unknown))

    oblast_by_slug = {v: k for k, v in REGIONS.items()}

    deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None
    geocoder = Geocoder(offline=args.cached, deadline=deadline) if (args.geocode or args.cached) else None
    for slug in slugs:
        part = [r for r in recs if r["region"] == slug]
        if geocoder:
            if not args.cached:
                evict = {geo_query(r) for r in outliers(part, geocoder)}
                if args.retry_missing:
                    evict |= {geo_query(r) for r in part if geocoder.cache.get(geo_query(r), 0) is None}
                for q in evict:
                    geocoder.cache.pop(q, None)
                geocoder.rechecking = evict
                if evict:
                    print("  re-checking %d outlier/missing addresses" % len(evict))
            todo = len({geo_query(r) for r in part} - set(geocoder.cache))
            print("Geocoding %s: %d addresses, %d not cached yet%s"
                  % (slug, len(part), todo, " (skipped: --cached)" if args.cached else ""))
            looked_up = 0
            for r in part:
                fresh = geo_query(r) not in geocoder.cache
                g = geocoder.geocode(r)
                if g:
                    r["lat"], r["lng"], r["precision"] = g[:3]
                if fresh and not geocoder.offline:
                    looked_up += 1
                    if looked_up % 50 == 0:
                        print("  %d/%d looked up" % (looked_up, todo))
            for r in outliers(part, geocoder):
                print("  ! far from other %s locations: %s" % (r["city"], r["address"]))
            geocoder.save()
            unique = {geo_query(r) for r in part}
            left = len(unique - set(geocoder.cache))
            done = 1 - left / len(unique) if unique else 1.0
            if left and not args.cached:
                print("  %d addresses not geocoded yet - run again to continue" % left)
            stats = {}
            for r in part:
                stats[r.get("precision", "missing")] = stats.get(r.get("precision", "missing"), 0) + 1
            print("  precision: %s" % stats)
        export_mymaps(slug, part)
        if args.site:
            published = os.path.exists(os.path.join(SITE_DATA_DIR, slug + ".js"))
            if geocoder and done < SITE_MIN_DONE and not published:
                # keep half-geocoded regions off the map until a later run finishes them
                print("  %s: only %d%% geocoded, not added to the web map yet" % (slug, done * 100))
            else:
                export_site(slug, oblast_by_slug[slug], part)
    if args.site and args.site_url:
        export_seo(args.site_url)


if __name__ == "__main__":
    main()
