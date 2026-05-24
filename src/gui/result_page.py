"""
TRFS GUI - Final result workspace.
"""
import asyncio
import os
import sys

import flet as ft

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gui.theme import (
    ACCENT,
    INFO,
    PANEL,
    SUCCESS,
    TEXT,
    TEXT_MUTED,
    background_gradient,
    badge,
    panel,
    primary_button_style,
    secondary_button_style,
)


class ResultPage:
    def __init__(self, page: ft.Page):
        self.page = page

    def build(self) -> ft.View:
        generated_files = getattr(self.page, "trfs_generated_files", []) or []
        duration = getattr(self.page, "trfs_duration", "—")

        file_cards: list[ft.Control] = []
        for fpath in generated_files:
            folder = os.path.dirname(fpath)
            fname = os.path.basename(fpath)
            file_cards.append(
                panel(
                    ft.Row(
                        [
                            ft.Container(
                                width=48,
                                height=48,
                                border_radius=14,
                                bgcolor=ft.Colors.with_opacity(0.14, SUCCESS),
                                alignment=ft.Alignment(0, 0),
                                content=ft.Icon(ft.Icons.DESCRIPTION_ROUNDED, color=SUCCESS, size=24),
                            ),
                            ft.Column(
                                [
                                    ft.Text(fname, size=14, color=TEXT, weight=ft.FontWeight.BOLD),
                                    ft.Text(fpath, size=11, color=TEXT_MUTED, selectable=True),
                                ],
                                spacing=5,
                                expand=True,
                            ),
                            ft.ElevatedButton(
                                "Open Folder",
                                icon=ft.Icons.FOLDER_OPEN,
                                style=secondary_button_style(),
                                on_click=lambda _, p=folder: os.startfile(p) if sys.platform == "win32" else None,
                            ),
                        ],
                        spacing=16,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    bgcolor="#11253A",
                    padding=18,
                )
            )

        if not file_cards:
            file_cards.append(
                panel(
                    ft.Text(
                        "No files were generated. Check the run log and ENM flow before trying again.",
                        color="#FFB4B4",
                    ),
                    bgcolor="#301A22",
                )
            )

        hero = panel(
            ft.Column(
                [
                    ft.Row(
                        [
                            badge("Run Complete", SUCCESS, ft.Icons.CHECK_CIRCLE),
                            ft.Container(expand=True),
                            ft.ElevatedButton(
                                "Run Another Site",
                                icon=ft.Icons.ARROW_BACK,
                                style=primary_button_style(),
                                on_click=lambda _: asyncio.create_task(self.page.push_route("/")),
                            ),
                            ft.ElevatedButton(
                                "Close",
                                icon=ft.Icons.CLOSE_ROUNDED,
                                style=secondary_button_style(),
                                on_click=lambda _: self.page.window.destroy(),
                            ),
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text("Reports are ready", size=34, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Text(
                        "Your TRFS output has been assembled and the generated files are ready for review.",
                        size=14,
                        color=TEXT_MUTED,
                    ),
                    ft.Row(
                        [
                            self._metric("Files", str(len(generated_files)), SUCCESS),
                            self._metric("Duration", duration, INFO),
                            self._metric("State", "Success" if generated_files else "Review needed", ACCENT),
                        ],
                        spacing=14,
                        wrap=True,
                    ),
                ],
                spacing=16,
            ),
            bgcolor=PANEL,
            padding=28,
        )

        files_panel = panel(
            ft.Column(
                [
                    ft.Text("Generated output", size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Text(
                        "Each Excel file is listed below with a direct folder shortcut.",
                        size=12,
                        color=TEXT_MUTED,
                    ),
                    *file_cards,
                ],
                spacing=16,
            ),
            bgcolor="#0E2031",
            padding=24,
        )

        summary_panel = self._build_summary_panel()

        content_children = [hero]
        if summary_panel is not None:
            content_children.append(summary_panel)
        content_children.append(files_panel)

        return ft.View(
            route="/result",
            padding=0,
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
            controls=[
                ft.Container(
                    gradient=background_gradient(),
                    padding=ft.Padding.symmetric(horizontal=28, vertical=24),
                    content=ft.Column(
                        content_children,
                        spacing=18,
                    ),
                )
            ],
        )

    def _status_chip(self, status: str) -> ft.Control:
        """Icon-backed colored pill for a band's result."""
        spec = {
            "OK":           (SUCCESS,     ft.Icons.CHECK_CIRCLE_ROUNDED),
            "FAIL":         ("#FF6B6B",   ft.Icons.CANCEL_ROUNDED),
            "NOT CAPTURED": ("#FFB454",   ft.Icons.WARNING_AMBER_ROUNDED),
            "empty":        (TEXT_MUTED,  ft.Icons.REMOVE_ROUNDED),
        }.get(status, (TEXT_MUTED, ft.Icons.HELP_OUTLINE))
        color, icon = spec
        label = "—" if status == "empty" else status
        return ft.Container(
            content=ft.Row(
                [
                    ft.Icon(icon, color=color, size=14),
                    ft.Text(label, size=11, color=color, weight=ft.FontWeight.BOLD),
                ],
                spacing=5,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
            ),
            bgcolor=ft.Colors.with_opacity(0.15, color),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.4, color)),
            border_radius=999,
            padding=ft.Padding(left=8, right=10, top=4, bottom=4),
        )

    def _stat_pill(self, label: str, value: int, color: str, icon) -> ft.Control:
        """Compact metric pill used in the summary stats strip."""
        return ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        width=34, height=34, border_radius=10,
                        bgcolor=ft.Colors.with_opacity(0.18, color),
                        alignment=ft.Alignment(0, 0),
                        content=ft.Icon(icon, color=color, size=18),
                    ),
                    ft.Column(
                        [
                            ft.Text(label, size=10, color=TEXT_MUTED,
                                    weight=ft.FontWeight.W_600),
                            ft.Text(str(value), size=18, color=TEXT,
                                    weight=ft.FontWeight.BOLD),
                        ],
                        spacing=0,
                        tight=True,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            bgcolor=ft.Colors.with_opacity(0.08, color),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.25, color)),
            border_radius=14,
        )

    def _build_summary_panel(self) -> ft.Control | None:
        """Render the per-node/per-band TRFS summary handed off by ProgressPage."""
        summary = getattr(self.page, "trfs_summary", None)
        if not summary:
            return None

        # ── Stats roll-up ─────────────────────────────────────────────
        total_nodes = len(summary.get("nodes", []))
        total_ok = total_fail = total_nc = 0
        for node in summary.get("nodes", []):
            for b in node.get("bands", []):
                st = b.get("status")
                if st == "OK":          total_ok   += 1
                elif st == "FAIL":      total_fail += 1
                elif st == "NOT CAPTURED": total_nc += 1
        missing_count = len(summary.get("missing_bands") or [])

        stats_strip = ft.Row(
            [
                self._stat_pill("Nodes",        total_nodes,  ACCENT,   ft.Icons.HUB_ROUNDED),
                self._stat_pill("Captured",     total_ok,     SUCCESS,  ft.Icons.CHECK_CIRCLE_ROUNDED),
                self._stat_pill("Failed",       total_fail,   "#FF6B6B", ft.Icons.CANCEL_ROUNDED),
                self._stat_pill("Not captured", total_nc,     "#FFB454", ft.Icons.WARNING_AMBER_ROUNDED),
                self._stat_pill("Missing bands", missing_count, INFO,   ft.Icons.HELP_OUTLINE_ROUNDED),
            ],
            spacing=10,
            wrap=True,
        )

        # ── Per-node cards ────────────────────────────────────────────
        node_rows: list[ft.Control] = []
        for node in summary.get("nodes", []):
            band_chips = ft.Row(
                [
                    ft.Container(
                        content=ft.Row(
                            [
                                ft.Text(
                                    b["band"], size=13, color=TEXT,
                                    weight=ft.FontWeight.W_600,
                                ),
                                self._status_chip(b["status"]),
                            ],
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            tight=True,
                        ),
                        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                        bgcolor=ft.Colors.with_opacity(0.08, TEXT),
                        border=ft.Border.all(1, ft.Colors.with_opacity(0.08, TEXT)),
                        border_radius=12,
                    )
                    for b in node.get("bands", [])
                ],
                spacing=10,
                wrap=True,
            )
            is_gsm = node.get("tech") == "GSM"
            tech_color = "#FFB454" if is_gsm else INFO
            accent_color = tech_color

            # Quick per-node tally line ("3 OK · 0 failed")
            node_ok   = sum(1 for b in node.get("bands", []) if b.get("status") == "OK")
            node_fail = sum(1 for b in node.get("bands", []) if b.get("status") == "FAIL")
            node_nc   = sum(1 for b in node.get("bands", []) if b.get("status") == "NOT CAPTURED")
            tally_bits = [f"{node_ok} OK"]
            if node_fail: tally_bits.append(f"{node_fail} failed")
            if node_nc:   tally_bits.append(f"{node_nc} not captured")
            tally_text = " · ".join(tally_bits)

            node_note = node.get("note")
            note_control = None
            if node_note:
                note_control = ft.Container(
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.INFO_OUTLINE, color="#FFB454", size=16),
                            ft.Text(node_note, size=12, color=TEXT_MUTED, expand=True),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    padding=ft.Padding(left=10, right=10, top=8, bottom=8),
                    bgcolor=ft.Colors.with_opacity(0.08, "#FFB454"),
                    border_radius=10,
                )

            _header_row = ft.Row(
                [
                    ft.Container(
                        content=ft.Text(
                            f"Node {node['index']}", size=11,
                            color=ACCENT, weight=ft.FontWeight.BOLD,
                        ),
                        bgcolor=ft.Colors.with_opacity(0.15, ACCENT),
                        border_radius=10,
                        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                    ),
                    ft.Text(
                        node["node_name"], size=14, color=TEXT,
                        weight=ft.FontWeight.BOLD, selectable=True,
                    ),
                    ft.Container(
                        content=ft.Row(
                            [
                                ft.Icon(
                                    ft.Icons.CELL_TOWER_ROUNDED
                                    if is_gsm else ft.Icons.SIGNAL_CELLULAR_ALT_ROUNDED,
                                    color=tech_color, size=14,
                                ),
                                ft.Text(
                                    node.get("tech", ""), size=11,
                                    color=tech_color, weight=ft.FontWeight.BOLD,
                                ),
                            ],
                            spacing=5,
                            tight=True,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        bgcolor=ft.Colors.with_opacity(0.12, tech_color),
                        border=ft.Border.all(1, ft.Colors.with_opacity(0.35, tech_color)),
                        border_radius=10,
                        padding=ft.Padding(left=8, right=10, top=4, bottom=4),
                    ),
                    ft.Container(expand=True),
                    ft.Text(tally_text, size=11, color=TEXT_MUTED,
                            weight=ft.FontWeight.W_600),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )

            node_body_children: list[ft.Control] = [_header_row]
            if note_control is not None:
                node_body_children.append(note_control)
            node_body_children.append(band_chips)

            node_body = ft.Column(
                node_body_children,
                spacing=12,
                expand=True,
            )

            # Colored left accent bar via a 4px LEFT border side — simpler
            # and avoids constructing an asymmetric BorderRadius (Flet's
            # class signature for that is version-dependent and has bitten
            # us once).
            node_rows.append(
                ft.Container(
                    content=node_body,
                    padding=ft.Padding(left=16, top=14, right=16, bottom=14),
                    bgcolor="#11253A",
                    border_radius=14,
                    border=ft.Border(
                        left=ft.BorderSide(4, accent_color),
                        top=ft.BorderSide(1, ft.Colors.with_opacity(0.06, TEXT)),
                        right=ft.BorderSide(1, ft.Colors.with_opacity(0.06, TEXT)),
                        bottom=ft.BorderSide(1, ft.Colors.with_opacity(0.06, TEXT)),
                    ),
                )
            )

        # Missing-bands notice (e.g. "NR700 is not present on any node").
        missing = summary.get("missing_bands") or []
        missing_block = None
        if missing:
            missing_block = panel(
                ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Icon(ft.Icons.INFO_OUTLINE, color="#FFB454", size=18),
                                ft.Text(
                                    "Not captured on any node",
                                    size=14, color="#FFB454", weight=ft.FontWeight.BOLD,
                                ),
                            ],
                            spacing=8,
                        ),
                        ft.Text(
                            ", ".join(missing)
                            + "  —  band(s) are not present on any of the configured nodes",
                            size=12, color=TEXT_MUTED,
                        ),
                    ],
                    spacing=6,
                ),
                bgcolor=ft.Colors.with_opacity(0.08, "#FFB454"),
                padding=14,
            )

        # Single output folder path (no per-band paths — every file lands here).
        out_folder = summary.get("output_folder") or ""
        folder_block = panel(
            ft.Row(
                [
                    ft.Icon(ft.Icons.FOLDER_OPEN, color=ACCENT, size=22),
                    ft.Column(
                        [
                            ft.Text("Output folder", size=11, color=TEXT_MUTED,
                                    weight=ft.FontWeight.W_600),
                            ft.Text(out_folder or "—", size=13, color=TEXT,
                                    selectable=True, weight=ft.FontWeight.W_600),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    ft.ElevatedButton(
                        "Open",
                        icon=ft.Icons.OPEN_IN_NEW,
                        style=secondary_button_style(),
                        on_click=(
                            lambda _: os.startfile(out_folder)
                            if out_folder and sys.platform == "win32"
                            and os.path.isdir(out_folder) else None
                        ),
                    ),
                ],
                spacing=14,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor="#0E2031",
            padding=16,
        )

        header = ft.Row(
            [
                ft.Container(
                    width=40, height=40, border_radius=12,
                    bgcolor=ft.Colors.with_opacity(0.18, ACCENT),
                    alignment=ft.Alignment(0, 0),
                    content=ft.Icon(ft.Icons.INSIGHTS_ROUNDED, color=ACCENT, size=22),
                ),
                ft.Column(
                    [
                        ft.Text("TRFS run summary", size=20,
                                weight=ft.FontWeight.BOLD, color=TEXT),
                        ft.Text(
                            "Per-node, per-band status from this run.",
                            size=12, color=TEXT_MUTED,
                        ),
                    ],
                    spacing=2,
                    tight=True,
                ),
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        divider = ft.Container(
            height=1,
            bgcolor=ft.Colors.with_opacity(0.08, TEXT),
        )

        children: list[ft.Control] = [
            header,
            stats_strip,
            divider,
            *node_rows,
        ]
        if missing_block is not None:
            children.append(missing_block)
        children.append(folder_block)

        return panel(
            ft.Column(children, spacing=14),
            bgcolor="#0E2031",
            padding=24,
        )

    def _metric(self, label: str, value: str, color: str) -> ft.Container:
        return ft.Container(
            width=180,
            padding=16,
            bgcolor=ft.Colors.with_opacity(0.14, color),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.3, color)),
            border_radius=18,
            content=ft.Column(
                [
                    ft.Text(label, size=11, color=TEXT_MUTED, weight=ft.FontWeight.W_600),
                    ft.Text(value, size=18, color=TEXT, weight=ft.FontWeight.BOLD),
                ],
                spacing=6,
            ),
        )
