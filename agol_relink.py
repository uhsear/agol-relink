#!/usr/bin/env python
"""Bulk-replace REST service URLs across ArcGIS Online / Portal content.

Dry-run by default. Nothing is written to the org unless you pass --apply.

Why this exists in this shape
-----------------------------
Service URLs hide in far more places than operationalLayers[].url. An early
scan of one real org of a few hundred web maps found them at 13 distinct JSON paths, including two levels
of GroupLayer nesting, baseMap styleUrl, presentation.slides[] basemaps, and
renderer symbol image URLs. Rather than maintain an allowlist of paths that
Esri keeps extending, this walks the whole item-data tree and rewrites any
string whose value is prefix-matched by the old service URL.

That is safe because the match is anchored at a URL boundary: a prefix only
counts if the next character is end-of-string, '/', '?' or '#'. So
".../Parcels/FeatureServer" matches ".../Parcels/FeatureServer/0" and
".../Parcels/FeatureServer/2/images/abc" but never ".../Parcels_Old/...".

Run --self-test to exercise the tool against a local stub server. It needs no
arcgis package, no organization and no credentials.
"""
from __future__ import annotations

import argparse
import builtins
import contextlib
import csv
import email.parser
import getpass
import http.server
import io
import json
import logging
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

log = logging.getLogger("agol_relink")
log.addHandler(logging.NullHandler())  # no "last resort" stderr output before setup_logging

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
SORT_FIELD = "created"  # MUST be near-unique. Paging a sort whose keys are
                        # mostly equal can skip and repeat rows between pages.

MAX_DEPTH = 200  # AGOL JSON never approaches this; guards against cyclic junk.

# ==========================================================================
# END CONFIGURATION
# ==========================================================================

EXIT_OK, EXIT_REFUSED, EXIT_FAILED, EXIT_INCOMPLETE = 0, 1, 2, 3

CSV_COLUMNS = ["item_id", "title", "type", "owner", "status",
               "json_path", "url_before", "url_after", "error"]


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
            out.append("%s://%s" % (other, m.group(2)))
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
        p = "%s.%s" % (path, k) if isinstance(k, str) else "%s[%d]" % (path, k)
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
# Output hygiene: tokens in URLs, and spreadsheet formulas in CSV cells
# --------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"(?i)((?:^|[?&;])(?:access_)?token=)[^&#\s\"']*")


def redact(text: Any) -> Any:
    """Blank the value of a token= query parameter. Other values pass through.

    A web map can carry a signed URL, and a library error message repeats the
    request URL, token included. changes.csv and run.log get shared in tickets
    and forum threads, so neither may hold a live token. The per-item backups
    keep the original data verbatim: a rollback needs it.
    """
    if not isinstance(text, str):
        return text
    return _TOKEN_RE.sub(r"\1[redacted]", text)


# OWASP "CSV Injection": a cell that starts with one of these is run as a
# formula by Excel, LibreOffice and Google Sheets.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r", "\n")


def cell(value: Any) -> Any:
    """A CSV cell that a spreadsheet will not run as a formula.

    Text that starts with = + - @ tab CR or LF gets one leading apostrophe.
    Numbers stay numbers, because a negative count is not a formula, and None
    becomes an empty cell. This tool never reads changes.csv back, so there is
    no strip on read.
    """
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    if text.startswith(_FORMULA_LEAD):
        return "'" + text
    return text


def out_cell(value: Any) -> Any:
    """redact, then guard. Every text cell of changes.csv goes through this."""
    return cell(redact(value))


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
            return "%s: %s" % (e.get("code", "?"), e.get("message", e))
        return str(e)
    return repr(resp)


# An HTTP status counts only as a whole token. A bare substring test read the
# 503 in an item id, or the 429 in a port number, as a throttling response.
_TRANSIENT_STATUS = re.compile(r"(?<![0-9A-Za-z])(?:429|502|503|504)(?![0-9A-Za-z])")
_TRANSIENT_WORDS = ("Timeout", "timed out", "Connection", "Unable to connect",
                    "temporarily")


def is_transient(msg: str) -> bool:
    """Is this error text a throttle or a transient server error worth a retry?"""
    return bool(_TRANSIENT_STATUS.search(msg)) or any(w in msg for w in _TRANSIENT_WORDS)


def with_retry(fn, *a, what: str = "call", tries: int = RETRY_TRIES, **kw):
    """Retry on AGOL throttling / transient server errors with backoff."""
    for attempt in range(1, tries):
        try:
            return fn(*a, **kw)
        except Exception as e:  # arcgis wraps HTTP errors in many classes
            msg = str(e)
            if not is_transient(msg):
                raise
            wait = min(RETRY_MAX_WAIT, 2 ** attempt) + random.uniform(0, 1)
            log.warning("%s failed (attempt %d/%d): %s. Retrying in %.1fs",
                        what, attempt, tries, msg[:200], wait)
            time.sleep(wait)
    return fn(*a, **kw)  # the last attempt: whatever it raises is the caller's


# --------------------------------------------------------------------------
# Enumeration
# --------------------------------------------------------------------------
def iter_items(gis, item_types: list[str], extra_query: str = "",
               max_items: int = 0, stats: dict | None = None) -> Iterator:
    """Yield every matching item, paging with a stable sort (see SORT_FIELD).

    `stats`, when given, receives "reported" (the server's total) and "seen"
    (items actually returned) so the caller can tell a complete scan from a
    partial one. "limited" is True when --max-items cut the scan short on
    purpose.
    """
    if stats is None:
        stats = {}
    stats["reported"], stats["seen"], stats["limited"] = None, 0, False
    orgid = gis.properties.id
    type_clause = " OR ".join('type:"%s"' % t for t in item_types)
    q = "orgid:%s AND (%s)" % (orgid, type_clause)
    if extra_query.strip():
        q += " AND (%s)" % extra_query.strip()
    log.info("query: %s", q)

    seen: set[str] = set()
    start, page, yielded = 1, PAGE_SIZE, 0
    while True:
        r = with_retry(gis.content.advanced_search, query=q, max_items=page,
                       start=start, sort_field=SORT_FIELD, sort_order="asc",
                       what="advanced_search")
        if stats["reported"] is None:
            stats["reported"] = r.get("total") or 0
            log.info("server reports %s matching items", stats["reported"])
        results = r.get("results") or []
        if not results:
            break
        for it in results:
            if it.id in seen:
                continue
            seen.add(it.id)
            stats["seen"] = len(seen)
            yield it
            yielded += 1
            if max_items and yielded >= max_items:
                stats["limited"] = True
                return
        nxt = r.get("nextStart", -1)
        # `<= start`, not `<= 0`: a server that keeps returning the same
        # nextStart would otherwise spin forever.
        if nxt is None or nxt <= 0 or nxt <= start:
            break
        start = nxt

    if stats["reported"] and stats["seen"] < stats["reported"]:
        why = ("a search returns at most the top 10,000 results"
               if stats["reported"] > 10000 else
               "a search returns only items the signed-in account can access")
        log.warning("enumerated %d of %s reported items. Some content was "
                    "not returned (%s)", stats["seen"], stats["reported"], why)


def is_incomplete(stats: dict) -> bool:
    """True when the server reported more items than the scan received.

    A scan that --max-items stopped on purpose is a sample, not a shortfall.
    """
    if stats.get("limited"):
        return False
    return bool(stats.get("reported")) and stats.get("seen", 0) < stats["reported"]


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


def _refuse_error_body(doc: Any, what: str) -> Any:
    """Raise when a 200 response carries an error body instead of content.

    The raw fetch uses try_json=False, so the library does not look at the body.
    A 403 or 498 then arrives as {"error": {...}}, which would be read as an item
    with nothing to rewrite. No item or resource document has "error" as its
    only key.
    """
    if isinstance(doc, dict) and set(doc) == {"error"}:
        raise RuntimeError("%s request refused: %s" % (what, err_text(doc)))
    return doc


def fetch_data(item) -> Any:
    """Get item data, distinguishing 'empty' from 'fetch failed'.

    Item.get_data() swallows every exception and returns {}. That makes a token
    expiry or a 500 indistinguishable from a map with no matching layers, so we
    re-fetch the raw payload ourselves when get_data returns something falsy.
    """
    data = with_retry(item.get_data, what="get_data %s" % item.id)
    if data:
        return data
    # get_data returned {} / None / b''. Confirm that is genuinely the case
    # rather than a swallowed error, by asking the sharing API directly.
    gis = item._gis
    url = "%scontent/items/%s/data" % (gis._portal.resturl, item.id)
    resp = with_retry(gis._con.get, url, {"f": "json"}, try_json=False,
                      what="raw data %s" % item.id)
    if isinstance(resp, bytes):
        resp = resp.decode("utf-8", "replace")
    if isinstance(resp, str):
        resp = resp.strip()
        if not resp:
            return {}
        try:
            resp = json.loads(resp)
        except ValueError:
            raise RuntimeError("item data is not JSON (%d bytes)" % len(resp))
        return _refuse_error_body(resp, "item data")
    return _refuse_error_body(resp, "item data") or {}


def process_item(item, old: str, new: str, opts: MatchOpts, apply: bool,
                 backup_dir: Path, scan_resources: bool = SCAN_RESOURCES) -> Result:
    base = Result(item.id, item.title or "", item.type, item.owner, "clean")
    try:
        data = fetch_data(item)
    except Exception as e:
        base.status, base.error = "failed", "fetch: %s" % e
        return base
    if not isinstance(data, (dict, list)):
        base.status, base.error = "skipped", "data is %s" % type(data).__name__
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
        (backup_dir / ("%s.json" % item.id)).write_text(original, encoding="utf-8")
        try:
            # update() returns False on a server-side refusal WITHOUT raising --
            # e.g. an item you can read but not edit. Treating that as success is
            # how you end a run believing you fixed maps you did not touch.
            resp = with_retry(item.update, data=data, what="update %s" % item.id)
            if not succeeded(resp):
                base.status = "failed"
                base.error = "server refused the write: %s" % err_text(resp)
                return base
        except Exception as e:
            base.status, base.error = "failed", "update: %s" % e
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
        listing = with_retry(item.resources.list, what="resources.list %s" % item.id)
    except Exception as e:
        return hits, opaque, "resources.list: %s" % e

    for entry in listing or []:
        name = entry.get("resource") if isinstance(entry, dict) else str(entry)
        if not name:
            continue
        if name.lower().endswith(OPAQUE_RESOURCE_EXT):
            opaque.append(name)
            continue
        if not name.lower().endswith(".json"):
            continue
        # A draft that cannot be read is a draft that may still hold the old
        # URL. Report the item as failed rather than clean.
        try:
            doc = with_retry(item.resources.get, file=name, try_json=True,
                             what="resources.get %s/%s" % (item.id, name))
            _refuse_error_body(doc, "resource")
        except Exception as e:
            return hits, opaque, "resources.get %s: %s" % (name, e)
        if not isinstance(doc, (dict, list)):
            return hits, opaque, "resources.get %s: the resource is not JSON" % name

        original = json.dumps(doc, ensure_ascii=False)
        rhits = walk_replace(doc, old, new, opts, path="[resource:%s]" % name)
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
            # Upload a file, never text=. AGOL HTML-sanitizes an updateResources
            # text= body: it strips style= attributes and blanks image sources.
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / leaf
                src.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
                resp = with_retry(item.resources.update, file=str(src),
                                  folder_name=folder or None, file_name=leaf,
                                  what="resources.update %s/%s" % (item.id, name))
            if not succeeded(resp):
                return hits, opaque, "resources.update %s: %s" % (name, err_text(resp))
        except Exception as e:
            return hits, opaque, "resources.update %s: %s" % (name, e)

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
                          what="update url %s" % item.id)
        ok = succeeded(resp)
        r.status = "updated" if ok else "failed"
        if not ok:
            r.error = "server refused the write: %s" % err_text(resp)
    except Exception as e:
        r.status, r.error = "failed", "update url: %s" % e
    return r


# --------------------------------------------------------------------------
# Report core: pure functions, no I/O
# --------------------------------------------------------------------------
def csv_rows(results: list[Result]) -> list[list]:
    """The rows of changes.csv, header first. Every text cell is guarded."""
    rows: list[list] = [list(CSV_COLUMNS)]
    for r in results:
        head = [r.item_id, r.title, r.type, r.owner, r.status]
        if r.hits:
            for h in r.hits:
                rows.append([out_cell(c) for c in
                             head + [h.path, h.before, h.after, r.error]])
        elif r.status in ("failed", "skipped"):
            rows.append([out_cell(c) for c in head + ["", "", "", r.error]])
    return rows


def plan_types(types: list[str], include_item_url: bool) -> tuple[list[str], list[str]]:
    """(item types to enumerate, service types named without --include-item-url)."""
    if include_item_url:
        return list(types) + [t for t in SERVICE_ITEM_TYPES if t not in types], []
    return list(types), [t for t in types if t in SERVICE_ITEM_TYPES]


def exit_status(failed: int, incomplete: bool) -> int:
    """2 beats 3: a failed write matters more than an incomplete listing."""
    if failed:
        return EXIT_FAILED
    if incomplete:
        return EXIT_INCOMPLETE
    return EXIT_OK


def validate_urls(old: str, new: str) -> None:
    """Reject inputs that would cause mass damage. Call with STRIPPED values."""
    for label, u in (("old", old), ("new", new)):
        if not u or not u.strip():
            raise SystemExit("--%s-url is empty. Refusing to run: an "
                             "empty old URL would match every string." % label)
        if not re.match(r"^https?://[^/\s]+/", u.strip() + "/"):
            raise SystemExit("--%s-url is not an absolute http(s) URL: %r" % (label, u))
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
            "new URL is nested under the old one:\n  old: %s\n  new: %s\n"
            "Rewritten URLs would still match the old prefix, so a second run "
            "would apply the change twice. Narrow --old-url so it does not "
            "prefix --new-url." % (old, new))


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
    return ("old URL does not end at a service endpoint (%r). It will "
            "match every URL beginning with that prefix, which may be more "
            "than one service." % path)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
class _RedactFilter(logging.Filter):
    """Blank token= values in every record, whichever library wrote it."""

    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info:
            # A traceback is formatted after the filters run, from exc_info.
            # Format it here, blank the tokens, and drop exc_info.
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


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
    # arcgis can pass the auth token as a GET parameter (the legacy sign-in
    # default in 2.4.2). Without this cap, a live
    # token lands in run.log, which is precisely the file someone pastes into a
    # forum thread when asking why their run failed. The redact filter below is
    # the second layer: it blanks token= values whatever level they arrive at.
    for noisy in ("urllib3", "requests", "urllib3.connectionpool"):
        logging.getLogger(noisy).setLevel(logging.INFO)

    fh = logging.FileHandler(out_dir / "run.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)-7s %(message)s"))
    fh.setLevel(logging.DEBUG)
    fh.addFilter(_RedactFilter())

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.addFilter(lambda r: r.name.startswith(log.name) or r.levelno >= logging.WARNING)
    ch.addFilter(_RedactFilter())

    root.handlers[:] = [fh, ch]


def teardown_logging() -> None:
    """Close run.log. A handle left open blocks deleting the out-dir on Windows."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.close()
    root.handlers[:] = []


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bulk-replace REST service URLs in AGOL/Portal content. "
                    "Dry-run unless --apply is given.",
        allow_abbrev=False)
    p.add_argument("--self-test", action="store_true",
                   help="run the offline self-test against a local stub server and exit")
    # Defaults come from the CONFIGURATION block; env vars beat it; flags win.
    p.add_argument("--org", default=os.environ.get("AGOL_URL") or ORG_URL)
    p.add_argument("--user", default=os.environ.get("AGOL_USER") or USERNAME or None)
    p.add_argument("--password", default=os.environ.get("AGOL_PASS") or PASSWORD or None)
    p.add_argument("--old-url", default=OLD_URL or None)
    p.add_argument("--new-url", default=NEW_URL or None)
    p.add_argument("--types", default=",".join(DEFAULT_TYPES),
                   help="comma-separated item types. Known data-bearing types: "
                        "%s" % ", ".join(KNOWN_DATA_TYPES))
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
    return p


_ARCGIS_MISSING = (
    "The arcgis package was not found. Install it with\n"
    "  pip install arcgis\n"
    "or run this with the Python that ships with ArcGIS Pro:\n"
    r'  "C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" '
    "agol_relink.py"
)


def connect(org: str, user: str, pwd: str):
    """Import arcgis late (so --self-test needs none) and sign in."""
    try:
        from arcgis.gis import GIS
    except Exception as e:
        if isinstance(e, ModuleNotFoundError) and e.name == "arcgis":
            sys.exit(_ARCGIS_MISSING)
        sys.exit("Importing the arcgis package failed: %s\nIf this runs as a "
                 "scheduled task, use a Python that has arcgis but not arcpy "
                 "(see the README)." % redact(str(e)))
    log.info("connecting to %s as %s ...", org, user)
    # NOTE: no verify_cert=False. Disabling TLS verification would POST these
    # credentials over an unvalidated connection. If you target a Portal with a
    # private CA, set REQUESTS_CA_BUNDLE to the CA path instead.
    try:
        gis = GIS(org, user, pwd)
    except Exception as e:
        sys.exit("Could not sign in to %s as %s: %s\nCheck the organization "
                 "URL, the username and the password." % (org, user, redact(str(e))))
    log.info("connected as %s (role=%s, org=%s)",
             gis.users.me.username, gis.users.me.role, gis.properties.id)
    return gis


def confirm_apply(old: str, new: str) -> bool:
    print("\n*** --apply will MODIFY items in this organization. ***")
    print("    %s\n -> %s\n" % (redact(old), redact(new)))
    return input("Type APPLY to continue: ").strip() == "APPLY"


def record_updated(path: Path, item_id: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(item_id + "\n")


@dataclass
class Scan:
    results: list = field(default_factory=list)
    counts: dict = field(default_factory=lambda: {
        "clean": 0, "matched": 0, "updated": 0, "skipped": 0,
        "failed": 0, "resumed": 0})
    opaque_items: list = field(default_factory=list)
    n: int = 0
    stats: dict = field(default_factory=dict)


def log_result(n: int, r: Result, item) -> None:
    title = (item.title or "")[:60]
    if r.hits:
        log.info("[%d] %-8s %s  %s  (%d url%s)", n, r.status, item.id, title,
                 len(r.hits), "" if len(r.hits) == 1 else "s")
        for h in r.hits[:20]:
            log.debug("        %s\n          %s\n       -> %s",
                      h.path, h.before, h.after)
        if len(r.hits) > 20:
            log.debug("        ... %d more", len(r.hits) - 20)
    elif r.status in ("failed", "skipped"):
        log.warning("[%d] %-8s %s  %s  : %s", n, r.status, item.id, title, r.error)
    else:
        log.debug("[%d] clean    %s  %s", n, item.id, title)


def scan_org(gis, args, old: str, new: str, opts: MatchOpts, apply: bool,
             scan_types: list[str], done: set, out_dir: Path) -> Scan:
    scan = Scan()
    backup_dir = out_dir / "backups"
    updated_ids = out_dir / "updated_ids.txt"
    for item in iter_items(gis, scan_types, args.query, args.max_items, scan.stats):
        scan.n += 1
        if item.id in done:
            scan.counts["resumed"] += 1
            continue

        if item.type in SERVICE_ITEM_TYPES:
            if args.include_item_url:
                r = process_item_url(item, old, new, opts, apply)
                if r:
                    scan.results.append(r)
                    scan.counts[r.status] = scan.counts.get(r.status, 0) + 1
                    log.info("[%d] %-8s %s  %s", scan.n, r.status, item.id,
                             (item.title or "")[:60])
                    if r.status == "updated":
                        record_updated(updated_ids, item.id)
            continue

        r = process_item(item, old, new, opts, apply, backup_dir,
                         scan_resources=not args.no_resources)
        scan.results.append(r)
        scan.counts[r.status] = scan.counts.get(r.status, 0) + 1
        if r.opaque:
            scan.opaque_items.append((item.id, r.opaque))
        log_result(scan.n, r, item)
        if r.hits and r.status == "updated":
            record_updated(updated_ids, item.id)
    return scan


def write_changes(csv_path: Path, results: list[Result]) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(csv_rows(results))


def log_summary(scan: Scan, csv_path: Path, apply: bool) -> None:
    total_urls = sum(len(r.hits) for r in scan.results)
    log.info("")
    log.info("scanned %d items", scan.n)
    for k in ("matched", "updated", "clean", "skipped", "failed", "resumed"):
        if scan.counts.get(k):
            log.info("  %-8s %d", k, scan.counts[k])
    log.info("  %-8s %d", "urls", total_urls)
    log.info("report : %s", csv_path.resolve())
    if scan.opaque_items:
        log.warning("")
        log.warning("%d changed item(s) also carry binary resources this tool "
                    "cannot read. URLs inside them are NOT rewritten. Check "
                    "them by hand:", len(scan.opaque_items))
        for iid, names in scan.opaque_items[:15]:
            log.warning("  %s  %s", iid, ", ".join(names[:5]))
        if len(scan.opaque_items) > 15:
            log.warning("  ... %d more (see run.log)", len(scan.opaque_items) - 15)
    if not apply and total_urls:
        log.info("")
        log.info("DRY-RUN. Nothing was written. Review %s, then re-run with "
                 "--apply to commit.", csv_path.name)


def read_resume(resume_dir: str) -> set:
    """Ids a previous run confirmed written. Only updated_ids.txt is read."""
    rp = Path(resume_dir) / "updated_ids.txt"
    if not rp.exists():
        return set()
    done = {ln.strip() for ln in rp.read_text(encoding="utf-8").splitlines()
            if ln.strip()}
    log.info("resume    : skipping %d already-updated items", len(done))
    return done


def run(args, org: str, user: str, pwd: str, old: str, new: str, apply: bool,
        out_dir: Path) -> int:
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

    done = read_resume(args.resume) if args.resume else set()
    gis = connect(org, user, pwd)
    if apply and not args.yes and not confirm_apply(old, new):
        log.info("aborted by operator")
        return EXIT_REFUSED

    scan_types, ignored = plan_types(types, args.include_item_url)
    if ignored:
        # Otherwise these get enumerated and then silently skipped, which reads
        # as "nothing to fix" rather than "you forgot a flag".
        log.warning("%s in --types but --include-item-url is off: those items "
                    "will be skipped, not scanned.", ", ".join(ignored))

    scan = scan_org(gis, args, old, new, opts, apply, scan_types, done, out_dir)
    csv_path = out_dir / "changes.csv"
    write_changes(csv_path, scan.results)
    log_summary(scan, csv_path, apply)

    incomplete = is_incomplete(scan.stats)
    status = exit_status(scan.counts.get("failed", 0), incomplete)
    if scan.counts.get("failed"):
        log.warning("%d items FAILED, see the report", scan.counts["failed"])
    if incomplete:
        log.warning("INCOMPLETE: %d of %s reported items were returned. A clean "
                    "result does not cover the rest. Exit status %d.",
                    scan.stats["seen"], scan.stats["reported"], EXIT_INCOMPLETE)
    return status


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

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
    out_dir = Path(args.out_dir) if args.out_dir else Path("agol_relink_%s" % stamp)
    (out_dir / "backups").mkdir(parents=True, exist_ok=True)
    setup_logging(out_dir, args.verbose)
    try:
        return run(args, org, user, pwd, old, new, apply, out_dir)
    except Exception as e:  # a library error text can repeat the request url
        log.error("aborted by an unexpected error: %s: %s. No changes.csv was "
                  "written. Items already written are in updated_ids.txt: "
                  "re-run with --resume %s", type(e).__name__, e, out_dir)
        log.debug("traceback", exc_info=True)
        return EXIT_REFUSED
    finally:
        teardown_logging()


# --------------------------------------------------------------------------
# Self-test: a stub org behind a real local HTTP server
#
# The stub speaks the sharing REST paths this tool reaches through the arcgis
# package. _StubGIS and _StubItem implement exactly the method surface the tool
# calls (see the README, "What the self-test does not cover") and send every
# call over HTTP to the stub server. The arcgis package itself is not imported.
# --------------------------------------------------------------------------
_STUB_TOKEN = "TOKEN-SECRET-7731"
_STUB_PASSWORD = "PASSWORD-SECRET-4410"
_STUB_USER = "tester"


class _StubOrg(object):
    """In-memory org. Items are dicts; every request is logged."""

    def __init__(self, items=None):
        self.items = items or {}
        self.requests = []
        self.faults = {}          # path fragment -> [(http status, payload)]
        self.total_override = None
        self.page_cap = 0
        self.stall = False
        self.server = None
        self.thread = None

    def start(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.server.daemon_threads = True
        self.server.org = self
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def writes(self):
        """POSTs that change content. generateToken is a sign-in, not a write."""
        return [r for r in self.requests
                if r[0] == "POST" and not r[1].endswith("/generateToken")]

    def pop_fault(self, path):
        for key, queue in self.faults.items():
            if key in path and queue:
                return queue.pop(0)
        return None

    def op_search(self, params):
        types = re.findall(r'type:"([^"]+)"', params["q"])
        ids = sorted(i for i, it in self.items.items() if it["type"] in types)
        start, num = int(params["start"]), int(params["num"])
        if self.page_cap:
            num = min(num, self.page_cap)
        page = ids[start - 1:start - 1 + num]
        nxt = start + len(page) if start - 1 + len(page) < len(ids) else -1
        if self.stall:
            nxt = start
        total = len(ids) if self.total_override is None else self.total_override
        rows = [dict(self.items[i], id=i) for i in page]
        rows = [dict((k, v) for k, v in row.items()
                     if k in ("id", "title", "type", "owner", "url")) for row in rows]
        return 200, {"total": total, "start": start, "num": len(page),
                     "nextStart": nxt, "results": rows}

    def op_data(self, iid, params, name):
        item = self.items[iid]
        if "raw" in item:
            return 200, item["raw"].encode("utf-8")
        if item["data"] is None:
            return 200, b""
        return 200, item["data"]

    def op_resources(self, iid, params, name):
        item = self.items[iid]
        if name is None:
            names = list(item["resources"]) + item.get("ghosts", [])
            return 200, {"resources": [{"resource": n} for n in names]}
        if name in item["resources"]:
            return 200, item["resources"][name].encode("utf-8")
        return 404, {"error": {"code": 404, "message": "Resource does not exist."}}

    def op_update(self, iid, params, name):
        item = self.items[iid]
        if item.get("refuse"):
            return 200, {"success": False}
        if "data" in params:
            item["data"] = json.loads(params["data"])
        if "url" in params:
            item["url"] = params["url"]
        return 200, {"success": True, "id": iid}

    def op_updateResources(self, iid, params, name):
        item = self.items[iid]
        if item.get("refuse_res"):
            return 200, {"error": {"code": 403, "message": "Not allowed."}}
        key = params["fileName"]
        if "resourcesPrefix" in params:
            key = params["resourcesPrefix"] + "/" + key
        item["resources"][key] = params["file"]
        return 200, {"success": True, "itemId": iid}


class _StubHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return None

    def _send(self, code, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        org = self.server.org
        parts = urllib.parse.urlsplit(self.path)
        params = dict(urllib.parse.parse_qsl(parts.query))
        if method == "POST":
            size = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(size)
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("multipart/form-data"):
                msg = email.parser.BytesParser().parsebytes(
                    b"Content-Type: " + ctype.encode("ascii") + b"\r\n\r\n" + body)
                for part in msg.get_payload():
                    field_name = part.get_param("name", header="content-disposition")
                    params[field_name] = part.get_payload(decode=True).decode("utf-8")
                    if part.get_filename():
                        params["_filename"] = part.get_filename()
            else:
                params.update(urllib.parse.parse_qsl(body.decode("utf-8")))
        path = parts.path
        org.requests.append((method, path, params))
        if path.endswith("/generateToken"):
            ok = (params.get("username") == _STUB_USER
                  and params.get("password") == _STUB_PASSWORD)
            self._send(200, {"token": _STUB_TOKEN} if ok else
                       {"error": {"code": 400, "message": "Invalid username or password."}})
            return
        if params.get("token") != _STUB_TOKEN:
            self._send(200, {"error": {"code": 498, "message": "Invalid token."}})
            return
        fault = org.pop_fault(path)
        if fault:
            self._send(*fault)
            return
        rest = path[len("/sharing/rest/"):]
        if rest == "search":
            self._send(*org.op_search(params))
            return
        m = re.match(r"content/(?:users/[^/]+/)?items/([^/]+)/(\w+)(?:/(.+))?$", rest)
        iid, op, name = m.groups()
        self._send(*getattr(org, "op_" + op)(iid, params, name))


class _StubCon(object):
    def __init__(self):
        self.token = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, url, params, try_json=True):
        return self._send(url, params, None, try_json)

    def post(self, url, params, files=None):
        return self._send(url, {}, params, True, files)

    def _send(self, url, query, form, try_json, files=None):
        query = dict(query)
        if self.token:
            query["token"] = self.token
        full = url + "?" + urllib.parse.urlencode(query)
        body = None if form is None else urllib.parse.urlencode(form).encode("ascii")
        headers = {}
        if files:
            boundary = "stubboundary7731"
            parts = [('Content-Disposition: form-data; name="%s"' % k, str(v).encode("utf-8"))
                     for k, v in form.items()]
            parts += [('Content-Disposition: form-data; name="file"; filename="%s"' % leaf,
                       data) for leaf, data in files]
            body = b"".join(b"--" + boundary.encode("ascii") + b"\r\n" + head.encode("ascii")
                            + b"\r\n\r\n" + payload + b"\r\n" for head, payload in parts)
            body += b"--" + boundary.encode("ascii") + b"--\r\n"
            headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        try:
            with self.opener.open(urllib.request.Request(full, data=body,
                                                         headers=headers)) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            exc.close()
            # requests puts the full URL, token included, in its HTTPError text.
            raise RuntimeError("%d Server Error: %s for url: %s"
                               % (exc.code, exc.reason, full))
        return json.loads(raw) if try_json else raw


class _StubResources(object):
    def __init__(self, item):
        self._item = item

    def _url(self, tail):
        gis = self._item._gis
        return "%scontent/items/%s/resources%s" % (gis._portal.resturl, self._item.id, tail)

    def list(self):
        return self._item._gis._con.get(self._url(""), {"f": "json"})["resources"]

    def get(self, file, try_json=True):
        raw = self._item._gis._con.get(self._url("/" + file), {}, try_json=False)
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    def update(self, file, folder_name=None, file_name=None):
        gis = self._item._gis
        params = {"f": "json", "fileName": file_name}
        if folder_name is not None:
            params["resourcesPrefix"] = folder_name
        url = "%scontent/users/%s/items/%s/updateResources" % (
            gis._portal.resturl, self._item.owner, self._item.id)
        with open(file, "rb") as fh:
            files = [(os.path.basename(file), fh.read())]
        return gis._con.post(url, params, files=files)


class _StubItem(object):
    def __init__(self, gis, row):
        self._gis = gis
        self.id = row["id"]
        self.title = row.get("title")
        self.type = row["type"]
        self.owner = row.get("owner")
        self.url = row.get("url")
        self.resources = _StubResources(self)

    def get_data(self):
        """Like arcgis.gis.Item.get_data: every exception becomes {}."""
        url = "%scontent/items/%s/data" % (self._gis._portal.resturl, self.id)
        try:
            raw = self._gis._con.get(url, {"f": "json"}, try_json=False)
            doc = json.loads(raw) if raw else None
        except Exception:
            return {}
        if isinstance(doc, dict) and "error" in doc:
            return {}
        return doc

    def update(self, item_properties=None, data=None):
        params = {"f": "json"}
        if data is not None:
            params["data"] = json.dumps(data)
        if item_properties:
            params.update(item_properties)
        url = "%scontent/users/%s/items/%s/update" % (
            self._gis._portal.resturl, self.owner, self.id)
        return bool(self._gis._con.post(url, params).get("success"))


class _StubContent(object):
    def __init__(self, gis):
        self._gis = gis

    def advanced_search(self, query, max_items, start, sort_field, sort_order):
        resp = self._gis._con.get(
            self._gis._portal.resturl + "search",
            {"q": query, "num": max_items, "start": start,
             "sortField": sort_field, "sortOrder": sort_order})
        resp["results"] = [_StubItem(self._gis, row) for row in resp["results"]]
        return resp


class _StubGIS(object):
    """Stands in for arcgis.gis.GIS. Signs in over HTTP, so a wrong password fails."""

    def __init__(self, org_url, user, password):
        self._portal = types.SimpleNamespace(resturl=org_url + "/sharing/rest/")
        self._con = _StubCon()
        resp = self._con.post(self._portal.resturl + "generateToken",
                              {"username": user, "password": password})
        if "error" in resp:
            raise RuntimeError(resp["error"]["message"])
        self._con.token = resp["token"]
        self.properties = types.SimpleNamespace(id="ORG1")
        self.users = types.SimpleNamespace(
            me=types.SimpleNamespace(username=user, role="org_admin"))
        self.content = _StubContent(self)


class _Harness(object):
    """The check()/raises() recorder, a class so the self-test can test it."""

    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, label, exc=SystemExit):
        try:
            fn()
        except exc:
            self.check(True, label)
        except Exception as e:
            self.check(False, "%s (wrong exception %r)" % (label, e))
        else:
            self.check(False, "%s (no error raised)" % label)

    def footer(self):
        """Print the closing line and return the process exit status."""
        print("-" * 68)
        total = self.passed + len(self.failed)
        if self.failed:
            print("%d assertions, %d failed" % (total, len(self.failed)))
            for label in self.failed:
                print("  FAILED: %s" % label)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0


PIN = "  <-- pinned defect"

_OLD = "https://gis.example.org/public/rest/services/General/Boundaries/MapServer"
_NEW = "https://services1.arcgis.com/AbC123/arcgis/rest/services/Boundaries/FeatureServer"
_OTHER = "https://other.example.net/rest/services/X/MapServer"


class _Cli(object):
    """Runs main() against a stub org with the arcgis module faked and I/O captured."""

    def __init__(self, org, workdir):
        self.org = org
        self.url = org.start()
        self.workdir = workdir
        self.count = 0

    def out_dir(self):
        self.count += 1
        return os.path.join(self.workdir, "run%d" % self.count)

    def run(self, argv, answers=(), password=_STUB_PASSWORD, env=None,
            arcgis="stub"):
        answers = list(answers)
        finder = _FailingFinder(_IMPORT_ERRORS.get(arcgis))
        saved_env = dict((k, os.environ.pop(k)) for k in
                         ("AGOL_URL", "AGOL_USER", "AGOL_PASS") if k in os.environ)
        os.environ.update(env or {})
        saved_mods = dict((k, sys.modules.get(k)) for k in ("arcgis", "arcgis.gis"))
        saved_input, saved_getpass = builtins.input, getpass.getpass

        def fake_input(prompt=""):
            if not answers:
                raise EOFError("stdin is closed")
            return answers.pop(0)

        builtins.input = fake_input
        getpass.getpass = lambda prompt="": password
        for k in saved_mods:
            sys.modules.pop(k, None)
        if arcgis == "stub":
            fake = types.ModuleType("arcgis")
            fake_gis = types.ModuleType("arcgis.gis")
            fake_gis.GIS = _StubGIS
            sys.modules["arcgis"], sys.modules["arcgis.gis"] = fake, fake_gis
        else:
            sys.meta_path.insert(0, finder)
        out, err = io.StringIO(), io.StringIO()
        code, msg = None, ""
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else EXIT_REFUSED
            msg = "" if isinstance(e.code, int) else str(e.code)
        finally:
            builtins.input, getpass.getpass = saved_input, saved_getpass
            if finder in sys.meta_path:
                sys.meta_path.remove(finder)
            for k in saved_mods:
                sys.modules.pop(k, None)
            sys.modules.update(dict((k, v) for k, v in saved_mods.items() if v is not None))
            for k in ("AGOL_URL", "AGOL_USER", "AGOL_PASS"):
                os.environ.pop(k, None)
            os.environ.update(saved_env)
        return types.SimpleNamespace(code=code, out=out.getvalue(), err=err.getvalue(),
                                     msg=msg)

    def base(self, extra=(), out_dir=None, user=_STUB_USER, old=_OLD, new=_NEW):
        return (["--org", self.url, "--user", user, "--password", _STUB_PASSWORD,
                 "--old-url", old, "--new-url", new,
                 "--out-dir", out_dir or self.out_dir()] + list(extra))


_IMPORT_ERRORS = {
    "missing": ModuleNotFoundError("No module named 'arcgis'", name="arcgis"),
    "license": RuntimeError("The Product License has not been initialized."),
}


class _FailingFinder(object):
    """A meta-path finder that makes `import arcgis` fail with a chosen error."""

    def __init__(self, error):
        self.error = error

    def find_spec(self, name, path=None, target=None):
        if name == "arcgis":
            raise self.error
        return None


def _exit_codes(fn):
    """Run fn with stderr captured. Returns the SystemExit codes it raised, [] if none."""
    codes = []
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            fn()
    except SystemExit as e:
        codes.append(e.code)
    return codes


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def _tree_text(root):
    """Every file under root as (relative path, text), for grepping artefacts."""
    found = []
    for base, _dirs, names in os.walk(root):
        for name in names:
            full = os.path.join(base, name)
            with open(full, encoding="utf-8") as fh:
                found.append((os.path.relpath(full, root).replace(os.sep, "/"), fh.read()))
    return found


def _webmap(*urls):
    return {"operationalLayers": [{"url": u} for u in urls],
            "keep": {"url": _OTHER}}


def _selftest_core(h):
    """Pure functions: matcher, walk, validation, guards. No server."""
    check, raises = h.check, h.raises
    o = MatchOpts()

    def sub(u, old=_OLD, new=_NEW, opts=o):
        i = match_prefix(u, old, opts)
        return rewrite(u, i, new) if i >= 0 else None

    # --- shapes observed in the real org ---
    check(sub(_OLD) == _NEW, "service root rewrites")
    check(sub(_OLD + "/0") == _NEW + "/0", "sublayer index is kept")
    check(sub(_OLD + "/12") == _NEW + "/12", "multi-digit sublayer is kept")
    check(sub(_OLD + "/2/images/0e0c479fe6") == _NEW + "/2/images/0e0c479fe6",
          "renderer symbol image under the service is kept")
    check(sub(_OLD + "/") == _NEW + "/", "trailing slash is kept")
    check(sub(_OLD + "?f=json") == _NEW + "?f=json", "query string is kept")
    check(sub(_OLD + "/0/query") == _NEW + "/0/query", "a path beyond the sublayer is kept")
    check(sub(_OLD + "#frag") == _NEW + "#frag", "fragment is kept")
    vt = "https://tiles.arcgis.com/tiles/AbC123/arcgis/rest/services/BM/VectorTileServer"
    check(sub(vt + "/resources/styles/root.json", old=vt, new=_NEW) ==
          _NEW + "/resources/styles/root.json", "vector tile styleUrl is kept")

    # --- case / scheme forgiveness ---
    check(sub(_OLD.replace("https", "http")) == _NEW, "http matches https")
    check(sub(_OLD.upper()) == _NEW, "uppercase stored url matches")
    check(sub(_OLD, old=_OLD.upper()) == _NEW, "uppercase operator input matches")
    check(sub(_OLD.replace("https", "http"), opts=MatchOpts(any_scheme=False)) is None,
          "strict scheme opt-out refuses http for https")
    check(sub(_OLD.upper(), opts=MatchOpts(ignore_case=False)) is None,
          "case-sensitive opt-out refuses the uppercase url")

    # --- must NOT match: the boundary check is the whole safety story ---
    check(sub(_OLD + "_Old") is None, "sibling service sharing a prefix does not match")
    check(sub(_OLD + "Extra/0") is None, "prefix without a boundary does not match")
    check(sub(_OTHER) is None, "other host does not match")
    check(sub("") is None and sub(None) is None and sub(123) is None,
          "non-string and empty values never match")
    check(match_prefix(_OLD, "") == -1, "empty old_url never matches")
    check(match_prefix(_OLD, "///") == -1, "slash-only old_url never matches")

    # --- idempotence: running twice must not double-apply ---
    once = sub(_OLD + "/3")
    check(match_prefix(once, _OLD, o) == -1, "rewritten url does not re-match")

    # --- recursive walk over a web map shaped like the real fixtures ---
    doc = {
        "operationalLayers": [
            {"url": _OLD + "/1"},
            {"layerType": "GroupLayer", "layers": [
                {"url": _OLD + "/2"},
                {"layerType": "GroupLayer", "layers": [{"url": _OLD + "/3"}]},
            ]},
            {"layerDefinition": {"drawingInfo": {"renderer": {"symbol": {
                "url": "https://static.arcgis.com/images/Symbols/Red.png"}}}}},
        ],
        "tables": [{"url": _OLD + "/9"}],
        "baseMap": {"baseMapLayers": [
            {"url": _OLD + "/0"},
            {"styleUrl": _OLD + "/resources/styles/root.json"},
        ]},
        "presentation": {"slides": [
            {"baseMap": {"baseMapLayers": [{"url": _OLD}]}}]},
        "unknownFutureEsriKey": {"deeply": {"nested": {"url": _OLD + "/7"}}},
    }
    hits = walk_replace(doc, _OLD, _NEW, o)
    check(len(hits) == 8, "the walk finds 8 rewrites, found %d" % len(hits))
    check(doc["operationalLayers"][1]["layers"][1]["layers"][0]["url"] == _NEW + "/3",
          "two-level GroupLayer nesting is reached")
    check(doc["presentation"]["slides"][0]["baseMap"]["baseMapLayers"][0]["url"] == _NEW,
          "presentation slide basemap is reached")
    check(doc["unknownFutureEsriKey"]["deeply"]["nested"]["url"] == _NEW + "/7",
          "a path in no allowlist is reached")
    check(doc["operationalLayers"][2]["layerDefinition"]["drawingInfo"][
        "renderer"]["symbol"]["url"].startswith("https://static.arcgis.com"),
        "an unrelated symbol image is left alone")
    check(hits[0].path == ".operationalLayers[0].url", "a hit names its json path")
    check(walk_replace(doc, _OLD, _NEW, o) == [], "a second walk is a no-op")
    check(walk_replace("a string", _OLD, _NEW, o) == [] and walk_replace(7, _OLD, _NEW, o) == [],
          "a bare scalar has nothing to walk")
    scalars = {"n": 5, "z": None, "b": True, "f": 1.5, "u": _OLD}
    check([x.path for x in walk_replace(scalars, _OLD, _NEW, o)] == [".u"] and scalars["n"] == 5,
          "numbers, booleans and null are left alone")
    check(walk_replace({"u": _OLD}, _OLD.upper(), _OLD, o) == [],
          "a rewrite that changes nothing is not a hit")
    keyed = {_OLD: "value"}
    check(walk_replace(keyed, _OLD, _NEW, o) == [] and list(keyed) == [_OLD],
          "a url used as a dict key is not rewritten")

    deep = leaf = {}
    for _ in range(MAX_DEPTH + 5):
        leaf["k"] = {}
        leaf = leaf["k"]
    leaf["url"] = _OLD
    check(walk_replace(deep, _OLD, _NEW, o) == [], "a tree deeper than MAX_DEPTH stops, it does not crash")

    # --- new_url is never treated as a regex replacement template ---
    evil = r"https://x/\g<0>/FeatureServer"
    check(sub(_OLD + "/0", new=evil) == evil + "/0",
          "backreference syntax in new_url stays literal")
    check(sub(_OLD + "/0", new=r"https://x/a\Rb/FeatureServer") is not None,
          "an invalid regex escape in new_url does not raise")

    # --- succeeded(): the two write APIs return different shapes ---
    check(succeeded(True), "Item.update True is success")
    check(not succeeded(False), "Item.update False is failure")
    check(not succeeded(None), "Item.update None is failure")
    check(succeeded({"success": True, "itemId": "x"}), "resources.update ok is success")
    check(not succeeded({"success": False}), "explicit success false is failure")
    err = {"error": {"code": 404, "message": "Resource does not exist"}}
    check(bool(err) is True, "an error dict really is truthy")
    check(not succeeded(err), "an error dict does not count as success" + PIN)
    check("404" in err_text(err) and "does not exist" in err_text(err),
          "err_text names code and message")
    check(err_text({"error": "plain text"}) == "plain text", "err_text reads a string error")
    check(err_text(7) == "7", "err_text reprs a non-error value")

    # --- breadth warning ---
    check(not breadth_warning(_OLD), "a service root does not warn")
    check(not breadth_warning(_OLD + "/"), "a trailing slash does not warn")
    for t in ("FeatureServer", "ImageServer", "GeocodeServer", "VectorTileServer"):
        check(not breadth_warning("https://h/rest/services/X/" + t), "%s does not warn" % t)
    check("BARE HOST" in breadth_warning("https://gis.example.org"), "a bare host warns")
    check(bool(breadth_warning("https://gis.example.org/public")), "a partial path warns")
    check(match_prefix("https://gis.example.org/portal", "https://gis.example.org", o) >= 0,
          "a bare host matches /portal, which is why it warns")

    # --- validate_urls rejects the inputs that would cause mass damage ---
    for bad_old, bad_new in [("", _NEW), ("   ", _NEW), (_OLD, ""),
                             ("not-a-url", _NEW), (_OLD, _OLD), (_OLD, _OLD.upper())]:
        raises(lambda a=bad_old, b=bad_new: validate_urls(a, b),
               "validate_urls refuses %r -> %r" % (bad_old, bad_new))
    validate_urls(_OLD, _NEW)
    check(True, "a valid pair is accepted")
    host = "https://gis.example.org"
    raises(lambda: validate_urls(host, host + "/arcgis"),
           "new nested under old (web adaptor) is refused" + PIN)
    raises(lambda: validate_urls(host, host.replace("https", "http") + "/arcgis"),
           "nested detection sees through the scheme variant")
    raises(lambda: validate_urls(_OLD, _OLD + "/extra"), "new is old plus a path segment is refused")
    doubled = rewrite(host + "/x", match_prefix(host + "/x", host, o), host + "/arcgis")
    check(doubled == host + "/arcgis/x" and match_prefix(doubled, host, o) >= 0,
          "the rewritten url still matches old, hence the refusal")

    # --- --apply is the write flag. A unique prefix of it must never reach it. ---
    check(_exit_codes(lambda: build_parser().parse_args(["--ap"])) == [2],
          "unique prefix --ap is refused, not read as --apply" + PIN)
    check(_exit_codes(lambda: build_parser().parse_args(["--apply"])) == [],
          "--apply itself is accepted")

    # --- retry classification ---
    check(is_transient("HTTP Error 503: Service Unavailable"), "503 is transient")
    check(is_transient("429 Client Error: Too Many Requests"), "429 is transient")
    check(is_transient("Connection aborted"), "a connection error is transient")
    check(not is_transient("404 Not Found"), "404 is not transient")
    check(not is_transient("500 Server Error for url: http://127.0.0.1:50429/items/ab503cd"),
          "a 429 inside a port and a 503 inside an item id are not throttling" + PIN)

    # --- plan_types, exit_status ---
    check(plan_types(["Web Map"], False) == (["Web Map"], []), "plan_types: default scope")
    check(plan_types(["Web Map", "WMS"], False) == (["Web Map", "WMS"], ["WMS"]),
          "plan_types: a service type without the flag is reported")
    scan_types, ignored = plan_types(["Web Map", "WMS"], True)
    check(scan_types.count("WMS") == 1 and "Feature Service" in scan_types and not ignored,
          "plan_types: the flag adds each service type once")
    check(exit_status(0, False) == 0 and exit_status(1, False) == 2
          and exit_status(0, True) == 3 and exit_status(2, True) == 2,
          "exit_status: failure beats incomplete beats ok")
    check(is_incomplete({"reported": 5, "seen": 3}) and not is_incomplete({"reported": 3, "seen": 3})
          and not is_incomplete({"reported": 0, "seen": 0}) and not is_incomplete({}),
          "is_incomplete compares what was reported with what was seen")


def _selftest_csv(h):
    """The formula guard, field by field, and the redaction."""
    check = h.check
    hostile = ["=1+1", "+1", "-1", "@SUM(A1)", "\t=1", "\r=1", "\n=1"]
    for lead in hostile:
        check(cell(lead) == "'" + lead, "cell guards a leading %r" % lead[0] + PIN)
    check(cell("Hydrants") == "Hydrants" and cell("") == "" and cell(" =1") == " =1",
          "cell leaves ordinary text, empty text and a leading space alone")
    check(cell("a=b") == "a=b" and cell("x\t=1") == "x\t=1",
          "cell guards only the first character")
    check(cell("'=1") == "'=1", "cell does not double an apostrophe the text already starts with")
    check(cell(None) == "" and cell(5) == 5 and cell(-5) == -5 and cell(0.5) == 0.5,
          "numbers stay numbers and None becomes empty")
    check(cell(("=x",)) == "('=x',)", "a non-text value is converted to text first")

    # one hostile value per text field, through the real row builder
    fields = ["item_id", "title", "type", "owner", "status", "json_path",
              "url_before", "url_after", "error"]
    for i, name in enumerate(fields):
        for lead in ("=", "\t", "\r"):
            vals = ["a"] * 9
            vals[i] = lead + "x"
            hit = Hit(vals[5], vals[6], vals[7])
            res = Result(vals[0], vals[1], vals[2], vals[3], vals[4], [hit], vals[8])
            row = csv_rows([res])[1]
            check(row[i] == "'" + lead + "x" and all(c == "a" for j, c in enumerate(row) if j != i),
                  "changes.csv %s guards a leading %r and nothing else" % (name, lead) + PIN)

    fail = Result("a", "t", "Web Map", "o", "failed", [], "=bad")
    rows = csv_rows([fail, Result("b", "t", "Web Map", "o", "clean")])
    check(rows[0] == CSV_COLUMNS and len(rows) == 2 and rows[1][8] == "'=bad"
          and rows[1][5:8] == ["", "", ""],
          "a failed item with no hits gets one guarded row, a clean item none")
    real = Result("a", "Roads", "Web Map", "jsmith", "matched",
                  [Hit(".operationalLayers[0].url", _OLD + "/0", _NEW + "/0")])
    row = csv_rows([real])[1]
    check(row == ["a", "Roads", "Web Map", "jsmith", "matched",
                  ".operationalLayers[0].url", _OLD + "/0", _NEW + "/0", ""],
          "a realistic row is unchanged by the guard")

    # redaction
    tok = "https://h/rest/services/X/MapServer/0?f=json&token=abc123&x=1"
    check(redact(tok) == "https://h/rest/services/X/MapServer/0?f=json&token=[redacted]&x=1",
          "redact blanks a token value and keeps the rest" + PIN)
    check(redact("u?access_token=zzz#frag") == "u?access_token=[redacted]#frag",
          "redact blanks access_token")
    check(redact("TOKEN=abc") == "TOKEN=[redacted]" and redact("mytoken=abc") == "mytoken=abc",
          "redact matches a whole parameter name only")
    check(redact("u?api_key=zzz&authToken=yyy") == "u?api_key=zzz&authToken=yyy",
          "only token= and access_token= are blanked, other secret-like names are not")
    check(redact(7) == 7 and redact("no secrets here") == "no secrets here",
          "redact passes other values through")
    check(out_cell("a?token=SECRET")== "a?token=[redacted]" and out_cell("=a?token=SECRET") == "'=a?token=[redacted]",
          "out_cell redacts, then guards")


def _item(title, data, **extra):
    row = {"title": title, "type": "Web Map", "owner": "jsmith", "data": data,
           "resources": {}}
    row.update(extra)
    return row


def _scenario_dry_run(cli, h):
    """Dry run over a mixed org: hostile titles, drafts, opaque resources."""
    check = h.check
    tokened = _OLD + "/3?token=LEAK-IN-URL-9"
    cli.org.items.update({
        "m1": _item('=HYPERLINK("http://evil.example","x")',
                    {"operationalLayers": [{"url": _OLD + "/0"},
                                           {"layers": [{"url": _OLD + "/1"}]}],
                     "tables": [{"url": tokened}], "keep": {"url": _OTHER}},
                    owner="+owner",
                    resources={"draft_1.json": json.dumps({"a": [{"url": _OLD + "/5"}]}),
                               "config/config.json": json.dumps({"url": _OLD + "/6"}),
                               "thumb.png": "x", "notes.txt": "plain"},
                    ghosts=[""]),
        "m2": _item("Clean", {"keep": {"url": _OTHER}}),
        "m3": _item("Empty", None),
        "m4": _item("Number", None, raw="5"),
    })
    out_dir = cli.out_dir()
    r = cli.run(cli.base(["-v"], out_dir=out_dir))
    check(r.code == 0, "dry run exits 0, got %r" % (r.code,))
    check(cli.org.writes() == [], "a dry run sends no write request to the org" + PIN)
    check(not os.path.exists(os.path.join(out_dir, "updated_ids.txt"))
          and not os.listdir(os.path.join(out_dir, "backups")),
          "a dry run writes no backup and no updated_ids.txt")
    rows = _read_csv(os.path.join(out_dir, "changes.csv"))
    check(rows[0] == CSV_COLUMNS, "changes.csv has the nine columns")
    m1 = [row for row in rows[1:] if row[0] == "m1"]
    check(len(m1) == 5 and all(row[4] == "matched" for row in m1),
          "m1 yields 5 matched url rows: 3 in data and 2 in resources")
    check(m1[0][1].startswith("'=HYPERLINK(") and m1[0][3] == "'+owner",
          "a hostile title and owner reach the file guarded" + PIN)
    paths = [row[5] for row in m1]
    check("[resource:config/config.json].url" in paths and "[resource:draft_1.json].a[0].url" in paths,
          "draft resources are scanned and named by path")
    check(any(row[6].endswith("?token=[redacted]") for row in m1)
          and not any("LEAK-IN-URL-9" in c for row in rows for c in row),
          "a token in a matched url is blanked in changes.csv" + PIN)
    skipped = dict((row[0], row[8]) for row in rows[1:] if row[4] == "skipped")
    check(skipped == {"m3": "empty data, no matching resources", "m4": "data is int"},
          "empty data and non-dict data are reported as skipped with a reason")
    check(not any(row[0] == "m2" for row in rows[1:]), "a clean item gets no row")
    check("thumb.png" in r.out and "binary resources" in r.out,
          "the opaque resource is listed for manual follow-up")
    check(not any("notes.txt" in c for row in rows for c in row) and "notes.txt" not in r.out,
          "a text resource is neither scanned nor listed")
    check("DRY-RUN. Nothing was written" in r.out, "a dry run says nothing was written")
    check("[2] clean    m2  Clean" in r.out, "-v shows clean items")
    log_text = dict(_tree_text(out_dir))["run.log"]
    check("LEAK-IN-URL-9" not in log_text and "LEAK-IN-URL-9" not in r.out,
          "a token in a matched url is blanked in run.log and on the console" + PIN)
    sort_params = [p for (_m, path, p) in cli.org.requests if path.endswith("/search")]
    check(sort_params and all(p["sortField"] == SORT_FIELD and p["sortOrder"] == "asc"
                              for p in sort_params),
          "every search page sorts on the near-unique field")
    check('orgid:ORG1 AND (type:"Web Map")' == sort_params[0]["q"],
          "the query is scoped to the org and the item types")


def _scenario_apply(cli, h):
    """Apply, backups, idempotence, resume, resource-only edits."""
    check = h.check
    data_a = _webmap(_OLD + "/0", _OLD + "/1")
    cli.org.items.update({
        "a1": _item("A one", json.loads(json.dumps(data_a)),
                    resources={"config/config.json": json.dumps({"url": _OLD + "/6"}),
                               "draft_9.json": json.dumps({"u": _OLD + "/7"}),
                               "thumb.png": "x"}),
        "a2": _item("A two", {}, resources={"draft_2.json": json.dumps({"u": _OLD})}),
        "a3": _item("A three", {"keep": {"url": _OTHER}}),
    })
    out_dir = cli.out_dir()
    r = cli.run(cli.base(["--apply", "--yes"], out_dir=out_dir))
    check(r.code == 0, "apply exits 0, got %r" % (r.code,))
    check(cli.org.items["a1"]["data"]["operationalLayers"][0]["url"] == _NEW + "/0",
          "apply rewrites the item data on the org")
    check(cli.org.items["a1"]["resources"]["config/config.json"] == json.dumps({"url": _NEW + "/6"}),
          "apply rewrites a resource inside a folder")
    check(json.loads(cli.org.items["a2"]["resources"]["draft_2.json"]) == {"u": _NEW},
          "apply rewrites a root resource")
    check(not any(p.endswith("/items/a2/update") for (_m, p, _q) in cli.org.requests),
          "an item whose only match is a resource never has its /data written" + PIN)
    uploads = [q for (_m, p, q) in cli.org.requests if p.endswith("/updateResources")]
    check(len(uploads) == 3 and all("file" in q and "text" not in q for q in uploads)
          and all(q["_filename"] == q["fileName"] for q in uploads),
          "a resource is written as an uploaded file, never as text=" + PIN)
    ids = open(os.path.join(out_dir, "updated_ids.txt"), encoding="utf-8").read().split()
    check(ids == ["a1", "a2"], "updated_ids.txt lists the written items")
    backup = json.load(open(os.path.join(out_dir, "backups", "a1.json"), encoding="utf-8"))
    check(backup == data_a, "the backup holds the original data, not the rewritten data")
    names = sorted(os.listdir(os.path.join(out_dir, "backups", "resources", "a1")))
    check(names == ["config__config.json", "draft_9.json"],
          "resource backups use __ for a folder separator")
    rows = _read_csv(os.path.join(out_dir, "changes.csv"))
    check(all(row[4] == "updated" for row in rows[1:]) and len(rows) == 6,
          "changes.csv marks every written url as updated")
    check("DRY-RUN" not in r.out, "an apply run does not print the dry-run notice")

    before = len(cli.org.writes())
    again = cli.run(cli.base(["--apply", "--yes"]))
    check(again.code == 0 and len(cli.org.writes()) == before,
          "a second apply finds nothing and writes nothing")

    resumed = cli.run(cli.base(["--apply", "--yes", "--resume", out_dir]))
    check("resumed  2" in resumed.out and resumed.code == 0,
          "--resume skips the items the first run wrote")
    empty = os.path.join(cli.workdir, "empty_resume")
    os.makedirs(empty)
    with open(os.path.join(empty, "changes.csv"), "w", encoding="utf-8") as fh:
        fh.write("item_id\n'=a1\n")
    r2 = cli.run(cli.base(["--resume", empty]))
    check("resumed" not in r2.out and "resume    :" not in r2.out,
          "a resume dir with no updated_ids.txt skips nothing, and changes.csv is never read back")

    # the README's rollback lines, replayed against the stub
    item = _StubItem(_StubGIS(cli.url, _STUB_USER, _STUB_PASSWORD),
                     {"id": "a1", "type": "Web Map", "owner": "jsmith"})
    backups = Path(out_dir) / "backups"
    item.update(data=json.load(open(str(backups / "a1.json"), encoding="utf-8")))
    for p in (backups / "resources" / "a1").glob("*"):
        folder, _, leaf = p.name.replace("__", "/").rpartition("/")
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copyfile(str(p), str(Path(tmp) / leaf))
            item.resources.update(file=str(Path(tmp) / leaf), folder_name=folder or None,
                                  file_name=leaf)
    check(cli.org.items["a1"]["data"] == data_a
          and cli.org.items["a1"]["resources"]["config/config.json"] == json.dumps({"url": _OLD + "/6"})
          and cli.org.items["a1"]["resources"]["draft_9.json"] == json.dumps({"u": _OLD + "/7"}),
          "the documented rollback restores the data and both resources, folder included")


def _scenario_failures(cli, h):
    """Every failure mode ends in failed, exit 2, and no silent skip."""
    check = h.check
    cli.org.items.update({
        "f1": _item("Not JSON", None, raw="this is not json"),
        "f2": _item("Refused", _webmap(_OLD + "/0"), refuse=True),
        "f3": _item("Res refused", {}, refuse_res=True,
                    resources={"draft_1.json": json.dumps({"u": _OLD})}),
        "f4": _item("List fails", _webmap(_OLD)),
        "f5": _item("Update raises", _webmap(_OLD + "/2")),
        "f6": _item("Retried", _webmap(_OLD + "/4")),
        "f7": _item("Res raises", {}, resources={"draft_1.json": json.dumps({"u": _OLD})}),
        "f8": _item("Res listed, gone", {}, ghosts=["gone.json"]),
        "f9": _item("Res not JSON", {}, resources={"bad.json": "not json at all"}),
        "fa": _item("Res error body", {}, resources={"draft_1.json": json.dumps({"u": _OLD})}),
        "fb": _item("Data error body", _webmap(_OLD + "/9")),
    })
    boom = (500, {"error": {"code": 500, "message": "boom"}})
    denied = (200, {"error": {"code": 403, "message": "Not allowed."}})
    cli.org.faults = {"f4/resources": [boom], "f5/update": [boom],
                      "f6/data": [(503, {}), (429, {}), (503, {})],
                      "f7/updateResources": [boom],
                      "fa/resources/draft_1.json": [denied],
                      "fb/data": [denied, denied]}
    sleeps = []
    real_sleep = time.sleep
    time.sleep = sleeps.append
    try:
        out_dir = cli.out_dir()
        r = cli.run(cli.base(["--apply", "--yes", "--password", _STUB_PASSWORD],
                             out_dir=out_dir))
    finally:
        time.sleep = real_sleep
    check(r.code == 2, "failed items make the run exit 2, got %r" % (r.code,))
    rows = _read_csv(os.path.join(out_dir, "changes.csv"))
    err = dict((row[0], row[8]) for row in rows[1:] if row[4] == "failed")
    check(sorted(err) == ["f1", "f2", "f3", "f4", "f5", "f7", "f8", "f9", "fa", "fb"],
          "ten items are reported failed, one per failure mode")
    check(err["f1"].startswith("fetch: item data is not JSON"),
          "get_data's swallowed error is found by the raw re-fetch" + PIN)
    check(err["f2"].startswith("server refused the write: "),
          "Item.update returning False is a failure, not a success" + PIN)
    check(err["f3"].startswith("resources.update draft_1.json: 403: Not allowed."),
          "a resource error dict is a failure" + PIN)
    check(err["f4"].startswith("resources.list: "), "a failed resource listing is a failure")
    check(err["f5"].startswith("update: "), "an update that raises is a failure")
    check(err["f7"].startswith("resources.update draft_1.json: "),
          "a resource update that raises is a failure")
    check(err["f8"].startswith("resources.get gone.json: 404 Server Error"),
          "a listed resource that cannot be read fails the item, it is not skipped" + PIN)
    check(err["f9"] == "resources.get bad.json: the resource is not JSON",
          "a .json resource that is not JSON fails the item" + PIN)
    check(err["fa"].startswith("resources.get draft_1.json: resource request refused: 403: Not allowed."),
          "a resource read that answers 200 with an error body fails the item" + PIN)
    check(err["fb"].startswith("fetch: item data request refused: 403: Not allowed."),
          "an item data read that answers 200 with an error body is a failure, not clean" + PIN)
    check(cli.org.items["f6"]["data"]["operationalLayers"][0]["url"] == _NEW + "/4",
          "a 503 then a 429 are retried and the write lands")
    check(len(sleeps) == 2 and all(0 < s <= RETRY_MAX_WAIT + 1 for s in sleeps),
          "the retries back off, %d sleeps" % len(sleeps))
    blob = r.out + r.err + "".join(t for _p, t in _tree_text(out_dir))
    check(_STUB_TOKEN not in blob and "[redacted]" in blob,
          "a library error that repeats the request url does not leak the token" + PIN)
    check("10 items FAILED" in r.out, "the console says how many items failed")


def _scenario_crash(cli, h):
    """An uncaught error ends in one redacted line and exit 1, never a raw traceback."""
    check = h.check
    cli.org.items["s1"] = _item("S", _webmap(_OLD + "/0"))
    cli.org.faults = {"search": [(503, {})] * RETRY_TRIES}
    real_sleep = time.sleep
    time.sleep = lambda seconds: None
    try:
        out_dir = cli.out_dir()
        r = cli.run(cli.base(["--apply", "--yes"], out_dir=out_dir))
    finally:
        time.sleep = real_sleep
    run_log = dict(_tree_text(out_dir))["run.log"]
    check(r.code == EXIT_REFUSED and "aborted by an unexpected error: RuntimeError" in r.out,
          "a search that keeps failing ends in one line and exit 1, got %r" % (r.code,))
    check(_STUB_TOKEN not in r.out + r.err + run_log and "[redacted]" in r.out,
          "the session token is in no traceback, console line or log after a crash" + PIN)
    check(r.err == "" and "Traceback (most recent call last)" in run_log,
          "the traceback goes to run.log, redacted, and not to the console")
    check(not os.path.exists(os.path.join(out_dir, "changes.csv"))
          and "--resume %s" % out_dir in r.out,
          "a crash writes no changes.csv and the message names --resume")


def _scenario_enumeration(cli, h):
    """Incomplete listings exit 3. A sample run does not."""
    check = h.check
    for i in range(5):
        cli.org.items["e%d" % i] = _item("E%d" % i, {"keep": {"url": _OTHER}})
    cli.org.page_cap = 2
    full = cli.run(cli.base())
    check(full.code == 0 and "scanned 5 items" in full.out,
          "paging in pages of 2 reaches all 5 items")
    cli.org.total_override = 7
    short = cli.run(cli.base())
    check(short.code == 3 and "enumerated 5 of 7" in short.out and "INCOMPLETE" in short.out,
          "a server that reports 7 but returns 5 exits 3, not 0" + PIN)
    check("only items the signed-in account can access" in short.out,
          "the shortfall warning gives the documented reason")
    cli.org.total_override = 10001
    big = cli.run(cli.base())
    check(big.code == 3 and "at most the top 10,000" in big.out,
          "a total over 10,000 names the search cap")
    cli.org.total_override = None
    cli.org.stall = True
    stalled = cli.run(cli.base())
    check(stalled.code == 3 and "scanned 2 items" in stalled.out,
          "a server that repeats nextStart ends the scan and the run exits 3" + PIN)
    cli.org.stall = False
    cli.org.page_cap = 0
    cli.org.total_override = 9
    sample = cli.run(cli.base(["--max-items", "2"]))
    check(sample.code == 0 and "scanned 2 items" in sample.out,
          "--max-items stops early on purpose and is not an incomplete scan")
    cli.org.total_override = None
    cli.org.items["e0"]["data"] = None
    cli.org.items["e1"]["raw"] = "not json"
    both = cli.run(cli.base())
    check(both.code == 2, "a failure outranks an incomplete listing")


def _scenario_item_url(cli, h):
    """Service items: --include-item-url, and the warning when it is missing."""
    check = h.check
    svc = lambda url, **kw: dict(_item("Svc", {}, **kw), type="Feature Service", url=url)
    cli.org.items.update({
        "svc1": svc(_OLD),
        "svc2": svc(_OTHER),
        "svc3": svc(_OLD + "/1", refuse=True),
        "svc4": svc(_OLD + "/2"),
        "w1": _item("Web", _webmap(_OLD + "/0")),
    })
    cli.org.faults = {"svc4/update": [(500, {"error": {"code": 500, "message": "boom"}})]}
    types_arg = ["--types", "Web Map,Feature Service"]
    r = cli.run(cli.base(types_arg))
    check("Feature Service in --types but --include-item-url is off" in r.out,
          "naming a service type without the flag warns")
    check(r.code == 0 and "svc1" not in r.out and "matched  1" in r.out,
          "those service items are skipped, not scanned")
    out_dir = cli.out_dir()
    dry = cli.run(cli.base(["--include-item-url"], out_dir=out_dir))
    rows = _read_csv(os.path.join(out_dir, "changes.csv"))
    check(dry.code == 0 and cli.org.writes() == [] and
          sorted(row[0] for row in rows[1:]) == ["svc1", "svc3", "svc4", "w1"],
          "a dry run with --include-item-url lists the matching service items and writes nothing")
    check(any(row[5] == "item.url" and row[6] == _OLD for row in rows), "the item.url hit is named")
    applied = cli.run(cli.base(["--include-item-url", "--apply", "--yes"]))
    check(cli.org.items["svc1"]["url"] == _NEW, "apply rewrites the registered url of a service item")
    check(cli.org.items["svc2"]["url"] == _OTHER, "a service item that does not match is untouched")
    run_dir = os.path.join(cli.workdir, "run%d" % cli.count)
    errs = dict((row[0], row[8]) for row in _read_csv(os.path.join(run_dir, "changes.csv"))[1:]
                if row[4] == "failed")
    check(applied.code == 2 and errs["svc3"].startswith("server refused the write")
          and errs["svc4"].startswith("update url: "),
          "a refused or raising url update is a failure")
    ids = open(os.path.join(run_dir, "updated_ids.txt"), encoding="utf-8").read().split()
    check(ids == ["svc1", "w1"], "updated_ids.txt records the service item too")


def _scenario_volume(cli, h):
    """Many hits, many opaque items, --no-resources, and the stub's own auth."""
    check = h.check
    cli.org.items["big"] = _item("Big", _webmap(*[_OLD + "/%d" % i for i in range(22)]))
    for i in range(16):
        cli.org.items["o%02d" % i] = _item(
            "O%d" % i, _webmap(_OLD + "/0"),
            resources={"thumb.png": "x", "draft_1.json": json.dumps({"u": _OLD})})
    out_dir = cli.out_dir()
    r = cli.run(cli.base(["-v"], out_dir=out_dir))
    log_text = dict(_tree_text(out_dir))["run.log"]
    check(r.code == 0 and "... 2 more" in log_text,
          "an item with 22 urls logs 20 and counts the rest")
    check("16 changed item(s) also carry binary resources" in r.out
          and "... 1 more (see run.log)" in r.out,
          "the opaque list shows 15 items and counts the rest")
    rows = _read_csv(os.path.join(out_dir, "changes.csv"))
    check(len(rows) == 1 + 22 + 16 * 2, "changes.csv has one row per url, %d rows" % len(rows))
    lean = cli.run(cli.base(["--no-resources"]))
    check(lean.code == 0 and "binary resources" not in lean.out and "urls     38" in lean.out,
          "--no-resources skips the resources: 38 urls and no opaque list")
    bad = _StubCon()
    bad.token = "wrong"
    resp = bad.get(cli.url + "/sharing/rest/search", {"q": "x", "start": 1, "num": 1})
    check(resp["error"]["code"] == 498, "the stub refuses a request that carries the wrong token")


def _scenario_options(cli, h):
    """Matching flags, --query, and a run driven by the CONFIGURATION block alone."""
    check = h.check
    cli.org.items.update({
        "x1": _item("Upper", _webmap(_OLD.upper() + "/0")),
        "x2": _item("Http", _webmap(_OLD.replace("https", "http") + "/0")),
        "x3": _item("Exact", _webmap(_OLD + "/0")),
    })

    def matched(extra):
        out_dir = cli.out_dir()
        cli.run(cli.base(extra, out_dir=out_dir))
        rows = _read_csv(os.path.join(out_dir, "changes.csv"))
        return sorted(row[0] for row in rows[1:])

    check(matched([]) == ["x1", "x2", "x3"], "by default case and scheme are forgiven")
    check(matched(["--case-sensitive"]) == ["x2", "x3"], "--case-sensitive refuses the uppercase url")
    check(matched(["--strict-scheme"]) == ["x1", "x3"], "--strict-scheme refuses the http url")
    check(matched(["--case-sensitive", "--strict-scheme"]) == ["x3"], "both flags leave the exact match")
    cli.run(cli.base(["--query", 'owner:"jsmith"']))
    last = [p for (_m, path, p) in cli.org.requests if path.endswith("/search")][-1]
    check(last["q"].endswith(' AND (owner:"jsmith")'), "--query is added to the search as a clause")

    names = ("OLD_URL", "NEW_URL", "ORG_URL", "USERNAME", "PASSWORD", "APPLY", "ASSUME_YES")
    saved = dict((k, globals()[k]) for k in names)
    globals().update(OLD_URL=_OLD, NEW_URL=_NEW, ORG_URL=cli.url, USERNAME=_STUB_USER,
                     PASSWORD=_STUB_PASSWORD, APPLY=True, ASSUME_YES=True)
    try:
        before = len(cli.org.writes())
        configured = cli.run(["--out-dir", cli.out_dir()])
        wrote = len(cli.org.writes()) - before
        forced = cli.run(["--out-dir", cli.out_dir(), "--dry-run"])
    finally:
        globals().update(saved)
    check(configured.code == 0 and wrote == 3 and cli.org.items["x3"]["data"]["operationalLayers"][0]["url"] == _NEW + "/0",
          "the CONFIGURATION block alone drives a run, APPLY and ASSUME_YES included")
    check(forced.code == 0 and len(cli.org.writes()) - before == wrote and "DRY-RUN (no writes)" in forced.out,
          "--dry-run overrides APPLY = True in the CONFIGURATION block" + PIN)


def _scenario_cli(cli, h):
    """Prompts, confirmation, environment, sign-in and import failures."""
    check = h.check
    cli.org.items["c1"] = _item("C one", _webmap(_OLD + "/0"))
    cli.org.items["c2"] = dict(_item("Z", {}), url=_OLD, type="Feature Service")
    r = cli.run(["--org", cli.url, "--out-dir", cli.out_dir()],
                answers=[_STUB_USER, _OLD, _NEW])
    check(r.code == 0 and "matched  1" in r.out, "missing values are prompted for")
    n = len(cli.org.writes())
    no = cli.run(cli.base(["--apply"]), answers=["no"])
    check(no.code == 1 and "aborted by operator" in no.out and len(cli.org.writes()) == n,
          "anything but APPLY at the prompt aborts with exit 1 and writes nothing" + PIN)
    check(_STUB_PASSWORD not in no.out + no.err, "the confirmation never prints the password")
    cli.org.items["c1"]["data"] = _webmap(_OLD + "/0")
    yes = cli.run(cli.base(["--apply"]), answers=["APPLY"])
    check(yes.code == 0 and len(cli.org.writes()) > n and "MODIFY items" in yes.out,
          "typing APPLY writes")
    cli.org.items["c1"]["data"] = _webmap(_OLD + "/0")
    n = len(cli.org.writes())
    dry = cli.run(cli.base(["--apply", "--dry-run"]))
    check(dry.code == 0 and len(cli.org.writes()) == n and "DRY-RUN (no writes)" in dry.out,
          "--dry-run beats --apply")
    env = cli.run(["--org", cli.url, "--old-url", _OLD, "--new-url", _NEW,
                   "--out-dir", cli.out_dir()],
                  env={"AGOL_USER": _STUB_USER, "AGOL_PASS": _STUB_PASSWORD})
    check(env.code == 0 and "connected as tester" in env.out,
          "AGOL_USER and AGOL_PASS supply the credentials")
    envurl = cli.run(["--user", _STUB_USER, "--password", _STUB_PASSWORD, "--old-url", _OLD,
                      "--new-url", _NEW, "--out-dir", cli.out_dir()], env={"AGOL_URL": cli.url})
    check(envurl.code == 0, "AGOL_URL supplies the organization")
    flag = cli.run(cli.base(user="other"), env={"AGOL_USER": _STUB_USER})
    check(flag.code == 1 and "Could not sign in to %s as other" % cli.url in flag.msg,
          "a flag beats the environment")
    pw = cli.run(["--org", cli.url, "--user", _STUB_USER, "--old-url", _OLD,
                  "--new-url", _NEW, "--out-dir", cli.out_dir()], password="wrong-pw-1")
    check(pw.code == 1 and "Could not sign in" in pw.msg and "Invalid username or password" in pw.msg
          and "Traceback" not in pw.err + pw.out,
          "a wrong password is one actionable message, not a traceback" + PIN)
    check("wrong-pw-1" not in pw.msg + pw.out + pw.err, "the wrong password is not echoed")

    missing = cli.run(cli.base(), arcgis="missing")
    check(missing.code == 1 and "pip install arcgis" in missing.msg and "Pro" in missing.msg,
          "a missing arcgis package says how to install it")
    lic = cli.run(cli.base(), arcgis="license")
    check(lic.code == 1 and "Product License has not been initialized" in lic.msg
          and "arcpy" in lic.msg and "Traceback" not in lic.err,
          "an arcgis import that dies on the Pro licence is explained, not a traceback" + PIN)
    same = cli.run(cli.base(new=_OLD))
    check(same.code == 1 and "same" in same.msg, "bad urls are refused before any sign-in")
    arg = cli.run(["--ap"])
    check(arg.code == 2, "a write-flag prefix exits 2 through main as well" + PIN)
    typo = cli.run(["--pass", "hunter2SECRET"])
    check(typo.code == 2 and "unrecognized arguments: --pass hunter2SECRET" in typo.err,
          "a misspelt --password is refused and argparse echoes the argument (a known limit)")
    before = len(cli.org.writes())
    closed = cli.run(cli.base(["--apply"]))
    check(closed.code == 1 and "EOFError" in closed.out and len(cli.org.writes()) == before,
          "a closed stdin at the APPLY prompt exits 1 and writes nothing")
    tidy = logging.getLogger().handlers
    check(tidy == [], "main closes its log handlers, nothing is left open")
    broad = cli.run(cli.base(old="https://127.0.0.1", new="https://new.example.net/x"))
    check("BREADTH" in broad.out and "BARE HOST" in broad.out, "a bare host warns before the scan")


def _scenario_secrets(cli, h):
    """No credential reaches an output file, the console or the log."""
    check = h.check
    cli.org.items["k1"] = _item("K", _webmap(_OLD + "/0?token=" + _STUB_TOKEN))
    cli.org.faults = {"k1/data": [(500, {})]}
    out_dir = cli.out_dir()
    r = cli.run(cli.base(["--apply", "--yes", "-v"], out_dir=out_dir))
    files = _tree_text(out_dir)
    blob = r.out + r.err + "".join(t for p, t in files if not p.startswith("backups/"))
    check(_STUB_PASSWORD not in blob, "the password appears in no output file and not on the console" + PIN)
    check(_STUB_TOKEN not in blob, "the session token appears in no output file and not on the console" + PIN)
    check(len(files) >= 3, "the grep covered the log, the report and the backup")
    backup = dict(files).get("backups/k1.json", "")
    check(_STUB_TOKEN in backup,
          "the backup keeps the original data verbatim, token included, so a rollback works")


def _selftest_io(h):
    """Everything that touches HTTP, files or logging."""
    check = h.check
    tmp = tempfile.mkdtemp(prefix="agol_relink_selftest_")
    try:
        for name, scenario in (("dry", _scenario_dry_run), ("apply", _scenario_apply),
                               ("failures", _scenario_failures), ("crash", _scenario_crash),
                               ("enumeration", _scenario_enumeration),
                               ("item_url", _scenario_item_url),
                               ("volume", _scenario_volume), ("options", _scenario_options),
                               ("cli", _scenario_cli),
                               ("secrets", _scenario_secrets)):
            cli = _Cli(_StubOrg(), os.path.join(tmp, name))
            os.makedirs(cli.workdir)
            try:
                scenario(cli, h)
            finally:
                cli.org.stop()
        _selftest_units(h, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    check(not os.path.exists(tmp), "the self-test removed its temporary directory")


class _FakeItem(object):
    def __init__(self, iid):
        self.id = iid


class _FakeContent(object):
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def advanced_search(self, **kw):
        self.calls.append(kw)
        return self.pages.pop(0)


def _fake_gis(pages):
    content = _FakeContent(pages)
    return types.SimpleNamespace(properties=types.SimpleNamespace(id="O"),
                                 content=content), content


def _selftest_units(h, tmp):
    """Branches that need a crafted server answer rather than a stub org."""
    check = h.check
    gis, content = _fake_gis([
        {"total": 3, "results": [_FakeItem("a"), _FakeItem("b")], "nextStart": 3},
        {"total": 3, "results": [_FakeItem("b"), _FakeItem("c")], "nextStart": -1}])
    stats = {}
    got = [i.id for i in iter_items(gis, ["Web Map", "Dashboard"], ' owner:"x" ', 0, stats)]
    check(got == ["a", "b", "c"] and stats == {"reported": 3, "seen": 3, "limited": False},
          "a row repeated on the next page is yielded once")
    check(content.calls[0]["query"] == 'orgid:O AND (type:"Web Map" OR type:"Dashboard") AND (owner:"x")'
          and content.calls[0]["sort_field"] == SORT_FIELD,
          "the query joins the types and the extra clause")
    gis, _c = _fake_gis([{"total": None, "results": [], "nextStart": None}])
    stats = {}
    check(list(iter_items(gis, ["Web Map"], stats=stats)) == [] and stats["seen"] == 0,
          "an empty first page ends the scan with nothing seen")
    gis, _c = _fake_gis([{"total": 1, "results": [_FakeItem("a")], "nextStart": None},
                         {"total": 1, "results": [_FakeItem("a")], "nextStart": None}])
    check([i.id for i in iter_items(gis, ["Web Map"])] == ["a"], "iter_items runs without a stats dict")
    stats = {}
    check([i.id for i in iter_items(gis, ["Web Map"], stats=stats)] == ["a"] and stats["seen"] == 1,
          "a null nextStart ends the scan")

    # retry: non-transient errors raise at once, transient ones stop at the limit
    calls = []

    def flaky():
        calls.append(1)
        raise RuntimeError("503 Service Unavailable")

    sleeps = []
    real_sleep = time.sleep
    time.sleep = sleeps.append
    try:
        h.raises(lambda: with_retry(flaky, what="probe", tries=3),
                 "with_retry re-raises the error of the last attempt", exc=RuntimeError)
    finally:
        time.sleep = real_sleep
    check(len(calls) == 3 and len(sleeps) == 2, "with_retry makes 3 attempts and sleeps twice")
    check(_FailingFinder(None).find_spec("json") is None, "the import finder ignores other modules")
    check(with_retry(lambda: "once", tries=1) == "once", "with_retry with one try just calls")
    calls[:] = []

    def hard():
        calls.append(1)
        raise ValueError("404 not found")

    h.raises(lambda: with_retry(hard, tries=5), "a non-transient error is raised on the first attempt",
             exc=ValueError)
    check(len(calls) == 1, "a non-transient error is not retried")
    check(with_retry(lambda x: x + 1, 1) == 2, "with_retry passes arguments and returns the value")

    # fetch_data edge shapes
    class Item(object):
        id = "i"

        def __init__(self, first, raw):
            self._first, self._raw = first, raw
            self._gis = types.SimpleNamespace(
                _portal=types.SimpleNamespace(resturl="http://x/"),
                _con=types.SimpleNamespace(get=lambda *a, **k: self._raw))

        def get_data(self):
            return self._first

    check(fetch_data(Item({"a": 1}, None)) == {"a": 1}, "fetch_data returns truthy data as is")
    check(fetch_data(Item({}, b"")) == {} and fetch_data(Item(None, "  ")) == {},
          "an empty raw payload, bytes or text, is empty data")
    check(fetch_data(Item({}, b'{"u": 1}')) == {"u": 1}, "a bytes raw payload is decoded and parsed")
    check(fetch_data(Item({}, {"x": 1})) == {"x": 1} and fetch_data(Item({}, None)) == {},
          "a raw payload that is already parsed is returned, None becomes empty")
    h.raises(lambda: fetch_data(Item({}, {"error": {"code": 403, "message": "no"}})),
             "a parsed error body is refused, not read as empty data" + PIN, exc=RuntimeError)
    h.raises(lambda: fetch_data(Item({}, "<html>")), "an HTML raw payload is an error, not empty data",
             exc=RuntimeError)

    # depth warning and the redacting log filter
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    grab = Grab()
    log.addHandler(grab)
    log.setLevel(logging.DEBUG)
    try:
        deep = leaf = {}
        for _ in range(MAX_DEPTH + 3):
            leaf["k"] = {}
            leaf = leaf["k"]
        walk_replace(deep, _OLD, _NEW, MatchOpts())
    finally:
        log.removeHandler(grab)
    check(any("max depth exceeded" in m for m in records), "the depth limit logs a warning")
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "u %s", ("a?token=ZZZ",), None)
    check(_RedactFilter().filter(rec) and rec.getMessage() == "u a?token=[redacted]",
          "the log filter blanks a token in a formatted record" + PIN)

    # the harness itself
    inner = _Harness()
    real_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        inner.check(True, "ok")
        inner.check(False, "bad")
        inner.raises(lambda: None, "no raise")
        inner.raises(lambda: 1 / 0, "wrong raise")
        inner.raises(lambda: sys.exit(1), "right raise")
        failed_status = inner.footer()
        clean = _Harness()
        clean.check(True, "ok")
        clean_status = clean.footer()
    finally:
        sys.stdout = real_stdout
    check(failed_status == 1 and inner.passed == 2 and len(inner.failed) == 3
          and clean_status == 0, "the harness counts passes and failures and sets the exit status")

    # setup_logging: quiet console unless verbose, a closed handle afterwards
    quiet = os.path.join(tmp, "logging")
    console = io.StringIO()
    with contextlib.redirect_stdout(console):
        setup_logging(Path(quiet), False)
        log.debug("hidden")
        log.info("shown")
        logging.getLogger("urllib3").info("library chatter")
        logging.getLogger("other").info("filtered from the console")
        teardown_logging()
    check("shown" in console.getvalue() and "hidden" not in console.getvalue()
          and "chatter" not in console.getvalue() and "filtered" not in console.getvalue(),
          "the console shows this tool's info records and nothing else")
    text = open(os.path.join(quiet, "run.log"), encoding="utf-8").read()
    check("shown" in text and "hidden" in text and "library chatter" in text,
          "run.log keeps every record, library chatter included")
    # the root level stays DEBUG after teardown, so nothing else is needed here
    check(logging.getLogger().handlers == [], "teardown_logging removes every handler")


def self_test() -> int:
    """Assertions over the pure core, then over the CLI against a local stub org."""
    print("agol_relink self-test: no arcgis package, no organization, local stub server only")
    print("-" * 68)
    h = _Harness()
    _selftest_core(h)
    _selftest_csv(h)
    _selftest_io(h)
    return h.footer()


if __name__ == "__main__":  # pragma: no branch
    sys.exit(main())
