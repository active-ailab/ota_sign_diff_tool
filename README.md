# OTA Sign Diff Tool

OTA 差分签名工具 —— 基于 Flask 的内网 Web 应用,把两个不同版本的 OTA 签名包(旧版 + 新版)整理成可发布的差分包,上传到内网 NAS 并返回可复制的分享链接。

## 核心功能

提供三种差分模式:

| 模式 | 输入 | 说明 |
|------|------|------|
| NAS 链接差分 | 两个 NAS 分享链接 | 自动下载旧/新包后生成差分包 |
| 版本号差分 | 项目名 + 两个版本号 | 自动从 NAS 版本目录定位对应包 |
| 本地上传差分 | 两个本地总包 | 自动提取 `watch@<芯片名>_ota_sign.zip` 后差分 |

通用流程:下载/接收原始包 → 提取 `watch@<芯片名>_ota_sign.zip` → 解析版本 → 生成差分包 → 上传 NAS → 返回分享链接。

## 支持平台

| 芯片 | 包名 |
|------|------|
| 黄山3 | `watch@mhs003_ota_sign.zip` |
| 黄山3s | `watch@mhs003s_ota_sign.zip` |
| apollo4 | `watch@apollo4_ota_sign.zip` |
| mimxrt595 | `watch@mimxrt595_ota_sign.zip` |

## 目录结构

```text
.
├── app.py                         # Flask 主应用(Web 界面 + API + 任务调度)
├── prepare_ota_sign_from_nas.py   # NAS 文件准备处理(命令行入口,被 app.py 调用)
├── ota_gen_diff.py                # 差分包生成核心
├── ota_diff_apply.py              # 差分包应用工具
├── templates/                     # 前端页面
│   ├── index.html                 # 主页面
│   ├── stats.html                 # 统计页
│   └── feishu_test.html           # 飞书调试页(已弃用,可忽略)
├── ota_local_config.example.json  # 配置模板(真实配置不入库)
├── requirements.txt               # Python 依赖清单
├── CLEANUP_SUMMARY.md             # 项目清理记录
├── 业务流程与问题总结.md          # 业务流程与踩坑记录
└── .github/workflows/             # CI(飞书通知工作流)
```

## 环境依赖

### 运行环境

- **Python 3.8+**(Windows;使用 `dataclasses`、`subprocess(text=True)` 等特性)
- **内网环境**:应用必须运行在能访问内网 NAS(`10.2.100.85`)的机器上,当前部署形态为合肥内网 Windows Server。外网 / 云服务器无法打通 NAS 访问链路。

### Python 依赖

```bash
pip install -r requirements.txt
```

`requirements.txt` 内容:

```text
flask>=2.2
requests>=2.28
urllib3>=1.26
```

### 外部工具(差分生成)

差分包生成依赖官方桌面工具 **OTA升级助手.exe**:

- 默认路径:`D:\ota_assistant1.0.2.3\ota_assistant\OTA升级助手.exe`
- 可通过命令行 `--ota-tool <路径>` 或在 `ota_local_config.json` 的 `ota_tool` 字段覆盖

### 配置与密钥(私发,不入库)

运行前需准备两个文件(由管理员通过私密渠道单独提供):

| 文件 | 作用 | 是否入库 |
|------|------|---------|
| `ota_local_config.json` | NAS 地址/账号、飞书 app 凭据、`pem`/`ota_tool` 路径等 | ❌ 不入库 |
| `sign.pem` | OTA 签名私钥(EC 私钥) | ❌ 不入库 |

复制 `ota_local_config.example.json` 为 `ota_local_config.json` 并填写真实值,把 `sign.pem` 放到项目根目录。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 准备配置(私发获得)
#    - 复制 ota_local_config.example.json 为 ota_local_config.json,填写真实凭据
#    - 将 sign.pem 放到项目根目录
#    - 确认 OTA升级助手.exe 路径正确(或写入 ota_tool 字段)

# 3. 启动
python app.py
```

访问 `http://127.0.0.1:5000`。

## 使用方法

1. 打开页面,选择差分模式(NAS 链接 / 版本号 / 本地上传)。
2. 填写对应参数并提交任务。
3. 页面实时显示阶段进度:上传 → 扫描压缩包 → 定位 ota_sign → 生成差分包 → 上传 NAS。
4. 完成后页面展示差分包文件名和 NAS 分享链接,点击一键复制。

## 注意事项

### 安全

- **敏感文件严禁提交仓库**:`ota_local_config.json`、`sign.pem` 已加入 `.gitignore`,仅通过私密渠道分发。
- **不要在代码中硬编码密钥**:`app.py` 中的飞书凭据已改为占位符,真实值统一从 `ota_local_config.json` 读取。
- 仓库启用了 GitHub push protection,推送含密钥的提交会被服务器拒绝。
- 差分包与签名产物属高风险发布资产,请校验输入来源、版本匹配与签名密钥权限。

### 网络

- 应用必须运行在**能访问内网 NAS** 的机器上;外网 / 云服务器无法打通 NAS 链路。
- NAS 分享链接下载走内网 WebDAV,自签名证书场景已默认关闭 TLS 校验(`nas_verify_tls: false`)。

### 功能

- 飞书机器人自动推送已**弃用**(内网无法满足飞书要求的 HTTPS + 公网域名),现改为页面展示链接 + 一键复制。`/feishu-test`、`/api/feishu/*` 为遗留调试路由,可忽略。
- 仅支持上述四个芯片平台的 `watch@<芯片名>_ota_sign.zip` 命名。
- 长任务耗时较长,请以页面阶段日志判断进度,勿重复提交。

### CI

- `.github/workflows/feishu-notify.yml` 复用 `active-ailab/skills-manifest` 的可复用工作流,用于推送/手动触发时向飞书通知仓库变更;需在仓库配置 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 两个 secrets。注意它与业务内的 OTA 任务结果通知不是一回事。

## 相关文档

- [CLEANUP_SUMMARY.md](./CLEANUP_SUMMARY.md) — 项目清理记录
- [业务流程与问题总结.md](./业务流程与问题总结.md) — 业务流程与开发踩坑记录
