# agol-relink

Bulk-replace REST service URLs across ArcGIS Online and Portal content.

**Dry-run by default. Nothing is written to your organization unless you pass `--apply`.**

One Python file. The `arcgis` package is the only dependency, and `--self-test` does not need it.

## The Monday after the move

You move a map service to a new server and switch the old one off on Friday. On Monday every web
map that used it opens with an empty layer. The map shows no error, because the layer is only a
URL that no longer answers. The fix is a find-and-replace, but the URL is not where you expect.
It sits in `operationalLayers`, and also in a group layer inside a group layer, in a basemap
`styleUrl`, in a presentation slide, in a symbol image path, and under machine-made keys like
`dataSources.dataSource_10.childDataSourceJsons.19465fe6e78-layer-61.url`.

You patch the maps you can see. The StoryMap and Experience Builder items look fixed, because their
published copy is fixed. Their working draft is a separate file in the item's resources, and it
still holds the old URL. The next time an author opens the draft, the old URL is in it again.

This tool walks the whole JSON tree of each item and its JSON resources. It rewrites every string that
starts with the old service URL, at a URL boundary. It writes a report first, and it writes to the
organization only when you pass `--apply`.

## What already exists

Esri ships two ways to do this. Use them when they fit.

- **Update ArcGIS Server site references.** Open the web map's item page, then Settings, then Layer
  Settings. Esri's page says all ArcGIS Server layers that reference the site are updated, "as well as
  associated layer items you own or administer". It checks the new site first and skips layers it
  cannot find there. It covers web maps only, and you must be the owner or an administrator
  ([Update the URL of ArcGIS Server web layers in web maps](https://doc.esri.com/en/arcgis-online/latest/manage-data/update-web-service-urls-in-maps.html)).
  It is the right choice for one web map on a moved site.
- **ArcGIS Assistant** ([Esri/ago-assistant](https://github.com/Esri/ago-assistant)). Its feature list
  includes "Update the URLs of services in a web map". Its README says the application "has not been updated in many years".

This tool adds a dry-run report for a whole organization, apps and dashboards as well as maps, the
draft resources, and a backup of every item before it changes.

On 2026-11-11, `gh search repos` for `agol-relink` returned this repository. The searches
`arcgis relink`, `webmap url replace arcgis` and `arcgis item url find replace` returned nothing. A
search that finds nothing shows only that the search found nothing. This is not a claim that no other tool exists.

## Install

Needs Python 3.9 or later and the [ArcGIS API for Python](https://developers.arcgis.com/python/).
ArcGIS Pro already ships it:

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" agol_relink.py --self-test
```

Otherwise run `conda install -c esri arcgis` or `pip install arcgis`. Then download `agol_relink.py`.
There is nothing to build.

**A Python that cannot start ArcPy cannot import `arcgis`.** The package loads a geometry engine
when it is imported, and that step tries to import ArcPy
(`arcgis/_impl/_geometry_engine.py`, `_detect_available_engines`, in arcgis 2.4.2). In a shell where
the ArcGIS Pro licence is not initialised, `import arcgis` stops with
`RuntimeError: The Product License has not been initialized.` The same failure happened with
`ARCGIS_IO_ENGINE=requests` set. That variable is read only in `arcgis/features/geo/_accessor.py`,
and the import fails earlier. This was reproduced from a plain shell with a Pro-derived conda
environment. It was not run as a scheduled task, which is the case where you are most likely to see it.
For a scheduled run, use a Python that has `arcgis` installed but not `arcpy`.
The tool catches the failure and prints this advice instead of a traceback.

## Use

```bash
# offline check. No network, no credentials, no arcgis package.
python agol_relink.py --self-test

# see what would change. Writes nothing to the organization.
python agol_relink.py \
  --user your_username \
  --old-url "https://gis.example.org/public/rest/services/General/Roads/MapServer" \
  --new-url "https://services1.arcgis.com/AbC123/arcgis/rest/services/Roads/FeatureServer" \
  --types "Web Map,Web Experience,Web Mapping Application,Dashboard,StoryMap"

# read changes.csv, then re-run with --apply. You will be asked to type APPLY.
```

Credentials come from `--user` and `--password`, from `AGOL_URL` / `AGOL_USER` / `AGOL_PASS`, or from
a prompt. Keep the password off the command line: a command line shows in the process list.
A wrong password prints one message that names the organization and the user. It never prints the password.

A `CONFIGURATION` block at the top of the script holds every setting: URLs, organization, item types,
matching strictness, retries. Fill it in and run with no arguments, or ignore it and use flags.
Flags beat environment variables, which beat the block. `--dry-run` beats `APPLY = True` in the block.

| Flag | Effect |
|---|---|
| `--apply` | write the changes. Typing `APPLY` at the prompt confirms it. `--yes` skips the prompt. |
| `--dry-run` | force a dry run, whatever the block says |
| `--types` | comma-separated item types. The default is `Web Map`. |
| `--include-item-url` | also rewrite the registered `url` of service items |
| `--no-resources` | skip item resources. Not recommended: see the draft problem above. |
| `--case-sensitive`, `--strict-scheme` | turn off the forgiving match |
| `--query` | add a search clause such as `owner:"jsmith"` |
| `--max-items` | stop after N items. This is a sample run, not an incomplete scan. |
| `--resume DIR` | skip the items that run `DIR` wrote |
| `--out-dir`, `-v` | choose the output directory, show debug lines |

An unambiguous prefix of a flag is refused: `--ap` exits 2 and never reaches the write path.
The parser sets `allow_abbrev=False`.

## Output

Every run writes a timestamped directory.

| File | Contents |
|---|---|
| `changes.csv` | one row per URL: item, title, type, owner, status, JSON path, before, after, error |
| `run.log` | full log, including library chatter kept off your screen |
| `backups/<itemid>.json` | the item's original data JSON, saved before any write |
| `backups/resources/<itemid>/` | original resource JSON |
| `updated_ids.txt` | ids confirmed written. Feed the directory to `--resume` after a crash |

A dry run writes `changes.csv`, `run.log` and an empty `backups` directory, all on your disk. The self-test
shows that a dry run sends no write request to the organization.

### Exit status

| Code | Meaning |
|---|---|
| 0 | the search returned every item the server reported, and no item failed |
| 1 | refused or aborted: bad URLs, no `APPLY` typed, sign-in failed, `arcgis` missing or broken |
| 2 | at least one item failed. This code also means a command-line usage error. |
| 3 | the scan was incomplete: the server reported more items than it returned |

A clean result with exit 3 does not cover the items the search did not return. Before this version, that
case printed a warning and exited 0.

### Spreadsheet safety

Titles, owners and item data come from the organization. A title such as `=HYPERLINK(...)` runs as a formula when
someone opens `changes.csv` in Excel, LibreOffice or Google Sheets.
Every text cell that starts with `=`, `+`, `-`, `@`, a tab, a carriage return or a line feed
gets one leading apostrophe. This follows the mitigation in
[OWASP CSV Injection](https://community.owasp.org/attacks/CSV_Injection).
Numbers stay numbers. An ordinary cell, such as a URL, is not changed.

This tool never reads `changes.csv` back, so it has no strip on read. If your own script reads the file,
remove one leading apostrophe when the next character is one of those seven. A title that began with an apostrophe
followed by `=` cannot be told from a guarded one. The OWASP page also lists full-width variants of `=`, `+`, `-` and `@`. This tool does not guard those.

### Tokens in URLs

A web map can hold a signed URL, and a library error message repeats the request URL.
The value of a `token=` or `access_token=` parameter is replaced with `[redacted]` in `changes.csv`,
in `run.log` and on the console. The per-item backups keep the original data as it was, because a
rollback needs it. Treat `backups/` as sensitive.

### Rolling back

Restore both halves of an item. Restoring only the data leaves a StoryMap or Experience Builder
item with the old URL published and the new one still in its draft.

```python
import json, pathlib

item = gis.content.get("<itemid>")      # gis = GIS(...), signed in
item.update(data=json.load(open("backups/<itemid>.json")))

# resources, if any. A '/' in the resource name became '__' on disk.
for p in pathlib.Path("backups/resources/<itemid>").glob("*"):
    folder, _, leaf = p.name.replace("__", "/").rpartition("/")
    item.resources.update(folder_name=folder or None, file_name=leaf,
                          text=p.read_text(encoding="utf-8"))
```

The self-test replays the `item.update` and `item.resources.update` lines against the stub and checks
that the data, a root resource and a folder resource come back as they were.
`gis.content.get` is not replayed.

`--include-item-url` rewrites have no backup file. `changes.csv` records the previous value in `url_before`.

## How it finds URLs

It walks the whole item-data JSON tree and rewrites any string value that starts with the old URL, anchored
at a URL boundary. The next character has to be end-of-string, `/`, `?`, or `#`.

That boundary rule is what keeps it safe. `.../Roads/MapServer` matches `.../Roads/MapServer/0`
and `.../Roads/MapServer/2/images/abc`. It never matches `.../Roads_Old/MapServer`.

A fixed list of JSON paths would miss the machine-made keys. The self-test fixture covers a group layer
nested two deep, `baseMap.baseMapLayers[].styleUrl`, a presentation slide basemap, a `tables` entry,
and an unknown future key. An early scan of one real organization of a few hundred web maps found URLs at 13 distinct
paths, which is why the walk does not use a list. That scan was not repeated for this version.

The draft problem is where the walk matters most. StoryMaps keep the working draft in a resource named
`draft_<13 digits>.json` and the published copy in `published_data.json`. Experience Builder keeps its
configuration in `config/config.json`. Both facts come from the source of the `arcgis` package
(`apps/storymap/_utils.py` and `apps/expbuilder/expbuilder.py`, version 2.4.2).
Patch `/data` alone and the draft still holds the old URL. This tool patches both, and takes a
backup of each resource first. Turn it off with `--no-resources` if you have a reason.

## What it relies on in Esri's products

Each line below was checked against Esri's documentation or the `arcgis` 2.4.2 source.

| The tool assumes | Source |
|---|---|
| `Item.update()` returns `True` or `False` | `Item.update` docstring: "A boolean indicating success (True) or failure (False)". The tool reads `False` as a refusal. |
| `ResourceManager.update()` returns the server dict, and a failure is `{"error": {...}}`, which is truthy | `ResourceManager.update` docstring, same file. The tool treats a dict with `error` as a failure. |
| `Item.get_data()` turns every exception into `{}` | source of `Item.get_data` in `arcgis/gis/__init__.py`: `except Exception: item_data = {}`. The tool re-fetches the raw payload when the answer is empty. |
| `ResourceManager.list()` returns dicts with a `resource` key | `ResourceManager.list` docstring |
| A search returns only items the signed-in account can access | [Search](https://developers.arcgis.com/rest/users-groups-and-items/search/): "The results of a search only contain items that the user has permission to access." |
| The top 10,000 results can be paged | same page: "The top 10,000 query results are available to retrieve through pagination." |
| `created` is a valid sort field | same page: "Supported sort field names are `title`, `created`, `type`, `owner`, `modified`" |
| `nextStart` is the next entry index | same page |
| A page returns at most 100 items | same page: "the maximum allowed value is `100`" for `num`. `PAGE_SIZE` is 100. |
| Resources can be read by an organization administrator | `ResourceManager.get` docstring: "Only available to the user, item owner, or an organization administrator." |

An administrator with the privilege to view all items has a separate operation,
[Portal Content](https://developers.arcgis.com/rest/users-groups-and-items/portal-content/), that lists
every item in the organization. This tool uses Search, not that operation.
So the tool does not claim to see the private items of other users.
If the search returns fewer items than it reports, the run says so and exits 3.

## What it cannot do

**Binary resources.** URLs inside `.zip`, `.xlsx`, and image resources stay as they are. The run lists
every changed item that carries one. A Survey123 `.xlsx` can hold a URL in a `pulldata()` call or an
external `choices` sheet. This tool cannot see it.

**URLs inside a longer string.** Only values that start with the old URL are rewritten. An `href`
inside pop-up HTML stays. Ports count. A dictionary key that holds the old URL is not rewritten.

**Items the search does not return.** See exit status 3 above.

## Notes

- Default scope is Web Map only. Pass `--types` to widen it.
- You need item-update privilege on whatever you change. Without it the write is refused and the item is reported
  as `failed`. It is never skipped silently.
- `--old-url` must not be a prefix of `--new-url`. The run refuses it, because every rewritten URL
  would still match the old one and a second run would apply the change twice (`.../arcgis/arcgis/...`).
- If `--old-url` stops short of a service endpoint, the run warns first. A bare host matches everything
  under it, including paths like `/portal`. That is right for a whole-host migration and wrong for a
  single-service swap.
- Matching ignores case and treats `http` and `https` as equivalent. The usual failure is typing the old URL
  slightly differently from how the content stores it, matching nothing, and concluding there was nothing to fix.
  `--case-sensitive` and `--strict-scheme` turn that off.
- The tool retries a call on `429`, `502`, `503` and `504`, and on timeouts and connection errors, with
  exponential backoff. It reads the status as a whole token. A `503` inside an item id is not a throttle.

## Testing

`--self-test` runs 236 assertions. It starts a stub organization behind a real HTTP server on
`127.0.0.1` and runs the real `main()` against it, in-process, with a fake `arcgis.gis.GIS`. The stub
signs in with a username and password, hands out a token, and refuses a request that lacks it.
It answers search, item data, item update, resource list, resource read and resource update.

```
$ python agol_relink.py --self-test
agol_relink self-test: no arcgis package, no organization, local stub server only
--------------------------------------------------------------------
PASS  service root rewrites
PASS  a url used as a dict key is not rewritten
...
PASS  changes.csv title guards a leading '=' and nothing else  <-- pinned defect
PASS  a dry run sends no write request to the org  <-- pinned defect
PASS  a token in a matched url is blanked in changes.csv  <-- pinned defect
PASS  a server that reports 7 but returns 5 exits 3, not 0  <-- pinned defect
PASS  a wrong password is one actionable message, not a traceback  <-- pinned defect
PASS  the password appears in no output file and not on the console  <-- pinned defect
PASS  the session token appears in no output file and not on the console  <-- pinned defect
...
--------------------------------------------------------------------
236 assertions, 0 failed
```

The self-test covers:

- the matcher, the boundary rule, case and scheme options, the walk, and idempotence
- input validation and the refusal of `--ap`
- the formula guard for each of the nine columns, for `=`, tab and carriage return, plus `+`, `-`, `@` and line feed on the cell
- dry run and `--apply`, backups, `updated_ids.txt`, `--resume`, and the documented rollback
- every failure path: a refused update, a resource error dict, a raw payload that is not JSON,
  a failing resource listing, and a retried `503` and `429`
- the incomplete-scan exit, `--max-items`, and `--include-item-url`
- prompts, environment variables, the `CONFIGURATION` block, a wrong password, a missing `arcgis` package,
  and an `arcgis` import that fails on the ArcGIS Pro licence
- a search of every output file and of the console for the password and the session token

Results on this version:

| Host | Python | Assertions |
|---|---|---|
| Windows | 3.13.2 (`C:\Python313`) | 236 |
| Windows | 3.13.7 (ArcGIS Pro conda clone) | 236 |
| Windows | 3.9.25 | 236 |
| Linux | 3.12.3 | 236 |

The counts are equal because the self-test does not use `arcpy` or `arcgis`.

Branch coverage was measured with `coverage run --branch agol_relink.py --self-test` on Windows: 100% of
1361 statements and 296 branches. One line carries `# pragma: no branch`: the `if __name__ == "__main__"`
guard, whose false side only happens on import. The self-test code is part of the measured file.

### What the self-test does not cover

- **The `arcgis` package.** The stub implements the methods this tool calls, with the shapes the
  docstrings above describe. It does not prove that a live organization answers the same way.
  The write path was checked by hand against a disposable item on a live organization before this version.
  That check was not repeated for this version.
- **TLS.** The stub is plain HTTP on the loopback interface.
- **How a spreadsheet opens the file.** The tests check the bytes in `changes.csv`. They do not open it in Excel.
- **A scheduled task.** See the install note.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [cimscan](https://github.com/uhsear/cimscan) - the same hunt for a dead service URL, but across .aprx and .lyrx files on disk
- [sightline](https://github.com/uhsear/sightline) - what a viewer can actually see after the relink
