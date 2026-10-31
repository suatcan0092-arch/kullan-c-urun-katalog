#!/usr/bin/env python3
"""
DS Damat / Süvari / Altınyıldız Classics / Kiğılı ürün scraper -> PostgreSQL

Strateji (her marka için otomatik):
  1) Shopify ise  /products.json  (Kiğılı bu yapıda görünüyor)
  2) Değilse robots.txt + sitemap.xml'den ürün URL'lerini topla, her sayfadaki
     schema.org JSON-LD "Product" verisini oku (ad, fiyat, görsel, stok, SKU).

Kullanım (Supabase REST, önerilen):
  export SUPABASE_URL="https://eidvwzpiqzqcrbqimgvk.supabase.co"
  export SUPABASE_SERVICE_KEY="<service_role anahtarı>"   # asla dosyaya yazmayın
  python scraper.py --limit 20     # deneme
  python scraper.py                # hepsi

Alternatif (doğrudan Postgres):
  export DATABASE_URL="postgresql://kullanici:sifre@localhost:5432/veritabani"
  psql "$DATABASE_URL" -f schema.sql
  python scraper.py                      # 4 markanın hepsi
  python scraper.py --brands kigili suvari
  python scraper.py --limit 50           # deneme: marka başına en fazla 50 ürün
  python scraper.py --dry-run            # DB'ye yazmadan sayıları göster

Not: Sitelerin kullanım koşullarına ve robots.txt kurallarına uyun. Bu script
robots.txt'i dikkate alır ve istekler arasında bekler.
"""
import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Alan adlarını gerekirse buradan düzeltin.
BRANDS = {
    "dsdamat": {"label": "D'S Damat", "base": "https://www.dsdamat.com"},
    "suvari": {"label": "Süvari", "base": "https://www.suvari.com.tr"},
    "altinyildiz": {"label": "Altınyıldız Classics", "base": "https://www.altinyildizclassics.com"},
    "kigili": {"label": "Kiğılı", "base": "https://www.kigili.com"},
}

UA = "Mozilla/5.0 (compatible; ProductCatalogBot/1.0; +personal-use)"
DELAY = 0.6        # saniye, iş parçacığı başına istekler arası bekleme
WORKERS = 4
TIMEOUT = 25

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "tr-TR,tr;q=0.9"})


# ----------------------------------------------------------------- yardımcılar
def get(url, **kw):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=TIMEOUT, **kw)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return r
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    return None


def parse_price(v):
    """'6.289,93' | '6289.93' | 6289.93 -> float"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.,]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif s.count(".") > 1 or re.search(r"\.\d{3}$", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def robots_for(base):
    rp = urllib.robotparser.RobotFileParser()
    r = get(urljoin(base, "/robots.txt"))
    rp.parse(r.text.splitlines() if r is not None and r.status_code == 200 else [])
    sitemaps = []
    if r is not None and r.status_code == 200:
        sitemaps = re.findall(r"(?im)^sitemap:\s*(\S+)", r.text)
    return rp, sitemaps


# ----------------------------------------------------------------- Shopify
def shopify_products(base, label, limit=None):
    out, page = [], 1
    while True:
        r = get(urljoin(base, "/products.json"), params={"limit": 250, "page": page})
        if r is None or r.status_code != 200:
            return None if page == 1 else out
        try:
            items = r.json().get("products", [])
        except ValueError:
            return None if page == 1 else out
        if not items:
            break
        for p in items:
            variants = p.get("variants") or []
            prices = [parse_price(v.get("price")) for v in variants if v.get("price")]
            compare = [parse_price(v.get("compare_at_price")) for v in variants if v.get("compare_at_price")]
            imgs = p.get("images") or []
            out.append({
                "brand": label,
                "url": urljoin(base, f"/products/{p['handle']}"),
                "sku": (variants[0].get("sku") if variants else None),
                "name": p.get("title"),
                "category": p.get("product_type") or None,
                "color": None,
                "price": min(prices) if prices else None,
                "original_price": max(compare) if compare else None,
                "currency": "TRY",
                "in_stock": any(v.get("available") for v in variants) if variants else None,
                "image_url": imgs[0]["src"] if imgs else None,
                "description": BeautifulSoup(p.get("body_html") or "", "lxml").get_text(" ", strip=True)[:2000],
                "raw": {"tags": p.get("tags"), "vendor": p.get("vendor"), "variants": len(variants)},
            })
        if limit and len(out) >= limit:
            return out[:limit]
        page += 1
        time.sleep(DELAY)
    return out


# ----------------------------------------------------------------- sitemap
def fetch_sitemap_urls(sm_url, depth=0):
    r = get(sm_url)
    if r is None or r.status_code != 200:
        return []
    data = r.content
    if sm_url.endswith(".gz") or data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError:
            pass
    soup = BeautifulSoup(data, "xml")
    if soup.find("sitemapindex") and depth < 3:
        urls = []
        for loc in soup.find_all("loc"):
            urls += fetch_sitemap_urls(loc.text.strip(), depth + 1)
        return urls
    return [loc.text.strip() for loc in soup.find_all("loc")]


PRODUCT_HINT = re.compile(r"(product|urun|ürün|/p/|-p-\d|\.html$)", re.I)
SKIP_HINT = re.compile(r"(/blog|/magaza|/store|/hakkimizda|/iletisim|/sss|/kategori|/category|\.(jpg|png|webp|pdf)$)", re.I)


def candidate_product_urls(base, rp, sitemaps):
    sms = sitemaps or [urljoin(base, "/sitemap.xml")]
    urls = []
    for sm in sms:
        urls += fetch_sitemap_urls(sm)
    host = urlparse(base).netloc.replace("www.", "")
    urls = [u for u in dict.fromkeys(urls) if host in u and not SKIP_HINT.search(u)]
    # Sitemap'te ürün/kategori ayrımı yoksa hepsini dener; ürün ipucu olanları öne alır
    hinted = [u for u in urls if PRODUCT_HINT.search(u)]
    return [u for u in (hinted or urls) if rp.can_fetch(UA, u)]


# ----------------------------------------------------------------- JSON-LD
def iter_ld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.text or "")
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                yield node
                for k in ("@graph", "itemListElement", "mainEntity"):
                    v = node.get(k)
                    if isinstance(v, (list, dict)):
                        stack.extend(v if isinstance(v, list) else [v])
            elif isinstance(node, list):
                stack.extend(node)


def is_type(node, t):
    nt = node.get("@type")
    return nt == t or (isinstance(nt, list) and t in nt)


def parse_product_page(html, url, label):
    soup = BeautifulSoup(html, "lxml")
    product, crumbs = None, []
    for node in iter_ld(soup):
        if is_type(node, "Product") and product is None:
            product = node
        elif is_type(node, "BreadcrumbList"):
            crumbs = [i.get("name") or (i.get("item") or {}).get("name")
                      for i in node.get("itemListElement", []) if isinstance(i, dict)]
    if not product:
        return None
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = parse_price(offers.get("price") or offers.get("lowPrice"))
    original = None
    spec = offers.get("priceSpecification")
    if isinstance(spec, list):
        vals = [parse_price(s.get("price")) for s in spec if isinstance(s, dict)]
        vals = [v for v in vals if v]
        original = max(vals) if len(vals) > 1 else None
    avail = str(offers.get("availability", ""))
    img = product.get("image")
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, dict):
        img = img.get("url")
    cats = [c for c in crumbs if c and c.lower() not in ("anasayfa", "ana sayfa", "home")]
    category = product.get("category") or (cats[-2] if len(cats) >= 2 else (cats[0] if cats else None))
    return {
        "brand": label,
        "url": url,
        "sku": product.get("sku") or product.get("mpn"),
        "name": product.get("name"),
        "category": category if isinstance(category, str) else None,
        "color": product.get("color"),
        "price": price,
        "original_price": original if original and price and original > price else None,
        "currency": offers.get("priceCurrency") or "TRY",
        "in_stock": ("InStock" in avail) if avail else None,
        "image_url": urljoin(url, img) if isinstance(img, str) else None,
        "description": (product.get("description") or "")[:2000],
        "raw": {"breadcrumbs": cats},
    }


def scrape_page(url, label):
    time.sleep(DELAY)
    r = get(url)
    if r is None or r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
        return None
    return parse_product_page(r.text, url, label)


# ----------------------------------------------------------------- DB
UPSERT = """
INSERT INTO products (brand,url,sku,name,category,color,price,original_price,currency,in_stock,image_url,description,raw)
VALUES (%(brand)s,%(url)s,%(sku)s,%(name)s,%(category)s,%(color)s,%(price)s,%(original_price)s,%(currency)s,
        %(in_stock)s,%(image_url)s,%(description)s,%(raw)s)
ON CONFLICT (url) DO UPDATE SET
  sku=EXCLUDED.sku, name=EXCLUDED.name, category=EXCLUDED.category, color=EXCLUDED.color,
  price=EXCLUDED.price, original_price=EXCLUDED.original_price, in_stock=EXCLUDED.in_stock,
  image_url=EXCLUDED.image_url, description=EXCLUDED.description, raw=EXCLUDED.raw, last_seen_at=now()
RETURNING id
"""


def save(conn, rows):
    import psycopg2.extras
    with conn.cursor() as cur:
        for row in rows:
            row = dict(row, raw=psycopg2.extras.Json(row.get("raw") or {}))
            cur.execute(UPSERT, row)
            pid = cur.fetchone()[0]
            cur.execute("INSERT INTO price_history (product_id, price, in_stock) VALUES (%s,%s,%s)",
                        (pid, row["price"], row["in_stock"]))
    conn.commit()


COLS = ["brand", "url", "sku", "name", "category", "color", "price", "original_price",
        "currency", "in_stock", "image_url", "description", "raw"]


def save_supabase(rows):
    """Supabase REST (PostgREST) ile toplu upsert. Gizli anahtar sadece ortam değişkeninden okunur."""
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    uniq = {r["url"]: r for r in rows}.values()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    batch = []
    for r in uniq:
        d = {c: r.get(c) for c in COLS}
        d["last_seen_at"] = now
        batch.append(d)
    for i in range(0, len(batch), 200):
        resp = requests.post(f"{base}/rest/v1/products?on_conflict=url", headers=headers,
                             data=json.dumps(batch[i:i + 200]), timeout=60)
        if resp.status_code >= 300:
            sys.exit(f"Supabase hatası {resp.status_code}: {resp.text[:300]}")


# ----------------------------------------------------------------- ana akış
def run_brand(key, cfg, limit, conn, dry):
    base, label = cfg["base"], cfg["label"]
    print(f"\n=== {label} ({base}) ===")
    rp, sitemaps = robots_for(base)

    rows = shopify_products(base, label, limit) if rp.can_fetch(UA, urljoin(base, "/products.json")) else None
    if rows:
        print(f"  Shopify /products.json: {len(rows)} ürün")
    else:
        urls = candidate_product_urls(base, rp, sitemaps)
        print(f"  Sitemap'ten {len(urls)} aday URL")
        if limit:
            urls = urls[: limit * 3]
        rows = []
        with ThreadPoolExecutor(WORKERS) as ex:
            futs = [ex.submit(scrape_page, u, label) for u in urls]
            for i, f in enumerate(as_completed(futs), 1):
                res = f.result()
                if res and res.get("name"):
                    rows.append(res)
                if i % 100 == 0:
                    print(f"  {i}/{len(urls)} sayfa tarandı, {len(rows)} ürün")
                if limit and len(rows) >= limit:
                    break
        print(f"  {len(rows)} ürün bulundu")

    if not rows:
        print("  UYARI: ürün bulunamadı. Alan adını (BRANDS) ve sitemap'i kontrol edin; "
              "site JSON-LD yerine farklı bir yapı kullanıyor olabilir.")
        return 0
    if not dry:
        save(conn, rows) if conn else save_supabase(rows)
        print(f"  {len(rows)} ürün veritabanına yazıldı")
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brands", nargs="*", choices=list(BRANDS), default=list(BRANDS))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = None
    use_supabase = bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"))
    if not args.dry_run and not use_supabase:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            sys.exit("SUPABASE_URL + SUPABASE_SERVICE_KEY (veya DATABASE_URL) tanımlı değil.")
        import psycopg2
        conn = psycopg2.connect(dsn)

    total = sum(run_brand(k, BRANDS[k], args.limit, conn, args.dry_run) for k in args.brands)
    print(f"\nToplam: {total} ürün")


if __name__ == "__main__":
    main()
