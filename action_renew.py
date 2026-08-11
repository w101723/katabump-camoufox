#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import requests
from camoufox.async_api import AsyncCamoufox, AsyncNewContext
from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://dashboard.katabump.com"
LOGIN_URL = f"{BASE_URL}/auth/login"
LOGOUT_URL = f"{BASE_URL}/auth/logout"
SCREENSHOT_DIR = Path("screenshots")
DEFAULT_TIMEOUT_MS = int(os.getenv("DEFAULT_TIMEOUT_MS", "60000"))
MAX_RENEW_ATTEMPTS = int(os.getenv("MAX_RENEW_ATTEMPTS", "8"))

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

ResultStatus = Literal["success", "skipped", "failed"]


@dataclass
class User:
    username: str
    password: str


@dataclass
class UserResult:
    username: str
    status: ResultStatus
    message: str


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", value)
    return cleaned[:120] or "user"


def load_users() -> list[User]:
    raw: Any = None

    users_json = os.getenv("USERS_JSON", "").strip()
    if users_json:
        try:
            raw = json.loads(users_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"USERS_JSON 不是合法 JSON: {exc}") from exc
    else:
        local_file = Path("login.json")
        if local_file.exists():
            try:
                raw = json.loads(local_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"login.json 不是合法 JSON: {exc}") from exc

    if isinstance(raw, dict):
        raw = raw.get("users", [])

    if not isinstance(raw, list) or not raw:
        raise RuntimeError(
            "未找到账号。GitHub Actions 请配置 USERS_JSON；本地运行可创建 login.json。"
        )

    users: list[User] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"第 {index} 个账号配置不是对象")
        username = str(item.get("username", "")).strip()
        password = str(item.get("password", ""))
        if not username or not password:
            raise RuntimeError(f"第 {index} 个账号缺少 username/password")
        users.append(User(username=username, password=password))
    return users


def browser_headless_mode() -> bool | Literal["virtual"]:
    value = os.getenv("CAMOUFOX_HEADLESS", "").strip().lower()
    if value == "virtual":
        return "virtual"
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    if os.getenv("GITHUB_ACTIONS", "").lower() == "true":
        return "virtual"
    return False


def _telegram_post_text(message: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        data={"chat_id": TG_CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()


def _telegram_post_photo(image_path: Path) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID or not image_path.exists():
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
    with image_path.open("rb") as fp:
        response = requests.post(
            url,
            data={"chat_id": TG_CHAT_ID},
            files={"photo": fp},
            timeout=30,
        )
    response.raise_for_status()


async def send_telegram(message: str, image_path: Path | None = None) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        await asyncio.to_thread(_telegram_post_text, message)
        if image_path:
            await asyncio.to_thread(_telegram_post_photo, image_path)
        print("[Telegram] 通知已发送")
    except Exception as exc:
        print(f"[Telegram] 发送失败: {exc}")


async def screenshot(page: Page, username: str, suffix: str) -> Path:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{safe_name(username)}_{suffix}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        print(f"[Screenshot] {path}")
    except Exception as exc:
        print(f"[Screenshot] 保存失败: {exc}")
    return path


async def locator_visible(locator, timeout: int = 700) -> bool:
    try:
        return await locator.is_visible(timeout=timeout)
    except Exception:
        return False


async def turnstile_response_value(page: Page) -> str:
    selectors = [
        'input[name="cf-turnstile-response"]',
        'textarea[name="cf-turnstile-response"]',
        '[name="cf-turnstile-response"]',
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() > 0:
                value = await locator.get_attribute("value")
                if not value:
                    try:
                        value = await locator.input_value(timeout=500)
                    except Exception:
                        value = None
                if value:
                    return value.strip()
        except Exception:
            pass
    return ""


async def has_turnstile(page: Page) -> bool:
    if await turnstile_response_value(page):
        return True
    selectors = [
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[title*="challenge" i]',
        'iframe[title*="security" i]',
        '[name="cf-turnstile-response"]',
        '.cf-turnstile',
    ]
    for selector in selectors:
        try:
            if await page.locator(selector).count() > 0:
                return True
        except Exception:
            pass
    return any("challenges.cloudflare.com" in (frame.url or "") for frame in page.frames)


async def click_turnstile_once(page: Page) -> bool:
    # First try Playwright locators inside the Cloudflare frame.
    for frame in page.frames:
        frame_url = frame.url or ""
        if "challenges.cloudflare.com" not in frame_url and "turnstile" not in frame_url.lower():
            continue

        candidates = [
            frame.get_by_role("checkbox").first,
            frame.locator('input[type="checkbox"]').first,
            frame.locator('[role="checkbox"]').first,
        ]
        for candidate in candidates:
            try:
                if await candidate.count() > 0 and await candidate.is_visible(timeout=700):
                    await candidate.click(timeout=2500)
                    print("[Turnstile] 已通过 frame locator 点击")
                    return True
            except Exception:
                pass

        # Fallback: click a human-like position inside the iframe using Playwright mouse.
        try:
            frame_element = await frame.frame_element()
            box = await frame_element.bounding_box()
            if box and box["width"] > 20 and box["height"] > 20:
                x = box["x"] + min(32, max(18, box["width"] * 0.12))
                y = box["y"] + box["height"] / 2
                await page.mouse.click(x, y)
                print(f"[Turnstile] 已点击 iframe 坐标 ({x:.1f}, {y:.1f})")
                return True
        except Exception:
            pass

    # Fallback for frames not exposed yet: locate the iframe from the top page.
    for selector in [
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[title*="challenge" i]',
        'iframe[title*="security" i]',
    ]:
        iframe = page.locator(selector).first
        try:
            if await iframe.count() == 0:
                continue
            box = await iframe.bounding_box()
            if box and box["width"] > 20 and box["height"] > 20:
                x = box["x"] + min(32, max(18, box["width"] * 0.12))
                y = box["y"] + box["height"] / 2
                await page.mouse.click(x, y)
                print(f"[Turnstile] 已点击顶层 iframe 坐标 ({x:.1f}, {y:.1f})")
                return True
        except Exception:
            pass

    return False


async def solve_turnstile_if_present(
    page: Page,
    stage: str,
    timeout_seconds: int = 20,
) -> bool:
    if not await has_turnstile(page):
        print(f"[{stage}] 未检测到 Turnstile，继续")
        return True

    if await turnstile_response_value(page):
        print(f"[{stage}] Turnstile 已有有效响应")
        return True

    print(f"[{stage}] 检测到 Turnstile")
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_click = 0.0

    while asyncio.get_running_loop().time() < deadline:
        if await turnstile_response_value(page):
            print(f"[{stage}] ✅ Turnstile 已通过")
            return True

        now = asyncio.get_running_loop().time()
        if now - last_click >= 3:
            await click_turnstile_once(page)
            last_click = now

        # Some Turnstile variants expose a Success! label inside the frame.
        for frame in page.frames:
            if "challenges.cloudflare.com" not in (frame.url or ""):
                continue
            try:
                if await frame.get_by_text("Success!", exact=False).is_visible(timeout=300):
                    print(f"[{stage}] ✅ Turnstile frame 显示 Success")
                    return True
            except Exception:
                pass

        await page.wait_for_timeout(700)

    print(f"[{stage}] ⚠️ Turnstile 未确认成功，将继续后续操作并由服务端结果判断")
    return False


async def get_altcha_status(page: Page) -> dict[str, Any]:
    script = r"""
    () => {
      const normalize = (v) => v == null ? '' : String(v).trim();
      const widget = document.querySelector('altcha-widget');
      const inputs = Array.from(document.querySelectorAll(
        'input[name="altcha"], textarea[name="altcha"], input[name*="altcha" i], textarea[name*="altcha" i]'
      ));
      const firstFilled = inputs.find(el => normalize(el.value).length > 0);
      const shadow = widget ? widget.shadowRoot : null;
      const checkbox = shadow ? shadow.querySelector('input[type="checkbox"], [role="checkbox"]') : null;
      const stateProp = normalize(widget ? widget.state : '');
      const stateAttr = normalize(widget ? widget.getAttribute('state') : '');
      const valueProp = normalize(widget ? widget.value : '');
      const valueAttr = normalize(widget ? widget.getAttribute('value') : '');
      const hiddenValue = normalize(firstFilled ? firstFilled.value : '');
      const checked = checkbox && typeof checkbox.checked === 'boolean' ? checkbox.checked : null;
      const ariaChecked = normalize(checkbox ? checkbox.getAttribute('aria-checked') : '');
      const busy = normalize(widget ? widget.getAttribute('aria-busy') : '') === 'true';
      const state = stateProp || stateAttr || '';
      const solved = state === 'verified' || valueProp.length > 0 || valueAttr.length > 0 || hiddenValue.length > 0;
      const verifying = !solved && (
        ['verifying', 'processing', 'working'].includes(state) ||
        checked === true || ariaChecked === 'true' || busy
      );
      return {
        exists: !!widget || inputs.length > 0,
        solved,
        verifying,
        state: state || 'unknown',
        checked,
        ariaChecked,
        valueLength: Math.max(valueProp.length, valueAttr.length),
        hiddenLength: hiddenValue.length,
        busy,
      };
    }
    """
    try:
        return await page.evaluate(script)
    except Exception:
        return {
            "exists": False,
            "solved": False,
            "verifying": False,
            "state": "error",
            "checked": None,
            "ariaChecked": "",
            "valueLength": 0,
            "hiddenLength": 0,
            "busy": False,
        }


def format_altcha(status: dict[str, Any]) -> str:
    return (
        f"state={status.get('state')}, solved={status.get('solved')}, "
        f"verifying={status.get('verifying')}, checked={status.get('checked')}, "
        f"valueLen={status.get('valueLength')}, hiddenLen={status.get('hiddenLength')}"
    )


async def click_altcha_once(page: Page) -> bool:
    # Playwright CSS locators can traverse open shadow DOM.
    candidates = [
        page.locator('altcha-widget input[type="checkbox"]').first,
        page.locator('altcha-widget [role="checkbox"]').first,
        page.locator('altcha-widget button').first,
    ]
    for candidate in candidates:
        try:
            if await candidate.count() > 0 and await candidate.is_visible(timeout=700):
                await candidate.click(timeout=2500)
                print("[ALTCHA] 已通过 locator 点击")
                return True
        except Exception:
            pass

    # Fallback: calculate an inner shadow-DOM element rectangle and click via page.mouse.
    try:
        box = await page.evaluate(
            r"""
            () => {
              const widget = document.querySelector('altcha-widget');
              if (!widget) return null;
              const target = widget.shadowRoot?.querySelector(
                'input[type="checkbox"], [role="checkbox"], label, button'
              );
              const el = target || widget;
              const r = el.getBoundingClientRect();
              if (!r || r.width <= 0 || r.height <= 0) return null;
              return {x: r.left, y: r.top, width: r.width, height: r.height, exact: !!target};
            }
            """
        )
        if box:
            if box["exact"]:
                x = box["x"] + box["width"] / 2
            else:
                x = box["x"] + min(25, max(12, box["width"] * 0.15))
            y = box["y"] + box["height"] / 2
            await page.mouse.click(x, y)
            print(f"[ALTCHA] 已点击坐标 ({x:.1f}, {y:.1f})")
            return True
    except Exception as exc:
        print(f"[ALTCHA] 坐标点击失败: {exc}")

    return False


async def solve_altcha_if_present(
    page: Page,
    stage: str = "Renew",
    timeout_seconds: int = 70,
) -> bool:
    status = await get_altcha_status(page)
    if not status.get("exists"):
        print(f"[{stage}] 未检测到 ALTCHA，继续")
        return True
    if status.get("solved"):
        print(f"[{stage}] ALTCHA 已通过")
        return True

    print(f"[{stage}] 检测到 ALTCHA: {format_altcha(status)}")
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_click = 0.0
    last_status = ""

    while asyncio.get_running_loop().time() < deadline:
        status = await get_altcha_status(page)
        status_text = format_altcha(status)
        if status_text != last_status:
            print(f"[{stage}] ALTCHA: {status_text}")
            last_status = status_text

        if status.get("solved"):
            print(f"[{stage}] ✅ ALTCHA 已通过")
            return True

        now = asyncio.get_running_loop().time()
        if not status.get("verifying") and now - last_click >= 4:
            if await click_altcha_once(page):
                last_click = now

        await page.wait_for_timeout(800)

    final_status = await get_altcha_status(page)
    print(f"[{stage}] ❌ ALTCHA 超时: {format_altcha(final_status)}")
    return False


async def ensure_login_page(page: Page) -> None:
    if "dashboard" in page.url and "/auth/login" not in page.url:
        try:
            await page.goto(LOGOUT_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
        except Exception:
            pass

    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(1200)

    if "/auth/login" not in page.url and "dashboard" in page.url:
        await page.goto(LOGOUT_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")


async def login(page: Page, user: User) -> tuple[bool, str]:
    await ensure_login_page(page)

    email = page.locator('input[type="email"], input[name="email"], input[autocomplete="username"]').first
    password = page.locator('input[type="password"], input[name="password"], input[autocomplete="current-password"]').first

    try:
        await email.wait_for(state="visible", timeout=10000)
        await password.wait_for(state="visible", timeout=10000)
        await email.fill(user.username)
        await password.fill(user.password)
    except Exception as exc:
        return False, f"找不到登录输入框: {exc}"

    await solve_turnstile_if_present(page, "登录", timeout_seconds=18)

    try:
        await page.get_by_role("button", name="Login", exact=True).click(timeout=10000)
    except Exception as exc:
        return False, f"Login 按钮点击失败: {exc}"

    # Wait for either login failure text, the server list, or URL transition.
    for _ in range(30):
        bad_password = page.get_by_text("Incorrect password or no account", exact=False)
        if await locator_visible(bad_password, 250):
            return False, "账号或密码错误"

        see = page.get_by_role("link", name="See", exact=True).first
        if await locator_visible(see, 250):
            return True, "登录成功"

        if "/auth/login" not in page.url:
            # Give the dashboard a little extra time to render.
            try:
                await see.wait_for(state="visible", timeout=5000)
                return True, "登录成功"
            except Exception:
                pass

        await page.wait_for_timeout(500)

    return False, "登录后未进入服务器列表"


async def open_first_server(page: Page) -> tuple[bool, str]:
    see = page.get_by_role("link", name="See", exact=True).first
    try:
        await see.wait_for(state="visible", timeout=15000)
        await see.click()
        await page.wait_for_timeout(1000)
        return True, "已进入服务器详情"
    except Exception as exc:
        return False, f"未找到 See 链接: {exc}"


async def renew_server(page: Page, user: User) -> UserResult:
    for attempt in range(1, MAX_RENEW_ATTEMPTS + 1):
        print(f"[Renew] 尝试 {attempt}/{MAX_RENEW_ATTEMPTS}")
        renew_button = page.get_by_role("button", name="Renew", exact=True).first

        if not await locator_visible(renew_button, 2500):
            shot = await screenshot(page, user.username, "no_renew")
            message = "未找到 Renew 按钮，服务器可能已续期或当前不可续期"
            await send_telegram(f"ℹ️ Katabump\n用户: {user.username}\n{message}", shot)
            return UserResult(user.username, "skipped", message)

        try:
            await renew_button.click(timeout=8000)
        except Exception as exc:
            await page.wait_for_timeout(1000)
            if attempt == MAX_RENEW_ATTEMPTS:
                return UserResult(user.username, "failed", f"Renew 按钮点击失败: {exc}")
            continue

        modal = page.locator("#renew-modal")
        try:
            await modal.wait_for(state="visible", timeout=7000)
        except PlaywrightTimeoutError:
            if attempt == MAX_RENEW_ATTEMPTS:
                return UserResult(user.username, "failed", "Renew 弹窗未出现")
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
            continue

        try:
            box = await modal.bounding_box()
            if box:
                await page.mouse.move(
                    box["x"] + box["width"] * 0.55,
                    box["y"] + box["height"] * 0.45,
                    steps=6,
                )
        except Exception:
            pass

        await solve_turnstile_if_present(page, "Renew/Turnstile", timeout_seconds=20)
        altcha_ok = await solve_altcha_if_present(page, "Renew/ALTCHA", timeout_seconds=70)
        if not altcha_ok:
            await screenshot(page, user.username, f"altcha_failed_{attempt}")
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            continue

        confirm = modal.get_by_role("button", name="Renew", exact=True).first
        if not await locator_visible(confirm, 2500):
            await screenshot(page, user.username, f"confirm_missing_{attempt}")
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
            continue

        await screenshot(page, user.username, f"before_confirm_{attempt}")

        try:
            await confirm.click(timeout=8000)
        except Exception as exc:
            if attempt == MAX_RENEW_ATTEMPTS:
                return UserResult(user.username, "failed", f"确认 Renew 点击失败: {exc}")
            await page.reload(wait_until="domcontentloaded")
            continue

        # Observe service response.
        captcha_error = False
        for _ in range(20):
            captcha_error_locator = page.get_by_text(
                "Please complete the captcha to continue", exact=False
            )
            if await locator_visible(captcha_error_locator, 250):
                captcha_error = True
                print("[Renew] 服务端提示 Captcha 未完成")
                break

            not_time = page.get_by_text("You can't renew your server yet", exact=False)
            if await locator_visible(not_time, 250):
                try:
                    text = await not_time.inner_text()
                except Exception:
                    text = "You can't renew your server yet"
                match = re.search(r"as of\s+(.*?)\s+\(", text, re.I)
                next_time = match.group(1).strip() if match else "未解析到具体时间"
                shot = await screenshot(page, user.username, "skipped")
                message = f"还没到续期时间；下次可用: {next_time}"
                await send_telegram(f"⏳ Katabump 暂不续期\n用户: {user.username}\n{message}", shot)
                return UserResult(user.username, "skipped", message)

            if not await locator_visible(modal, 250):
                shot = await screenshot(page, user.username, "success")
                message = "服务器续期成功"
                await send_telegram(f"✅ Katabump 续期成功\n用户: {user.username}", shot)
                return UserResult(user.username, "success", message)

            await page.wait_for_timeout(250)

        if captcha_error or await locator_visible(modal, 300):
            if attempt < MAX_RENEW_ATTEMPTS:
                print("[Renew] 本轮未成功，刷新后重试")
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)
                continue

    shot = await screenshot(page, user.username, "failed")
    message = f"连续 {MAX_RENEW_ATTEMPTS} 次尝试后仍未完成续期"
    await send_telegram(f"❌ Katabump 续期失败\n用户: {user.username}\n{message}", shot)
    return UserResult(user.username, "failed", message)


async def process_user(context: BrowserContext, user: User, index: int, total: int) -> UserResult:
    print(f"\n{'=' * 72}\n处理账号 {index}/{total}: {user.username}\n{'=' * 72}")
    page = await context.new_page()
    page.set_default_timeout(DEFAULT_TIMEOUT_MS)

    try:
        ok, message = await login(page, user)
        if not ok:
            shot = await screenshot(page, user.username, "login_failed")
            await send_telegram(f"❌ Katabump 登录失败\n用户: {user.username}\n原因: {message}", shot)
            return UserResult(user.username, "failed", message)

        ok, message = await open_first_server(page)
        if not ok:
            shot = await screenshot(page, user.username, "server_not_found")
            await send_telegram(f"❌ Katabump 未找到服务器\n用户: {user.username}\n原因: {message}", shot)
            return UserResult(user.username, "failed", message)

        return await renew_server(page, user)

    except Exception as exc:
        shot = await screenshot(page, user.username, "exception")
        message = f"未处理异常: {type(exc).__name__}: {exc}"
        await send_telegram(f"❌ Katabump 执行异常\n用户: {user.username}\n{message}", shot)
        return UserResult(user.username, "failed", message)
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def main() -> int:
    try:
        users = load_users()
    except Exception as exc:
        print(f"配置错误: {exc}")
        return 2

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    mode = browser_headless_mode()
    print(f"Camoufox headless={mode!r}; accounts={len(users)}")
    print("网络出口: GitHub Actions/当前主机直连（本版本不启动代理）")

    results: list[UserResult] = []

    try:
        async with AsyncCamoufox(
            headless=mode,
            humanize=True,
            disable_coop=True,
        ) as browser:
            for index, user in enumerate(users, start=1):
                # Each account gets an isolated browser context and fingerprint identity.
                context = await AsyncNewContext(browser)
                try:
                    result = await process_user(context, user, index, len(users))
                    results.append(result)
                finally:
                    try:
                        await context.close()
                    except Exception:
                        pass
    except Exception as exc:
        print(f"Camoufox 启动/运行失败: {type(exc).__name__}: {exc}")
        return 3

    print("\n执行汇总")
    print("-" * 72)
    for result in results:
        icon = {"success": "✅", "skipped": "⏳", "failed": "❌"}[result.status]
        print(f"{icon} {result.username}: {result.status} - {result.message}")

    failed = [result for result in results if result.status == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)
