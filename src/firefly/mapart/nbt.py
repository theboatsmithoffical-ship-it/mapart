"""Tiny, dependency-free NBT (Named Binary Tag) reader/writer (stdlib only).

Supports the tag types needed by the Sponge schematic format (v2/v3):
TAG_Byte(1) Short(2) Int(3) Long(4) Float(5) Double(6) ByteArray(7)
String(8) List(9) Compound(10) IntArray(11) LongArray(12).

All integers are big-endian; strings are modified-UTF-8 length-prefixed.
Returns and consumes plain Python structures:
    compound -> dict[str, value]
    list     -> dict {"__type__": tag_id, "value": [items]}
    tag id string constant:  TAG_Byte = 1 ... TAG_Long_Array = 12
"""
from __future__ import annotations

import gzip
import struct

END = 0
TAG_Byte = 1
TAG_Short = 2
TAG_Int = 3
TAG_Long = 4
TAG_Float = 5
TAG_Double = 6
TAG_Byte_Array = 7
TAG_String = 8
TAG_List = 9
TAG_Compound = 10
TAG_Int_Array = 11
TAG_Long_Array = 12

class NBTError(ValueError):
    pass


# ---------- writer ----------

def _pack(fmt_prefix: int, v: int) -> bytes:
    fmt = {1: "b", 2: "h", 3: "i", 4: "q"}.get(fmt_prefix)
    if fmt is None:
        raise NBTError(f"pack unsupported tag {fmt_prefix}")
    return struct.pack(">" + fmt, v)


def _enc_tag(tid: int, v) -> bytes:
    if tid in (1, 2, 3, 4):
        i = int(v)
        fmt = {1: "b", 2: "h", 3: "i", 4: "q"}[tid]
        if not (-(1 << (8 * struct.calcsize(fmt) - 1)) <= i < 1 << (8 * struct.calcsize(fmt) - 1)):
            raise NBTError(f"value {i} out of range for tag {tid}")
        return struct.pack(">" + fmt, i)
    if tid == 5:
        return struct.pack(">f", float(v))
    if tid == 6:
        return struct.pack(">d", float(v))
    if tid == 7:
        return struct.pack(">i", len(v)) + bytes(bytearray(v))
    if tid == 8:
        s = str(v).encode("utf-8", "surrogatepass")
        return struct.pack(">H", len(s)) + s
    if tid == 9:
        # v = {"item_type": int, "value": [...]} or a plain python list (auto-wrapped)
        if not (isinstance(v, dict) and "item_type" in v):
            v = wrap_list(list(v))
        items = v.get("value", [])
        itid = v.get("item_type")
        if itid is None:
            raise NBTError("list requires explicit item_type")
        return struct.pack(">B", itid) + struct.pack(">i", len(items)) + b"".join(_enc_tag(itid, i) for i in items)
    if tid == 10:
        out = b""
        tags = v["__tags__"] if "__tags__" in v else [(k, _infer(val), val) for k, val in v.items()]
        for k, tv, val in tags:
            if tv == TAG_List and not (isinstance(val, dict) and "item_type" in val):
                val = wrap_list(val)
            out += struct.pack(">B", tv) + _enc_tag(8, k) + _enc_payload(tv, val)
        return out + b"\x00"
    if tid == 11:
        return struct.pack(">i", len(v)) + b"".join(struct.pack(">i", int(x)) for x in v)
    if tid == 12:
        return struct.pack(">i", len(v)) + b"".join(struct.pack(">q", int(x)) for x in v)
    raise NBTError(f"unsupported tag {tid}")


def _strip_wrap(item):
    """unwrap {'__tag__': T, 'value': x} payloads inside lists."""
    if isinstance(item, dict) and "__tag__" in item:
        return _strip_wrap(item["value"])
    return item


def wrap_list(items: list) -> dict:
    """Wrap a plain python list into the TAG_List payload representation."""
    if not items:
        return {"item_type": TAG_String, "value": []}
    first = items[0]
    if isinstance(first, bool):
        itid = TAG_Byte
    elif isinstance(first, int):
        itid = TAG_Int
    elif isinstance(first, float):
        itid = TAG_Double
    elif isinstance(first, str):
        itid = TAG_String
    elif isinstance(first, bytes):
        itid = TAG_Byte_Array
    elif isinstance(first, dict):
        itid = TAG_Compound
    elif isinstance(first, list):
        itid = TAG_List
    else:
        raise NBTError(f"cannot wrap list of {type(first)}")
    return {"item_type": itid, "value": items}


def _infer(v):
    if isinstance(v, bool):
        return TAG_Byte
    if isinstance(v, int):
        return TAG_Int
    if isinstance(v, float):
        return TAG_Double
    if isinstance(v, str):
        return TAG_String
    if isinstance(v, bytes):
        return TAG_Byte_Array
    if isinstance(v, list):
        # keep lists of ints as IntArray if caller has not decided otherwise;
        # the caller passes explicit type via payload dict
        return TAG_List
    if isinstance(v, dict):
        return TAG_Compound
    raise NBTError(f"cannot infer tag for {type(v)}")


def _enc_payload(tid: int, v) -> bytes:
    return _enc_tag(tid, v)


def write(root_name: str, root: dict) -> bytes:
    """Encode {root_name: dict-compound} as *uncompressed* NBT bytes.

    A root of {"__tags__": [(name, tid, value), ...]} selects explicit tag
    types per member (needed for Int/Long/Byte arrays and typed empty lists
    that plain python values cannot express)."""
    if "__tags__" in root:
        tags = list(root["__tags__"])
    else:
        tags = [(k, _infer(val), val) for k, val in root.items()]
    return (struct.pack(">B", TAG_Compound) + _enc_tag(8, root_name)
            + _enc_tag(TAG_Compound, {"__tags__": tags}))


def write_gzip(root_name: str, root: dict) -> bytes:
    return gzip.compress(write(root_name, root), mtime=0)


# ---------- reader ----------

class _R:
    def __init__(self, data: bytes):
        self.d = data
        self.i = 0

    def _take(self, n: int) -> bytes:
        if self.i + n > len(self.d):
            raise NBTError("truncated NBT")
        b = self.d[self.i:self.i + n]
        self.i += n
        return b

    def ubyte(self) -> int:
        return self._take(1)[0]

    def struct(self, fmt: str):
        return struct.unpack(">" + fmt, self._take(struct.calcsize(">" + fmt)))[0]

    def string(self) -> str:
        n = self.ushort()
        return self._take(n).decode("utf-8", "surrogatepass")

    def ushort(self) -> int:
        return self.struct("H")


def _fmt_for(tid):
    return {1: "b", 2: "h", 3: "i", 4: "q"}[tid]


def read_payload(r: _R, tid: int):
    if tid in (1, 2, 3, 4):
        return r.struct(_fmt_for(tid))
    if tid == 5:
        return r.struct("f")
    if tid == 6:
        return r.struct("d")
    if tid == 7:
        n = r.struct("i")
        return r._take(n)
    if tid == 8:
        return r.string()
    if tid == 9:
        itid = r.ubyte()
        n = r.struct("i")
        return [read_payload(r, itid) for _ in range(n)]
    if tid == 10:
        out = {}
        while True:
            t = r.ubyte()
            if t == 0:
                break
            name = r.string()
            out[name] = read_payload(r, t)
        return out
    if tid == 11:
        n = r.struct("i")
        return [r.struct("i") for _ in range(n)]
    if tid == 12:
        n = r.struct("i")
        return [r.struct("q") for _ in range(n)]
    raise NBTError(f"unsupported tag {tid}")


def read(data: bytes) -> dict:
    r = _R(data)
    t = r.ubyte()
    if t != TAG_Compound:
        raise NBTError("root must be compound")
    name = r.string()
    root = read_payload(r, TAG_Compound)
    root["__name__"] = name
    return root


def read_gzip(data: bytes) -> dict:
    return read(gzip.decompress(data))
