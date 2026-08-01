"""Durable per-file journal for resumable Integration relation scripts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "") or "NODE"


def journal_path(log_dir: str, node_name: str) -> str:
    return os.path.join(
        log_dir, "INTEGRATION_JOURNAL", f"RELATION_{_safe(node_name)}.json",
    )


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data["updated_at"] = now_iso()
    temp = f"{path}.tmp-{os.getpid()}"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _archive(path: str, data: dict) -> None:
    if not os.path.exists(path):
        return
    archive_dir = os.path.join(os.path.dirname(path), "ARCHIVE")
    os.makedirs(archive_dir, exist_ok=True)
    run_id = _safe(str(data.get("run_id", int(time.time()))))
    target = os.path.join(archive_dir, f"{run_id}_{os.path.basename(path)}")
    os.replace(path, target)


def open_or_create(log_dir: str, node_name: str, shortcode: str,
                   input_path: str, remote_files: list[str],
                   remote_dir: str) -> tuple[str, dict, bool]:
    """Return ``(path, journal, resumed)`` for this exact relation input.

    Completed or input-mismatched journals are archived. File order and the
    ZIP hash are both part of identity so stale progress can never skip a new
    relation package that happens to use the same filenames.
    """
    path = journal_path(log_dir, node_name)
    input_hash = sha256_file(input_path)
    filenames = [os.path.basename(p) for p in sorted(remote_files)]
    existing = None
    if os.path.isfile(path):
        try:
            existing = load(path)
        except Exception:
            existing = None
    if existing:
        same = (
            existing.get("input_sha256") == input_hash
            and existing.get("files_order") == filenames
            and existing.get("node_name") == node_name
            and existing.get("status") != "COMPLETED"
        )
        if same:
            return path, existing, True
        _archive(path, existing)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    progress_path = (
        f"{remote_dir}/relation_progress_{_safe(node_name)}_{run_id}.log"
    )
    data = {
        "schema_version": 1,
        "kind": "integration_relation",
        "run_id": run_id,
        "shortcode": shortcode,
        "node_name": node_name,
        "input_file": os.path.abspath(input_path),
        "input_sha256": input_hash,
        "files_order": filenames,
        "remote_progress_path": progress_path,
        "status": "RUNNING",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "items": [
            {
                "index": index,
                "file": os.path.basename(remote),
                "remote_path": remote,
                "marker": hashlib.sha256(remote.encode("utf-8")).hexdigest()[:16],
                "status": "PENDING",
                "attempts": 0,
                "summary": "",
                "local_log": "",
                "started_at": "",
                "completed_at": "",
            }
            for index, remote in enumerate(sorted(remote_files), 1)
        ],
    }
    save(path, data)
    return path, data, False


def item_for(journal: dict, filename: str) -> dict:
    for item in journal.get("items", []):
        if item.get("file") == filename:
            return item
    raise KeyError(filename)
