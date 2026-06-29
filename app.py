#!/usr/bin/env python3
"""
THE VIDIAN METHOD - Scan Engine (Firecrawl-powered web service)
------------------------------------------------------------------
A small API the dashboard calls. POST a URL, it fetches the live site
through Firecrawl (handles JavaScript + anti-bot), runs 42 real
checkpoints across five sections, and returns scored JSON.

The Firecrawl API key is read from the environment (FIRECRAWL_API_KEY).
It is NEVER hardcoded and NEVER sent to the browser.

Run locally:
    export FIRECRAWL_API_KEY=fc-xxxxx
    pip install -r requirements.txt
    python3 app.py                      # CLI:  python3 app.py https://site.com
    gunicorn app:app                    # server on :8000  (POST /scan {"url": "..."} )
"""
import os, re, json, sys
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from flask_cors import CORS

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# ----------------------------------------------------------------------
# FETCH LAYER
# ----------------------------------------------------------------------
def firecrawl_fetch(url):
    """Fetch the main page via Firecrawl. Returns (html, final_url, headers, status)."""
    from firecrawl import Firecrawl
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        raise RuntimeError("FIRECRAWL_API_KEY is not set in the environment.")
    app_fc = Firecrawl(api_key=key)
    doc = app_fc.scrape(url, formats=["rawHtml", "html", "links"], only_main_content=False)
    # firecrawl-py v4 returns a Document object; be defensive about attribute names
    html = (getattr(doc, "raw_html", None) or getattr(doc, "rawHtml", None)
            or getattr(doc, "html", None) or "")
    meta = getattr(doc, "metadata", None) or {}
    if hasattr(meta, "model_dump"):
        meta = meta.model_dump()
    final_url = (meta.get("url") or meta.get("sourceURL") or url) if isinstance(meta, dict) else url
    status = (meta.get("statusCode") or meta.get("status_code") or 200) if isinstance(meta, dict) else 200
    headers = {}  # Firecrawl does not return raw response headers; header-based checks degrade gracefully
    return html, final_url, headers, status


def plain_fetch(url):
    """Direct fetch fallback (used for the CLI without a key, and for robots/sitemap/llms)."""
    r = requests.get(url, headers=UA, timeout=20, allow_redirects=True)
    return r.text, r.url, {k.lower(): v for k, v in r.headers.items()}, r.status_code


def aux_get(path, base):
    """Cheap direct fetch for robots.txt / sitemap.xml / llms.txt (rarely bot-protected)."""
    try:
        r = requests.get(urljoin(base, path), headers=UA, timeout=10, allow_redirects=True)
        return r.status_code, r.text, {k.lower(): v for k, v in r.headers.items()}
    except Exception:
        return None, "", {}


# ----------------------------------------------------------------------
# ANALYSIS (pure: no network) -- the 42 checkpoints
# ----------------------------------------------------------------------
# Maps engine check labels to the scan-item ids used by the Vidian Method
# foundational tool (the 6-group website scan). This lets a live scan drop
# straight into the existing scan section without re-checking anything.
# Items not listed (mobile, speed, layout shift, pricing clarity, etc.)
# are eyeball/performance items the engine can't see, left for manual review.
LABEL_TO_SID = {
    "Page title present and descriptive": "title",
    "Search snippet description set": "meta",
    "Single clear H1 headline": "h1",
    "Canonical URL declared": "canonical",
    "Indexable by search engines": "robots",
    "XML sitemap published": "sitemap",
    "Local business schema present": "schema",
    "Structured data for AI engines (JSON-LD)": "schema",
    "Phone number readable on page": "nap",
    "Street address readable on page": "nap",
    "Google Maps / location linked": "gbp",
    "Images labeled for context": "alt",
    "Clear primary call to action": "atf",
    "Click-to-call phone link": "calltap",
    "Contact form on site": "form",
    "Online booking / scheduling": "booking",
    "Web analytics installed": "events",
    "Conversion / ad tracking present": "pixel",
    "Retargeting pixel active": "pixel",
    "Secure connection (HTTPS)": "ssl",
    "Reviews / social proof visible": "proof",
    "Working, current social links": "social",
    "Footer copyright current": "copyright",
    "Privacy policy posted": "privacy",
    "Terms of service posted": "terms",
    "Cookie consent before tracking": "cookie",
    "Accessibility baseline (alt text + lang)": "ada",
    "ARIA accessibility attributes": "ada",
    "Strict transport security header": "headers",
}


def _check(label, passed, detail=""):
    return {"label": label, "status": "PASS" if passed else "GAP", "detail": detail}


def scan_core(html, final_url, headers=None, robots=None, sitemap=None, llms=None):
    headers = headers or {}
    soup = BeautifulSoup(html or "", "lxml")
    base = "{u.scheme}://{u.netloc}".format(u=urlparse(final_url))
    text = soup.get_text(" ", strip=True)
    text_l = text.lower()
    html_l = (html or "").lower()
    scripts = " ".join([s.get("src", "") + " " + (s.string or "") for s in soup.find_all("script")]).lower()

    # JSON-LD types
    jsonld_types = []
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(s.string or "{}")
            items = data if isinstance(data, list) else [data]
            for it in items:
                t = it.get("@type")
                if isinstance(t, list): jsonld_types += [str(x) for x in t]
                elif t: jsonld_types.append(str(t))
                for g in it.get("@graph", []) if isinstance(it, dict) else []:
                    gt = g.get("@type")
                    if isinstance(gt, list): jsonld_types += [str(x) for x in gt]
                    elif gt: jsonld_types.append(str(gt))
        except Exception:
            pass
    jsonld_types = [t.lower() for t in jsonld_types]

    imgs = soup.find_all("img")
    imgs_alt = [i for i in imgs if i.get("alt", "").strip()]
    alt_ratio = (len(imgs_alt) / len(imgs)) if imgs else 1.0
    phone_links = soup.select('a[href^="tel:"]')
    forms = soup.find_all("form")
    https = final_url.startswith("https")

    S = []

    # 1) FOUND AND CHOSEN
    title = (soup.title.string or "").strip() if soup.title else ""
    md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    md_c = (md.get("content", "").strip() if md else "")
    h1s = soup.find_all("h1")
    canonical = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
    robots_meta = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    noindex = bool(robots_meta and "noindex" in robots_meta.get("content", "").lower())
    has_local = any(t in jsonld_types for t in ["localbusiness","organization","professionalservice","medicalbusiness","store","dentist","physician"]) or "localbusiness" in html_l
    phone = re.search(r"(\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}", text)
    addr = re.search(r"\d{1,6}\s+[\w\.\s]{3,40}\b(st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|ln|lane|way|suite|ste|pkwy|hwy)\b", text_l)
    maplink = bool(soup.select('a[href*="google.com/maps"], a[href*="goo.gl/maps"], iframe[src*="google.com/maps"]')) or "maps.google" in html_l
    og = soup.find("meta", attrs={"property": re.compile("^og:", re.I)})
    S.append({"name":"Found and Chosen","checks":[
        _check("Page title present and descriptive", bool(title) and len(title)>=15, f'"{title[:70]}"' if title else "No <title>"),
        _check("Search snippet description set", bool(md_c) and len(md_c)>=50, f"{len(md_c)} chars" if md_c else "Missing"),
        _check("Single clear H1 headline", len(h1s)==1, f"{len(h1s)} H1 tags"),
        _check("Canonical URL declared", bool(canonical), canonical.get("href","") if canonical else "Missing"),
        _check("Indexable by search engines", not noindex, "noindex present" if noindex else "Open"),
        _check("XML sitemap published", sitemap=="ok", "Found" if sitemap=="ok" else "Not found"),
        _check("robots.txt present", bool(robots), "Found" if robots else "Missing"),
        _check("Local business schema present", has_local, ", ".join(sorted(set(jsonld_types))[:4]) if jsonld_types else "None"),
        _check("Phone number readable on page", bool(phone), phone.group(0) if phone else "Not found"),
        _check("Street address readable on page", bool(addr), "Found" if addr else "Not found"),
        _check("Google Maps / location linked", maplink, "Linked" if maplink else "Not linked"),
        _check("Social share preview tags (Open Graph)", bool(og), "Present" if og else "Missing"),
    ]})

    # 2) READABLE BY AI
    ai_bots = ["gptbot","claudebot","perplexitybot","google-extended","ccbot","anthropic-ai","oai-searchbot"]
    rl = (robots or "").lower()
    blocks_ai = any(re.search(rf"user-agent:\s*{b}[\s\S]*?disallow:\s*/", rl) for b in ai_bots)
    wc = len(text.split())
    headings = soup.find_all(["h1","h2","h3"])
    faq = any("faq" in t or "question" in t for t in jsonld_types)
    org = any(t in jsonld_types for t in ["organization","localbusiness","professionalservice"])
    sameas = "sameas" in html_l
    semantic = bool(soup.find(["main","article","section","nav","header","footer"]))
    S.append({"name":"Readable by AI","checks":[
        _check("Structured data for AI engines (JSON-LD)", len(jsonld_types)>0, ", ".join(sorted(set(jsonld_types))[:5]) if jsonld_types else "None"),
        _check("Organization/entity identity defined", org, "Defined" if org else "Missing"),
        _check("Linked authoritative profiles (sameAs)", sameas, "Present" if sameas else "Missing"),
        _check("FAQ / Q&A structured content", faq, "Present" if faq else "Missing"),
        _check("Substantive content for AI to read", wc>=300, f"{wc} words"),
        _check("Clear heading structure", len(headings)>=3, f"{len(headings)} headings"),
        _check("Semantic page structure", semantic, "Present" if semantic else "Missing"),
        _check("Images labeled for context", alt_ratio>=0.7, f"{len(imgs_alt)}/{len(imgs)} labeled"),
        _check("AI crawlers not blocked", not blocks_ai, "Blocked" if blocks_ai else "Allowed"),
        _check("AI guidance file (llms.txt)", llms=="ok", "Present" if llms=="ok" else "Not present (emerging)"),
    ]})

    # 3) BUILT TO CONVERT
    cta = any(w in text_l for w in ["book","schedule","appointment","get started","contact us","request","call now","free consult","quote","sign up","buy now","order"])
    booking = any(w in html_l for w in ["calendly","acuity","squarespace-scheduling","youcanbook","booksy","vagaro","schedulicity","setmore","/book","book-now","book-online","zocdoc"])
    ga = "google-analytics.com" in scripts or "googletagmanager.com/gtag" in scripts or "gtag(" in scripts
    gtm = "googletagmanager.com/gtm" in scripts or "gtm-" in html_l
    meta_px = "connect.facebook.net" in scripts or "fbq(" in scripts
    gads = "googleadservices" in scripts or "google_conversion" in scripts or "aw-" in html_l
    ttk = "analytics.tiktok.com" in scripts
    any_track = ga or gtm or meta_px or gads or ttk
    retarget = meta_px or gads or ttk or "doubleclick" in scripts
    S.append({"name":"Built to Convert","checks":[
        _check("Clear primary call to action", cta, "Present" if cta else "None obvious"),
        _check("Click-to-call phone link", bool(phone_links), f"{len(phone_links)} tel: link(s)" if phone_links else "Not click-to-call"),
        _check("Contact form on site", bool(forms), f"{len(forms)} form(s)" if forms else "None"),
        _check("Online booking / scheduling", booking, "Detected" if booking else "Not found"),
        _check("Web analytics installed", ga or gtm, "Present" if (ga or gtm) else "None"),
        _check("Conversion / ad tracking present", any_track, ", ".join([n for n,v in [("GA",ga),("GTM",gtm),("Meta",meta_px),("GAds",gads),("TikTok",ttk)] if v]) or "None"),
        _check("Retargeting pixel active", retarget, "Present" if retarget else "None"),
    ]})

    # 4) TRUSTED ON SIGHT
    review = any(t in jsonld_types for t in ["aggregaterating","review","rating"]) or any(w in text_l for w in ["reviews","testimonial","★","5-star","5 star","rated"])
    socials = {}
    for net,pat in {"facebook":"facebook.com","instagram":"instagram.com","linkedin":"linkedin.com","youtube":"youtube.com","x/twitter":"twitter.com","tiktok":"tiktok.com","yelp":"yelp.com"}.items():
        if soup.select(f'a[href*="{pat}"]'): socials[net]=True
    years = re.findall(r"©?\s*(20\d{2})", text)
    ty = datetime.now().year
    year_ok = bool(years) and (str(ty) in years or str(ty-1) in years)
    favicon = bool(soup.find("link", attrs={"rel": re.compile("icon", re.I)}))
    S.append({"name":"Trusted on Sight","checks":[
        _check("Secure connection (HTTPS)", https, "HTTPS" if https else "Not secure"),
        _check("Reviews / social proof visible", review, "Present" if review else "None visible"),
        _check("Working, current social links", len(socials)>=1, ", ".join(socials.keys()) if socials else "None"),
        _check("Footer copyright current", year_ok, f"Latest: {max(years) if years else 'none'}"),
        _check("Brand favicon set", favicon, "Present" if favicon else "Missing"),
    ]})

    # 5) LIABILITY EXPOSURE
    def has_link(words):
        for a in soup.find_all("a"):
            t=(a.get_text(" ",strip=True) or "").lower(); h=(a.get("href","") or "").lower()
            if any(w in t or w in h for w in words): return True
        return False
    privacy = has_link(["privacy"])
    terms = has_link(["terms","conditions","tos"])
    cookie = ("cookie" in text_l and any(w in text_l for w in ["consent","accept","we use cookies"])) or "cookieconsent" in html_l or "cookie-consent" in html_l
    lang = bool(soup.html.get("lang")) if soup.html else False
    aria = "aria-" in html_l
    hsts = "strict-transport-security" in headers
    access_ok = (alt_ratio>=0.7) and lang
    S.append({"name":"Liability Exposure","checks":[
        _check("Privacy policy posted", privacy, "Linked" if privacy else "Not found"),
        _check("Terms of service posted", terms, "Linked" if terms else "Not found"),
        _check("Cookie consent before tracking", cookie or not any_track, "Present" if cookie else ("No trackers" if not any_track else "Tracking w/o visible consent")),
        _check("Page language declared", lang, "Set" if lang else "Missing"),
        _check("Accessibility baseline (alt text + lang)", access_ok, f"alt {len(imgs_alt)}/{len(imgs)}, lang {'yes' if lang else 'no'}"),
        _check("ARIA accessibility attributes", aria, "Present" if aria else "None"),
        _check("HTTPS encryption enforced", https, "Yes" if https else "No"),
        _check("Strict transport security header", hsts, "HSTS on" if hsts else "Not verified via Firecrawl" if not headers else "No HSTS"),
    ]})

    for sec in S:
        p = sum(1 for c in sec["checks"] if c["status"]=="PASS")
        sec["pass"]=p; sec["total"]=len(sec["checks"]); sec["score"]=round(100*p/len(sec["checks"]))
    tp = sum(s["pass"] for s in S); tt = sum(s["total"] for s in S)
    # Aggregate every check into the foundational tool's scan-item ids.
    # A leak is a leak: if any check mapping to an id is a GAP, the id is a gap.
    scan_map = {}
    for sec in S:
        for c in sec["checks"]:
            sid = LABEL_TO_SID.get(c["label"])
            if not sid:
                continue
            if c["status"] == "GAP":
                scan_map[sid] = "gap"
            elif sid not in scan_map:
                scan_map[sid] = "pass"
    return {"url":final_url,"scanned_score":round(100*tp/tt),"pass":tp,"total":tt,
            "sections":S,"scan_map":scan_map}


def run_scan(url, use_firecrawl=True):
    if not url.startswith("http"):
        url = "https://" + url
    if use_firecrawl and os.environ.get("FIRECRAWL_API_KEY"):
        html, final_url, headers, status = firecrawl_fetch(url)
    else:
        html, final_url, headers, status = plain_fetch(url)
    base = "{u.scheme}://{u.netloc}".format(u=urlparse(final_url))
    rb_s, rb_t, _ = aux_get("/robots.txt", base)
    sm_s, _, sm_h = aux_get("/sitemap.xml", base)
    lm_s, _, _ = aux_get("/llms.txt", base)
    robots = rb_t if (rb_s==200) else None
    sitemap = "ok" if (sm_s==200 and "xml" in sm_h.get("content-type","").lower()) else None
    llms = "ok" if lm_s==200 else None
    res = scan_core(html, final_url, headers, robots, sitemap, llms)
    res["fetch"] = "firecrawl" if (use_firecrawl and os.environ.get("FIRECRAWL_API_KEY")) else "direct"
    res["http_status"] = status
    return res


# ----------------------------------------------------------------------
# WEB SERVICE
# ----------------------------------------------------------------------
app = Flask(__name__)
CORS(app)  # allow the GHL dashboard to call this. Restrict to your domain in production.


@app.route("/", methods=["GET"])
def health():
    return jsonify({"service": "vidian-scan-engine", "status": "ok", "has_key": bool(os.environ.get("FIRECRAWL_API_KEY"))})


@app.route("/scan", methods=["POST"])
def scan_endpoint():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Provide a 'url' in the JSON body."}), 400
    try:
        res = run_scan(url, use_firecrawl=True)
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _print(res):
    print("\n" + "="*64)
    print(f"  VIDIAN WEBSITE SCAN  ·  {res['url']}   [fetch: {res.get('fetch')}]")
    print(f"  SCORE: {res['scanned_score']}/100   ({res['pass']}/{res['total']} passed)")
    print("="*64)
    for sec in res["sections"]:
        print(f"\n  {sec['name'].upper()}  —  {sec['score']}/100  ({sec['pass']}/{sec['total']})")
        print("  " + "-"*60)
        for c in sec["checks"]:
            print(f"   [{'PASS' if c['status']=='PASS' else 'GAP '}] {c['label']}")
            if c["detail"]: print(f"          -> {c['detail']}")
    print()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].startswith(("http", "www", "-")) is False and "." in sys.argv[1]:
        sys.argv.insert(1, sys.argv[1])  # no-op guard
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--serve"):
        res = run_scan(sys.argv[1], use_firecrawl=bool(os.environ.get("FIRECRAWL_API_KEY")))
        _print(res)
        if "--json" in sys.argv:
            out = sys.argv[sys.argv.index("--json")+1]
            json.dump(res, open(out,"w"), indent=2); print(f"JSON -> {out}")
    else:
        port = int(os.environ.get("PORT", 8000))
        app.run(host="0.0.0.0", port=port)
