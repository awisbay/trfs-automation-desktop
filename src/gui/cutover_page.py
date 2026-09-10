"""
NodeCraft GUI — Cut Over page.

Layout, and why:

    Cut Over — <SHORTCODE>                      [ Cancel ]  [ Back ]
    ───────────────────────────────────────────────────────────────
      LB — 2 cells · 2 enabled · 2 traffic          [ Unlock LB ]
          ✓ NODEA  EUtranCellFDD=…Y-11  L900  enabled  UE 14
      MB — 2 cells · pending                        [ Unlock MB ]
      HB — 2 cells · pending                        [ Unlock HB ]
    ───────────────────────────────────────────────────────────────
    [ Unlock All ] [ Verify ]        log panel (bounded)

Each per-group Unlock button lives **in its own group header** rather than in
a shared row of four, so it is never ambiguous which cells a button acts on
and the button stays next to its cells as the list scrolls. ``Unlock All``
sits in the footer bar because it is the only action that spans groups.

Threading contract, copied from ``IntegrationRunPage`` for the same reasons:
worker threads never call ``page.update()``; they mutate the shared
``CutoverRun`` under its lock and push to queues, and a single asyncio flush
loop started with ``page.run_task`` does every repaint. Status icons are
static — an animated ``ft.ProgressRing`` per row repaints at ~60 fps and, with
dozens of cells, pegs a CPU core.
"""
import asyncio
import os
import sys
import threading
from typing import Optional

import flet as ft

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from cutover_model import (
    GSM,
    STATUS_ICON_STATE,
    UNMAPPED,
    CellStatus,
    GroupStatus,
    RunPhase,
)
from cutover_runner import CutoverEngine, load_cutover_config
from gui.theme import (
    ACCENT,
    ACCENT_WARM,
    BG_BOTTOM,
    BG_TOP,
    BORDER,
    DANGER,
    INFO,
    PANEL,
    PANEL_RAISED,
    SUCCESS,
    TEXT,
    TEXT_MUTED,
    background_gradient,
    panel,
)

#: Group accent colours — LB/MB/HB read as low→high at a glance.
GROUP_ACCENT = {"LB": INFO, "MB": ACCENT, "HB": ACCENT_WARM, GSM: SUCCESS,
                UNMAPPED: TEXT_MUTED}

_ICONS = {
    "pending": (ft.Icons.RADIO_BUTTON_UNCHECKED, TEXT_MUTED),
    "running": (ft.Icons.HOURGLASS_TOP, ACCENT),
    "done": (ft.Icons.CHECK_CIRCLE, SUCCESS),
    "warn": (ft.Icons.WARNING_AMBER_ROUNDED, ACCENT_WARM),
    "error": (ft.Icons.ERROR, DANGER),
    "skip": (ft.Icons.REMOVE_CIRCLE_OUTLINE, TEXT_MUTED),
}

_LOG_MAX = 300
_LOG_TRIM = 250


#: A port VSWR above this reads as a problem (red).
VSWR_WARN = 1.40

#: Fixed column widths so the cell rows and their column header line up. The
#: VSWR column has no fixed width — it grows with the number of ports and the
#: row is left-packed with a trailing spacer, so the right edge stays ragged.
_W_NODE = 180
_W_NODE_MAX = 330
_W_MO = 300
_W_MO_MAX = 520
_W_BAND = 64
_W_STATE = 150
_W_TRAFFIC = 64


class _CellRow:
    """One cell: status icon, node, MO, band chip, state, UE count, VSWR."""

    def __init__(self, cell):
        self.key = cell.key
        self._icon = ft.Icon(ft.Icons.RADIO_BUTTON_UNCHECKED, size=16,
                             color=TEXT_MUTED)
        self._node = ft.Text(cell.node_name, size=11, color=TEXT_MUTED,
                             width=_W_NODE, no_wrap=True, max_lines=1,
                             overflow=ft.TextOverflow.ELLIPSIS,
                             tooltip=cell.node_name)
        self._mo = ft.Text(cell.mo_ref, size=12, color=TEXT, width=_W_MO,
                           no_wrap=True, max_lines=1,
                           overflow=ft.TextOverflow.ELLIPSIS, selectable=True,
                           tooltip=cell.mo_ref)
        self._band = ft.Container(
            content=ft.Text(cell.band_key or "?", size=10, color=TEXT,
                            weight=ft.FontWeight.W_600),
            bgcolor=ft.Colors.with_opacity(0.14, GROUP_ACCENT.get(cell.group, ACCENT)),
            border=ft.Border.all(
                1, ft.Colors.with_opacity(0.30, GROUP_ACCENT.get(cell.group, ACCENT))),
            border_radius=999,
            padding=ft.Padding.symmetric(horizontal=8, vertical=2),
            width=_W_BAND,
            alignment=ft.Alignment(0, 0),
        )
        self._state = ft.Text("—", size=11, color=TEXT_MUTED, width=_W_STATE,
                              no_wrap=True, max_lines=1,
                              overflow=ft.TextOverflow.ELLIPSIS)
        # Traffic (UE) and VSWR sit right after the cell, left-aligned. VSWR has
        # no fixed width — it grows/shrinks with the port count, and the trailing
        # spacer keeps the row left-packed so the right edge is ragged.
        self._ue = ft.Text("—", size=11, color=TEXT_MUTED, width=_W_TRAFFIC,
                           text_align=ft.TextAlign.LEFT)
        self._vswr = ft.Text("—", size=10, color=TEXT_MUTED, no_wrap=True,
                             text_align=ft.TextAlign.LEFT)

        self.control = ft.Container(
            padding=ft.Padding.symmetric(horizontal=10, vertical=5),
            border_radius=8,
            content=ft.Row(
                [self._icon, self._node, self._mo, self._band,
                 self._state, self._ue, self._vswr,
                 ft.Container(expand=True)],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def set_identity_widths(self, node_width: int, mo_width: int) -> None:
        """Keep the two variable identity columns aligned with their header."""
        self._node.width = node_width
        self._mo.width = mo_width

    def refresh(self, cell) -> None:
        self._node.value = cell.node_name
        self._node.tooltip = cell.node_name
        icon_state = STATUS_ICON_STATE.get(cell.status, "pending")
        icon_name, colour = _ICONS[icon_state]
        self._icon.name = icon_name
        self._icon.color = colour

        # Show the plain administrativeState/operationalState, e.g.
        # "UNLOCKED/ENABLED" or "UNLOCKED/DISABLED", whenever both are known —
        # short and consistent, one line. Fall back to the status detail only
        # for states with no admin/op yet (pending) or that aren't about the
        # cell's live state (relocked / cancelled / failed / GSM).
        admin = (getattr(cell, "admin_state", "") or "").upper()
        op = (getattr(cell, "op_state", "") or "").upper()
        _plain = cell.status not in (
            CellStatus.RELOCKED, CellStatus.CANCELLED, CellStatus.UNLOCK_FAILED,
            CellStatus.ERROR, CellStatus.SKIPPED)
        if admin and op and _plain and getattr(cell, "rat", "") != "GSM":
            detail = f"{admin}/{op}"
        else:
            detail = cell.status_detail or cell.status.value.replace("_", " ")
        self._state.value = detail
        # For LTE/NR the live state is more useful than the workflow icon when
        # choosing the text colour: enabled cells should read green at a
        # glance, while an unlocked cell that remains disabled is a fault and
        # stays red.
        if getattr(cell, "rat", "") != "GSM" and admin == "UNLOCKED":
            self._state.color = SUCCESS if op == "ENABLED" else DANGER
        else:
            self._state.color = (
                SUCCESS if icon_state == "done" else
                DANGER if icon_state == "error" else
                ACCENT_WARM if icon_state == "warn" else TEXT_MUTED
            )
        self._ue.value = "—" if cell.ue_count is None else str(cell.ue_count)
        if cell.ue_count is None:
            self._ue.color = TEXT_MUTED
        elif cell.is_live_enabled and cell.ue_count == 0:
            self._ue.color = DANGER
        elif cell.ue_count > 0:
            self._ue.color = SUCCESS
        else:
            self._ue.color = TEXT_MUTED
        self._refresh_vswr(cell)
        self.control.bgcolor = (
            ft.Colors.with_opacity(0.07, ACCENT)
            if icon_state == "running" else None
        )
        # A cell that was already carrying customers before this run is not
        # ours — dim the whole row so it reads as "don't touch" at a glance.
        self.control.opacity = 0.55 if cell.already_in_service else 1.0
        self._mo.color = TEXT_MUTED if cell.already_in_service else TEXT
        # GSM rows carry the GeranCell name only — it stands in for both the
        # GeranCell (BSC) and its GsmSector (node).
        if getattr(cell, "rat", "") == "GSM":
            self._mo.value = cell.cell_dn
            self._mo.tooltip = cell.cell_dn
        else:
            self._mo.value = cell.mo_ref
            self._mo.tooltip = cell.mo_ref

    def _refresh_vswr(self, cell) -> None:
        """Per-RF-port VSWR next to the cell, e.g. ``A1.13 B1.15 C1.12 D1.30``.

        Coloured danger if any port is at/above :data:`VSWR_WARN`. A radio that
        reports no numeric VSWR (AAS/AIR ``-``) shows ``n/a``; a cell not yet
        seen by sdirc shows ``—``."""
        ports = getattr(cell, "vswr_ports", None) or []
        if not ports:
            self._vswr.value = "—"
            self._vswr.color = TEXT_MUTED
            self._vswr.tooltip = None
            return
        nums = [p for p in ports if isinstance(p.get("vswr"), (int, float))]
        if not nums:
            self._vswr.value = "VSWR n/a"
            self._vswr.color = TEXT_MUTED
            self._vswr.tooltip = "Radio does not report a per-port VSWR (AAS/AIR)."
            return
        worst = max(p["vswr"] for p in nums)
        self._vswr.value = ", ".join(
            f"{p['port']}={p['vswr']:.2f}" for p in nums)
        self._vswr.color = DANGER if worst > VSWR_WARN else SUCCESS
        self._vswr.tooltip = "  ".join(
            f"Port {p['port']}: VSWR {p['vswr']:.2f}"
            + (f" (RL {p['rl']:.1f} dB)" if isinstance(p.get("rl"), (int, float)) else "")
            for p in nums) + f"\nWorst: {worst:.2f}"


class _GroupSection:
    """A band group: coloured header with counts, Unlock and Re-lock buttons."""

    def __init__(self, name: str, on_unlock, on_relock=None, on_evidence=None,
                 on_lock=None):
        self.name = name
        self._on_unlock = on_unlock      # (group, sector=None)
        self._on_relock = on_relock      # (group, sector=None)
        self._on_lock = on_lock          # (group, sector=None) — lock what's up
        self._sector_buttons: dict = {}  # sector -> ElevatedButton
        self._sector_ctrls: dict = {}    # sector -> (icon, text)
        self._sector_mode: dict = {}     # sector -> "unlock" | "lock"
        accent = GROUP_ACCENT.get(name, ACCENT)

        self.title = ft.Text(name, size=15, weight=ft.FontWeight.BOLD, color=accent)
        self.counts = ft.Text("no cells", size=11, color=TEXT_MUTED)
        self.status_chip = ft.Container(visible=False)
        # Label is a Row(Icon, Text) used as the button's `content` rather than
        # the `text`/`icon` shorthand: in Flet 0.84 reassigning a button's
        # `.text`/`.icon` does not reliably re-render (colour flips but the word
        # "Unlock" lingers), whereas updating an Icon's name and a Text's value
        # always repaints. This is what lets the button truly become "Lock".
        self._btn_icon = ft.Icon(ft.Icons.LOCK_OPEN_ROUNDED, size=18,
                                 color="#06242A")
        self._btn_text = ft.Text(f"Unlock All {name}", color="#06242A",
                                 weight=ft.FontWeight.W_600)
        self.button = ft.ElevatedButton(
            content=ft.Row([self._btn_icon, self._btn_text], tight=True,
                           spacing=8),
            disabled=True,
            style=ft.ButtonStyle(
                bgcolor=accent,
                color="#06242A",
                shape=ft.RoundedRectangleBorder(radius=12),
                padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            ),
            on_click=lambda e, g=name: on_unlock(g),
        )
        # Rollback. Hidden until this group actually has something to undo, so
        # it never sits there inviting a click that would do nothing — but
        # present the moment it matters, which is when traffic has not shown up
        # and the operator needs a way back that isn't hand-typing MOs.
        self.relock_button = ft.OutlinedButton(
            "Re-lock",
            icon=ft.Icons.LOCK_OUTLINE,
            visible=False,
            tooltip="Lock the cells this run unlocked (rollback)",
            style=ft.ButtonStyle(
                color=DANGER,
                side=ft.BorderSide(1, ft.Colors.with_opacity(0.55, DANGER)),
                shape=ft.RoundedRectangleBorder(radius=12),
                padding=ft.Padding.symmetric(horizontal=12, vertical=12),
            ),
            on_click=(lambda e, g=name: on_relock(g)) if on_relock else None,
        )
        # Evidence → WhatsApp. Appears once the group has cells up, so the
        # operator can share the traffic + alarm screenshot to the group chat
        # at any time (the non-blocking flow no longer does it automatically).
        self.evidence_button = ft.OutlinedButton(
            "Evidence",
            icon=ft.Icons.CHAT_ROUNDED,
            visible=False,
            tooltip="Build the traffic + alarm screenshot and share it to "
                    "WhatsApp",
            style=ft.ButtonStyle(
                color=SUCCESS,
                side=ft.BorderSide(1, ft.Colors.with_opacity(0.55, SUCCESS)),
                shape=ft.RoundedRectangleBorder(radius=12),
                padding=ft.Padding.symmetric(horizontal=12, vertical=12),
            ),
            on_click=(lambda e, g=name: on_evidence(g)) if on_evidence else None,
        )
        self.rows_column = ft.Column([], spacing=2)
        # Per-(band group × sector) unlock buttons — one per sector that
        # actually has a cell in this group, built on demand from discovery.
        self._sector_accent = accent
        self.sector_row = ft.Row([], spacing=6, wrap=True, run_spacing=6,
                                 visible=False)
        #: When set, the counts line keeps this text instead of live counts.
        self.static_note = ""

        header = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            bgcolor=ft.Colors.with_opacity(0.10, accent),
            border_radius=10,
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.LAYERS_OUTLINED, size=18, color=accent),
                    self.title,
                    self.counts,
                    self.status_chip,
                    self.sector_row,
                    ft.Container(expand=True),
                    self.evidence_button,
                    self.relock_button,
                    self.button,
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        # Thin column header so Traffic and VSWR are labelled. Widths match the
        # cell row so the labels sit above their columns.
        def _hd(label, width=None):
            return ft.Text(label, size=9, color=TEXT_MUTED,
                           weight=ft.FontWeight.W_600, width=width, no_wrap=True)

        self._node_header = _hd("Node", _W_NODE)
        self._mo_header = _hd("Cell", _W_MO)
        self.col_header = ft.Container(
            padding=ft.Padding.only(left=10, right=10, top=2, bottom=0),
            content=ft.Row(
                [ft.Container(width=16), self._node_header,
                 self._mo_header, ft.Container(width=_W_BAND),
                 _hd("State", _W_STATE), _hd("Traffic", _W_TRAFFIC),
                 _hd("VSWR")],
                spacing=10),
        )
        self.control = ft.Column(
            [header, self.col_header, self.rows_column], spacing=4)

    def set_identity_widths(self, node_width: int, mo_width: int) -> None:
        self._node_header.width = node_width
        self._mo_header.width = mo_width

    def set_sectors(self, sectors: list, unlockable_by_sector: dict,
                    busy: bool, ready: bool, lockable_by_sector: dict = None) -> None:
        """One small button per sector present in this group (``S1``/``S2``/…).
        Each toggles Unlock ↔ Lock the same way the group button does: it
        unlocks while that sector still has cells to unlock, and becomes Lock
        once the sector is fully up."""
        lockable_by_sector = lockable_by_sector or {}
        if not sectors:
            self.sector_row.visible = False
            return
        if set(sectors) != set(self._sector_buttons):
            self._sector_buttons = {}
            self._sector_ctrls = {}      # sector -> (icon, text)
            controls = []
            for s in sectors:
                icon = ft.Icon(ft.Icons.LOCK_OPEN_ROUNDED, size=16,
                               color=self._sector_accent)
                text = ft.Text(f"Unlock S{s}", size=13,
                               color=self._sector_accent)
                btn = ft.OutlinedButton(
                    content=ft.Row([icon, text], tight=True, spacing=6),
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=10),
                        padding=ft.Padding.symmetric(horizontal=10, vertical=8),
                    ),
                )
                self._sector_buttons[s] = btn
                self._sector_ctrls[s] = (icon, text)
                controls.append(btn)
            self.sector_row.controls = controls
        self.sector_row.visible = True
        for s, btn in self._sector_buttons.items():
            un = int(unlockable_by_sector.get(s, 0))
            lo = int(lockable_by_sector.get(s, 0))
            icon, text = self._sector_ctrls[s]

            def _sec_style(col):
                return ft.ButtonStyle(
                    color=col,
                    side=ft.BorderSide(1, ft.Colors.with_opacity(0.5, col)),
                    shape=ft.RoundedRectangleBorder(radius=10),
                    padding=ft.Padding.symmetric(horizontal=10, vertical=8))

            if un > 0 or not (lo > 0 and self._on_lock is not None):
                # Unlock mode (or nothing to do — stays an inert Unlock button).
                text.value = f"Unlock S{s}"
                text.color = self._sector_accent
                icon.name = ft.Icons.LOCK_OPEN_ROUNDED
                icon.color = self._sector_accent
                btn.style = _sec_style(self._sector_accent)
                btn.on_click = (lambda e, g=self.name, sec=s:
                                self._on_unlock(g, sec))
                btn.disabled = busy or not ready or un == 0
                btn.tooltip = (f"Unlock the {un} S{s} {self.name} cell(s)" if un
                               else f"No unlockable S{s} cell in {self.name}")
            else:
                text.value = f"Lock S{s}"
                text.color = DANGER
                icon.name = ft.Icons.LOCK_ROUNDED
                icon.color = DANGER
                btn.style = _sec_style(DANGER)
                btn.on_click = (lambda e, g=self.name, sec=s:
                                self._on_lock(g, sec))
                btn.disabled = busy or not ready
                btn.tooltip = f"Lock the {lo} S{s} {self.name} cell(s) that are unlocked"

    def set_counts(self, counts: dict, status: GroupStatus, busy: bool,
                   unlockable: int, relockable: int = 0, lockable: int = 0) -> None:
        total = counts["total"]
        if self.static_note:
            self.counts.value = self.static_note
        elif total == 0:
            self.counts.value = "no cells"
        else:
            parts = [f"{total} cell{'s' if total != 1 else ''}"]
            if counts["enabled"]:
                parts.append(f"{counts['enabled']} enabled")
            if counts["traffic_ok"]:
                parts.append(f"{counts['traffic_ok']} traffic")
            if counts["failed"]:
                parts.append(f"{counts['failed']} failed")
            self.counts.value = " · ".join(parts)

        label, colour = {
            GroupStatus.DONE: ("done", SUCCESS),
            GroupStatus.DONE_WITH_FAILURES: ("partial", ACCENT_WARM),
            GroupStatus.FAILED: ("failed", DANGER),
            GroupStatus.RUNNING: ("running", ACCENT),
            GroupStatus.CANCELLED: ("cancelled", TEXT_MUTED),
        }.get(status, (None, None))
        if label:
            self.status_chip.visible = True
            self.status_chip.content = ft.Text(label, size=10, color=colour,
                                               weight=ft.FontWeight.W_600)
            self.status_chip.bgcolor = ft.Colors.with_opacity(0.14, colour)
            self.status_chip.border_radius = 999
            self.status_chip.padding = ft.Padding.symmetric(horizontal=8, vertical=2)
        else:
            self.status_chip.visible = False

        # Primary button toggles Unlock ↔ Lock: while there is anything to
        # unlock it unlocks; once the group is fully up (nothing to unlock) it
        # becomes a Lock button that locks what is unlocked. Reassign a fresh
        # ButtonStyle (not an in-place bgcolor mutation) so Flet re-renders the
        # whole button — otherwise the colour flips but the text/icon lag.
        accent = GROUP_ACCENT.get(self.name, ACCENT)

        def _btn_style(bg):
            return ft.ButtonStyle(
                bgcolor=bg, color="#06242A",
                shape=ft.RoundedRectangleBorder(radius=12),
                padding=ft.Padding.symmetric(horizontal=16, vertical=12))

        if unlockable > 0:
            self._btn_text.value = f"Unlock All {self.name}"
            self._btn_icon.name = ft.Icons.LOCK_OPEN_ROUNDED
            self.button.style = _btn_style(accent)
            self.button.disabled = busy
            self.button.on_click = lambda e, g=self.name: self._on_unlock(g)
            self.button.tooltip = (
                "Another action is running" if busy else
                f"Unlock the {unlockable} {self.name} cell(s) on this site")
        elif lockable > 0 and self._on_lock is not None:
            self._btn_text.value = f"Lock All {self.name}"
            self._btn_icon.name = ft.Icons.LOCK_ROUNDED
            self.button.style = _btn_style(DANGER)
            self.button.disabled = busy
            self.button.on_click = lambda e, g=self.name: self._on_lock(g)
            self.button.tooltip = (
                "Another action is running" if busy else
                f"Lock the {lockable} {self.name} cell(s) that are unlocked")
        else:
            self._btn_text.value = f"Unlock All {self.name}"
            self._btn_icon.name = ft.Icons.LOCK_OPEN_ROUNDED
            self.button.style = _btn_style(accent)
            self.button.disabled = True
            self.button.on_click = lambda e, g=self.name: self._on_unlock(g)
            self.button.tooltip = "No cells in this band group"

        # Rollback stays available only while there is still something to unlock
        # (mid-cutover). Once the group is fully up, the primary Lock button
        # covers taking it down, so the extra Re-lock button is hidden.
        show_relock = relockable > 0 and unlockable > 0
        self.relock_button.visible = show_relock
        self.relock_button.disabled = busy or not show_relock
        self.relock_button.tooltip = (
            f"Roll back: lock the {relockable} {self.name} cell(s) this run "
            f"unlocked. Cells that were already in service are not touched."
        )

        up = counts["enabled"] + counts["traffic_ok"]
        self.evidence_button.visible = up > 0
        self.evidence_button.disabled = busy
        self.evidence_button.tooltip = (
            f"Build the traffic + alarm screenshot for {self.name} "
            f"({up} cell(s) up) and share it to WhatsApp"
        )


class CutOverPage:
    def __init__(self, page: ft.Page):
        self.page = page
        self.form = getattr(page, "integration_form", {}) or {}
        self.shortcode = str(self.form.get("shortcode", "")).strip()

        self.cfg = load_cutover_config()
        self.engine = CutoverEngine(
            self.form,
            cfg=self.cfg,
            confirm_cb=self._confirm_unlock,
            wait_for_user=self._ask_continue,
        )
        self.run = self.engine.run

        self._rows: dict = {}          # cell key -> _CellRow
        self._sections: dict = {}      # group name -> _GroupSection
        self._dialog_lock = threading.Lock()
        self._finished = False
        self._last_version = -1

    # ── view ─────────────────────────────────────────────────────
    def build(self) -> ft.View:
        self.page.title = f"NodeCraft — {self.shortcode or 'Cut Over'} (Cut Over)"

        self.status_text = ft.Text(
            "Ready — click Pre HC to create CV and run preHC (no modump)",
            size=13, color=ACCENT,
        )
        self.start_hc_btn = ft.ElevatedButton(
            "Pre HC",
            icon=ft.Icons.HEALTH_AND_SAFETY_OUTLINED,
            tooltip="Create CV, run preHC (download its logfile), then discover cells",
            style=ft.ButtonStyle(
                bgcolor=ACCENT,
                color="#06242A",
                shape=ft.RoundedRectangleBorder(radius=12),
                padding=ft.Padding.symmetric(horizontal=18, vertical=12),
            ),
            on_click=self._on_start_hc,
        )
        self.trfs_btn = ft.OutlinedButton(
            "TRFS Log",
            icon=ft.Icons.RECEIPT_LONG_OUTLINED,
            disabled=True,
            tooltip="Run LABEL.mos then the per-node TRFS script (chosen from "
                    "each node's techs) and download the log folders",
            style=ft.ButtonStyle(
                color=ACCENT,
                side=ft.BorderSide(1, ft.Colors.with_opacity(0.6, ACCENT)),
                shape=ft.RoundedRectangleBorder(radius=12),
                padding=ft.Padding.symmetric(horizontal=18, vertical=12),
            ),
            on_click=self._on_trfs_log,
        )
        self.post_hc_btn = ft.OutlinedButton(
            "Post HC",
            icon=ft.Icons.HEALTH_AND_SAFETY_OUTLINED,
            disabled=True,
            tooltip="Post-cutover check: create the Post_CutOver CV, run postHC "
                    "(download its logfile), then re-read cell status",
            style=ft.ButtonStyle(
                color=ACCENT_WARM,
                side=ft.BorderSide(1, ft.Colors.with_opacity(0.6, ACCENT_WARM)),
                shape=ft.RoundedRectangleBorder(radius=12),
                padding=ft.Padding.symmetric(horizontal=18, vertical=12),
            ),
            on_click=self._on_post_hc,
        )
        self.start_unlock_btn = ft.OutlinedButton(
            "Start Unlock",
            icon=ft.Icons.LOCK_OPEN_ROUNDED,
            tooltip="Skip preparation (CV, modump, preHC) — go straight to "
                    "discovering cells and their status so you can unlock now",
            style=ft.ButtonStyle(
                color=ACCENT,
                side=ft.BorderSide(1, ft.Colors.with_opacity(0.6, ACCENT)),
                shape=ft.RoundedRectangleBorder(radius=12),
                padding=ft.Padding.symmetric(horizontal=18, vertical=12),
            ),
            on_click=self._on_start_unlock,
        )
        self.cancel_btn = ft.OutlinedButton(
            "Cancel", icon=ft.Icons.STOP_CIRCLE_OUTLINED,
            style=ft.ButtonStyle(color=DANGER,
                                 side=ft.BorderSide(1, ft.Colors.with_opacity(0.6, DANGER)),
                                 shape=ft.RoundedRectangleBorder(radius=12)),
            on_click=self._on_cancel,
        )
        back_btn = ft.OutlinedButton(
            "Back", icon=ft.Icons.ARROW_BACK,
            style=ft.ButtonStyle(color=TEXT_MUTED,
                                 side=ft.BorderSide(1, BORDER),
                                 shape=ft.RoundedRectangleBorder(radius=12)),
            on_click=self._on_back,
        )

        header = ft.Row(
            [
                ft.Icon(ft.Icons.SWAP_HORIZ_ROUNDED, size=26, color=ACCENT),
                ft.Column(
                    [
                        ft.Text(f"Cut Over — {self.shortcode or 'site'}", size=20,
                                weight=ft.FontWeight.BOLD, color=TEXT),
                        self.status_text,
                    ],
                    spacing=2,
                ),
                ft.Container(expand=True),
                self.start_hc_btn,
                self.trfs_btn,
                self.post_hc_btn,
                self.start_unlock_btn,
                self.cancel_btn,
                back_btn,
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        if self.cfg.get("dry_run"):
            dry_banner = ft.Container(
                visible=True,
                padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                bgcolor=ft.Colors.with_opacity(0.14, ACCENT_WARM),
                border_radius=10,
                content=ft.Row(
                    [ft.Icon(ft.Icons.SCIENCE_OUTLINED, size=16, color=ACCENT_WARM),
                     ft.Text("DRY RUN — commands are logged, not sent to the node.",
                             size=12, color=ACCENT_WARM, weight=ft.FontWeight.W_600)],
                    spacing=8),
            )
        else:
            dry_banner = ft.Container(visible=False)

        gsm_on = bool(self.cfg.get("gsm", {}).get("enabled", True))
        section_names = list(self.cfg["group_order"])
        if gsm_on:
            section_names.append(GSM)
        for name in section_names + [UNMAPPED]:
            self._sections[name] = _GroupSection(
                name, self._on_unlock_group, self._on_relock_group,
                self._on_share_evidence, on_lock=self._on_lock_group)
        if gsm_on:
            gsm_sec = self._sections[GSM]
            gsm_sec._btn_text.value = "Unlock All GSM"
            gsm_sec.title.value = "GSM (GeranCell)"
            # GSM has no traffic screenshot evidence — its state is GeranCell +
            # Trx, not UE/alarms — so hide the WhatsApp evidence button.
            gsm_sec.evidence_button.visible = False
        # The unmapped bucket has no button by design — these cells are
        # deliberately unreachable from any unlock action. Say why, so an
        # operator who expected to see them in a group isn't left guessing.
        unmapped = self._sections[UNMAPPED]
        unmapped.button.visible = False
        unmapped.control.visible = False
        unmapped.title.value = "Not in a band group"
        unmapped.title.size = 13
        unmapped.static_note = ("these bands aren't listed in "
                                "cutover.band_groups — never unlocked")

        self.cells_column = ft.Column(
            [s.control for s in self._sections.values()],
            spacing=14, scroll=ft.ScrollMode.AUTO, expand=True,
        )

        self.unlock_all_btn = ft.ElevatedButton(
            "Unlock All", icon=ft.Icons.PLAYLIST_PLAY_ROUNDED, disabled=True,
            tooltip="Run LB, then MB, then HB in order",
            style=ft.ButtonStyle(bgcolor=ACCENT, color="#06242A",
                                 shape=ft.RoundedRectangleBorder(radius=12),
                                 padding=ft.Padding.symmetric(horizontal=20, vertical=14)),
            on_click=self._on_unlock_all,
        )
        self.sector_unlock_btns = {}
        for sector in ("1", "2", "3"):
            self.sector_unlock_btns[sector] = ft.OutlinedButton(
                f"Unlock S{sector}", icon=ft.Icons.LOCK_OPEN_ROUNDED,
                visible=False, disabled=True,
                tooltip=(f"Unlock sector S{sector} across LB, MB, HB and GSM; "
                         "other sectors stay locked"),
                style=ft.ButtonStyle(
                    color=ACCENT,
                    side=ft.BorderSide(1, ft.Colors.with_opacity(0.6, ACCENT)),
                    shape=ft.RoundedRectangleBorder(radius=12),
                    padding=ft.Padding.symmetric(horizontal=14, vertical=14),
                ),
                on_click=lambda e, s=sector: self._on_unlock_sector(s),
            )
        self.verify_btn = ft.OutlinedButton(
            "Run Verification", icon=ft.Icons.FACT_CHECK_OUTLINED, disabled=True,
            tooltip="Run the post-cutover checks from config.json",
            style=ft.ButtonStyle(color=ACCENT,
                                 side=ft.BorderSide(1, ft.Colors.with_opacity(0.6, ACCENT)),
                                 shape=ft.RoundedRectangleBorder(radius=12),
                                 padding=ft.Padding.symmetric(horizontal=18, vertical=14)),
            on_click=self._on_verify,
        )
        self.relock_all_btn = ft.OutlinedButton(
            "Roll Back All", icon=ft.Icons.UNDO_ROUNDED, visible=False,
            tooltip="Lock every cell this run unlocked",
            style=ft.ButtonStyle(color=DANGER,
                                 side=ft.BorderSide(1, ft.Colors.with_opacity(0.55, DANGER)),
                                 shape=ft.RoundedRectangleBorder(radius=12),
                                 padding=ft.Padding.symmetric(horizontal=18, vertical=14)),
            on_click=self._on_relock_all,
        )
        self.summary_text = ft.Text("", size=12, color=TEXT_MUTED)

        action_bar = ft.Row(
            [*self.sector_unlock_btns.values(), self.unlock_all_btn,
             self.verify_btn, self.relock_all_btn,
             ft.Container(width=8), self.summary_text],
            spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER, wrap=True,
            run_spacing=10,
        )

        self.log_column = ft.Column([], spacing=1, scroll=ft.ScrollMode.AUTO,
                                    auto_scroll=True, expand=True)

        body = ft.Container(
            expand=True,
            gradient=background_gradient(),
            padding=ft.Padding.symmetric(horizontal=24, vertical=18),
            content=ft.Column(
                [
                    header,
                    dry_banner,
                    panel(self.cells_column, bgcolor=PANEL, padding=16, expand=True),
                    panel(action_bar, bgcolor=PANEL_RAISED, padding=14),
                    ft.Container(
                        height=170,
                        content=panel(self.log_column, bgcolor=PANEL, padding=12,
                                      expand=True),
                    ),
                ],
                spacing=12,
                expand=True,
            ),
        )

        if self.engine.recovery_checkpoint:
            self.status_text.value = "Unfinished Cut Over found — action required"
            self.start_hc_btn.disabled = True
            self.start_unlock_btn.disabled = True
            self.page.run_task(self._show_recovery_prompt)
        else:
            self._refresh_chrome()
        self.page.run_task(self._flush_loop)

        return ft.View(route="/cutover", padding=0, spacing=0, bgcolor=BG_TOP,
                       controls=[body])

    async def _show_recovery_prompt(self):
        """Offer only reconciled recovery actions; never silently resume."""
        await asyncio.sleep(0.2)
        info = self.engine.recovery_info()
        cells = info.get("cells", [])
        attempted = sum(1 for c in cells if c.get("was_unlocked_by_run"))

        def _choose(mode: str):
            self._close_dialog(dlg)
            if mode == "close":
                try:
                    path = self.engine.close_recovery_as_incomplete()
                    self.status_text.value = (
                        "Previous run closed — click Start HC for a new run"
                    )
                    self.engine.log(f"Previous run manifest saved: {path}")
                    self._refresh_chrome()
                    self.page.update()
                except Exception as exc:
                    self._alert("Recovery", f"Could not close checkpoint: {exc}")
                return
            self.engine.recover(mode)
            self._refresh_chrome()
            self.page.update()

        body = ft.Column(
            [
                ft.Text(
                    "An unfinished Cut Over exists for this site. The tool "
                    "will reconnect and read back every saved cell before any "
                    "resume or rollback action.",
                    size=13, color=TEXT,
                ),
                ft.Text(
                    f"Run: {info.get('run_id', '?')}\n"
                    f"Last phase: {info.get('phase', '?')}\n"
                    f"Cells: {len(cells)} · attempted by run: {attempted}",
                    size=11, color=TEXT_MUTED, selectable=True,
                ),
            ],
            spacing=10, tight=True, width=560,
        )
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Recover unfinished Cut Over", color=ACCENT_WARM),
            content=body,
            actions=[
                ft.TextButton(
                    "Close incomplete & start new",
                    on_click=lambda e: _choose("close"),
                ),
                ft.OutlinedButton(
                    "Reconcile & Roll Back",
                    on_click=lambda e: _choose("rollback"),
                ),
                ft.ElevatedButton(
                    "Reconcile & Resume",
                    on_click=lambda e: _choose("resume"),
                ),
            ],
        )
        self._show_dialog(dlg)

    # ── the single repainter ─────────────────────────────────────
    async def _flush_loop(self):
        """The only place ``page.update()`` is called for this page."""
        while not self._finished:
            try:
                self._flush_once()
            except Exception:
                pass
            await asyncio.sleep(0.4)
        try:
            self._flush_once()
        except Exception:
            pass

    def _flush_once(self) -> None:
        dirty = False

        ingested = 0
        while not self.engine.log_queue.empty() and ingested < 200:
            try:
                msg = self.engine.log_queue.get_nowait()
            except Exception:
                break
            ingested += 1
            dirty = True
            self.log_column.controls.append(
                ft.Text(msg, size=11, color=TEXT_MUTED, selectable=True,
                        font_family="Consolas"))
        if len(self.log_column.controls) > _LOG_MAX:
            self.log_column.controls = self.log_column.controls[-_LOG_TRIM:]

        while not self.engine.event_queue.empty():
            try:
                event = self.engine.event_queue.get_nowait()
            except Exception:
                break
            dirty = True
            try:
                self._handle_event(event)
            except Exception:
                pass

        run = self.run
        with run.lock:
            changed = run.version != self._last_version
            keys = set(run.dirty_cells)
            meta = run.dirty_meta
            if changed:
                run.dirty_cells.clear()
                run.dirty_meta = False
                self._last_version = run.version

        if changed:
            dirty = True
            self._sync_rows()
            for key in keys:
                row = self._rows.get(key)
                cell = run.by_key.get(key)
                if row is not None and cell is not None:
                    row.refresh(cell)
            self._refresh_chrome()

        if dirty:
            try:
                self.page.update()
            except Exception:
                pass

    def _sync_rows(self) -> None:
        """Create row widgets for any cells we have not rendered yet."""
        run = self.run
        # Size Node and Cell from the longest discovered values. Bounds keep a
        # short site compact and prevent an unusual DN from consuming the
        # entire window; ellipsis + tooltip remain available beyond the cap.
        all_cells = list(run.cells)
        node_width = min(
            _W_NODE_MAX,
            max([_W_NODE] + [24 + len(c.node_name) * 7 for c in all_cells]),
        )
        mo_labels = [c.cell_dn if c.rat == "GSM" else c.mo_ref
                     for c in all_cells]
        mo_width = min(
            _W_MO_MAX,
            max([_W_MO] + [24 + len(label) * 7 for label in mo_labels]),
        )
        for section in self._sections.values():
            section.set_identity_widths(node_width, mo_width)
        for row in self._rows.values():
            row.set_identity_widths(node_width, mo_width)

        for name, section in self._sections.items():
            cells = run.cells_of(name)
            if len(section.rows_column.controls) == len(cells):
                continue
            section.rows_column.controls = []
            for cell in cells:
                row = self._rows.get(cell.key)
                if row is None:
                    row = _CellRow(cell)
                    self._rows[cell.key] = row
                row.set_identity_widths(node_width, mo_width)
                row.refresh(cell)
                section.rows_column.controls.append(row.control)
            section.col_header.visible = bool(cells)
            if name == UNMAPPED:
                section.control.visible = bool(cells)

    def _refresh_chrome(self) -> None:
        run = self.run
        busy = self.engine.is_busy()

        for name, section in self._sections.items():
            section.set_counts(
                run.group_counts(name),
                run.groups[name].status if name in run.groups else GroupStatus.PENDING,
                busy,
                len(run.unlockable_cells_of(name)),
                len(run.relockable_cells_of(name)),
                len(run.lockable_cells_of(name)),
            )
            if name != UNMAPPED:
                sectors = run.sectors_of(name)
                unlockable_by_sector = {
                    s: len(run.unlockable_cells_of(name, s)) for s in sectors
                }
                lockable_by_sector = {
                    s: len(run.lockable_cells_of(name, s)) for s in sectors
                }
                section.set_sectors(
                    sectors, unlockable_by_sector, busy,
                    run.phase == RunPhase.READY, lockable_by_sector,
                )
            if run.phase != RunPhase.READY:
                section.button.disabled = True
                section.button.tooltip = (
                    "Cut Over preparation and pre-state must be READY first"
                )

        discovered = bool(run.cells)
        any_unlockable = any(run.unlockable_cells_of(g)
                             for g in self.cfg["group_order"])
        any_relockable = sum(len(run.relockable_cells_of(g))
                             for g in self.cfg["group_order"])
        self.unlock_all_btn.disabled = (
            busy or run.phase != RunPhase.READY or not any_unlockable
        )
        global_groups = list(self.cfg["group_order"])
        if self.cfg.get("gsm", {}).get("enabled", True):
            global_groups.append(GSM)
        for sector, button in self.sector_unlock_btns.items():
            count = sum(len(run.unlockable_cells_of(group, sector))
                        for group in global_groups)
            exists = any(c.sector == sector and c.group in global_groups
                         for c in run.cells)
            button.visible = exists
            button.disabled = busy or run.phase != RunPhase.READY or count == 0
            button.tooltip = (
                f"Unlock {count} cell(s) in S{sector} across LB, MB, HB and GSM; "
                "other sectors are not touched"
            )
        self.verify_btn.disabled = busy or not discovered
        self.relock_all_btn.visible = any_relockable > 0
        self.relock_all_btn.disabled = busy or not any_relockable
        self.relock_all_btn.tooltip = (
            f"Lock the {any_relockable} cell(s) this run unlocked. "
            f"Cells already in service before this run are not touched."
        )
        self.cancel_btn.disabled = not busy
        self.start_hc_btn.disabled = (
            busy or run.phase != RunPhase.IDLE
            or bool(self.engine.recovery_checkpoint)
        )
        self.start_unlock_btn.disabled = self.start_hc_btn.disabled
        # TRFS Log and Post HC are independent actions: each connects the nodes
        # itself (TRFS identifies techs with `pv $rats`; Post HC runs its own
        # discovery), so they only wait on another action finishing — not on the
        # IDLE phase or a prior Pre HC.
        # The unfinished checkpoint deliberately remains after a successful
        # resume, so it is not a valid disabled-state guard.  These independent
        # actions only need to wait until reconciliation/the current action has
        # released the node sessions.
        reconciling = run.phase == RunPhase.RECOVERING
        self.trfs_btn.disabled = busy or reconciling
        self.post_hc_btn.disabled = busy or reconciling

        phase_text = {
            RunPhase.IDLE: (
                "Ready — click Start HC to create CV and run preHC (no modump)"
            ),
            RunPhase.PREPARING: "Preparing Cut Over — CV, modump, and preHC…",
            RunPhase.RECOVERING: "Recovering — reconciling live node state…",
            RunPhase.DISCOVERING: "Discovering cells…",
            RunPhase.READY: "Ready — pick a band group to unlock",
            RunPhase.UNLOCKING: f"Unlocking {run.active_group}…",
            RunPhase.WAIT_ENABLE: f"Waiting for {run.active_group} cells to enable…",
            RunPhase.WAIT_TRAFFIC: f"Waiting for traffic on {run.active_group}…",
            RunPhase.REPORTING: f"Capturing {run.active_group} evidence…",
            RunPhase.FINAL_VERIFY: "Running post-cutover verification…",
            RunPhase.DONE: "Cut over complete",
            RunPhase.CANCELLED: "Cancelled",
            RunPhase.FAILED: run.error or "Failed",
        }.get(run.phase, str(run.phase))
        self.status_text.value = phase_text
        self.status_text.color = (
            DANGER if run.phase == RunPhase.FAILED else
            SUCCESS if run.phase == RunPhase.DONE else ACCENT
        )

        if run.cells:
            traffic_cells = [c for c in run.cells
                             if c.rat != "GSM" and c.group != UNMAPPED]
            total = len(traffic_cells)
            ok = sum(1 for c in traffic_cells if c.is_carrying_traffic)
            unmapped = len(run.cells_of(UNMAPPED))
            parts = [f"{ok}/{total} cell(s) carrying traffic"]
            if unmapped:
                parts.append(f"{unmapped} in an unmapped band (never unlocked)")
            self.summary_text.value = " · ".join(parts)

    # ── engine events ────────────────────────────────────────────
    def _handle_event(self, event) -> None:
        if event.kind == "handoff":
            self._whatsapp_handoff(event)
        elif event.kind == "confirm_traffic":
            self._traffic_gate_dialog(event)
        elif event.kind == "diagnostic":
            self._alert("Cut Over — check the configuration", event.message)
        elif event.kind == "run_done":
            self._finished_hint(event.message)
        elif event.kind == "trfs_done":
            self._trfs_done_dialog(event)
        elif event.kind == "posthc_done":
            self._posthc_done_dialog(event)

    def _posthc_done_dialog(self, event) -> None:
        """Show the final Post HC folder only after the whole action finishes."""
        local_dir = event.png_path or ""
        body = ft.Column(
            [
                ft.Text(event.message, size=13, color=TEXT, selectable=True),
                ft.Container(height=8),
                ft.Text("Saved on this laptop under:", size=11, color=TEXT_MUTED),
                ft.Text(local_dir, size=12, color=ACCENT, selectable=True,
                        font_family="Consolas"),
            ],
            spacing=4, tight=True, width=620,
        )
        actions = []
        if local_dir:
            def _open(_e):
                try:
                    os.startfile(local_dir)  # noqa: S606 (Windows only)
                except Exception as exc:
                    self._alert("Post HC", f"Could not open the folder: {exc}")
            actions.append(ft.TextButton("Open folder", on_click=_open))
        dlg = ft.AlertDialog(
            modal=False,
            title=ft.Text("Post HC completed", color=SUCCESS),
            content=body,
        )
        actions.append(ft.TextButton("OK", on_click=lambda e: self._close_dialog(dlg)))
        dlg.actions = actions
        self._show_dialog(dlg)

    def _trfs_done_dialog(self, event) -> None:
        """TRFS finished — tell the operator where the folders landed and offer
        to open the folder on this laptop."""
        local_dir = event.png_path or ""
        body = ft.Column(
            [
                ft.Text(event.message, size=13, color=TEXT, selectable=True),
                ft.Container(height=8),
                ft.Text("Saved on this laptop under:", size=11, color=TEXT_MUTED),
                ft.Text(local_dir, size=12, color=ACCENT, selectable=True,
                        font_family="Consolas"),
            ],
            spacing=4, tight=True, width=620,
        )
        actions = []
        if local_dir:
            def _open(_e):
                try:
                    from whatsapp_sender import open_containing_folder
                    if open_containing_folder(local_dir):
                        return
                except Exception:
                    pass
                try:
                    os.startfile(local_dir)  # noqa: S606 (Windows only)
                except Exception as exc:
                    self._alert("TRFS", f"Could not open the folder: {exc}")
            actions.append(ft.TextButton("Open folder", on_click=_open))
        dlg = ft.AlertDialog(
            modal=False,
            title=ft.Text("TRFS logs downloaded", color=SUCCESS),
            content=body,
        )
        actions.append(ft.TextButton("OK", on_click=lambda e: self._close_dialog(dlg)))
        dlg.actions = actions
        self._show_dialog(dlg)

    def _finished_hint(self, message: str) -> None:
        self.status_text.value = message or "Done"
        self.status_text.color = SUCCESS

    def _whatsapp_handoff(self, event) -> None:
        wa = self.cfg["report"]["whatsapp"]
        try:
            from whatsapp_sender import open_containing_folder, send_image_semi_auto
        except Exception as exc:
            self._alert("Cut Over", f"Screenshot saved to:\n{event.png_path}\n\n"
                                    f"(WhatsApp handoff unavailable: {exc})")
            return

        result = send_image_semi_auto(
            event.png_path, caption=event.caption,
            group_link=wa.get("group_link", ""))

        body = ft.Column(
            [
                ft.Text(result.message, size=13, color=TEXT, selectable=True),
                ft.Container(height=6),
                ft.Text(event.png_path, size=11, color=TEXT_MUTED, selectable=True),
            ],
            spacing=4, tight=True,
        )
        dlg = ft.AlertDialog(
            modal=False,
            title=ft.Text(f"Send {event.group} evidence to WhatsApp", color=TEXT),
            content=body,
            actions=[
                ft.TextButton("Open folder",
                              on_click=lambda e: open_containing_folder(event.png_path)),
                ft.TextButton("Done", on_click=lambda e: self._close_dialog(dlg)),
            ],
        )
        self._show_dialog(dlg)

    def _traffic_gate_dialog(self, event) -> None:
        """UE column unreadable — ask rather than fabricate a pass."""
        group = event.group

        def _resolve(ok: bool):
            self._close_dialog(dlg)
            self.engine.confirm_traffic(group, ok)

        body = ft.Column(
            [
                ft.Text(event.message, size=13, color=TEXT),
                ft.Container(height=6),
                ft.Text(event.png_path or "(no screenshot captured)", size=11,
                        color=TEXT_MUTED, selectable=True),
            ],
            spacing=4, tight=True,
        )
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"{group}: confirm traffic manually", color=ACCENT_WARM),
            content=body,
            actions=[
                ft.TextButton("Not carrying traffic",
                              on_click=lambda e: _resolve(False)),
                ft.ElevatedButton("Traffic confirmed",
                                  on_click=lambda e: _resolve(True)),
            ],
            on_dismiss=lambda e: self.engine.confirm_traffic(group, False),
        )
        self._show_dialog(dlg)

    # ── blocking prompts called from worker threads ──────────────
    def _confirm_unlock(self, groups: str, commands: list) -> bool:
        """Show exactly what will be sent, and wait for an answer.

        Deliberately lists the literal commands rather than a summary: this
        changes live cells on a production network, and "12 cells in LB" is
        not enough for an operator to catch a wrong node or a wrong template.
        """
        result = {"ok": False}
        done = threading.Event()

        action = str(groups or "").strip().upper()
        if action.startswith("ROLL BACK"):
            action_label = "Roll Back"
            action_icon = ft.Icons.UNDO_ROUNDED
            action_color = DANGER
            warning = (
                "This rolls back cells unlocked by this Cut Over run.\n"
                "The listed cells will be locked and taken out of service.")
        elif action.startswith("LOCK"):
            action_label = "Lock"
            action_icon = ft.Icons.LOCK_ROUNDED
            action_color = DANGER
            warning = (
                "This locks live cells on the production network.\n"
                "The listed cells will be taken out of service.")
        else:
            action_label = "Unlock"
            action_icon = ft.Icons.LOCK_OPEN_ROUNDED
            action_color = ACCENT_WARM
            warning = (
                "This unlocks live cells on the production network.\n"
                "Cancelling later does NOT re-lock them.")

        def _resolve(ok: bool):
            if done.is_set():
                return
            result["ok"] = ok
            self._close_dialog(dlg)
            done.set()

        preview = ft.Column(
            [ft.Text(c, size=11, color=TEXT, font_family="Consolas",
                     selectable=True) for c in commands[:200]],
            spacing=1, scroll=ft.ScrollMode.AUTO, height=240, tight=True,
        )
        warn = ft.Row(
            [ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=18, color=ACCENT_WARM),
             ft.Text(warning,
                     size=12, color=ACCENT_WARM)],
            spacing=8,
        )
        body = ft.Column(
            [
                warn,
                ft.Container(height=8),
                ft.Text(f"{len(commands)} command(s) will be sent for {groups}:",
                        size=12, color=TEXT_MUTED),
                ft.Container(
                    content=preview, bgcolor=ft.Colors.with_opacity(0.25, BG_BOTTOM),
                    border=ft.Border.all(1, BORDER), border_radius=10, padding=10),
            ],
            spacing=4, tight=True, width=620,
        )
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Confirm Cut Over — {groups}", color=TEXT),
            content=body,
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: _resolve(False)),
                ft.ElevatedButton(
                    action_label, icon=action_icon,
                    style=ft.ButtonStyle(bgcolor=action_color, color="#06242A"),
                    on_click=lambda e: _resolve(True)),
            ],
            on_dismiss=lambda e: _resolve(False),
        )
        with self._dialog_lock:
            self._show_dialog(dlg)
            done.wait()
        return result["ok"]

    def _ask_continue(self, message: str) -> bool:
        """Partial-success gate: continue with the cells that did come up?"""
        result = {"ok": True}
        done = threading.Event()

        def _resolve(ok: bool):
            if done.is_set():
                return
            result["ok"] = ok
            self._close_dialog(dlg)
            done.set()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Cut Over — partial result", color=ACCENT_WARM),
            content=ft.Text(message, size=13, color=TEXT),
            actions=[
                ft.TextButton("Stop", on_click=lambda e: _resolve(False)),
                ft.ElevatedButton("Continue", on_click=lambda e: _resolve(True)),
            ],
            on_dismiss=lambda e: _resolve(True),
        )
        with self._dialog_lock:
            self._show_dialog(dlg)
            done.wait()
        return result["ok"]

    # ── dialog plumbing (Flet 0.84 dialog stack) ─────────────────
    def _show_dialog(self, dlg) -> None:
        if hasattr(self.page, "show_dialog"):
            try:
                self.page.show_dialog(dlg)
                return
            except Exception:
                pass
        try:
            self.page.overlay.append(dlg)
            dlg.open = True
            self.page.update()
        except Exception:
            pass

    def _close_dialog(self, dlg) -> None:
        if hasattr(self.page, "pop_dialog"):
            try:
                self.page.pop_dialog()
                return
            except Exception:
                pass
        try:
            dlg.open = False
            if dlg in self.page.overlay:
                self.page.overlay.remove(dlg)
            self.page.update()
        except Exception:
            pass

    def _alert(self, title: str, message: str) -> None:
        dlg = ft.AlertDialog(
            title=ft.Text(title, color=TEXT),
            content=ft.Text(message, size=13, color=TEXT, selectable=True),
        )
        dlg.actions = [ft.TextButton("OK", on_click=lambda e: self._close_dialog(dlg))]
        self._show_dialog(dlg)

    # ── button handlers ──────────────────────────────────────────
    def _on_start_hc(self, e) -> None:
        """Health check: run preparation (CV backup + preHC) and discovery, but
        skip the slow modump capture."""
        self.engine.start_discovery(skip_modump=True)
        self._refresh_chrome()
        self.page.update()

    def _on_post_hc(self, e) -> None:
        """Post-cutover health check: create the Post_CutOver CV, run postHC
        (downloading its logfile), then re-read cell status."""
        self.engine.start_posthc()
        self._refresh_chrome()
        self.page.update()

    def _on_trfs_log(self, e) -> None:
        """Run LABEL.mos (first click) then the per-node TRFS script, and
        download each node's newest TRFS log folder."""
        self.engine.run_trfs_log()
        self._refresh_chrome()
        self.page.update()

    def _on_start_unlock(self, e) -> None:
        """Skip preparation (CV / modump / preHC) and go straight to cell
        discovery + status, so the operator can unlock right away."""
        self.engine.start_discovery(skip_preparation=True)
        self._refresh_chrome()
        self.page.update()

    def _on_unlock_group(self, group: str, sector: Optional[str] = None) -> None:
        self.engine.unlock_group(group, sector)
        self._refresh_chrome()
        self.page.update()

    def _on_relock_group(self, group: str, sector: Optional[str] = None) -> None:
        self.engine.relock_group(group, sector)
        self._refresh_chrome()
        self.page.update()

    def _on_lock_group(self, group: str, sector: Optional[str] = None) -> None:
        self.engine.lock_group(group, sector)
        self._refresh_chrome()
        self.page.update()

    def _on_share_evidence(self, group: str) -> None:
        self.engine.share_evidence(group)
        self._refresh_chrome()
        self.page.update()

    def _on_relock_all(self, e) -> None:
        self.engine.relock_all()
        self._refresh_chrome()
        self.page.update()

    def _on_unlock_all(self, e) -> None:
        self.engine.unlock_all()
        self._refresh_chrome()
        self.page.update()

    def _on_unlock_sector(self, sector: str) -> None:
        self.engine.unlock_sector(sector)
        self._refresh_chrome()
        self.page.update()

    def _on_verify(self, e) -> None:
        self.engine.run_final_verification()
        self._refresh_chrome()
        self.page.update()

    def _on_cancel(self, e) -> None:
        self.engine.cancel()
        self.status_text.value = "Cancelling…"
        self.page.update()

    def _on_back(self, e) -> None:
        self._finished = True
        try:
            self.engine.shutdown()
        except Exception:
            pass
        self.page.go("/form")
