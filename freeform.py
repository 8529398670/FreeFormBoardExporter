#!/usr/bin/env python3
"""
freeform.py — list, back up, and export Apple Freeform boards on macOS.

Freeform keeps every board in a private SQLite database inside its app
container, with attachments as content-addressed blobs beside it:

    ~/Library/Containers/com.apple.freeform/Data/Library/Freeform/Boards/
        boards.db        board rows, item rows, asset index
        Assets/          the original image / movie / file bytes
    ~/Library/Group Containers/group.com.apple.freeform/Snapshot.plist
                         the board list the app shows in its browser

Board titles, item geometry and text live in `crdt`-framed protobuf blobs.
This tool decodes the parts that matter for archiving: title, position, size,
rotation, text content, links, and the mapping from item to original file.

Nothing here writes to Freeform's own files. The database is snapshotted to a
temp directory before it is opened.

Requires only the Python 3 standard library.
"""

import argparse
import ctypes
import json
import os
import plistlib
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

try:
    import html_generator
except ImportError:  # the viewer is optional; exports still work without it
    html_generator = None

HOME = os.path.expanduser("~")
BOARDS_DIR = os.path.join(
    HOME, "Library/Containers/com.apple.freeform/Data/Library/Freeform/Boards"
)
SNAPSHOT_PLIST = os.path.join(
    HOME, "Library/Group Containers/group.com.apple.freeform/Snapshot.plist"
)
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# item_type values, confirmed against Freeform's own asset roles and the
# CRL*ItemData class names in the app binary. Types not present in any
# observed database are marked with "?" and still export with full geometry.
ITEM_TYPES = {
    1: "board",
    2: "container",
    3: "text",
    4: "sticky-note?",
    5: "image",
    6: "movie",
    7: "file",
    8: "link",
    9: "connection-line?",
    10: "drawing",
    11: "usdz?",
}

# Asset roles that are originals the user put on the board, as opposed to
# derived previews Freeform generated.
ORIGINAL_ROLES = ("image", "movie", "file")
DERIVED_ROLES = ("thumbnail", "posterImage", "linkMetadata")

# String-pool entries that are text styling keys, never filenames.
ATTR_NAMES = {
    "baseWritingDirection", "fontSize", "fontName", "paragraphStyle",
    "alignment", "lineSpacing", "underline", "strikethrough", "superscript",
    "kerning", "ligature", "foregroundColor", "backgroundColor",
}


# --------------------------------------------------------------------------
# protobuf / CRDT decoding
# --------------------------------------------------------------------------

def _varint(buf, i):
    result = shift = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise ValueError("bad varint")
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def fields(buf):
    """Decode a protobuf message into [(field_number, wire_type, value)].

    Returns whatever parsed cleanly; these blobs interleave real messages with
    opaque payloads, so a partial read is normal rather than an error.
    """
    i, out = 0, []
    while i < len(buf):
        try:
            key, i = _varint(buf, i)
            num, wire = key >> 3, key & 7
            if wire == 0:
                val, i = _varint(buf, i)
            elif wire == 1:
                if i + 8 > len(buf):
                    return out
                val, i = buf[i:i + 8], i + 8
            elif wire == 2:
                length, i = _varint(buf, i)
                if length > len(buf) - i:
                    return out
                val, i = buf[i:i + length], i + length
            elif wire == 5:
                if i + 4 > len(buf):
                    return out
                val, i = buf[i:i + 4], i + 4
            else:
                return out
            out.append((num, wire, val))
        except ValueError:
            return out
    return out


def _payload(blob):
    """Strip the 8-byte `crdt` + version header. None if not a CRDT blob."""
    if not blob or len(blob) < 8 or blob[:4] != b"crdt":
        return None
    return blob[8:]


def string_pool(blob):
    """Top-level field 6 is a pool of strings: titles, filenames, URLs, style keys."""
    body = _payload(blob)
    if body is None:
        return []
    pool = []
    for num, wire, val in fields(body):
        if num == 6 and wire == 2:
            for n2, w2, v2 in fields(val):
                if n2 == 2 and w2 == 2:
                    try:
                        pool.append(v2.decode("utf-8"))
                    except UnicodeDecodeError:
                        pass
    return pool


# --- geometry -------------------------------------------------------------
#
# Every item carries a geometry record whose slots are positional:
#   slot 0  position  {4:{1:f32 x, 2:f32 y}}
#   slot 1  size      {4:{1:f32 w, 2:f32 h}}
#   slot 2  rotation  {15:f32}
#   slot 3+ flags     {5:varint}
# A slot written empty means "default", and a slot may carry only x or only y
# (a text box with a fixed width and automatic height, for example).

def _slot_vec2(blob):
    fs = fields(blob)
    if len(fs) != 1 or fs[0][0] != 4 or fs[0][1] != 2:
        return None
    vec = {"x": None, "y": None}
    for num, wire, val in fields(fs[0][2]):
        if wire != 5 or num not in (1, 2):
            return None
        vec["x" if num == 1 else "y"] = struct.unpack("<f", val)[0]
    return vec


def _slot_rotation(blob):
    fs = fields(blob)
    if not fs:
        return 0.0
    if len(fs) == 1 and fs[0][0] == 15 and fs[0][1] == 5:
        return struct.unpack("<f", fs[0][2])[0]
    return None


def _geometry_record(blob):
    slots = [v for n, w, v in fields(blob) if n == 2 and w == 2]
    if len(slots) < 3:
        return None
    pos = _slot_vec2(slots[0])
    size = _slot_vec2(slots[1])
    rotation = _slot_rotation(slots[2])
    if pos is None or size is None or rotation is None:
        return None
    flags = []
    for slot in slots[3:]:
        fs = fields(slot)
        if len(fs) == 1 and fs[0][0] == 5 and fs[0][1] == 0:
            flags.append(fs[0][2])
    return {
        "x": pos["x"] or 0.0,
        "y": pos["y"] or 0.0,
        "width": size["x"],
        "height": size["y"],
        "rotation": rotation,
        "flags": flags,
    }


def _find_geometry(buf, depth=0):
    if depth > 40:
        return None
    record = _geometry_record(buf)
    if record:
        return record
    for num, wire, val in fields(buf):
        if wire == 2 and len(val) > 2:
            found = _find_geometry(val, depth + 1)
            if found:
                return found
    return None


def geometry(common_data):
    body = _payload(common_data)
    return _find_geometry(body) if body is not None else None


def content_rect(specific_data):
    """An image item's visible window, in the coordinates of its own frame.

    Cropping in Freeform does not touch the file and does not resize the item's
    frame. The frame keeps describing where the whole picture would sit, and a
    second rectangle — stored in the item's own data — says which part of it to
    actually show. An uncropped image carries this rectangle too, covering the
    frame exactly, so the two cases decode the same way.
    """
    body = _payload(specific_data)
    return _find_geometry(body) if body is not None else None


def place_on_board(items):
    """Move grouped items from their group's coordinates onto the board.

    A group is a container item, and it stores its children relative to its
    own origin — groups nest, so an item two groups deep is two offsets away
    from the board. The board's root container has no parent and sits at the
    origin, so its direct children are already in board coordinates.

    A group's size slot holds values like 1.0 or 1.72 rather than a size.
    Freeform does not apply them to what is inside: a grouped item keeps the
    size it is drawn at. Only the offset carries down.
    """
    by_id = {item["id"]: item for item in items}
    origins = {}

    def origin(container_id, seen=()):
        """Where a container's local (0, 0) lands on the board."""
        if container_id in origins:
            return origins[container_id]
        node = by_id.get(container_id)
        if (node is None or node["parent"] is None or not node["geometry"]
                or container_id in seen):
            point = (0.0, 0.0)
        else:
            px, py = origin(node["parent"], seen + (container_id,))
            point = (px + node["geometry"]["x"], py + node["geometry"]["y"])
        origins[container_id] = point
        return point

    # Resolve every offset before moving anything, since a nested group's own
    # position is itself one of the local coordinates being rewritten.
    shifts = [(item, origin(item["parent"])) for item in items
              if item["parent"] and item["geometry"]]
    for item, (dx, dy) in shifts:
        if dx or dy:
            item["geometry"] = dict(item["geometry"],
                                    x=item["geometry"]["x"] + dx,
                                    y=item["geometry"]["y"] + dy)


# --- text -----------------------------------------------------------------
#
# Text is a CRDT sequence: each inserted run appears as {5:{1:"chars"}}.
# Concatenating the runs in blob order reproduces the paragraph.

def _text_runs(buf, out, depth=0):
    if depth > 40:
        return
    for num, wire, val in fields(buf):
        if wire != 2 or not val:
            continue
        if num == 5:
            sub = fields(val)
            if sub and sub[0][0] == 1 and sub[0][1] == 2:
                try:
                    run = sub[0][2].decode("utf-8")
                except UnicodeDecodeError:
                    run = None
                if run is not None and all(
                    c in "\n\t" or ord(c) >= 0x20 for c in run
                ):
                    out.append(run)
                    continue
        if len(val) > 2:
            _text_runs(val, out, depth + 1)


def item_text(specific_data):
    body = _payload(specific_data)
    if body is None:
        return ""
    runs = []
    _text_runs(body, runs)
    return "".join(runs)


# --- text styling ---------------------------------------------------------
#
# Styled text is a list of runs. Each run is {6:{1:length, 2:{1:key, 2:value}}}
# where `key` indexes the item's string pool ("fontSize", "bold", ...) and
# `value` is a float, an integer, or a nested colour.

def _find_color(buf, depth=0):
    """A colour is three or four consecutive {15:f32} children, each in 0..1."""
    if depth > 25:
        return None
    parts = fields(buf)
    comps = []
    for num, wire, val in parts:
        if wire == 2 and len(val) == 5:
            sub = fields(val)
            if len(sub) == 1 and sub[0][0] == 15 and sub[0][1] == 5:
                comps.append(struct.unpack("<f", sub[0][2])[0])
    if len(comps) >= 3 and all(0.0 <= c <= 1.0 for c in comps[:4]):
        return tuple(comps[:4])
    for num, wire, val in parts:
        if wire == 2 and len(val) > 2:
            found = _find_color(val, depth + 1)
            if found:
                return found
    return None


def _css_color(comps):
    if not comps:
        return None
    r, g, b = (max(0, min(255, round(c * 255))) for c in comps[:3])
    if len(comps) >= 4 and comps[3] < 0.999:
        return f"rgba({r}, {g}, {b}, {comps[3]:.3f})"
    return f"#{r:02x}{g:02x}{b:02x}"


def _attr_value(node):
    """{1:key_index, 2:{typed value}} -> (key_index, value)"""
    key = value = None
    for num, wire, val in fields(node):
        if num == 1 and wire == 0:
            key = val
        elif num == 2 and wire == 2:
            for n2, w2, v2 in fields(val):
                if n2 == 15 and w2 == 5:
                    value = struct.unpack("<f", v2)[0]
                elif n2 == 5 and w2 == 0:
                    value = v2
            if value is None:
                value = _find_color(val)
    return key, value


def _style_runs(buf, out, depth=0):
    if depth > 40:
        return
    for num, wire, val in fields(buf):
        if wire != 2 or len(val) < 2:
            continue
        if num == 6:
            length, pairs = None, []
            for n2, w2, v2 in fields(val):
                if n2 == 1 and w2 == 0:
                    length = v2
                elif n2 == 2 and w2 == 2:
                    key, value = _attr_value(v2)
                    if key is not None and value is not None:
                        pairs.append((key, value))
            if pairs:
                out.append((length or 0, pairs))
        _style_runs(val, out, depth + 1)


def text_style(specific_data):
    """The style covering the most characters, as CSS-ready values.

    Freeform text can mix sizes and weights inside one box. Rather than guess
    at run boundaries, report the style that dominates the item so it renders
    at roughly the size it occupies on the board.
    """
    body = _payload(specific_data)
    if body is None:
        return {}
    pool = string_pool(specific_data)
    runs = []
    _style_runs(body, runs)
    if not runs:
        return {}

    weights = {}
    for length, pairs in runs:
        for key, value in pairs:
            if key >= len(pool):
                continue
            name = pool[key]
            if isinstance(value, tuple):
                value = _css_color(value)
            bucket = weights.setdefault(name, {})
            bucket[value] = bucket.get(value, 0) + max(length, 1)

    def dominant(name):
        bucket = weights.get(name)
        return max(bucket.items(), key=lambda kv: kv[1])[0] if bucket else None

    style = {}
    size = dominant("fontSize")
    if isinstance(size, (int, float)):
        style["font_size"] = round(float(size), 2)
    for flag in ("bold", "italic", "underline", "strikethrough"):
        if dominant(flag):
            style[flag] = True
    color = dominant("characterFill")
    if isinstance(color, str):
        style["color"] = color
    return style


def fill_color(specific_data):
    """An item's own fill colour — a shape's background, not its text colour.

    Text colours live inside style runs (field 6), so those are skipped.
    """
    body = _payload(specific_data)
    if body is None:
        return None

    def search(buf, depth=0):
        if depth > 30:
            return None
        parts = fields(buf)
        comps = []
        for num, wire, val in parts:
            if wire == 2 and len(val) == 5:
                sub = fields(val)
                if len(sub) == 1 and sub[0][0] == 15 and sub[0][1] == 5:
                    comps.append(struct.unpack("<f", sub[0][2])[0])
        if len(comps) >= 3 and all(0.0 <= c <= 1.0 for c in comps[:4]):
            return tuple(comps[:4])
        for num, wire, val in parts:
            if num == 6 or wire != 2 or len(val) <= 2:
                continue
            found = search(val, depth + 1)
            if found:
                return found
        return None

    return _css_color(search(body))


# --- link previews --------------------------------------------------------
#
# A link on a board carries an LPLinkMetadata archive: the page title, the site
# name, and the preview picture itself. Freeform draws that picture cropped to
# fill the card, which is why a link is a small image on the board rather than
# a line of text.

_MIME_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
    "image/gif": "gif", "image/webp": "webp", "image/heic": "heic",
    "image/tiff": "tiff",
}


def _unarchive(path):
    """Resolve an NSKeyedArchiver plist into plain dicts."""
    with open(path, "rb") as fh:
        archive = plistlib.load(fh)
    objects = archive.get("$objects")
    if objects is None:
        return None

    def resolve(value, depth=0):
        if depth > 20:
            return None
        if isinstance(value, plistlib.UID):
            target = objects[value.data]
            return None if target == "$null" else resolve(target, depth + 1)
        if isinstance(value, dict):
            return {k: resolve(v, depth + 1) for k, v in value.items() if k != "$class"}
        if isinstance(value, list):
            return [resolve(v, depth + 1) for v in value]
        return value

    return resolve(archive.get("$top"))


def _image_bytes(node):
    """LPLinkMetadata stores picture data either bare or wrapped in NS.data."""
    if not isinstance(node, dict):
        return None, None
    data = node.get("data")
    if isinstance(data, dict):
        data = data.get("NS.data")
    if not isinstance(data, bytes) or not data:
        return None, None
    return data, node.get("MIMEType")


def link_metadata(path):
    """{'title', 'site', 'image', 'ext'} for a link item's metadata asset."""
    try:
        root = (_unarchive(path) or {}).get("root") or {}
    except Exception:
        return {}
    out = {}
    for key in ("title", "originalTitle"):
        if isinstance(root.get(key), str) and root[key].strip():
            out["title"] = root[key].strip()
            break
    if isinstance(root.get("siteName"), str) and root["siteName"].strip():
        out["site"] = root["siteName"].strip()
    for key in ("image", "icon"):
        data, mime = _image_bytes(root.get(key))
        if data:
            out["image"] = data
            out["ext"] = _MIME_EXT.get((mime or "").lower(), "png")
            break
    return out


def _pool_candidates(specific_data):
    """String-pool entries that could be a filename or URL."""
    return [
        s for s in string_pool(specific_data)
        if len(s) > 1 and not s.startswith("com.apple.") and s not in ATTR_NAMES
    ]


def item_source_name(item_type, specific_data):
    """The original filename or URL the user sees on the board, if recorded."""
    names = _pool_candidates(specific_data)
    if not names:
        return None
    if item_type == 8:
        for name in names:
            if name.startswith(("http://", "https://")):
                return name
        return names[0]
    if item_type == 5:
        for name in names:
            stem, _ = os.path.splitext(name)
            if not stem.endswith("-small"):
                return name
    elif item_type == 6:
        for name in names:
            if not name.startswith("posterImage"):
                return name
    for name in names:
        if "." in name:
            return name
    return names[0]


# --------------------------------------------------------------------------
# reading the library
# --------------------------------------------------------------------------

class FreeformLibrary:
    """A read-only snapshot of the Freeform database and its asset store."""

    def __init__(self, boards_dir=BOARDS_DIR):
        self.boards_dir = boards_dir
        self.assets_dir = os.path.join(boards_dir, "Assets")
        self._tmp = None
        self._conn = None

    def __enter__(self):
        db = os.path.join(self.boards_dir, "boards.db")
        if not os.path.exists(db):
            raise SystemExit(
                f"No Freeform database at {db}\n"
                "Open Freeform once so it creates its library, then retry."
            )
        # Copy the database with its write-ahead log so we see committed data
        # without touching (or locking) the files Freeform is using.
        self._tmp = tempfile.mkdtemp(prefix="freeform-snapshot-")
        for suffix in ("", "-wal", "-shm"):
            src = db + suffix
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(self._tmp, "boards.db" + suffix))
        self._conn = sqlite3.connect(os.path.join(self._tmp, "boards.db"))
        self._conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, *exc):
        if self._conn:
            self._conn.close()
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)

    # -- snapshot plist ----------------------------------------------------

    @staticmethod
    def snapshot_titles():
        """Titles as shown in the app's board browser, keyed by board UUID."""
        titles = {}
        try:
            with open(SNAPSHOT_PLIST, "rb") as fh:
                tree = plistlib.load(fh)
        except (OSError, plistlib.InvalidFileException):
            return titles

        def walk(nodes):
            for node in nodes:
                item = node.get("item", {})
                board = item.get("board")
                if board:
                    key = board["boardIdentifier"]["storage"]["boardUUID"]
                    titles[key] = board.get("title")
                walk(node.get("children", []))

        walk(tree)
        return titles

    # -- boards ------------------------------------------------------------

    def boards(self, include_deleted=False):
        titles = self.snapshot_titles()
        sql = (
            "SELECT b.board_identifier, b.owner_name, b.data, b.last_activity_time,"
            "       b.tombstoned, b.ckshare_data IS NOT NULL AS is_shared,"
            "       m.is_favorite, m.view_state_data "
            "FROM boards b LEFT JOIN boards_metadata m"
            "  ON m.board_identifier = b.board_identifier"
        )
        if not include_deleted:
            sql += " WHERE b.tombstoned = 0"

        out = []
        for row in self._conn.execute(sql):
            raw = row["board_identifier"]
            # Boards shared by someone else append the owner id to the UUID.
            board_uuid = str(uuid.UUID(bytes=raw[:16])).upper()
            pool = string_pool(row["data"])
            title = pool[2] if len(pool) > 2 else None
            if not title:
                title = titles.get(board_uuid) or f"Untitled-{board_uuid[:8]}"

            viewport = None
            if row["view_state_data"]:
                try:
                    state = json.loads(row["view_state_data"])
                    viewport = {
                        "zoom": state.get("viewScale"),
                        "offset": state.get("contentOffset"),
                    }
                except (ValueError, TypeError):
                    pass

            out.append({
                "uuid": board_uuid,
                "title": title,
                "owner": row["owner_name"] or None,
                "shared": bool(row["is_shared"]),
                "owned_by_others": bool(row["owner_name"]),
                "favorite": bool(row["is_favorite"]),
                "deleted": bool(row["tombstoned"]),
                "modified": self._apple_time(row["last_activity_time"]),
                "viewport": viewport,
                "_raw_id": raw,
            })
        out.sort(key=lambda b: (b["title"] or "").lower())
        return out

    @staticmethod
    def _apple_time(seconds):
        if seconds is None:
            return None
        return (APPLE_EPOCH + timedelta(seconds=seconds)).isoformat()

    # -- items and assets --------------------------------------------------

    def items(self, raw_board_id):
        rows = self._conn.execute(
            "SELECT item_uuid, parent_uuid, item_type, common_data, specific_data,"
            "       tombstoned "
            "FROM board_items WHERE board_identifier = ? AND tombstoned = 0",
            (raw_board_id,),
        ).fetchall()

        assets = self.assets_for_board(raw_board_id)
        items = []
        for row in rows:
            item_id = str(uuid.UUID(bytes=row["item_uuid"][:16])).upper()
            item_type = row["item_type"]
            frame = geometry(row["common_data"])
            crop = None

            # Report where the item actually appears, and keep the mapping back
            # to the full picture so the crop can be reproduced.
            window = content_rect(row["specific_data"]) if frame else None
            if (window and frame and window["width"] is not None
                    and window["height"] is not None
                    and frame["width"] is not None
                    and frame["height"] is not None):
                shifted = abs(window["x"]) > 0.5 or abs(window["y"]) > 0.5
                shrunk = (abs(window["width"] - frame["width"]) > 0.5
                          or abs(window["height"] - frame["height"]) > 0.5)
                if shifted or shrunk:
                    crop = {
                        "offset_x": round(window["x"], 2),
                        "offset_y": round(window["y"], 2),
                        "full_width": round(frame["width"], 2),
                        "full_height": round(frame["height"], 2),
                    }
                    frame = dict(frame,
                                 x=frame["x"] + window["x"],
                                 y=frame["y"] + window["y"],
                                 width=window["width"],
                                 height=window["height"])

            entry = {
                "id": item_id,
                "parent": (
                    str(uuid.UUID(bytes=row["parent_uuid"][:16])).upper()
                    if row["parent_uuid"] else None
                ),
                "type": ITEM_TYPES.get(item_type, f"type-{item_type}"),
                "type_id": item_type,
                "geometry": frame,
                "assets": assets.get(row["item_uuid"], []),
            }
            if crop:
                entry["crop"] = crop
            if item_type == 3:
                text = item_text(row["specific_data"])
                if text:
                    entry["text"] = text
                style = text_style(row["specific_data"])
                if style:
                    entry["style"] = style
            fill = fill_color(row["specific_data"])
            if fill:
                entry["fill"] = fill
            source = item_source_name(item_type, row["specific_data"])
            if source:
                entry["url" if item_type == 8 else "source_name"] = source
            if item_type in (7, 8):
                meta = next((a for a in entry["assets"]
                             if a["role"] == "linkMetadata" and a["path"]
                             and os.path.exists(a["path"])), None)
                if meta:
                    info = link_metadata(meta["path"])
                    if info.get("title"):
                        entry["title"] = info["title"]
                    if info.get("site"):
                        entry["site"] = info["site"]
                    meta["has_image"] = bool(info.get("image"))
            items.append(entry)

        place_on_board(items)
        items.sort(key=lambda i: (
            i["geometry"]["y"] if i["geometry"] else 0.0,
            i["geometry"]["x"] if i["geometry"] else 0.0,
        ))
        return items

    def assets_for_board(self, raw_board_id):
        """item_uuid -> [{role, uuid, extension, path, size}]"""
        rows = self._conn.execute(
            "SELECT r.referrer_identifier, r.referrer_asset_name, r.asset_uuid,"
            "       a.extension "
            "FROM asset_references r "
            "LEFT JOIN assets a ON a.asset_uuid = r.asset_uuid "
            "WHERE r.board_identifier = ?",
            (raw_board_id,),
        ).fetchall()

        by_item = {}
        for row in rows:
            asset_id = str(uuid.UUID(bytes=row["asset_uuid"][:16])).upper()
            ext = row["extension"] or ""
            filename = asset_id + ("." + ext if ext else "")
            path = os.path.join(self.assets_dir, filename)
            by_item.setdefault(row["referrer_identifier"], []).append({
                "role": row["referrer_asset_name"],
                "uuid": asset_id,
                "extension": ext or None,
                "path": path,
                "size": os.path.getsize(path) if os.path.exists(path) else None,
            })
        return by_item

    def board_stats(self, raw_board_id):
        items = self._conn.execute(
            "SELECT COUNT(*) FROM board_items "
            "WHERE board_identifier = ? AND tombstoned = 0",
            (raw_board_id,),
        ).fetchone()[0]
        # One asset can appear on a board more than once; count it once,
        # matching what `export` actually writes out.
        seen = {}
        for asset_list in self.assets_for_board(raw_board_id).values():
            for asset in asset_list:
                if asset["role"] in ORIGINAL_ROLES and asset["size"]:
                    seen[asset["uuid"]] = asset["size"]
        return {"items": items, "files": len(seen), "bytes": sum(seen.values())}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def freeform_is_running():
    try:
        subprocess.run(
            ["pgrep", "-x", "Freeform"], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def human_size(num):
    if not num:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024


def safe_name(name, fallback="untitled"):
    """A filename that survives macOS, Linux and Windows."""
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", (name or "").strip())
    name = name.strip(". ")
    if name.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        name = "_" + name
    # Trim the stem, never the extension: a long title must not cost a file
    # its ".mp4", which is what tells Finder and a browser what it is.
    if len(name) > 120:
        stem, ext = os.path.splitext(name)
        if len(ext) > 12:
            ext = ""
        name = stem[:120 - len(ext)].rstrip() + ext
    return name or fallback


def unique_path(directory, name):
    base, ext = os.path.splitext(name)
    candidate, n = name, 2
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{base} ({n}){ext}"
        n += 1
    return os.path.join(directory, candidate)


def _load_clonefile():
    """clonefile(2): an APFS copy-on-write clone, instant and free of disk cost."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        fn = libc.clonefile
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        fn.restype = ctypes.c_int
        return fn
    except (OSError, AttributeError):
        return None


_CLONEFILE = _load_clonefile()
_clone_works = True


def copy_file(src, dst):
    """Clone on APFS when possible, otherwise copy the bytes."""
    global _clone_works
    if _CLONEFILE and _clone_works:
        if _CLONEFILE(os.fsencode(src), os.fsencode(dst), 0) == 0:
            return
        # EXDEV / unsupported filesystem: stop trying, copy for real.
        if ctypes.get_errno() in (18, 45, 78):  # EXDEV, EOPNOTSUPP, ENOSYS
            _clone_works = False
    shutil.copy2(src, dst)


def clone_available(directory):
    """True when files can be cloned into `directory` at no storage cost."""
    if not _CLONEFILE:
        return False
    probe_src = os.path.join(directory, ".freeform-clone-probe-src")
    probe_dst = os.path.join(directory, ".freeform-clone-probe-dst")
    try:
        with open(probe_src, "wb") as fh:
            fh.write(b"probe")
        return _CLONEFILE(os.fsencode(probe_src), os.fsencode(probe_dst), 0) == 0
    except OSError:
        return False
    finally:
        for path in (probe_src, probe_dst):
            try:
                os.unlink(path)
            except OSError:
                pass


def dir_size(path):
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass
    return total


def select_boards(boards, patterns):
    """Match by UUID (full or prefix) or by case-insensitive title substring."""
    if not patterns:
        return boards
    chosen, missing = [], []
    for pattern in patterns:
        needle = pattern.lower()
        hits = [
            b for b in boards
            if b["uuid"].lower().startswith(needle)
            or needle in (b["title"] or "").lower()
        ]
        if not hits:
            missing.append(pattern)
        for hit in hits:
            if hit not in chosen:
                chosen.append(hit)
    if missing:
        raise SystemExit("No board matched: " + ", ".join(missing))
    return chosen


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_list(args):
    with FreeformLibrary() as lib:
        boards = lib.boards(include_deleted=args.include_deleted)
        for board in boards:
            board.update(lib.board_stats(board["_raw_id"]))

        if args.json:
            for board in boards:
                board.pop("_raw_id", None)
            print(json.dumps(boards, indent=2, ensure_ascii=False))
            return

        if not boards:
            print("No boards found.")
            return

        width = max(len(b["title"]) for b in boards)
        width = min(max(width, 5), 44)
        print(f"{'TITLE'.ljust(width)}  {'MODIFIED':10}  {'ITEMS':>5}  "
              f"{'FILES':>5}  {'SIZE':>9}  FLAGS")
        print("-" * (width + 46))
        total_bytes = total_files = 0
        for board in boards:
            flags = "".join([
                "S" if board["shared"] else "",
                "O" if board["owned_by_others"] else "",
                "*" if board["favorite"] else "",
                "D" if board["deleted"] else "",
            ])
            title = board["title"]
            title = title if len(title) <= width else title[:width - 1] + "…"
            modified = (board["modified"] or "")[:10]
            print(f"{title.ljust(width)}  {modified:10}  {board['items']:>5}  "
                  f"{board['files']:>5}  {human_size(board['bytes']):>9}  {flags}")
            total_bytes += board["bytes"]
            total_files += board["files"]
        print("-" * (width + 46))
        print(f"{len(boards)} boards, {total_files} original files, "
              f"{human_size(total_bytes)}")
        print("\nFlags: S=shared  O=owned by someone else  *=favorite  D=deleted")
        print("Board ids (for --board) are shown by: freeform.py list --json")


def cmd_backup(args):
    dest = os.path.abspath(args.dest)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    root = os.path.join(dest, f"freeform-backup-{stamp}")

    if freeform_is_running():
        print("Note: Freeform is running. Quit it for a guaranteed-consistent "
              "backup; boards edited during the copy may be captured mid-write.",
              file=sys.stderr)

    os.makedirs(root, exist_ok=True)
    print(f"Backing up to {root}")

    # On the same APFS volume a backup is a clone: instant, and no extra space.
    # Anywhere else it is a real copy, so make sure the bytes will fit.
    needed = 0 if args.no_assets else dir_size(os.path.join(BOARDS_DIR, "Assets"))
    if not clone_available(root):
        free = shutil.disk_usage(root).free
        if args.zip:
            needed = int(needed * 1.05)  # the archive is built beside the tree
        if needed > free * 0.95:
            raise SystemExit(
                f"Not enough space at {root}\n"
                f"  needs about {human_size(needed)}, {human_size(free)} free.\n"
                "  Back up to a location on this Mac's own drive to use "
                "instant APFS clones, or pass --no-assets."
            )
        if needed:
            print(f"  copying {human_size(needed)} of attachments "
                  "(destination is on another volume)")
    elif not args.no_assets:
        print("  using APFS clones — no extra disk space consumed")

    for name in ("boards.db", "boards.db-wal", "boards.db-shm",
                 "side.db", "side.db-wal", "side.db-shm"):
        src = os.path.join(BOARDS_DIR, name)
        if os.path.exists(src):
            copy_file(src, os.path.join(root, name))
    print("  database    copied")

    if os.path.exists(SNAPSHOT_PLIST):
        copy_file(SNAPSHOT_PLIST, os.path.join(root, "Snapshot.plist"))
        print("  board index copied")

    copied = total = 0
    if not args.no_assets:
        src_assets = os.path.join(BOARDS_DIR, "Assets")
        dst_assets = os.path.join(root, "Assets")
        os.makedirs(dst_assets, exist_ok=True)
        names = sorted(os.listdir(src_assets)) if os.path.isdir(src_assets) else []
        for i, name in enumerate(names, 1):
            src = os.path.join(src_assets, name)
            if not os.path.isfile(src):
                continue
            copy_file(src, os.path.join(dst_assets, name))
            copied += 1
            total += os.path.getsize(src)
            if i % 250 == 0 or i == len(names):
                print(f"\r  assets      {i}/{len(names)}", end="", flush=True)
        print(f"\r  assets      {copied} files, {human_size(total)}      ")

    with FreeformLibrary() as lib:
        boards = lib.boards(include_deleted=True)
        manifest = []
        for board in boards:
            stats = lib.board_stats(board.pop("_raw_id"))
            board.update(stats)
            manifest.append(board)
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "created": datetime.now(timezone.utc).isoformat(),
            "source": BOARDS_DIR,
            "boards": manifest,
        }, fh, indent=2, ensure_ascii=False)
    print(f"  manifest    {len(manifest)} boards")

    if args.zip:
        archive = root + ".zip"
        print(f"Compressing to {archive} (this takes a while)")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for folder, _, files in os.walk(root):
                for name in files:
                    full = os.path.join(folder, name)
                    zf.write(full, os.path.relpath(full, os.path.dirname(root)))
        shutil.rmtree(root)
        print(f"Done: {archive} ({human_size(os.path.getsize(archive))})")
    else:
        print(f"Done: {root}")
        print("Restore: quit Freeform, then copy boards.db*, side.db* and "
              f"Assets/ back into\n  {BOARDS_DIR}")


def cmd_export(args):
    dest = os.path.abspath(args.dest)
    os.makedirs(dest, exist_ok=True)

    with FreeformLibrary() as lib:
        boards = select_boards(lib.boards(include_deleted=args.include_deleted),
                               args.board)
        if not boards:
            print("No boards to export.")
            return

        used = set()
        pages = []
        for board in boards:
            folder_name = safe_name(board["title"], board["uuid"][:8])
            if folder_name.lower() in used:
                folder_name = f"{folder_name} {board['uuid'][:8]}"
            used.add(folder_name.lower())
            board_dir = os.path.join(dest, folder_name)
            os.makedirs(board_dir, exist_ok=True)

            items = lib.items(board["_raw_id"])
            exported = _export_files(
                lib, board_dir, items,
                want_previews=not (args.no_previews or args.no_html),
                skip_files=args.no_files,
            )
            _write_layout(board_dir, board, items)
            _write_outline(board_dir, board, items)

            want_viewer = html_generator is not None and not args.no_html
            if want_viewer:
                stats = lib.board_stats(board["_raw_id"])
                with open(os.path.join(board_dir, "index.html"), "w",
                          encoding="utf-8") as fh:
                    fh.write(html_generator.board_html(
                        board, items, has_index=len(boards) > 1))
                pages.append({
                    "title": board["title"],
                    "folder": folder_name,
                    "items": len([i for i in items if i["type"] != "container"]),
                    "files": stats["files"],
                    "bytes": stats["bytes"],
                    "modified": board["modified"],
                    "shared": board["shared"],
                })

            print(f"{board['title']}  —  {len(items)} items, "
                  f"{exported} files  ->  {os.path.relpath(board_dir, dest)}")

    if pages and len(pages) > 1:
        with open(os.path.join(dest, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(html_generator.index_html(pages))

    print(f"\nExported {len(boards)} board(s) to {dest}")
    if pages:
        entry = "index.html" if len(pages) > 1 else os.path.join(
            pages[0]["folder"], "index.html")
        print(f"Open the viewer:  open '{os.path.join(dest, entry)}'")
    elif html_generator is None and not args.no_html:
        print("Note: html_generator.py was not found next to freeform.py, "
              "so no viewer was written.", file=sys.stderr)


def _export_files(lib, board_dir, items, want_previews, skip_files):
    """Copy originals into files/, and Freeform's small previews into previews/.

    The two are kept apart on purpose: files/ is meant to be the folder of
    things the user actually put on the board, under the names they had. The
    previews exist only so the viewer can draw a huge board quickly, and they
    are a fraction of the size — a gigabyte of photos has about 30 MB of them.
    """
    if skip_files:
        return 0
    files_dir = os.path.join(board_dir, "files")
    preview_dir = os.path.join(board_dir, "previews")
    written = 0
    seen = {}

    for item in items:
        for asset in item["assets"]:
            original = asset["role"] in ORIGINAL_ROLES
            if not original and not (want_previews and asset["role"] in DERIVED_ROLES):
                continue
            if not asset["path"] or not os.path.exists(asset["path"]):
                asset["exported"] = None
                continue
            if asset["uuid"] in seen:
                asset["exported"] = seen[asset["uuid"]]
                continue

            ext = "." + asset["extension"] if asset["extension"] else ""

            # A link's metadata archive is mostly the preview picture. Unpack
            # that picture rather than copying the archive: it is what the
            # viewer draws, and the blobs run to hundreds of megabytes.
            if asset["role"] == "linkMetadata":
                info = link_metadata(asset["path"])
                if not info.get("image"):
                    asset["exported"] = None
                    continue
                os.makedirs(preview_dir, exist_ok=True)
                target = unique_path(preview_dir,
                                     f"{asset['uuid']}.{info['ext']}")
                with open(target, "wb") as fh:
                    fh.write(info["image"])
                rel = os.path.relpath(target, board_dir)
                asset["exported"] = rel
                seen[asset["uuid"]] = rel
                continue

            if original:
                folder = files_dir
                name = item.get("source_name") or (asset["uuid"] + ext)
                # Keep the real extension even when the recorded name lacks one.
                if ext and not os.path.splitext(name)[1]:
                    name += ext
            else:
                folder = preview_dir
                name = asset["uuid"] + ext

            os.makedirs(folder, exist_ok=True)
            target = unique_path(folder, safe_name(name, asset["uuid"]))
            copy_file(asset["path"], target)
            rel = os.path.relpath(target, board_dir)
            asset["exported"] = rel
            seen[asset["uuid"]] = rel
            written += original
    return written


def _write_layout(board_dir, board, items):
    payload = {
        "board": {
            "title": board["title"],
            "uuid": board["uuid"],
            "modified": board["modified"],
            "shared": board["shared"],
            "owner": board["owner"],
            "owned_by_others": board["owned_by_others"],
            "favorite": board["favorite"],
            "viewport": board["viewport"],
        },
        "coordinate_system": {
            "units": "points",
            "origin": "top-left; x grows right, y grows down",
            "note": ("width/height are null when Freeform sizes the item "
                     "automatically. rotation is in radians. geometry is the "
                     "rectangle the item occupies on the board, for grouped "
                     "items as well; parent names the group."),
            "crop": ("Present on a cropped image. Draw the file at "
                     "full_width x full_height, offset by minus offset_x and "
                     "minus offset_y, clipped to geometry."),
        },
        "item_count": len(items),
        "items": items,
    }
    path = os.path.join(board_dir, "layout.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def _write_outline(board_dir, board, items):
    """A readable companion to layout.json: text, links and files in place."""
    lines = [f"# {board['title']}", ""]
    if board["modified"]:
        lines.append(f"Last modified: {board['modified'][:19].replace('T', ' ')}")
    lines.append(f"Board id: {board['uuid']}")
    if board["shared"]:
        lines.append("Shared board")
    lines += [f"Items: {len(items)}", "", "---", ""]

    for item in items:
        geo = item["geometry"]
        where = f"({geo['x']:.0f}, {geo['y']:.0f})" if geo else "(position unknown)"
        kind = item["type"]

        if item.get("text"):
            lines.append(f"## {kind} {where}")
            lines.append("")
            lines.append(item["text"].rstrip())
        elif item.get("url"):
            lines.append(f"## link {where}")
            lines.append("")
            lines.append(item["url"])
        else:
            name = item.get("source_name")
            exported = next(
                (a.get("exported") for a in item["assets"]
                 if a["role"] in ORIGINAL_ROLES and a.get("exported")), None
            )
            if not name and not exported:
                continue
            lines.append(f"## {kind} {where}")
            lines.append("")
            if exported:
                label = (name or os.path.basename(exported)).replace("]", "\\]")
                href = urllib.parse.quote(exported)
                lines.append(f"[{label}]({href})")
            else:
                lines.append(name)
        lines.append("")

    with open(os.path.join(board_dir, "board.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")


def cmd_info(args):
    with FreeformLibrary() as lib:
        boards = select_boards(lib.boards(include_deleted=True), args.board)
        for board in boards:
            items = lib.items(board["_raw_id"])
            stats = lib.board_stats(board["_raw_id"])
            print(f"\n{board['title']}")
            print(f"  id         {board['uuid']}")
            print(f"  modified   {board['modified']}")
            print(f"  shared     {board['shared']}"
                  + (f"  (owned by {board['owner']})" if board["owner"] else ""))
            if board["viewport"]:
                vp = board["viewport"]
                print(f"  viewport   zoom {vp['zoom']}  offset {vp['offset']}")
            print(f"  items      {stats['items']}")
            print(f"  files      {stats['files']}  ({human_size(stats['bytes'])})")

            counts = {}
            for item in items:
                counts[item["type"]] = counts.get(item["type"], 0) + 1
            if counts:
                print("  breakdown  " + ", ".join(
                    f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])
                ))
            extents = [i["geometry"] for i in items if i["geometry"]]
            if extents:
                xs = [g["x"] for g in extents]
                ys = [g["y"] for g in extents]
                print(f"  extent     x {min(xs):.0f}..{max(xs):.0f}   "
                      f"y {min(ys):.0f}..{max(ys):.0f}")


def main():
    parser = argparse.ArgumentParser(
        prog="freeform.py",
        description="List, back up and export Apple Freeform boards.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  freeform.py list
  freeform.py info MCAT
  freeform.py backup ~/Backups
  freeform.py backup ~/Backups --zip
  freeform.py export ~/Desktop/boards
  freeform.py export ~/Desktop/boards --board MCAT --board Logic
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list every board")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--include-deleted", action="store_true",
                   help="also show boards in Recently Deleted")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("info", help="show detail for one or more boards")
    p.add_argument("board", nargs="+", help="board id or part of its title")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("backup", help="copy the whole library verbatim")
    p.add_argument("dest", help="directory to back up into")
    p.add_argument("--zip", action="store_true", help="compress into a .zip")
    p.add_argument("--no-assets", action="store_true",
                   help="database only, skip the attachment store")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("export", help="export boards as files plus layout data")
    p.add_argument("dest", help="directory to export into")
    p.add_argument("--board", action="append", default=[],
                   help="board id or part of its title (repeatable; "
                        "default is every board)")
    p.add_argument("--no-previews", action="store_true",
                   help="skip Freeform's small preview images; the viewer then "
                        "loads full-size originals at every zoom level")
    p.add_argument("--no-files", action="store_true",
                   help="layout data only, do not copy originals")
    p.add_argument("--include-deleted", action="store_true",
                   help="also export boards in Recently Deleted")
    p.add_argument("--no-html", action="store_true",
                   help="skip the pan-and-zoom HTML viewer")
    p.set_defaults(func=cmd_export)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
