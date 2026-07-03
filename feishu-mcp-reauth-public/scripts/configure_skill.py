#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG_DIR = ROOT / 'config'
CFG_DIR.mkdir(parents=True, exist_ok=True)
CFG = CFG_DIR / 'user_config.json'
VENV_DIR = ROOT / '.venv'
VENV_PYTHON = VENV_DIR / 'bin' / 'python'
RUNNER = ROOT / 'scripts' / 'run_feishu_mcp_reauth.py'


def ask(prompt: str, default: str = '') -> str:
    suffix = f' [{default}]' if default else ''
    value = input(f'{prompt}{suffix}: ').strip()
    return value or default


def detect_python() -> str:
    candidates = [
        VENV_PYTHON,
        Path(shutil.which('python3') or ''),
        Path(shutil.which('python') or ''),
    ]
    for path in candidates:
        if path and str(path) and path.exists():
            return str(path)
    return 'python3'


def normalize_bool(value: str) -> bool:
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def main() -> int:
    existing = {}
    if CFG.exists():
        try:
            existing = json.loads(CFG.read_text(encoding='utf-8'))
        except Exception:
            existing = {}

    default_python = existing.get('python_path') or detect_python()
    data = {
        'feishu_openid': ask('请输入你的飞书 OpenID', existing.get('feishu_openid', '')),
        'default_mcp_url': ask('请输入默认 Feishu MCP 链接', existing.get('default_mcp_url', 'https://open.feishu.cn/page/mcp/')),
        'python_path': ask('请输入本地 Python 路径（留空默认使用 .venv / python3）', default_python),
        'headed_default': normalize_bool(
            ask('首次是否默认 headed 运行 (true/false)', str(existing.get('headed_default', True)).lower())
        ),
    }
    CFG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'已写入配置: {CFG}')
    print('下一步可执行：')
    default_flag = '--headed ' if data['headed_default'] else ''
    print(f"  {data['python_path']} {RUNNER} {default_flag}--url {data['default_mcp_url']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
