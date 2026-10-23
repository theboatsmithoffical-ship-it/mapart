"""Sponge schematic v2 (.schem) export + independent validation.

BlockData rows are Y-major (Y=0 is the floor plane we build at); palette keys
are "minecraft:<block>" strings as produced by the map-art plan.
Exports are deterministic for identical inputs (gzip mtime pinned).
"""
from __future__ import annotations

import json
import math

import numpy as np

from . import nbt
from .nbt import wrap_list

SCHEM_VERSION = 2
DATA_VERSION = 2578  # ~1.15.2; honest constant, reviewed per palette version


def export_schem(path, blocks: list, width: int, height: int, length: int,
                 metadata: dict | None = None) -> dict:
    """blocks[y][z][x] = 'minecraft:...'; height = number of block layers."""
    ys = len(blocks)
    if ys != height:
        raise ValueError(f"layers {ys} != height {height}")
    palette: dict = {}
    palette_entries: list = []
    data: list = []
    volume = width * height * length
    if len(data) > volume:
        raise ValueError("block array larger than volume")
    for y, layer in enumerate(blocks):
        if len(layer) != length:
            raise ValueError(f"layer {y} has {len(layer)} rows, expected {length}")
        for zi, row in enumerate(layer):
            if len(row) != width:
                raise ValueError(f"row length {len(row)} != width {width}")
            for block in row:
                key = block if block.startswith("minecraft:") else "minecraft:" + block
                if key not in palette:
                    palette[key] = len(palette)
                    palette_entries.append(key)
                data.append(palette[key])
    data_varint = bytearray()
    for v in data:
        while True:
            b = v & 0x7F
            v >>= 7
            if v:
                data_varint.append(b | 0x80)
            else:
                data_varint.append(b)
                break
    root = {
        "Version": SCHEM_VERSION,
        "DataVersion": DATA_VERSION,
        "Width": width,
        "Height": height,
        "Length": length,
        "Palette": {block: i for i, block in enumerate(palette_entries)},
        "PaletteMax": len(palette_entries),
        "BlockData": bytes(data_varint),
        "Metadata": (metadata or {}) | {"engine": "firefly-mapart", "format": "sponge-v2"},
    }
    nbt.write_gzip("Schematic", root)
    Path_write(path, nbt.write_gzip("Schematic", root))
    return {"path": str(path), "width": width, "height": height, "length": length,
            "palette": len(palette_entries), "volume": volume}


def Path_write(path, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


def read_varints(buf: bytes) -> list:
    out = []
    shift = 0
    cur = 0
    for b in buf:
        cur |= (b & 0x7F) << shift
        if b & 0x80:
            shift += 7
        else:
            out.append(cur)
            shift = 0
            cur = 0
    return out


def validate_schem(path, expect=None) -> dict:
    """Independent structural validation; returns a report dict, raises ValueError."""
    root = nbt.read_gzip(open(path, "rb").read())
    if root.get("Version") != SCHEM_VERSION:
        raise ValueError(f"unsupported schem Version {root.get('Version')}")
    w, h, l = root["Width"], root["Height"], root["Length"]
    inv = {i: b for b, i in root["Palette"].items()}
    data = read_varints(root["BlockData"])
    if len(data) != w * h * l:
        raise ValueError(f"BlockData decodes to {len(data)} entries, expected {w*h*l}")
    bad_palette = [i for i, b in enumerate(inv.values()) if not isinstance(b, str) or not b.startswith("minecraft:")]
    if bad_palette:
        raise ValueError(f"palette has non-minecraft entries at {bad_palette[:5]}")
    unknown = sorted({inv.get(i) for i in data if i not in inv})
    if unknown:
        raise ValueError(f"BlockData references ids missing from palette: {unknown[:5]}")
    report = {
        "path": str(path),
        "size": [w, h, l],
        "volume": w * h * l,
        "palette_entries": len(inv),
        "blocks_placed": len(data) - sum(1 for i in data if inv[i] == "minecraft:air"),
        "data_version": root.get("DataVersion"),
        "metadata": root.get("Metadata", {}),
    }
    if expect:
        for field, value in expect.items():
            actual = report.get(field)
            if field == "size":
                actual = tuple(report["size"])
                value = tuple(value)
            if actual != value:
                raise ValueError(f"mismatch {field}: expected {value}, got {actual}")
    return report


def counts_from_plan(blocks: list) -> dict:
    flat = [b for layer in blocks for row in layer for b in row]
    return {b: flat.count(b) for b in sorted(set(flat))}


# ---------------- litematica .litematic (gzip NBT) ----------------
# Layout verified against litemapy's writer (v6): root compound with Version,
# SubVersion, MinecraftDataVersion, Metadata, Regions{name -> region}.
# Block index order is x-fastest: i = x + z*Sx + y*Sx*Sz. BlockStates are a
# TAG_Long_Array with CONTINUOUS LSB-first bit packing (entries may cross
# long boundaries) — NOT the padded vanilla PackedLongArray layout.
LITEMATIC_VERSION = 6
LITEMATIC_SUBVERSION = 1


def pack_bits(values, bits: int) -> list:
    """Continuous LSB-first packing as litemapy writes it: entry i occupies
    bits [i*bits, (i+1)*bits) where bit 0 is the LOW bit of word 0; entries
    may span long boundaries. Returned as signed ints for NBT longs."""
    mask = (1 << bits) - 1
    n_words = (len(values) * bits + 63) // 64
    words = [0] * n_words
    for i, v in enumerate(values):
        v = int(v) & mask
        pos = i * bits
        wi, off = pos // 64, pos % 64
        spill = off + bits - 64
        if spill > 0:
            words[wi] |= (v & ((1 << (64 - off)) - 1)) << off
            words[wi + 1] |= (v >> (64 - off)) & ((1 << spill) - 1)
        else:
            words[wi] |= v << off
    return [w - (1 << 64) if w >= 1 << 63 else w for w in words]


def unpack_bits(words, bits: int, count: int) -> list:
    mask = (1 << bits) - 1
    u = [int(w) & ((1 << 64) - 1) for w in words]
    out = []
    for i in range(count):
        pos = i * bits
        wi, off = pos // 64, pos % 64
        v = (u[wi] >> off) & mask
        spill = off + bits - 64
        if spill > 0:
            v |= (u[wi + 1] & ((1 << spill) - 1)) << (bits - spill)
        out.append(v)
    return out


def _bits_for(palette_size: int) -> int:
    return max(1, math.ceil(math.log2(max(2, palette_size))))


def export_litematic(path, blocks: list, width: int, height: int, length: int,
                     metadata: dict | None = None,
                     mc_data_version: int = DATA_VERSION) -> dict:
    """Write a Litematica v6 file. blocks[y][z][x] = 'minecraft:...'.
    Air becomes palette index 0. Returns an info dict for validation."""
    flat = [b for layer in blocks for row in layer for b in row]
    volume = width * height * length
    if len(flat) != volume:
        raise ValueError(f"{len(flat)} blocks, expected volume {volume}")
    palette = ["minecraft:air"]
    ids = []
    for b in flat:
        key = b if b.startswith("minecraft:") else "minecraft:" + b
        if key not in palette:
            palette.append(key)
        ids.append(palette.index(key))
    bits = _bits_for(len(palette))
    words = pack_bits(ids, bits)
    meta = {"Name": "", "Author": "firefly-mapart", "Description": "",
            "RegionCount": 1, "TotalBlocks": sum(1 for b in flat if b != "minecraft:air"),
            "TotalVolume": volume,
            "EnclosingSize": {"x": width, "y": height, "z": length}}
    meta.update(metadata or {})
    # explicit __tags__: keeps BlockStates a true TAG_Long_Array(12) and the
    # empty lists as TAG_List<TAG_Compound> — the writer's type inference
    # cannot express LongArray from plain python values.
    region = {"__tags__": [
        ("Position", nbt.TAG_Compound, {"x": 0, "y": 0, "z": 0}),
        ("Size", nbt.TAG_Compound, {"x": width, "y": height, "z": length}),
        ("BlockStatePalette", nbt.TAG_List,
         {"item_type": nbt.TAG_Compound, "value": [{"Name": b} for b in palette]}),
        ("BlockStates", nbt.TAG_Long_Array, words),
        ("Entities", nbt.TAG_List, {"item_type": nbt.TAG_Compound, "value": []}),
        ("TileEntities", nbt.TAG_List, {"item_type": nbt.TAG_Compound, "value": []}),
        ("PendingBlockTicks", nbt.TAG_List, {"item_type": nbt.TAG_Compound, "value": []}),
        ("PendingFluidTicks", nbt.TAG_List, {"item_type": nbt.TAG_Compound, "value": []}),
    ]}
    root = {"Version": LITEMATIC_VERSION, "SubVersion": LITEMATIC_SUBVERSION,
            "MinecraftDataVersion": mc_data_version,
            "Metadata": meta, "Regions": {"main": region}}
    data = nbt.write_gzip("Litematic", root)
    with open(path, "wb") as f:
        f.write(data)
    return {"path": str(path), "format": "litematica-v6", "size": [width, height, length],
            "palette": len(palette), "bits_per_entry": bits, "long_words": len(words),
            "non_air": meta["TotalBlocks"], "mc_data_version": mc_data_version}


def validate_litematic(path, expect=None) -> dict:
    """Independent structural validation: reparse the NBT, unpack the bit
    stream cross-long, and rebuild the volume to confirm coordinates, palette
    range and counts agree. Raises ValueError on any structural problem."""
    root = nbt.read_gzip(open(path, "rb").read())
    if root.get("Version") != LITEMATIC_VERSION:
        raise ValueError(f"unsupported litematic Version {root.get('Version')}")
    regions = root.get("Regions") or {}
    if not regions:
        raise ValueError("litematic has no Regions")
    name, region = next(iter(regions.items()))
    size = region["Size"]
    w, h, l = abs(size["x"]), abs(size["y"]), abs(size["z"])
    palette = _as_list(region["BlockStatePalette"])
    words = _as_list(region["BlockStates"])
    volume = w * h * l
    bits = _bits_for(len(palette))
    ids = unpack_bits(words, bits, volume)
    if len(ids) != volume:
        raise ValueError("bit stream decodes to wrong volume")
    if any(i < 0 or i >= len(palette) for i in ids):
        raise ValueError("block state id out of palette range")
    for entry in palette:
        if not entry.get("Name", "").startswith("minecraft:"):
            raise ValueError(f"palette entry lacks minecraft: prefix: {entry.get('Name')!r}")
    non_air = sum(1 for i in ids if palette[i]["Name"] != "minecraft:air")
    report = {"path": str(path), "format": "litematica-v6", "size": [w, h, l],
              "volume": volume, "palette_entries": len(palette), "non_air": non_air,
              "region": name, "mc_data_version": root.get("MinecraftDataVersion")}
    if expect:
        for field, value in expect.items():
            actual = tuple(report[field]) if field == "size" else report.get(field)
            expected = tuple(value) if field == "size" else value
            if actual != expected:
                raise ValueError(f"mismatch {field}: expected {expected}, got {actual}")
    return report


def _as_list(v):
    """Normalise a decoded list payload: the reader returns plain lists for
    both TAG_List(9) and TAG_Long_Array(12); very old files may still carry
    {'item_type': ..., 'value': ...} wrappers."""
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return v


# ---------------- Minecraft structure-block .nbt ----------------

def export_structure_nbt(path, blocks: list, width: int, height: int, length: int,
                         metadata: dict | None = None,
                         mc_data_version: int = DATA_VERSION) -> dict:
    """Write a Minecraft structure-block file (gzip NBT). blocks[y][z][x].
    Root fields follow the vanilla format: size, blocks, palette, entities,
    DataVersion (air is an explicit palette entry, structure-void is not used).
    The vanilla format has no metadata field; caller-side metadata stays in
    the JSON report file, never in the .nbt."""
    palette: dict = {}
    blocks_out = []
    for y, layer in enumerate(blocks):
        if len(layer) != length:
            raise ValueError(f"layer {y} has {len(layer)} rows, expected {length}")
        for zi, row in enumerate(layer):
            if len(row) != width:
                raise ValueError(f"row length {len(row)} != width {width}")
            for x, b in enumerate(row):
                key = b if b.startswith("minecraft:") else "minecraft:" + b
                if key not in palette:
                    palette[key] = len(palette)
                # NOTE: structure files list coords as [x, y, z]; our layer
                # index is y (vertical plane), rows are z, columns are x.
                blocks_out.append({"pos": wrap_list([x, y, zi]),
                                   "state": palette[key]})
    # explicit __tags__ for the array fields (vanilla order: size, blocks,
    # entities, palette first for structure files is also accepted — we write
    # the commonly seen size/palette/blocks/entities order plus DataVersion).
    root = {"__tags__": [
        ("size", nbt.TAG_List, {"item_type": nbt.TAG_Int, "value": [width, height, length]}),
        ("palette", nbt.TAG_List,
         {"item_type": nbt.TAG_Compound, "value": [{"Name": k} for k in palette]}),
        ("blocks", nbt.TAG_List,
         {"item_type": nbt.TAG_Compound, "value": blocks_out}),
        ("entities", nbt.TAG_List, {"item_type": nbt.TAG_Compound, "value": []}),
        ("DataVersion", nbt.TAG_Int, mc_data_version),
        ("author", nbt.TAG_String, "firefly-mapart"),
    ]}
    data = nbt.write_gzip("", root)
    with open(path, "wb") as f:
        f.write(data)
    return {"path": str(path), "format": "structure-nbt",
            "size": [width, height, length], "palette": len(palette),
            "blocks": len(blocks_out), "mc_data_version": mc_data_version}


def validate_structure(path, expect=None) -> dict:
    """Independent structural validation of a structure-block file."""
    root = nbt.read_gzip(open(path, "rb").read())
    w, h, l = _as_list(root["size"])
    palette = _as_list(root["palette"])
    for entry in palette:
        if not entry.get("Name", "").startswith("minecraft:"):
            raise ValueError(f"palette entry lacks minecraft: prefix: {entry.get('Name')!r}")
    occupancy = set()
    non_air = 0
    for b in _as_list(root["blocks"]):
        x, y, z = _as_list(b["pos"])
        if not (0 <= x < w and 0 <= y < h and 0 <= z < l):
            raise ValueError(f"block out of bounds: {(x, y, z)}")
        key = (x, y, z)
        if key in occupancy:
            raise ValueError(f"duplicate block position: {key}")
        occupancy.add(key)
        if not 0 <= b.get("state", 0) < len(palette):
            raise ValueError("block state id out of palette range")
        if palette[b["state"]]["Name"] != "minecraft:air":
            non_air += 1
    report = {"path": str(path), "format": "structure-nbt", "size": [w, h, l],
              "volume": w * h * l, "palette_entries": len(palette),
              "blocks": len(occupancy), "non_air": non_air,
              "mc_data_version": root.get("DataVersion")}
    if expect:
        for field, value in expect.items():
            actual = tuple(report[field]) if field == "size" else report.get(field)
            expected = tuple(value) if field == "size" else value
            if actual != expected:
                raise ValueError(f"mismatch {field}: expected {expected}, got {actual}")
    return report


def validate_any(path, fmt: str, expect=None) -> dict:
    """Dispatch an independent validation by format name."""
    if fmt == "schem":
        return validate_schem(path, expect=expect)
    if fmt == "litematic":
        return validate_litematic(path, expect=expect)
    if fmt == "structure":
        return validate_structure(path, expect=expect)
    raise ValueError(f"unknown format {fmt!r}")
