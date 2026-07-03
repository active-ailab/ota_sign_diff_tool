#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

SKILL_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = SKILL_ROOT / "state" / "feishu_mcp_auth.json"
CONFIG_PATH = SKILL_ROOT / "config" / "user_config.json"
LOGIN_SUBSTRING = "accounts.feishu.cn/accounts/page/login"
AUTH_IFRAME_SUBSTRING = "accounts.feishu.cn/open-apis/authen/v1/authorize"


def load_user_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def parse_args() -> argparse.Namespace:
    cfg = load_user_config()
    parser = argparse.ArgumentParser(description="Run the Feishu MCP reauthorization automation flow")
    parser.add_argument(
        "--url",
        default=cfg.get("default_mcp_url", "https://open.feishu.cn/page/mcp/7615508457076231131"),
        help="Target Feishu MCP page URL",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        default=bool(cfg.get("headed_default", False)),
        help="Run browser in headed mode for login/debugging",
    )
    parser.add_argument("--output-dir", default=None, help="Optional explicit output directory")
    parser.add_argument(
        "--login-wait-seconds",
        type=int,
        default=600,
        help="How long headed mode waits for manual login to complete",
    )
    return parser.parse_args()


def wait_for_url_not_contains(page, substring: str, timeout_ms: int) -> str:
    deadline = time.time() + timeout_ms / 1000.0
    last_url = page.url
    while time.time() < deadline:
        last_url = page.url
        if substring not in last_url:
            return last_url
        page.wait_for_timeout(500)
    raise PlaywrightTimeoutError(f"Timed out waiting for URL to stop containing {substring}: {last_url}")


def wait_for_auth_frame(page, timeout_ms: int):
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        for frame in page.frames:
            if AUTH_IFRAME_SUBSTRING in frame.url:
                return frame
        page.wait_for_timeout(250)
    raise PlaywrightTimeoutError(f"Timed out waiting for auth iframe containing {AUTH_IFRAME_SUBSTRING}")


def normalize_text(value: str) -> str:
    return "".join(value.split())


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def add_history(history: list[dict[str, Any]], step: str, action: str, url: str, **extra: Any) -> None:
    item: dict[str, Any] = {
        "step": step,
        "action": action,
        "url": url,
        "timestamp": time.time(),
    }
    item.update(extra)
    history.append(item)


def main() -> int:
    args = parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_dir).resolve() if args.output_dir else SKILL_ROOT / "runs" / stamp
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = output_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "history": [],
        "artifacts": {},
        "last_url": None,
        "status": None,
    }

    storage_state_value = str(STATE_PATH) if STATE_PATH.exists() else None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        context = browser.new_context(storage_state=storage_state_value)
        page = context.new_page()
        page.set_default_timeout(20000)

        try:
            page.goto(args.url, wait_until="domcontentloaded")
            result["last_url"] = page.url
            add_history(result["history"], "open_page", "open", page.url)

            if LOGIN_SUBSTRING in page.url:
                add_history(result["history"], "check_login_redirect", "url_check", page.url, matched=True)
                if not args.headed:
                    result["status"] = "login-required"
                    write_json(output_dir / "run.json", result)
                    return 3
                print("Headed mode enabled. Browser is waiting for manual Feishu login.")
                new_url = wait_for_url_not_contains(page, LOGIN_SUBSTRING, args.login_wait_seconds * 1000)
                result["last_url"] = new_url
                add_history(result["history"], "wait_manual_login", "wait_url_not_contains", new_url, matched=True)
            else:
                add_history(result["history"], "check_login_redirect", "url_check", page.url, matched=False)

            page.get_by_role("button", name="重新授权").wait_for(timeout=20000)
            add_history(result["history"], "wait_reauth", "wait_text", page.url, text="重新授权")

            before_path = artifacts_dir / "before-reauth.png"
            page.screenshot(path=str(before_path))
            result["artifacts"]["before"] = str(before_path)
            add_history(result["history"], "capture_before", "screenshot", page.url, path=str(before_path))

            page.get_by_role("button", name="重新授权").click(timeout=15000)
            add_history(result["history"], "click_reauth", "click", page.url, target="button:重新授权")

            auth_frame = wait_for_auth_frame(page, 20000)
            auth_frame.get_by_text("飞书 MCP请求你的授权").wait_for(timeout=20000)
            add_history(result["history"], "wait_dialog", "wait_frame_text", page.url, frame_url=auth_frame.url)

            dialog_path = artifacts_dir / "auth-dialog.png"
            page.screenshot(path=str(dialog_path))
            result["artifacts"]["dialog"] = str(dialog_path)
            add_history(result["history"], "capture_dialog", "screenshot", page.url, path=str(dialog_path))

            auth_frame.get_by_role("button", name="授权", exact=True).click(timeout=15000)
            add_history(result["history"], "click_authorize", "click", page.url, frame_url=auth_frame.url, target="button:授权")

            page.get_by_text("重新授权成功").wait_for(timeout=20000)
            add_history(result["history"], "wait_success_toast", "wait_text", page.url, text="重新授权成功")

            page.get_by_text("该链接将于").wait_for(timeout=20000)
            page.get_by_role("button", name="重新授权").wait_for(timeout=20000)
            add_history(result["history"], "wait_return_page", "wait_text", page.url, text="该链接将于")

            after_path = artifacts_dir / "after-reauth.png"
            page.screenshot(path=str(after_path))
            result["artifacts"]["after"] = str(after_path)
            add_history(result["history"], "capture_after", "screenshot", page.url, path=str(after_path))

            body_text = page.locator("body").inner_text()
            expected_date = (datetime.now() + timedelta(days=7)).strftime("%Y年%m月%d日")
            checks = {
                "has_reauth_button": "重新授权" in body_text,
                "has_expiry_text": "自动失效" in body_text,
                "has_expected_date": expected_date in normalize_text(body_text),
                "expected_date": expected_date,
            }
            result["verification"] = checks
            result["last_url"] = page.url

            if all([checks["has_reauth_button"], checks["has_expiry_text"], checks["has_expected_date"]]):
                result["status"] = "reauth-verified"
                add_history(result["history"], "finish_ok", "finish", page.url, status="reauth-verified")
                write_json(output_dir / "run.json", result)
                return 0

            result["status"] = "reauth-check-failed"
            add_history(result["history"], "finish_fail", "finish", page.url, status="reauth-check-failed")
            write_json(output_dir / "run.json", result)
            return 4
        except PlaywrightTimeoutError as exc:
            result["status"] = "timeout"
            result["error"] = str(exc)
            result["last_url"] = page.url
            fail_path = artifacts_dir / "failure.png"
            try:
                page.screenshot(path=str(fail_path))
                result["artifacts"]["failure"] = str(fail_path)
            except Exception:
                pass
            write_json(output_dir / "run.json", result)
            return 1
        finally:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(STATE_PATH))
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
