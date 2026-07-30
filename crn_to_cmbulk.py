#!/usr/bin/env python3
"""
crn_to_cmbulk.py — convert Ericsson moshell relation scripts (crn ... end)
to ENM CMBulk create blocks.

Input block (moshell):
    crn ENodeBFunction=1,EUtranCellFDD=X-L1,GUtranFreqRelation=513390-30
    anrMeasOn TRUE
    cellReselectionPriority 7
    gUtranSyncSignalFrequencyRef GUtraNetwork=1,GUtranSyncSignalFrequency=513390-30
    qRxLevMin -140
    end

Output block (CMBulk):
    create
    FDN : "<PREFIX>,ENodeBFunction=1,EUtranCellFDD=X-L1,GUtranFreqRelation=513390-30"
    anrMeasOn : true
    cellReselectionPriority : 7
    gUtranSyncSignalFrequencyRef : "<PREFIX>,ENodeBFunction=1,GUtraNetwork=1,GUtranSyncSignalFrequency=513390-30"
    qRxLevMin : -140

where PREFIX = <ossprefix>,MeContext=<node>,ManagedElement=<node>
"""
import re
import sys
import argparse

INT_RE = re.compile(r"^-?\d+$")
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
# Struct fields that are ALWAYS strings in ENM (must stay quoted even
# though they look numeric — leading zeros / PLMN semantics).
STRUCT_STRING_FIELDS = {"mcc", "mnc"}
# A value that is an MO reference path: key=val segments whose KEYS are
# UpperCamelCase MO classes (e.g. GUtraNetwork=1,ExternalGUtranCell=..).
FDN_SEG_RE = re.compile(r"^[A-Z][A-Za-z0-9]*=[^,]+(,[A-Za-z][A-Za-z0-9]*=[^,]+)*$")
# A struct value: key=val segments whose KEYS are lowerCamelCase fields
# (e.g. mcc=515,mnc=02,mncLength=2).
STRUCT_SEG_RE = re.compile(r"^[a-z][A-Za-z0-9]*=[^,]+(,[a-z][A-Za-z0-9]*=[^,]+)*$")

# *Network MO classes that live directly under ENodeBFunction=1. moshell
# relation scripts often omit the "ENodeBFunction=1," prefix on refs that
# start at one of these — ENM needs the full FDN, so we re-insert it.
ENB_CHILD_NETWORKS = ("GUtraNetwork=", "EUtraNetwork=", "GeraNetwork=",
                      "UtraNetwork=", "Cdma2000Network=")


def fmt_struct(value: str) -> str:
    """mcc=515,mnc=02,mncLength=2  ->  {mcc="515", mnc="02", mncLength=2}
    Non-integer fields and leading-zero fields are quoted (plmn mcc/mnc are
    strings — "02" must NOT collapse to 2)."""
    parts = []
    for seg in value.split(","):
        k, _, v = seg.partition("=")
        k, v = k.strip(), v.strip()
        if (k in STRUCT_STRING_FIELDS or not INT_RE.match(v)
                or (len(v) > 1 and v.startswith("0"))):
            parts.append(f'{k}="{v}"')
        else:
            parts.append(f"{k}={v}")
    return "{" + ", ".join(parts) + "}"


def fmt_ref(value: str, prefix: str, node: str, ossprefix: str) -> str:
    """Turn a moshell relative MO reference into a full quoted ENM FDN."""
    v = value.strip()
    if v.startswith("ManagedElement="):
        # already local-rooted at the node → prepend ossprefix + MeContext
        return f'"{ossprefix},MeContext={node},{v}"'
    # Re-insert the omitted ENodeBFunction=1 when the ref starts at a
    # *Network MO that lives under ENodeBFunction=1.
    if v.startswith(ENB_CHILD_NETWORKS):
        v = "ENodeBFunction=1," + v
    return f'"{prefix},{v}"'


def fmt_value(attr: str, value: str, prefix: str, node: str,
              ossprefix: str) -> str:
    v = value.strip()
    up = v.upper()
    if up in ("TRUE", "FALSE"):
        return up.lower()
    if INT_RE.match(v):
        return v
    # Reference (UpperCamelCase-keyed FDN path)
    if FDN_SEG_RE.match(v) and "=" in v:
        return fmt_ref(v, prefix, node, ossprefix)
    # Struct (lowerCamelCase-keyed)
    if STRUCT_SEG_RE.match(v) and "=" in v:
        return fmt_struct(v)
    # IP address → quoted string.
    if IP_RE.match(v):
        return f'"{v}"'
    # Plain token (enum / identifier) — leave as-is.
    return v


def convert(text: str, node: str, ossprefix: str) -> str:
    prefix = f"{ossprefix},MeContext={node},ManagedElement={node}"
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("crn "):
            fdn_rel = line[4:].strip()
            block = ["create", f'FDN : "{prefix},{fdn_rel}"']
            i += 1
            while i < len(lines) and lines[i].strip() != "end":
                a = lines[i].strip()
                if a and not a.startswith("##"):
                    key, _, val = a.partition(" ")
                    block.append(
                        f"{key} : {fmt_value(key, val, prefix, node, ossprefix)}")
                i += 1
            out.append("\n".join(block))
        i += 1
    return "\n\n".join(out) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("node")
    ap.add_argument("--ossprefix", default="${OSSPREFIX}")
    ap.add_argument("-o", "--out")
    args = ap.parse_args()
    with open(args.infile, encoding="utf-8") as f:
        result = convert(f.read(), args.node, args.ossprefix)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(result)
