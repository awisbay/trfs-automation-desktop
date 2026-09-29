"""
ims2_reader.py — read a Nokia BTSOAM IM snapshot (.ims2) without WebEM.

The snapshot is self-describing, so no per-release schema is hard-coded here.

Envelope (big endian):

    u32 1, u32 22, u32 18, "BTSOAM IM SNAPSHOT", MAGIC(12)
    then repeatedly:  u32 recordType, u64 length, body, MAGIC(12)

      recordType 2 -> ZIP holding <interface>/meta.xml (the info model)
      recordType 3 -> 4-byte flag
      recordType 0 -> concatenated gzip members holding the MO data (last)

Each inflated gzip member is a stream of 12-byte block headers followed by

    write  = u16 dnLen, dn, u8 0, u32 payloadLen, protobuf payload
    delete = u16 dnLen, dn, u8 1                  (e.g. a cleared alarm)

meta.xml maps every MO class's protobuf field numbers to parameter names,
types and enums (``<p name=..><proto index=.. type=../></p>``). The snapshot
is a log, so one DN may be written several times: the last write wins.
"""
from __future__ import annotations

import io
import re
import struct
import zipfile
import zlib
import xml.etree.ElementTree as ET
from collections import OrderedDict
from typing import Dict, Iterator, Optional, Tuple

MAGIC = bytes.fromhex("e91100a843a0412d94b306da")
_HEADER = b"BTSOAM IM SNAPSHOT"
_GZIP = b"\x1f\x8b\x08"
_DN_RE = re.compile(rb"^/[\x20-\x7e]+$")


class Ims2Error(Exception):
    pass


# ── info model (meta.xml) ─────────────────────────────────────────────
class Field:
    __slots__ = ("name", "kind", "repeated", "struct", "enum")

    def __init__(self, name, kind, repeated, struct=None, enum=None):
        self.name = name
        self.kind = kind          # proto type: string/double/enum/...
        self.repeated = repeated
        self.struct = struct      # {index: Field} for a nested message
        self.enum = enum          # {value: name}


def _enums(elem) -> dict:
    return {en.get("name"): {int(e.get("value")): e.get("name")
                             for e in en.findall("enum")}
            for en in elem.findall("enumeration")}


def _fields(elem, enum_scope: dict) -> Dict[int, Field]:
    """{proto index: Field} for a managedObject or struct element."""
    scope = dict(enum_scope)
    scope.update(_enums(elem))
    structs = {st.get("name"): _fields(st, scope)
               for st in elem.findall("struct")}
    out = {}
    for p in elem.findall("p"):
        proto = p.find("proto")
        if proto is None:
            continue
        ptype = p.get("type") or ""
        pscope = dict(scope)
        pscope.update(_enums(p))
        struct = structs.get(ptype)
        if struct is None and p.find("struct") is not None:
            struct = _fields(p.find("struct"), pscope)
        out[int(proto.get("index"))] = Field(
            p.get("name"), proto.get("type"),
            proto.get("repeated") == "true" or p.get("recurrence") == "repeated",
            struct=struct, enum=pscope.get(ptype))
    return out


def load_model(meta_xml: bytes) -> Dict[str, Dict[int, Field]]:
    """meta.xml -> {MO class: {proto index: Field}}."""
    root = ET.fromstring(meta_xml)
    return {mo.get("class"): _fields(mo, {})
            for mo in root.findall("managedObject")}


# ── protobuf ──────────────────────────────────────────────────────────
def _varint(buf, off):
    r = s = 0
    while True:
        b = buf[off]
        off += 1
        r |= (b & 0x7F) << s
        if not b & 0x80:
            return r, off
        s += 7


def _zigzag(v):
    return (v >> 1) ^ -(v & 1)


_PACKABLE = {"enum", "int32", "uint32", "int64", "uint64", "sint32",
             "sint64", "bool"}


def _scalar(v, f: Optional[Field]):
    if f is None:
        return v
    if f.kind in ("sint32", "sint64"):
        return _zigzag(v)
    if f.kind in ("int32", "int64") and v >= 1 << 63:
        return v - (1 << 64)
    if f.kind == "bool":
        return bool(v)
    if f.enum:
        return f.enum.get(v, v)
    return v


def decode(buf: bytes, fields: Dict[int, Field]) -> "OrderedDict[str, object]":
    """Decode one protobuf message using a meta.xml field map. Unknown
    field numbers are kept as ``#<index>`` so nothing is silently lost."""
    out: "OrderedDict[str, object]" = OrderedDict()

    def put(f, idx, value):
        name = f.name if f else "#%d" % idx
        if f is not None and f.repeated:
            out.setdefault(name, []).append(value)
        else:
            out[name] = value

    off, end = 0, len(buf)
    while off < end:
        tag, off = _varint(buf, off)
        idx, wire = tag >> 3, tag & 7
        f = fields.get(idx)
        if wire == 0:
            v, off = _varint(buf, off)
            put(f, idx, _scalar(v, f))
        elif wire == 1:
            raw = buf[off:off + 8]
            off += 8
            fmt = "<d" if f is None or f.kind == "double" else "<Q"
            put(f, idx, struct.unpack(fmt, raw)[0])
        elif wire == 5:
            raw = buf[off:off + 4]
            off += 4
            put(f, idx, struct.unpack("<f" if f and f.kind == "float"
                                      else "<I", raw)[0])
        elif wire == 2:
            ln, off = _varint(buf, off)
            raw = buf[off:off + ln]
            off += ln
            if f is None:
                put(f, idx, raw)
            elif f.struct is not None:
                put(f, idx, decode(raw, f.struct))
            elif f.kind == "string":
                put(f, idx, raw.decode("utf-8", "replace"))
            elif f.repeated and f.kind in _PACKABLE:
                o = 0
                while o < len(raw):
                    v, o = _varint(raw, o)
                    put(f, idx, _scalar(v, f))
            elif f.repeated and f.kind == "double":
                for v in struct.unpack("<%dd" % (len(raw) // 8), raw):
                    put(f, idx, v)
            else:
                put(f, idx, raw)
        else:                       # groups (3/4) are not used by the model
            break
    return out


# ── container ─────────────────────────────────────────────────────────
def _envelope(raw: bytes) -> Iterator[Tuple[int, bytes]]:
    if _HEADER not in raw[:64]:
        raise Ims2Error("Not a Nokia IM snapshot (.ims2): header missing")
    i = raw.find(MAGIC)
    if i < 0:
        raise Ims2Error("Not a Nokia IM snapshot (.ims2): magic missing")
    i += len(MAGIC)
    while i + 12 <= len(raw):
        typ, = struct.unpack_from(">I", raw, i)
        if typ == 0:
            # The data record runs to the end of the file; its length field
            # is not reliable, so don't use it.
            yield typ, raw[i + 12:]
            return
        length, = struct.unpack_from(">Q", raw, i + 4)
        yield typ, raw[i + 12:i + 12 + length]
        i += 12 + length
        if raw[i:i + len(MAGIC)] == MAGIC:
            i += len(MAGIC)


def _gzip_members(blob: bytes) -> Iterator[bytes]:
    pos = blob.find(_GZIP)
    while pos >= 0:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        yield d.decompress(blob[pos:])
        if not d.unused_data:
            return
        pos = blob.find(_GZIP, len(blob) - len(d.unused_data))


def _mo_records(member: bytes) -> Iterator[Tuple[bytes, int, bytes]]:
    """Yield (dn, flag, payload); flag 1 = the MO was deleted."""
    off, end = 0, len(member)
    while off + 7 <= end:
        dn_len, = struct.unpack_from(">H", member, off)
        dn = member[off + 2:off + 2 + dn_len]
        if dn_len and _DN_RE.match(dn):
            o = off + 2 + dn_len
            if o < end and member[o] == 1:      # delete: no length, no payload
                yield dn, 1, b""
                off = o + 1
                continue
            if o + 5 > end:
                return
            pl, = struct.unpack_from(">I", member, o + 1)
            if member[o] == 0 and o + 5 + pl <= end:
                yield dn, 0, member[o + 5:o + 5 + pl]
                off = o + 5 + pl
                continue
        off += 12                   # a block header — skip it


def mo_class(dn: str) -> str:
    """'/MRBTS-1/.../RETU_R-3' -> 'RETU_R'."""
    return re.sub(r"-[^-/]*$", "", dn.rsplit("/", 1)[-1])


def parent(dn: str) -> str:
    return dn.rsplit("/", 1)[0]


class Snapshot:
    """A parsed .ims2 file. ``raw`` holds the last payload per live DN
    (deleted MOs are dropped); decode lazily with :meth:`get` /
    :meth:`by_class`. For the classes in ``history_classes`` every write and
    delete is also kept, in log order, in ``history``."""

    def __init__(self, path: str, history_classes=("ALARM",)):
        self.path = path
        with open(path, "rb") as fh:
            raw = fh.read()
        self.model: Dict[str, Dict[int, Field]] = {}
        data = b""
        for typ, body in _envelope(raw):
            if typ == 2:
                try:
                    z = zipfile.ZipFile(io.BytesIO(body))
                except zipfile.BadZipFile as exc:
                    raise Ims2Error("Info model (meta.xml) is unreadable: %s"
                                    % exc) from exc
                for name in z.namelist():
                    if name.endswith("meta.xml"):
                        self.model.update(load_model(z.read(name)))
            elif typ == 0:
                data = body
        if not self.model:
            raise Ims2Error("Snapshot has no info model (meta.xml)")
        if not data:
            raise Ims2Error("Snapshot has no MO data")
        self.raw: "OrderedDict[str, bytes]" = OrderedDict()
        # [(dn, deleted, payload)] in log order, for history_classes only
        self.history: list = []
        keep = set(history_classes or ())
        for member in _gzip_members(data):
            for dn_b, flag, payload in _mo_records(member):
                dn = dn_b.decode("ascii")
                if keep and mo_class(dn) in keep:
                    self.history.append((dn, flag == 1, payload))
                if flag == 1:
                    self.raw.pop(dn, None)
                else:
                    self.raw[dn] = payload

    def get_payload(self, cls: str, payload: bytes) -> "OrderedDict[str, object]":
        """Decode a payload of MO class ``cls`` (e.g. one from history)."""
        return decode(payload, self.model.get(cls) or {})

    def get(self, dn: str) -> "OrderedDict[str, object]":
        return decode(self.raw[dn], self.model.get(mo_class(dn)) or {})

    def by_class(self, *classes: str) -> Iterator[Tuple[str, dict]]:
        want = set(classes)
        for dn in self.raw:
            if mo_class(dn) in want:
                yield dn, self.get(dn)
