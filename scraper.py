#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aggregate Yelp review pages saved as HTML and write data.json + reviews.csv.

Usage examples:
  # scan default places: ./Curl/*.html and ./pages/*.html
  python3 aggregate_offline_yelp.py

  # or specify your own input globs (repeat --html-glob as needed)
  python3 aggregate_offline_yelp.py --html-glob "Curl/*.html" --html-glob "pages/*.html" \
                                    --out-json data.json --out-csv reviews.csv --min 15
"""
import argparse, csv, html, json, re
from pathlib import Path
from typing import List, Dict, Tuple
from bs4 import BeautifulSoup

BLOCK_TOKENS = ("you have been blocked", "are you not a robot", "captcha", "temporarily unavailable")

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def unesc(s: str) -> str:
    return html.unescape(s or "")

def looks_blocked(text: str) -> bool:
    lo = text.lower()
    return any(t in lo for t in BLOCK_TOKENS)

def parse_json_blobs(raw_html: str) -> Tuple[List[Dict], Dict[str, str]]:
    """
    Yelp pages often embed a big JSON object inside <script> tags.
    This pulls Review and User nodes out of that graph.
    Returns (reviews, users_by_id)
    """
    soup = BeautifulSoup(raw_html, "lxml")
    reviews, users = [], {}

    for tag in soup.find_all("script"):
        txt = tag.string if tag.string is not None else tag.get_text()
        if not txt:
            continue
        u = unesc(txt).strip()
        if u.startswith("<!--") and u.endswith("-->"):
            u = u[4:-3].strip()

        try:
            data = json.loads(u)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        for key, node in data.items():
            if not isinstance(node, dict):
                continue
            t = node.get("__typename")
            if t == "User":
                uid = key.split(":", 1)[-1]
                name = unesc(node.get("displayName") or "")
                if uid and name:
                    users[uid] = name
            elif t == "Review":
                text = (node.get("text") or {}).get("full") or (node.get("text") or {}).get("plain") or ""
                if not text.strip():
                    continue
                reviews.append({
                    "review_id": node.get("encid") or node.get("reviewId") or key,
                    "author_ref": (node.get("author") or {}).get("__ref", "").split(":", 1)[-1],
                    "stars": node.get("rating"),
                    "date": (node.get("createdAt") or {}).get("localDateTimeForBusiness") or node.get("localizedDate") or "",
                    "text": norm(text),
                })
    return reviews, users

def parse_dom_fallback(raw_html: str) -> List[Dict]:
    """
    Very defensive DOM parsing if JSON blobs are not present.
    Works on many static copies, but not on block pages.
    """
    soup = BeautifulSoup(raw_html, "lxml")
    blocks = soup.select('[data-testid="review"]')
    if not blocks:
        blocks = soup.select("section[aria-label*='Review'] article, li.review, div.review, ul.list__09f24__ynIEd > li")

    out = []
    for b in blocks:
        # reviewer
        reviewer = None
        for sel in (".user-passport-info span a", "[data-testid='author-name']", "a[href*='/user_details']", ".user-display-name", "strong"):
            el = b.select_one(sel)
            if el:
                reviewer = el.get_text(" ", strip=True)
                break

        # rating
        rating = None
        star = b.select_one('div[role="img"][aria-label*="star"]') or b.select_one('[aria-label*="star"]')
        if star and star.has_attr("aria-label"):
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*star", star["aria-label"], re.I)
            if m: rating = float(m.group(1))

        # date
        date = ""
        for sel in ("span.y-css-1vi7y4e", "[data-testid='review-date']", "time", "span:has(time)"):
            el = b.select_one(sel)
            if el:
                date = el.get_text(" ", strip=True)
                break
        if not date:
            t = b.find("time")
            if t and t.has_attr("datetime"):
                date = t["datetime"]

        # text
        text = ""
        for sel in ("div:nth-of-type(4) p span", "[data-testid='review-comment']", "span.break-words", "p"):
            el = b.select_one(sel)
            if el:
                text = el.get_text(" ", strip=True)
                break

        if reviewer or rating or text:
            out.append({
                "review_id": None,
                "reviewer": reviewer or "",
                "stars": rating,
                "date": date,
                "text": norm(text),
            })
    return out

def attach_names(reviews: List[Dict], users: Dict[str, str]) -> None:
    for r in reviews:
        if "author_ref" in r:
            r["reviewer"] = users.get(r.pop("author_ref", ""), "")  # set and remove ref key

def dedupe(reviews: List[Dict]) -> List[Dict]:
    seen, out = set(), []
    for r in reviews:
        if not r.get("text"):  # skip empties
            continue
        key = r.get("review_id") or f"{r.get('reviewer','')}|{r.get('date','')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    out.sort(key=lambda x: x.get("date",""), reverse=True)
    return out

def write_outputs(rows: List[Dict], out_json: Path, out_csv: Path) -> None:
    # SLUview-friendly JSON
    payload = {"reviews": [
        {"reviewer": r.get("reviewer",""),
         "rating": r.get("stars"),
         "date": r.get("date",""),
         "text": r.get("text","")}
        for r in rows
    ]}
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # CSV (more columns)
    fields = ["review_id", "reviewer", "stars", "date", "text"]
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html-glob", action="append",
                    help="Glob(s) to HTML files (repeatable). Default: Curl/*.html and pages/*.html")
    ap.add_argument("--out-json", default="data.json")
    ap.add_argument("--out-csv",  default="reviews.csv")
    ap.add_argument("--min", type=int, default=15, help="Warn if fewer than this many reviews (default 15)")
    args = ap.parse_args()

    globs = args.html_glob or ["Curl/*.html", "pages/*.html"]
    files: list[Path] = []
    for g in globs:
        files += [Path(p) for p in sorted(Path().glob(g)) if Path(p).is_file()]

    if not files:
        raise SystemExit("[!] No HTML files found. Put your saved pages in ./Curl or ./pages.")

    print("[INFO] Parsing:")
    for fp in files:
        print("  -", fp)

    all_reviews: List[Dict] = []
    for fp in files:
        raw = fp.read_text(encoding="utf-8", errors="ignore")
        if looks_blocked(raw):
            print(f"[WARN] {fp.name}: looks like a block / captcha page. Skipping DOM fallback may be empty.")
        json_reviews, users = parse_json_blobs(raw)
        if json_reviews:
            attach_names(json_reviews, users)
            all_reviews.extend(json_reviews)
        else:
            # fallback
            all_reviews.extend(parse_dom_fallback(raw))
        print(f"[OK] {fp.name}: +{len(json_reviews) or 0} (json) / +{0 if json_reviews else len(parse_dom_fallback(raw))} (dom)")

    final_rows = dedupe(all_reviews)
    write_outputs(final_rows, Path(args.out_json), Path(args.out_csv))
    print(f"[DONE] Wrote {args.out_json} ({len(final_rows)} reviews) and {args.out_csv}")
    if len(final_rows) < args.min:
        print(f"[NOTE] Only {len(final_rows)} reviews. Save more pages (start=10,20,30,...) and rerun.")

if __name__ == "__main__":
    main()
