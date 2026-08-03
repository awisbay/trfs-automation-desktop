"""
integration_queue.py — a durable, ordered queue of integration jobs so an
operator can line up several sites and let NodeCraft run them back-to-back
unattended.

A *job* is one site's full run request: the form fields (SSH creds, node
names, BSC, shortcode) plus the per-node step selection. The queue is a plain
JSON list on disk (``LOG/_QUEUE/integration_queue.json``) so it survives an app
restart and a crash mid-batch — the runner marks each job's status as it goes,
and a resumed app picks up the first ``pending`` one.

This module is pure data (no SSH, no Flet): add / remove / reorder / mark, and
load/save. The GUI drives it; the runner reuses the existing per-site
integration flow one job at a time.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import List, Optional


#: Job lifecycle. ``running`` is transient; a clean finish is ``done`` (even
#: with per-step failures — that's captured in the site's own summary).
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass
class QueueJob:
    shortcode: str
    form: dict                       # the integration form fields for this site
    selected_per_node: dict          # {node_tag: [step_key, ...]}
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: str = STATUS_PENDING
    note: str = ""
    added_at: str = ""
    finished_at: str = ""

    def label(self) -> str:
        n = sum(len(v) for v in (self.selected_per_node or {}).values())
        return f"{self.shortcode or '(no site)'} · {n} step(s)"


def _queue_path(app_dir: str) -> str:
    return os.path.join(app_dir, "LOG", "_QUEUE", "integration_queue.json")


class IntegrationQueue:
    """Ordered, persisted list of :class:`QueueJob`."""

    def __init__(self, app_dir: str):
        self._path = _queue_path(app_dir)
        self.jobs: List[QueueJob] = []
        self.load()

    # ── persistence ─────────────────────────────────────────────
    def load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.jobs = [QueueJob(**j) for j in data.get("jobs", [])]
        except (FileNotFoundError, ValueError, TypeError):
            self.jobs = []

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"updated_at": _now(), "jobs": [asdict(j) for j in self.jobs]},
                      fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    # ── mutations ───────────────────────────────────────────────
    def add(self, shortcode: str, form: dict, selected_per_node: dict,
            note: str = "") -> QueueJob:
        job = QueueJob(
            shortcode=shortcode,
            form=dict(form or {}),
            selected_per_node={k: list(v) for k, v in
                               (selected_per_node or {}).items()},
            note=note, added_at=_now())
        self.jobs.append(job)
        self.save()
        return job

    def remove(self, job_id: str) -> None:
        self.jobs = [j for j in self.jobs if j.id != job_id]
        self.save()

    def move(self, job_id: str, delta: int) -> None:
        i = self._index(job_id)
        if i is None:
            return
        j = max(0, min(len(self.jobs) - 1, i + delta))
        if j != i:
            self.jobs.insert(j, self.jobs.pop(i))
            self.save()

    def clear_finished(self) -> None:
        self.jobs = [j for j in self.jobs
                     if j.status in (STATUS_PENDING, STATUS_RUNNING)]
        self.save()

    def set_status(self, job_id: str, status: str) -> None:
        for j in self.jobs:
            if j.id == job_id:
                j.status = status
                if status in (STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED):
                    j.finished_at = _now()
                break
        self.save()

    def reset_running_to_pending(self) -> None:
        """After a crash, a job left 'running' is retried from the top."""
        changed = False
        for j in self.jobs:
            if j.status == STATUS_RUNNING:
                j.status = STATUS_PENDING
                changed = True
        if changed:
            self.save()

    # ── queries ─────────────────────────────────────────────────
    def next_pending(self) -> Optional[QueueJob]:
        return next((j for j in self.jobs if j.status == STATUS_PENDING), None)

    def counts(self) -> dict:
        c = {STATUS_PENDING: 0, STATUS_RUNNING: 0, STATUS_DONE: 0,
             STATUS_FAILED: 0, STATUS_SKIPPED: 0}
        for j in self.jobs:
            c[j.status] = c.get(j.status, 0) + 1
        return c

    def _index(self, job_id: str) -> Optional[int]:
        for i, j in enumerate(self.jobs):
            if j.id == job_id:
                return i
        return None


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
