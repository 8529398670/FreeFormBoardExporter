#!/usr/bin/env python3
"""
html_generator.py — build a browsable viewer for exported Freeform boards.

`freeform.py export` writes a folder per board holding layout.json and the
original files. This turns that into HTML you can open straight from Finder or
copy to a phone: an infinite pan-and-zoom canvas that draws the board from its
real coordinates, plus an index page listing every board.

Design notes
------------
Items are real DOM nodes on a CSS-transformed plane, not a canvas or SVG
drawing. That choice buys several things at once: text stays crisp at any zoom
because the browser re-rasterises it, images are ordinary <img> elements so a
long press offers "Save to Photos" on iOS, and the whole board stays selectable
and findable with the browser's own Find.

Layout data is inlined into the page rather than fetched, because a fetch() from
a file:// page is blocked as a cross-origin request. Opening the .html directly
has to work — that is the point.

Positions follow Freeform's own anchoring: an axis Freeform sizes explicitly is
measured from the item's leading edge, while an axis it sizes automatically is
measured from the item's centre, since such a box grows symmetrically.

Every item can be linked to. Selecting one puts its Freeform id in the URL
fragment (board/#<id>, the id layout.json uses), and opening such a URL flies
the board to that item and opens its card. The fragment rather than a query
string: it never reaches the server, and following a link to another item on
the same board moves within the page instead of reloading it.
"""

import html
import json
import os

# Freeform's own canvas is light, and the text colours in a board were chosen
# against it, so the board surface stays light in either system theme. Only the
# surrounding interface follows the viewer's preference.
BOARD_BACKGROUND = "#f7f7f5"

MIN_SCALE = 0.002
MAX_SCALE = 1000.0


# --------------------------------------------------------------------------
# item preparation
# --------------------------------------------------------------------------

# Freeform files three different things under "movie": real video, animated
# images, and audio. They each need a different element — a <video> pointed at
# a GIF refuses the source outright.
_AUDIO_EXTS = {"m4a", "mp3", "wav", "aac", "aiff", "aif", "flac", "caf"}
_IMAGE_EXTS = {"gif", "apng", "webp", "avif", "png", "jpg", "jpeg",
               "heic", "heif", "tiff", "tif", "bmp"}


def _primary_asset(item):
    """The exported original for an item, if one was written."""
    for asset in item.get("assets", []):
        if asset.get("role") in ("image", "movie", "file") and asset.get("exported"):
            return asset
    return None


def _preview_asset(item):
    """Freeform's own small rendering of an item, when it was exported.

    For a link this is the page's preview picture, which is the whole visual;
    for a photo or a video it is a thumbnail standing in until the original is
    worth loading.
    """
    for asset in item.get("assets", []):
        if (asset.get("role") in ("thumbnail", "posterImage", "linkMetadata")
                and asset.get("exported")):
            return asset
    return None


def prepare_items(items):
    """Reduce layout.json items to what the viewer needs, dropping the rest."""
    out = []
    for item in items:
        geo = item.get("geometry")
        kind = item.get("type")
        # Containers are Freeform's grouping nodes: no visual of their own.
        if not geo or kind == "container":
            continue

        node = {
            "t": kind,
            "x": round(geo["x"], 2),
            "y": round(geo["y"], 2),
        }
        # Freeform's own id, so a link to an item survives a re-export.
        if item.get("id"):
            node["id"] = item["id"]
        if geo.get("width") is not None:
            node["w"] = round(geo["width"], 2)
        if geo.get("height") is not None:
            node["h"] = round(geo["height"], 2)
        if geo.get("rotation"):
            node["r"] = round(geo["rotation"], 5)

        crop = item.get("crop")
        if crop:
            node["crop"] = [crop["offset_x"], crop["offset_y"],
                            crop["full_width"], crop["full_height"]]

        if item.get("text"):
            node["text"] = item["text"]
        if item.get("url"):
            node["url"] = item["url"]
        if item.get("title"):
            node["title"] = item["title"]
        if item.get("site"):
            node["site"] = item["site"]
        if item.get("fill"):
            node["fill"] = item["fill"]

        style = item.get("style") or {}
        if style.get("font_size"):
            node["fs"] = style["font_size"]
        if style.get("color"):
            node["fg"] = style["color"]
        for flag, key in (("bold", "b"), ("italic", "i"),
                          ("underline", "u"), ("strikethrough", "st")):
            if style.get(flag):
                node[key] = 1

        asset = _primary_asset(item)
        if asset:
            node["src"] = asset["exported"]
            node["name"] = item.get("source_name") or os.path.basename(asset["exported"])
            if asset.get("size"):
                node["bytes"] = asset["size"]
            if kind == "movie":
                ext = (asset.get("extension")
                       or os.path.splitext(asset["exported"])[1].lstrip(".")).lower()
                if ext in _AUDIO_EXTS:
                    node["media"] = "audio"
                elif ext in _IMAGE_EXTS:
                    node["media"] = "image"
        preview = _preview_asset(item)
        if preview:
            node["prev"] = preview["exported"]
        elif item.get("source_name") and "src" not in node:
            node["name"] = item["source_name"]

        out.append(node)
    return out


def _json_for_script(payload):
    """Embed JSON in a <script> without letting its text close the tag."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _human_size(num):
    if not num:
        return ""
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


# --------------------------------------------------------------------------
# board page
# --------------------------------------------------------------------------

def board_html(board, items, has_index=False):
    nodes = prepare_items(items)
    payload = {
        "title": board.get("title") or "Untitled",
        "uuid": board.get("uuid"),
        "modified": board.get("modified"),
        "viewport": board.get("viewport") or {},
        "items": nodes,
        "minScale": MIN_SCALE,
        "maxScale": MAX_SCALE,
    }
    title = html.escape(payload["title"])
    back = ('<a class="back" href="../index.html" aria-label="All boards">'
            '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">'
            '<path d="M15 5l-7 7 7 7" fill="none" stroke="currentColor" '
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
            '</svg></a>') if has_index else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{title}</title>
<style>{_CSS}</style>
</head>
<body>
<div id="stage">
  <div id="world"></div>
</div>

<header id="bar">
  {back}
  <div class="titles">
    <h1>{title}</h1>
    <p id="count"></p>
  </div>
</header>

<div id="controls">
  <button id="zoomIn"  aria-label="Zoom in">+</button>
  <button id="zoomPct" aria-label="Zoom to fit">100%</button>
  <button id="zoomOut" aria-label="Zoom out">&minus;</button>
</div>

<div id="sheet" hidden>
  <div class="grip"></div>
  <div class="row">
    <div class="meta">
      <strong id="sheetName"></strong>
      <span id="sheetInfo"></span>
    </div>
    <button id="sheetClose" aria-label="Close">&times;</button>
  </div>
  <div class="actions">
    <a id="sheetDownload" class="primary" download>Download</a>
    <a id="sheetOpen" target="_blank" rel="noopener">Open</a>
    <button id="sheetShare" type="button">Share</button>
  </div>
</div>

<div id="hint">Drag to pan · pinch or scroll to zoom · tap an item to save, share or select it</div>

<script type="application/json" id="data">{_json_for_script(payload)}</script>
<script>{_JS}</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# index page
# --------------------------------------------------------------------------

def index_html(entries):
    """entries: [{title, folder, items, files, bytes, modified, shared}]"""
    cards = []
    for entry in sorted(entries, key=lambda e: (e["title"] or "").lower()):
        href = html.escape(f"{entry['folder']}/index.html", quote=True)
        size = _human_size(entry.get("bytes"))
        bits = [f"{entry['items']} items"]
        if entry.get("files"):
            bits.append(f"{entry['files']} files")
        if size:
            bits.append(size)
        badge = '<span class="badge">shared</span>' if entry.get("shared") else ""
        modified = (entry.get("modified") or "")[:10]
        cards.append(
            f'<a class="card" href="{href}">'
            f'<span class="name">{html.escape(entry["title"] or "Untitled")}{badge}</span>'
            f'<span class="sub">{html.escape(" · ".join(bits))}</span>'
            f'<span class="date">{html.escape(modified)}</span>'
            f'</a>'
        )

    total_items = sum(e["items"] for e in entries)
    total_files = sum(e.get("files") or 0 for e in entries)
    total_bytes = sum(e.get("bytes") or 0 for e in entries)
    summary = (f"{len(entries)} boards · {total_items} items · "
               f"{total_files} files · {_human_size(total_bytes) or '0 B'}")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Freeform Boards</title>
<style>{_INDEX_CSS}</style>
</head>
<body>
<header>
  <h1>Freeform Boards</h1>
  <p>{html.escape(summary)}</p>
</header>
<main>{''.join(cards)}</main>
</body>
</html>
"""


# --------------------------------------------------------------------------
# assets
# --------------------------------------------------------------------------

_CSS = """
*{box-sizing:border-box}
:root{
  --chrome:rgba(255,255,255,.82); --chrome-line:rgba(0,0,0,.10);
  --ink:#14141a; --ink-dim:#6b6b76; --accent:#0a68f0; --sheet:#fff;
}
@media (prefers-color-scheme:dark){
  :root{
    --chrome:rgba(28,28,32,.82); --chrome-line:rgba(255,255,255,.12);
    --ink:#f2f2f5; --ink-dim:#9b9ba6; --accent:#4b9bff; --sheet:#1c1c20;
  }
}
html,body{margin:0;height:100%;overflow:hidden;overscroll-behavior:none}
body{
  font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,sans-serif;
  background:BOARDBG; color:var(--ink); -webkit-text-size-adjust:100%;
}

#stage{
  position:fixed; inset:0; background:BOARDBG;
  touch-action:none; cursor:grab; overflow:hidden;
  -webkit-user-select:none; user-select:none;
}
#stage.dragging{cursor:grabbing}
#world{position:absolute; top:0; left:0; transform-origin:0 0; will-change:auto}
#stage.interacting #world{will-change:transform}

.it{position:absolute; transform-origin:center center}
.it.sel{outline:2px solid var(--accent); outline-offset:3px; border-radius:3px}

.it.image,.it.movie{background:rgba(0,0,0,.05); border-radius:2px; overflow:hidden}
.it.image img,.it.movie video,.it.movie img{
  display:block; width:100%; height:100%; object-fit:contain;
  -webkit-user-select:none; user-select:none; -webkit-touch-callout:default;
}
.it.movie.audio{
  background:#fff; border:1px solid rgba(0,0,0,.12); border-radius:10px;
  display:flex; flex-direction:column; justify-content:center; gap:8px; padding:12px;
}
.it.movie.audio .label{font-size:13px; color:#14141a; word-break:break-word;
  display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden}
.it.movie.audio audio{width:100%}
/* A video's play badge, standing in for the player's controls until the
   video is selected. */
.it.movie .play{
  position:absolute; left:50%; top:50%; transform:translate(-50%,-50%);
  width:min(64px,36%); aspect-ratio:1; border-radius:50%; cursor:pointer;
  background:rgba(20,20,26,.55); border:1.5px solid rgba(255,255,255,.9);
}
.it.movie .play::after{
  content:""; position:absolute; top:28%; bottom:28%; left:37%; right:22%;
  background:#fff; clip-path:polygon(0 0,100% 50%,0 100%);
}
.it.movie.playing .play{display:none}
.it.image img{background:transparent}
.it.image.cropped img{
  position:absolute; max-width:none; max-height:none; object-fit:fill;
}

/* Text stays unselectable so a drag over it pans the board. Tapping an item
   turns selection on for that one item, which is when copying is intended. */
.it.text{
  color:#14141a; white-space:pre-wrap; word-break:break-word;
  padding:2px 0; -webkit-user-select:none; user-select:none;
}
.it.text.auto{white-space:pre}
.it.text.selectable{-webkit-user-select:text; user-select:text; cursor:text}

.it.link{
  background:#fff; border:1px solid rgba(0,0,0,.12); border-radius:10px;
  padding:10px 12px; overflow:hidden; display:flex; flex-direction:column;
  justify-content:center; gap:4px; color:#14141a; text-decoration:none;
}
/* No position here: .it is already absolute, which is the containing block
   the picture anchors to. Setting position:relative would outrank it and drop
   every card back into normal flow, stacking them down the page. */
.it.link.rich{padding:0; justify-content:flex-end}
.it.link .shot{
  position:absolute; inset:0; width:100%; height:100%;
  object-fit:cover;             /* fill and crop, the way Freeform does */
  background:rgba(0,0,0,.05);
}
.it.link .cap{display:flex; flex-direction:column; gap:2px; padding:10px 12px}
.it.link.rich .cap{
  position:relative; background:rgba(255,255,255,.93);
  border-top:1px solid rgba(0,0,0,.08);
  backdrop-filter:blur(8px); -webkit-backdrop-filter:blur(8px);
}
.it.link .host{font-weight:600; font-size:13px; color:#14141a;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden}
.it.link .path{font-size:12px; color:#6b6b76; word-break:break-all;
  display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden}
.it.link.rich .path{-webkit-line-clamp:1}

.it.file{
  background:#fff; border:1px solid rgba(0,0,0,.12); border-radius:10px;
  display:flex; align-items:center; justify-content:center; text-align:center;
  padding:10px; font-size:13px; color:#14141a; word-break:break-word; overflow:hidden;
}
.it.drawing{
  border:1px dashed rgba(0,0,0,.22); border-radius:6px;
  background:repeating-linear-gradient(45deg,rgba(0,0,0,.02) 0 8px,transparent 8px 16px);
}

#bar{
  position:fixed; top:0; left:0; right:0; display:flex; align-items:center; gap:6px;
  padding:calc(env(safe-area-inset-top) + 8px) 12px 8px;
  background:var(--chrome); backdrop-filter:saturate(180%) blur(18px);
  -webkit-backdrop-filter:saturate(180%) blur(18px);
  border-bottom:1px solid var(--chrome-line); z-index:10;
}
#bar .titles{min-width:0}
#bar h1{margin:0; font-size:16px; font-weight:600; letter-spacing:-.01em;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
#bar p{margin:0; font-size:12px; color:var(--ink-dim)}
.back{display:grid; place-items:center; width:34px; height:34px; flex:none;
  color:var(--ink); text-decoration:none; border-radius:9px}
.back:active{background:var(--chrome-line)}

#controls{
  position:fixed; right:12px; bottom:calc(env(safe-area-inset-bottom) + 16px);
  display:flex; flex-direction:column; z-index:10;
  background:var(--chrome); backdrop-filter:saturate(180%) blur(18px);
  -webkit-backdrop-filter:saturate(180%) blur(18px);
  border:1px solid var(--chrome-line); border-radius:12px; overflow:hidden;
}
#controls button{
  appearance:none; border:0; background:transparent; color:var(--ink);
  font:inherit; font-size:19px; width:46px; height:42px; cursor:pointer;
}
#controls #zoomPct{font-size:11px; font-variant-numeric:tabular-nums;
  border-block:1px solid var(--chrome-line); color:var(--ink-dim)}
#controls button:active{background:var(--chrome-line)}
body.sheet-open #controls{opacity:0; pointer-events:none; transition:opacity .15s}

#sheet{
  position:fixed; left:0; right:0; bottom:0; z-index:20;
  padding:6px 16px calc(env(safe-area-inset-bottom) + 16px);
  background:var(--sheet); border-top:1px solid var(--chrome-line);
  border-radius:16px 16px 0 0; box-shadow:0 -8px 40px rgba(0,0,0,.18);
  animation:rise .18s ease-out;
}
@keyframes rise{from{transform:translateY(100%)}to{transform:none}}
#sheet .grip{width:36px; height:4px; border-radius:2px; margin:0 auto 10px;
  background:var(--chrome-line)}
#sheet .row{display:flex; align-items:flex-start; gap:12px}
#sheet .meta{min-width:0; flex:1}
/* A link's title and address, or a text's first line, can run long. */
#sheet strong{display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:3;
  overflow:hidden; font-size:15px; word-break:break-word}
#sheet span{display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2;
  overflow:hidden; font-size:13px; color:var(--ink-dim); overflow-wrap:anywhere}
#sheetClose{appearance:none; border:0; background:transparent; color:var(--ink-dim);
  font-size:26px; line-height:1; padding:0 4px; cursor:pointer}
#sheet .actions{display:flex; gap:10px; margin-top:14px}
#sheet .actions a,#sheet .actions button{
  flex:1; min-width:0; text-align:center; padding:12px 8px; border-radius:11px;
  font:inherit; font-weight:600; font-size:15px; text-decoration:none; color:var(--ink);
  -webkit-appearance:none; appearance:none; background:transparent; cursor:pointer;
  border:1px solid var(--chrome-line); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
#sheet .actions .primary{background:var(--accent); color:#fff; border-color:transparent}
#sheet .actions [hidden]{display:none}

#hint{
  position:fixed; left:50%; transform:translateX(-50%);
  bottom:calc(env(safe-area-inset-bottom) + 18px); z-index:9;
  padding:7px 14px; border-radius:999px; font-size:12px; color:var(--ink-dim);
  background:var(--chrome); border:1px solid var(--chrome-line);
  backdrop-filter:blur(18px); -webkit-backdrop-filter:blur(18px);
  transition:opacity .5s ease; pointer-events:none; max-width:88vw; text-align:center;
}
#hint.gone{opacity:0}
@media (max-width:520px){ #hint{font-size:11px} }
""".replace("BOARDBG", BOARD_BACKGROUND)


_JS = r"""
(function(){
'use strict';
var DATA = JSON.parse(document.getElementById('data').textContent);
var stage = document.getElementById('stage');
var world = document.getElementById('world');
var items = DATA.items || [];

document.getElementById('count').textContent =
  items.length + (items.length === 1 ? ' item' : ' items');

/* ---- build nodes ------------------------------------------------------ */

function px(v){ return v + 'px'; }

function hostOf(url){
  try { return new URL(url).host.replace(/^www\./, ''); } catch (e) { return ''; }
}

var nodes = [];
var byId = {};
items.forEach(function(it){
  var el = document.createElement(it.t === 'link' ? 'a' : 'div');
  el.className = 'it ' + it.t;
  el.style.left = px(it.x);
  el.style.top  = px(it.y);

  /* Freeform measures an automatically sized axis from the item's centre,
     because such a box grows outward in both directions. */
  var tx = (it.w === undefined) ? '-50%' : '0';
  var ty = (it.h === undefined) ? '-50%' : '0';
  var t = (tx !== '0' || ty !== '0') ? 'translate(' + tx + ',' + ty + ')' : '';
  if (it.r) t += (t ? ' ' : '') + 'rotate(' + it.r + 'rad)';
  if (t) el.style.transform = t;

  if (it.w !== undefined) el.style.width = px(it.w);
  if (it.h !== undefined) el.style.height = px(it.h);
  if (it.fill) el.style.background = it.fill;

  if (it.t === 'text'){
    if (it.w === undefined) el.classList.add('auto');
    el.textContent = it.text || '';
    if (it.fs) el.style.fontSize = px(it.fs);
    if (it.fg) el.style.color = it.fg;
    if (it.b)  el.style.fontWeight = '600';
    if (it.i)  el.style.fontStyle = 'italic';
    if (it.u || it.st){
      el.style.textDecoration = (it.u ? 'underline ' : '') + (it.st ? 'line-through' : '');
    }
  } else if (it.t === 'image'){
    var img = document.createElement('img');
    img.alt = it.name || '';
    img.decoding = 'async';
    img.draggable = false;
    if (it.crop){
      /* Cropping keeps the whole file and shows a window onto it: lay the
         picture out at its full size and slide it under a clipping box. */
      el.classList.add('cropped');
      img.style.left = px(-it.crop[0]);
      img.style.top = px(-it.crop[1]);
      img.style.width = px(it.crop[2]);
      img.style.height = px(it.crop[3]);
    }
    el.appendChild(img);
    it._img = img;
  } else if (it.t === 'movie' && it.media === 'image'){
    /* An animated GIF is filed as a movie but is really a picture. */
    var anim = document.createElement('img');
    anim.alt = it.name || '';
    anim.decoding = 'async';
    anim.draggable = false;
    el.appendChild(anim);
    it._img = anim;
  } else if (it.t === 'movie' && it.media === 'audio'){
    el.classList.add('audio');
    var label = document.createElement('span');
    label.className = 'label';
    label.textContent = it.name || 'audio';
    var snd = document.createElement('audio');
    snd.controls = true; snd.preload = 'none';
    el.appendChild(label); el.appendChild(snd);
    it._vid = snd;               /* playable media: always gets the real file */
  } else if (it.t === 'movie'){
    /* A <video controls> answers every tap itself, so a bare one could never
       be selected the way everything else is. It shows its poster and a play
       badge instead, and gets the player's controls while it is selected. */
    var vid = document.createElement('video');
    vid.preload = 'none'; vid.playsInline = true;
    if (it.prev) vid.poster = it.prev;
    var badge = document.createElement('span');
    badge.className = 'play';
    var showPlaying = function(){ el.classList.toggle('playing', !vid.paused); };
    vid.addEventListener('play', showPlaying);
    vid.addEventListener('pause', showPlaying);
    vid.addEventListener('emptied', showPlaying);
    el.appendChild(vid); el.appendChild(badge);
    it._vid = vid;
  } else if (it.t === 'link'){
    el.href = it.url || '#';
    el.target = '_blank'; el.rel = 'noopener';
    var domain = hostOf(it.url);

    /* Freeform draws the page's preview picture filling the card and crops
       whatever will not fit, so the card is mostly image with a caption. */
    if (it.prev){
      var shot = document.createElement('img');
      shot.className = 'shot';
      shot.alt = '';
      shot.decoding = 'async';
      shot.draggable = false;
      el.appendChild(shot);
      el.classList.add('rich');
      it._img = shot;
      it._linkShot = true;
    }
    var cap = document.createElement('span'); cap.className = 'cap';
    var host = document.createElement('span'); host.className = 'host';
    host.textContent = it.title || domain || 'link';
    var path = document.createElement('span'); path.className = 'path';
    if (it.title) path.textContent = it.site || domain;
    else {
      try {
        var u = new URL(it.url);
        path.textContent = decodeURI(u.pathname + u.search).replace(/^\//, '') || domain;
      } catch (e) { path.textContent = it.url || ''; }
    }
    cap.appendChild(host); cap.appendChild(path);
    el.appendChild(cap);
  } else if (it.t === 'file'){
    el.textContent = it.name || 'file';
  } else if (it.t === 'drawing'){
    el.title = 'freehand drawing (strokes not decoded)';
  }

  it._el = el;
  nodes.push(it);
  if (it.id) byId[it.id.toUpperCase()] = it;
  world.appendChild(el);
});

/* ---- view state -------------------------------------------------------- */

var view = { x: 0, y: 0, s: 1 };
var MIN = DATA.minScale, MAX = DATA.maxScale;
var pending = false;

/* Keep a sliver of the board on screen at all times. Without this an infinite
   canvas lets a single flick strand you in empty space with nothing to aim at. */
var KEEP = 110;

function clampView(){
  var b = bounds();
  if (!b) return false;
  var W = window.innerWidth, H = window.innerHeight, hit = false;
  var left = view.x + b.x1 * view.s, right = view.x + b.x2 * view.s;
  var top  = view.y + b.y1 * view.s, bottom = view.y + b.y2 * view.s;
  var keepX = Math.min(KEEP, (right - left) / 2);
  var keepY = Math.min(KEEP, (bottom - top) / 2);
  if (right < keepX){ view.x += keepX - right; hit = true; }
  else if (left > W - keepX){ view.x -= left - (W - keepX); hit = true; }
  if (bottom < keepY){ view.y += keepY - bottom; hit = true; }
  else if (top > H - keepY){ view.y -= top - (H - keepY); hit = true; }
  return hit;
}

function commit(){
  if (clampView() && glide){ vx = vy = 0; }
  world.style.transform = 'translate(' + view.x + 'px,' + view.y + 'px) scale(' + view.s + ')';
  document.getElementById('zoomPct').textContent =
    view.s >= 10 ? Math.round(view.s) + '\u00d7'
    : view.s >= 0.1 ? Math.round(view.s * 100) + '%'
    : (view.s * 100).toFixed(1) + '%';
  if (!pending){
    pending = true;
    requestAnimationFrame(function(){ pending = false; updateVisible(); });
  }
}

function clampScale(s){ return Math.min(MAX, Math.max(MIN, s)); }

function zoomAt(cx, cy, factor){
  var s2 = clampScale(view.s * factor);
  factor = s2 / view.s;
  view.x = cx - (cx - view.x) * factor;
  view.y = cy - (cy - view.y) * factor;
  view.s = s2;
  commit();
}

/* ---- lazy media -------------------------------------------------------- */
/* Boards can hold gigabytes of photos. Only the ones near the viewport get a
   src, and ones far outside give it back, so a phone never decodes the lot. */

var NEAR = 600, FAR = 2600;
/* Swap up to the original once an item is drawn large, and back down well
   before that, so nudging the zoom around a threshold cannot thrash. */
var FULL_IN = 380, FULL_OUT = 250;

function updateVisible(){
  var W = window.innerWidth, H = window.innerHeight;
  for (var i = 0; i < nodes.length; i++){
    var it = nodes[i];
    var media = it._img || it._vid;
    if (!media) continue;
    var only = it._linkShot ? it.prev : null;
    if (!it.src && !only) continue;

    var left   = view.x + it.x * view.s;
    var top    = view.y + it.y * view.s;
    var right  = left + (it.w || 0) * view.s;
    var bottom = top  + (it.h || 0) * view.s;

    var near = right > -NEAR && left < W + NEAR && bottom > -NEAR && top < H + NEAR;
    var far  = right < -FAR || left > W + FAR || bottom < -FAR || top > H + FAR;
    var playing = it._vid && !it._vid.paused;

    if (far && !playing){
      if (media.getAttribute('src')){
        media.removeAttribute('src');
        it._full = false;
        if (it._vid) it._vid.load();
      }
      continue;
    }
    if (!near) continue;

    /* A 24 MB photo drawn 60 px wide is pure waste, so the preview carries the
       zoomed-out view and the original arrives only once it can be seen. */
    var onScreen = Math.max(right - left, bottom - top);
    var wantFull = !it.prev || (it._full ? onScreen > FULL_OUT : onScreen >= FULL_IN);
    var desired;
    if (only){
      desired = only;              /* a link card's picture is the whole visual */
    } else if (it._vid){
      /* A video's still frame belongs in poster=, never in src. Putting an
         image path on a <video> makes it refuse the source outright. The file
         itself costs nothing to name here because preload is off: the browser
         fetches it only once someone presses play. */
      desired = it.src;
      wantFull = true;
    } else {
      desired = wantFull ? it.src : it.prev;
    }

    if (media.getAttribute('src') !== desired && !(playing && it._full)){
      media.setAttribute('src', desired);
      it._full = wantFull;
      if (it._vid) it._vid.load();
    }
  }
}

/* ---- fit --------------------------------------------------------------- */

var _bounds = null;
function bounds(){
  if (_bounds !== null) return _bounds;
  var b = null;
  nodes.forEach(function(it){
    var w = it.w || 240, h = it.h || (it.fs ? it.fs * 2 : 40);
    var x = it.w === undefined ? it.x - w / 2 : it.x;
    var y = it.h === undefined ? it.y - h / 2 : it.y;
    if (!b) b = { x1: x, y1: y, x2: x + w, y2: y + h };
    else {
      b.x1 = Math.min(b.x1, x); b.y1 = Math.min(b.y1, y);
      b.x2 = Math.max(b.x2, x + w); b.y2 = Math.max(b.y2, y + h);
    }
  });
  return b;
}

/* `floor` keeps the opening view readable. A long, thin board fits the screen
   only at a scale where nothing can be read, so on load we settle for showing
   the top-left of the content at a usable size; the zoom chip still fits the
   whole board on demand. */
function fit(animate, floor){
  var b = bounds();
  if (!b){ view = { x: 0, y: 0, s: 1 }; commit(); return; }
  var padX = 40, padTop = 96, padBottom = 96;
  var W = window.innerWidth, H = window.innerHeight;
  var bw = Math.max(b.x2 - b.x1, 1), bh = Math.max(b.y2 - b.y1, 1);
  var s = clampScale(Math.min((W - padX * 2) / bw, (H - padTop - padBottom) / bh));
  var target;
  if (floor && s < floor){
    s = Math.min(floor, 1);
    target = { s: s, x: padX - b.x1 * s, y: padTop - b.y1 * s };
  } else {
    target = {
      s: s,
      x: (W - bw * s) / 2 - b.x1 * s,
      y: (H - padTop - padBottom - bh * s) / 2 + padTop - b.y1 * s
    };
  }
  if (!animate){ view = target; commit(); return; }
  var from = { x: view.x, y: view.y, s: view.s };
  animateView(320, function(e){
    view.x = from.x + (target.x - from.x) * e;
    view.y = from.y + (target.y - from.y) * e;
    view.s = from.s + (target.s - from.s) * e;
  });
}

/* frame(e) sets the view for an eased progress e from 0 to 1. Touching the
   board stops the animation where it is, the same way it stops a glide. */
var tween = null;
function animateView(ms, frame){
  stopGlide();
  if (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) ms = 0;
  var t0 = performance.now();
  (function step(now){
    var k = ms > 0 ? Math.min(1, (now - t0) / ms) : 1;
    frame(1 - Math.pow(1 - k, 3));
    commit();
    tween = k < 1 ? requestAnimationFrame(step) : null;
  })(t0);
}

/* Move the view so board point (wx, wy) lands on screen point (sx, sy) at
   scale s. The point travels in a straight line across the screen while the
   zoom changes at a steady rate, so a long trip and a large change of scale
   both stay easy to follow. */
function flyTo(wx, wy, sx, sy, s){
  var s0 = view.s, x0 = view.x + wx * s0, y0 = view.y + wy * s0;
  animateView(600, function(e){
    view.s = s0 * Math.pow(s / s0, e);
    view.x = x0 + (sx - x0) * e - wx * view.s;
    view.y = y0 + (sy - y0) * e - wy * view.s;
  });
}

/* ---- gestures ---------------------------------------------------------- */

var pointers = new Map();
var last = null, pinch = null, moved = 0, origin = null, touchy = false;
var vx = 0, vy = 0, vt = 0, glide = null;

function stopGlide(){
  if (glide){ cancelAnimationFrame(glide); glide = null; }
  if (tween){ cancelAnimationFrame(tween); tween = null; }
}
function interacting(on){ stage.classList.toggle('interacting', on); }

stage.addEventListener('pointerdown', function(e){
  /* A selected video's player takes its own taps. */
  if (e.target.closest('video[controls]')) return;
  stopGlide();
  stage.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  moved = 0;
  interacting(true);
  if (pointers.size === 1){
    origin = { x: e.clientX, y: e.clientY };
    touchy = e.pointerType !== 'mouse';
    last = { x: e.clientX, y: e.clientY };
    stage.classList.add('dragging');
    vx = vy = 0; vt = performance.now();
  } else if (pointers.size === 2){
    pinch = snapshot();
    last = null;
  }
});

function snapshot(){
  var p = Array.from(pointers.values());
  var dx = p[1].x - p[0].x, dy = p[1].y - p[0].y;
  return {
    dist: Math.hypot(dx, dy) || 1,
    mx: (p[0].x + p[1].x) / 2,
    my: (p[0].y + p[1].y) / 2
  };
}

stage.addEventListener('pointermove', function(e){
  if (!pointers.has(e.pointerId)) return;
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

  if (pointers.size === 1 && last){
    var dx = e.clientX - last.x, dy = e.clientY - last.y;
    /* How far the finger ended up from where it started, not how far it
       wandered getting there — a shaky finger still counts as a tap. */
    if (origin) moved = Math.hypot(e.clientX - origin.x, e.clientY - origin.y);
    view.x += dx; view.y += dy;
    /* Smooth the velocity and cap it. A single sample taken over a very short
       interval can read as an enormous speed, which would fling the board out
       of sight on release. */
    var now = performance.now(), dt = Math.max(now - vt, 8);
    vx = vx * 0.7 + (dx / dt) * 0.3;
    vy = vy * 0.7 + (dy / dt) * 0.3;
    vt = now;
    last = { x: e.clientX, y: e.clientY };
    commit();
  } else if (pointers.size === 2 && pinch){
    var now2 = snapshot();
    moved = 999;                       /* a pinch is never a tap */
    var factor = now2.dist / pinch.dist;
    var s2 = clampScale(view.s * factor);
    factor = s2 / view.s;
    /* zoom about the midpoint, and follow the midpoint as it travels */
    view.x = now2.mx - (pinch.mx - view.x) * factor;
    view.y = now2.my - (pinch.my - view.y) * factor;
    view.s = s2;
    pinch = now2;
    commit();
  }
});

function release(e){
  if (!pointers.has(e.pointerId)) return;
  pointers.delete(e.pointerId);
  if (pointers.size === 1){ pinch = null; var p = Array.from(pointers.values())[0]; last = { x: p.x, y: p.y }; vx = vy = 0; }
  if (pointers.size === 0){
    stage.classList.remove('dragging');
    last = null; pinch = null;
    if (moved <= (touchy ? 14 : 6)) tap(e); else coast();
    if (Math.hypot(vx, vy) < 0.02) interacting(false);
  }
}
stage.addEventListener('pointerup', release);
stage.addEventListener('pointercancel', release);

var MAX_FLING = 3.0;   /* px per ms — about as fast as a real flick goes */

function coast(){
  var speed = Math.hypot(vx, vy);
  if (speed < 0.05){ interacting(false); return; }
  if (speed > MAX_FLING){ vx *= MAX_FLING / speed; vy *= MAX_FLING / speed; }
  (function step(){
    view.x += vx * 16; view.y += vy * 16;
    vx *= 0.94; vy *= 0.94;
    commit();
    if (Math.hypot(vx, vy) > 0.02) glide = requestAnimationFrame(step);
    else { glide = null; interacting(false); }
  })();
}

/* A physical wheel sends large, whole-number steps; a trackpad sends small
   fractional ones and often a sideways component. Zoom the first, pan the
   second, so both feel like what the hardware is for. */
stage.addEventListener('wheel', function(e){
  e.preventDefault();
  stopGlide();
  var wheelish = e.deltaX === 0 && Math.abs(e.deltaY) >= 40 &&
                 Number.isInteger(e.deltaY) && e.deltaMode === 0;
  if (e.ctrlKey || e.metaKey || e.deltaMode === 1 || wheelish){
    var d = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
    zoomAt(e.clientX, e.clientY, Math.exp(-d * 0.0022));
  } else {
    view.x -= e.deltaX; view.y -= e.deltaY;
    commit();
  }
}, { passive: false });

stage.addEventListener('dblclick', function(e){
  if (e.target.closest('a,video,.selectable')) return;
  zoomAt(e.clientX, e.clientY, 1.9);
});

window.addEventListener('resize', function(){ commit(); });
window.addEventListener('keydown', function(e){
  if (e.key === '0'){ fit(true); }
  else if (e.key === '+' || e.key === '=') zoomAt(innerWidth / 2, innerHeight / 2, 1.3);
  else if (e.key === '-') zoomAt(innerWidth / 2, innerHeight / 2, 1 / 1.3);
  else if (e.key === 'Escape') closeSheet();
});

document.getElementById('zoomIn').onclick  = function(){ zoomAt(innerWidth / 2, innerHeight / 2, 1.4); };
document.getElementById('zoomOut').onclick = function(){ zoomAt(innerWidth / 2, innerHeight / 2, 1 / 1.4); };
document.getElementById('zoomPct').onclick = function(){ fit(true); };

/* ---- selection and the card -------------------------------------------- */
/* Every item can be selected, and selecting one opens its card and names it
   in the address bar, board/#<id>. So the address always points at what is
   on screen, and Share has only to copy it. */

var sheet = document.getElementById('sheet');
var sheetDownload = document.getElementById('sheetDownload');
var sheetOpen = document.getElementById('sheetOpen');
var sheetShare = document.getElementById('sheetShare');
var selected = null;

function isVideo(it){ return it.t === 'movie' && !it.media; }

function closeSheet(){
  sheet.hidden = true;
  document.body.classList.remove('sheet-open');
  if (selected){
    selected._el.classList.remove('sel', 'selectable');
    if (isVideo(selected)) selected._vid.controls = false;
    selected = null;
  }
  setAddress(null);
}
document.getElementById('sheetClose').onclick = closeSheet;

function select(it){
  if (it === selected) return;
  closeSheet();
  selected = it;
  it._el.classList.add('sel');
  if (it.t === 'text') it._el.classList.add('selectable');
  if (isVideo(it)) it._vid.controls = true;

  var card = describe(it);
  document.getElementById('sheetName').textContent = card.name;
  document.getElementById('sheetInfo').textContent = card.info;
  var open = it.src || (it.t === 'link' && it.url) || '';
  sheetDownload.hidden = !it.src;
  if (it.src){
    sheetDownload.href = it.src;
    sheetDownload.setAttribute('download', it.name || '');
  }
  sheetOpen.hidden = !open;
  if (open) sheetOpen.href = open;
  sheetShare.hidden = !it.id;
  /* Whichever button comes first is the one drawn as the main action. */
  sheetOpen.classList.toggle('primary', !it.src);
  sheetShare.classList.toggle('primary', !open);
  resetShare();

  sheet.hidden = false;
  document.body.classList.add('sheet-open');
  setAddress(it.id);
}

function describe(it){
  if (it.src){
    var bits = [];
    if (it.bytes) bits.push(humanSize(it.bytes));
    if (it.w && it.h) bits.push(Math.round(it.w) + ' \u00d7 ' + Math.round(it.h) + ' pt');
    return { name: it.name || 'file', info: bits.join(' \u00b7 ') };
  }
  if (it.t === 'link'){
    return { name: it.title || hostOf(it.url) || 'Link',
             info: (it.url || '').replace(/^https?:\/\/(www\.)?/, '') };
  }
  if (it.t === 'text'){
    var line = (it.text || '').trim().split('\n')[0];
    if (line.length > 80) line = line.slice(0, 79) + '\u2026';
    return { name: line || 'Text', info: 'Text' };
  }
  if (it.t === 'drawing') return { name: 'Drawing', info: 'Freehand strokes are not exported' };
  return { name: it.name || it.t, info: 'Not included in this export' };
}

function tap(e){
  var el = document.elementFromPoint(e.clientX, e.clientY);
  var box = el && el.closest('.it');
  var it = box && nodes.find(function(n){ return n._el === box; });
  if (!it){ closeSheet(); return; }
  /* Text is selectable while it is selected; a second tap hands it back to
     panning. */
  if (it === selected && it.t === 'text'){ closeSheet(); return; }

  select(it);
  var media = it._img || it._vid;
  if (media && it.src && media.getAttribute('src') !== it.src){
    media.setAttribute('src', it.src);
    it._full = true;
  }
  if (el.closest('.play')){
    var started = it._vid.play();
    if (started) started.catch(function(){});
  }
}

/* A tap on a link card selects it like any other item, and the page is one
   press of Open away. A modified click, or Enter from the keyboard, still
   goes straight to it. */
world.addEventListener('click', function(e){
  if (e.detail && !(e.metaKey || e.ctrlKey || e.shiftKey) && e.target.closest('a.it')){
    e.preventDefault();
  }
});

function humanSize(n){
  var u = ['B', 'KB', 'MB', 'GB'], i = 0, v = n;
  while (v >= 1024 && i < u.length - 1){ v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i];
}

/* ---- links to items ---------------------------------------------------- */

function addressOf(id){
  return location.href.split('#')[0] + (id ? '#' + encodeURIComponent(id) : '');
}

/* replaceState, not pushState: tapping around a board should not leave a
   trail of entries for the back button to walk through. */
function setAddress(id){
  var url = addressOf(id);
  if (url === location.href) return;
  try { history.replaceState(null, '', url); } catch (e) {}
}

function addressedItem(){
  var id = '';
  try { id = decodeURIComponent(location.hash.slice(1)); } catch (e) {}
  return byId[id.trim().toUpperCase()] || null;
}

/* Open an item's card and bring the item to the middle of the screen, large
   enough to look at. The title bar and the card cover part of the screen, so
   the middle is the middle of what they leave. */
var FOCUS_MAX_SCALE = 2;

function focusItem(it){
  select(it);
  /* A page can load into a window that has no size yet, such as a tab still
     being laid out, and there is no middle to find until it has one. */
  if (!window.innerWidth || !window.innerHeight){
    window.addEventListener('resize', function(){ focusItem(it); }, { once: true });
    return;
  }
  var box = itemBox(it);
  var W = window.innerWidth, pad = 24;
  var top = document.getElementById('bar').offsetHeight;
  var bottom = window.innerHeight - sheet.offsetHeight;
  var s = clampScale(Math.min((W - pad * 2) / box.w,
                              Math.max(bottom - top - pad * 2, 40) / box.h,
                              FOCUS_MAX_SCALE));
  flyTo(box.cx, box.cy, W / 2, (top + bottom) / 2, s);
}

/* The rectangle an item covers on the board, rotation included. An axis
   Freeform sizes itself is anchored at its centre, as in the build above. */
function itemBox(it){
  var w = it._el.offsetWidth, h = it._el.offsetHeight;
  var cx = it.w === undefined ? it.x : it.x + w / 2;
  var cy = it.h === undefined ? it.y : it.y + h / 2;
  if (it.r){
    var c = Math.abs(Math.cos(it.r)), s = Math.abs(Math.sin(it.r));
    var rw = w * c + h * s;
    h = w * s + h * c; w = rw;
  }
  return { cx: cx, cy: cy, w: Math.max(w, 1), h: Math.max(h, 1) };
}

window.addEventListener('hashchange', function(){
  var it = addressedItem();
  if (it) focusItem(it);
  else if (!location.hash) closeSheet();
});

/* ---- share ------------------------------------------------------------- */

var shareTimer = 0;
function resetShare(){
  clearTimeout(shareTimer);
  sheetShare.textContent = 'Share';
}

sheetShare.onclick = function(){
  if (!selected) return;
  var url = addressOf(selected.id);
  copyText(url, function(ok){
    if (!ok){ window.prompt('Copy this link:', url); return; }
    sheetShare.textContent = 'Copied';
    clearTimeout(shareTimer);
    shareTimer = setTimeout(resetShare, 1600);
  });
};

/* The clipboard API exists only on https and localhost. A board served over
   plain http on a home network falls back to copying out of a hidden text
   field, which browsers still allow during a tap. */
function copyText(text, done){
  if (navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(
      function(){ done(true); },
      function(){ done(copyFromField(text)); });
  } else {
    done(copyFromField(text));
  }
}

function copyFromField(text){
  var field = document.createElement('textarea');
  field.value = text;
  field.setAttribute('readonly', '');
  /* 16px, or iOS zooms the page in on the field. */
  field.style.cssText = 'position:fixed;top:0;left:0;opacity:0;font-size:16px';
  document.body.appendChild(field);
  field.select();
  field.setSelectionRange(0, text.length);
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (e) {}
  field.remove();
  return ok;
}

/* ---- go ---------------------------------------------------------------- */

fit(false, 0.25);
var linked = addressedItem();
if (linked) focusItem(linked);
var hint = document.getElementById('hint');
setTimeout(function(){ hint.classList.add('gone'); }, 4200);
setTimeout(function(){ hint.remove(); }, 5200);
})();
"""


_INDEX_CSS = """
*{box-sizing:border-box}
:root{ --bg:#f5f5f7; --card:#fff; --ink:#14141a; --dim:#6b6b76;
       --line:rgba(0,0,0,.10); --accent:#0a68f0; }
@media (prefers-color-scheme:dark){
  :root{ --bg:#0f0f12; --card:#1b1b20; --ink:#f2f2f5; --dim:#9b9ba6;
         --line:rgba(255,255,255,.10); --accent:#4b9bff; }
}
html,body{margin:0}
body{
  background:var(--bg); color:var(--ink);
  font:16px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,sans-serif;
  padding:0 16px calc(env(safe-area-inset-bottom) + 40px);
  -webkit-text-size-adjust:100%;
}
header{max-width:900px; margin:0 auto; padding:calc(env(safe-area-inset-top) + 32px) 0 20px}
header h1{margin:0; font-size:26px; letter-spacing:-.02em}
header p{margin:6px 0 0; color:var(--dim); font-size:14px}
main{max-width:900px; margin:0 auto; display:grid; gap:10px;
     grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
.card{
  display:grid; gap:3px; padding:15px 16px; border-radius:14px;
  background:var(--card); border:1px solid var(--line);
  text-decoration:none; color:inherit;
}
.card:hover{border-color:var(--accent)}
.card:active{transform:scale(.99)}
.name{font-weight:600; letter-spacing:-.01em; display:flex; align-items:center; gap:8px}
.badge{font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.04em;
  color:var(--accent); border:1px solid currentColor; border-radius:5px; padding:1px 5px}
.sub{font-size:13px; color:var(--dim)}
.date{font-size:12px; color:var(--dim); opacity:.75}
"""
