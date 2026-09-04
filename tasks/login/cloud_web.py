"""
Web-based cloud game login flow for StarRailCopilot.

Handles browser-based login, queue waiting, and game entry for the
cloud HSR web version (https://sr.mihoyo.com/cloud).

This mirrors the structure of tasks/login/cloud.py (LoginAndroidCloud)
but operates on a browser DOM instead of Android UI hierarchy.
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
    # The login button on the landing page
    ENTER_GAME = '.btn-start'  # CSS, may vary
    # "扫码登录" tab
    QR_LOGIN_TAB = '//div[contains(@class,"login")]//div[contains(text(),"扫码")]'
    # QR code image
    QR_CODE_IMG = 'img.qr-loaded'
    # "扫码成功" confirmation text
    QR_SUCCESS = "//*[contains(text(), '扫码成功')]"
    # QR expired overlay
    QR_EXPIRED = 'div.qr-expired'

    # --- Cloud game main page (before entering game) ---
    # "开始游戏" button
    START_GAME_BTN = '//button[contains(text(),"开始游戏")] | //div[contains(@class,"btn") and contains(text(),"开始游戏")] | //*[@id="app"]//div[contains(@class,"start")]'

    # --- Queue ---
    # Queue info text (e.g. "预计等待时间")
    QUEUE_TEXT = '//div[contains(text(),"预计等待")]'
    QUEUE_REMAIN = '//div[contains(@class,"queue")]//span[contains(text(),"分钟")]'

    # --- Popup dialogs ---
    # Generic confirm button in popups
    POPUP_CONFIRM = '//div[contains(@class,"dialog")]//button[contains(text(),"确认")] | //div[contains(@class,"dialog")]//span[contains(text(),"确认")] | //button[contains(text(),"进入游戏")] | //span[contains(text(),"进入游戏")]'

    # Billing notice: "本次游戏将使用畅玩卡无限畅玩" / "确认使用星云币"
    BILLING_CONFIRM = '//button[contains(text(),"进入游戏")] | //span[contains(text(),"进入游戏")]'

    # "连接中断" — network disconnect
    DISCONNECT_CONFIRM = '//button[contains(text(),"退出游戏")] | //span[contains(text(),"退出游戏")]'

    # Free time / paid time text
    REMAIN_FREE = '//span[contains(text(),"免费时长")] | //div[contains(text(),"免费时长")]'
    REMAIN_PAID = '//span[contains(text(),"星云币")] | //div[contains(text(),"星云币")]'

    # --- In-game floating UI ---
    FLOAT_WINDOW = '.game-player__float, .float-window, [class*="float"]'
    FLOAT_EXIT = '//div[contains(@class,"exit")] | //div[contains(text(),"退出")]'

    # --- Network state ---
    NET_STATE = '[class*="net-state"], [class*="ping"]'


class LoginWebCloud(ModuleBase):
    """
    Handles login + queue + game entry for the cloud HSR web version.

    Usage (from SRC's login flow):
        device.browser_start()           # start browser
        login = LoginWebCloud(config, device)
        login.cloud_web_enter_game()     # login + queue + enter
    """

    # How long to wait for user login (seconds)
    LOGIN_TIMEOUT = 300  # 5 minutes

    def __init__(self, config, device: BrowserDevice):
        super().__init__(config, device)
        self.browser = device

    # ------------------------------------------------------------------
    # Login detection
    # ------------------------------------------------------------------

    def _is_logged_in(self) -> bool:
        """Check if user is already logged in (game page is visible)."""
        try:
            # If we can see the start game button or are already in game,
            # we're logged in
            result = self.browser.driver.execute_script("""
                // Check if we're on the game page (start button visible)
                const startBtn = document.querySelector('.btn-start, [class*="start-game"]');
                // Check if game is running
                const gamePlayer = document.querySelector('.game-player');
                // Check if there's no login form visible
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
        """Wait for user to complete login (manual or QR scan).

        In headless mode, we attempt QR-code login.
        In headed mode, we just wait for the user to log in manually.
        """
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

    def _click_start_game(self):
        """Click the 'Start Game' button on the cloud game home page."""
        logger.info('Clicking start game button')
        # Try multiple selectors
        selectors = [
            "//button[contains(text(),'开始游戏')]",
            "//div[contains(@class,'btn') and contains(text(),'开始游戏')]",
            "//span[contains(text(),'开始游戏')]",
            "//a[contains(text(),'开始游戏')]",
        ]
        for sel in selectors:
            try:
                els = self.browser.driver.find_elements(By.XPATH, sel)
                if els and els[0].is_displayed():
                    els[0].click()
                    return True
            except Exception:
                continue

        # Fallback: try CSS selector
        try:
            els = self.browser.driver.find_elements(By.CSS_SELECTOR,
                '.btn-start, [class*="start-game"], [class*="play-btn"]')
            for el in els:
                if el.is_displayed():
                    el.click()
                    return True
        except Exception:
            pass

        logger.warning('Could not find start game button')
        return False

    def _handle_popups(self):
        """Dismiss common popups: billing confirm, network notice, etc."""
        try:
            # Billing confirm — click "进入游戏" or "确认"
            confirm_texts = ['进入游戏', '确认使用', '确认', '使用流量进行游戏']
            for text in confirm_texts:
                els = self.browser.driver.find_elements(By.XPATH,
                    f"//*[contains(text(),'{text}')]")
                for el in els:
                    if el.is_displayed():
                        el.click()
                        logger.info(f'Dismissed popup: {text}')
                        time.sleep(0.5)
                        return True
        except Exception:
            pass
        return False

    def _wait_in_queue(self, max_wait=1800):
        """Wait while in queue to enter the game."""
        logger.info('Waiting in queue...')
        start = time.time()
        while time.time() - start < max_wait:
            # Check if we've entered the game
            if self.browser.is_in_cloud_game():
                logger.info('Queue passed, in game!')
                return True

            # Check for queue info
            try:
                remain_els = self.browser.driver.find_elements(By.XPATH,
                    WebXPath.QUEUE_TEXT)
                if remain_els:
                    logger.info(f'Queue: {remain_els[0].text}')
            except Exception:
                pass

            # Dismiss popups while waiting
            self._handle_popups()
            time.sleep(3)

        logger.error(f'Queue timeout after {max_wait}s')
        return False

    def _get_remaining_time(self) -> tuple[int, int]:
        """Get remaining playtime (paid_minutes, free_minutes)."""
        paid, free = 0, 0
        try:
            page_text = self.browser.driver.execute_script(
                "return document.body.innerText;"
            )
            # Parse "星云币时长：X分钟"
            m = re.search(r'星云币[时長]*[：:]\s*(\d+)\s*分[钟鐘]', page_text)
            if m:
                paid = int(m.group(1))
            # Parse "免费时长：X分钟"
            m = re.search(r'免费[时長]*[：:]\s*(\d+)\s*分[钟鐘]', page_text)
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

        if self.browser.driver is None:
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
            # Maybe we're already in game
            if self.browser.is_in_cloud_game():
                logger.info('Already in game')
                return True
            logger.error('Cannot find start game button and not in game')
            raise GameNotRunningError('Cannot start cloud game')

        # Handle popups and wait for queue
        self._handle_popups()

        # Wait for game to start (may be in queue)
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
            # Try to find and click exit button
            exit_selectors = [
                '//div[contains(@class,"exit")]',
                '//div[contains(text(),"退出游戏")]',
                '//button[contains(text(),"退出游戏")]',
            ]
            for sel in exit_selectors:
                try:
                    els = self.browser.driver.find_elements(By.XPATH, sel)
                    for el in els:
                        if el.is_displayed():
                            el.click()
                            logger.info('Clicked exit button')
                            time.sleep(2)
                            # Confirm exit
                            self._handle_popups()
                            return
                except Exception:
                    continue
            logger.warning('Could not find exit button')
        except Exception as e:
            logger.error(f'Error exiting cloud game: {e}')

    def cloud_web_keep_alive(self):
        """Periodically interact to prevent idle kick, similar to
        LoginAndroidCloud.cloud_keep_alive()."""
        logger.hr('Cloud web keep alive', level=2)
        while True:
            self.device.sleep((45, 60))
            logger.info('Cloud web keep alive')
            # Open and close the settings panel to prevent idle timeout
            try:
                float_els = self.browser.driver.find_elements(
                    By.CSS_SELECTOR, WebXPath.FLOAT_WINDOW)
                if float_els and float_els[0].is_displayed():
                    float_els[0].click()
                    time.sleep(1)
                # Click somewhere to dismiss
                self.browser.click_browser(640, 360)
            except Exception as e:
                logger.debug(f'Keep alive interaction failed: {e}')


# Need By for XPath/CSS selectors
from selenium.webdriver.common.by import By