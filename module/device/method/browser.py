"""
Browser-based device control for cloud game (星穹铁道网页版).

Uses Selenium + Chrome DevTools Protocol to interact with the
cloud game at https://sr.mihoyo.com/cloud via a real browser.

Screenshots are taken via CDP Page.captureScreenshot (JPEG, fast).
Input (click, swipe, drag, key press) is sent via CDP Input.dispatchMouseEvent
and Input.dispatchKeyEvent.

Heavily inspired by March7thAssistant's CloudGameController.
"""

import base64
import io
import json
import os
import re
import time

import cv2
import numpy as np
from PIL import Image
from selenium import webdriver
from selenium.common.exceptions import (
    SessionNotCreatedException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from module.base.timer import Timer
from module.logger import logger


class BrowserDevice:
    """
    Selenium/CDP based device driver for cloud HSR web version.

    This class is NOT a mixin like other method modules (Adb, Minitouch, etc.)
    because the browser device replaces ALL emulator interaction — there is no
    ADB serial, no uiautomator2, no emulator at all.

    It provides:
    - screenshot() → np.ndarray (1280x720 BGR)
    - click(x, y)
    - long_click(x, y, duration)
    - swipe(p1, p2, duration)
    - drag(p1, p2, ...)
    - press_key(key)
    - app_start() / app_stop() / app_is_running()
    - dump_hierarchy() → lxml etree (parsed from browser DOM)
    """

    GAME_URL = "https://sr.mihoyo.com/cloud"
    BROWSER_TAG = "--starrail-copilot-cloud-web"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, config):
        self.config = config
        self.driver: webdriver.Chrome | None = None
        self._driver_pid = None
        self._webdriver_service = None
        self.orientation = 0  # browser is always landscape

        # Browser settings (read from config, with sane defaults)
        self._browser_path = getattr(config, 'Browser_BrowserPath', '') or None
        self._driver_path = getattr(config, 'Browser_DriverPath', '') or None
        self._headless = getattr(config, 'Browser_Headless', False)
        self._debug_port = getattr(config, 'Browser_DebugPort', 9222)
        self._persistent = getattr(config, 'Browser_PersistentProfile', True)
        self._user_profile_dir = os.path.join(
            os.getcwd(), 'browser_profile'
        )

        # Remote browser support: set Browser_RemoteURL to
        # "http://<ip>:<port>" to connect to a Chrome instance on another
        # machine instead of launching a local one.
        # The remote machine must start Chrome with:
        #   chrome --remote-debugging-port=9222 --remote-debugging-address=0.0.0.0 \
        #        --user-data-dir=/path/to/profile --app=https://sr.mihoyo.com/cloud
        self._remote_url = getattr(config, 'Browser_RemoteURL', '') or None
        self._remote_mode = False  # True when using CDP WebSocket directly (no local Chrome)
        self._cdp_ws = None        # Browser-level CDP WebSocket (for Target commands)
        self._page_ws = None        # Page-level CDP WebSocket (for Page, Input commands)
        self._page_ws_url = None
        self._ws_url = None

    # ------------------------------------------------------------------
    # Browser start / stop
    # ------------------------------------------------------------------

    def _build_chrome_options(self) -> ChromeOptions:
        opts = ChromeOptions()
        opts.add_argument(self.BROWSER_TAG)
        opts.add_argument("--disable-infobars")
        opts.add_argument("--lang=zh-CN")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        # Force 1920x1080 viewport via CDP after session start — see _set_viewport()
        opts.add_argument(f"--force-device-scale-factor=1")
        if self._persistent:
            opts.add_argument(f"--user-data-dir={self._user_profile_dir}")
            opts.add_argument("--profile-directory=Default")
        if self._headless:
            opts.add_argument("--headless=new")
            opts.add_argument("--mute-audio")
        opts.add_argument(f"--app={self.GAME_URL}")
        # Cloud game needs keyboard-lock and clipboard permissions
        prefs = {
            "profile": {
                "content_settings": {
                    "exceptions": {
                        "keyboard_lock": {
                            "https://sr.mihoyo.com:443,*": {"setting": 1}
                        },
                        "clipboard": {
                            "https://sr.mihoyo.com:443,*": {"setting": 1}
                        },
                    }
                }
            }
        }
        opts.add_experimental_option("prefs", prefs)
        opts.add_argument(f"--remote-debugging-port={self._debug_port}")
        return opts

    def browser_start(self):
        """Launch or reconnect to the browser.

        If Browser_RemoteURL is set (e.g. 'http://192.168.1.100:9222'),
        connect to a remote Chrome instance via CDP instead of launching locally.
        This allows running SRC on one machine while the browser runs on another
        (e.g. a headless server with GPU for video decoding, or a different desktop).
        """
        # Remote browser mode: connect via CDP to an already-running Chrome
        if self._remote_url:
            logger.info(f"Connecting to remote browser at {self._remote_url}")
            try:
                import requests as _requests
                from urllib.parse import urlparse as _urlparse
                # Discover WebSocket debugger URL from the remote Chrome
                remote_host = _urlparse(self._remote_url).hostname
                remote_port = _urlparse(self._remote_url).port or 9222

                resp = _requests.get(f"{self._remote_url}/json/version", timeout=10)
                resp.raise_for_status()
                version_info = resp.json()
                ws_url = version_info.get('webSocketDebuggerUrl', '')
                if not ws_url:
                    raise RuntimeError(f"No webSocketDebuggerUrl found at {self._remote_url}/json/version")

                # Replace host/port in ws_url to match the remote URL
                _ws_parsed = _urlparse(ws_url)
                if _ws_parsed.hostname in ('localhost', '127.0.0.1', '0.0.0.0'):
                    ws_url = ws_url.replace(
                        f"{_ws_parsed.hostname}:{_ws_parsed.port or 9222}",
                        f"{remote_host}:{remote_port}"
                    )

                # Use CDP WebSocket directly — no local Chrome/chromedriver needed
                import websocket as _ws
                logger.info(f"Connecting to CDP WebSocket: {ws_url}")
                self._cdp_ws = _ws.create_connection(ws_url, timeout=30)
                self._cdp_ws.settimeout(10)
                self._remote_mode = True
                self._ws_url = ws_url

                # Get the first page target
                targets = _requests.get(f"{self._remote_url}/json", timeout=10).json()
                page_targets = [t for t in targets if t.get('type') == 'page']
                if page_targets:
                    self._page_ws_url = page_targets[0].get('webSocketDebuggerUrl', '')
                    # Fix localhost in page ws url
                    if self._page_ws_url:
                        _p = _urlparse(self._page_ws_url)
                        if _p.hostname in ('localhost', '127.0.0.1', '0.0.0.0'):
                            self._page_ws_url = self._page_ws_url.replace(
                                f"{_p.hostname}:{_p.port or 9222}",
                                f"{remote_host}:{remote_port}"
                            )
                    logger.info(f"Found {len(page_targets)} page target(s)")

                self._set_viewport_remote(remote_host, remote_port)
                logger.info(f"Connected to remote browser at {self._remote_url}")
                return
            except Exception as e:
                logger.error(f"Failed to connect to remote browser: {e}")
                raise

        # Local browser mode: launch Chrome on this machine
        # Try reconnecting first
        if self._try_reconnect():
            logger.info("Reconnected to existing browser session")
            return

        logger.info("Starting browser for cloud HSR")
        options = self._build_chrome_options()

        service = ChromeService(log_path=os.devnull)
        if self._driver_path:
            service = ChromeService(executable_path=self._driver_path, log_path=os.devnull)
        if self._browser_path:
            options.binary_location = self._browser_path

        try:
            self.driver = webdriver.Chrome(service=service, options=options)
        except SessionNotCreatedException as e:
            logger.error(f"Browser session creation failed: {e}")
            raise

        self._set_viewport()
        self._inject_pointer_lock_block()
        logger.info("Browser started successfully")

    def browser_stop(self):
        """Quit the browser. In remote mode, only disconnect (don't kill the remote browser)."""
        if self._remote_mode:
            logger.info("Disconnecting from remote browser (not quitting)")
            try:
                if self._page_ws:
                    self._page_ws.close()
            except Exception:
                pass
            try:
                if self._cdp_ws:
                    self._cdp_ws.close()
            except Exception:
                pass
            self._page_ws = None
            self._cdp_ws = None
            self._remote_mode = False
            return
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def _try_reconnect(self) -> bool:
        """Try to connect to an existing browser session by debug port."""
        # Not implementing full reconnect for now — start fresh each time.
        return False

    # ------------------------------------------------------------------
    # CDP helpers
    # ------------------------------------------------------------------

    def _cdp_send(self, ws, method: str, params: dict = None, msg_id: int = 1):
        """Send a CDP command over WebSocket and return the result."""
        import json as _json
        msg = {"id": msg_id, "method": method, "params": params or {}}
        ws.send(_json.dumps(msg))
        # Read responses until we get the matching id
        for _ in range(100):
            resp = _json.loads(ws.recv())
            if resp.get("id") == msg_id:
                return resp.get("result", {})
        return {}

    def _cdp(self, cmd: str, params: dict = None):
        """Execute a Chrome DevTools Protocol command.
        In remote mode, uses WebSocket; otherwise uses Selenium driver."""
        if self._remote_mode and self._page_ws:
            return self._cdp_send(self._page_ws, cmd, params)
        if self.driver is None:
            return {}
        return self.driver.execute_cdp_cmd(cmd, params or {})

    def _set_viewport_remote(self, host, port):
        """Set viewport via direct CDP WebSocket in remote mode."""
        import json as _json
        import websocket as _ws

        # Connect to the first page target for Page/Input commands
        if self._page_ws_url:
            self._page_ws = _ws.create_connection(self._page_ws_url, timeout=30)
            self._page_ws.settimeout(10)
            # Set viewport via page-level CDP
            self._cdp_send(self._page_ws, "Emulation.setDeviceMetricsOverride", {
                "width": 1920,
                "height": 1080,
                "deviceScaleFactor": 1,
                "mobile": False,
            })
            # Navigate to cloud game if not already there
            self._cdp_send(self._page_ws, "Page.enable", {})
            self._cdp_send(self._page_ws, "Page.navigate", {"url": "https://sr.mihoyo.com/cloud"})
            logger.info("Remote viewport set to 1920x1080, navigated to cloud game")
        else:
            logger.warning("No page target found for remote viewport setup")

    def _set_viewport(self):
        """Force viewport to 1920x1080 @ 1x scale (matching SRC's 1280x720
        assets after downscaling in screenshot())."""
        if self._remote_mode and self._page_ws:
            self._cdp_send(self._page_ws, "Emulation.setDeviceMetricsOverride", {
                "width": 1920,
                "height": 1080,
                "deviceScaleFactor": 1,
                "mobile": False,
            })
            return
        self._cdp("Emulation.setDeviceMetricsOverride", {
            "width": 1920,
            "height": 1080,
            "deviceScaleFactor": 1,
            "mobile": False,
        })

    def _inject_pointer_lock_block(self):
        """In headless mode, block pointer lock requests so the browser
        doesn't steal the cursor."""
        if not self._headless:
            return
        script = """
        (() => {
            const blocked = function () {
                return Promise.reject(new DOMException(
                    'Pointer Lock is disabled in background mode.',
                    'NotAllowedError'
                ));
            };
            Object.defineProperty(Element.prototype, 'requestPointerLock', {
                configurable: true, writable: true, value: blocked,
            });
            if (document.pointerLockElement && document.exitPointerLock) {
                document.exitPointerLock();
            }
        })();
        """
        try:
            self._cdp("Page.addScriptToEvaluateOnNewDocument", {
                "source": script, "runImmediately": True,
            })
        except Exception as e:
            logger.warning(f"Failed to inject pointer-lock block: {e}")

    # ------------------------------------------------------------------
    # Screenshot — returns 1280x720 BGR ndarray
    # ------------------------------------------------------------------

    def screenshot_browser(self) -> np.ndarray:
        """Take a screenshot via CDP, downscale to 1280x720, return BGR ndarray."""
        for attempt in range(3):
            try:
                result = self._cdp("Page.captureScreenshot", {"format": "jpeg", "quality": 90})
                data_b64 = result.get("data")
                if not data_b64:
                    continue
                img_bytes = base64.b64decode(data_b64)
                img = Image.open(io.BytesIO(img_bytes))
                img = img.convert("RGB")
                arr = np.array(img)
                # PIL gives RGB, SRC expects BGR everywhere
                arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
                # Downscale 1920x1080 → 1280x720 to match SRC's asset resolution
                arr = cv2.resize(arr, (1280, 720), interpolation=cv2.INTER_AREA)
                return arr
            except Exception as e:
                logger.warning(f"Screenshot attempt {attempt + 1} failed: {e}")
                time.sleep(0.2)

        # Fallback: Selenium native screenshot (not available in remote mode)
        if not self._remote_mode:
            try:
                png = self.driver.get_screenshot_as_png()
                img = Image.open(io.BytesIO(png)).convert("RGB")
                arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
                arr = cv2.resize(arr, (1280, 720), interpolation=cv2.INTER_AREA)
                return arr
            except Exception as e:
                logger.error(f"All screenshot methods failed: {e}")
                raise
        logger.error(f"All screenshot attempts failed in remote mode")
        raise RuntimeError("Failed to take screenshot via CDP WebSocket")

    def screenshot_video_element(self) -> np.ndarray | None:
        """Attempt to grab a frame directly from the <video> element via JS.
        This is faster but may fail depending on codec and CORS."""
        try:
            result = self.driver.execute_async_script("""
            const callback = arguments[arguments.length - 1];
            try {
                const video = document.querySelector('.game-player__video');
                if (!video) { callback(null); return; }
                const canvas = document.createElement('canvas');
                canvas.width = video.videoWidth || 1920;
                canvas.height = video.videoHeight || 1080;
                const ctx = canvas.getContext('2d');
                ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
                callback(canvas.toDataURL('image/jpeg', 0.9));
            } catch(e) { callback(null); }
            """)
            if result is None:
                return None
            # result is a data URL: "data:image/jpeg;base64,..."
            _, encoded = result.split(",", 1)
            img_bytes = base64.b64decode(encoded)
            arr = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
            if arr is not None:
                arr = cv2.resize(arr, (1280, 720), interpolation=cv2.INTER_AREA)
            return arr
        except Exception as e:
            logger.debug(f"Video element screenshot failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Input — click, swipe, drag, key
    # ------------------------------------------------------------------

    # CDP mouse button mapping
    _MOUSE_BUTTONS = {"left": "left", "middle": "middle", "right": "right"}
    # CDP key event types
    _KEY_DOWN = "keyDown"
    _KEY_UP = "keyUp"

    def click_browser(self, x: int, y: int):
        """Click at (x, y) in 1280x720 coordinate space (mapped to 1920x1080)."""
        sx, sy = self._scale_coord(x, y)
        self._cdp("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": sx, "y": sy,
            "button": "left",
            "clickCount": 1,
        })
        time.sleep(0.02)
        self._cdp("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": sx, "y": sy,
            "button": "left",
            "clickCount": 1,
        })

    def long_click_browser(self, x: int, y: int, duration: float = 1.0):
        """Long click at (x, y)."""
        sx, sy = self._scale_coord(x, y)
        self._cdp("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": sx, "y": sy,
            "button": "left",
            "clickCount": 1,
        })
        time.sleep(duration)
        self._cdp("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": sx, "y": sy,
            "button": "left",
            "clickCount": 1,
        })

    def swipe_browser(self, p1, p2, duration: float = 0.3):
        """Swipe from p1 to p2 with intermediate steps."""
        x1, y1 = self._scale_coord(*p1)
        x2, y2 = self._scale_coord(*p2)
        steps = max(int(duration / 0.016), 10)
        step_x = (x2 - x1) / steps
        step_y = (y2 - y1) / steps

        self._cdp("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x1, "y": y1,
            "button": "left", "clickCount": 1,
        })
        time_per_step = duration / steps
        for i in range(1, steps + 1):
            time.sleep(time_per_step)
            self._cdp("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": int(x1 + step_x * i),
                "y": int(y1 + step_y * i),
            })
        self._cdp("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x2, "y": y2,
            "button": "left", "clickCount": 1,
        })

    def drag_browser(self, p1, p2, duration: float = 1.0):
        """Drag — same as swipe but longer duration."""
        self.swipe_browser(p1, p2, duration=duration)

    def press_key_browser(self, key: str):
        """Press a key. `key` should be a DOM key value (e.g. 'Escape', 'Enter')."""
        # Map common names
        key_map = {
            "escape": "Escape", "esc": "Escape",
            "enter": "Enter", "return": "Enter",
            "space": " ",
            "backspace": "Backspace",
            "tab": "Tab",
        }
        key = key_map.get(key.lower(), key)
        self._cdp("Input.dispatchKeyEvent", {
            "type": self._KEY_DOWN, "key": key,
        })
        time.sleep(0.05)
        self._cdp("Input.dispatchKeyEvent", {
            "type": self._KEY_UP, "key": key,
        })

    def _scale_coord(self, x, y):
        """Scale from SRC's 1280x720 space to browser's 1920x1080 space."""
        return int(x * 1920 / 1280), int(y * 1080 / 720)

    # ------------------------------------------------------------------
    # App lifecycle (browser = "the app")
    # ------------------------------------------------------------------

    def app_start_browser(self):
        """Navigate to the cloud game URL (or start browser if needed)."""
        if self.driver is None:
            self.browser_start()
            return
        # Already running — just navigate
        try:
            self.driver.get(self.GAME_URL)
            self._wait_page_loaded()
            self._set_viewport()
        except Exception as e:
            logger.warning(f"Navigation failed, restarting browser: {e}")
            self.browser_stop()
            self.browser_start()

    def app_stop_browser(self):
        """Close the browser."""
        self.browser_stop()

    def app_is_running_browser(self) -> bool:
        """Check if the browser is alive and on the game page."""
        if self.driver is None:
            return False
        try:
            _ = self.driver.current_url
            return True
        except Exception:
            return False

    def _wait_page_loaded(self, timeout=30):
        """Wait until the cloud game page's background image has loaded."""
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.execute_script("""
                    const img = document.querySelector(
                        '#app > div.home-wrapper > picture > img'
                    );
                    return img && img.complete && img.naturalWidth > 0;
                """)
            )
        except TimeoutException:
            logger.warning("Timed out waiting for page to load")

    # ------------------------------------------------------------------
    # DOM hierarchy (replaces uiautomator2 dump_hierarchy for cloud_android)
    # ------------------------------------------------------------------

    def dump_hierarchy_browser(self):
        """Parse the browser DOM into an lxml element tree that can be
        queried with XPath, mirroring the structure SRC expects from
        uiautomator2 dump_hierarchy.

        We serialize relevant elements (interactive + visible) with
        text/content-desc attributes so that existing XPath selectors
        from cloud.py work unchanged.
        """
        if self.driver is None:
            return None

        # Use JS to extract a simplified DOM tree
        js = """
        (function walk(el) {
            if (el.nodeType !== 1) return null;
            // Skip invisible elements
            const style = getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0')
                return null;

            const node = {
                tag: el.tagName.toLowerCase(),
                attrs: {}
            };
            // Collect useful attributes
            const keep = ['id', 'class', 'text', 'resource-id', 'content-desc',
                          'data-index', 'type', 'name', 'value', 'placeholder',
                          'href', 'src', 'alt', 'title', 'role', 'aria-label'];
            if (el.id) node.attrs['resource-id'] = el.id;
            if (el.className && typeof el.className === 'string')
                node.attrs['class'] = el.className;
            // Text content (direct text only, not from children)
            const text = (el.childNodes.length === 1 && el.childNodes[0].nodeType === 3)
                ? el.textContent.trim()
                : (el.innerText || '').split('\\n')[0].trim().substring(0, 80);
            if (text) node.attrs['text'] = text;
            if (el.getAttribute('aria-label')) node.attrs['content-desc'] = el.getAttribute('aria-label');
            if (el.getAttribute('role')) node.attrs['role'] = el.getAttribute('role');
            if (el.getAttribute('data-index')) node.attrs['data-index'] = el.getAttribute('data-index');

            // Bounds
            const rect = el.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                node.attrs['bounds'] = `[${Math.round(rect.left)},${Math.round(rect.top)}][${Math.round(rect.right)},${Math.round(rect.bottom)}]`;
            }

            const children = [];
            for (const child of el.children) {
                const c = walk(child);
                if (c) children.push(c);
            }
            if (children.length > 0) node.children = children;
            return node;
        })(document.body);
        """
        try:
            tree_dict = self.driver.execute_script(js)
        except Exception as e:
            logger.error(f"DOM dump failed: {e}")
            return None

        if tree_dict is None:
            return None

        return self._dict_to_etree(tree_dict)

    @staticmethod
    def _dict_to_etree(d, parent=None):
        """Convert the JS-serialized DOM dict into an lxml etree."""
        from lxml import etree

        if parent is None:
            root = etree.Element("hierarchy")
            BrowserDevice._dict_to_etree(d, root)
            return root

        el = etree.SubElement(parent, d.get("tag", "node"))
        for k, v in d.get("attrs", {}).items():
            el.set(k, str(v))
        for child in d.get("children", []):
            BrowserDevice._dict_to_etree(child, el)
        return parent

    # ------------------------------------------------------------------
    # Cloud-game-specific helpers
    # ------------------------------------------------------------------

    def is_in_cloud_game(self) -> bool:
        """Check whether the cloud game video element is present (i.e. we're
        inside the game, not on the login/queue page)."""
        if self.driver is None:
            return False
        try:
            return len(self.driver.find_elements(By.CSS_SELECTOR, ".game-player")) > 0
        except Exception:
            return False

    def set_auto_battle(self, enable: bool):
        """Toggle auto-battle via localStorage manipulation, similar to
        March7thAssistant's change_auto_battle."""
        if self.driver is None:
            return
        try:
            ls_raw = self.driver.execute_script("return JSON.stringify(localStorage)")
            ls = json.loads(ls_raw)
            cloud = json.loads(ls.get("cg_hkrpg_cn_cloudData", "{}"))
            cloud.setdefault("value", {})
            save = json.loads(cloud["value"].get("RPGCloudSave", "{}") or "{}")
            int_dicts = save.get("IntDicts", {})
            int_dicts["OtherSettings_AutoBattleOpen"] = int(enable)
            int_dicts["OtherSettings_IsSaveBattleSpeed"] = int(enable)
            uid = int_dicts.get("App_LastUserID")
            if uid:
                int_dicts[f"User_{uid}_SpeedUpOpen"] = int(enable)
            save["IntDicts"] = int_dicts
            cloud["value"]["RPGCloudSave"] = json.dumps(save)
            ls["cg_hkrpg_cn_cloudData"] = json.dumps(cloud)
            for k, v in ls.items():
                self.driver.execute_script(
                    f"localStorage.setItem('{k}', arguments[0]);", v
                )
            logger.info(f"Auto-battle {'enabled' if enable else 'disabled'} via localStorage")
        except Exception as e:
            logger.warning(f"Failed to set auto-battle: {e}")