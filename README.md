# Freeform Board Exporter

List, back up, and export Apple Freeform boards from the command line.

Freeform has no export-everything feature — it can only share one board at a
time, and PDF export flattens a board into a picture. This reads Freeform's own
storage directly and gets your content back out: the **original files** exactly
as you dropped them in, plus the **layout** — what is on the board, where, and
what it says.

Python 3, standard library only. No dependencies, nothing to install.

```bash
python3 freeform.py list
```

## Commands

### `list` — every board

```bash
python3 freeform.py list
python3 freeform.py list --json          # ids, timestamps, viewport, sizes
python3 freeform.py list --include-deleted
```

```
TITLE                           MODIFIED    ITEMS  FILES       SIZE  FLAGS
----------------------------------------------------------------------------
50Food                          2026-02-14     54     52     5.6 MB  S
BL                              2026-08-21    165     95     2.3 GB  S
Catan / Seafareous / RBTL       2026-08-22    221     24   138.2 MB  S
...
----------------------------------------------------------------------------
37 boards, 1025 original files, 5.6 GB

Flags: S=shared  O=owned by someone else  *=favorite  D=deleted
```

### `info` — detail for one board

Match a board by title fragment or by id:

```bash
python3 freeform.py info Propaganda
```

```
Propaganda
  id         83723B86-F9AC-42F5-835B-A58A5883FC6B
  modified   2026-08-17T04:22:55.947062+00:00
  shared     True
  viewport   zoom 0.75  offset [6813.3, 184.0]
  items      72
  files      10  (110.7 MB)
  breakdown  link 55, image 7, text 4, container 2, movie 2, file 1, drawing 1
  extent     x -1483..8059   y 0..1452
```

### `backup` — the whole library, verbatim

A complete, restorable copy of Freeform's database and attachment store.

```bash
python3 freeform.py backup ~/Backups
python3 freeform.py backup ~/Backups --zip        # compress instead
python3 freeform.py backup ~/Backups --no-assets  # database only
```

On the same drive this uses **APFS clones**: a 5.9 GB library backs up in about
two seconds and consumes no additional disk space until the originals change.
Backing up to an external drive falls back to a real copy, and the tool checks
there is room before it starts.

```
freeform-backup-2026-08-22-144140/
├── boards.db, boards.db-wal, boards.db-shm
├── side.db, side.db-wal, side.db-shm
├── Snapshot.plist          the board list the app shows
├── Assets/                 2737 files, 5.9 GB
└── manifest.json           what was in this backup, in readable form
```

**To restore:** quit Freeform, then copy `boards.db*`, `side.db*` and `Assets/`
back into
`~/Library/Containers/com.apple.freeform/Data/Library/Freeform/Boards/`.

### `export` — files, layout, and a viewer

```bash
python3 freeform.py export ~/Desktop/boards
python3 freeform.py export ~/Desktop/boards --board MCAT --board Logic
python3 freeform.py export ~/Desktop/boards --no-files      # layout only
python3 freeform.py export ~/Desktop/boards --no-html       # no viewer
python3 freeform.py export ~/Desktop/boards --no-previews   # originals only
```

One folder per board:

```
Storage Unit Sizes/
├── index.html      pan-and-zoom viewer for this board
├── layout.json     every item: type, position, size, rotation, text, links
├── board.md        the same thing, readable
├── files/          your originals, under their original names
│   ├── 10x20-1.png
│   ├── 5x15-1.png
│   └── pasted-image.png
└── previews/       Freeform's own small renderings, used by the viewer
```

plus an `index.html` at the top listing every board.

`layout.json` per item:

```json
{
  "id": "599BF172-ABB3-472B-AD60-CF1F6090AFD4",
  "parent": "0AC8A152-071B-41EC-8C52-9444C6B4F671",
  "type": "text",
  "type_id": 3,
  "geometry": {
    "x": 1213.52, "y": 383.5,
    "width": null, "height": null,
    "rotation": 0.0, "flags": [0, 0, 0, 0]
  },
  "assets": [],
  "text": "5x10"
}
```

Coordinates are in points, origin top-left, x right and y down — the same space
Freeform uses. `geometry` is the rectangle the item occupies on the board.
Freeform stores a grouped item relative to its group, and groups nest; the
exporter adds those offsets back in, so grouped items are in board
coordinates like everything else, and `parent` names the group.
`width`/`height` are `null` where Freeform sizes an item automatically (text
boxes, mostly). `rotation` is radians. The board's saved camera is under
`board.viewport` as `zoom` and `offset`.

### Cropped images

Cropping an image in Freeform never touches the file. The item's frame goes on
describing where the whole picture would sit, and a second rectangle records
which part of it to show — which is why an exported file is always the full
original even when the board shows a corner of it.

A cropped image carries a `crop` alongside its geometry:

```json
"geometry": { "x": 1925.27, "y": 862.79, "width": 259.05, "height": 302.23 },
"crop": { "offset_x": 614.67, "offset_y": 54.85,
          "full_width": 912.34, "full_height": 513.19 }
```

Draw the file at `full_width` × `full_height`, shifted up and left by the two
offsets, and clip it to `geometry`. The viewer does exactly this, so a cropped
image looks the way it does on the board while `files/` still holds everything
the original had.

Item types: `text`, `image`, `movie`, `file`, `link`, `drawing`, `container`.

Freeform files three different things under `movie`: real video, animated GIFs,
and audio. The viewer picks the right element for each — a `<video>` pointed at
a GIF refuses to play it.

## The viewer

Each board gets a self-contained `index.html`. Open it from Finder, or copy the
whole folder to a phone and open it there — it needs no server and no network.

- **Pan and zoom** — drag to pan with a flick and glide, pinch or scroll to
  zoom, double-tap to zoom in, and the zoom chip fits the whole board. Zoom runs
  from 0.2% to 100000%; text and link cards stay sharp the whole way up, because
  the board is real HTML being re-rendered rather than a picture being enlarged.
  You cannot pan off into nothing — a corner of the board always stays on screen.
- **Tap any item for its card** — its name and size, with **Download**,
  **Open** and **Share**. Download and Open are there for anything with a file;
  on a link card Open goes to the page. On iOS a long press on a picture still
  offers "Save to Photos" as usual.
- **Link to any item** — tapping an item puts its id in the address bar,
  `Propaganda/#DF103E7B-7551-4A39-A649-BDAC3A826C0D`, and Share copies that
  link. Opening it glides the board to that item, centres it, and opens its
  card. The id is Freeform's own, the same one `layout.json` uses, so a link
  keeps working after the board is exported again.
- **Videos** — show their poster with a play button. The play button plays the
  video in place; tapping anywhere else on it opens its card, and a selected
  video gets the player's own controls. Videos keep `preload="none"`, so a
  board full of them costs nothing until you press play.
- **Text** — selectable and copyable once you tap it, and findable with the
  browser's own Find. Tapping first means a drag over text pans instead of
  selecting.
- **Big boards stay usable** — a board holding gigabytes of photos opens in a
  moment. Only items near the screen load at all, they load as small previews,
  and the full-size original is fetched for an item only once it is drawn large
  enough to be worth it. On a 2.3 GB board that is a few megabytes on open
  instead of one and a half gigabytes.

`--no-previews` skips the preview images. The viewer still works, but it then
loads full-size originals at every zoom level, which is slow on a large board.

The previews are Freeform's own: about 35 MB of thumbnails stand in for 1.1 GB
of photos. Link cards get their picture from the page metadata Freeform saved
with each link, which is also where their titles come from.

## Serving it

The viewer needs no server — open `index.html` from Finder and it works. To
reach the boards from a phone or another machine instead, there is a small
hardened container that serves the export directory:

```bash
./boardctl refresh        # export everything, then serve it
./dockerRun.sh            # or just serve an export you already have
```

Boards you would rather not publish go in `.boardignore`: they are still
exported and still get their viewer, but into a separate directory that is
never mounted, so they are absent from the site and from its index.

Python on Alpine, standard library only, running unprivileged with a read-only
filesystem and the boards mounted read-only. The image holds the server and
nothing else, so refreshing the content is a file operation rather than a
rebuild, and moving the whole thing to another machine is a copy of the export
folder. See [DEPLOY.md](DEPLOY.md).

## What gets exported

| | |
|---|---|
| Images, videos, PDFs and other attachments | Original bytes, original filenames, verified byte-identical |
| Crops | Decoded and applied in the viewer; the exported file stays uncropped |
| Text and sticky notes | Full text content, with font size, weight, italics and colour |
| Links | URL, page title, site name, and the preview picture |
| Position, size, rotation | Per item, in board coordinates |
| Parent/child grouping | `parent` field; grouped items placed in board coordinates |
| Board viewport | zoom and scroll offset |
| Freehand drawings | Recorded with geometry; stroke paths are **not** decoded |
| Shape fills | Decoded where present |
| Text alignment and lists | **Not** decoded — the stored values are ambiguous, so text renders left-aligned |
| Z-order (stacking) | **Not** recovered |

For anything in the "not decoded" rows, `backup` is what preserves it — the
database keeps full fidelity even where this exporter does not read it.

## Where the data lives

```
~/Library/Containers/com.apple.freeform/Data/Library/Freeform/Boards/
    boards.db      boards, items, asset index (SQLite)
    Assets/        attachment blobs, named by content id
~/Library/Group Containers/group.com.apple.freeform/Snapshot.plist
    the board list shown in the app's browser
```

Freeform syncs through CloudKit, not iCloud Drive, so boards are not files in
Finder and nothing here needs Full Disk Access.

Board titles, geometry and text are stored as `crdt`-framed protobuf. This tool
decodes them by structure rather than by a schema, which is why it reports a
type id alongside every item: an item Freeform adds in a future release still
exports with its geometry intact.

## Safety

- **Nothing is written to Freeform's own files.** The database is copied to a
  temp directory (with its write-ahead log) before it is opened, so exports can
  run while Freeform is open.
- `backup` warns if Freeform is running — quit it for a guaranteed-consistent
  snapshot, since a board being edited mid-copy may be caught between writes.
- Exports never overwrite: colliding filenames become `name (2).ext`.

## Notes

- Boards shared *with* you are included and marked `O`. Their identifier carries
  the owner's account id, which the tool handles.
- Link cards get their picture cropped to fill, the way Freeform draws them.
- Item counts in `list` count each attachment once, even when the same image
  appears twice on a board, so they match the file count `export` writes.
