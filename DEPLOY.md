# Serving the boards

The exporter writes a folder of static files. This runs that folder behind a
small hardened container so the boards are reachable from a browser — your
phone on the same network, a home server, a machine you tunnel into.

**The image contains the server and nothing else.** Boards are never copied
into it; they are mounted read-only from the host. That one decision is what
makes both of the things you actually want to do cheap:

| | |
|---|---|
| Refresh the content | Re-export in place. No rebuild, no restart, no downtime. |
| Move it to another machine | Copy the export folder across and start the same image there. |

A 35 GB library never enters a build context, an image layer, or a registry.

## Start

```bash
cp .env.example .env          # optional; the defaults work
./boardctl refresh            # export from Freeform, then serve it
```

Then open <http://127.0.0.1:9384/>.

If you already have an export, skip straight to starting the server:

```bash
./dockerRun.sh
```

## The two commands

`./dockerRun.sh` starts the container. Plain `docker run`, no compose.

```bash
./dockerRun.sh                 # start (builds the image the first time)
./dockerRun.sh --rebuild       # rebuild the image first
./dockerRun.sh --foreground    # run attached, logs on the terminal, ^C stops
./dockerRun.sh --stop          # stop and remove the container
```

`./boardctl` wraps that plus everything around it. `boardctl up` runs
`dockerRun.sh`, so there is one copy of the docker invocation and no chance of
the two drifting apart.

```bash
./boardctl refresh             # re-export everything and swap it in
./boardctl status              # what exists, what is running, what answers
./boardctl logs -f
./boardctl down
./boardctl serve               # run the server directly, no docker at all
```

## Refreshing

```bash
./boardctl refresh
```

It exports into a staging directory beside the live one, then replaces the
live directory's **contents** — the directory itself is never renamed. That
matters: a bind mount follows the inode rather than the path, so swapping the
directory would leave the container looking at the old one forever. Swapping
the contents leaves the mount pointing at the same place, and the server reads
files per request, so new boards appear without touching the container.

Staging first means the live copy is only replaced once a complete export
exists, and it fixes something you would otherwise hit: the exporter never
overwrites, so a second export into a live directory accumulates
`photo (2).png` beside `photo.png` and keeps boards you have since deleted.

On APFS the staging copy is made of clones, so it is fast and costs almost no
disk. Elsewhere it needs room for a second copy — use `--in-place` there,
which clears the directory and exports straight into it, at the price of the
boards being unavailable while it runs.

```bash
./boardctl refresh --rebuild                    # rebuild the image too
./boardctl refresh --keep-old                   # keep the previous export
./boardctl refresh --in-place                   # no second copy on disk
./boardctl refresh -- --board MCAT --no-files   # flags after -- go to the exporter
```

`refresh` reads Apple Freeform directly, so it only runs on the Mac holding
the boards. Everywhere else, copy the export across.

## Holding boards back

Some boards should not be on the site. List them in `.boardignore`, one name
per line:

```
Propaganda
Group Think
MCAT*
```

Those boards are **still exported and still get their viewer** — they are
written to `IGNORED_DIR` (`./boards-ignored` by default) instead. That
directory is never mounted into the container, so the held-back boards are not
merely unlisted: they are not present for the server to serve at all, and
asking for one by name returns 404. They are absent from the master
`index.html` too, because that page is generated from the boards in the
directory it sits in.

`freeform.py backup` is untouched by any of this. It copies the whole library
verbatim, held-back boards included.

Matching:

| | |
|---|---|
| `Propaganda` | that board exactly, ignoring case |
| `MCAT*` | every board whose name starts with MCAT |
| `*TEMP*` | every board with TEMP anywhere in the name |
| `83723B86` | the start of a board id, from `freeform.py list --json` |

Plain names match **exactly** on purpose. Listing `MCAT` holds back the board
called MCAT and leaves `MCAT - TEMP1` alone — a substring rule would quietly
take both, and quietly taking more than you asked for is the one thing this
file must not do. Ask for a glob when you want the family.

A line starting with `#` is a comment; a `#` anywhere else is part of the name.

Check what it will do before running anything:

```bash
./boardctl ignored
```

```
  ignore file: /path/to/.boardignore
  36 served, 1 held back
    held back: MCAT
```

Entries matching no board at all are reported as you refresh, so a typo shows
up rather than silently protecting nothing.

Two things to know. A board you stop ignoring is exported back into the served
directory on the next refresh, but its old copy stays in `IGNORED_DIR` until
you delete it — `refresh` says so when that happens. And if the file holds
back *everything*, the served directory is emptied rather than left serving
what it served last time.

## Moving it to another machine

Nothing has to be rebuilt, because nothing about the boards is baked in.

```bash
rsync -a --delete boards/ server:/srv/freeform-boards/
scp serve.py Dockerfile dockerRun.sh .env.example server:/opt/freeform/
```

On the other machine, set `BOARDS_DIR=/srv/freeform-boards` in `.env` and run
`./dockerRun.sh`. Updating later is the same `rsync` — `--delete` gives you
the same replace-don't-merge behaviour that `refresh` gives you locally, and
the running container picks it up.

`serve.py` runs on its own too, if you would rather not use docker at all:

```bash
python3 serve.py /srv/freeform-boards --host 0.0.0.0 --port 9384
```

## Settings

`.env` next to the scripts. Anything already set in your environment wins over
what is written there, so one-offs are easy:

```bash
PORT=9000 ./dockerRun.sh
```

| | |
|---|---|
| `BOARDS_DIR` | the export to serve. Mounted, never copied. |
| `IGNORED_DIR` | where held-back boards are exported. Never mounted. |
| `IGNORE_FILE` | the list of boards to hold back, default `.boardignore`. |
| `BIND_ADDR` | `127.0.0.1` for this machine only, `0.0.0.0` for the network. |
| `PORT` | host port, default 9384. |
| `FREEFORM_AUTH` | `user:password` to require basic auth. Blank for none. |
| `FREEFORM_ASSET_MAX_AGE` | seconds a browser may reuse media. Pages always revalidate. |

## Reaching it from your phone

Set `BIND_ADDR=0.0.0.0`, restart, and open your Mac's LAN address. Two things
worth knowing before you do:

- Everything on the network can then read every board. Set `FREEFORM_AUTH` as
  well if that is not what you want.
- Basic auth sends the password in the clear. Over plain HTTP it keeps out the
  casually curious and nothing more. If the boards are private, put this behind
  a private network — Tailscale, WireGuard, or an SSH forward:

  ```bash
  ssh -N -L 9384:127.0.0.1:9384 you@yourmac
  ```

  and leave `BIND_ADDR` at `127.0.0.1`.

## What "hardened" means here

The container:

- runs as uid 10001, never root, with no login shell and no home directory
- has a **read-only root filesystem**; the only writable path is a 16 MB
  `/tmp` mounted `noexec,nosuid,nodev`
- mounts the boards **read-only** — the server cannot alter your export
- drops **all** capabilities and sets `no-new-privileges`, so a setuid binary
  is not a route to anything
- is capped at 128 processes and 512 MB
- ships **no pip and no setuptools**: the interpreter has no way to fetch code
- carries `serve.py` owned by root and mode `0444`, so the user running the
  server cannot rewrite the server

The server:

- answers `GET` and `HEAD` only
- resolves every path and refuses anything that lands outside the export,
  symlinks included
- writes no directory listings, apart from a board index at `/` when the
  export has no `index.html` of its own
- sets `Content-Security-Policy` (`default-src 'none'`, self only, no frames,
  no forms), `nosniff`, `no-referrer`, `X-Frame-Options: DENY`
- has no dependencies. Standard library only, like the exporter

The CSP allows `'unsafe-inline'` for script and style, because the generated
viewer inlines both and there is nothing rewriting it at request time to hand
it a nonce.

## Why not just `python3 -m http.server`

It does not do byte ranges, so video will not seek and Safari will not play it
at all; it does not do conditional requests, so every visit re-downloads
everything instead of costing a few 304s; and it prints a browsable listing of
your files.

## Speed

On a Linux host the mount is a real bind mount and files move at disk speed.

On macOS the boards cross a VM boundary, and how fast depends entirely on the
mount type. This machine's colima runs QEMU with **sshfs**, which measured
about **27 MB/s** — fine for the viewer, which loads small previews and only
fetches originals on demand, and enough for video, but a fraction of the
2.2 GB/s the same server does natively. `virtiofs` is much faster and needs
`vmType: vz`:

```bash
colima stop && colima start --vm-type vz --mount-type virtiofs
```

For qemu, `--mount-type 9p` is the faster stable option. Changing this
restarts the VM and affects everything else running in it, so it is a
deliberate choice rather than a default worth flipping.

sshfs also rounds file timestamps to the second and makes up its own inode
numbers. Nothing you will notice: it only matters for two versions of a file
that have the same size and are written inside the same second, and a refresh
writes them minutes apart.

## When it will not start

**`the docker daemon is not reachable`** — `colima start`, or open Docker
Desktop.

**`~/.docker/buildx is not readable by you`** — that directory is owned by
root, left behind by a `sudo docker` at some point. The scripts route around
it automatically. To fix it properly:

```bash
sudo chown -R "$(id -un)" ~/.docker/buildx
```

**A board 404s** — check the name survived the trip. `boardctl status` shows
what is mounted where; `docker logs freeform-boards` shows the request that
missed.
