# feishu-mcp-reauth-public

可公开发布的飞书 MCP 重新授权技能（Linux 版）。

## 目标

安装后帮助用户：
- 自动安装运行依赖
- 自动安装浏览器（推荐 Chromium / Chrome）
- 自动生成本地配置文件
- 指导用户填写飞书 OpenID、本地路径、目标 MCP 页面 URL
- 运行一次 headed 模式完成首次登录与授权态落盘

## 适用环境

- Linux（Ubuntu / Debian 优先）
- Python 3.10+
- 可联网下载 Python 依赖与 Playwright Chromium

## 安装步骤

```bash
cd feishu-mcp-reauth-public
bash scripts/install_linux.sh
.venv/bin/python scripts/configure_skill.py
.venv/bin/python scripts/run_feishu_mcp_reauth.py --headed --url <你的飞书MCP链接>
```

## 自动安装内容

`install_linux.sh` 会尝试：
- 安装 Python venv
- 安装 `playwright pillow opencv-python-headless rapidocr-onnxruntime`
- 安装 Playwright Chromium
- 检查系统是否已有 `google-chrome` / `chromium` / `chromium-browser`
- 若检测不到，给出下一步安装提示

## 自动配置内容

`configure_skill.py` 会交互式生成：
- `config/user_config.json`

需要用户填写：
- Feishu OpenID
- 默认 MCP URL
- 本地 Python 路径（可选；安装后默认已写为 `.venv/bin/python`，通常无需手改）
- 是否默认 headed

## 重要说明

- 安装脚本会优先创建 `.venv`，并把 `python_path` 直接写成该虚拟环境里的 Python 3 解释器，避免用户机器只有 `python3` 没有 `python` 时额外手工修配置。

- `state/`、`runs/`、日志、截图、登录态不会提交到 GitHub。
- 首次运行建议使用 `--headed`，手工登录飞书后保存本地授权态。
- 后续可切换为 headless 自动运行。
