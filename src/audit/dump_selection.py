"""Select one coherent snapshot per node, using payload identities and time."""
from __future__ import annotations

import datetime as dt
import os
import re

from .dump_parser import parse_dump

_NAMED_DUMP = re.compile(
    r'^(?P<node>.+)_(?:cm|mo)dump(?:_(?P<time>\d{8}_\d{6}))?\.(?:zip|gz|log|xml)$', re.I)
_OWNER = re.compile(r'(?:^|,)ManagedElement=([^,]+)(?:,|$)')


def site_matches(node, site):
    node, site = node.casefold(), site.strip().casefold()
    return not site or node == site or node.startswith((site + '_', site + '-'))


def select_node_dumps(paths, *, site='', expected_nodes=None, existing_nodes=(), log=lambda m: None):
    """Newest snapshot wins across formats; never fill it with older config.

    Filename capture time, when present, precedes filesystem modification time.
    Untimestamped files use mtime, explicitly marked as such in the evidence.
    Recognizable per-node filenames must agree with the payload. Generic filenames
    (including combined exports) derive their nodes entirely from ManagedElement.
    Existing nodes represent the fresh live batch export and cannot be replaced.
    """
    expected = {n.casefold() for n in expected_nodes} if expected_nodes is not None else None
    protected = {n.casefold() for n in existing_nodes}
    candidates = []
    seen_paths = set()
    for path in paths:
        identity = os.path.normcase(os.path.abspath(path))
        if identity in seen_paths:
            continue
        seen_paths.add(identity)
        if not os.path.isfile(path):
            log(f'[audit/dump] File not found: {path}')
            continue
        named = _NAMED_DUMP.match(os.path.basename(path))
        if named and named['node'].casefold().endswith('_batch'):
            named = None  # Combined cluster export, not a per-node filename.
        stamp, basis = os.path.getmtime(path), 'filesystem mtime (capture time unavailable)'
        if named and named['time']:
            try:
                stamp = dt.datetime.strptime(named['time'], '%Y%m%d_%H%M%S').timestamp()
                basis = 'filename capture time'
            except ValueError:
                log(f'[audit/dump] Invalid capture timestamp: {os.path.basename(path)}; using mtime')
        candidates.append((stamp, identity, path, named, basis))

    chosen, records = {}, {}
    # Stable path tie-break, independent of input ordering and file format.
    for stamp, _, path, named, basis in sorted(candidates, key=lambda c: (-c[0], c[1])):
        if named and (named['node'].casefold() in protected or named['node'].casefold() in chosen):
            log(f"[audit/dump] Ignored older/duplicate snapshot for {named['node']}: {os.path.basename(path)}")
            continue
        try:
            parsed = parse_dump(path)
        except Exception as exc:
            log(f'[audit/dump] Parse failed: {os.path.basename(path)}: {exc}')
            continue
        grouped = {}
        for dn, attrs in parsed.items():
            owner = _OWNER.search(dn)
            if owner:
                grouped.setdefault(owner[1], {})[dn] = attrs
        if not grouped:
            log(f'[audit/dump] Rejected {os.path.basename(path)}: no ManagedElement identity in payload')
            continue
        if named and {n.casefold() for n in grouped} != {named['node'].casefold()}:
            log(f'[audit/dump] Rejected {os.path.basename(path)}: filename node '
                f"'{named['node']}' differs from payload {', '.join(sorted(grouped))}")
            continue
        for node, node_records in grouped.items():
            key = node.casefold()
            if not site_matches(node, site) or (expected is not None and key not in expected):
                log(f'[audit/dump] Ignored {node} in {os.path.basename(path)}: outside requested scope')
                continue
            if key in protected or key in chosen:
                log(f'[audit/dump] Ignored older/duplicate snapshot for {node}: {os.path.basename(path)}')
                continue
            chosen[key] = {'node': node, 'path': os.path.abspath(path),
                           'timestamp': dt.datetime.fromtimestamp(stamp).isoformat(timespec='seconds'),
                           'time_basis': basis, 'mo_count': len(node_records)}
            records.update(node_records)
            log(f'[audit/dump] Selected {node}: {os.path.basename(path)}; '
                f"{chosen[key]['timestamp']} ({basis}); {len(node_records)} MOs")
    evidence = sorted(chosen.values(), key=lambda row: row['node'].casefold())
    return records, [row['node'] for row in evidence], evidence
