"""
dump_parser.py — parse an Ericsson node dump (modump DCG log OR cmdump 3GPP
XML) into a single flat structure:

    records: dict[ldn, dict[attr, value]]

where ``ldn`` is the MO FDN starting at ``ManagedElement=<node>`` (the
node-name segment intact) and ``attr``/``value`` are strings.

Ported from C:\\dev\\enp-generator (services/modump_dcg_parser.py +
services/cmdump_parser.py) — trimmed to the pure extraction we need for the
CDD audit (no web/Excel/comparator code). Format is auto-detected from the
file contents so the caller can feed either dump type.
"""
from __future__ import annotations

import gzip
import io
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict


# ══════════════════════════════════════════════════════════════════
#  Public entry point
# ══════════════════════════════════════════════════════════════════
def parse_dump(path: str) -> Dict[str, Dict[str, str]]:
    """Parse a dump file (any of: modump zip/gz/log, cmdump zip/xml) into
    ``{ldn: {attr: value}}``. Raises ValueError if nothing parseable."""
    with open(path, "rb") as fh:
        data = fh.read()
    kind = _detect_kind(data)
    if kind == "cmdump":
        return _parse_cmdump_bytes(data)
    return _parse_modump_bytes(data)


def _detect_kind(data: bytes) -> str:
    """Return 'cmdump' (3GPP XML) or 'modump' (DCG log / intmomlog)."""
    # Zip: look at member names.
    if zipfile.is_zipfile(io.BytesIO(data)):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
        except Exception:
            names = []
        if any(n.lower().endswith(".xml") for n in names):
            return "cmdump"
        return "modump"          # _dcg_k.log.gz / intmomlog*
    # Gzip magic → modump dcg.
    if data[:2] == b"\x1f\x8b":
        return "modump"
    head = data[:4096].lstrip()
    if head[:5] == b"<?xml" or head[:1] == b"<":
        return "cmdump"
    return "modump"


# ══════════════════════════════════════════════════════════════════
#  MODUMP (DCG log) — ported from DcgKModumpParser
# ══════════════════════════════════════════════════════════════════
def _modump_text(data: bytes) -> str:
    """Extract the DCG/intmomlog text from a modump zip/gz/plain payload."""
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            dcg = [n for n in names if n.endswith("_dcg_k.log.gz")]
            if dcg:
                member = sorted(dcg, key=len)[0]
                raw = zf.read(member)
                return gzip.decompress(raw).decode("utf-8", "replace")
            intmom = [n for n in names
                      if os.path.basename(n) in ("intmomlog2.txt", "intmomlog.txt")]
            if intmom:
                member = sorted(intmom,
                                key=lambda n: 0 if os.path.basename(n) == "intmomlog2.txt" else 1)[0]
                return zf.read(member).decode("utf-8", "replace")
            raise ValueError("modump zip has no _dcg_k.log.gz / intmomlog*.txt")
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data).decode("utf-8", "replace")
    return data.decode("utf-8", "replace")


def _parse_modump_bytes(data: bytes) -> Dict[str, Dict[str, str]]:
    text = _modump_text(data)
    # DCG columnar log begins lines with "MO  <ldn>". If we don't see that,
    # fall back to the intmomlog "<ldn>  <param>  <value>" format.
    if re.search(r"(?m)^\s*MO\s+\S+=", text):
        return _parse_dcg_text(text)
    return _parse_intmomlog_text(text)


def _parse_dcg_text(text: str) -> Dict[str, Dict[str, str]]:
    records: Dict[str, Dict[str, str]] = {}
    cur_ldn = None
    cur_rdn = ""
    pending_struct = ""
    active_struct = ""
    struct_fields: list[str] = []

    def commit_struct():
        nonlocal active_struct, struct_fields
        if cur_ldn and active_struct and struct_fields:
            val = "{" + ", ".join(struct_fields) + "}"
            existing = records[cur_ldn].get(active_struct)
            records[cur_ldn][active_struct] = f"{existing} | {val}" if existing else val
        active_struct = ""
        struct_fields = []

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        s = line.strip()
        if not s:
            continue

        if s.startswith("MO"):
            commit_struct()
            pending_struct = ""
            mo_val = re.sub(r"^MO\s+", "", s, count=1).strip()
            if mo_val and "=" in mo_val:
                cur_ldn = _display_ldn(mo_val)
                cur_rdn = cur_ldn.split(",")[-1]
                records.setdefault(cur_ldn, {})
            continue

        if cur_ldn is None:
            continue
        if s.startswith("=") or s.startswith("Proxy Id"):
            continue

        m = re.match(r"^([A-Za-z][A-Za-z0-9_.-]*)\[(\d+)\]$", s)
        if m:
            commit_struct()
            pending_struct = m.group(1)
            if m.group(2) == "0":
                records[cur_ldn][pending_struct] = ""
                pending_struct = ""
            continue

        m = re.match(r"^Struct\s+([A-Za-z][A-Za-z0-9_.-]*)(?:\s+has\s+\d+\s+members:)?", s)
        if m:
            commit_struct()
            active_struct = m.group(1)
            pending_struct = ""
            continue

        if re.match(r"^>>>\s+Struct\[\d+\].*has\s+\d+\s+members:", s) and pending_struct:
            commit_struct()
            active_struct = pending_struct
            continue

        member = _parse_struct_member(s)
        if member and active_struct:
            struct_fields.append(f"{member[0]}={member[1]}")
            continue

        parsed = _parse_attr_line(s)
        if not parsed:
            continue
        commit_struct()
        pending_struct = ""
        records[cur_ldn][parsed[0]] = parsed[1]

    commit_struct()
    return records


def _parse_attr_line(line: str):
    if line.startswith(">>>") or line.startswith("Struct "):
        return None
    parts = re.split(r"\s{2,}", line, maxsplit=1)
    if len(parts) != 2:
        return None
    attr, value = parts[0].strip(), parts[1].strip()
    if not attr or "=" in attr or attr.startswith("$"):
        return None
    arr = re.match(r"^([^\[]+)\[\d+\]$", attr)
    is_arr = arr is not None
    if arr:
        attr = arr.group(1)
    if is_arr:
        value = re.sub(r"\s*\([^)]*\)", "", value).strip()
    value = re.sub(r"\s+", " ", value)
    if is_arr:
        value = ";".join(p for p in value.split(" ") if p)
    return attr, value


def _parse_struct_member(line: str):
    m = re.match(r"^>>>\s+(?:\d+\.)?([^=]+?)\s*=\s*(.*)$", line)
    if not m:
        return None
    attr = m.group(1).strip()
    if not attr:
        return None
    return attr, re.sub(r"\s+", " ", m.group(2).strip())


def _parse_intmomlog_text(text: str) -> Dict[str, Dict[str, str]]:
    records: Dict[str, Dict[str, str]] = {}
    pat = re.compile(
        r"^([A-Za-z][A-Za-z0-9]*=[^,\s]+(?:,[A-Za-z][A-Za-z0-9]*=[^,\s]+)*)\s+(\S+)\s*(.*)$")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("=") or ">>>" in line:
            continue
        m = pat.match(line)
        if not m:
            continue
        mo, param, value = m.groups()
        value = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
        value = re.sub(r"\s+", " ", value)
        records.setdefault(_display_ldn(mo), {})[param] = value
    return records


def _display_ldn(ldn: str) -> str:
    i = ldn.find("ManagedElement=")
    return ldn[i:] if i >= 0 else ldn


# ══════════════════════════════════════════════════════════════════
#  CMDUMP (3GPP XML) — ported from CmdumpXMLParser
# ══════════════════════════════════════════════════════════════════
def _parse_cmdump_bytes(data: bytes) -> Dict[str, Dict[str, str]]:
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xmls = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            if not xmls:
                raise ValueError("cmdump zip has no .xml")
            xml_bytes = zf.read(xmls[0])
    else:
        xml_bytes = data
    root = ET.fromstring(xml_bytes)
    config_data = root.find("{configData.xsd}configData")
    if config_data is None:
        # some exports nest differently — search anywhere
        for el in root.iter():
            if _local(el.tag) == "configData":
                config_data = el
                break
    if config_data is None:
        raise ValueError("cmdump XML has no configData")

    parent_map = {c: p for p in config_data.iter() for c in p}
    records: Dict[str, Dict[str, str]] = {}
    for el in config_data.iter():
        if _local(el.tag) != "VsDataContainer":
            continue
        vtype = _vs_data_type(el)
        if not vtype:
            continue
        dn = _build_dn(el, parent_map)
        attrs = _vs_attributes(el)
        # Generic 3GPP attributes (e.g. <xn:userLabel>) live in the MO wrapper's
        # own <xn:attributes> block — a sibling of the VsDataContainer, NOT
        # inside <es:vsData…>. Merge them so attributes like ManagedElement's
        # userLabel aren't lost. Ericsson (es:) values take priority on clash.
        parent = parent_map.get(el)
        if parent is not None:
            for k, v in _generic_attributes(parent).items():
                if not attrs.get(k):
                    attrs[k] = v
        if dn:
            records.setdefault(dn, {}).update(attrs)
    return records


def _generic_attributes(mo_el) -> Dict[str, str]:
    """Read the MO wrapper element's own <attributes> block (generic 3GPP
    attrs like userLabel), skipping vsData markers and the nested container."""
    out: Dict[str, str] = {}
    for child in mo_el:
        if _local(child.tag) != "attributes":
            continue
        for a in child:
            tag = _local(a.tag)
            if tag.startswith("vsData"):
                continue
            _add_attr(out, tag, _attr_value(a))
        break   # only the wrapper's direct generic attributes block
    return out


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _vs_data_type(el) -> str:
    a = el.find("{genericNrm.xsd}attributes")
    if a is None:
        return ""
    t = a.find("{genericNrm.xsd}vsDataType")
    return t.text if (t is not None and t.text) else ""


def _build_dn(el, parent_map) -> str:
    parts = []
    cur = el
    seen = set()
    while cur is not None and len(seen) < 200:
        tag = _local(cur.tag)
        mo_id = cur.attrib.get("id")
        if tag == "VsDataContainer":
            vt = _vs_data_type(cur)
            if vt and mo_id:
                parts.insert(0, (vt.replace("vsData", "", 1), mo_id))
        elif tag != "attributes" and mo_id:
            parts.insert(0, (tag, mo_id))
        cur = parent_map.get(cur)
        seen.add(cur)
    norm = []
    for cls, mid in parts:
        if norm and norm[-1] == (cls, mid):
            continue
        norm.append((cls, mid))
    dn = ",".join(f"{c}={i}" for c, i in norm)
    return _display_ldn(dn)


def _vs_attributes(el) -> Dict[str, str]:
    out: Dict[str, str] = {}
    attrs = el.find("{genericNrm.xsd}attributes")
    if attrs is None:
        return out
    for child in attrs:
        tag = _local(child.tag)
        if tag in ("vsDataType", "vsDataFormatVersion"):
            continue
        if tag.startswith("vsData"):
            for a in child:
                _add_attr(out, _local(a.tag), _attr_value(a))
        else:
            _add_attr(out, tag, _attr_value(child))
    return out


def _attr_value(el) -> str:
    if len(el) == 0:
        return el.text or ""
    vals = [f"{_local(c.tag)}={_attr_value(c)}" for c in el]
    return "{" + ", ".join(vals) + "}"


def _add_attr(out: Dict[str, str], name: str, value: str):
    value = (value or "").replace("vsData", "")
    if name in out and out[name]:
        sep = " | " if value.startswith("{") or out[name].startswith("{") else " "
        out[name] = f"{out[name]}{sep}{value}".strip()
    else:
        out[name] = value
