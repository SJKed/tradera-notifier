#!/usr/bin/env python3
"""
Tradera watcher: searches Tradera for Warhammer listings, keeps a rolling
list of cheap ones in docs/listings.json (shown by the PWA in docs/), and sends
a Web Push notification for each new find to devices in push/subscriptions.json.

Uses Tradera's official API (free developer account required) and only reads
search results - it never bids or buys.

Environment variables:
  TRADERA_APP_ID    your Tradera developer App ID (integer)
  TRADERA_APP_KEY   your Tradera developer App Key (GUID)
  VAPID_PRIVATE_KEY optional PEM push signing key (otherwise one is made in state/)

Usage:
  python bot.py                 run one check (what GitHub Actions does)
  python bot.py --dry-run       print matches, don't write listings.json
  python bot.py --loop 900      keep running, checking every 900 seconds
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
STATE_DIR = Path(os.environ.get("STATE_DIR", HERE / "state"))   # cached by Actions, not committed
VAPID_PATH = STATE_DIR / "vapid.pem"
PUSH_STATE_PATH = STATE_DIR / "push.json"
PUSH_PUBLIC_PATH = DATA_PATH.parent / "push.json"
SUBS_PATH = Path(os.environ.get("SUBS_PATH", HERE / "push" / "subscriptions.json"))

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


_pattern_cache: dict[str, re.Pattern] = {}


def _word_pattern(word: str) -> re.Pattern | None:
    """Whole-word match ("paint" hides "Paint" but not "painted"). A trailing *
    matches the start of a word instead ("paint*" also hides "painted")."""
    w = word.strip().lower()
    if not w or w == "*":
        return None
    if w not in _pattern_cache:
        prefix = w.endswith("*")
        core = re.escape(w.rstrip("*").strip())
        _pattern_cache[w] = re.compile(rf"(?<!\w){core}" + ("" if prefix else r"(?!\w)"))
    return _pattern_cache[w]


def excluded(title: str, words: list[str]) -> bool:
    t = title.lower()
    return any(p.search(t) for w in words if (p := _word_pattern(w)))


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
# Web Push notifications
# --------------------------------------------------------------------------- #
# The signing ("VAPID") key is created on the first run and kept in state/,
# which GitHub Actions caches between runs - it never goes in the repo. Only
# the public half is published (docs/push.json) so the app can subscribe.
# Devices subscribe from the app, which commits them to push/subscriptions.json.

MAX_PUSHES = 5  # more new listings than this in one run -> a summary push instead


def _b64url(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def ensure_vapid() -> tuple[bytes, str]:
    """Return (private key PEM, public key as base64url), creating the key if needed."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = os.environ.get("VAPID_PRIVATE_KEY", "").encode() or None
    if not pem and VAPID_PATH.exists():
        pem = VAPID_PATH.read_bytes()
    if pem:
        key = serialization.load_pem_private_key(pem, password=None)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        VAPID_PATH.parent.mkdir(parents=True, exist_ok=True)
        VAPID_PATH.write_bytes(pem)
        print("Created a new push signing key; devices will re-subscribe next time the app opens.")
    pub = key.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    return pem, _b64url(pub)


def app_url() -> str:
    if os.environ.get("APP_URL"):
        return os.environ["APP_URL"]
    owner, _, repo = os.environ.get("GITHUB_REPOSITORY", "SJKed/tradera-notifier").partition("/")
    return f"https://{owner.lower()}.github.io/{repo}/"


def load_subscriptions() -> list[dict]:
    try:
        subs = json.loads(SUBS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return [s for s in subs if isinstance(s, dict) and s.get("endpoint") and s.get("keys")]


def _sub_id(endpoint: str) -> str:
    import hashlib
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


def push_messages(new: list[dict]) -> list[dict]:
    def one(l):
        if l["bin"] and l["auction"]:
            kind = f"Buy now · bid {l['auction']} kr"
        elif l["bin"]:
            kind = "Buy now"
        else:
            kind = f"Auction · {l['bids']} bids" if l["bids"] else "Auction · no bids"
        if l.get("bargain"):
            return {"title": f"Bargain: {l['auction']} kr ({l['msrp_pct']}% of MSRP)",
                    "body": f"{l['title']}\n{kind} · MSRP ~{l['msrp']} kr",
                    "url": l["url"], "image": l.get("thumb") or "", "tag": f"item-{l['id']}"}
        return {"title": f"{l['watch']}: {lowest_price(l)} kr", "body": f"{l['title']}\n{kind}",
                "url": l["url"], "image": l.get("thumb") or "", "tag": f"item-{l['id']}"}
    if len(new) <= MAX_PUSHES:
        return [one(l) for l in new]
    cheapest = sorted(new, key=lowest_price)[:3]
    lines = "\n".join(f"{lowest_price(l)} kr · {l['title']}" for l in cheapest)
    return [{"title": f"{len(new)} new bargains", "body": lines, "url": "", "tag": "summary"}]


def send_pushes(new: list[dict], quiet: bool) -> None:
    """Notify every subscribed device. quiet=True only sends welcome pushes."""
    try:
        pem, pub = ensure_vapid()
    except Exception as e:
        print(f"Push disabled: {e}", file=sys.stderr)
        return
    PUSH_PUBLIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    PUSH_PUBLIC_PATH.write_text(json.dumps({"publicKey": pub}))

    subs = [s for s in load_subscriptions() if s.get("publicKey") == pub]
    if not subs:
        return
    try:
        from py_vapid import Vapid02  # RFC 8292 "vapid t=, k=" header (needed by Apple)
        from pywebpush import WebPushException, webpush
    except ImportError:
        print("pywebpush not installed; skipping notifications (pip install pywebpush).", file=sys.stderr)
        return

    vapid = Vapid02.from_pem(pem)
    # Push services want a contact in the signature: a bare https://host (no path) works.
    claims = {"sub": "https://" + urllib.parse.urlsplit(app_url()).netloc}
    state = {}
    if PUSH_STATE_PATH.exists():
        try:
            state = json.loads(PUSH_STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    welcomed = set(state.get("welcomed", []))
    messages = [] if quiet else push_messages(new)
    sent = 0

    for s in subs:
        sid = _sub_id(s["endpoint"])
        outbox = list(messages)
        if sid not in welcomed:
            outbox.insert(0, {"title": "Notifications are on",
                              "body": "You'll get a ping here for each new bargain.", "url": "", "tag": "welcome"})
        for msg in outbox:
            try:
                webpush({"endpoint": s["endpoint"], "keys": s["keys"]}, json.dumps(msg),
                        vapid_private_key=vapid, vapid_claims=dict(claims), ttl=86400,
                        headers={"Urgency": "high"}, timeout=15)
                welcomed.add(sid)
                sent += 1
            except WebPushException as e:
                code = getattr(e.response, "status_code", None)
                if code in (404, 410):
                    print(f"Device {s.get('label', sid)} unsubscribed or expired; remove it in the app.")
                    break
                print(f"Push to {s.get('label', sid)} failed: {e}", file=sys.stderr)
            except Exception as e:
                print(f"Push to {s.get('label', sid)} failed: {e}", file=sys.stderr)
    if sent:
        print(f"Sent {sent} notification(s) to {len(subs)} device(s).")
    PUSH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PUSH_STATE_PATH.write_text(json.dumps({"welcomed": sorted(welcomed)}))


# --------------------------------------------------------------------------- #
# MSRP catalog and bargain detection
# --------------------------------------------------------------------------- #
# Games Workshop's own site can't be read by a bot, so the catalog comes from a
# UK retailer's public Shopify feed: each Age of Sigmar kit's GW product code
# (e.g. 99120211003) and GW's UK RRP ("compare at" price). The bot refreshes it
# weekly into state/ and converts £ to kr with a factor you can change in the app.

CATALOG_URL = "https://www.goblingaming.co.uk/collections/warhammer-age-of-sigmar/products.json"
CATALOG_PATH = STATE_DIR / "msrp_catalog.json"
CATALOG_MAX_AGE = timedelta(days=7)

# Faction and range names, longest first, stripped from the front of catalog titles
# ("Maggotkin of Nurgle Plaguebearers of Nurgle" -> kit "Plaguebearers of Nurgle").
FACTIONS = sorted([
    "warhammer age of sigmar", "age of sigmar", "warhammer 40000", "warhammer 40,000", "warhammer 40k",
    "warhammer", "games workshop", "aos", "citadel",
    "helsmiths of hashut", "maggotkin of nurgle", "daemons of nurgle", "blades of khorne",
    "daemons of khorne", "khorne bloodbound", "orruk warclans", "kruleboyz", "ironjawz", "bonesplitterz",
    "lumineth realm-lords", "lumineth realm lords", "lumineth realmlords", "slaves to darkness",
    "disciples of tzeentch", "daemons of tzeentch", "hedonites of slaanesh", "daemons of slaanesh",
    "chaos daemons", "skaven", "stormcast eternals", "seraphon", "sylvaneth", "idoneth deepkin",
    "daughters of khaine", "kharadron overlords", "fyreslayers", "cities of sigmar", "soulblight gravelords",
    "nighthaunt", "ossiarch bonereapers", "flesh-eater courts", "flesh eater courts", "gloomspite gitz",
    "sons of behemat", "ogor mawtribes", "beasts of chaos", "warcry", "warhammer underworlds", "legends",
], key=len, reverse=True)
_GENERIC = {"of", "the", "and", "with", "a", "an", "on", "in", "to", "for", "warhammer", "age",
            "sigmar", "aos", "games", "workshop", "edition", "eng", "english", "new"}
_FACTION_GENERIC = _GENERIC | {"daemons", "chaos", "death", "destruction", "order", "realm", "lords"}

# Titles that suggest part of a kit (a sprue, bits, a single model from a box).
_PARTIAL = re.compile(r"(?<!\w)(spru\w*|spure\w*|bits?|bitz|delar|del av|parts?|arms?|heads?|huvud\w*|"
                      r"rester|incomplete|ofullständig\w*|saknar|saknas|broken|trasig\w*|single|"
                      r"enstaka|kroppar?|torsos?)(?!\w)|#\s*\d")
_ONE = re.compile(r"(?<!\w)1\s*(st|x|stk|styck)(?!\w)")
_GW_CODE = re.compile(r"(?<!\d)99\d{9}(?!\d)")


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 4 and t.endswith("s") and not t.endswith("ss") else t


def _tokens(s: str) -> list[str]:
    """Words, lowercased and de-pluralised. Hyphenated words are joined ("Stab-grot" -> stabgrot)."""
    s = s.lower().replace("’", "'").replace("'", "")
    s = re.sub(r"(?<=\w)-(?=\w)", "", s)
    return [_stem(t) for t in re.sub(r"[^\wåäöé]+", " ", s).split()]


def _title_tokens(s: str) -> set[str]:
    """Listing-side tokens: also hyphen parts and adjacent pairs joined, so
    "Killa-Boss With Stab Grot" and "stabgrot" both meet "Killaboss with Stab-grot"."""
    low = s.lower().replace("’", "'").replace("'", "")
    words = [w for w in re.sub(r"[^\wåäöé]+", " ", low.replace("-", " ")).split()]
    toks = set(_tokens(s)) | {_stem(w) for w in words}
    toks |= {_stem(a + b) for a, b in zip(words, words[1:])}
    return toks


def _head_noun(kit: str) -> str:
    """The main noun of a kit name: last word before of/with/on ("Plaguebearers of Nurgle")."""
    first = re.split(r"\s(?:of|with|on|and|&)\s", kit.lower(), maxsplit=1)[0]
    words = re.sub(r"[^\wåäöé-]+", " ", first).split()
    return words[-1] if words else ""


def _strip_factions(title: str) -> tuple[str, str]:
    """('Maggotkin of Nurgle', 'Plaguebearers of Nurgle') from a catalog title. Handles
    "Faction Kit", "Faction: Kit", "Range - Faction - Kit" and "Kit - Range - Games Workshop"."""
    faction, kit_parts = [], []
    for seg in re.split(r"\s+[-–—|]\s+|:\s+", title.strip()):
        rest = seg.strip()
        changed = True
        while changed and rest:
            changed = False
            low = rest.lower()
            for f in FACTIONS:
                if low.startswith(f) and (len(low) == len(f) or not low[len(f)].isalnum()):
                    faction.append(rest[:len(f)])
                    rest = rest[len(f):].lstrip(" :-–—/|,")
                    changed = True
                    break
        rest = re.sub(r"\s*\((?:19|20)\d\d\)|\s*\(\d+(?:st|nd|rd|th) edition\)", "", rest, flags=re.I).strip()
        if rest:
            kit_parts.append(rest)
    return " ".join(faction), " ".join(kit_parts)


def fetch_catalog() -> list[dict]:
    """Download every Age of Sigmar kit with its GW code and UK RRP."""
    items, seen = [], set()
    for page in range(1, 25):
        req = urllib.request.Request(f"{CATALOG_URL}?limit=250&page={page}",
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        products = json.loads(_http(req, timeout=30)).get("products", [])
        if not products:
            break
        for p in products:
            v = (p.get("variants") or [{}])[0]
            sku = str(v.get("sku") or "").strip()
            try:
                gbp = float(v.get("compare_at_price") or 0) or float(v.get("price") or 0) / 0.85
            except (TypeError, ValueError):
                continue
            if not gbp or not p.get("title") or (sku and sku in seen):
                continue
            seen.add(sku)
            faction, kit = _strip_factions(p["title"])
            items.append({"sku": sku, "title": p["title"], "faction": faction, "kit": kit or p["title"],
                          "gbp": round(gbp, 2)})
        time.sleep(1)
    return items


def load_catalog(now: datetime) -> tuple[list[dict], str]:
    """The cached catalog, refreshed when older than a week. Never fails the run."""
    cached = {}
    if CATALOG_PATH.exists():
        try:
            cached = json.loads(CATALOG_PATH.read_text())
        except json.JSONDecodeError:
            pass
    fetched = _parse_dt(cached.get("fetched", ""))
    if os.environ.get("MSRP_OFFLINE") != "1" and (not fetched or now - fetched > CATALOG_MAX_AGE):
        try:
            items = fetch_catalog()
            if len(items) > 50:
                cached = {"fetched": _iso(now), "items": items}
                CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                CATALOG_PATH.write_text(json.dumps(cached))
                print(f"Updated MSRP catalog: {len(items)} kits.")
            else:
                print(f"MSRP catalog fetch returned only {len(items)} kits; keeping the old one.", file=sys.stderr)
        except Exception as e:
            print(f"MSRP catalog fetch failed ({e}); keeping the old one.", file=sys.stderr)
    return cached.get("items", []), cached.get("fetched", "")


class Pricer:
    """Matches a listing title to a kit and gives its MSRP in kr."""

    def __init__(self, catalog: list[dict], sek_per_gbp: float, overrides: dict | None = None):
        self.factor = sek_per_gbp
        self.by_sku = {c["sku"]: c for c in catalog if c.get("sku")}
        self.kits = []
        for c in catalog:
            fac = {t for t in _tokens(c["faction"]) if t not in _FACTION_GENERIC}
            # "Bloodthrone / Skull Cannon": either name identifies the kit.
            for name in re.split(r"\s*/\s*", c["kit"]):
                kit_tokens = [t for t in _tokens(name) if t not in _GENERIC]
                kit_tokens = [t for t in kit_tokens if t not in fac] or kit_tokens  # "Plaguebearers of Nurgle" -> plaguebearer
                if kit_tokens:
                    self.kits.append((c, kit_tokens, fac))
        self.overrides = {k.strip().lower(): v for k, v in (overrides or {}).items() if _int(v) > 0}

    def sek(self, c: dict) -> int:
        o = self.overrides.get(c["kit"].lower()) or self.overrides.get(c["title"].lower())
        if o:
            return _int(o)
        return int(round(c["gbp"] * self.factor / 5.0) * 5)

    def match(self, title: str, context: list[str] = ()) -> dict | None:
        """Best kit for a title, or None. context = the search words that found it."""
        if not title:
            return None
        code = _GW_CODE.search(title)
        if code and code.group() in self.by_sku:
            return self.by_sku[code.group()]
        toks = _title_tokens(title)
        ctx = toks | {t for q in context for t in _tokens(q)}
        best, best_len = None, 0
        for c, kit_tokens, fac in self.kits:
            if len(kit_tokens) < best_len or not set(kit_tokens) <= toks:
                continue
            if fac and not fac & ctx:
                continue  # kit belongs to another army than the listing mentions
            if len(kit_tokens) == 1 and len(kit_tokens[0]) < 5:
                continue  # too generic on its own ("lord", "chariot")
            if len(kit_tokens) > best_len or (best and self.sek(c) < self.sek(best)):
                best, best_len = c, len(kit_tokens)  # longest name wins; ties -> cheaper kit
        return best

    def annotate(self, l: dict, ratio: float) -> None:
        """Set msrp / msrp_kit / msrp_pct / bargain on a listing."""
        for k in ("msrp", "msrp_kit", "msrp_pct", "bargain"):
            l.pop(k, None)
        title = l.get("title", "")
        c = self.match(title, l.get("queries", []))
        if not c:
            return
        msrp = self.sek(c)
        l["msrp"], l["msrp_kit"] = msrp, c["kit"]
        price = l["auction"] if l.get("auction") else l.get("bin", 0)
        if price and msrp:
            l["msrp_pct"] = round(100 * price / msrp)
        low = title.lower()
        head = _head_noun(c["kit"])
        plural_kit = len(head) > 4 and head.endswith("s") and not head.endswith("ss")
        # A box of several models offered as one ("1 st Plaguebearer", "Flesh Hound").
        single_of_box = plural_kit and (
            _ONE.search(low) or (re.search(rf"(?<!\w){re.escape(head[:-1])}(?!\w)", low)
                                 and not re.search(rf"(?<!\w){re.escape(head)}(?!\w)", low)))
        partial = _PARTIAL.search(low) or single_of_box
        l["bargain"] = bool(l.get("auction") and not partial and l["auction"] < ratio * msrp)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def reject_reason(l: dict, cfg: dict, exclude_all: list[str], include_auctions: bool,
                  auction_max: int | None) -> str | None:
    """Why a tracked listing no longer fits the current settings, or None if it still does."""
    watch = next((w for w in cfg.get("watch", []) if (w.get("name") or "Unnamed") == l.get("watch")), None)
    if watch is None:
        return "its search was deleted"
    queries = [q.strip().lower() for q in watch.get("queries", []) if q.strip()]
    found_by = [q.lower() for q in l.get("queries", [])]
    if found_by:
        if not set(found_by) & set(queries):
            return "the search word that found it was removed"
    elif not any(q in l.get("title", "").lower() for q in queries):
        # Listed before search words were tracked: fall back to the title.
        return "the search word that found it was removed"
    exclude = [x for x in watch.get("exclude", []) if x.strip()] + exclude_all
    if excluded(l.get("title", ""), exclude):
        return "matches an exclude word"
    if not is_cheap(l, _int(watch.get("max_price")), include_auctions, auction_max) and not l.get("bargain"):
        return "over the price limit (or auctions are turned off)"
    return None


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
    exclude_all = [w for w in settings.get("exclude_all", []) if isinstance(w, str)]
    bargains_on = settings.get("bargains", True) is not False
    bargain_pct = min(100, max(1, _int(settings.get("bargain_pct")) or 50))
    sek_per_gbp = float(settings.get("msrp_sek_per_gbp") or 14)

    now = _now()
    pricer, catalog_info = None, {}
    if bargains_on:
        catalog, fetched = load_catalog(now)
        if catalog:
            pricer = Pricer(catalog, sek_per_gbp, settings.get("msrp_overrides"))
        catalog_info = {"kits": len(catalog), "updated": fetched}
    data = load_data()
    first_run = not data
    data_before_bargains = data.get("bargains")
    # Searches that have completed a run before. A brand-new search fills up with
    # everything already on Tradera, which shouldn't all buzz your phone.
    seeded = set(data.get("seeded_watches", {l.get("watch") for l in data.get("listings", [])}))
    seen = set(data.get("seen", []))
    listings = {l["id"]: l for l in data.get("listings", [])}
    new: list[dict] = []
    errors = 0
    failed_watches: set[str] = set()

    for watch in cfg.get("watch", []):
        name = watch.get("name") or "Unnamed"
        max_price = _int(watch.get("max_price"))
        exclude = [w for w in watch.get("exclude", []) if w.strip()] + exclude_all
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
                    failed_watches.add(name)
                    break
                if not raw_items:
                    break
                items = [normalize(r) for r in raw_items]
                for item in items:
                    if not item["id"]:
                        continue
                    if item["id"] in listings:
                        # Already tracked: refresh price, bids and end time, and remember
                        # which search words still find it. Whether it stays is decided at save time.
                        l = listings[item["id"]]
                        l.update({k: item[k] for k in ("title", "bin", "auction", "bids", "end", "thumb")})
                        if l.get("watch") == name and query not in l.setdefault("queries", []):
                            l["queries"].append(query)
                        continue
                    if item["id"] in seen or excluded(item["title"], exclude):
                        continue
                    entry = {**item, "watch": name, "queries": [query], "found": _iso(now)}
                    if pricer:
                        pricer.annotate(entry, bargain_pct / 100)
                    if not is_cheap(item, max_price, include_auctions, auction_max) and not entry.get("bargain"):
                        continue
                    listings[item["id"]] = entry
                    seen.add(item["id"])
                    new.append(entry)
                # Sorted cheapest first: once a whole page is over budget, stop paging
                # (unless hunting bargains, which can be pricier kits going cheap).
                if not pricer and all(lowest_price(i) > cap for i in items if lowest_price(i)):
                    break
                time.sleep(0.5)

    for l in new:
        tag = f" [BARGAIN {l['msrp_pct']}% of ~{l['msrp']} kr {l['msrp_kit']}]" if l.get("bargain") else ""
        print(f"NEW  {l['watch']}: {lowest_price(l)} kr | {l['title']} | {l['url']}{tag}")
    if not new:
        print("No new cheap listings.")

    if not dry_run:
        # Re-check every listing against the current settings and drop the ones that
        # wouldn't be added today. Their ids are forgotten too, so they come back if the
        # settings change back (a raised limit, a re-added search word, a renamed search).
        names = {w.get("name") or "Unnamed" for w in cfg.get("watch", [])}
        watch_by_name = {w.get("name") or "Unnamed": w for w in cfg.get("watch", [])}
        for l in listings.values():
            l.pop("over_budget", None)
            if pricer:
                pricer.annotate(l, bargain_pct / 100)
            else:
                for k in ("msrp", "msrp_kit", "msrp_pct", "bargain"):
                    l.pop(k, None)
            w = watch_by_name.get(l.get("watch"))
            # in_budget: shown in the main list; bargains above the limit only show under Bargains.
            l["in_budget"] = bool(w) and is_cheap(l, _int(w.get("max_price")), include_auctions, auction_max)
        dropped, reasons = set(), {}
        for l in listings.values():
            why = reject_reason(l, cfg, exclude_all, include_auctions, auction_max)
            if why:
                dropped.add(l["id"])
                reasons[why] = reasons.get(why, 0) + 1
        for why, n in sorted(reasons.items()):
            print(f"Removed {n} listing(s): {why}.")
        new = [l for l in new if l["id"] not in dropped]
        kept = prune([l for l in listings.values() if l["id"] not in dropped], now)
        kept.sort(key=lambda l: l.get("found", ""), reverse=True)
        data.update({
            "updated": _iso(now),
            "errors": errors,
            "watches": [{"name": w.get("name") or "Unnamed", "max_price": w.get("max_price")} for w in cfg.get("watch", [])],
            "seeded_watches": sorted(n for n in names if n not in failed_watches),
            "bargains": {"enabled": bool(pricer), "pct": bargain_pct, "sek_per_gbp": sek_per_gbp, **catalog_info},
            "listings": kept,
            "seen": [i for i in data.get("seen", []) if i not in dropped] + [l["id"] for l in new],
        })
        save_data(data)
        to_notify = [] if first_run else [l for l in new if l["watch"] in seeded]
        if pricer and not (data_before_bargains or {}).get("enabled"):
            # First run with the price list: today's bargains fill the tab quietly.
            to_notify = [l for l in to_notify if l.get("in_budget")]
        if len(to_notify) < len(new):
            print(f"Not notifying {len(new) - len(to_notify)} listing(s) from a first run or a new search.")
        send_pushes(to_notify, quiet=not to_notify)
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
