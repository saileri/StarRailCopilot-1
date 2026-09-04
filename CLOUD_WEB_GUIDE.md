# 云星穹铁道网页版适配 (cloud_web)

在浏览器中直接运行云·星穹铁道，无需模拟器。

## 前提条件

- **国服账号**（云铁网页版仅支持国服）
- **Chrome 或 Chromium 浏览器**（需安装对应版本 chromedriver）
- **Python 3.10+**
- **selenium >= 4.10.0**（已加入 requirements-in.txt）

## 安装步骤

### 1. 克隆分支

```bash
git clone -b feature/cloud-web https://github.com/YOUR_FORK/StarRailCopilot.git
cd StarRailCopilot
pip install -r requirements-in.txt
pip install selenium>=4.10.0
```

### 2. 安装 ChromeDriver

**方式 A（推荐）**：使用内置 Chrome for Testing（March7thAssistant 方案）
```bash
# selenium 4.x 会自动管理 chromedriver
# 只需确保系统安装了 Chrome/Chromium
```

**方式 B**：手动安装
```bash
# 查看你的 Chrome 版本
google-chrome --version
# 下载对应版本的 chromedriver
# https://chromedriver.chromium.org/downloads
```

### 3. 配置

在 SRC 的 WebUI 中或直接编辑配置文件：

```json
{
  "Alas": {
    "Emulator": {
      "GameClient": "cloud_web"
    }
  }
}
```

或者手动编辑 `config/你的配置名.json`：

```json
{
  "Alas": {
    "Emulator": {
      "GameClient": "cloud_web",
      "PackageName": "CN-Official",
      "Serial": "auto"
    },
    "Optimization": {
      "WhenTaskQueueEmpty": "close_game"
    }
  }
}
```

> ⚠️ `GameClient` 设为 `cloud_web` 后，`PackageName` 会自动设为 `CN-Official`，
> `WhenTaskQueueEmpty` 会自动设为 `close_game`。

### 4. 运行

```bash
python src.py
```

首次运行时浏览器会自动打开 `https://sr.mihoyo.com/cloud`，你需要手动登录一次。
登录信息会保存在 `browser_profile/` 目录，后续运行免登录。

## 工作原理

```
┌─────────────────────────────────────────────────────┐
│                    SRC 任务调度                       │
├─────────────────────────────────────────────────────┤
│  Device.screenshot()  ──→  BrowserDevice.screenshot_browser()  │
│         │                         │                           │
│         │                   CDP Page.captureScreenshot        │
│         │                   1920×1080 → resize 1280×720       │
│         │                         │                           │
│         ▼                         ▼                           │
│  模板匹配/OCR            与现有素材完全兼容                    │
├─────────────────────────────────────────────────────┤
│  Device.click(x,y)    ──→  BrowserDevice.click_browser(x,y)  │
│         │                         │                           │
│         │                   坐标 ×1.5 (1280→1920)             │
│         │                   CDP Input.dispatchMouseEvent      │
├─────────────────────────────────────────────────────┤
│  Device.app_start()   ──→  browser_start()                  │
│  Device.app_stop()    ──→  browser_stop()                   │
│  Device.dump_hierarchy()──→  dump_hierarchy_browser()        │
│                            (JS DOM → lxml etree)             │
└─────────────────────────────────────────────────────┘
```

## 新增配置项

| 配置 | 位置 | 说明 |
|------|------|------|
| `GameClient = cloud_web` | `Alas.Emulator` | 启用网页云铁模式 |

### BrowserDevice 内部设置（需编辑代码）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GAME_URL` | `https://sr.mihoyo.com/cloud` | 云铁网页地址 |
| `_headless` | `False` | 无头模式（True = 不显示浏览器窗口） |
| `_persistent` | `True` | 保留浏览器 profile（免登录） |
| `_debug_port` | `9222` | Chrome 调试端口 |

## 首次登录流程

1. 运行 SRC，设置 `GameClient = cloud_web`
2. Chrome 浏览器自动打开云铁页面
3. **手动登录**你的米哈游账号（手机号/邮箱 + 密码 或 扫码）
4. 登录成功后，SRC 自动接管后续流程
5. 浏览器 profile 保存在 `browser_profile/`，下次免登录

## 常见问题

### Q: 截图分辨率不对？
A: 云铁网页版默认 1920×1080。BrowserDevice 通过 CDP `Emulation.setDeviceMetricsOverride` 强制设为 1920×1080，
截图后 resize 到 1280×720 与 SRC 素材匹配。如果你改成移动端 UA，UI 会变成手机版，
但需要重新制作所有模板图片——不建议。

### Q: 首次启动后黑屏？
A: 确保 Chrome 版本 ≥ 100，且 chromedriver 版本匹配。
无 GPU 服务器上云铁会走软件解码，CPU 占用较高。

### Q: 云铁网页版只支持国服吗？
A: 是的。`sr.mihoyo.com/cloud` 仅限国服。国际服需要对应域名。

### Q: 可以用 headless 模式吗？
A: 可以，但登录需要扫码。设置 `_headless = True` 后，
浏览器不显示窗口，但你需要另外扫码登录（暂未实现自动 QR 推送，
可参考 March7thAssistant 的实现）。

### Q: 性能如何？
A: CDP 截图约 50-100ms/帧，与 ADB 方式相当。
如果需要更快（20-30ms），可切换到 `screenshot_video_element()`（从 `<video>` 标签抓帧），
但该方法在 HEVC 编码下可能失败。

## 与 cloud_android 模式的对比

| 特性 | cloud_android | cloud_web |
|------|-------------|-----------|
| 运行环境 | 模拟器 + 云铁APK | Chrome 浏览器 |
| 截图方式 | uiautomator2 / ADB | CDP Page.captureScreenshot |
| 输入方式 | uiautomator2 XPath | CDP Input.dispatchMouseEvent |
| 登录流程 | APK 自动登录 | 网页手动/扫码登录 |
| 内存占用 | 高（模拟器 2-4GB） | 低（Chrome 200-500MB） |
| CPU 占用 | 高 | 中 |
| 无头运行 | 否 | 是 |
| 多开 | 困难 | 容易（不同 Chrome profile） |
| 服务器运行 | 需要图形环境 | headless 可用 |

## 文件结构

```
module/device/method/browser.py     # BrowserDevice: 截图/输入/浏览器管理
tasks/login/cloud_web.py           # LoginWebCloud: 网页登录/排队/进出游戏
module/device/device.py             # Device 类 cloud_web 分支
module/config/config.py             # is_cloud_web_game 属性
module/config/config_generated.py   # GameClient cloud_web 选项
module/config/config_updater.py     # 配置联动
tasks/login/login.py                # 登录流程分派
```

## 致谢

本实现参考了 [March7thAssistant](https://github.com/moesnow/March7thAssistant) 的
`CloudGameController` 实现，特别是：
- 浏览器启动参数和 profile 管理
- CDP 截图方案
- 云铁网页版 DOM 选择器
- localStorage 自动战斗设置
- HEVC/H264 兼容模式分析