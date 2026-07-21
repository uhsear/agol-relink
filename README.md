# agol-relink

Bulk-replace REST service URLs across ArcGIS Online and Portal content.

You moved a service to a new server or hostname, and now every web map, app,
dashboard, and Experience Builder site pointing at the old URL is broken. This
finds them and rewrites the URLs.

**Dry-run by default. Nothing is written unless you pass `--apply`.**

One file, no dependencies beyond the `arcgis` package.

## Install

Needs Python 3.9+ and the [ArcGIS API for Python](https://developers.arcgis.com/python/).
ArcGIS Pro already ships it:

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" agol_relink.py --self-test
```

Otherwise `conda install -c esri arcgis` or `pip install arcgis`. Then download
`agol_relink.py`. There is nothing to build.

## Use

```bash
# offline check. No network, no credentials.
python agol_relink.py --self-test

# see what would change. Writes nothing.
python agol_relink.py \
  --user your_username \
  --old-url "https://gis.example.org/public/rest/services/General/Roads/MapServer" \
  --new-url "https://services1.arcgis.com/AbC123/arcgis/rest/services/Roads/FeatureServer" \
  --types "Web Map,Web Experience,Web Mapping Application,Dashboard,StoryMap"

# read changes.csv, then re-run with --apply. You will be asked to type APPLY.
```

Credentials come from `--user` and `--password`, from `AGOL_URL` / `AGOL_USER` /
`AGOL_PASS`, or from a prompt. Keep the password out of the file and off the
command line.

A `CONFIGURATION` block at the top of the script holds every setting: URLs, org,
item types, matching strictness, retries. Fill it in and run with no arguments,
or ignore it and use flags. Flags beat environment variables, which beat the
block.

## Output

Every run writes a timestamped directory.

| File | Contents |
|---|---|
| `changes.csv` | one row per URL: item, type, owner, JSON path, before, after |
| `run.log` | full log, including library chatter kept off your screen |
| `backups/<itemid>.json` | the item's original data JSON, saved before any write |
| `backups/resources/<itemid>/` | original resource JSON |
| `updated_ids.txt` | ids confirmed written. Feed to `--resume` after a crash |

To roll an item back, restore both halves. Restoring only the data leaves a
StoryMap or Experience Builder item with the old URL published and the new one
still in its draft.

```python
import json, pathlib
from arcgis.gis import GIS

item = GIS(...).content.get("<itemid>")
item.update(data=json.load(open("backups/<itemid>.json")))

# resources, if any. A '/' in the resource name became '__' on disk.
for p in pathlib.Path("backups/resources/<itemid>").glob("*"):
    folder, _, leaf = p.name.replace("__", "/").rpartition("/")
    item.resources.update(folder_name=folder or None, file_name=leaf,
                          text=p.read_text(encoding="utf-8"))
```

`--include-item-url` rewrites have no backup file. `changes.csv` records the
previous value in `url_before`.

## How it finds URLs

It walks the whole item-data JSON tree and rewrites any string prefix-matched by
the old URL, anchored at a URL boundary. The next character has to be
end-of-string, `/`, `?`, or `#`.

That boundary rule is what keeps it safe. `…/Roads/MapServer` matches
`…/Roads/MapServer/0` and `…/Roads/MapServer/2/images/abc`. It never matches
`…/Roads_Old/MapServer`.

Walking the tree beats checking a list of known JSON paths, because real content
nests URLs under machine-generated keys that no fixed list can anticipate:

```
dataSources.dataSource_10.childDataSourceJsons.19465fe6e78-layer-61.url
```

Measured across one real org, URLs turned up in 13 distinct places, including
group layers nested two deep, `baseMap.baseMapLayers[].styleUrl`,
`presentation.slides[]` basemaps, Web AppBuilder `utilities.utility_N.url`,
`widgets.widget_N.config.addressSettings.geocodeServiceUrl`, and Experience
Builder `childDataSourceJsons`. All are handled.

One deserves attention. StoryMaps and Experience Builder keep the working draft
in item *resources*, while `/data` holds only the published copy. Patch `/data`
alone and the old URL returns the next time an author hits Publish, weeks after
you thought the job was done. This patches both. Turn it off with
`--no-resources` if you have a reason.

## What it cannot do

**Binary resources.** URLs inside `.zip`, `.xlsx`, and image resources stay as
they are. The run lists every affected item at the end. Survey123 `.xlsx` is the
one that bites: a URL in a `pulldata()` call or an external `choices` sheet is
invisible here, and to Esri's own tooling.

**Private items owned by other users.** AGOL search returns only what the
authenticated identity can discover, and an admin role does not change that. If
the enumerated count falls short of the server's reported total, the run warns.

## Notes

- Default scope is Web Map only. Pass `--types` to widen it.
- You need item-update privilege on whatever you change. Editing other people's
  items needs an administrative role. Without it those writes get refused and
  reported as `failed`, never silently skipped.
- `--old-url` must not be a prefix of `--new-url`. The run refuses it, because
  every rewritten URL would still match the old one and a second run would apply
  the change twice (`…/arcgis/arcgis/…`).
- If `--old-url` stops short of a service endpoint, the run warns first. A
  boundary-anchored prefix means a bare host matches everything under it,
  including paths like `/portal`. Right for a whole-host migration, wrong for a
  single-service swap.
- Matching ignores case and treats `http` and `https` as equivalent. The usual
  failure is typing the old URL slightly differently from how the content stores
  it, matching nothing, and concluding there was nothing to fix.
  `--case-sensitive` and `--strict-scheme` turn that off.
- Ports count, and URLs embedded mid-string (an `href` inside popup HTML) stay
  as they are. Only values that start with the old URL get rewritten.
- `--include-item-url` also rewrites the registered `url` of Feature, Map, and
  Image Service items, a separate API call from the item-data rewrite.
- Retries with backoff on 429, 502, 503, and 504.
  `--resume <previous-out-dir>` skips items already confirmed written.

## Testing

`--self-test` runs 50 assertions offline covering URL shapes, the boundary rule,
case and scheme options, a recursive walk over nested group layers, idempotence,
and input validation.

The write path was checked separately against a disposable item on a live org,
verifying server-side persistence, backup, and rollback, then dry-run over 2000+
real items.

## Issues

Open an issue on GitHub.

## License

MIT. Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).
