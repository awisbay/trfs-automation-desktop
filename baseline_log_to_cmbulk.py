#!/usr/bin/env python3
"""
baseline_log_to_cmbulk.py — convert a moshell BASELINE session log into
ENM CMBulk `set` blocks.

The adaptive baseline .mos can't be converted statically (regex MO
addressing, if/for, runtime queries). Instead the node RESOLVES it by
running it; we capture the session log and harvest the concrete
attribute-set operations moshell actually performed.

For every executed `set`/`lset` moshell prints the echoed command plus a
result table:

    MIN791_BOALANB02> set  ENodeBFunction=1,EUtranCell.DD=.*,EUtranFreqRelation=1650  endcHoFreqPriority 6
    ====...
       Id  MO                                        endcHoFreqPriority  Result
    ====...
       45  EUtranCellFDD=BOALANG-L1,EUtranFreqRelation=1650  6 --> 6  -no change-

We take:
  * attribute  ← the table header (canonical casing)
  * value      ← from the ECHOED command (clean & complete: also structs,
                 lists and refs, which the table's "old --> new" column
                 mangles)
  * concrete MOs ← the table rows (regex already resolved to real cells)

FDN reconstruction: the table MO is the concrete path from the matched
class down (e.g. `EUtranCellFDD=..,EUtranFreqRelation=..`); moshell drops
the top concrete ancestor (`ENodeBFunction=1`). We re-attach it by taking
the echoed template's segments BEFORE the table's first class — which
drops any regex middle segments.

Rows that FAILED (`!!!! ERROR`) are skipped. Operational commands
(bl/ma/mr/wait/commit/cvms/st/get...) are ignored — no CMBulk equivalent.
"""
import re
import sys
import argparse
from collections import OrderedDict

ANSI = re.compile(r"\x1b\[[0-9;]*m")
PROMPT_CMD = re.compile(r"^\S+>\s+(\w[\w+-]*)\b(.*)$")
ROW = re.compile(r"^\s*\d+\s+(\S+)\s+.+?\s+-->\s+.+?\s+(\S.*\S|\S)\s*$")
INT_RE = re.compile(r"^-?\d+$")

# MO classes that moshell scripts often address WITHOUT their parent
# (relying on moshell's runtime resolution). ENM needs the full FDN, so
# when a reconstructed path or a ref STARTS with one of these we prepend
# its required parent. Classes not listed are treated as already rooted.
ROOT_PARENT = {
    "EUtranCellFDD": "ENodeBFunction=1",
    "EUtranCellTDD": "ENodeBFunction=1",
    "QciTable": "ENodeBFunction=1",
    "EndcProfile": "ENodeBFunction=1",
    "DrxProfile": "ENodeBFunction=1",
    "RlfProfile": "ENodeBFunction=1",
    "SectorCarrier": "ENodeBFunction=1",
    "PmFlexCounterFilter": "ENodeBFunction=1",
    "FieldReplaceableUnit": "Equipment=1",
    "NRCellDU": "GNBDUFunction=1",
    "NRCellCU": "GNBCUCPFunction=1",
}


def ensure_rooted(rel: str) -> str:
    """Prepend the required parent when a relative FDN starts with a
    class that moshell left un-rooted (e.g. EUtranCellFDD -> ENodeBFunction=1)."""
    first = class_of(rel.split(",", 1)[0])
    parent = ROOT_PARENT.get(first)
    if parent and not rel.startswith(parent + ","):
        return parent + "," + rel
    return rel


def norm_scalar(v: str) -> str:
    if v.upper() == "TRUE":
        return "true"
    if v.upper() == "FALSE":
        return "false"
    return v


def class_of(seg: str) -> str:
    return seg.split("=", 1)[0]


def class_match(tmpl_class: str, concrete_class: str) -> bool:
    if tmpl_class == concrete_class or tmpl_class.lower() == concrete_class.lower():
        return True
    try:
        return re.fullmatch(tmpl_class, concrete_class) is not None
    except re.error:
        return False


def reconstruct_fdn(mo_tmpl: str, table_mo: str) -> str:
    """Relative FDN (from ManagedElement) for one matched MO."""
    tbl_segs = table_mo.split(",")
    t0 = class_of(tbl_segs[0])
    prefix = []
    for seg in mo_tmpl.split(","):
        if class_match(class_of(seg), t0):
            rel = (",".join(prefix) + "," if prefix else "") + table_mo
            return ensure_rooted(rel)
        prefix.append(seg)
    # first table class not present in the template → the table MO already
    # carries its own ancestors (e.g. Lm=1,FeatureState=..) → use as-is.
    return ensure_rooted(table_mo)


def fmt_ref(v: str, full_prefix: str) -> str:
    if v.startswith("ManagedElement="):
        return f'"{full_prefix.rsplit(",ManagedElement=", 1)[0]},{v}"'
    return f'"{full_prefix},{ensure_rooted(v)}"'


def fmt_value(value: str, full_prefix: str):
    v = value.strip()
    if v == "":
        return None            # empty (e.g. userLabel) — skip
    if v.upper() in ("TRUE", "FALSE"):
        return v.lower()
    if INT_RE.match(v):
        return v
    if "," in v or "=" in v:
        segs = v.split(",")
        if all("=" in s for s in segs):
            first = class_of(segs[0])
            if first[:1].isupper():
                return fmt_ref(v, full_prefix)      # MO reference
            return "{" + v + "}"                    # struct
        if "=" not in v:
            return "[" + v + "]"                    # comma-separated list
    # space-separated list (e.g. cipheringAlgoPrio "2 1 3 0")
    if " " in v and "=" not in v:
        parts = v.split()
        if all(INT_RE.match(p) for p in parts):
            return "[" + ",".join(parts) + "]"
    return v                                        # enum / scalar string


def parse_log(text: str):
    lines = [ANSI.sub("", ln).rstrip("\r") for ln in text.splitlines()]
    triples = []          # (rel_fdn, attr, raw_value)
    stats = {"cmds": 0, "rows": 0, "err": 0}

    capture = False
    mo_tmpl = ""
    value = ""
    attr = None
    for line in lines:
        m = PROMPT_CMD.match(line)
        if m:
            verb, rest = m.group(1), m.group(2)
            if verb in ("set", "lset"):
                toks = rest.split()
                if len(toks) >= 2:
                    mo_tmpl = toks[0]
                    value = " ".join(toks[2:])   # attr=toks[1] (use header)
                    attr = None
                    capture = True
                    stats["cmds"] += 1
                else:
                    capture = False
            else:
                capture = False
            continue
        if not capture:
            continue
        if line.rstrip().endswith("Result") and " MO " in f" {line} ":
            toks = line.split()
            if "MO" in toks:
                attr = toks[toks.index("MO") + 1]
            continue
        rm = ROW.match(line)
        if rm and attr is not None:
            table_mo, result = rm.group(1), rm.group(2)
            if "ERROR" in result or result.startswith("!!!!"):
                stats["err"] += 1
                continue
            if result.startswith(">>>") or "no change" in result:
                rel = reconstruct_fdn(mo_tmpl, table_mo)
                triples.append((rel, attr, value))
                stats["rows"] += 1
    return triples, stats


def emit_cmbulk(triples, node, ossprefix):
    full_prefix = f"{ossprefix},MeContext={node},ManagedElement={node}"
    grouped = OrderedDict()      # rel_fdn -> OrderedDict(attr -> formatted)
    skipped = 0
    for rel, attr, raw in triples:
        fv = fmt_value(raw, full_prefix)
        if fv is None:
            skipped += 1
            continue
        grouped.setdefault(rel, OrderedDict())[attr] = fv
    blocks = []
    for rel, params in grouped.items():
        b = ["set", f'FDN : "{full_prefix},{rel}"']
        for a, v in params.items():
            b.append(f"{a} : {v}")
        blocks.append("\n".join(b))
    return "\n\n".join(blocks) + "\n", len(grouped), skipped


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile")
    ap.add_argument("node")
    ap.add_argument("--ossprefix", default="SubNetwork=ONRM_ROOT_MO_R,SubNetwork=T7")
    ap.add_argument("-o", "--out")
    args = ap.parse_args()
    with open(args.logfile, encoding="utf-8", errors="replace") as f:
        triples, stats = parse_log(f.read())
    out, nblocks, skipped = emit_cmbulk(triples, args.node, args.ossprefix)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out)
    sys.stderr.write(
        f"[stats] set/lset cmds={stats['cmds']}  rows->set={stats['rows']}  "
        f"MO blocks={nblocks}  errors_skipped={stats['err']}  "
        f"empty_skipped={skipped}\n")
    if not args.out:
        sys.stdout.write(out)
