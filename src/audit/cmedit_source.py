"""
cmedit_source.py — live node/BSC values via ``cmedit get ... -t`` over the
prepared SSH session, for audit profiles whose ``source == "cmedit"`` (GSM
audit is mostly BSC-level, not in the node dump).

For each such profile we run ONE bulk command:
    !python <cli.py> "cmedit get <scope> <MOClass>.(attr1,attr2,...) -t"
and parse the table into records ``{ldn: {attr: value}}`` — the SAME shape
as the dump parser, so ``audit_core.compare`` treats both identically.

NOTE: the exact ``-t`` table layout is calibrated against a real command
output (see _parse_cmedit_table). Adjust there if columns don't line up.
"""
from __future__ import annotations

import re
from typing import Dict, List


def _leaf_class(mo_fdn: str) -> str:
    """'GeranCell={geranCellId}' -> 'GeranCell'."""
    last = mo_fdn.split(",")[-1]
    return last.split("=", 1)[0].strip()


def fetch_cmedit_records(ssh, node_name: str, profiles: List[dict],
                         site_id: str = "",
                         log=lambda m: None) -> Dict[str, Dict[str, str]]:
    """Run cmedit for every cmedit-sourced profile; merge into records.

    The command is built from the profile's optional ``cmedit_cmd`` template
    (placeholders ``{scope} {mo} {attrs} {site_id} {site_mod}``), else a
    default ``cmedit get {scope} {mo}.({attrs}) -t``. ``site_mod`` is the
    integration-style modified Site ID (e.g. MIN3117 → M3117) used to scope
    the query to this site's cells."""
    from integration_runner import CLI_PY
    try:
        from integration_runner import _shortcode_to_cell_id
        site_mod = _shortcode_to_cell_id(site_id) if site_id else ""
    except Exception:
        site_mod = ""

    records: Dict[str, Dict[str, str]] = {}
    for prof in profiles:
        if prof.get("source") != "cmedit":
            continue
        # The QUERY class name (``cmedit_mo``) can differ in case from the
        # FDN leaf class — cli.py's cmedit is case-sensitive, and the
        # integration's proven GSM query uses lowercase ``gerancell``.
        mo_class = prof.get("cmedit_mo") or _leaf_class(prof.get("mo_fdn", ""))
        attrs = [c["attr"] for c in prof.get("columns", []) if c.get("attr")]
        if not mo_class or not attrs:
            continue
        scope = prof.get("cmedit_scope", "*")
        attr_list = ",".join(attrs)
        tmpl = prof.get("cmedit_cmd", "cmedit get {scope} {mo}.({attrs}) -t")
        inner = (tmpl.replace("{scope}", scope).replace("{mo}", mo_class)
                     .replace("{attrs}", attr_list)
                     .replace("{site_id}", site_id)
                     .replace("{site_mod}", site_mod))
        # cli.py talks to ENM directly, so we run it in the plain shell —
        # no need to enter AMOS on a specific node (GSM audit is BSC-level
        # and often has no single node to attach to).
        cmd = f'python {CLI_PY} "{inner}"'
        log(f"[audit/cmedit] {prof.get('name')}: {inner}")
        try:
            out = ssh.run_command(cmd, timeout=180)
        except Exception as exc:
            log(f"[audit/cmedit] command failed: {exc}")
            continue
        # Show the raw cmedit output so a 0-MO result can be diagnosed
        # (empty query vs a parser mismatch).
        clean = out.strip()
        log("[audit/cmedit] -- raw output (first 1500 chars) --")
        for line in clean[:1500].splitlines():
            log("   " + line)
        if len(clean) > 1500:
            log(f"   ... ({len(clean)} chars total)")
        parsed = _parse_cmedit_table(out, attrs)
        log(f"[audit/cmedit] parsed {len(parsed)} MO(s) for {mo_class}")
        for ldn, a in parsed.items():
            records.setdefault(ldn, {}).update(a)
    return records


_FDN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*=[^,\s]+(?:,[A-Za-z][A-Za-z0-9_]*=[^,\s]+)*")


def _split_row(ln: str) -> List[str]:
    """Split a cmedit ``-t`` row. cli.py emits TAB-separated columns; fall
    back to whitespace when a line has no tabs."""
    if "\t" in ln:
        return [c.strip() for c in ln.split("\t")]
    return ln.split()


_INSTANCE_RE = re.compile(r"^\d+\s+instance", re.IGNORECASE)


def _parse_cmedit_table(out: str, wanted: List[str]) -> Dict[str, Dict[str, str]]:
    """Parse ``cmedit get ... -t`` output (cli.py table mode).

    Real layout (TAB-separated), e.g. for GeranCell:
        SubNetwork,SubNetwork,...,GeranCellM,GeranCell          <- DN hierarchy
        NodeId  BscFunctionId ... GeranCellId  bcc  bcchNo ...  <- header
        MINBS00 1 ... M48939S1  2  22  ...                      <- data rows
        3 instance(s)

    The DN is spread across leading ``<Class>Id`` columns rather than one FDN
    token. We locate the header (contains the wanted attrs), take the last
    ``...Id`` column *before* the first attribute column as the leaf identity
    (e.g. ``GeranCellId`` → leaf class ``GeranCell``), and build the record key
    ``GeranCell=<value>`` so it matches the CDD ``mo_fdn`` leaf.
    """
    lines = out.replace("\r", "").split("\n")
    wanted_l = [w.lower() for w in wanted]

    header_idx = -1
    header_cols: List[str] = []
    for i, ln in enumerate(lines):
        toks = _split_row(ln)
        low = [t.lower() for t in toks]
        if sum(1 for w in wanted_l if w in low) >= max(1, len(wanted_l) // 2):
            header_cols = toks
            header_idx = i
            break
    if header_idx < 0:
        return {}

    # attribute name -> column index in a data row
    attr_pos: Dict[str, int] = {}
    for pos, col in enumerate(header_cols):
        cl = col.lower()
        for w in wanted:
            if cl == w.lower() and w not in attr_pos:
                attr_pos[w] = pos
    if not attr_pos:
        return {}

    # Leaf identity: last "<Class>Id" column before the first attribute column.
    first_attr = min(attr_pos.values())
    leaf_pos = -1
    for pos in range(first_attr - 1, -1, -1):
        if header_cols[pos].lower().endswith("id"):
            leaf_pos = pos
            break
    leaf_class = header_cols[leaf_pos][:-2] if leaf_pos >= 0 else ""

    records: Dict[str, Dict[str, str]] = {}
    for ln in lines[header_idx + 1:]:
        s = ln.strip()
        if not s or set(s) <= set("="):
            continue
        if _INSTANCE_RE.match(s) or s.lower().startswith("total:"):
            break
        if s.startswith("[") and s.endswith("$"):   # shell prompt echo
            continue
        toks = _split_row(ln)
        if leaf_pos < 0 or leaf_pos >= len(toks):
            # Fallback: a single FDN token in column 0.
            if toks and _FDN_RE.match(toks[0]):
                key = _display_ldn(toks[0])
            else:
                continue
        else:
            leaf_val = toks[leaf_pos]
            if not leaf_val:
                continue
            key = f"{leaf_class}={leaf_val}" if leaf_class else leaf_val
        rec = records.setdefault(key, {})
        for attr, pos in attr_pos.items():
            if pos < len(toks):
                rec[attr] = toks[pos]
    return records


def _display_ldn(ldn: str) -> str:
    i = ldn.find("ManagedElement=")
    return ldn[i:] if i >= 0 else ldn
