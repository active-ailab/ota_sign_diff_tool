---
name: feishu-mcp-reauth-public
description: Automate Feishu MCP reauthorization for open.feishu.cn/page/mcp links. Use when the user needs to click 重新授权, complete the 授权 dialog, verify the temporary success state, and confirm the expiry date refreshes correctly. Public-safe edition with no personal auth state or local private paths. Includes Linux install helpers that create a local .venv, install Python 3 dependencies, install Playwright Chromium, and default python_path to the .venv python3 interpreter to reduce manual setup.
---

# Feishu MCP Reauthorization (Public Edition)

Use this skill to automate Feishu MCP reauthorization flows for MCP pages under `open.feishu.cn/page/mcp/...`.

## What This Skill Does

1. Open the target Feishu MCP page.
2. Wait for `重新授权`.
3. Click `重新授权`.
4. Wait for the Feishu authorization dialog.
5. Click `授权`.
6. Wait for the temporary `重新授权成功` state.
7. Confirm the page returns to the MCP config view.
8. Confirm the expiry date matches `today + 7 days`.

## Public-Safe Scope

- Exclude personal browser session, cookies, screenshots, logs, and historical runs from the published package.
- Keep `state/`, `runs/`, and user-local config as local-only data.

## Install and Configure

Linux first-run path:

```bash
bash scripts/install_linux.sh
/home/your-user/path/to/skill/.venv/bin/python scripts/configure_skill.py
/home/your-user/path/to/skill/.venv/bin/python scripts/run_feishu_mcp_reauth.py --headed --url https://open.feishu.cn/page/mcp/XXXXXXXXXXXX
```

## Important Behavior

- `scripts/install_linux.sh` creates `.venv` with `python3`, installs Python dependencies, installs Playwright Chromium, and writes `config/user_config.json` with `python_path` defaulted to `.venv/bin/python`.
- `scripts/configure_skill.py` preserves that default unless the user overrides it.
- `scripts/run_feishu_mcp_reauth.py` reads defaults from `config/user_config.json` so post-install commands are shorter.
- First run should usually use `--headed` to complete manual Feishu login and save local auth state.
