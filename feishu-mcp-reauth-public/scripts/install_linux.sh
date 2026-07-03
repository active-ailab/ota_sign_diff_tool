#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
PY_BIN="${PYTHON_BIN:-python3}"
CONFIG_DIR="$ROOT_DIR/config"
CONFIG_FILE="$CONFIG_DIR/user_config.json"
RUNNER="$ROOT_DIR/scripts/run_feishu_mcp_reauth.py"
CONFIGURER="$ROOT_DIR/scripts/configure_skill.py"

mkdir -p "$CONFIG_DIR"

echo "[1/6] 检查 Python3..."
command -v "$PY_BIN" >/dev/null 2>&1 || { echo "未找到 python3"; exit 1; }

if [ ! -d "$VENV_DIR" ]; then
  echo "[2/6] 创建虚拟环境..."
  "$PY_BIN" -m venv "$VENV_DIR"
else
  echo "[2/6] 复用已有虚拟环境: $VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "[3/6] 安装 Python 依赖..."
python -m pip install -U pip setuptools wheel
python -m pip install playwright pillow opencv-python-headless rapidocr-onnxruntime

echo "[4/6] 安装 Playwright Chromium..."
python -m playwright install chromium || python -m playwright install --with-deps chromium

echo "[5/6] 生成默认配置（优先写入 .venv python3 路径）..."
if [ ! -f "$CONFIG_FILE" ]; then
  cat > "$CONFIG_FILE" <<JSON
{
  "feishu_openid": "",
  "default_mcp_url": "https://open.feishu.cn/page/mcp/",
  "python_path": "$VENV_DIR/bin/python",
  "headed_default": true
}
JSON
else
  python - <<PY
import json
from pathlib import Path
cfg = Path(r'''$CONFIG_FILE''')
data = json.loads(cfg.read_text(encoding='utf-8'))
data['python_path'] = r'''$VENV_DIR/bin/python'''
data.setdefault('default_mcp_url', 'https://open.feishu.cn/page/mcp/')
data.setdefault('headed_default', True)
data.setdefault('feishu_openid', '')
cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('updated', cfg)
PY
fi

echo "[6/6] 检查系统浏览器..."
if command -v google-chrome >/dev/null 2>&1; then
  echo "检测到 google-chrome"
elif command -v chromium >/dev/null 2>&1; then
  echo "检测到 chromium"
elif command -v chromium-browser >/dev/null 2>&1; then
  echo "检测到 chromium-browser"
else
  echo "未检测到系统 Chrome/Chromium。"
  echo "建议安装其一："
  echo "  sudo apt update"
  echo "  sudo apt install -y chromium-browser"
fi

echo
echo "安装完成。默认解释器已写为：$VENV_DIR/bin/python"
echo "建议下一步："
echo "  $VENV_DIR/bin/python $CONFIGURER"
echo "  $VENV_DIR/bin/python $RUNNER --headed --url https://open.feishu.cn/page/mcp/XXXXXXXXXXXX"
