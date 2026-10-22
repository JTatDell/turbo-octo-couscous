#!/usr/bin/env python3
"""
jt-webcrawler: polite same-site crawler that renders each public HTML page to PDF
(headless Chromium) and downloads any linked PDFs as-is. Output is a RAG-ready corpus
plus a manifest.

Usage (--url and --email are required):
    python3 jt-webcrawler.py --url https://www.example.com/ --email you@example.com \
        --out ./out --max-pages 300 --delay 1.5

Behaviour
- Honors robots.txt (Disallow and Crawl-delay) for the configured user agent.
- Seeds from sitemap(s) listed in robots.txt or /sitemap.xml, then breadth-first links.
- Same host only; skips login/account/cart/search-style paths and binary assets.
- Each PDF gets a header/footer with the source URL and retrieval time (UTC), so
  retrieved chunks can be cited back to the live page.
- Resumable: rerun with the same --out and it skips URLs already in manifest.csv.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

import requests
from playwright.sync_api import sync_playwright

TOOL_NAME = "jt-webcrawler"
try:  # single source of truth: VERSION file shipped next to this script
    with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "VERSION"), encoding="utf-8") as _vf:
        TOOL_VERSION = _vf.read().strip() or "unknown"
except OSError:
    TOOL_VERSION = "unknown"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SKIP_EXT = re.compile(
    r"\.(jpg|jpeg|png|gif|svg|webp|ico|bmp|tiff?|mp4|mp3|wav|avi|mov|webm|zip|gz|tgz|rar|7z|"
    r"exe|dmg|msi|css|js|json|xml|rss|atom|woff2?|ttf|eot|ics|vcf|xlsx?|pptx?|docx?|csv)$",
    re.I,
)
DEFAULT_EXCLUDE = (
    r"/(login|log-in|signin|sign-in|logout|sign-out|register|signup|sign-up|account|my-account|"
    r"myaccount|cart|checkout|basket|wishlist|search|password|subscribe/checkout)(/|$|\?)"
    r"|[?&](q|query|search|s|sort|filter|returnurl|redirect)="
)
TRACKING_PARAMS = re.compile(r"^(utm_.*|gclid|fbclid|mc_cid|mc_eid|_ga|ref|source)$", re.I)

# Hide common cookie/consent overlays and sticky chrome before printing.
PRINT_CSS = """
[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i],
#onetrust-banner-sdk, #onetrust-consent-sdk, .osano-cm-window, #CybotCookiebotDialog,
[class*="newsletter-popup" i], [class*="modal-backdrop" i] { display: none !important; }
* { position: static !important; }
"""

MANIFEST_FIELDS = [
    "url", "final_url", "title", "http_status", "content_type", "kind",
    "file", "bytes", "sha256", "retrieved_at_utc", "note",
]


def normalize(url: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", urlencode(sorted(q)), ""))


def slug_for(url: str, used: set[str]) -> str:
    p = urlparse(url)
    base = (p.path.strip("/") or "home").replace("/", "__")
    if p.query:
        base += "__" + hashlib.sha1(p.query.encode()).hexdigest()[:8]
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base)[:150].strip("-") or "page"
    base = re.sub(r"\.pdf$", "", base, flags=re.I)
    name, i = base, 2
    while name in used:
        name, i = f"{base}-{i}", i + 1
    used.add(name)
    return name + ".pdf"


def fetch_sitemap_urls(session: requests.Session, sitemap_url: str, host: str, limit: int, seen=None) -> list[str]:
    seen = seen if seen is not None else set()
    if sitemap_url in seen or len(seen) > 50:
        return []
    seen.add(sitemap_url)
    try:
        r = session.get(sitemap_url, timeout=30)
        if r.status_code != 200:
            return []
        data = r.content
        if sitemap_url.endswith(".gz") or data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        root = ET.fromstring(data)
    except Exception as e:  # noqa: BLE001
        print(f"  sitemap skipped {sitemap_url}: {e}", file=sys.stderr)
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    out: list[str] = []
    for loc in root.findall(".//sm:sitemap/sm:loc", ns):
        out += fetch_sitemap_urls(session, loc.text.strip(), host, limit, seen)
        if len(out) >= limit:
            return out[:limit]
    for loc in root.findall(".//sm:url/sm:loc", ns):
        u = loc.text.strip()
        if urlparse(u).netloc.lower() == host:
            out.append(u)
    return out[:limit]


def load_manifest(path: str) -> tuple[set[str], set[str], set[str]]:
    done, files, hashes = set(), set(), set()
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add(row["url"])
                if row.get("file"):
                    files.add(os.path.splitext(os.path.basename(row["file"]))[0])
                if row.get("sha256"):
                    hashes.add(row["sha256"])
    return done, files, hashes


def main() -> int:
    ap = argparse.ArgumentParser(prog=TOOL_NAME, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="target site start URL, e.g. https://www.irmi.com/")
    ap.add_argument("--email", required=True, help="contact email published in the User-Agent so site operators can reach you")
    ap.add_argument("--out", default=None, help="output folder (default: ./out-<host>)")
    ap.add_argument("--max-pages", type=int, default=300, help="cap on documents written (HTML->PDF plus native PDFs)")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests (raised to robots Crawl-delay)")
    ap.add_argument("--exclude", default=DEFAULT_EXCLUDE, help="regex of URLs to skip")
    ap.add_argument("--include", default=None, help="optional regex; if set, only matching URLs are saved (links still followed)")
    ap.add_argument("--no-sitemap", action="store_true")
    ap.add_argument("--save-html", action="store_true", help="also keep rendered HTML next to each PDF")
    ap.add_argument("--chromium", default=os.environ.get("CHROMIUM_PATH"), help="path to a Chromium binary (optional)")
    ap.add_argument("--ignore-robots", action="store_true", help="ONLY for local testing against your own server")
    args = ap.parse_args()

    if urlparse(args.url).scheme not in ("http", "https") or not urlparse(args.url).netloc:
        ap.error(f"--url must be an absolute http(s) URL, got: {args.url!r}")
    if not EMAIL_RE.match(args.email):
        ap.error(f"--email does not look like an email address: {args.email!r}")
    args.user_agent = f"{TOOL_NAME}/{TOOL_VERSION} (+mailto:{args.email})"

    start = normalize(args.url)
    host = urlparse(start).netloc
    scheme = urlparse(start).scheme
    if args.out is None:
        args.out = "./out-" + re.sub(r"[^A-Za-z0-9.-]+", "_", host)
    pdf_dir = os.path.join(args.out, "pdf")
    html_dir = os.path.join(args.out, "html")
    os.makedirs(pdf_dir, exist_ok=True)
    if args.save_html:
        os.makedirs(html_dir, exist_ok=True)
    manifest_path = os.path.join(args.out, "manifest.csv")
    done, used_names, seen_hashes = load_manifest(manifest_path)
    new_manifest = not os.path.exists(manifest_path)
    mf = open(manifest_path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(mf, fieldnames=MANIFEST_FIELDS)
    if new_manifest:
        writer.writeheader()

    session = requests.Session()
    session.headers["User-Agent"] = args.user_agent

    # robots.txt
    rp = RobotFileParser()
    robots_url = f"{scheme}://{host}/robots.txt"
    sitemaps: list[str] = []
    if args.ignore_robots:
        rp.parse([])
    else:
        r = session.get(robots_url, timeout=30)
        if r.status_code >= 500 or r.status_code in (401, 403):
            print(f"robots.txt returned {r.status_code}; treating site as disallowed. Aborting.", file=sys.stderr)
            return 2
        lines = r.text.splitlines() if r.status_code == 200 else []
        rp.parse(lines)
        sitemaps = [l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("sitemap:")]
        cd = rp.crawl_delay(args.user_agent)
        if cd:
            args.delay = max(args.delay, float(cd))
    print(f"{TOOL_NAME} {TOOL_VERSION} | user-agent: {args.user_agent}")
    print(f"host={host} delay={args.delay}s max_pages={args.max_pages} robots={'ignored' if args.ignore_robots else robots_url}")

    exclude = re.compile(args.exclude, re.I) if args.exclude else None
    include = re.compile(args.include, re.I) if args.include else None

    def allowed(u: str) -> bool:
        p = urlparse(u)
        if p.scheme not in ("http", "https") or p.netloc.lower() != host:
            return False
        if SKIP_EXT.search(p.path) or (exclude and exclude.search(u)):
            return False
        return args.ignore_robots or rp.can_fetch(args.user_agent, u)

    frontier: deque[str] = deque()
    queued: set[str] = set()

    def enqueue(u: str, front: bool = False):
        n = normalize(u)
        if n in queued or n in done or not allowed(n):
            return
        queued.add(n)
        frontier.appendleft(n) if front else frontier.append(n)

    frontier_path = os.path.join(args.out, "frontier.txt")
    enqueue(start)
    if os.path.exists(frontier_path):  # resume: restore links discovered by the previous run
        with open(frontier_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    enqueue(line.strip())

    def save_frontier():
        with open(frontier_path, "w", encoding="utf-8") as f:
            f.write("\n".join(frontier))

    if not args.no_sitemap:
        sm_list = sitemaps or [f"{scheme}://{host}/sitemap.xml"]
        sm_urls: list[str] = []
        for sm in sm_list:
            sm_urls += fetch_sitemap_urls(session, sm, host, limit=args.max_pages * 5)
        print(f"sitemap URLs found: {len(sm_urls)}")
        for u in sm_urls:
            enqueue(u)

    written = len(used_names)  # documents already saved by a previous run

    launch_kw: dict = {"headless": True}
    if args.chromium:
        launch_kw["executable_path"] = args.chromium
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        launch_kw["proxy"] = {"server": proxy, "bypass": os.environ.get("NO_PROXY", "localhost,127.0.0.1")}

    def record(**kw):
        row = {k: "" for k in MANIFEST_FIELDS}
        row.update(kw)
        row["retrieved_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        writer.writerow(row)
        mf.flush()
        done.add(kw["url"])

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kw)
        ctx = browser.new_context(user_agent=args.user_agent, ignore_https_errors=False)
        page = ctx.new_page()
        last = 0.0
        while frontier and written < args.max_pages:
            url = frontier.popleft()
            if len(done) % 10 == 0:
                save_frontier()
            wait = args.delay - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            last = time.time()

            # Cheap HEAD-ish probe to route native PDFs without rendering.
            if url.lower().endswith(".pdf"):
                try:
                    r = session.get(url, timeout=60)
                    ok = r.status_code == 200 and r.content[:4] == b"%PDF"
                    if ok:
                        sha = hashlib.sha256(r.content).hexdigest()
                        if sha in seen_hashes:
                            record(url=url, final_url=r.url, http_status=r.status_code, kind="pdf", note="duplicate content")
                            continue
                        seen_hashes.add(sha)
                        name = slug_for(url, used_names)
                        with open(os.path.join(pdf_dir, name), "wb") as f:
                            f.write(r.content)
                        written += 1
                        record(url=url, final_url=r.url, http_status=r.status_code, content_type="application/pdf",
                               kind="pdf", file=f"pdf/{name}", bytes=len(r.content), sha256=sha)
                        print(f"[{written}] PDF  {url}")
                    else:
                        record(url=url, http_status=r.status_code, kind="pdf", note="not a PDF or non-200")
                except Exception as e:  # noqa: BLE001
                    record(url=url, kind="pdf", note=f"error: {e}"[:300])
                continue

            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:  # noqa: BLE001
                    pass
            except Exception as e:  # noqa: BLE001
                record(url=url, kind="html", note=f"nav error: {e}"[:300])
                print(f"  ! {url}: {e}", file=sys.stderr)
                continue

            status = resp.status if resp else 0
            ctype = (resp.headers.get("content-type", "") if resp else "").split(";")[0]
            final = normalize(page.url)
            if urlparse(final).netloc.lower() != host:
                record(url=url, final_url=final, http_status=status, kind="html", note="redirected off-site")
                continue
            if final != url and final in done:
                record(url=url, final_url=final, http_status=status, kind="html", note="redirect to already-saved page")
                continue

            # Harvest links before any DOM changes.
            try:
                hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            except Exception:  # noqa: BLE001
                hrefs = []
            for h in hrefs:
                enqueue(urljoin(final, h))

            if status != 200 or "html" not in ctype:
                record(url=url, final_url=final, http_status=status, content_type=ctype, kind="html", note="skipped: status/content-type")
                continue
            if include and not include.search(final):
                record(url=url, final_url=final, http_status=status, content_type=ctype, kind="html", note="followed only (not in --include)")
                continue

            title = (page.title() or "").strip()
            text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            sha = hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode()).hexdigest()
            if len(text.strip()) < 200:
                record(url=url, final_url=final, title=title, http_status=status, content_type=ctype, kind="html", note="thin page (<200 chars)")
                continue
            if sha in seen_hashes:
                record(url=url, final_url=final, title=title, http_status=status, content_type=ctype, kind="html", sha256=sha, note="duplicate content")
                continue
            seen_hashes.add(sha)

            name = slug_for(final, used_names)
            out_path = os.path.join(pdf_dir, name)
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
            try:
                page.add_style_tag(content=PRINT_CSS)
                page.emulate_media(media="print")
                page.pdf(
                    path=out_path, format="Letter", print_background=True,
                    margin={"top": "0.7in", "bottom": "0.7in", "left": "0.5in", "right": "0.5in"},
                    display_header_footer=True,
                    header_template=f'<div style="font-size:7px;width:100%;padding:0 0.5in;color:#555">{esc(title)[:150]}</div>',
                    footer_template=(
                        f'<div style="font-size:7px;width:100%;padding:0 0.5in;color:#555;display:flex;justify-content:space-between">'
                        f'<span>Source: {esc(final)} | Retrieved {stamp}</span>'
                        f'<span><span class="pageNumber"></span>/<span class="totalPages"></span></span></div>'
                    ),
                )
                page.emulate_media(media="screen")
            except Exception as e:  # noqa: BLE001
                record(url=url, final_url=final, title=title, http_status=status, content_type=ctype, kind="html", note=f"pdf error: {e}"[:300])
                continue
            if args.save_html:
                with open(os.path.join(html_dir, name[:-4] + ".html"), "w", encoding="utf-8") as f:
                    f.write(page.content())
            written += 1
            record(url=url, final_url=final, title=title, http_status=status, content_type=ctype, kind="html",
                   file=f"pdf/{name}", bytes=os.path.getsize(out_path), sha256=sha)
            print(f"[{written}] HTML {final}  ->  {name}")

        browser.close()
    save_frontier()
    mf.close()
    print(f"done: {written} documents in {pdf_dir}; frontier remaining {len(frontier)}; manifest {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
