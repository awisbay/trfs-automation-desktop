"""
SRS screenshot capture via Microsoft Edge (user's authenticated session).

Strategy:
  1. Launch Edge with the user's real profile + remote debugging port
  2. User logs in via Azure SSO in the Edge window (manual step)
  3. Playwright connects via CDP, opens new tabs, takes screenshots
  4. Closes tabs when done — user's Edge stays open

This avoids automating Azure SSO / OTP which is fragile and unreliable.
"""
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SRS_BASE_URL = "https://srs.ericsson.com"
CDP_PORT = 9223  # Use non-standard port to avoid conflicts

# Common Edge install locations on Windows
_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _find_edge_exe() -> Optional[str]:
    """Find the Edge executable on this system."""
    # Check PATH first
    edge_path = shutil.which("msedge") or shutil.which("microsoft-edge")
    if edge_path:
        return edge_path
    # Check common locations
    for candidate in _EDGE_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def _get_edge_user_data_dir() -> str:
    """Return the default Edge user data directory."""
    if os.name == "nt":
        return os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Microsoft", "Edge", "User Data",
        )
    # macOS / Linux fallbacks (unlikely for this use case)
    return os.path.expanduser("~/.config/microsoft-edge")


def _get_srs_debug_profile_dir() -> str:
    """Return a dedicated Edge profile directory for SRS debug sessions.

    We can't reuse the user's default profile when Edge is already running
    because Chrome/Edge locks the profile directory.  A separate profile
    directory lets us launch a *new* Edge process with
    --remote-debugging-port even when normal Edge is open.

    The user will need to log in to Azure SSO once in this profile, but
    the session cookies are persisted so subsequent runs skip the login.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    else:
        base = os.path.expanduser("~/.config")
    return os.path.join(base, "TRFS_Edge_SRS_Profile")


def launch_edge_with_debug_port(
    url: str = SRS_BASE_URL,
    port: int = CDP_PORT,
) -> Optional[subprocess.Popen]:
    """Launch Edge with remote debugging enabled in a dedicated SRS profile.

    Uses a separate user-data-dir so it works even when the user's main
    Edge is already running (Edge locks its profile directory).

    Returns the Popen process object, or None on failure.
    """
    edge_exe = _find_edge_exe()
    if not edge_exe:
        logger.error("[SRS] Microsoft Edge not found on this system")
        return None

    # Use a dedicated profile so we don't conflict with running Edge
    user_data = _get_srs_debug_profile_dir()
    os.makedirs(user_data, exist_ok=True)
    logger.info(f"[SRS] Using dedicated SRS profile: {user_data}")

    args = [
        edge_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]

    logger.info(f"[SRS] Launching Edge: {' '.join(args)}")
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Give Edge time to start and open the debug port
        time.sleep(3)
        return proc
    except Exception as exc:
        logger.error(f"[SRS] Failed to launch Edge: {exc}")
        return None


def is_edge_debug_port_open(port: int = CDP_PORT) -> bool:
    """Check if Edge's CDP debug port is accepting connections."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def wait_for_cdp_ready(port: int = CDP_PORT, retries: int = 15, delay: float = 2.0) -> bool:
    """Wait until the CDP port is open, retrying up to *retries* times."""
    for i in range(retries):
        if is_edge_debug_port_open(port):
            # Double-check by fetching /json/version — the true readiness signal
            try:
                import urllib.request
                resp = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=3
                )
                resp.read()
                logger.info(f"[SRS] CDP port {port} is fully ready (attempt {i+1})")
                return True
            except Exception:
                pass
        time.sleep(delay)
    return False


class SrsBrowserSession:
    """Connect to Edge via CDP and capture SRS screenshots.

    Usage:
        with SrsBrowserSession(port=9223) as session:
            session.capture_page("https://srs.ericsson.com/some/page", "output.png")
    """

    def __init__(self, port: int = CDP_PORT, debug_dir: str = ""):
        self.port = port
        self.debug_dir = debug_dir
        self._pw = None
        self._browser = None
        self._page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        cdp_url = f"http://127.0.0.1:{self.port}"
        logger.info(f"[SRS] Connecting to Edge via CDP: {cdp_url}")

        try:
            self._browser = self._pw.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            self._cleanup()
            raise RuntimeError(
                f"Could not connect to Edge on port {self.port}. "
                f"Make sure Edge was launched with --remote-debugging-port={self.port}"
            ) from exc

        logger.info(f"[SRS] Connected. Contexts: {len(self._browser.contexts)}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._cleanup()
        return False

    def _cleanup(self):
        # Don't close the browser — it's the user's Edge!
        # Just disconnect Playwright
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self._browser = None

    def _save_debug(self, page, stage: str) -> str:
        """Save a debug screenshot."""
        if not self.debug_dir:
            return ""
        try:
            path = os.path.join(self.debug_dir, f"debug_srs_{stage}.png")
            page.screenshot(path=path, full_page=True)
            logger.info(f"[SRS] Debug screenshot: {path}")
            return path
        except Exception as exc:
            logger.warning(f"[SRS] Debug screenshot failed for {stage}: {exc}")
            return ""

    def check_logged_in(self) -> bool:
        """Check if the user is logged into SRS by looking at the existing tabs."""
        if not self._browser or not self._browser.contexts:
            return False

        ctx = self._browser.contexts[0]
        for page in ctx.pages:
            url = page.url.lower()
            # If any tab is on srs.ericsson.com and NOT on a login page, user is in
            if "srs.ericsson.com" in url and "login" not in url and "auth" not in url:
                logger.info(f"[SRS] User appears logged in (found tab: {page.url})")
                return True
        return False

    def capture_page(
        self,
        url: str,
        save_path: str,
        wait_ms: int = 5000,
        full_page: bool = True,
        viewport_width: int = 1920,
        viewport_height: int = 1080,
    ) -> Optional[str]:
        """Open a URL in a new tab, wait for it to load, take a screenshot, close the tab.

        Returns the save_path on success, None on failure.
        """
        if not self._browser or not self._browser.contexts:
            logger.error("[SRS] No browser context available")
            return None

        ctx = self._browser.contexts[0]
        page = None
        try:
            page = ctx.new_page()
            page.set_viewport_size({"width": viewport_width, "height": viewport_height})
            logger.info(f"[SRS] Navigating to: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)

            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            page.screenshot(path=save_path, full_page=full_page)
            logger.info(f"[SRS] Screenshot saved: {save_path}")
            return save_path
        except Exception as exc:
            logger.error(f"[SRS] Capture failed for {url}: {exc}")
            if page:
                self._save_debug(page, "capture_error")
            return None
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass

    def capture_multiple(
        self,
        url_save_pairs: list[tuple[str, str]],
        wait_ms: int = 5000,
    ) -> list[str]:
        """Capture multiple pages. Returns list of successful save paths."""
        results = []
        for url, save_path in url_save_pairs:
            path = self.capture_page(url, save_path, wait_ms=wait_ms)
            if path:
                results.append(path)
        return results

    def capture_measurement_monitoring(
        self,
        segment_name: str,
        save_path: str,
        monitoring_url: str = "https://srs.connected-operations.ericsson.net/MeasurementMonitoring/Index/3cdbda8f-eaa4-462d-9243-86720c5a72b9",
        viewport_width: int = 1920,
        viewport_height: int = 1080,
    ) -> Optional[dict[str, str]]:
        """Navigate the SRS Measurement Monitoring page, filter by segment,
        select MEASUREMENT_OK status, click into the first result's detail view,
        and capture screenshots of the map, measurement details, and full page.

        Steps:
          1. Navigate to the monitoring URL
          2. Expand "Segment" accordion and fill in segment_name
          3. Expand "Status" accordion and check MEASUREMENT_OK
          4. Click the zoom/details button on the first result row
          5. Wait for detail page to load
          6. Capture the canvas/map element
          7. Expand collapsed accordions and capture measurement details
          8. Capture full-page screenshot

        Returns a dict of {name: filepath} on success, None on failure.
        """
        if not self._browser or not self._browser.contexts:
            logger.error("[SRS] No browser context available")
            return None

        ctx = self._browser.contexts[0]
        page = None
        try:
            page = ctx.new_page()
            page.set_viewport_size({"width": viewport_width, "height": viewport_height})

            # --- Step 1: Navigate to Measurement Monitoring page ---
            logger.info(f"[SRS] Navigating to: {monitoring_url}")
            page.goto(monitoring_url, wait_until="domcontentloaded", timeout=60000)
            # Give the page time — first load may trigger an SSO redirect
            # or login flow that takes 10-15 seconds to complete
            logger.info("[SRS] Waiting 15s for SSO/login redirect to settle...")
            page.wait_for_timeout(15000)
            self._save_debug(page, "01_monitoring_loaded")

            # --- Step 2: Expand "Segment" accordion and fill in filter ---
            logger.info(f"[SRS] Looking for Segment accordion...")
            # Use exact text match to avoid matching "Secondary Segment"
            segment_header = page.locator("label.filterAccordion-header").filter(has_text=re.compile(r"^Segment$"))
            # Scroll into view first — the filter panel may need scrolling
            segment_header.scroll_into_view_if_needed(timeout=10000)
            segment_header.click()
            page.wait_for_timeout(1000)  # Wait for accordion animation
            logger.info("[SRS] Segment accordion clicked")
            self._save_debug(page, "02a_segment_accordion_clicked")

            segment_input = page.locator("input#gs_SegmentName")
            # The input may need a moment to become visible after accordion expands
            try:
                segment_input.wait_for(state="visible", timeout=5000)
            except Exception:
                # If still hidden, try clicking the header again (toggle)
                logger.info("[SRS] Segment input still hidden, clicking accordion again...")
                segment_header.click()
                page.wait_for_timeout(1000)
                self._save_debug(page, "02b_segment_accordion_retry")
                segment_input.wait_for(state="visible", timeout=5000)

            segment_input.fill(segment_name)
            logger.info(f"[SRS] Filled segment name: {segment_name}")
            # Press Enter to trigger the filter
            segment_input.press("Enter")
            page.wait_for_timeout(3000)
            self._save_debug(page, "02c_segment_filtered")

            # --- Step 3: Expand "Status" accordion and check MEASUREMENT_OK ---
            logger.info("[SRS] Looking for Status accordion...")
            status_header = page.locator("label.filterAccordion-header", has_text="Status")
            status_header.scroll_into_view_if_needed(timeout=10000)
            status_header.click()
            page.wait_for_timeout(1000)
            logger.info("[SRS] Status accordion clicked")
            self._save_debug(page, "03a_status_accordion_clicked")

            measurement_ok_checkbox = page.locator("input#MEASUREMENT_OK")
            try:
                measurement_ok_checkbox.wait_for(state="visible", timeout=5000)
            except Exception:
                logger.info("[SRS] MEASUREMENT_OK still hidden, clicking Status accordion again...")
                status_header.click()
                page.wait_for_timeout(1000)
                self._save_debug(page, "03b_status_accordion_retry")
                try:
                    measurement_ok_checkbox.wait_for(state="visible", timeout=5000)
                except Exception:
                    # Checkbox may be hidden but still interactable via label/JS
                    logger.info("[SRS] MEASUREMENT_OK input hidden — using JavaScript click")

            # Use JS to check the checkbox if Playwright can't click it (hidden by CSS)
            is_checked = measurement_ok_checkbox.is_checked()
            if not is_checked:
                try:
                    measurement_ok_checkbox.check(force=True)
                except Exception:
                    page.evaluate("document.getElementById('MEASUREMENT_OK').click()")
                logger.info("[SRS] Checked MEASUREMENT_OK")
            else:
                logger.info("[SRS] MEASUREMENT_OK already checked")
            page.wait_for_timeout(3000)  # Wait for table to reload with filters
            self._save_debug(page, "03c_status_filtered")

            # --- Step 4: Click the first row's details/zoom button ---
            logger.info("[SRS] Looking for first result row detail button...")
            # Target the <a> link wrapping the zoom icon
            detail_link = page.locator("a.btn.btn-action.c-blue").filter(
                has=page.locator("i.icon.icon-zoom-in")
            ).first
            try:
                detail_link.wait_for(state="visible", timeout=10000)
                detail_link.scroll_into_view_if_needed(timeout=5000)
                detail_link.click()
                logger.info("[SRS] Clicked detail button on first row")
                page.wait_for_timeout(5000)  # Wait for detail page to load
                self._save_debug(page, "04_detail_page")
            except Exception:
                logger.warning("[SRS] No result rows found — the filter may have returned 0 results")
                logger.warning("[SRS] Taking screenshot of the filtered table view instead")
                self._save_debug(page, "04_no_results")

            # --- Step 5: Wait for detail page content to fully load ---
            try:
                page.locator(".measurementDetailsWrapper").first.wait_for(
                    state="visible", timeout=15000
                )
                logger.info("[SRS] Detail page measurement details loaded")
            except Exception:
                logger.warning("[SRS] Measurement details wrapper not found — page may still be loading")
            self._save_debug(page, "05_detail_content_loaded")

            results: dict[str, str] = {}
            save_dir = os.path.dirname(save_path)
            os.makedirs(save_dir, exist_ok=True)

            # --- Step 6: Capture the map canvas (OpenLayers) ---
            # The map is rendered on a <canvas> inside the OL viewport.
            # We screenshot just that canvas so the output shows only the
            # antenna sectors / map content (no surrounding filter panel).
            try:
                map_accordion = page.locator("#tabGeneralDev_MapAndFeaturesAccordion")
                if map_accordion.count() > 0:
                    map_accordion.scroll_into_view_if_needed(timeout=3000)
                    page.wait_for_timeout(4000)

                # Compute a tight crop around the OL viewport (map area),
                # using the viewport's actual on-screen bounding box so we
                # don't capture the off-screen canvas buffer (black area).
                map_path = os.path.join(save_dir, f"SRS_map_{segment_name}.png")
                saved = False

                # Approach: read the canvas pixel data via toDataURL. This
                # returns only the rendered canvas content (no black padding
                # from Playwright's element-screenshot quirks with large
                # absolutely-positioned canvases inside scrolled containers).
                try:
                    data_url = page.evaluate("""() => {
                        const sels = [
                            '#tabGeneralDev_panelMap_amapMap canvas',
                            '#tabGeneralDev_panelMap .ol-viewport canvas',
                            '#tabGeneralDev_MapAndFeaturesAccordion canvas',
                        ];
                        for (const s of sels) {
                            const cvs = document.querySelectorAll(s);
                            let best = null, bestArea = 0;
                            for (const c of cvs) {
                                const r = c.getBoundingClientRect();
                                const area = (r.width || c.width) * (r.height || c.height);
                                if (area > bestArea) { best = c; bestArea = area; }
                            }
                            if (best) {
                                try { return best.toDataURL('image/png'); }
                                catch (e) { return null; }
                            }
                        }
                        return null;
                    }""")
                    if data_url and data_url.startswith("data:image/png;base64,"):
                        import base64
                        png_bytes = base64.b64decode(
                            data_url[len("data:image/png;base64,"):]
                        )
                        with open(map_path, "wb") as f:
                            f.write(png_bytes)
                        results["map"] = map_path
                        logger.info(f"[SRS] Map canvas (toDataURL) saved: {map_path}")
                        saved = True
                except Exception as exc:
                    logger.warning(f"[SRS] canvas toDataURL failed: {exc}")

                if not saved and map_accordion.count() > 0:
                    map_accordion.screenshot(path=map_path)
                    results["map"] = map_path
                    logger.info(f"[SRS] Map (accordion fallback) saved: {map_path}")
            except Exception as exc:
                logger.warning(f"[SRS] Map capture failed: {exc}")
            self._save_debug(page, "06_map_capture_attempted")

            # --- Step 7: Capture the measurement-details panel ---
            # This is the 3-column area containing User Information,
            # Measurement Validation, and Summary accordions.
            try:
                closed_accordions = page.locator(
                    "#generalMeasurementDetails .accordion:not(.accordion-opened) .accordion-header"
                )
                count = closed_accordions.count()
                for i in range(count):
                    try:
                        closed_accordions.nth(i).click()
                        page.wait_for_timeout(300)
                    except Exception:
                        pass
                if count > 0:
                    logger.info(f"[SRS] Expanded {count} collapsed accordion(s)")
                    page.wait_for_timeout(1000)
            except Exception:
                pass

            try:
                box = page.evaluate("""() => {
                    const ids = ['userInformationAccordion', 'measurementValidationAccordion', 'measurementSummaryAccordion'];
                    const els = ids.map(id => document.getElementById(id)).filter(Boolean);
                    if (els.length === 0) return null;
                    els[0].scrollIntoView({block: 'start'});
                    const rects = els.map(el => el.getBoundingClientRect());
                    const left = Math.min(...rects.map(r => r.left));
                    const right = Math.max(...rects.map(r => r.right));
                    const top = Math.min(...rects.map(r => r.top));
                    const bot = Math.max(...rects.map(r => r.bottom));
                    return {x: left, y: top, w: right - left, h: bot - top};
                }""")
                if box and box["w"] > 0 and box["h"] > 0:
                    page.wait_for_timeout(800)
                    box2 = page.evaluate("""() => {
                        const ids = ['userInformationAccordion', 'measurementValidationAccordion', 'measurementSummaryAccordion'];
                        const rects = ids.map(id => document.getElementById(id))
                            .filter(Boolean).map(el => el.getBoundingClientRect());
                        const left = Math.min(...rects.map(r => r.left));
                        const right = Math.max(...rects.map(r => r.right));
                        const top = Math.min(...rects.map(r => r.top));
                        const bot = Math.max(...rects.map(r => r.bottom));
                        return {x: left + window.scrollX, y: top + window.scrollY,
                                w: right - left, h: bot - top};
                    }""")
                    details_path = os.path.join(
                        save_dir, f"SRS_measurement_details_{segment_name}.png"
                    )
                    page.screenshot(path=details_path, full_page=True, clip={
                        "x": max(0, box2["x"]),
                        "y": max(0, box2["y"]),
                        "width": box2["w"],
                        "height": box2["h"],
                    })
                    results["measurement_details"] = details_path
                    logger.info(f"[SRS] Measurement details screenshot saved: {details_path}")
                else:
                    logger.warning("[SRS] #generalMeasurementDetails not found or not sized")
            except Exception as exc:
                logger.warning(f"[SRS] Measurement details capture failed: {exc}")
            self._save_debug(page, "07_details_capture_attempted")

            # --- Step 8: Click "Test result" tab and capture SMS + Voice call ---
            try:
                # Click the Test result tab
                test_result_tab = page.locator("div.title[hash='testResult']").first
                if test_result_tab.count() == 0:
                    test_result_tab = page.locator("div.title").filter(has_text="Test result").first
                test_result_tab.click()
                page.wait_for_timeout(2000)
                logger.info("[SRS] Clicked Test result tab")

                # Reset any horizontal scroll before clipping so left=0 is the true left edge
                page.evaluate("window.scrollTo(0, window.scrollY)")

                # 8a: SMS test result — scroll element into view, use Playwright's
                #     bounding_box() (viewport-relative) and screenshot without full_page
                def _capture_testresult_table(keyword, path):
                    """Scroll testResultTable for keyword into view and screenshot.
                    Temporarily shrinks the viewport height so only this table is
                    visible, preventing adjacent sections from bleeding in."""
                    # Scroll the table to viewport top and reset X
                    page.evaluate(f"""() => {{
                        for (const w of document.querySelectorAll('.table-flex-wrapper')) {{
                            if (!w.innerText.includes('{keyword}')) continue;
                            const t = w.querySelector('table.testResultTable');
                            if (t) {{
                                t.scrollIntoView({{block: 'start'}});
                                window.scrollTo(0, window.scrollY);
                                return;
                            }}
                        }}
                    }}""")
                    page.wait_for_timeout(600)
                    # Measure the table's actual content height (direct children)
                    info = page.evaluate(f"""() => {{
                        let tbl = null;
                        for (const w of document.querySelectorAll('.table-flex-wrapper')) {{
                            if (!w.innerText.includes('{keyword}')) continue;
                            tbl = w.querySelector('table.testResultTable');
                            if (tbl) break;
                        }}
                        if (!tbl) return null;
                        const tbody = tbl.tBodies[0] || tbl;
                        const rows = Array.from(tbody.children).filter(e=>e.tagName==='TR');
                        if (!rows.length) return null;
                        const fr = rows[0].getBoundingClientRect();
                        // Last row with non-empty text (skip empty hrDouble separators)
                        let lastRow = rows[rows.length - 1];
                        for (let i = rows.length - 1; i >= 0; i--) {{
                            const t = (rows[i].innerText || '').trim();
                            if (t.length > 0) {{ lastRow = rows[i]; break; }}
                        }}
                        const lr = lastRow.getBoundingClientRect();
                        const tr = tbl.getBoundingClientRect();
                        return {{
                            x: Math.max(0, tr.left), y: Math.max(0, fr.top),
                            w: tr.right - tr.left,
                            contentH: lr.bottom - fr.top,
                            lastText: (lastRow.innerText || '').trim().slice(0, 30),
                        }};
                    }}""")
                    if not info or info["contentH"] <= 0:
                        logger.warning(f"[SRS] No table info for '{keyword}'")
                        return False
                    # Shrink viewport to just fit the content (+ small padding)
                    snap_h = int(info["contentH"]) + 20
                    page.set_viewport_size({"width": viewport_width, "height": snap_h})
                    page.wait_for_timeout(300)
                    # Re-scroll to ensure table is at top after resize
                    page.evaluate(f"""() => {{
                        for (const w of document.querySelectorAll('.table-flex-wrapper')) {{
                            if (!w.innerText.includes('{keyword}')) continue;
                            const t = w.querySelector('table.testResultTable');
                            if (t) {{ t.scrollIntoView({{block: 'start'}}); return; }}
                        }}
                    }}""")
                    page.wait_for_timeout(300)
                    info2 = page.evaluate(f"""() => {{
                        let tbl = null;
                        for (const w of document.querySelectorAll('.table-flex-wrapper')) {{
                            if (!w.innerText.includes('{keyword}')) continue;
                            tbl = w.querySelector('table.testResultTable');
                            if (tbl) break;
                        }}
                        if (!tbl) return null;
                        const tbody = tbl.tBodies[0] || tbl;
                        const rows = Array.from(tbody.children).filter(e=>e.tagName==='TR');
                        if (!rows.length) return null;
                        const fr = rows[0].getBoundingClientRect();
                        let lastRow = rows[rows.length-1];
                        for (let i = rows.length-1; i >= 0; i--) {{
                            if ((rows[i].innerText||'').trim().length > 0) {{
                                lastRow = rows[i]; break;
                            }}
                        }}
                        const lr = lastRow.getBoundingClientRect();
                        const tr = tbl.getBoundingClientRect();
                        return {{x:Math.max(0,tr.left), y:Math.max(0,fr.top),
                                 w:tr.right-tr.left, h:lr.bottom-fr.top+4}};
                    }}""")
                    if info2 and info2["h"] > 0:
                        page.screenshot(path=path, clip={
                            "x": info2["x"], "y": info2["y"],
                            "width": info2["w"], "height": info2["h"],
                        })
                    else:
                        page.screenshot(path=path)
                    # Restore original viewport height
                    page.set_viewport_size({"width": viewport_width, "height": viewport_height})
                    page.wait_for_timeout(200)
                    logger.info(f"[SRS] Captured '{keyword}' clip: {info2}")
                    return True

                try:
                    sms_path = os.path.join(save_dir, f"SRS_sms_result_{segment_name}.png")
                    if _capture_testresult_table("Sms test result", sms_path):
                        results["sms_result"] = sms_path
                        logger.info(f"[SRS] SMS result screenshot saved: {sms_path}")
                    else:
                        logger.warning("[SRS] SMS test result table not found")
                except Exception as exc:
                    logger.warning(f"[SRS] SMS screenshot failed: {exc}")
                self._save_debug(page, "08a_sms_result")

                # 8b: Voice Call result — summary table + voiceCallTable + pagination,
                #     stopping before the BROWSER section.
                #     Uses same viewport-shrink approach as SMS to avoid black padding.
                try:
                    voice_path = os.path.join(save_dir, f"SRS_voice_result_{segment_name}.png")
                    # Step 1: scroll Voice Call wrapper into view
                    page.evaluate("""() => {
                        for (const w of document.querySelectorAll('.table-flex-wrapper')) {
                            if ((w.innerText||'').includes('Voice Call test result')) {
                                w.scrollIntoView({block: 'start'});
                                window.scrollTo(window.scrollX, window.scrollY);
                                return;
                            }
                        }
                    }""")
                    page.wait_for_timeout(600)
                    # Step 2: measure viewport-relative bounds of all voice elements
                    vinfo = page.evaluate("""() => {
                        let wrap = null;
                        for (const w of document.querySelectorAll('.table-flex-wrapper')) {
                            if ((w.innerText||'').includes('Voice Call test result')) {
                                wrap = w; break;
                            }
                        }
                        if (!wrap) return null;
                        const dataTbl = document.querySelector('[data-testid="voiceCallTable"]');
                        // Pagination is a direct next sibling of dataTbl
                        const pag = dataTbl?.nextElementSibling || null;
                        const items = [wrap, dataTbl, pag].filter(Boolean);
                        const tops  = items.map(e => e.getBoundingClientRect().top);
                        const bots  = items.map(e => e.getBoundingClientRect().bottom);
                        const lefts = items.map(e => e.getBoundingClientRect().left);
                        const rights= items.map(e => e.getBoundingClientRect().right);
                        const top   = Math.min(...tops);
                        const bot   = Math.max(...bots);
                        const left  = Math.min(...lefts);
                        const right = Math.max(...rights);
                        return {
                            x: Math.max(0, left), y: Math.max(0, top),
                            w: right - left, contentH: bot - top,
                            items: items.length,
                        };
                    }""")
                    if vinfo and vinfo["contentH"] > 0:
                        # Step 3: scroll Voice Call wrap to top of viewport (no resize yet)
                        page.wait_for_timeout(300)
                        # Step 4: measure BROWSER top in the current (full) viewport
                        # Use document-absolute scrollY to find BROWSER, then compute
                        # how many px from wrap.top to just before BROWSER.
                        vinfo2 = page.evaluate("""() => {
                            let wrap = null;
                            for (const w of document.querySelectorAll('.table-flex-wrapper')) {
                                if ((w.innerText||'').includes('Voice Call test result')) {
                                    wrap = w; break;
                                }
                            }
                            if (!wrap) return null;
                            const dataTbl = document.querySelector('[data-testid="voiceCallTable"]');
                            // Pagination is direct next sibling of dataTbl
                            const pag = dataTbl?.nextElementSibling || null;
                            const items = [wrap, dataTbl, pag].filter(Boolean);
                            const tops  = items.map(e => e.getBoundingClientRect().top);
                            const bots  = items.map(e => e.getBoundingClientRect().bottom);
                            const lefts = items.map(e => e.getBoundingClientRect().left);
                            const rights= items.map(e => e.getBoundingClientRect().right);
                            const wrapTop   = Math.min(...tops);
                            const contentBot = Math.max(...bots);
                            const left  = Math.min(...lefts);
                            const right = Math.max(...rights);
                            // BROWSER: walk DOM upward from dataTbl to find its
                            // container, then the next major sibling accordion
                            let browserTop = contentBot + 300;
                            // Walk siblings after the pagination
                            if (pag) {
                                let sib = pag.nextElementSibling;
                                for (let i = 0; sib && i < 8; i++, sib = sib.nextElementSibling) {
                                    const r = sib.getBoundingClientRect();
                                    if (r.top > wrapTop) {
                                        // first sibling below voice content = next section
                                        browserTop = r.top;
                                        break;
                                    }
                                }
                            }
                            // Also search parent container's siblings for next accordion
                            const container = dataTbl?.parentElement?.parentElement;
                            if (container) {
                                let sib = container.nextElementSibling;
                                for (let i = 0; sib && i < 5; i++, sib = sib.nextElementSibling) {
                                    const r = sib.getBoundingClientRect();
                                    if (r.top > wrapTop && r.top < browserTop) {
                                        browserTop = r.top;
                                        break;
                                    }
                                }
                            }
                            const clipH = Math.min(contentBot, browserTop - 8) - wrapTop + 4;
                            return {
                                x: Math.max(0, left),
                                y: Math.max(0, wrapTop),
                                w: right - left,
                                h: Math.max(clipH, 10),
                                dbg: {wrapTop, contentBot, browserTop, clipH},
                            };
                        }""")
                        logger.warning(f"[SRS] vinfo2={vinfo2}")
                        if vinfo2 and vinfo2["h"] > 0:
                            page.screenshot(path=voice_path, clip={
                                "x": vinfo2["x"], "y": vinfo2["y"],
                                "width": vinfo2["w"], "height": vinfo2["h"],
                            })
                        else:
                            page.screenshot(path=voice_path)
                        page.wait_for_timeout(200)
                        results["voice_result"] = voice_path
                        logger.warning(
                            f"[SRS] Voice call result saved: {voice_path}"
                        )
                    else:
                        logger.warning("[SRS] Voice Call test result section not found")
                except Exception as exc:
                    logger.warning(f"[SRS] Voice screenshot failed: {exc}")
                    page.set_viewport_size({"width": viewport_width,
                                            "height": viewport_height})
                self._save_debug(page, "08b_voice_result")

            except Exception as exc:
                logger.warning(f"[SRS] Test result tab capture failed: {exc}")

            # --- Step 9: Full-page screenshot ---
            try:
                page.screenshot(path=save_path, full_page=True)
                results["full_page"] = save_path
                logger.info(f"[SRS] Full page screenshot saved: {save_path}")
            except Exception as exc:
                logger.error(f"[SRS] Full page screenshot failed: {exc}")

            return results if results else None

        except Exception as exc:
            logger.error(f"[SRS] Measurement monitoring capture failed: {exc}")
            if page:
                self._save_debug(page, "error_measurement")
            return None
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


def capture_srs_screenshots(
    urls_and_paths: list[tuple[str, str]],
    port: int = CDP_PORT,
    debug_dir: str = "",
) -> list[str]:
    """High-level function: connect to Edge and capture SRS pages.

    Args:
        urls_and_paths: List of (url, save_path) tuples
        port: CDP debug port
        debug_dir: Directory for debug screenshots

    Returns:
        List of successfully saved screenshot paths
    """
    if not is_edge_debug_port_open(port):
        logger.error(f"[SRS] Edge debug port {port} is not open")
        return []

    try:
        with SrsBrowserSession(port=port, debug_dir=debug_dir) as session:
            return session.capture_multiple(urls_and_paths)
    except Exception as exc:
        logger.error(f"[SRS] Capture session failed: {exc}")
        return []
