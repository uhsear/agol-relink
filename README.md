# agol-relink

Bulk-replace REST service URLs across ArcGIS Online / Portal content.

You moved a service — new server, new hostname, on-prem to hosted — and now
every web map, app, dashboard and Experience Builder site that pointed at the
old URL is broken. This finds all of them and rewrites the URLs.

**Dry-run by default. Nothing is written unless you pass `--apply`.**

Single file, no dependencies beyond the `arcgis` package you already have.

---

## Install

Needs Python 3.9+ and the [ArcGIS API for Python](https://developers.arcgis.com/python/).
If you have ArcGIS Pro, it is already installed — use Pro's interpreter:

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" agol_relink.py --self-test
```

Otherwise: `conda install -c esri arcgis` (or `pip install arcgis`).

Then just download `agol_relink.py`. There is nothing to build.

## Use

```bash
# 1. offline sanity check. No network, no credentials, no org.
python agol_relink.py --self-test

# 2. see what WOULD change. Writes nothing.
python agol_relink.py \
  --user your_username \
  --old-url "https://gis.example.org/public/rest/services/General/Roads/MapServer" \
  --new-url "https://services1.arcgis.com/AbC123/arcgis/rest/services/Roads/FeatureServer" \
  --types "Web Map,Web Experience,Web Mapping Application,Dashboard,StoryMap"

# 3. read changes.csv. Then re-run with --apply to commit.
#    You will be asked to type APPLY.
```

Credentials come from `--user` / `--password`, the `AGOL_URL` / `AGOL_USER` /
`AGOL_PASS` environment variables, or an interactive prompt. Put the password in
the environment or the prompt, never in the file.

### Configuration

Everything tunable is in one `CONFIGURATION` block at the top of the script —
URLs, org, item types, matching strictness, retry behaviour. Fill it in and run
the file with no arguments, or leave it alone and use CLI flags. Flags override
the block; environment variables sit in between.

### Output

Every run writes a timestamped directory:

| File | Contents |
|---|---|
| `changes.csv` | one row per URL: item, type, owner, JSON path, before, after |
| `run.log` | full log, including arcgis library chatter kept off your screen |
| `backups/<itemid>.json` | the item's original data JSON, written *before* its first mutation |
| `backups/resources/<itemid>/` | original resource JSON |
| `updated_ids.txt` | ids confirmed written — feed to `--resume` after a crash |

Rolling back one item — restore **both** halves, or a StoryMap/ExB item ends up
with the old URL published and the new one still in its draft:

```python
import json, pathlib
from arcgis.gis import GIS

item = GIS(...).content.get("<itemid>")

# 1. item data
item.update(data=json.load(open("backups/<itemid>.json")))

# 2. resources, if any. '/' in the resource name was flattened to '__' on disk.
for p in pathlib.Path("backups/resources/<itemid>").glob("*"):
    name = p.name.replace("__", "/")
    folder, _, leaf = name.rpartition("/")
    item.resources.update(folder_name=folder or None, file_name=leaf,
                          text=p.read_text(encoding="utf-8"))
```

`--include-item-url` rewrites have **no backup file** — the registered URL is a
single string, and its previous value is recorded in `changes.csv` under
`url_before`. Restore with
`item.update(item_properties={"url": "<url_before>"})`.

## How it finds URLs

It walks the entire item-data JSON tree and rewrites any string prefix-matched
by the old URL, anchored at a URL boundary — the next character must be
end-of-string, `/`, `?` or `#`.

That boundary rule is the whole safety story. `…/Roads/MapServer` matches
`…/Roads/MapServer`, `…/Roads/MapServer/0`, `…/Roads/MapServer/2/images/abc`
and `…/Roads/MapServer?f=json`, but never `…/Roads_Old/MapServer`.

Walking the tree rather than checking a list of known JSON paths is deliberate.
Real content nests URLs under machine-generated keys:

```
dataSources.dataSource_10.childDataSourceJsons.19465fe6e78-layer-61.url
```

No fixed path list survives contact with that, or with the next key Esri adds.

### Places URLs actually live

Measured across one real org's content — every one of these is handled:

```
operationalLayers[].url
operationalLayers[].layers[].url                      group layers
operationalLayers[].layers[].layers[].url             nested two deep
operationalLayers[]…renderer.symbol.url               symbol images under a service
tables[].url
baseMap.baseMapLayers[].url
baseMap.baseMapLayers[].styleUrl                      vector tile styles
presentation.slides[].baseMap.baseMapLayers[].url
utilities.utility_N.url                               Web AppBuilder
widgets.widget_N.config.addressSettings.geocodeServiceUrl
widgetPool.widgets[].config.searchSourceSettings.sources[].url
values.searchConfiguration.sources[].layer.url
dataSources.dataSource_N.childDataSourceJsons.<id>.url    Experience Builder
[resource:config/config.json]…                        ExB / StoryMap DRAFTS
```

That last one matters more than it looks. **StoryMaps and Experience Builder
keep the working draft in item *resources*, while `/data` holds only the
published copy.** Patch `/data` alone and the old URL comes straight back the
next time an author hits Publish — weeks later, and it will not look like your
fault. This patches both. Disable with `--no-resources` if you have a reason.

## Two things it cannot do

**Binary resources.** URLs inside `.zip`, `.xlsx` and image resources are not
rewritten. Every affected item is listed at the end of the run — check those by
hand. Survey123 `.xlsx` is the one that bites: a URL in a `pulldata()` call or
an external `choices` sheet is invisible here, and to Esri's own tooling.

**Private items owned by other users.** AGOL search only returns what the
authenticated identity can discover, and being an org admin does not change
that. If the enumerated count comes in under the server's reported total, the
run warns you.

## Notes

- **Default scope is Web Map only.** Pass `--types` to widen it — most orgs also
  need `Web Experience`, `Web Mapping Application`, `Dashboard` and `StoryMap`.
- You need item-update privilege on whatever you are changing. Modifying other
  people's items needs an administrative role; without it those writes are
  refused and reported as `failed`, not silently skipped.
- **`--old-url` must not be a prefix of `--new-url`.** The run refuses it: every
  rewritten URL would still match the old one, so a second run would apply the
  change twice (`…/arcgis/arcgis/…`). Narrow the old URL instead.
- Ports are part of the match. `https://host:6443/…` and `https://host/…` are
  different URLs and neither matches the other.
- URLs embedded mid-string — an `href` inside popup HTML, say — are not
  rewritten. Only values that *start* with the old URL are.
- `--password` on the command line lands in your shell history. Prefer
  `AGOL_PASS` or the interactive prompt.
- `--include-item-url` also rewrites the top-level registered `url` of
  Feature/Map/Image Service items. That is a separate API call from the
  item-data rewrite, and you need it when the service items themselves point at
  the old host.
- If `--old-url` does not end at a recognised service endpoint, the run warns
  first. A boundary-anchored prefix means a bare host legitimately matches
  *everything* under it, including non-service paths like `/portal`. Correct for
  a whole-host migration, wrong for a single-service swap — only you know which
  you meant.
- Matching ignores case and treats `http`/`https` as equivalent by default. The
  worst realistic failure is typing the old URL slightly differently from how
  content stores it, matching nothing, and concluding there was nothing to fix.
  `--case-sensitive` and `--strict-scheme` turn that off.
- `item.resources.get()` caches per GIS connection. A resource re-read in the
  same process returns the pre-update copy even after a successful write —
  verify with a fresh connection, not a re-read.
- Retries with exponential backoff on 429/502/503/504. `--resume <previous-out-dir>`
  skips items already confirmed written.

## Testing

`--self-test` runs ~50 assertions offline: URL shape handling, the boundary
rule, case and scheme options, recursive walk over a web map with two levels of
group nesting, idempotence, injection-safety of the replacement string, and the
input validation that refuses an empty old URL.

The write path was additionally verified against a disposable item on a live
org — server-side persistence checked path by path, plus backup, rollback and
idempotence — and dry-run over 2000+ real items.

## License

MIT
