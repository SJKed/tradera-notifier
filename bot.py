#!/usr/bin/env python3
"""
Tradera watcher: searches Tradera for Warhammer listings and keeps a rolling
list of cheap ones in docs/listings.json, which the PWA in docs/ displays.

Uses Tradera's official API (free developer account required) and only reads
search results - it never bids or buys.

Environment variables:
  TRADERA_APP_ID    your Tradera developer App ID (integer)
  TRADERA_APP_KEY   your Tradera developer App Key (GUID)

Usage:
  python bot.py                 run one check (what GitHub Actions does)
  python bot.py --dry-run       print matches, don't write listings.json
  python bot.py --loop 900      keep running, checking every 900 seconds
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", HERE / "config.json"))
DATA_PATH = Path(os.environ.get("DATA_PATH", HERE / "docs" / "listings.json"))

API_V4 = "https://api.tradera.com/v4/search"
API_V3 = "https://api.tradera.com/v3/SearchService.asmx/Search"
USER_AGENT = "tradera-watcher/1.0 (personal search alerts)"
MAX_SEEN = 20000          # cap on remembered listing ids
KEEP_DAYS = 30            # drop listings found longer ago than this


# --------------------------------------------------------------------------- #
# Tradera API
# --------------------------------------------------------------------------- #

def _http(req: urllib.request.Request, timeout: int = 30) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _lower_keys(obj):
    """Recursively lower-case dict keys so v3 (PascalCase) and v4 (camelCase) look the same."""
    if isinstance(obj, dict):
        return {str(k).lower(): _lower_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_lower_keys(v) for v in obj]
    return obj


def parse_v4(body: bytes) -> list[dict]:
    data = _lower_keys(json.loads(body))
    items = data.get("items") or data.get("searchitems") or []
    return [i for i in items if isinstance(i, dict)]


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _xml_to_dict(el: ET.Element) -> dict:
    out = {}
    for child in el:
        key = _strip_ns(child.tag).lower()
        if len(child):
            out[key] = _xml_to_dict(child)
        else:
            out[key] = (child.text or "").strip()
    return out


def parse_v3(body: bytes) -> list[dict]:
    root = ET.fromstring(body)
    items = []
    for el in root.iter():
        tag = _strip_ns(el.tag)
        if tag in ("Items", "SearchItem"):
            child_tags = {_strip_ns(c.tag) for c in el}
            if "Id" in child_tags:
                items.append(_xml_to_dict(el))
    return items


def search(query: str, page: int, app_id: str, app_key: str) -> list[dict]:
    """One page of results, cheapest first. Tries REST v4, falls back to SOAP v3."""
    params = {"query": query, "categoryId": 0, "pageNumber": page, "orderBy": "PriceAscending"}
    try:
        req = urllib.request.Request(
            f"{API_V4}?{urllib.parse.urlencode(params)}",
            headers={
                "X-App-Id": str(app_id),
                "X-App-Key": app_key,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        return parse_v4(_http(req))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429):
            raise  # credentials or rate limit: v3 won't help
        print(f"  v4 search failed ({e}), trying v3", file=sys.stderr)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        print(f"  v4 search failed ({e}), trying v3", file=sys.stderr)

    params_v3 = {"appId": app_id, "appKey": app_key, **params}
    req = urllib.request.Request(
        f"{API_V3}?{urllib.parse.urlencode(params_v3)}",
        headers={"User-Agent": USER_AGENT},
    )
    return parse_v3(_http(req))


# --------------------------------------------------------------------------- #
# Listing helpers
# --------------------------------------------------------------------------- #

def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1")


def normalize(raw: dict) -> dict:
    item_id = _int(raw.get("id"))
    title = raw.get("shortdescription") or raw.get("title") or ""
    bin_price = _int(raw.get("buyitnowprice"))
    total_bids = _int(raw.get("bidcount") or raw.get("totalbids"))
    has_bids = _bool(raw.get("hasbids")) or total_bids > 0
    max_bid = _int(raw.get("maxbid"))
    next_bid = _int(raw.get("nextbid"))
    opening = _int(raw.get("openingbid"))
    item_type = str(raw.get("itemtype") or "").lower()

    pure_bin = item_type in ("3", "4", "purebuyitnow", "shopitem") or (
        bin_price > 0 and not next_bid and not opening and not max_bid
    )
    if pure_bin:
        auction_price = 0
    elif has_bids and max_bid:
        auction_price = max_bid
    else:
        auction_price = next_bid or opening or max_bid

    url = raw.get("itemurl") or raw.get("itemlink") or ""
    if not url and item_id:
        url = f"https://www.tradera.com/item/{_int(raw.get('categoryid'))}/{item_id}/"

    thumb = raw.get("thumbnaillink") or ""
    if not thumb and isinstance(raw.get("imagelinks"), list) and raw["imagelinks"]:
        thumb = raw["imagelinks"][0]

    return {
        "id": item_id,
        "title": title,
        "bin": bin_price,
        "auction": auction_price,
        "bids": total_bids,
        "url": url,
        "thumb": thumb if isinstance(thumb, str) else "",
        "end": raw.get("enddate") or "",
    }


def is_cheap(item: dict, max_price: int, include_auctions: bool, auction_max: int | None) -> bool:
    if 0 < item["bin"] <= max_price:
        return True
    if include_auctions and item["auction"] > 0:
        cap = auction_max if auction_max is not None else max_price
        return item["auction"] <= cap
    return False


def excluded(title: str, words: list[str]) -> bool:
    t = f" {title.lower()} "
    return any(w.lower() in t for w in words)


def lowest_price(item: dict) -> int:
    prices = [p for p in (item["bin"], item["auction"]) if p > 0]
    return min(prices) if prices else 0


# --------------------------------------------------------------------------- #
# Storage (docs/listings.json, read by the PWA)
# --------------------------------------------------------------------------- #

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # Tradera times without offset are Swedish local time
        dt = dt.replace(tzinfo=timezone(timedelta(hours=1)))
    return dt


def load_data() -> dict:
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_data(data: dict) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["seen"] = data.get("seen", [])[-MAX_SEEN:]
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def prune(listings: list[dict], now: datetime) -> list[dict]:
    keep = []
    for l in listings:
        end = _parse_dt(l.get("end", ""))
        found = _parse_dt(l.get("found", "")) or now
        if end and end < now:
            continue
        if now - found > timedelta(days=KEEP_DAYS):
            continue
        keep.append(l)
    return keep


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run_once(cfg: dict, dry_run: bool = False, search_fn=search) -> list[dict]:
    """Search everything, update listings.json. Returns newly found listings."""
    app_id = os.environ.get("TRADERA_APP_ID")
    app_key = os.environ.get("TRADERA_APP_KEY")
    if not (app_id and app_key) and search_fn is search:
        raise SystemExit("TRADERA_APP_ID and TRADERA_APP_KEY must be set")

    settings = cfg.get("settings", {})
    max_pages = int(settings.get("max_pages", 3))
    include_auctions = bool(settings.get("include_auctions", True))
    auction_max = settings.get("auction_max_price") or None

    now = _now()
    data = load_data()
    seen = set(data.get("seen", []))
    listings = {l["id"]: l for l in data.get("listings", [])}
    new: list[dict] = []
    errors = 0

    for watch in cfg.get("watch", []):
        name = watch.get("name") or "Unnamed"
        max_price = _int(watch.get("max_price"))
        exclude = [w for w in watch.get("exclude", []) if w.strip()]
        queries = [q.strip() for q in watch.get("queries", []) if q.strip()]
        if not queries or max_price <= 0:
            print(f"[{name}] skipped: needs search words and a max price", file=sys.stderr)
            continue
        cap = max(max_price, auction_max or 0)
        for query in queries:
            for page in range(1, max_pages + 1):
                try:
                    raw_items = search_fn(query, page, app_id, app_key)
                except Exception as e:  # keep going with other searches
                    print(f"[{name}] '{query}' page {page}: {e}", file=sys.stderr)
                    errors += 1
                    break
                if not raw_items:
                    break
                items = [normalize(r) for r in raw_items]
                for item in items:
                    if not item["id"]:
                        continue
                    cheap = is_cheap(item, max_price, include_auctions, auction_max)
                    if item["id"] in listings:
                        # Already tracked: refresh price, bids and end time.
                        l = listings[item["id"]]
                        l.update({k: item[k] for k in ("title", "bin", "auction", "bids", "end", "thumb")})
                        l["over_budget"] = not cheap
                        continue
                    if item["id"] in seen or excluded(item["title"], exclude) or not cheap:
                        continue
                    entry = {**item, "watch": name, "found": _iso(now), "over_budget": False}
                    listings[item["id"]] = entry
                    seen.add(item["id"])
                    new.append(entry)
                # Sorted cheapest first: once a whole page is over budget, stop paging.
                if all(lowest_price(i) > cap for i in items if lowest_price(i)):
                    break
                time.sleep(0.5)

    for l in new:
        print(f"NEW  {l['watch']}: {lowest_price(l)} kr | {l['title']} | {l['url']}")
    if not new:
        print("No new cheap listings.")

    if not dry_run:
        # Drop listings from searches you have deleted in the app.
        # (Their ids are forgotten too, so a renamed search finds them again.)
        names = {w["name"] for w in cfg.get("watch", [])}
        dropped = {l["id"] for l in listings.values() if l.get("watch") not in names}
        kept = prune([l for l in listings.values() if l["id"] not in dropped], now)
        kept.sort(key=lambda l: l.get("found", ""), reverse=True)
        data.update({
            "updated": _iso(now),
            "errors": errors,
            "watches": [{"name": w["name"], "max_price": w["max_price"]} for w in cfg.get("watch", [])],
            "listings": kept,
            "seen": [i for i in data.get("seen", []) if i not in dropped] + [l["id"] for l in new],
        })
        save_data(data)
    return new


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", type=int, metavar="SECONDS")
    args = ap.parse_args()

    while True:
        cfg = json.loads(CONFIG_PATH.read_text())  # re-read: the app may have edited it
        run_once(cfg, dry_run=args.dry_run)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
