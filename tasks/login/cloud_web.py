"""
Web-based cloud game login flow for StarRailCopilot.

Handles browser-based login, queue waiting, and game entry for the
cloud HSR web version (https://sr.mihoyo.com/cloud).

This mirrors the structure of tasks/login/cloud.py (LoginAndroidCloud)
but operates on a browser DOM instead of Android UI hierarchy.

All DOM interactions use BrowserDevice._execute_js() which works in both
local Selenium mode and remote CDP WebSocket mode (no driver dependency).
"""

import json
import re
import time

from module.base.base import ModuleBase
from module.base.timer import Timer
from module.device.method.browser import BrowserDevice
from module.exception import GameNotRunningError, RequestHumanTakeover
from module.logger import logger


class WebXPath:
    """
    XPath / CSS selectors for cloud game web page elements.

    The cloud game web page uses class-based selectors (not resource-id),
    so we use CSS selectors + text matching instead of Android XPath.
    """

    # --- Login page ---
    ENTER_GAME = '.btn-start'
    QR_LOGIN_TAB = '//div[contains(@class,"login")]//div[contains(text(),"扫码")]'
    QR_CODE_IMG = 'img.qr-loaded'
    QR_SUCCESS = "//*[contains(text(), '扫码成功')]"
    QR_EXPIRED = 'div.qr-expired'

    # --- Cloud game main page (before entering game) ---
    START_GAME_BTN = '//button[contains(text(),"开始游戏")] | //div[contains(@class,"btn") and contains(text(),"开始游戏")]'

    # --- Queue ---
    QUEUE_TEXT = '//div[contains(text(),"预计等待")]'
    QUEUE_REMAIN = '//div[contains(@class,"queue")]//span[contains(text(),"分钟")]'

    # --- Popup dialogs ---
    POPUP_CONFIRM = '//div[contains(@class,"dialog")]//button[contains(text(),"确认")]'
    BILLING_CONFIRM = '//button[contains(text(),"进入游戏")] | //span[contains(text(),"进入游戏")]'
    DISCONNECT_CONFIRM = '//button[contains(text(),"退出游戏")] | //span[contains(text(),"退出游戏")]'

    # --- In-game floating UI ---
    FLOAT_WINDOW = '.game-player__float, .float-window, [class*="float"]'
    FLOAT_EXIT = '//div[contains(@class,"exit")] | //div[contains(text(),"退出")]'

    # --- Network state ---
    NET_STATE = '[class*="net-state"], [class*="ping"]'


class LoginWebCloud(ModuleBase):
    """
    Handles login + queue + game entry for the cloud HSR web version.

    All DOM interactions use self.browser._execute_js() so they work in both:
    - Local mode (Selenium driver)
    - Remote mode (CDP WebSocket, no local browser needed)
    """

    LOGIN_TIMEOUT = 300  # 5 minutes

    def __init__(self, config, device: BrowserDevice):
        super().__init__(config, device)
        self.browser = device

    # ------------------------------------------------------------------
    # JS helpers — all DOM interaction goes through _execute_js
    # ------------------------------------------------------------------

    def _js_click(self, js_selector: str) -> bool:
        """Find element via JS and click it. Returns True if clicked."""
        script = f"""
            const el = {js_selector};
            if (el) {{ el.click(); return true; }}
            return false;
        """
        return bool(self.browser._execute_js(script))

    def _js_click_all(self, js_selector: str) -> int:
        """Click all matching elements. Returns count clicked."""
        script = f"""
            const els = {js_selector};
            let clicked = 0;
            for (const el of els) {{
                if (el.offsetParent !== null) {{ el.click(); clicked++; }}
            }}
            return clicked;
        """
        return int(self.browser._execute_js(script) or 0)

    def _js_text(self, js_selector: str) -> str:
        """Get text content of first matching element."""
        script = f"""
            const el = {js_selector};
            return el ? (el.innerText || el.textContent || '') : '';
        """
        return str(self.browser._execute_js(script) or '')

    def _js_exists_visible(self, js_selector: str) -> bool:
        """Check if a visible element matching the selector exists."""
        script = f"""
            const el = {js_selector};
            return el ? (el.offsetParent !== null || el.offsetWidth > 0) : false;
        """
        return bool(self.browser._execute_js(script))

    # ------------------------------------------------------------------
    # Login detection
    # ------------------------------------------------------------------

    def _is_logged_in(self) -> bool:
        """Check if user is already logged in (game page is visible)."""
        try:
            result = self.browser._execute_js("""
                const startBtn = document.querySelector('.btn-start, [class*="start-game"]');
                const gamePlayer = document.querySelector('.game-player');
                const loginForm = document.querySelector('[class*="login-form"], [class*="account"]');
                return {
                    hasStartBtn: !!startBtn,
                    inGame: !!gamePlayer,
                    noLoginForm: !loginForm
                };
            """)
            return bool(result and (result.get('hasStartBtn') or result.get('inGame') or result.get('noLoginForm')))
        except Exception:
            return False

    def _wait_for_login(self, timeout=None):
        """Wait for user to complete login (manual or QR scan)."""
        if timeout is None:
            timeout = self.LOGIN_TIMEOUT

        logger.info('Waiting for user login...')
        deadline = time.time() + timeout

        while time.time() < deadline:
            if self._is_logged_in():
                logger.info('Login detected!')
                return True
            time.sleep(2)

        raise GameNotRunningError(f'Login timeout after {timeout}s')

    # ------------------------------------------------------------------
    # Game entry
    # ------------------------------------------------------------------

    def _click_start_game(self) -> bool:
        """Click the 'Start Game' button on the cloud game home page."""
        logger.info('Clicking start game button')
        # Try text-based selectors via JS
        click_scripts = [
            """document.querySelector('button.start-btn, .btn-start, [class*="start-game"], [class*="play-btn"]')""",
            """[...document.querySelectorAll('button, div.btn, span, a')].find(el => el.textContent.includes('开始游戏'))""",
            """document.querySelector('#app button[class*="start"]')""",
        ]
        for script in click_scripts:
            if self._js_click(script):
                logger.info('Clicked start game button')
                return True

        logger.warning('Could not find start game button')
        return False

    def _handle_popups(self) -> bool:
        """Dismiss common popups: billing confirm, network notice, etc."""
        confirm_texts = ['进入游戏', '确认使用', '确认', '使用流量进行游戏']
        for text in confirm_texts:
            script = f"""
                const els = [...document.querySelectorAll('button, span, div, a')]
                    .filter(el => el.textContent.includes('{text}') && el.offsetParent !== null);
                if (els.length > 0) {{ els[0].click(); return true; }}
                return false;
            """
            if self._js_click(script):
                logger.info(f'Dismissed popup: {text}')
                time.sleep(0.5)
                return True
        return False

    def _wait_in_queue(self, max_wait=1800) -> bool:
        """Wait while in queue to enter the game."""
        logger.info('Waiting in queue...')
        start = time.time()
        while time.time() - start < max_wait:
            if self.browser.is_in_cloud_game():
                logger.info('Queue passed, in game!')
                return True

            # Check for queue info text
            queue_text = self._js_text(
                """document.querySelector('div[class*="queue"], [class*="wait"]')"""
            )
            if queue_text:
                logger.info(f'Queue: {queue_text}')

            self._handle_popups()
            time.sleep(3)

        logger.error(f'Queue timeout after {max_wait}s')
        return False

    def _get_remaining_time(self) -> tuple:
        """Get remaining playtime (paid_minutes, free_minutes)."""
        paid, free = 0, 0
        try:
            page_text = self.browser._execute_js("return document.body.innerText;")
            if page_text:
                m = re.search(r'星云币[时長]*[：：]\s*(\d+)\s*分[钟鐘]', page_text)
                if m:
                    paid = int(m.group(1))
                m = re.search(r'免费[时長]*[：：]\s*(\d+)\s*分[钟鐘]', page_text)
                if m:
                    free = int(m.group(1))
        except Exception as e:
            logger.debug(f"Failed to parse remaining time: {e}")
        return paid, free

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def cloud_web_enter_game(self):
        """
        Complete flow: ensure browser is running → login → enter game.
        Returns True if successfully in game.
        """
        logger.hr('Cloud web enter game', level=1)

        if self.browser.driver is None and not self.browser._remote_mode:
            self.browser.browser_start()

        # Ensure we're on the right page
        if not self.browser.app_is_running_browser():
            self.browser.browser_start()

        # Wait for page load
        self.browser._wait_page_loaded()

        # Login
        if not self._is_logged_in():
            logger.info('Not logged in, waiting for login...')
            self._wait_for_login()

        # Check remaining time
        paid, free = self._get_remaining_time()
        logger.info(f'Cloud remain: {paid} min paid, {free} min free')
        remaining = paid + free
        if remaining == 0:
            logger.error('No remaining cloud game time!')
            raise RequestHumanTakeover('Cloud game time exhausted')

        # Click start game
        if not self._click_start_game():
            if self.browser.is_in_cloud_game():
                logger.info('Already in game')
                return True
            logger.error('Cannot find start game button and not in game')
            raise GameNotRunningError('Cannot start cloud game')

        # Handle popups and wait for queue
        self._handle_popups()

        if not self._wait_in_queue():
            raise GameNotRunningError('Queue timeout')

        # Set viewport again after entering game
        self.browser._set_viewport()

        logger.info('Cloud web game entered successfully')
        return True

    def cloud_web_exit_game(self):
        """Exit the cloud game (click exit button, back to home page)."""
        logger.hr('Cloud web exit game')
        try:
            exit_selectors = [
                """document.querySelector('div[class*="exit"]')""",
                """[...document.querySelectorAll('div, button, span')].find(el => el.textContent.includes('退出游戏'))""",
                """document.querySelector('button[class*="exit"]')""",
            ]
            for script in exit_selectors:
                if self._js_click(script):
                    logger.info('Clicked exit button')
                    time.sleep(2)
                    self._handle_popups()
                    return
            logger.warning('Could not find exit button')
        except Exception as e:
            logger.error(f'Error exiting cloud game: {e}')

    def cloud_web_keep_alive(self):
        """Periodically interact to prevent idle kick."""
        logger.hr('Cloud web keep alive', level=2)
        while True:
            self.device.sleep((45, 60))
            logger.info('Cloud web keep alive')
            try:
                # Click the floating window if visible, then dismiss
                self._js_click(
                    """document.querySelector('.game-player__float, .float-window, [class*="float"]')"""
                )
                time.sleep(1)
                self.browser.click_browser(640, 360)
            except Exception as e:
                logger.debug(f'Keep alive interaction failed: {e}')