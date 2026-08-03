"""Compact persistence for Cut Over recovery and audit.

Raw Moshell output remains in separate log files. The mutable checkpoint keeps
only enough state to reconnect and reconcile safely. At closure it becomes a
write-once manifest plus a SHA-256 sidecar.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Optional

from cutover_model import (
    UNMAPPED,
    CellStatus,
    CutoverCell,
    GroupState,
    GroupStatus,
    RunPhase,
)

SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def config_hash(cfg: dict) -> str:
    raw = json.dumps(cfg or {}, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cell_dict(cell: CutoverCell) -> dict:
    return {
        "node_name": cell.node_name,
        "mo_type": cell.mo_type,
        "cell_dn": cell.cell_dn,
        "rat": cell.rat,
        "prefix_letter": cell.prefix_letter,
        "cell_id": cell.cell_id,
        "sector": cell.sector,
        "band_number": cell.band_number,
        "band_key": cell.band_key,
        "extra_band_numbers": list(cell.extra_band_numbers),
        "group": cell.group,
        "admin_state": cell.admin_state,
        "op_state": cell.op_state,
        "avail_status": cell.avail_status,
        "ue_count": cell.ue_count,
        "ue_peak": cell.ue_peak,
        "flags": cell.flags,
        "was_unlocked_before": cell.was_unlocked_before,
        "already_in_service": cell.already_in_service,
        "was_unlocked_by_run": cell.was_unlocked_by_run,
        "cell_barred": cell.cell_barred,
        "dependency_locked": cell.dependency_locked,
        "traffic_samples": cell.traffic_samples,
        "status": cell.status.value,
        "status_detail": cell.status_detail,
        "attempts": cell.attempts,
        "last_error": cell.last_error,
        "unlock_command": cell.unlock_command,
    }


def snapshot(run, run_id: str, created_at: str, cfg: dict,
             verdict: Optional[str] = None) -> dict:
    try:
        from version import __version__
        app_version = __version__
    except Exception:
        app_version = "unknown"

    alarm_hashes = {
        node: hashlib.sha256((output or "").encode("utf-8")).hexdigest()
        for node, output in run.alarm_baseline.items()
    }
    data = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "app_version": app_version,
        "shortcode": run.shortcode,
        "node_names": list(run.node_names),
        "created_at": created_at,
        "updated_at": now_iso(),
        "config_sha256": config_hash(cfg),
        "phase": run.phase.value,
        "active_group": run.active_group,
        "error": run.error,
        "alarm_baseline_sha256": alarm_hashes,
        "artifacts": dict(run.artifacts),
        "cells": [_cell_dict(c) for c in run.cells],
        "groups": {
            name: {
                "status": group.status.value,
                "message": group.message,
                "screenshot_path": group.screenshot_path,
            }
            for name, group in run.groups.items()
        },
        "final_steps": [
            {
                "key": step.key,
                "label": step.label,
                "status": step.status,
                "detail": step.detail,
                "screenshot_path": step.screenshot_path,
            }
            for step in run.final_steps
        ],
    }
    if verdict:
        data["verdict"] = verdict
        data["finalized_at"] = now_iso()
    return data


def write_json_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp-{os.getpid()}"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def find_unfinished(precutover_root: str, shortcode: str,
                    node_names: list) -> Optional[str]:
    if not os.path.isdir(precutover_root):
        return None
    wanted_nodes = sorted(str(n).strip().upper() for n in node_names if n)
    wanted_site = str(shortcode or "").strip().upper()
    candidates = []
    for run_id in os.listdir(precutover_root):
        checkpoint = os.path.join(precutover_root, run_id, "checkpoint.json")
        manifest = os.path.join(precutover_root, run_id, "manifest.json")
        if not os.path.isfile(checkpoint) or os.path.exists(manifest):
            continue
        try:
            data = load(checkpoint)
        except Exception:
            continue
        nodes = sorted(str(n).strip().upper()
                       for n in data.get("node_names", []) if n)
        site = str(data.get("shortcode", "")).strip().upper()
        if nodes == wanted_nodes and site == wanted_site:
            candidates.append((data.get("updated_at", ""), checkpoint))
    return max(candidates, default=(None, None))[1]


def restore(run, data: dict) -> None:
    """Restore domain state. Live sessions are deliberately never restored."""
    cells = []
    for raw in data.get("cells", []):
        cell = CutoverCell(
            node_name=raw["node_name"],
            mo_type=raw["mo_type"],
            cell_dn=raw["cell_dn"],
            rat=raw.get("rat", "LTE"),
            prefix_letter=raw.get("prefix_letter", ""),
            cell_id=raw.get("cell_id", ""),
            sector=raw.get("sector", ""),
            band_number=int(raw.get("band_number", -1)),
            band_key=raw.get("band_key", ""),
            extra_band_numbers=list(raw.get("extra_band_numbers", [])),
            group=raw.get("group", UNMAPPED),
        )
        for name in (
            "admin_state", "op_state", "avail_status", "ue_count", "ue_peak",
            "flags", "was_unlocked_before", "already_in_service",
            "cell_barred", "dependency_locked", "traffic_samples", "attempts",
            "last_error", "unlock_command", "status_detail",
        ):
            if name in raw:
                setattr(cell, name, raw[name])
        cell.was_unlocked_by_run = bool(raw.get(
            "was_unlocked_by_run",
            raw.get("unlock_command") or raw.get("status") not in
            (None, CellStatus.PENDING.value, CellStatus.SKIPPED.value,
             CellStatus.ALREADY_IN_SERVICE.value),
        ))
        try:
            cell.status = CellStatus(raw.get("status", CellStatus.PENDING.value))
        except ValueError:
            cell.status = CellStatus.ERROR
        cells.append(cell)

    run.cells = cells
    run.by_key = {c.key: c for c in cells}
    run.groups = {}
    raw_groups = data.get("groups", {})
    for name in set(raw_groups) | {c.group for c in cells}:
        raw = raw_groups.get(name, {})
        try:
            status = GroupStatus(raw.get("status", GroupStatus.PENDING.value))
        except ValueError:
            status = GroupStatus.PENDING
        run.groups[name] = GroupState(
            name=name,
            status=status,
            cell_keys=[c.key for c in cells if c.group == name],
            message=raw.get("message", ""),
            screenshot_path=raw.get("screenshot_path", ""),
        )
    run.artifacts = dict(data.get("artifacts", {}))
    run.active_group = data.get("active_group", "")
    run.error = data.get("error", "")
    run.sessions = {}
    run.cancel_event.clear()
    run.phase = RunPhase.FAILED
    run.touch()


def finalize(checkpoint_path: str, data: dict, verdict: str) -> str:
    """Write a manifest once, checksum it, then retire the checkpoint."""
    folder = os.path.dirname(checkpoint_path)
    manifest_path = os.path.join(folder, "manifest.json")
    if os.path.exists(manifest_path):
        return manifest_path
    final_data = dict(data)
    final_data.pop("updated_at", None)
    final_data["verdict"] = verdict
    final_data["finalized_at"] = now_iso()
    encoded = (json.dumps(final_data, ensure_ascii=False, indent=2,
                          sort_keys=True) + "\n").encode("utf-8")
    os.makedirs(folder, exist_ok=True)
    with open(manifest_path, "xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    digest = hashlib.sha256(encoded).hexdigest()
    with open(manifest_path + ".sha256", "x", encoding="ascii") as handle:
        handle.write(f"{digest}  manifest.json\n")
    try:
        os.remove(checkpoint_path)
    except FileNotFoundError:
        pass
    return manifest_path
