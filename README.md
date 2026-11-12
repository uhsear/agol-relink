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
starts with the old service URL, at a URL boundary. It also looks for the old URL inside longer text
(pop-up HTML, story text, an encoded `url=` parameter) and reports each such item, because it does not
rewrite those. It writes a report first, and it writes to the organization only when you pass `--apply`.

## What already exists

Esri ships two ways to do this. Use them when they fit.

- **Update ArcGIS Server site references.** Open the web map's item page, then Settings, then Layer
  Settings. Esri's page says all ArcGIS Server layers that reference the site are updated, "as well as
  associated layer items you own or administer". The page adds a note: the layers at the replacement
  site must exactly match the existing layers. The page is about web maps, and you must be the owner
  or an administrator
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
(`arcgis/_impl/_geometry_engine.py`, `_detect_available_engines`, by way of its helper `_is_installed`, in arcgis 2.4.2). In a shell where
the ArcGIS Pro licence is not initialised, `import arcgis` stops with
`RuntimeError: The Product License has not been initialized.` The same failure happened with
`ARCGIS_IO_ENGINE=requests` set. That variable is read in `_select_io_engine`, in the same
`arcgis/_impl/_geometry_engine.py`. It runs after `_detect_available_engines`, so ArcPy is
imported before the variable can matter. This was reproduced from a plain shell with a Pro-derived conda
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

The user name comes from `--user`, from `AGOL_USER`, or from a prompt. The password comes from
`AGOL_PASS` or from a prompt, and nowhere else: there is no `--password` flag and no password setting in the
script. A password on the command line shows in the process list, in shell history and in a scheduled task's
definition. `--password hunter2` exits 2 with `unrecognized arguments: --password [hidden]`.
A wrong password prints one message that names the organization and the user. It never prints the password.

A `CONFIGURATION` block at the top of the script holds every setting: URLs, organization, item types,
matching strictness, retries. Fill it in and run with no arguments, or ignore it and use flags.
Flags beat environment variables, which beat the block. `--dry-run` beats `APPLY = True` in the block.

| Flag | Effect |
|---|---|
| `--apply` | write the changes. Typing `APPLY` at the prompt confirms it. `--yes` skips the prompt. |
| `--dry-run` | force a dry run, whatever the block says |
| `--types` | comma-separated item types. The default is `Web Map`. A service type such as `Feature Service` needs `--include-item-url`. Without it the run exits 1, because it would examine no item of that type. |
| `--include-item-url` | also rewrite the registered `url` of service items |
| `--no-resources` | skip item resources. Not recommended: see the draft problem above. The run exits 3, because no resource was read. |
| `--case-sensitive`, `--strict-scheme` | turn off the forgiving match |
| `--query` | add a search clause such as `owner:"jsmith"` |
| `--max-items` | stop after N items. A run that leaves items unexamined exits 3 and logs `INCOMPLETE ... (--max-items stopped the scan)`. A run that reaches the last item exits as usual. |
| `--resume DIR` | skip the items that run `DIR` wrote |
| `--out-dir`, `-v` | choose the output directory, show debug lines |

An unambiguous prefix of a flag is refused: `--ap` exits 2 and never reaches the write path.
The parser sets `allow_abbrev=False`.

## Output

A run without `--out-dir` writes a timestamped directory. With `--out-dir`, it writes into that directory.

| File | Contents |
|---|---|
| `changes.csv` | one row per URL: item, title, type, owner, status, JSON path, before, after, error. A skipped item, or a failed item that found no URL, gets one row with the three URL columns empty. A failed item that found a URL before it failed keeps one row per URL, status `failed`, with before and after filled in. The after value is what the rewrite would have been: do not read it as written. |
| `run.log` | full log, including library chatter kept off your screen, and one `unread` line for every item with data or resources the tool did not read |
| `backups/<itemid>.json` | the item's original data JSON, saved before any write, with each credential value blanked (see "Tokens in URLs") |
| `backups/resources/<itemid>/` | original resource JSON, with each credential value blanked |
| `updated_ids.txt` | ids confirmed written. Feed the directory to `--resume` after a crash |

A dry run writes `changes.csv`, `run.log` and an empty `backups` directory, all on your disk. The self-test
shows that a dry run sends no write request to the organization.

### Exit status

| Code | Meaning |
|---|---|
| 0 | the search returned at least one item and every item the server reported, no item failed, and every item's data and resources were read |
| 1 | refused or aborted: bad URLs, no `APPLY` typed, sign-in failed, `arcgis` missing or broken, or an unexpected error ended the run. After an unexpected error, items may already be written: read `updated_ids.txt`. |
| 2 | at least one item failed. This code also means a command-line usage error. |
| 3 | the scan was incomplete: the server reported more items than it returned, `--max-items` stopped the scan before the last item, the search returned no item at all, an item carries data or a resource this tool did not read, an item holds the old URL inside longer text, or `--no-resources` was set |

A clean result with exit 3 does not cover the items the search did not return, nor the data or resources
the tool did not read. The console lists the first 15 such items at the end, with up to five resource names
each. `run.log` has an `unread` line for every one of them, with all the names. Before this version, a short
search printed a warning and exited 0, and an unchanged item with a binary resource was not mentioned.
An organization with `.xlsx` or `.zip` resources on its StoryMaps will see exit 3 on every run.
That is the intended signal: the tool cannot read those files.

An item that holds the old URL inside longer text is listed the same way, with the JSON path of the first
such string: `data: old URL inside longer text at .popupInfo.description`. The status of the item stays
`clean` or `matched`, because the tool rewrote nothing there. The run exits 3, so a wrapper does not read
the item as done. A dry run that finds URLs to rewrite exits 0, because only a failure or an unfinished
scan changes the exit code. Read `changes.csv` to tell "URLs found" from "nothing found".

### Spreadsheet safety

Titles, owners and item data come from the organization. A title such as `=HYPERLINK(...)` runs as a formula when
someone opens `changes.csv` in Excel, LibreOffice or Google Sheets.
Every text cell that starts with `=`, `+`, `-`, `@`, a tab, a carriage return or a line feed
gets one leading apostrophe. This follows the mitigation in
[OWASP CSV Injection](https://community.owasp.org/attacks/CSV_Injection).
Numbers stay numbers. An ordinary cell, such as a URL, is not changed. No column of `changes.csv`
holds a number today, so that rule describes `cell()` and guards nothing in the file.

This tool never reads `changes.csv` back, so it has no strip on read. If your own script reads the file,
remove one leading apostrophe when the next character is one of those seven. A title that began with an apostrophe
followed by `=` cannot be told from a guarded one. The OWASP page also lists full-width variants of `=`, `+`, `-` and `@`. This tool does not guard those. It checks the first character only, so text that starts with a space and
then `=` is not guarded either.

### Tokens in URLs

A web map can hold a signed URL, and a library error message repeats the request URL.
The value of a credential parameter is replaced with `[redacted]` in `changes.csv`,
in `run.log`, on the console and in the backups. The tool blanks each string of the parsed JSON, not the
dumped text, so a token inside a string that holds more JSON leaves that JSON valid. The self-test searches
every file of a run, the backups included, and the console for the session token. It finds none.
A URL you type that fails the check, and an organization URL in a sign-in error, are blanked the same way.

The price is in the backups. A rollback from a backup writes `[redacted]` where a token stood.
The run says so with `backup of <itemid> has N credential value(s) blanked` and never prints the value.
The tool leaves the token in the item itself, because it changes only the start of each URL. To undo a
relink and keep the tokens, run the tool again with the two URLs swapped (`--old-url` the new one,
`--new-url` the old one). The self-test does this and checks that data and a resource come back with
the token as it was. Each backup is written with mode 0600 where the system honours it.
Python's [`os.chmod` documentation](https://docs.python.org/3/library/os.html#os.chmod) says Windows sets
only the read-only flag and ignores the other bits. On Windows the folder's access list decides who can
read a backup. A reused `--out-dir` keeps the mode of an old backup file, because the file is truncated and
not created again.

The parameter names are `token`, `access_token`, `oauth_token`, `refresh_token`, `id_token`, `api_key`,
`apikey`, `key`, `ticket`, `password`, `passwd`, `pwd`, `secret`, `client_secret` and `sig`, in any letter case.
The whole name must match, so `authToken=` and `monkey=` stay. The name may follow `?`, `&`, `;`, `#` or an
encoded `?` or `&`, and the equals sign may be `%3D`. The value of `user:password@host` in a URL is blanked too.
A secret in free text that is not part of a query string is not blanked. A secret under a JSON key name, such as
`"customParameters": {"token": "..."}`, is not blanked either, because the tool cannot tell a secret from other data
there. The backup, and `changes.csv` for a URL that starts with the old URL, keep such a value as the item holds it.
A traceback from an unexpected error goes to `run.log` with the same blanking. With `-v` it also reaches the
console, blanked.

### Rolling back

Restore both halves of an item. Restoring only the data leaves a StoryMap or Experience Builder
item with the old URL published and the new one still in its draft.
A backup holds `[redacted]` where a URL carried a token. See "Tokens in URLs" for the swapped-URL run that keeps it.

```python
import json, pathlib, shutil, tempfile

item = gis.content.get("<itemid>")      # gis = GIS(...), signed in
item.update(data=json.load(open("backups/<itemid>.json")))

# resources, if any. A '/' in the resource name became '__' on disk.
# Upload each one as a file, as the tool does. See "Why a resource is uploaded as a file".
for p in pathlib.Path("backups/resources/<itemid>").glob("*"):
    folder, _, leaf = p.name.replace("__", "/").rpartition("/")
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copyfile(p, pathlib.Path(tmp) / leaf)
        item.resources.update(file=str(pathlib.Path(tmp) / leaf),
                              folder_name=folder or None, file_name=leaf)
```

The self-test replays the `item.update` and `item.resources.update` lines against the stub and checks
that the data, a root resource and a folder resource come back as they were.
`gis.content.get` is not replayed.

### Why a resource is uploaded as a file

The tool writes a patched resource with `item.resources.update(file=...)`. It never sends the JSON as `text=`.
Esri's [Update Resources](https://developers.arcgis.com/rest/users-groups-and-items/update-resources/)
page says `file` is a "multipart https parameter containing the binary file to upload". It says `text` is
"text input to update an existing text resource, used together with fileName". The page does not say
what ArcGIS Online does to a `text=` body. The author saw it strip every `style=` attribute from
rich-text widgets and blank an image source in two Experience Builder apps, in October 2026.
Uploading the same JSON as a file repaired both. The stub cannot reproduce that sanitizing, and this was
not repeated for this version. The self-test checks only that each write is a multipart upload with no `text` field.

`--include-item-url` rewrites have no backup file. `changes.csv` records the previous value in `url_before`,
with a `token=` value blanked. If the item's own `url` held a token, that token cannot be recovered from the output.

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

After the walk, a second read-only pass looks for the old URL anywhere else in each string and in each
dictionary key. It also looks for the percent-encoded form, in either hex case. A hit counts only at a name
boundary: the next character must not continue a host or path name, so `.../MapServer2` and
`.../MapServer.evil.org` are not hits. The pass changes nothing. It makes the item an unread item, and the run exits 3.

## What it relies on in Esri's products

Each line below was checked against Esri's documentation or the `arcgis` 2.4.2 source.

| The tool assumes | Source |
|---|---|
| `Item.update()` returns `True` or `False` | `Item.update` docstring: "A boolean indicating success (True) or failure (False)". The tool reads `False` as a refusal. |
| `ResourceManager.update()` returns the server dict, and a failure is `{"error": {...}}`, which is truthy | `ResourceManager.update` docstring, same file. The tool treats a dict with `error` as a failure. |
| `Item.get_data()` turns every exception into `{}`, and saves a non-JSON answer to the temp folder | source of `Item.get_data` in `arcgis/gis/__init__.py`: `except Exception: item_data = {}`. It calls `get_item_data` in `arcgis/gis/_impl/_portalpy.py`, which calls `Connection.get(..., out_folder=None)`. The tool does not call `get_data`. It reads the raw answer itself. |
| `Connection.get(..., try_json=False, return_raw_response=True)` returns the `requests` Response and saves no file | `Connection.get` in `arcgis/gis/_impl/_con/_connection.py`: after the request it runs `if return_raw_response: return resp` before `_handle_response`. `_handle_response` is the step that, without `force_bytes`, streams an answer whose `Content-Type` has neither `json` nor `text` to a file in the temp folder, after deleting a file of the same name. With `force_bytes` it returns `resp.content` before that step, but it never checks the status, and the file has no `raise_for_status` call: a `503` body comes back as content. A dry run must write nothing outside the output directory, and a failed read must not read as an empty item. So every read of data, resource list and resource asks for the Response and checks `status_code` itself. The `get` docstring lists `return_raw_response` ("Returns the requests' Response object"). |
| `ResourceManager.update(file=..., folder_name=..., file_name=...)` replaces an existing resource from an uploaded file | `ResourceManager.update` docstring (`file` is "the path to the file on disk to be used for overwriting an existing file resource"), and the Esri Update Resources page quoted above |
| The tool does not call `ResourceManager.list()`. It reads the resource list itself, pages it, and refuses an error body | `ResourceManager.list` in arcgis 2.4.2 reads `resp.get('resources', [])` from the raw JSON, so a `200` answer that holds an error body comes back as an empty list, as if the item had no drafts. The paging parameters ([Common parameters](https://developers.arcgis.com/rest/users-groups-and-items/common-parameters/)) give `num` a default of 10 and a maximum of 100, and `nextStart` is "the next entry index if the current result set doesn't contain all results, or -1 if it is the last batch". The tool asks for `num=100` and follows `nextStart`. |
| A search returns only items the signed-in account can access | [Search](https://developers.arcgis.com/rest/users-groups-and-items/search/): "The results of a search only contain items that the user has permission to access." |
| The top 10,000 results can be paged | same page: "The top 10,000 query results are available to retrieve through pagination." |
| `created` is a valid sort field | same page: "Supported sort field names are `title`, `created`, `type`, `owner`, `modified`" and the list goes on with more names |
| `nextStart` is the next entry index | same page |
| A page returns at most 100 items | same page: "the maximum allowed value is `100`" for `num`. `PAGE_SIZE` is 100. |
| Resources can be read by an organization administrator | `ResourceManager.get` docstring: "Only available to the user, item owner, or an organization administrator." |

An administrator with the privilege to view all items has a separate operation,
[Portal Content](https://developers.arcgis.com/rest/users-groups-and-items/portal-content/), that lists
every item in the organization. This tool uses Search, not that operation.
So the tool does not claim to see the private items of other users.
If the search returns fewer items than it reports, the run says so and exits 3.

## What it cannot do

**Resources that are not JSON.** Only `.json` resources are read. URLs inside `.zip`, `.xlsx`, `.txt`,
`.html` and every other non-JSON resource stay as they are. The run lists every item that carries
one, changed or not, and exits 3. A Survey123 `.xlsx` can hold a URL in a `pulldata()` call or an
external `choices` sheet. This tool cannot see it. Plain images (`.png`, `.jpg`, `.jpeg`, `.gif`) are
neither read nor listed.

**URLs inside a longer string.** Only values that start with the old URL are rewritten. An `href`
inside pop-up HTML stays, and so does a dictionary key that holds the old URL. The tool finds these and
reports the item (exit 3), so you can fix them by hand, but it does not rewrite them. Ports count.

**Items the search does not return.** See exit status 3 above.

**Smaller limits.**

- A resource that cannot be read, or that is not JSON, fails its item. It is never skipped. The item is not clean until you read the resource by hand.
- If a second resource write fails, the first is already changed. The item is reported `failed` and `/data` is not written.
- A crash writes no `changes.csv`. Use `updated_ids.txt` and `--resume`.
- Use a fresh `--out-dir` for each `--apply` run. The tool writes `backups/<itemid>.json` without checking for an old file.
- A rollback splits resource names on `__`. A name that contains `__` restores into the wrong folder, and `a/b.json` and `a__b.json` share one backup path.
- Resource names come from the server and build the backup path. Under `--apply`, a name with a backslash, a colon, a leading slash or a `..` segment fails its item with `resources.backup`, and nothing is written for that resource. A dry run reports such a resource as usual.
- Item data that is not a JSON object or array makes the item `skipped`, and its JSON resources are not scanned. The run lists the item and exits 3.
- The walk stops at depth 200 with a log warning. The item still counts as clean or matched, and the run exits 0.
- A mistyped flag such as `--pass SECRET` prints `error: unrecognized arguments: --pass [hidden]`. The tool hides the value of an unrecognised argument. It keeps the option name, so you see what you mistyped.
- The apostrophe prefix stops a formula on first open. OWASP warns it is not reliable in Excel after the file is saved and re-opened.
- `changes.csv` is UTF-8 without a byte order mark, so Excel on Windows can show non-ASCII titles as garbled text. Open it with Data > From Text and choose UTF-8.
- The tool strips no control character from a title, owner or URL. `changes.csv` gets only the leading-character apostrophe guard, and a title with a newline can forge a line in `run.log` and on the console.
- On a Windows console that uses cp1252, a title with characters outside cp1252 makes the logger print a `--- Logging error ---` block and drop that one console line. The run still finishes. `run.log` and `changes.csv` are complete. Set `PYTHONUTF8=1` for non-Latin titles.
- A search that returns zero items logs `scanned 0 items` and exits 3, because nothing was checked. A mistyped `--types` such as `Storymap` gives this. A typo beside a valid name, as in `Web Map,Storymap`, still scans the valid name and exits 0 on a clean result. Check the `--types` spelling in the `query:` line.
- A `--resume` directory that does not exist, or has no `updated_ids.txt`, resumes nothing and says nothing. Re-processing an updated item finds no match, so no harm follows.
- A dry run that reuses an `--out-dir` overwrites the earlier `changes.csv`, which may be the record of an applied run.
- A lone surrogate character in item JSON makes the backup write fail and ends the run with exit 1. A lone surrogate in an item title, owner or URL ends the run with exit 1 as well, at the `changes.csv` write, so no report is written. After `--apply` the writes are already made: read `updated_ids.txt`.
- The raw item-data, resource-list and resource reads pass `return_raw_response=True` to `Connection.get`, which the `get` docstring lists, and check `status_code` themselves. In arcgis 2.4.2 the `force_bytes` path returns the body of an HTTP error without a status check, which is why the tool does not use it. This was read from the source of that version, not tried against a live organization.
- Five observations in this README come from hand checks and are not covered by the self-test: the hand check of the write path on a live organization, the 13 JSON paths in one organization, the October 2026 `text=` sanitizing, the cp1252 console logging error, and the `PYTHONUTF8=1` advice for it.
- A closed stdin at the `APPLY` prompt, or at the `Username` prompt, ends the run with exit 1. At the `APPLY` prompt nothing is written. At the `Username` prompt the run has not started.
- A data or resource answer that is a JSON object with an `error` key and other keys, such as `messages`, is read as the item's own content. Only an object whose single key is `error` is refused.
- A `MAX_ITEMS` left in the `CONFIGURATION` block cuts every run the same way as `--max-items`. The run logs `INCOMPLETE` and exits 3, so the cut is not silent.
- `--resume` trusts the `updated_ids.txt` of any directory, so a directory from a run with other URLs skips items that this pair never changed.
- A search that names `orgid:<id>` was exercised against the stub only. A Portal that ignores `orgid:` would return no items, and the run would exit 3 with `scanned 0 items`.
- An `http://` value for `--org` is accepted, and the sign-in then travels without encryption. Use `https://`.
- The apostrophe prefix is applied only when the first character is a formula lead. Text that starts with a space or a vertical tab and then `=` is not guarded.
- A UTF-8 byte order mark at the start of item data or of a `.json` resource makes the item fail with `is not JSON`. The failure is loud, not a clean result.
- Under `--apply` on Windows, a resource name that holds a character the file system forbids, such as `*`, ends the run with exit 1 at the backup write, because the backup file name is built from the resource name. `safe_backup_name` rejects only a backslash, a colon, a leading slash and a `..` segment. An item id is used in a backup file name unchecked.
- A resource name goes into the request path unquoted. A name with `#`, `?` or `%` reads another path, and the item fails with exit 2 instead of being read.
- A resource list whose `nextStart` is not a number, or does not advance, ends with the entries read so far and no warning. A search answer without a `total` skips the incomplete-scan check.
- A JSON tree that nests deeper than 200 levels is not scanned below that depth, and a token below that depth is not blanked in a backup. The item is reported clean with exit 0.
- `--yes=hunter2` makes `argparse` print `ignored explicit argument 'hunter2'`: only the `unrecognized arguments` error hides values. A stray argument that begins with `--` is printed as typed, so a secret that begins with `--` would show.
- The password prompt runs before the URL and `--types` checks, so a bad flag value is reported after you type the password.
- Exit 1 covers both a refusal (no `APPLY` typed, bad URLs) and an unexpected error that may follow writes. A wrapper script cannot tell them apart without `run.log`.
- A resource listing entry whose `resource` value is not a string ends the run with exit 1 and writes no `changes.csv`. An entry with no `resource` key is skipped and not listed as unread. Esri always sends a string.
- A move that changes only the scheme, such as `http://` to `https://` on the same path, is refused as nested under the old URL, because the match ignores the scheme by default. Nothing is written. Set `ANY_SCHEME` to `False` in the `CONFIGURATION` block to allow it. The `--strict-scheme` flag does not lift the refusal.
- The Esri quotes in "What it relies on in Esri's products" are exact except for the capital letters: the page writes "Multipart" and "Text input", and this README lowercases them.

## Notes

- Default scope is Web Map only. Pass `--types` to widen it.
- You need item-update privilege on whatever you change. Without it the write is refused and the item is reported
  as `failed`. It is never skipped silently. The same holds for a resource that cannot be read.
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
  A raw read checks the HTTP status itself: any other status outside 200 to 299 fails the item.

## Testing

`--self-test` runs 356 assertions. It starts a stub organization behind a real HTTP server on
`127.0.0.1` and runs the real `main()` against it, in-process, with a fake `arcgis.gis.GIS`. The stub
signs in with a username and password, hands out a token, and refuses a request that lacks it.
It answers search, item data, item update, resource list, resource read and resource update.
The resource update arrives as a multipart file upload, as it does from the `arcgis` package.

```
$ python agol_relink.py --self-test
agol_relink self-test: no arcgis package, no organization, local stub server only
--------------------------------------------------------------------
PASS  service root rewrites
PASS  sublayer index is kept
...
PASS  changes.csv title guards a leading '=' and nothing else  <-- pinned defect
...
PASS  a dry run sends no write request to the org  <-- pinned defect
...
PASS  a token in a matched url is blanked in changes.csv  <-- pinned defect
...
PASS  a dry run that finds an old url inside longer text exits 3, got 3  <-- pinned defect
PASS  each item is listed with the path of the embedded url  <-- pinned defect
...
PASS  a text resource that is not .json is listed as unread and not scanned  <-- pinned defect
...
PASS  an item whose data or resource is unread is listed even when it is clean  <-- pinned defect
PASS  a resource list longer than one page is followed to the end  <-- pinned defect
...
PASS  a dry run saves nothing to the temp folder, which the library does for a non-JSON type  <-- pinned defect
...
PASS  a resource is written as an uploaded file, never as text=  <-- pinned defect
...
PASS  a resource list that answers 200 with an error body fails the item, it is not an empty list  <-- pinned defect
...
PASS  a server that reports 7 but returns 5 exits 3, not 0  <-- pinned defect
...
PASS  --max-items that leaves items unexamined exits 3 and says so, not exit 0  <-- pinned defect
...
PASS  a search that returns no item exits 3, not a clean 0  <-- pinned defect
...
PASS  naming a service type without the flag is refused before any request  <-- pinned defect
...
PASS  run.log names every unread item, the 16th included, with its resource  <-- pinned defect
...
PASS  --no-resources skips the resources: 38 urls, and the run exits 3, not 0  <-- pinned defect
...
PASS  there is no --password flag: it is refused and its value is not echoed  <-- pinned defect
...
PASS  the password appears in no output file and not on the console  <-- pinned defect
PASS  no token appears in any output file, the backups included, or on the console  <-- pinned defect
PASS  a key=, apikey=, ticket= or fragment credential reaches no output file or the console  <-- pinned defect
...
PASS  a backup holds the original data with the token value blanked  <-- pinned defect
...
PASS  a raw read asks for the response, so the library saves no file to the temp folder and the tool sees the status  <-- pinned defect
...
PASS  a 502 with an empty body is an error, not an item with no data  <-- pinned defect
...
--------------------------------------------------------------------
356 assertions, 0 failed
```

The self-test covers:

- the matcher, the boundary rule, case and scheme options, the walk, and idempotence
- input validation and the refusal of `--ap`
- the formula guard for each of the nine columns, for `=`, tab and carriage return, plus `+`, `-`, `@` and line feed on the cell
- dry run and `--apply`, backups, `updated_ids.txt`, `--resume`, and the documented rollback
- every failure path: a refused update, a resource error dict, a raw payload that is not JSON,
  a failing resource listing, a listing that answers `200` with an error body, a resource that cannot be read or is not JSON, a `200` answer with an
  error body for item data or a resource, a search that keeps failing, and a retried `503` and `429`
- that a resource write is a multipart file upload and never `text=`
- the incomplete-scan exit, the unread-item exit, the exit for a search that returns nothing, a resource list of several pages, `--max-items` with and without items left over, and `--include-item-url`
- prompts, environment variables, the `CONFIGURATION` block, a wrong password, a missing `arcgis` package,
  and an `arcgis` import that fails on the ArcGIS Pro licence
- a search of every output file, the backups included, and of the console, for the password, the session token and a `key=`, `apikey=`, `ticket=` or fragment credential, with an item whose data and resource both carry a `token=`
- an old URL inside pop-up HTML, in a resource and in an encoded form: the run lists the item and exits 3, and the boundary rule that keeps `.../MapServer2` out
- a dry run against a stub that saves every raw read made without `return_raw_response` to a temp folder, and a check that the folder stays empty
- a stub that answers an item-data read with `503` and an HTML body, with `502` and no body, and with `404`: the first is retried, the others fail the item
- the refusal of a service type without `--include-item-url`, of `--no-resources` as a clean run, and of a resource name that points out of `backups/`
- the swapped-URL run that undoes a relink and keeps the token

Results on this version:

| Host | Python | Assertions |
|---|---|---|
| Windows | 3.13.2 (`C:\Python313`) | 356 |
| Windows | 3.13.7 (ArcGIS Pro conda clone) | 356 |
| Windows | 3.9.25 | 356 |
| Windows | 3.12 | 356 |
| Linux | 3.12.3 | 356 |

The counts are equal because the self-test does not use `arcpy` or `arcgis`. The permission-mode
assertions pass trivially on Windows, where they test nothing. They run for real on Linux.

Branch coverage was measured with `coverage run --branch agol_relink.py --self-test` on Windows, with
coverage.py 7.16.1 on a Python 3.13 virtual environment: 100% of
1761 statements and 374 branches. One line carries `# pragma: no branch`: the `if __name__ == "__main__"`
guard, whose false side only happens on import. The self-test code is part of the measured file.

### What the self-test does not cover

- **The `arcgis` package.** The stub implements the methods this tool calls, with the shapes the
  docstrings above describe. It does not prove that a live organization answers the same way.
  The write path was checked by hand against a disposable item on a live organization before this version.
  That check was not repeated for this version, and it predates the switch to a file upload.
- **The licence failure on import.** The self-test raises it from a fake module. The real
  `RuntimeError` was reproduced by hand in a Pro-derived environment.
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
