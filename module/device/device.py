import collections
import itertools

from lxml import etree

from module.device.env import IS_WINDOWS
# Patch pkg_resources before importing adbutils and uiautomator2
from module.device.pkg_resources import get_distribution

# Just avoid being removed by import optimization
_ = get_distribution

from module.base.timer import Timer
from module.device.app_control import AppControl
from module.device.control import Control
from module.device.screenshot import Screenshot
from module.device.method.browser import BrowserDevice
from module.exception import (
    EmulatorNotRunningError,
    GameNotRunningError,
    GameStuckError,
    GameTooManyClickError,
    RequestHumanTakeover
)
from module.logger import logger


def show_function_call():
    """
    INFO     21:07:31.554 │ Function calls:
                       <string>   L1 <module>
                   spawn.py L116 spawn_main()
                   spawn.py L129 _main()
                 process.py L314 _bootstrap()
                 process.py L108 run()
         process_manager.py L149 run_process()
                    alas.py L285 loop()
                    alas.py  L69 run()
                     src.py  L55 rogue()
                   rogue.py  L36 run()
                   rogue.py  L18 rogue_once()
                   entry.py L335 rogue_world_enter()
                    path.py L193 rogue_path_select()
    """
    import os
    import traceback
    stack = traceback.extract_stack()
    func_list = []
    for row in stack:
        filename, line_number, function_name, _ = row
        filename = os.path.basename(filename)
        # /tasks/character/switch.py:64 character_update()
        func_list.append([filename, str(line_number), function_name])
    max_filename = max([len(row[0]) for row in func_list])
    max_linenum = max([len(row[1]) for row in func_list]) + 1

    def format_(file, line, func):
        file = file.rjust(max_filename, " ")
        line = f'L{line}'.rjust(max_linenum, " ")
        if not func.startswith('<'):
            func = f'{func}()'
        return f'{file} {line} {func}'

    func_list = [f'\n{format_(*row)}' for row in func_list]
    logger.info('Function calls:' + ''.join(func_list))


class Device(Screenshot, Control, AppControl):
    _screen_size_checked = False
    detect_record = set()
    click_record = collections.deque(maxlen=30)
    stuck_timer = Timer(60, count=60).start()

    def __init__(self, *args, **kwargs):
        # Cloud web mode: use BrowserDevice instead of emulator
        self._browser = None  # lazy init

        if len(args) >= 1:
            config = args[0]
        else:
            config = kwargs.get('config')

        if config and getattr(config, 'is_cloud_web_game', False):
            # Cloud web mode — skip all emulator init
            logger.info('Cloud web mode: initializing browser device')
            self.config = config
            self._browser = BrowserDevice(config)
            self._browser.browser_start()
            # Skip the parent __init__ which tries ADB connection
            # Instead, just set up the basics we need
            from module.base.timer import Timer as _Timer
            self._screen_size_checked = False
            self.detect_record = set()
            self.click_record = collections.deque(maxlen=30)
            self.stuck_timer = _Timer(60, count=60).start()
            self.orientation = 0
            self.screenshot_interval_set()
            return

        for trial in range(4):
            try:
                super().__init__(*args, **kwargs)
                break
            except EmulatorNotRunningError:
                if trial >= 3:
                    logger.critical('Failed to start emulator after 3 trial')
                    raise RequestHumanTakeover
                # Try to start emulator
                if self.emulator_instance is not None:
                    self.emulator_start()
                else:
                    logger.critical(
                        f'No emulator with serial "{self.config.Emulator_Serial}" found, '
                        f'please set a correct serial'
                    )
                    raise RequestHumanTakeover

        # Auto-fill emulator info
        if IS_WINDOWS and self.config.EmulatorInfo_Emulator == 'auto':
            _ = self.emulator_instance

        self.screenshot_interval_set()
        self.method_check()

        # Auto-select the fastest screenshot method
        if not self.config.is_template_config and self.config.Emulator_ScreenshotMethod == 'auto':
            self.run_simple_screenshot_benchmark()

        # Early init
        if self.config.is_actual_task:
            if self.config.Emulator_ControlMethod == 'MaaTouch':
                self.early_maatouch_init()
            if self.config.Emulator_ControlMethod == 'minitouch':
                self.early_minitouch_init()

    def run_simple_screenshot_benchmark(self):
        """
        Perform a screenshot method benchmark, test 3 times on each method.
        The fastest one will be set into config.
        """
        logger.info('run_simple_screenshot_benchmark')
        # Check resolution first
        self.resolution_check_uiautomator2()
        # Perform benchmark
        from module.daemon.benchmark import Benchmark
        bench = Benchmark(config=self.config, device=self)
        method = bench.run_simple_screenshot_benchmark()
        # Set
        with self.config.multi_set():
            self.config.Emulator_ScreenshotMethod = method
            # if method == 'nemu_ipc':
            #     self.config.Emulator_ControlMethod = 'nemu_ipc'

    def method_check(self):
        """
        Check combinations of screenshot method and control methods
        """
        # nemu_ipc should be together
        # if self.config.Emulator_ScreenshotMethod == 'nemu_ipc' and self.config.Emulator_ControlMethod != 'nemu_ipc':
        #     logger.warning('When using nemu_ipc, both screenshot and control should use nemu_ipc')
        #     self.config.Emulator_ControlMethod = 'nemu_ipc'
        # if self.config.Emulator_ScreenshotMethod != 'nemu_ipc' and self.config.Emulator_ControlMethod == 'nemu_ipc':
        #     logger.warning('When not using nemu_ipc, both screenshot and control should not use nemu_ipc')
        #     self.config.Emulator_ControlMethod = 'minitouch'
        # Allow Hermit on VMOS only
        if self.config.Emulator_ControlMethod == 'Hermit' and not self.is_vmos:
            logger.warning('ControlMethod Hermit is allowed on VMOS only')
            self.config.Emulator_ControlMethod = 'MaaTouch'
        if self.config.Emulator_ScreenshotMethod == 'ldopengl' \
                and self.config.Emulator_ControlMethod == 'minitouch':
            logger.warning('Use MaaTouch on ldplayer')
            self.config.Emulator_ControlMethod = 'MaaTouch'

        # Fallback to auto if nemu_ipc and ldopengl are selected on non-corresponding emulators
        if self.config.Emulator_ScreenshotMethod == 'nemu_ipc':
            if not (self.is_emulator and self.is_mumu_family):
                logger.warning('ScreenshotMethod nemu_ipc is available on MuMu Player 12 only, fallback to auto')
                self.config.Emulator_ScreenshotMethod = 'auto'
        if self.config.Emulator_ScreenshotMethod == 'ldopengl':
            if not (self.is_emulator and self.is_ldplayer_bluestacks_family):
                logger.warning('ScreenshotMethod ldopengl is available on LD Player only, fallback to auto')
                self.config.Emulator_ScreenshotMethod = 'auto'
        # DroidCast is available on SDK 23 (Android 6.0) to SDK 32 (Android 12)
        if self.config.Emulator_ScreenshotMethod in ['DroidCast', 'DroidCast_raw']:
            if self.sdk_ver < 23 or self.sdk_ver > 32:
                logger.warning(f'ScreenshotMethod {self.config.Emulator_ScreenshotMethod} is available on '
                               f'Android 6.0 to 12 only (current sdk_ver={self.sdk_ver}), fallback to auto')
                self.config.Emulator_ScreenshotMethod = 'auto'
        if not IS_WINDOWS and self.config.Emulator_ScreenshotMethod in ['nemu_ipc', 'ldopengl']:
            logger.warning(f'ScreenshotMethod {self.config.Emulator_ScreenshotMethod} is available on Windows only, '
                           f'fallback to auto')
            self.config.Emulator_ScreenshotMethod = 'auto'

    def screenshot(self):
        """
        Returns:
            np.ndarray:
        """
        self.stuck_record_check()

        # Cloud web mode: use browser screenshot
        if self._browser is not None:
            self.image = self._browser.screenshot_browser()
            return self.image

        try:
            super().screenshot()
        except RequestHumanTakeover:
            if not self.ascreencap_available:
                logger.error('aScreenCap unavailable on current device, fallback to auto')
                self.run_simple_screenshot_benchmark()
                super().screenshot()
            else:
                raise

        return self.image

    def dump_hierarchy(self) -> etree._Element:
        self.stuck_record_check()
        # Cloud web mode: use browser DOM dump
        if self._browser is not None:
            result = self._browser.dump_hierarchy_browser()
            if result is not None:
                self.hierarchy = result
                return self.hierarchy
            # Fallback to empty hierarchy
            from lxml import etree as _etree
            self.hierarchy = _etree.Element("hierarchy")
            return self.hierarchy
        return super().dump_hierarchy()

    def release_during_wait(self):
        # Scrcpy server is still sending video stream,
        # stop it during wait
        if self.config.Emulator_ScreenshotMethod == 'scrcpy':
            self._scrcpy_server_stop()
        if self.config.Emulator_ScreenshotMethod == 'nemu_ipc':
            self.nemu_ipc_release()

    def get_orientation(self):
        """
        Callbacks when orientation changed.
        """
        o = super().get_orientation()

        self.on_orientation_change_maatouch()

        return o

    def stuck_record_add(self, button):
        self.detect_record.add(str(button))

    def stuck_record_clear(self):
        self.detect_record = set()
        self.stuck_timer.reset()

    def stuck_record_check(self):
        """
        Raises:
            GameStuckError:
        """
        reached = self.stuck_timer.reached()
        if not reached:
            return False

        show_function_call()
        logger.warning('Wait too long')
        logger.warning(f'Waiting for {self.detect_record}')
        self.stuck_record_clear()

        if self.app_is_running():
            raise GameStuckError(f'Wait too long')
        else:
            raise GameNotRunningError('Game died')

    def handle_control_check(self, button):
        self.stuck_record_clear()
        self.click_record_add(button)
        self.click_record_check()

    def click_record_add(self, button):
        self.click_record.append(str(button))

    def click_record_clear(self):
        self.click_record.clear()

    def click_record_remove(self, button):
        """
        Remove a button from `click_record`

        Args:
            button (Button):

        Returns:
            int: Number of button removed
        """
        removed = 0
        for _ in range(self.click_record.maxlen):
            try:
                self.click_record.remove(str(button))
                removed += 1
            except ValueError:
                # Value not in queue
                break

        return removed

    def click_record_check(self):
        """
        Raises:
            GameTooManyClickError:
        """
        first15 = itertools.islice(self.click_record, 0, 15)
        count = collections.Counter(first15).most_common(2)
        if count[0][1] >= 12:
            # Allow more clicks in Ruan Mei event
            if 'CHOOSE_OPTION_CONFIRM' in self.click_record and 'BLESSING_CONFIRM' in self.click_record:
                count = collections.Counter(self.click_record).most_common(2)
                if count[0][0] == 'BLESSING_CONFIRM' and count[0][1] < 25:
                    return
            show_function_call()
            logger.warning(f'Too many click for a button: {count[0][0]}')
            logger.warning(f'History click: {[str(prev) for prev in self.click_record]}')
            self.click_record_clear()
            raise GameTooManyClickError(f'Too many click for a button: {count[0][0]}')
        if len(count) >= 2 and count[0][1] >= 6 and count[1][1] >= 6:
            show_function_call()
            logger.warning(f'Too many click between 2 buttons: {count[0][0]}, {count[1][0]}')
            logger.warning(f'History click: {[str(prev) for prev in self.click_record]}')
            self.click_record_clear()
            raise GameTooManyClickError(f'Too many click between 2 buttons: {count[0][0]}, {count[1][0]}')

    def disable_stuck_detection(self):
        """
        Disable stuck detection and its handler. Usually uses in semi auto and debugging.
        """
        logger.info('Disable stuck detection')

        def empty_function(*arg, **kwargs):
            return False

        self.click_record_check = empty_function
        self.stuck_record_check = empty_function

    def app_start(self):
        super().app_start()
        self.stuck_record_clear()
        self.click_record_clear()

    def app_stop(self):
        super().app_stop()
        self.stuck_record_clear()
        self.click_record_clear()

    # ------------------------------------------------------------------
    # Cloud web mode: override control methods to use browser
    # ------------------------------------------------------------------

    def click(self, button, control_check=True):
        """Override click to use browser in cloud_web mode."""
        if self._browser is not None:
            return self._click_browser(button, control_check=control_check)
        return super().click(button, control_check=control_check)

    def long_click(self, button, duration=(1, 1.2)):
        """Override long_click to use browser in cloud_web mode."""
        if self._browser is not None:
            duration = ensure_time(duration)
            return self._long_click_browser(button, duration=duration)
        return super().long_click(button, duration=duration)

    def swipe(self, p1, p2, duration=(0.1, 0.2), name='SWIPE', distance_check=True):
        """Override swipe to use browser in cloud_web mode."""
        if self._browser is not None:
            duration = ensure_time(duration)
            if distance_check:
                if np.linalg.norm(np.subtract(p1, p2)) < 10:
                    logger.info('Swipe distance < 10px, dropped')
                    return
            return self._swipe_browser(p1, p2, duration=duration)
        return super().swipe(p1, p2, duration=duration, name=name, distance_check=distance_check)

    def drag(self, p1, p2, segments=1, shake=(0, 15), point_random=(-10, -10, 10, 10),
             shake_random=(-5, -5, 5, 5), swipe_duration=0.25, shake_duration=0.1, name='DRAG'):
        """Override drag to use browser in cloud_web mode."""
        if self._browser is not None:
            return self._drag_browser(p1, p2, point_random=point_random)
        return super().drag(p1, p2, segments=segments, shake=shake, point_random=point_random,
                           shake_random=shake_random, swipe_duration=swipe_duration,
                           shake_duration=shake_duration, name=name)

    def app_start(self):
        if self._browser is not None:
            self._browser.app_start_browser()
            self.stuck_record_clear()
            self.click_record_clear()
            return
        super().app_start()
        self.stuck_record_clear()
        self.click_record_clear()

    def app_stop(self):
        if self._browser is not None:
            self._browser.app_stop_browser()
            self.stuck_record_clear()
            self.click_record_clear()
            return
        super().app_stop()
        self.stuck_record_clear()
        self.click_record_clear()

    def app_is_running(self) -> bool:
        if self._browser is not None:
            return self._browser.app_is_running_browser()
        return super().app_is_running()

    def _click_browser(self, button, control_check=True):
        """Click using browser CDP instead of emulator."""
        if control_check:
            self.handle_control_check(button)
        x, y = ensure_int(*random_rectangle_point(button.button))
        logger.info(f'Click {button} @ ({x}, {y}) [browser]')
        self._browser.click_browser(x, y)

    def _swipe_browser(self, p1, p2, duration=0.3):
        """Swipe using browser CDP instead of emulator."""
        p1, p2 = ensure_int(p1, p2)
        logger.info(f'Swipe {point2str(*p1)} -> {point2str(*p2)} [browser]')
        self._browser.swipe_browser(p1, p2, duration=duration)

    def _long_click_browser(self, button, duration=1.0):
        """Long click using browser CDP."""
        x, y = ensure_int(*random_rectangle_point(button.button))
        logger.info(f'Long click {button} @ ({x}, {y}), {duration}s [browser]')
        self._browser.long_click_browser(x, y, duration)

    def _drag_browser(self, p1, p2, point_random=(-10, -10, 10, 10)):
        """Drag using browser CDP."""
        p1, p2 = ensure_int(p1, p2)
        logger.info(f'Drag {point2str(*p1)} -> {point2str(*p2)} [browser]')
        self._browser.drag_browser(p1, p2)
