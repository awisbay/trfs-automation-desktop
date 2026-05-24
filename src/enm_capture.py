"""
ENM GUI screenshot capture.

For items marked with (ENM ...) in the commands file, this module handles
capturing screenshots either semi-automatically by browser automation or by
picking up manually saved screenshots from a folder.
"""
import os
import time
import logging
import re
from typing import Optional

from config_loader import AppConfig

logger = logging.getLogger(__name__)

import sys

# When running as a PyInstaller frozen exe, Playwright's PipeTransport
# auto-sets PLAYWRIGHT_BROWSERS_PATH=0, which makes it look for browsers
# inside the bundle directory.  We don't bundle browsers (too large ~500MB),
# so we pre-set the env var to the default system cache so Playwright finds
# the normally-installed Chromium.
if getattr(sys, "frozen", False) and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    if os.name == "nt":
        _default = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    else:
        _default = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _default

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.info("playwright not available - ENM browser capture will be manual only")


def ensure_chromium_installed() -> bool:
    """Check if Playwright Chromium is installed; auto-install if missing.

    Returns True if Chromium is ready to use, False otherwise.

    Strategy:
        1. Look on disk for the Playwright Chromium cache directory. If we
           find one with an ``INSTALLATION_COMPLETE`` marker, trust it —
           this is the fastest and most reliable check and does not need
           to spawn a subprocess (which is fragile in PyInstaller bundles).
        2. If on-disk check is inconclusive, try launching Chromium via
           Playwright. On success, we're ready.
        3. If launch fails, attempt ``playwright install chromium`` and
           retry the launch.

    Every failure path logs its reason so the UI can tell the user
    **why** Chromium was reported missing.
    """
    if not PLAYWRIGHT_AVAILABLE:
        logger.warning(
            "[ENM] Playwright package is not importable in this Python "
            "interpreter (sys.executable=%s). ENM screenshots will be SKIPPED. "
            "FIX: launch the GUI with the project venv — "
            "`venv\\Scripts\\python.exe src\\gui_app.py` (or use run_gui.bat).",
            sys.executable,
        )
        return False

    # ── Step 1: on-disk probe ────────────────────────────────────────
    def _browsers_dir() -> str:
        env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if env_path and env_path != "0":
            return env_path
        if os.name == "nt":
            return os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
        return os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")

    browsers_dir = _browsers_dir()
    try:
        if os.path.isdir(browsers_dir):
            # Any dir starting with "chromium-" that has INSTALLATION_COMPLETE
            # is a valid install.
            for entry in os.listdir(browsers_dir):
                if not entry.startswith("chromium-"):
                    continue
                full = os.path.join(browsers_dir, entry)
                if os.path.isfile(os.path.join(full, "INSTALLATION_COMPLETE")):
                    logger.info(f"[ENM] Chromium found on disk: {full}")
                    return True
            logger.info(
                f"[ENM] No Chromium cache under {browsers_dir} — will try launching"
            )
        else:
            logger.info(f"[ENM] Playwright browsers dir does not exist: {browsers_dir}")
    except Exception as exc:
        logger.warning(f"[ENM] On-disk chromium probe failed: {exc}")

    # ── Step 2: live launch test ─────────────────────────────────────
    try:
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        finally:
            pw.stop()
        logger.info("[ENM] Chromium launch test passed")
        return True
    except Exception as exc:
        logger.warning(f"[ENM] Chromium launch test failed: {exc!r}")

    # ── Step 3: attempt auto-install then retry ──────────────────────
    import subprocess
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        node_exe, cli_js = compute_driver_executable()
        env = get_driver_env()
    except Exception as exc:
        logger.warning(f"[ENM] Cannot locate Playwright driver for install: {exc!r}")
        return False

    logger.info("[ENM] Chromium not usable — attempting auto-install...")
    try:
        result = subprocess.run(
            [node_exe, cli_js, "install", "chromium"],
            env=env, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            logger.warning(
                f"[ENM] Chromium install failed (rc={result.returncode}): "
                f"stdout={result.stdout[-400:]!r} stderr={result.stderr[-400:]!r}"
            )
            return False
        logger.info("[ENM] Chromium installed — re-verifying launch")
    except subprocess.TimeoutExpired:
        logger.warning("[ENM] Chromium install timed out after 5 minutes")
        return False
    except Exception as exc:
        logger.warning(f"[ENM] Chromium install error: {exc!r}")
        return False

    # Re-verify after install
    try:
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        finally:
            pw.stop()
        return True
    except Exception as exc:
        logger.warning(f"[ENM] Chromium still not launchable after install: {exc!r}")
        return False

try:
    import pyautogui
    PYAUTOGUI_AVAILABLE = True
except ImportError:
    PYAUTOGUI_AVAILABLE = False
    logger.info("pyautogui not available - screen capture will be manual only")


def _find_visible_locator(page, selectors):
    """Return the first visible locator matching any selector in order."""
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()
            for idx in range(count):
                candidate = locator.nth(idx)
                try:
                    if candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _click_any(page, selectors, timeout_ms: int):
    """Click the first matching selector from a candidate list."""
    last_error = None
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()
            for idx in range(count):
                candidate = locator.nth(idx)
                if candidate.is_visible():
                    candidate.click(timeout=timeout_ms)
                    return selector
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Could not click any selector from: {selectors}") from last_error


def _fill_any(page, selectors, value: str, timeout_ms: int):
    """Fill the first matching input from a candidate list."""
    last_error = None
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()
            for idx in range(count):
                candidate = locator.nth(idx)
                if candidate.is_visible():
                    candidate.fill(value, timeout=timeout_ms)
                    return selector
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Could not fill any selector from: {selectors}") from last_error


def _click_text_in_locator(locator, timeout_ms: int):
    """Click the first visible item within a locator collection."""
    count = locator.count()
    for idx in range(count):
        candidate = locator.nth(idx)
        try:
            if candidate.is_visible():
                candidate.click(timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


def _maybe_login(page, config: AppConfig) -> bool:
    """
    Try to sign in if a login form is present.

    Returns True if a login flow was attempted.
    """
    timeout_ms = config.enm.timeout_ms if config.enm else 30000

    # Some ENM builds show a legal notice that must be acknowledged first.
    notice_ok = _find_visible_locator(
        page,
        [
            'button#loginNoticeOk',
            'button:has-text("OK")',
        ],
    )
    if notice_ok is not None:
        try:
            notice_ok.click(timeout=timeout_ms)
            page.wait_for_timeout(1000)
        except Exception:
            pass

    password_field = _find_visible_locator(
        page,
        [
            '#loginPassword',
            'input[type="password"]',
            'input[name*="pass" i]',
            'input[id*="pass" i]',
            'input[placeholder*="pass" i]',
        ],
    )
    if password_field is None:
        return False

    username_field = _find_visible_locator(
        page,
        [
            '#loginUsername',
            'input[type="email"]',
            'input[name*="user" i]',
            'input[id*="user" i]',
            'input[placeholder*="user" i]',
            'input[type="text"]',
        ],
    )
    if username_field is None:
        return False

    username_field.fill(config.ssh.username, timeout=timeout_ms)
    password_field.fill(config.ssh.password, timeout=timeout_ms)

    try:
        _click_any(
            page,
            [
                'button#submit',
                'button:has-text("Login")',
                'button:has-text("Sign in")',
                'button:has-text("Sign In")',
                '[role="button"]:has-text("Login")',
                '[role="button"]:has-text("Sign in")',
                'input[type="submit"]',
            ],
            timeout_ms,
        )
    except Exception:
        password_field.press("Enter")

    page.wait_for_timeout(1500)
    return True


def _open_search_tab(page, timeout_ms: int) -> None:
    """Open the Network Search tab in the left panel.

    Poll for the tab because the Network panel may not be fully rendered
    yet immediately after Cell Management loads.
    """
    selectors = [
        'div.ebTabs-tabItem:has-text("Search")',
        'span.ebTabs-tabItem:has-text("Search")',
        'text="Search"',
    ]
    tab = None
    for attempt in range(6):  # up to ~12s polling
        tab = _find_visible_locator(page, selectors)
        if tab is not None:
            break
        logger.info(f"Search tab not yet visible, retry {attempt + 1}/6")
        page.wait_for_timeout(2000)

    if tab is None:
        raise RuntimeError("Could not find the Search tab in the ENM Network panel.")
    tab.click(timeout=timeout_ms)
    page.wait_for_timeout(2000)


def _open_cell_management_app(page, timeout_ms: int) -> None:
    """Open the Cell Management app from the launcher after login."""
    try:
        page.get_by_role("link", name="Ericsson Network Manager").click(timeout=timeout_ms)
        page.wait_for_timeout(2000)
    except Exception:
        pass

    try:
        page.get_by_text("Cell Management", exact=True).click(timeout=timeout_ms)
    except Exception:
        # If we are already in Cell Management, continue.
        pass

    page.wait_for_timeout(8000)


def _open_alarm_viewer_app(page, config: AppConfig, timeout_ms: int) -> None:
    """Open the Alarm Viewer app and wait for it to fully load.

    The URL may gain dynamic query params (e.g. ?workspaceId=...) after
    navigation — that is expected and must not be treated as an error.

    After login, ENM redirects to #launcher/groups — so we navigate to
    the alarm_url a second time to land on the correct page.
    """
    try:
        page.goto(config.enm.alarm_url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass

    page.wait_for_timeout(3000)

    logged_in = _maybe_login(page, config)

    if logged_in:
        # ENM redirects to #launcher/groups after login — navigate back to alarm URL
        logger.info("Login detected, re-navigating to alarm URL after auth redirect...")
        page.wait_for_timeout(2000)
        try:
            page.goto(config.enm.alarm_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass

    # Wait for alarm viewer to fully load (URL may get ?workspaceId=... appended — that's fine)
    # 5s was not enough on slower ENM responses — the Network panel and its
    # buttons render after the initial DOM is ready. Give it more time.
    page.wait_for_timeout(10000)
    logger.info(f"Alarm Viewer URL after load: {page.url}")


def _open_shm_software_administration(page, config: AppConfig, timeout_ms: int) -> None:
    """Open the SHM Software Administration workspace."""
    try:
        page.goto(config.enm.shm_url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass

    page.wait_for_timeout(4000)
    logged_in = _maybe_login(page, config)
    if logged_in:
        # ENM redirects to #launcher/groups after login — navigate back to SHM URL
        logger.info("Login detected, re-navigating to SHM URL after auth redirect...")
        page.wait_for_timeout(2000)
        try:
            page.goto(config.enm.shm_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        page.wait_for_timeout(4000)
    try:
        page.get_by_role("link", name="Software and Hardware Manager").click(timeout=timeout_ms)
        page.wait_for_timeout(4000)
    except Exception:
        try:
            page.get_by_text("Software and Hardware Manager", exact=False).click(timeout=timeout_ms)
            page.wait_for_timeout(4000)
        except Exception:
            pass
    try:
        page.get_by_role("link", name="Software Administration").click(timeout=timeout_ms)
        page.wait_for_timeout(4000)
    except Exception:
        try:
            page.get_by_text("Software Administration", exact=False).click(timeout=timeout_ms)
            page.wait_for_timeout(4000)
        except Exception:
            pass


def _is_site_already_in_network_panel(page, site_name: str) -> bool:
    """Check if the site node is already visible in the left Network panel.

    ENM remembers topology per user workspace on the server side, so a
    previously added site may still be present even in a fresh browser.
    """
    try:
        # Look for the site name anywhere in the left Network panel area
        locator = page.locator(f'text="{site_name}"')
        if locator.count() > 0:
            for idx in range(locator.count()):
                try:
                    if locator.nth(idx).is_visible():
                        return True
                except Exception:
                    continue
        # Also try partial match (ENM may truncate long node names)
        short_name = site_name[:20]
        partial = page.locator(f'text=/{re.escape(short_name)}/')
        if partial.count() > 0:
            for idx in range(partial.count()):
                try:
                    if partial.nth(idx).is_visible():
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def _open_add_topology_data(page, timeout_ms: int) -> None:
    """Open the Add Topology Data panel/button.

    The button may not be rendered immediately after the Alarm Viewer loads,
    so we poll for it with short waits before giving up.
    """
    selectors = [
        'button:has-text("Add Topology Data")',
        'span.ebBtn-caption:has-text("Add Topology Data")',
        'text="Add Topology Data"',
    ]
    button = None
    for attempt in range(6):  # up to ~12s of polling
        button = _find_visible_locator(page, selectors)
        if button is not None:
            break
        logger.info(f"Add Topology Data button not yet visible, retry {attempt + 1}/6")
        page.wait_for_timeout(2000)

    if button is None:
        raise RuntimeError("Could not find the Add Topology Data button.")
    button.evaluate("(el) => el.click()")
    page.wait_for_timeout(2000)


def _clear_existing_nodes_from_panel(page, timeout_ms: int) -> int:
    """Right-click every node currently in the left Network panel and pick
    "Remove" from the context menu, so we start from a clean panel before
    adding the intended site.

    Returns the number of nodes removed. Failures are logged but not raised —
    we'd rather proceed with topology add than abort on a stale node.
    """
    removed = 0
    # Safety cap — don't loop forever if Remove doesn't actually take effect
    for _ in range(20):
        items = page.locator('.elUmpLib-NodeItem').all()
        visible_items = []
        for it in items:
            try:
                if it.is_visible():
                    visible_items.append(it)
            except Exception:
                continue
        if not visible_items:
            break

        target = visible_items[0]
        try:
            label = (target.locator('.elUmpLib-NodeItem-label').first
                     .inner_text(timeout=1000))
        except Exception:
            label = "<unknown>"

        try:
            target.click(button="right", timeout=timeout_ms, force=True)
        except Exception as exc:
            logger.warning(f"[ENM] right-click on node '{label}' failed: {exc}")
            break

        # Wait for context menu, then click "Remove"
        try:
            remove_item = page.locator(
                '.ebComponentList-item-name', has_text="Remove"
            ).first
            remove_item.wait_for(state="visible", timeout=5000)
            remove_item.click(timeout=timeout_ms)
            removed += 1
            logger.info(f"[ENM] Removed node '{label}' from Network panel")
            page.wait_for_timeout(800)
        except Exception as exc:
            logger.warning(
                f"[ENM] 'Remove' menu item not clickable for '{label}': {exc}"
            )
            # Dismiss the open menu before giving up
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            break

    if removed:
        logger.info(f"[ENM] Cleared {removed} existing node(s) from Network panel")
    else:
        logger.info("[ENM] Network panel was already empty — nothing to clear")
    return removed


def _add_topology_for_site(page, site_name: str, timeout_ms: int) -> None:
    """Search topology by site name in the Add Network Objects sidebar,
    tick the checkbox, click Add, then close the sidebar."""

    # Click the "Search" tab inside the Add Network Objects sidebar
    tab = _find_visible_locator(
        page,
        [
            'div.ebTabs-tabItem:has-text("Search")',
            'span.ebTabs-tabItem:has-text("Search")',
        ],
    )
    if tab is not None:
        tab.click(timeout=timeout_ms)
        page.wait_for_timeout(1500)

    search_box = _find_visible_locator(
        page,
        [
            'input.elNetworkExplorerLib-wSimpleSearchInput-searchInput',
            'input[placeholder="Enter Search Criteria"]',
            'input[name="criteria"]',
        ],
    )
    if search_box is None:
        raise RuntimeError("Could not find the topology search input for Alarm Viewer.")

    search_box.fill(site_name, timeout=timeout_ms)
    try:
        search_box.press("Enter")
    except Exception:
        pass

    page.wait_for_timeout(1000)

    search_button = _find_visible_locator(
        page,
        [
            'button.elNetworkExplorerLib-rSimpleSearch-form-searchBtn',
            'button[type="submit"]',
            'button:has-text("Search")',
        ],
    )
    if search_button is not None:
        try:
            search_button.click(timeout=timeout_ms)
        except Exception:
            pass

    page.wait_for_timeout(2500)

    # Tick the checkbox next to the search result
    checkbox = _find_visible_locator(
        page,
        [
            'input.ebCheckbox',
            'input[type="checkbox"]',
        ],
    )
    if checkbox is None:
        raise RuntimeError("Could not find a topology checkbox to select.")

    checkbox.click(timeout=timeout_ms)
    page.wait_for_timeout(1000)

    # Click the "Add" button at the bottom of the sidebar.
    # Must use exact text match — "Add Topology Data" also contains "Add".
    # Target: <span class="ebBtn-caption">Add</span>
    add_button = page.locator('span.ebBtn-caption', has_text="Add").filter(has_text=re.compile(r'^Add$'))
    clicked = False
    try:
        count = add_button.count()
        for idx in range(count):
            candidate = add_button.nth(idx)
            if candidate.is_visible():
                candidate.click(timeout=timeout_ms)
                clicked = True
                logger.info("Clicked the Add button in sidebar")
                break
    except Exception:
        pass

    if not clicked:
        # Fallback: find by JS — look for span.ebBtn-caption with exact "Add" text
        try:
            page.evaluate("""() => {
                const spans = document.querySelectorAll('span.ebBtn-caption');
                for (const s of spans) {
                    if (s.textContent.trim() === 'Add') {
                        s.closest('button') ? s.closest('button').click() : s.click();
                        return true;
                    }
                }
                return false;
            }""")
            clicked = True
            logger.info("Clicked the Add button via JS fallback")
        except Exception:
            pass

    if not clicked:
        raise RuntimeError("Could not find or click the Add button in sidebar.")

    page.wait_for_timeout(3000)

    # Close the "Add Network Objects" sidebar by clicking X or Cancel
    _close_add_network_objects_sidebar(page, timeout_ms)


def _close_add_network_objects_sidebar(page, timeout_ms: int) -> None:
    """Close the Add Network Objects sidebar after adding topology."""
    # Try the X close button first (top-right of the sidebar)
    close_btn = _find_visible_locator(
        page,
        [
            'button.ebDialogBox-actionBlock-close',
            'button.ebDialog-close',
            'button[title="Close"]',
            'span.ebIcon_close',
        ],
    )
    if close_btn is not None:
        try:
            close_btn.click(timeout=timeout_ms)
            page.wait_for_timeout(1000)
            logger.info("Closed Add Network Objects sidebar via X button")
            return
        except Exception:
            pass

    # Try Cancel button
    cancel_btn = _find_visible_locator(
        page,
        [
            'button:has-text("Cancel")',
            'span.ebBtn-caption:has-text("Cancel")',
        ],
    )
    if cancel_btn is not None:
        try:
            cancel_btn.click(timeout=timeout_ms)
            page.wait_for_timeout(1000)
            logger.info("Closed Add Network Objects sidebar via Cancel button")
            return
        except Exception:
            pass

    # Fallback: try clicking the X icon shown in the screenshot (small box with X)
    try:
        page.evaluate("""() => {
            // Look for close buttons near "Add Network Objects" text
            const allButtons = document.querySelectorAll('button, [role="button"], .ebIcon_close, .ebDialogBox-closeIcon');
            for (const btn of allButtons) {
                const rect = btn.getBoundingClientRect();
                // The X button is in the top-right area of the sidebar
                if (rect.x > 1100 && rect.y < 100 && rect.width < 40) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }""")
        page.wait_for_timeout(1000)
        logger.info("Closed Add Network Objects sidebar via fallback JS click")
    except Exception:
        logger.warning("Could not close the Add Network Objects sidebar")


def capture_shm_software_administration_screenshot(
    config: AppConfig,
    save_path: str,
) -> str:
    """
    Open the SHM Software Administration page, add topology, and capture a screenshot.
    """
    if not config.enm or not config.enm.enabled:
        raise RuntimeError("ENM browser capture is not enabled in config.yaml.")
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "playwright is not installed. Install it with: pip install -r requirements.txt"
        )

    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    timeout_ms = config.enm.timeout_ms
    viewport = {
        "width": config.enm.viewport_width,
        "height": config.enm.viewport_height,
    }

    browser = None
    context = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=config.enm.headless)
            context = browser.new_context(
                viewport=viewport,
                ignore_https_errors=True,
            )
            page = context.new_page()
            _open_shm_software_administration(page, config, timeout_ms)

            if _is_site_already_in_network_panel(page, config.site.node_name):
                logger.info(f"Site already present in SHM, skipping Add Topology")
            else:
                _open_add_topology_data(page, timeout_ms)
                _add_topology_for_site(page, config.site.node_name, timeout_ms)

            page.wait_for_timeout(3000)
            page.screenshot(path=save_path, full_page=True)
            logger.info(f"ENM SHM screenshot saved: {save_path}")
            return save_path
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass


def _search_site_and_open(page, site_name: str, timeout_ms: int) -> None:
    """Search for the site and open the matching result row."""
    search_box = _find_visible_locator(
        page,
        [
            'input.elNetworkExplorerLib-wSimpleSearchInput-searchInput',
            'input[placeholder="Enter Search Criteria"]',
            'input[name="criteria"]',
        ],
    )
    if search_box is None:
        raise RuntimeError("Could not find the ENM simple search input.")

    search_box.fill(site_name, timeout=timeout_ms)
    try:
        search_box.press("Enter")
    except Exception:
        pass

    page.wait_for_timeout(1200)

    search_button = _find_visible_locator(
        page,
        [
            'button.elNetworkExplorerLib-rSimpleSearch-form-searchBtn',
            'button[type="submit"]',
        ],
    )
    if search_button is not None:
        try:
            search_button.click(timeout=timeout_ms)
        except Exception:
            pass

    page.wait_for_timeout(2500)

    _click_any(
        page,
        [
            f'td:has-text("{site_name}")',
            f'.ebTableCell:has-text("{site_name}")',
            f'tr:has-text("{site_name}")',
            f'text="{site_name}"',
        ],
        timeout_ms,
    )
    page.wait_for_timeout(1500)


def _click_tab(page, tab_name: str, timeout_ms: int) -> None:
    """Click a tab or tab-like control by visible text."""
    _click_any(
        page,
        [
            f'div.ebTabs-tabItem:has-text("{tab_name}")',
            f'span.ebTabs-tabItem:has-text("{tab_name}")',
            f'text="{tab_name}"',
        ],
        timeout_ms,
    )


def _adjust_multi_sliding_panel(page, target_left_px: int | None, timeout_ms: int) -> None:
    """
    Move the ENM multi-sliding panel splitter to a target left position.

    The UI exposes a draggable resize handle. We first try to drag it into place,
    then we fall back to a direct style adjustment if the drag is not enough.
    """
    if target_left_px is None:
        return

    handle = _find_visible_locator(
        page,
        [
            'div.elLayouts-MultiSlidingPanels-resizeElement_left',
        ],
    )
    if handle is None:
        return

    try:
        handle.scroll_into_view_if_needed(timeout=timeout_ms)
    except Exception:
        pass

    try:
        current_left = handle.evaluate(
            """(el) => {
                const value = window.getComputedStyle(el).left || el.style.left || "";
                const parsed = parseFloat(value);
                return Number.isFinite(parsed) ? parsed : null;
            }"""
        )
    except Exception:
        current_left = None

    try:
        box = handle.bounding_box()
    except Exception:
        box = None

    if box is not None:
        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + box["height"] / 2
        if current_left is not None:
            delta_x = target_left_px - current_left
        else:
            delta_x = -90

        try:
            page.mouse.move(start_x, start_y)
            page.mouse.down()
            page.mouse.move(start_x + delta_x, start_y, steps=10)
            page.mouse.up()
            page.wait_for_timeout(800)
        except Exception:
            pass

    try:
        handle.evaluate(
            """(el, targetLeftPx) => {
                const root = el.closest('.elLayouts-MultiSlidingPanels') || el.parentElement;
                if (!root) {
                    return false;
                }

                const leftPane = root.querySelector('.elLayouts-MultiSlidingPanels-leftWrapper');
                const rightPane = root.querySelector('.elLayouts-MultiSlidingPanels-rightWrapper');
                el.style.left = `${targetLeftPx}px`;
                if (leftPane) {
                    leftPane.style.width = `${targetLeftPx}px`;
                }
                if (rightPane) {
                    rightPane.style.left = `${targetLeftPx + 1}px`;
                }
                root.dispatchEvent(new Event('resize', { bubbles: true }));
                window.dispatchEvent(new Event('resize'));
                return true;
            }""",
            target_left_px,
        )
        page.wait_for_timeout(500)
    except Exception:
        pass


def _scroll_table_to_suffixes(page, suffixes: list[str], timeout_ms: int) -> None:
    """
    Scroll the active cell table until a row containing one of the suffixes is visible.

    ENM uses a virtualized or clipped grid, so the table may not show all rows at once.
    We look for rows in the right-side table and bring the first matching row into view.
    """
    if not suffixes:
        return

    right_rows = page.locator('div.elLayouts-MultiSlidingPanels-rightWrapper tr')
    row_count = right_rows.count()
    if row_count == 0:
        return

    normalized_patterns = [s.strip() for s in suffixes if s and s.strip()]
    if not normalized_patterns:
        return

    for idx in range(row_count):
        row = right_rows.nth(idx)
        try:
            row_text = row.inner_text(timeout=timeout_ms).strip()
            if any(pattern in row_text for pattern in normalized_patterns):
                row.scroll_into_view_if_needed(timeout=timeout_ms)
                page.wait_for_timeout(800)
                return
        except Exception:
            continue

    # Fallback: nudge the ENM table scrollbar downward a little and leave the
    # table in a more useful position even if the exact suffix is not present.
    thumb = _find_visible_locator(
        page,
        [
            'div.elWidgets-ScrollBar-thumb',
        ],
    )
    if thumb is None:
        return

    try:
        box = thumb.bounding_box()
    except Exception:
        box = None

    if not box:
        return

    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    try:
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        page.mouse.move(start_x, start_y + 120, steps=12)
        page.mouse.up()
        page.wait_for_timeout(800)
    except Exception:
        pass


class EnmBrowserSession:
    """
    A single persistent Playwright browser session that handles all ENM
    captures for one band. Login happens once; subsequent captures just
    navigate to a different URL on the same page so cookies/session are
    preserved. Use as a context manager.

    Debug artifacts: on any capture failure, a debug_<stage>.png is saved
    to the screenshots dir so the caller can inspect the page state.
    """

    def __init__(self, config: AppConfig, debug_dir: str):
        if not config.enm or not config.enm.enabled:
            raise RuntimeError("ENM browser capture is not enabled in config.yaml.")
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("playwright is not installed.")

        self.config = config
        self.debug_dir = debug_dir
        os.makedirs(debug_dir, exist_ok=True)
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None
        self._logged_in = False

    def __enter__(self):
        timeout_ms = self.config.enm.timeout_ms
        viewport = {
            "width": self.config.enm.viewport_width,
            "height": self.config.enm.viewport_height,
        }
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.config.enm.headless)
        self._context = self._browser.new_context(
            viewport=viewport,
            ignore_https_errors=True,
        )
        self.page = self._context.new_page()

        # Initial login — navigate to the base ENM page and sign in once.
        login_url = self.config.enm.url
        logger.info(f"[ENM] Opening session and logging in: {login_url}")
        try:
            self.page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            self._save_debug("login_nav_failed")
            raise RuntimeError(f"Failed to navigate to ENM base URL: {exc}") from exc

        self.page.wait_for_timeout(2000)
        try:
            self._logged_in = _maybe_login(self.page, self.config)
        except Exception as exc:
            self._save_debug("login_flow_failed")
            raise RuntimeError(f"ENM login flow failed: {exc}") from exc

        if self._logged_in:
            logger.info("[ENM] Login successful — session is live.")
            self.page.wait_for_timeout(2000)
        else:
            logger.info("[ENM] Page loaded without login form (already authenticated).")

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        return False

    def _save_debug(self, stage: str) -> str:
        """Save a debug screenshot + current URL to help diagnose failures."""
        try:
            path = os.path.join(self.debug_dir, f"debug_enm_{stage}.png")
            self.page.screenshot(path=path, full_page=True)
            logger.info(f"[ENM] Debug screenshot: {path} (url={self.page.url})")
            return path
        except Exception as exc:
            logger.warning(f"[ENM] Could not save debug screenshot for {stage}: {exc}")
            return ""

    def capture_cell_management(
        self,
        save_base_path: str,
        band: str | None = None,
        tabs_override: list[str] | None = None,
    ) -> list[str]:
        timeout_ms = self.config.enm.timeout_ms
        os.makedirs(os.path.dirname(save_base_path), exist_ok=True)
        page = self.page

        try:
            page.goto(self.config.enm.url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            self._save_debug(f"cellmgmt_nav_{band}")
            raise RuntimeError(f"Cell Management navigation failed: {exc}") from exc

        page.wait_for_timeout(3000)

        try:
            _open_cell_management_app(page, timeout_ms)
            _open_search_tab(page, timeout_ms)
            _search_site_and_open(page, self.config.site.node_name, timeout_ms)
            _adjust_multi_sliding_panel(page, self.config.enm.panel_splitter_left_px, timeout_ms)
        except Exception as exc:
            self._save_debug(f"cellmgmt_setup_{band}")
            raise RuntimeError(f"Cell Management setup failed: {exc}") from exc

        tabs_to_capture = tabs_override if tabs_override else self.config.enm.tabs
        # IMPORTANT: per-tab failures must NOT abort the whole Cell Management
        # pass. Under parallel multi-node load we have seen the 2nd tab
        # (e.g. "LTE Cells") occasionally timeout on element render while the
        # 1st tab ("NR Cells") captured fine. Previously we raised on the
        # first failure, losing every sibling tab and the alarm/SHM siblings
        # too (because the caller wraps this whole call in one try/except).
        # Now we log, save a debug dump, append None, and continue.
        screenshots: list[str | None] = []
        for idx, tab_name in enumerate(tabs_to_capture, start=1):
            try:
                _click_tab(page, tab_name, timeout_ms)
                page.wait_for_timeout(2000)
                if band:
                    _scroll_table_to_suffixes(
                        page,
                        self.config.enm.band_cell_patterns.get(band, []),
                        timeout_ms,
                    )
                save_path = f"{save_base_path}_{idx}.png"
                page.screenshot(path=save_path, full_page=True)
                screenshots.append(save_path)
                logger.info(f"[ENM] Cell Management screenshot saved: {save_path}")
            except Exception as exc:
                self._save_debug(f"cellmgmt_tab_{band}_{tab_name}")
                logger.warning(
                    f"[ENM] Cell Management tab capture failed for "
                    f"'{tab_name}' (continuing to next tab): {exc}"
                )
                screenshots.append(None)

        return screenshots

    def capture_alarm_viewer(self, save_path: str) -> str:
        timeout_ms = self.config.enm.timeout_ms
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        page = self.page

        try:
            page.goto(self.config.enm.alarm_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            self._save_debug("alarm_nav")
            raise RuntimeError(f"Alarm Viewer navigation failed: {exc}") from exc

        # Give Network panel + toolbar time to render.
        page.wait_for_timeout(10000)
        logger.info(f"[ENM] Alarm Viewer URL after load: {page.url}")

        try:
            # Always clear whatever is already in the left Network panel
            # before adding the target node — otherwise the alarm view shows
            # alarms for the previously-added site(s) instead of (or alongside)
            # the one we actually want.
            try:
                _clear_existing_nodes_from_panel(page, timeout_ms)
            except Exception as clear_exc:
                logger.warning(
                    f"[ENM] Clearing existing nodes failed (continuing): {clear_exc}"
                )

            logger.info(f"[ENM] Adding topology for alarm viewer")
            _open_add_topology_data(page, timeout_ms)
            _add_topology_for_site(page, self.config.site.node_name, timeout_ms)
        except Exception as exc:
            self._save_debug("alarm_addtopo")
            raise RuntimeError(f"Alarm Viewer topology add failed: {exc}") from exc

        page.wait_for_timeout(5000)
        try:
            page.screenshot(path=save_path, full_page=False)
            logger.info(f"[ENM] Alarm Viewer screenshot saved: {save_path}")
            return save_path
        except Exception as exc:
            self._save_debug("alarm_screenshot")
            raise RuntimeError(f"Alarm Viewer screenshot failed: {exc}") from exc

    def capture_shm_software_admin(self, save_path: str) -> str:
        timeout_ms = self.config.enm.timeout_ms
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        page = self.page

        try:
            page.goto(self.config.enm.shm_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            self._save_debug("shm_nav")
            raise RuntimeError(f"SHM navigation failed: {exc}") from exc

        page.wait_for_timeout(4000)

        # Click the drill-down links (Software and Hardware Manager -> Software Administration)
        try:
            page.get_by_role("link", name="Software and Hardware Manager").click(timeout=timeout_ms)
            page.wait_for_timeout(4000)
        except Exception:
            try:
                page.get_by_text("Software and Hardware Manager", exact=False).click(timeout=timeout_ms)
                page.wait_for_timeout(4000)
            except Exception:
                pass
        try:
            page.get_by_role("link", name="Software Administration").click(timeout=timeout_ms)
            page.wait_for_timeout(4000)
        except Exception:
            try:
                page.get_by_text("Software Administration", exact=False).click(timeout=timeout_ms)
                page.wait_for_timeout(4000)
            except Exception:
                pass

        try:
            if _is_site_already_in_network_panel(page, self.config.site.node_name):
                logger.info(f"[ENM] Site already in SHM, skipping Add Topology")
            else:
                _open_add_topology_data(page, timeout_ms)
                _add_topology_for_site(page, self.config.site.node_name, timeout_ms)
        except Exception as exc:
            self._save_debug("shm_addtopo")
            raise RuntimeError(f"SHM topology add failed: {exc}") from exc

        page.wait_for_timeout(3000)
        try:
            page.screenshot(path=save_path, full_page=True)
            logger.info(f"[ENM] SHM screenshot saved: {save_path}")
            return save_path
        except Exception as exc:
            self._save_debug("shm_screenshot")
            raise RuntimeError(f"SHM screenshot failed: {exc}") from exc


def capture_cell_management_screenshots(
    config: AppConfig,
    save_base_path: str,
    band: str | None = None,
    tabs_override: list[str] | None = None,
) -> list[str]:
    """
    Open the ENM page, search the configured site, and capture the tab screenshots.

    Args:
        tabs_override: If provided, only capture these tabs instead of all configured tabs.
                       e.g. ["LTE Cells"] for LTE bands, ["NR Cells"] for NR bands.
    """
    if not config.enm or not config.enm.enabled:
        raise RuntimeError("ENM browser capture is not enabled in config.yaml.")
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "playwright is not installed. Install it with: pip install -r requirements.txt"
        )

    os.makedirs(os.path.dirname(save_base_path), exist_ok=True)

    timeout_ms = config.enm.timeout_ms
    viewport = {
        "width": config.enm.viewport_width,
        "height": config.enm.viewport_height,
    }

    screenshots: list[str] = []
    browser = None
    context = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=config.enm.headless)
            context = browser.new_context(
                viewport=viewport,
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.goto(config.enm.url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1500)

            logged_in = _maybe_login(page, config)
            if logged_in:
                # ENM redirects to #launcher/groups after login — re-navigate
                logger.info("Login detected, re-navigating to Cell Management URL...")
                page.wait_for_timeout(2000)
                try:
                    page.goto(config.enm.url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception:
                    pass
                page.wait_for_timeout(3000)
            _open_cell_management_app(page, timeout_ms)
            _open_search_tab(page, timeout_ms)
            _search_site_and_open(page, config.site.node_name, timeout_ms)
            _adjust_multi_sliding_panel(page, config.enm.panel_splitter_left_px, timeout_ms)

            tabs_to_capture = tabs_override if tabs_override else config.enm.tabs
            for idx, tab_name in enumerate(tabs_to_capture, start=1):
                _click_tab(page, tab_name, timeout_ms)
                page.wait_for_timeout(2000)
                if band:
                    _scroll_table_to_suffixes(
                        page,
                        config.enm.band_cell_patterns.get(band, []),
                        timeout_ms,
                    )

                save_path = f"{save_base_path}_{idx}.png"
                page.screenshot(path=save_path, full_page=True)
                screenshots.append(save_path)
                logger.info(f"ENM browser screenshot saved: {save_path}")

            return screenshots
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass


def capture_alarm_viewer_screenshot(
    config: AppConfig,
    save_path: str,
) -> str:
    """
    Open the Alarm Viewer, add the site topology, and capture a screenshot.

    The final URL may contain dynamic params (e.g. ?workspaceId=...) — this
    is normal ENM behaviour and should not cause failures.
    """
    if not config.enm or not config.enm.enabled:
        raise RuntimeError("ENM browser capture is not enabled in config.yaml.")
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "playwright is not installed. Install it with: pip install -r requirements.txt"
        )

    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    timeout_ms = config.enm.timeout_ms
    viewport = {
        "width": config.enm.viewport_width,
        "height": config.enm.viewport_height,
    }

    browser = None
    context = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=config.enm.headless)
            context = browser.new_context(
                viewport=viewport,
                ignore_https_errors=True,
            )
            page = context.new_page()

            # Open Alarm Viewer (URL may get ?workspaceId=... appended — that's fine)
            _open_alarm_viewer_app(page, config, timeout_ms)

            # Check if the site is already in the Network panel (server-side session)
            if _is_site_already_in_network_panel(page, config.site.node_name):
                logger.info(f"Site {config.site.node_name} already present in Network panel, skipping Add Topology")
            else:
                logger.info(f"Site not found in Network panel, adding topology...")
                _open_add_topology_data(page, timeout_ms)
                _add_topology_for_site(page, config.site.node_name, timeout_ms)

            # Wait for alarm data to fully load
            page.wait_for_timeout(5000)

            # Take viewport-only screenshot (not full_page) to match the actual
            # browser view the user sees — clean view without sidebar
            page.screenshot(path=save_path, full_page=False)
            logger.info(f"ENM alarm viewer screenshot saved: {save_path}")
            return save_path
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass


def capture_screen_region(
    save_path: str,
    region: Optional[tuple] = None,
) -> str:
    """
    Capture a screenshot of the screen or a specific region.

    Args:
        save_path: Path to save the screenshot PNG
        region: Optional (left, top, width, height) tuple for region capture.
                If None, captures the full screen.

    Returns:
        Path to saved screenshot
    """
    if not PYAUTOGUI_AVAILABLE:
        raise RuntimeError("pyautogui is not installed. Install it with: pip install pyautogui")

    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    if region:
        screenshot = pyautogui.screenshot(region=region)
    else:
        screenshot = pyautogui.screenshot()

    screenshot.save(save_path)
    logger.info(f"ENM screenshot captured: {save_path}")
    return save_path


def prompt_and_capture(
    enm_item: str,
    save_path: str,
    band: str,
    category: str,
) -> Optional[str]:
    """
    Prompt the user to navigate to the ENM GUI page, then capture screenshot.

    Args:
        enm_item: The ENM item description (e.g., "ENM CELL M SS")
        save_path: Path to save the screenshot
        band: Current band being processed
        category: Current category

    Returns:
        Path to saved screenshot, or None if skipped
    """
    print(f"\n{'='*60}")
    print(f"  ENM Screenshot Required")
    print(f"  Band: {band} | Category: {category}")
    print(f"  Item: {enm_item}")
    print(f"{'='*60}")
    print(f"  Please open the ENM GUI in your browser and navigate to:")
    print(f"  -> {enm_item}")
    print()

    while True:
        choice = input("  [C]apture screen | [S]kip | [M]anual (provide file path): ").strip().upper()

        if choice == "C":
            if not PYAUTOGUI_AVAILABLE:
                print("  pyautogui not available. Please use [M]anual or [S]kip.")
                continue

            print("  Capturing screenshot in 3 seconds... Switch to ENM browser window!")
            time.sleep(3)
            return capture_screen_region(save_path)

        elif choice == "S":
            logger.info(f"Skipped ENM screenshot: {enm_item}")
            return None

        elif choice == "M":
            file_path = input("  Enter path to screenshot file: ").strip().strip('"')
            if os.path.exists(file_path):
                # Copy to expected location
                import shutil
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                shutil.copy2(file_path, save_path)
                logger.info(f"Manual ENM screenshot copied: {save_path}")
                return save_path
            else:
                print(f"  File not found: {file_path}")
                continue

        else:
            print("  Invalid choice. Please enter C, S, or M.")


def check_manual_screenshots(
    screenshots_dir: str,
    band: str,
    category: str,
) -> list[str]:
    """
    Check for pre-saved manual screenshots in the screenshots directory.

    Expected naming convention: {band}_{category}_ENM_*.png
    e.g., L900_CELL_ENM_1.png

    Args:
        screenshots_dir: Directory to search for screenshots
        band: Band key
        category: Category key (without _ENM suffix)

    Returns:
        List of found screenshot paths
    """
    prefix = f"{band}_{category}_ENM"
    found = []

    if not os.path.exists(screenshots_dir):
        return found

    for filename in sorted(os.listdir(screenshots_dir)):
        if filename.startswith(prefix) and filename.lower().endswith(".png"):
            found.append(os.path.join(screenshots_dir, filename))

    if found:
        logger.info(f"Found {len(found)} pre-saved ENM screenshots for {band}/{category}")

    return found
