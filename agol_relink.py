#!/usr/bin/env python
"""Bulk-replace REST service URLs across ArcGIS Online / Portal content.

Dry-run by default. Nothing is written to the org unless you pass --apply.

Why this exists in this shape
-----------------------------
Service URLs hide in far more places than operationalLayers[].url. A scan of a
real 336-web-map org found them at 13 distinct JSON paths, including two levels
of GroupLayer nesting, baseMap styleUrl, presentation.slides[] basemaps, and
renderer symbol image URLs. Rather than maintain an allowlist of paths that
Esri keeps extending, this walks the whole item-data tree and rewrites any
string whose value is prefix-matched by the old service URL.

That is safe because the match is anchored at a URL boundary: a prefix only
counts if the next character is end-of-string, '/', '?' or '#'. So
".../Parcels/FeatureServer" matches ".../Parcels/FeatureServer/0" and
".../Parcels/FeatureServer/2/images/abc" but never ".../Parcels_Old/...".

Run --self-test to exercise the matcher without touching a server.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

log = logging.getLogger("agol_relink")

# ==========================================================================
# CONFIGURATION
#
# Everything tunable lives here. Command-line flags override any of it, so you
# can either fill this in and run the file with no arguments, or leave it alone
# and drive it entirely from the CLI.
# ==========================================================================

# --- what to change -------------------------------------------------------
# Fill these in to run without CLI flags. Leave empty to be prompted (or to
# pass --old-url / --new-url).
OLD_URL = ""   # e.g. "https://gis.example.org/public/rest/services/General/Roads/MapServer"
NEW_URL = ""   # e.g. "https://services1.arcgis.com/AbC123/arcgis/rest/services/Roads/FeatureServer"

# --- where ----------------------------------------------------------------
ORG_URL = "https://www.arcgis.com"   # or your Portal, e.g. "https://gis.example.org/portal"
USERNAME = ""                        # blank -> $AGOL_USER, else prompt
PASSWORD = ""                        # LEAVE BLANK. Use $AGOL_PASS or the prompt.
                                     # A password typed here can reach version control.

# --- safety ---------------------------------------------------------------
# False = dry-run: scan and report, write nothing. Flipping this to True is the
# same as passing --apply, and still requires typing APPLY at the prompt unless
# ASSUME_YES is also True.
APPLY = False
ASSUME_YES = False       # True skips the typed APPLY confirmation. Automation only.

# --- scope ----------------------------------------------------------------
# Item types whose *data* JSON is scanned. Web Map is the common case; add more
# from KNOWN_DATA_TYPES below as needed.
DEFAULT_TYPES = ["Web Map"]

KNOWN_DATA_TYPES = [
    "Web Map", "Web Scene", "Web Mapping Application", "Dashboard",
    "StoryMap", "Web Experience", "Form", "Insights Workbook", "Notebook",
]

# Item types whose top-level item.url property points at a service. Only
# touched when INCLUDE_ITEM_URL / --include-item-url is on.
SERVICE_ITEM_TYPES = [
    "Feature Service", "Map Service", "Image Service", "Vector Tile Service",
    "Scene Service", "Stream Service", "Geoprocessing Service", "WMS", "WMTS",
]

INCLUDE_ITEM_URL = False   # also rewrite service items' own registered url
EXTRA_QUERY = ""           # extra AGOL search clause, e.g. 'owner:"jsmith"'
MAX_ITEMS = 0              # 0 = no limit. Useful for a quick sample run.

# --- matching -------------------------------------------------------------
# Deliberately forgiving. The worst realistic failure is an operator typing the
# old URL with different case or scheme than the content stores, matching
# nothing, and concluding there was nothing to fix. Two services differing only
# by case or scheme do not exist in practice.
IGNORE_CASE = True    # False -> exact case must match
ANY_SCHEME = True     # False -> http:// and https:// are treated as different

# --- resources ------------------------------------------------------------
# StoryMap and Experience Builder keep the working DRAFT in item resources
# while /data holds only the published copy. Skip these and the old URL comes
# back the next time an author hits Publish.
SCAN_RESOURCES = True

# Binary resources that can embed URLs but cannot be read as JSON. Items
# carrying these are listed at the end of the run for manual follow-up.
OPAQUE_RESOURCE_EXT = (".zip", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".gif",
                       ".pdf", ".docx", ".sd", ".gdb")

# --- reliability ----------------------------------------------------------
RETRY_TRIES = 5        # attempts per AGOL call before giving up
RETRY_MAX_WAIT = 60    # seconds, ceiling on exponential backoff
PAGE_SIZE = 100        # items per search page
SORT_FIELD = "created"  # MUST be near-unique. The API default 'avgRating' is
                        # 0.0 for almost every item, and paging an all-ties
                        # sort silently skips and repeats rows.

MAX_DEPTH = 200  # AGOL JSON never approaches this; guards against cyclic junk.

# ==========================================================================
# END CONFIGURATION
# ==========================================================================


# --------------------------------------------------------------------------
# URL matching
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MatchOpts:
    """How forgiving the old-URL comparison is. Defaults set in CONFIGURATION."""
    ignore_case: bool = IGNORE_CASE
    any_scheme: bool = ANY_SCHEME


def _scheme_variants(url: str, any_scheme: bool) -> list[str]:
    out = [url]
    if any_scheme:
        m = re.match(r"^(https?)://(.*)$", url, re.I)
        if m:
            other = "http" if m.group(1).lower() == "https" else "https"
            out.append(f"{other}://{m.group(2)}")
    return out


def match_prefix(url: Any, old: str, opts: MatchOpts = MatchOpts()) -> int:
    """Length of the old-URL prefix inside `url`, or -1 if it does not match.

    Returns an index into the ORIGINAL string so the remainder (/0, ?f=json,
    /2/images/hash, /resources/styles/root.json) can be preserved verbatim.
    """
    if not isinstance(url, str) or not url:
        return -1
    for cand in _scheme_variants(old, opts.any_scheme):
        cand = cand.rstrip("/")
        if not cand:
            return -1
        n = len(cand)
        head = url[:n]
        hit = head.lower() == cand.lower() if opts.ignore_case else head == cand
        if hit and (len(url) == n or url[n] in "/?#"):
            return n
    return -1


def rewrite(url: str, n: int, new: str) -> str:
    """Swap the first `n` chars of `url` for `new`, keeping the remainder."""
    return new.rstrip("/") + url[n:]


# --------------------------------------------------------------------------
# Recursive data walk
# --------------------------------------------------------------------------
@dataclass
class Hit:
    path: str
    before: str
    after: str


def walk_replace(node: Any, old: str, new: str, opts: MatchOpts,
                 path: str = "", hits: list[Hit] | None = None,
                 depth: int = 0) -> list[Hit]:
    """Rewrite every prefix-matching URL string in `node`, in place."""
    if hits is None:
        hits = []
    if depth > MAX_DEPTH:
        log.warning("max depth exceeded at %s", path)
        return hits

    if isinstance(node, dict):
        pairs: Iterable = list(node.items())
    elif isinstance(node, list):
        pairs = list(enumerate(node))
    else:
        return hits

    for k, v in pairs:
        p = f"{path}.{k}" if isinstance(k, str) else f"{path}[{k}]"
        if isinstance(v, str):
            i = match_prefix(v, old, opts)
            if i >= 0:
                nv = rewrite(v, i, new)
                if nv != v:
                    node[k] = nv
                    hits.append(Hit(p, v, nv))
        elif isinstance(v, (dict, list)):
            walk_replace(v, old, new, opts, p, hits, depth + 1)
    return hits


# --------------------------------------------------------------------------
# Retry
# --------------------------------------------------------------------------
def succeeded(resp: Any) -> bool:
    """Did an AGOL write actually take?

    The two APIs we call disagree on shape. Item.update() returns a bool.
    ResourceManager.update() returns the raw server dict. On failure that is
    {"error": {...}}, a TRUTHY value. A plain `if resp:` therefore reports
    success for every failed resource write.
    """
    if isinstance(resp, dict):
        if "error" in resp:
            return False
        return bool(resp.get("success", True))
    return bool(resp)


def err_text(resp: Any) -> str:
    if isinstance(resp, dict) and "error" in resp:
        e = resp["error"]
        if isinstance(e, dict):
            return f"{e.get('code', '?')}: {e.get('message', e)}"
        return str(e)
    return repr(resp)


def with_retry(fn, *a, what: str = "call", tries: int = RETRY_TRIES, **kw):
    """Retry on AGOL throttling / transient server errors with backoff."""
    for attempt in range(1, tries + 1):
        try:
            return fn(*a, **kw)
        except Exception as e:  # arcgis wraps HTTP errors in many classes
            msg = str(e)
            transient = any(t in msg for t in
                            ("429", "502", "503", "504", "Timeout", "timed out",
                             "Connection", "Unable to connect", "temporarily"))
            if not transient or attempt == tries:
                raise
            wait = min(RETRY_MAX_WAIT, 2 ** attempt) + random.uniform(0, 1)
            log.warning("%s failed (attempt %d/%d): %s. Retrying in %.1fs",
                        what, attempt, tries, msg[:200], wait)
            time.sleep(wait)


# --------------------------------------------------------------------------
# Enumeration
# --------------------------------------------------------------------------
def iter_items(gis, item_types: list[str], extra_query: str = "",
               max_items: int = 0) -> Iterator:
    """Yield every matching item, paging with a stable sort (see SORT_FIELD)."""
    orgid = gis.properties.id
    type_clause = " OR ".join(f'type:"{t}"' for t in item_types)
    q = f"orgid:{orgid} AND ({type_clause})"
    if extra_query.strip():
        q += f" AND ({extra_query.strip()})"
    log.info("query: %s", q)

    seen: set[str] = set()
    start, page, yielded = 1, PAGE_SIZE, 0
    total_reported = None
    while True:
        r = with_retry(gis.content.advanced_search, query=q, max_items=page,
                       start=start, sort_field=SORT_FIELD, sort_order="asc",
                       what="advanced_search")
        if total_reported is None:
            total_reported = r.get("total")
            log.info("server reports %s matching items", total_reported)
        results = r.get("results") or []
        if not results:
            break
        for it in results:
            if it.id in seen:
                continue
            seen.add(it.id)
            yield it
            yielded += 1
            if max_items and yielded >= max_items:
                return
        nxt = r.get("nextStart", -1)
        # `<= start`, not `<= 0`: a server that keeps returning the same
        # nextStart would otherwise spin forever.
        if nxt is None or nxt <= 0 or nxt <= start:
            break
        start = nxt

    if total_reported and len(seen) < total_reported:
        why = ("AGOL caps search paging at 10,000 results"
               if total_reported > 10000 else
               "private items owned by other users are not discoverable via "
               "search, even for an admin")
        log.warning("enumerated %d of %s reported items. Some content was "
                    "not returned (%s)", len(seen), total_reported, why)


# --------------------------------------------------------------------------
# Per-item processing
# --------------------------------------------------------------------------
@dataclass
class Result:
    item_id: str
    title: str
    type: str
    owner: str
    status: str            # matched | updated | clean | skipped | failed
    hits: list[Hit] = field(default_factory=list)
    error: str = ""
    opaque: list[str] = field(default_factory=list)  # binary resources we cannot read


def fetch_data(item) -> Any:
    """Get item data, distinguishing 'empty' from 'fetch failed'.

    Item.get_data() swallows every exception and returns {}. That makes a token
    expiry or a 500 indistinguishable from a map with no matching layers, so we
    re-fetch the raw payload ourselves when get_data returns something falsy.
    """
    data = with_retry(item.get_data, what=f"get_data {item.id}")
    if data:
        return data
    # get_data returned {} / None / b''. Confirm that is genuinely the case
    # rather than a swallowed error, by asking the sharing API directly.
    gis = item._gis
    url = f"{gis._portal.resturl}content/items/{item.id}/data"
    resp = with_retry(gis._con.get, url, {"f": "json"}, try_json=False,
                      what=f"raw data {item.id}")
    if isinstance(resp, bytes):
        resp = resp.decode("utf-8", "replace")
    if isinstance(resp, str):
        resp = resp.strip()
        if not resp:
            return {}
        try:
            return json.loads(resp)
        except ValueError:
            raise RuntimeError(f"item data is not JSON ({len(resp)} bytes)")
    return resp or {}


def process_item(item, old: str, new: str, opts: MatchOpts, apply: bool,
                 backup_dir: Path, scan_resources: bool = SCAN_RESOURCES) -> Result:
    base = Result(item.id, item.title or "", item.type, item.owner, "clean")
    try:
        data = fetch_data(item)
    except Exception as e:
        base.status, base.error = "failed", f"fetch: {e}"
        return base
    if not isinstance(data, (dict, list)):
        base.status, base.error = "skipped", f"data is {type(data).__name__}"
        return base

    hits: list[Hit] = []
    original = json.dumps(data, ensure_ascii=False) if data else ""
    if data:
        hits = walk_replace(data, old, new, opts)

    # /data is only half the story for draft-bearing types. See the docstring
    # on process_resources.
    res_hits: list[Hit] = []
    if scan_resources:
        res_hits, opaque, rerr = process_resources(item, old, new, opts, apply,
                                                   backup_dir)
        if rerr:
            base.status, base.error = "failed", rerr
            base.hits = hits + res_hits
            return base
        # Report binaries whenever the item changed at all. A draft-only edit
        # (resources matched, /data did not) still needs the manual follow-up.
        if opaque and (hits or res_hits):
            base.opaque = opaque

    base.hits = hits + res_hits
    if not base.hits:
        if not data:
            base.status, base.error = "skipped", "empty data, no matching resources"
        return base

    base.status = "matched"
    if not apply:
        return base

    if hits:
        # Backup before /data is touched. (Resources, if any, were written
        # earlier, each having taken its own backup first.)
        (backup_dir / f"{item.id}.json").write_text(original, encoding="utf-8")
        try:
            # update() returns False on a server-side refusal WITHOUT raising --
            # e.g. an item you can read but not edit. Treating that as success is
            # how you end a run believing you fixed maps you did not touch.
            resp = with_retry(item.update, data=data, what=f"update {item.id}")
            if not succeeded(resp):
                base.status = "failed"
                base.error = f"server refused the write: {err_text(resp)}"
                return base
        except Exception as e:
            base.status, base.error = "failed", f"update: {e}"
            return base

    base.status = "updated"
    return base


def process_resources(item, old: str, new: str, opts: MatchOpts, apply: bool,
                      backup_dir: Path) -> tuple[list[Hit], list[str], str]:
    """Scan (and optionally patch) the item's JSON *resources*.

    This is not optional polish. For StoryMaps and Experience Builder, /data
    holds the PUBLISHED copy while the working draft lives in a resource
    (draft_<ts>.json, config/config.json). Patch only /data and the next time
    an author hits Publish, the old URL comes straight back, and the tool will
    have reported success for a change that silently reverts weeks later.

    Returns (hits, opaque_resource_names, error).
    """
    hits: list[Hit] = []
    opaque: list[str] = []
    try:
        listing = with_retry(item.resources.list, what=f"resources.list {item.id}")
    except Exception as e:
        return hits, opaque, f"resources.list: {e}"

    for entry in listing or []:
        name = entry.get("resource") if isinstance(entry, dict) else str(entry)
        if not name:
            continue
        if name.lower().endswith(OPAQUE_RESOURCE_EXT):
            opaque.append(name)
            continue
        if not name.lower().endswith(".json"):
            continue
        try:
            doc = with_retry(item.resources.get, file=name, try_json=True,
                             what=f"resources.get {item.id}/{name}")
        except Exception as e:
            log.debug("  resource %s unreadable: %s", name, e)
            continue
        if not isinstance(doc, (dict, list)):
            continue

        original = json.dumps(doc, ensure_ascii=False)
        rhits = walk_replace(doc, old, new, opts, path=f"[resource:{name}]")
        if not rhits:
            continue
        hits.extend(rhits)
        if not apply:
            continue

        bdir = backup_dir / "resources" / item.id
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / name.replace("/", "__")).write_text(original, encoding="utf-8")

        folder, _, leaf = name.rpartition("/")
        try:
            resp = with_retry(item.resources.update, folder_name=folder or None,
                              file_name=leaf,
                              text=json.dumps(doc, ensure_ascii=False),
                              what=f"resources.update {item.id}/{name}")
            if not succeeded(resp):
                return hits, opaque, f"resources.update {name}: {err_text(resp)}"
        except Exception as e:
            return hits, opaque, f"resources.update {name}: {e}"

    return hits, opaque, ""


def process_item_url(item, old: str, new: str, opts: MatchOpts,
                     apply: bool) -> Result | None:
    """Rewrite the item's own top-level `url` property (service items).

    This is a different API call than update(data=...). The registered URL of
    a Feature/Map Service item lives on the item, not in its data.
    """
    u = getattr(item, "url", None)
    i = match_prefix(u, old, opts)
    if i < 0:
        return None
    nv = rewrite(u, i, new)
    r = Result(item.id, item.title or "", item.type, item.owner, "matched",
               hits=[Hit("item.url", u, nv)])
    if not apply:
        return r
    try:
        resp = with_retry(item.update, item_properties={"url": nv},
                          what=f"update url {item.id}")
        ok = succeeded(resp)
        r.status = "updated" if ok else "failed"
        if not ok:
            r.error = f"server refused the write: {err_text(resp)}"
    except Exception as e:
        r.status, r.error = "failed", f"update url: {e}"
    return r


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------
def self_test() -> int:
    o = MatchOpts()
    OLD = "https://gis.example.org/public/rest/services/General/Boundaries/MapServer"
    NEW = "https://services1.arcgis.com/AbC123/arcgis/rest/services/Boundaries/FeatureServer"

    def sub(u, old=OLD, new=NEW, opts=o):
        i = match_prefix(u, old, opts)
        return rewrite(u, i, new) if i >= 0 else None

    # --- shapes observed in the real org ---
    assert sub(OLD) == NEW, "service root"
    assert sub(OLD + "/0") == NEW + "/0", "sublayer index"
    assert sub(OLD + "/12") == NEW + "/12", "multi-digit sublayer"
    assert sub(OLD + "/2/images/0e0c479fe6") == NEW + "/2/images/0e0c479fe6", \
        "renderer symbol image under the service"
    assert sub(OLD + "/") == NEW + "/", "trailing slash"
    assert sub(OLD + "?f=json") == NEW + "?f=json", "query string"
    assert sub(OLD + "/0/query") == NEW + "/0/query", "beyond sublayer"
    assert sub(OLD + "#frag") == NEW + "#frag", "fragment"
    vt = "https://tiles.arcgis.com/tiles/AbC123/arcgis/rest/services/BM/VectorTileServer"
    assert sub(vt + "/resources/styles/root.json", old=vt, new=NEW) == \
        NEW + "/resources/styles/root.json", "vector tile styleUrl"

    # --- case / scheme forgiveness ---
    assert sub(OLD.replace("https", "http")) == NEW, "http vs https"
    assert sub(OLD.upper()) == NEW, "uppercase stored url"
    assert sub(OLD, old=OLD.upper()) == NEW, "uppercase operator input"
    assert sub(OLD.replace("https", "http"), opts=MatchOpts(any_scheme=False)) \
        is None, "strict scheme opt-out"
    assert sub(OLD.upper(), opts=MatchOpts(ignore_case=False)) is None, \
        "case-sensitive opt-out"

    # --- must NOT match: the boundary check is the whole safety story ---
    assert sub(OLD + "_Old") is None, "sibling service sharing a prefix"
    assert sub(OLD + "Extra/0") is None, "prefix without boundary"
    assert sub("https://other.host/rest/services/X/MapServer") is None, "other host"
    assert sub("") is None and sub(None) is None and sub(123) is None, "non-str"
    assert match_prefix(OLD, "") == -1, "empty old_url must never match"
    assert match_prefix(OLD, "///") == -1, "slash-only old_url must never match"

    # --- idempotence: running twice must not double-apply ---
    once = sub(OLD + "/3")
    assert match_prefix(once, OLD, o) == -1, "rewritten url must not re-match"

    # --- recursive walk over a web map shaped like the real fixtures ---
    doc = {
        "operationalLayers": [
            {"url": OLD + "/1"},
            {"layerType": "GroupLayer", "layers": [
                {"url": OLD + "/2"},
                {"layerType": "GroupLayer", "layers": [{"url": OLD + "/3"}]},
            ]},
            {"layerDefinition": {"drawingInfo": {"renderer": {"symbol": {
                "url": "https://static.arcgis.com/images/Symbols/Red.png"}}}}},
        ],
        "tables": [{"url": OLD + "/9"}],
        "baseMap": {"baseMapLayers": [
            {"url": OLD + "/0"},
            {"styleUrl": OLD + "/resources/styles/root.json"},
        ]},
        "presentation": {"slides": [
            {"baseMap": {"baseMapLayers": [{"url": OLD}]}}]},
        "unknownFutureEsriKey": {"deeply": {"nested": {"url": OLD + "/7"}}},
    }
    hits = walk_replace(doc, OLD, NEW, o)
    assert len(hits) == 8, f"expected 8 rewrites, got {len(hits)}: " \
        + "\n".join(h.path for h in hits)
    assert doc["operationalLayers"][1]["layers"][1]["layers"][0]["url"] == NEW + "/3", \
        "two-level GroupLayer nesting"
    assert doc["presentation"]["slides"][0]["baseMap"]["baseMapLayers"][0]["url"] == NEW, \
        "presentation slide basemap"
    assert doc["unknownFutureEsriKey"]["deeply"]["nested"]["url"] == NEW + "/7", \
        "path not in any allowlist"
    assert doc["operationalLayers"][2]["layerDefinition"]["drawingInfo"][
        "renderer"]["symbol"]["url"].startswith("https://static.arcgis.com"), \
        "unrelated symbol image must be left alone"

    # a second pass must be a no-op
    assert walk_replace(doc, OLD, NEW, o) == [], "walk must be idempotent"

    # --- new_url is never treated as a regex replacement template ---
    evil = r"https://x/\g<0>/FeatureServer"
    assert sub(OLD + "/0", new=evil) == evil + "/0", \
        "backreference syntax in new_url must be literal"
    assert sub(OLD + "/0", new=r"https://x/a\Rb/FeatureServer") is not None, \
        "invalid regex escape in new_url must not raise"

    # --- succeeded(): the two write APIs return different shapes ---
    assert succeeded(True), "Item.update -> True"
    assert not succeeded(False), "Item.update -> False"
    assert not succeeded(None), "Item.update -> None"
    assert succeeded({"success": True, "itemId": "x"}), "resources.update ok"
    assert not succeeded({"success": False}), "explicit success:false"
    # the trap: an error dict is truthy, so `if resp:` would call this a success
    err = {"error": {"code": 404, "message": "Resource does not exist"}}
    assert bool(err) is True, "error dict really is truthy"
    assert not succeeded(err), "error dict must NOT count as success"
    assert "404" in err_text(err) and "does not exist" in err_text(err)

    # --- breadth warning fires for broad prefixes, stays quiet for services ---
    assert not breadth_warning(OLD), "service root must not warn"
    assert not breadth_warning(OLD + "/"), "trailing slash must not warn"
    for t in ("FeatureServer", "ImageServer", "GeocodeServer", "VectorTileServer"):
        assert not breadth_warning(f"https://h/rest/services/X/{t}"), t
    assert "BARE HOST" in breadth_warning("https://gis.example.org"), "bare host"
    assert breadth_warning("https://gis.example.org/public"), "partial path"
    # the real hazard this guards: a bare host sweeps in non-service paths
    assert match_prefix("https://gis.example.org/portal",
                        "https://gis.example.org", o) >= 0, \
        "bare host matches /portal, which is why it warns"

    # --- validate_urls rejects the inputs that would cause mass damage ---
    def rejects(a, b):
        try:
            validate_urls(a, b)
        except SystemExit:
            return True
        return False

    for bad_old, bad_new in [("", NEW), ("   ", NEW), (OLD, ""),
                             ("not-a-url", NEW), (OLD, OLD), (OLD, OLD.upper())]:
        assert rejects(bad_old, bad_new), \
            f"validate_urls should reject {bad_old!r} -> {bad_new!r}"
    assert not rejects(OLD, NEW), "valid pair must be accepted"

    # new nested under old: correct once, doubled on a second run, and a reused
    # --out-dir would overwrite the backup with already-rewritten data.
    host = "https://gis.example.org"
    assert rejects(host, host + "/arcgis"), "new nested under old (web adaptor)"
    assert rejects(host, host.replace("https", "http") + "/arcgis"), \
        "nested detection must see through the scheme variant"
    assert rejects(OLD, OLD + "/extra"), "new is old plus a path segment"
    # demonstrate the damage the guard prevents
    doubled = rewrite(host + "/x", match_prefix(host + "/x", host, o), host + "/arcgis")
    assert doubled == host + "/arcgis/x"
    assert match_prefix(doubled, host, o) >= 0, \
        "rewritten url still matches old, hence the rejection"

    print("self-test: all assertions passed")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def setup_logging(out_dir: Path, verbose: bool) -> None:
    """Everything to the file; only our own records to the console.

    The arcgis package logs chatter like 'Retrieving roles(start=1, num=100)' at
    INFO on the root logger. Keep it in run.log for forensics, keep it off the
    operator's screen.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # urllib3 logs every request line at DEBUG, query string included, and
    # arcgis passes the auth token as a GET parameter. Without this cap, a live
    # token lands in run.log, which is precisely the file someone pastes into a
    # forum thread when asking why their run failed.
    for noisy in ("urllib3", "requests", "urllib3.connectionpool"):
        logging.getLogger(noisy).setLevel(logging.INFO)

    fh = logging.FileHandler(out_dir / "run.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)-7s %(message)s"))
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.addFilter(lambda r: r.name.startswith(log.name) or r.levelno >= logging.WARNING)

    root.handlers[:] = [fh, ch]


def validate_urls(old: str, new: str) -> None:
    """Reject inputs that would cause mass damage. Call with STRIPPED values."""
    for label, u in (("old", old), ("new", new)):
        if not u or not u.strip():
            raise SystemExit(f"--{label}-url is empty. Refusing to run: an "
                             "empty old URL would match every string.")
        if not re.match(r"^https?://[^/\s]+/", u.strip() + "/"):
            raise SystemExit(f"--{label}-url is not an absolute http(s) URL: {u!r}")
    if old.rstrip("/").lower() == new.rstrip("/").lower():
        raise SystemExit("old and new URL are the same. Nothing to do.")
    # The new URL must not itself match the old one. Otherwise every rewritten
    # URL still prefix-matches, so a second run compounds the change --
    # old=https://host, new=https://host/arcgis (the standard add-a-web-adaptor
    # migration) yields /arcgis/arcgis/ on run two. Worse, a reused --out-dir
    # then overwrites each backup with run-one's already-rewritten data,
    # destroying the only copy of the original.
    if match_prefix(new, old, MatchOpts()) >= 0:
        raise SystemExit(
            f"new URL is nested under the old one:\n  old: {old}\n  new: {new}\n"
            "Rewritten URLs would still match the old prefix, so a second run "
            "would apply the change twice. Narrow --old-url so it does not "
            "prefix --new-url.")


def breadth_warning(old: str) -> str:
    """Flag an old_url broad enough to catch more than one service.

    A prefix match is anchored at a URL boundary, so a bare host like
    'https://gis.example.org' legitimately matches EVERY path under it --
    including non-service URLs such as '/portal'. That is the right behaviour
    for a whole-host migration and the wrong one for a single-service swap, and
    the operator is the only one who knows which they meant.
    """
    path = re.sub(r"^https?://[^/]+", "", old.rstrip("/"))
    if re.search(r"/(?:Feature|Map|Image|Vector ?Tile|Scene|Stream|Geocode|GP|"
                 r"Geometry|GeoData|NA|Mobile|Globe|Schematics)Server$",
                 path, re.I):
        return ""
    if not path:
        return ("old URL is a BARE HOST. Every URL on that host will be "
                "rewritten, including non-service paths like /portal.")
    return (f"old URL does not end at a service endpoint ({path!r}). It will "
            "match every URL beginning with that prefix, which may be more "
            "than one service.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Bulk-replace REST service URLs in AGOL/Portal content. "
                    "Dry-run unless --apply is given.")
    p.add_argument("--self-test", action="store_true",
                   help="run offline assertions on the matcher and exit")
    # Defaults come from the CONFIGURATION block; env vars beat it; flags win.
    p.add_argument("--org", default=os.environ.get("AGOL_URL") or ORG_URL)
    p.add_argument("--user", default=os.environ.get("AGOL_USER") or USERNAME or None)
    p.add_argument("--password", default=os.environ.get("AGOL_PASS") or PASSWORD or None)
    p.add_argument("--old-url", default=OLD_URL or None)
    p.add_argument("--new-url", default=NEW_URL or None)
    p.add_argument("--types", default=",".join(DEFAULT_TYPES),
                   help=f"comma-separated item types. Known data-bearing types: "
                        f"{', '.join(KNOWN_DATA_TYPES)}")
    p.add_argument("--query", default=EXTRA_QUERY, help="extra AGOL search clause")
    p.add_argument("--max-items", type=int, default=MAX_ITEMS, help="0 = no limit")
    p.add_argument("--apply", action="store_true", default=APPLY,
                   help="actually write changes. Without this, nothing is modified.")
    p.add_argument("--dry-run", action="store_true",
                   help="force dry-run, overriding APPLY in the config block")
    p.add_argument("--yes", action="store_true", default=ASSUME_YES,
                   help="skip the --apply confirmation prompt")
    p.add_argument("--include-item-url", action="store_true", default=INCLUDE_ITEM_URL,
                   help="also rewrite the top-level item.url of service items")
    p.add_argument("--no-resources", action="store_true", default=not SCAN_RESOURCES,
                   help="skip item resources. NOT recommended: StoryMap and "
                        "Experience Builder drafts live in resources, and "
                        "patching only /data lets the old URL return on the "
                        "next republish.")
    p.add_argument("--case-sensitive", action="store_true", default=not IGNORE_CASE)
    p.add_argument("--strict-scheme", action="store_true", default=not ANY_SCHEME,
                   help="do not treat http:// and https:// as equivalent")
    p.add_argument("--out-dir", default="")
    p.add_argument("--resume", default="",
                   help="path to a previous run's out-dir; skip items already updated")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    if args.self_test:
        return self_test()

    org = args.org or input("Organization URL [https://www.arcgis.com]: ") or "https://www.arcgis.com"
    user = args.user or input("Username: ")
    pwd = args.password or getpass.getpass("Password: ")
    old = args.old_url or input("Old REST service URL to replace: ")
    new = args.new_url or input("New REST service URL: ")
    # Strip BEFORE validating: otherwise " https://HOST/x" and "https://host/x"
    # slip past the same-URL guard and produce a mass write of case-only edits.
    old, new = old.strip(), new.strip()
    validate_urls(old, new)

    # --dry-run always wins, so a config file with APPLY = True can still be
    # overridden from the command line.
    apply = args.apply and not args.dry_run

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else Path(f"agol_relink_{stamp}")
    backup_dir = out_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(out_dir, args.verbose)

    opts = MatchOpts(ignore_case=not args.case_sensitive,
                     any_scheme=not args.strict_scheme)
    types = [t.strip() for t in args.types.split(",") if t.strip()]

    log.info("mode      : %s", "APPLY (writes to the org)" if apply
             else "DRY-RUN (no writes)")
    log.info("old url   : %s", old)
    log.info("new url   : %s", new)
    log.info("item types: %s", ", ".join(types))
    log.info("match     : case-%s, %s",
             "sensitive" if args.case_sensitive else "insensitive",
             "strict scheme" if args.strict_scheme else "http==https")
    log.info("output    : %s", out_dir.resolve())
    warn = breadth_warning(old)
    if warn:
        log.warning("BREADTH: %s", warn)

    done: set[str] = set()
    if args.resume:
        rp = Path(args.resume) / "updated_ids.txt"
        if rp.exists():
            done = {ln.strip() for ln in rp.read_text().splitlines() if ln.strip()}
            log.info("resume    : skipping %d already-updated items", len(done))

    from arcgis.gis import GIS  # imported late so --self-test needs no arcgis
    log.info("connecting to %s as %s ...", org, user)
    # NOTE: no verify_cert=False. Disabling TLS verification would POST these
    # credentials over an unvalidated connection. If you target a Portal with a
    # private CA, set REQUESTS_CA_BUNDLE to the CA path instead.
    gis = GIS(org, user, pwd)
    log.info("connected as %s (role=%s, org=%s)",
             gis.users.me.username, gis.users.me.role, gis.properties.id)

    if apply and not args.yes:
        print("\n*** --apply will MODIFY items in this organization. ***")
        print(f"    {old}\n -> {new}\n")
        if input("Type APPLY to continue: ").strip() != "APPLY":
            log.info("aborted by operator")
            return 1

    results: list[Result] = []
    counts = {"clean": 0, "matched": 0, "updated": 0, "skipped": 0,
              "failed": 0, "resumed": 0}
    updated_ids = out_dir / "updated_ids.txt"

    scan_types = list(types)
    if args.include_item_url:
        scan_types += [t for t in SERVICE_ITEM_TYPES if t not in scan_types]
    elif [t for t in types if t in SERVICE_ITEM_TYPES]:
        # Otherwise these get enumerated and then silently skipped, which reads
        # as "nothing to fix" rather than "you forgot a flag".
        log.warning("%s in --types but --include-item-url is off: those items "
                    "will be skipped, not scanned.",
                    ", ".join(t for t in types if t in SERVICE_ITEM_TYPES))

    opaque_items: list[tuple[str, list[str]]] = []
    n = 0
    for item in iter_items(gis, scan_types, args.query, args.max_items):
        n += 1
        if item.id in done:
            counts["resumed"] += 1
            continue

        if item.type in SERVICE_ITEM_TYPES:
            if args.include_item_url:
                r = process_item_url(item, old, new, opts, apply)
                if r:
                    results.append(r)
                    counts[r.status] = counts.get(r.status, 0) + 1
                    log.info("[%d] %-8s %s  %s", n, r.status, item.id,
                             (item.title or "")[:60])
                    if r.status == "updated":
                        with updated_ids.open("a", encoding="utf-8") as fh:
                            fh.write(item.id + "\n")
            continue

        r = process_item(item, old, new, opts, apply, backup_dir,
                         scan_resources=not args.no_resources)
        results.append(r)
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.opaque:
            opaque_items.append((item.id, r.opaque))

        if r.hits:
            log.info("[%d] %-8s %s  %s  (%d url%s)", n, r.status, item.id,
                     (item.title or "")[:60], len(r.hits),
                     "" if len(r.hits) == 1 else "s")
            for h in r.hits[:20]:
                log.debug("        %s\n          %s\n       -> %s",
                          h.path, h.before, h.after)
            if len(r.hits) > 20:
                log.debug("        ... %d more", len(r.hits) - 20)
            if r.status == "updated":
                with updated_ids.open("a", encoding="utf-8") as fh:
                    fh.write(item.id + "\n")
        elif r.status in ("failed", "skipped"):
            log.warning("[%d] %-8s %s  %s  : %s", n, r.status, item.id,
                        (item.title or "")[:60], r.error)
        else:
            log.debug("[%d] clean    %s  %s", n, item.id, (item.title or "")[:60])

    # ---- audit artefacts ----
    csv_path = out_dir / "changes.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["item_id", "title", "type", "owner", "status",
                    "json_path", "url_before", "url_after", "error"])
        for r in results:
            if r.hits:
                for h in r.hits:
                    w.writerow([r.item_id, r.title, r.type, r.owner, r.status,
                                h.path, h.before, h.after, r.error])
            elif r.status in ("failed", "skipped"):
                w.writerow([r.item_id, r.title, r.type, r.owner, r.status,
                            "", "", "", r.error])

    total_urls = sum(len(r.hits) for r in results)
    log.info("")
    log.info("scanned %d items", n)
    for k in ("matched", "updated", "clean", "skipped", "failed", "resumed"):
        if counts.get(k):
            log.info("  %-8s %d", k, counts[k])
    log.info("  %-8s %d", "urls", total_urls)
    log.info("report : %s", csv_path.resolve())
    if opaque_items:
        log.warning("")
        log.warning("%d changed item(s) also carry binary resources this tool "
                    "cannot read. URLs inside them are NOT rewritten. Check "
                    "them by hand:", len(opaque_items))
        for iid, names in opaque_items[:15]:
            log.warning("  %s  %s", iid, ", ".join(names[:5]))
        if len(opaque_items) > 15:
            log.warning("  ... %d more (see run.log)", len(opaque_items) - 15)
    if not apply and total_urls:
        log.info("")
        log.info("DRY-RUN. Nothing was written. Review %s, then re-run with "
                 "--apply to commit.", csv_path.name)
    if counts.get("failed"):
        log.warning("%d items FAILED, see the report", counts["failed"])
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
