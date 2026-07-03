from flask import Flask, request, render_template, Response, stream_with_context, jsonify, url_for
import os
import sys
import json
from datetime import datetime
import time
import subprocess
from pathlib import Path
import re
import hashlib
import random
import string
import zipfile
import tarfile
import tempfile
import struct
import shutil
from urllib.parse import urlparse, unquote
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from dataclasses import dataclass, field, asdict
from enum import Enum

import requests
import urllib3

# 飞书机器人配置（真实凭据在 ota_local_config.json，切勿硬编码）
APP_ID = "这里贴你的App ID"
APP_SECRET = "这里贴你的App Secret"
FEISHU_TENANT_ID = ""  # 可选，从 app_secret 推导
FEISHU_SEND_MESSAGE_ENABLED = False  # 仅当配置正确时启用

app = Flask(__name__)
os.makedirs("generated_files", exist_ok=True)
os.makedirs("task_logs", exist_ok=True)

ARTIFACT_KEEP_COUNT = 10
TASK_KEEP_COUNT = 100
TASK_LOG_MAX_LINES = 2000
TASK_LOG_MAX_LINE_CHARS = 2000
PROJECT_NAME_CAPTURE = r'[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*'
OTA_SIGN_ZIP_NAME_RE = re.compile(r'^watch@[^/\\]+_ota_sign\.zip$', re.IGNORECASE)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _collect_candidates(root: Path, recurse: bool, only_files: bool, only_dirs: bool, name_re: str = None):
    if not root.exists():
        return []

    iterator = root.rglob('*') if recurse else root.iterdir()
    pattern = re.compile(name_re) if name_re else None
    results = []
    for p in iterator:
        if only_files and not p.is_file():
            continue
        if only_dirs and not p.is_dir():
            continue
        if pattern and not pattern.search(p.name):
            continue
        results.append(p)
    return results


def _prune_old_entries(root: Path, keep_count: int, recurse: bool = False, only_files: bool = False,
                       only_dirs: bool = False, name_re: str = None, emit=None):
    candidates = _collect_candidates(root, recurse=recurse, only_files=only_files, only_dirs=only_dirs, name_re=name_re)
    if len(candidates) <= keep_count:
        return 0

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    stale = candidates[keep_count:]
    removed = 0

    for p in stale:
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=False)
            else:
                p.unlink(missing_ok=True)
            removed += 1
        except Exception as e:
            if emit:
                emit(f"[WARN] 清理旧文件失败: {p} ({e})\n")

    if removed and emit:
        emit(f"[INFO] 自动清理完成: {root}，删除 {removed} 个旧条目（保留最近 {keep_count} 个）\n")
    return removed


def cleanup_runtime_artifacts(emit=None):
    keep_count = max(1, ARTIFACT_KEEP_COUNT)
    removed_total = 0

    # Web 端历史配置文本（generated_files/*.txt）
    removed_total += _prune_old_entries(Path("generated_files"), keep_count, only_files=True, emit=emit)

    # 异步上传任务工作目录（work/uploads/<task_id>/）
    removed_total += _prune_old_entries(Path("work") / "uploads", keep_count, only_dirs=True, emit=emit)

    # ota_input 任务目录（work/ota_input/<project>/<nas_link_xxx|local_upload_xxx>/）
    removed_total += _prune_old_entries(
        Path("work") / "ota_input",
        keep_count,
        recurse=True,
        only_dirs=True,
        name_re=r"^(nas_link|local_upload)_",
        emit=emit,
    )

    # 差分产物（递归清理 *_diff_*.zip）
    removed_total += _prune_old_entries(
        Path("work"),
        keep_count,
        recurse=True,
        only_files=True,
        name_re=r"_diff_.*\.zip$",
        emit=emit,
    )

    # 日志目录（若存在）
    removed_total += _prune_old_entries(Path("task_logs"), keep_count, only_files=True, emit=emit)
    removed_total += _prune_old_entries(Path("log"), keep_count, only_files=True, emit=emit)

    return removed_total


# 启动时清理改为“加载本地配置后再执行”，以支持可配置保留数量

# ========== 异步任务系统 ==========

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class Task:
    task_id: str
    mode: str  # 'version', 'nas_link', 'local_upload'
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    logs: list = field(default_factory=list)
    created_at: str = ''
    completed_at: str = ''
    open_id: str = ''  # 飞书触发人（兼容旧字段，实际为 receive_id）
    receive_id_type: str = 'open_id'  # open_id / user_id
    error_message: str = ''
    result_file: str = ''  # 差分包输出路径
    nas_sharing_url: str = ''  # NAS 分享链接（可选）
    project_name: str = ''  # 项目名称（用于统计）
    old_version: str = ''  # 旧版本号
    new_version: str = ''  # 新版本号
    
    def to_dict(self):
        d = asdict(self)
        d['status'] = self.status.value
        d['result_file_name'] = Path(self.result_file).name if self.result_file else ''
        return d

class TaskManager:
    def __init__(self):
        self.tasks = {}
        self.lock = Lock()
        self.executor = ThreadPoolExecutor(max_workers=3)

    @staticmethod
    def _is_completed(status):
        return status in (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED)

    @staticmethod
    def _task_sort_time(task):
        ts = task.completed_at or task.created_at or ''
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            return datetime.min

    def _prune_tasks_locked(self):
        keep_count = max(1, TASK_KEEP_COUNT)
        if len(self.tasks) <= keep_count:
            return

        completed_items = [
            (task_id, task)
            for task_id, task in self.tasks.items()
            if self._is_completed(task.status)
        ]
        completed_items.sort(key=lambda item: self._task_sort_time(item[1]))

        # 优先删除最老的已完成任务，避免长期运行占用内存持续增加。
        while len(self.tasks) > keep_count and completed_items:
            stale_task_id, _ = completed_items.pop(0)
            self.tasks.pop(stale_task_id, None)
    
    def create_task(self, mode, data):
        task_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        task = Task(
            task_id=task_id,
            mode=mode,
            created_at=now,
            open_id=data.get('open_id', ''),
            receive_id_type=(data.get('receive_id_type') or 'open_id'),
        )
        with self.lock:
            self.tasks[task_id] = task
            self._prune_tasks_locked()
        return task
    
    def get_task(self, task_id):
        with self.lock:
            return self.tasks.get(task_id)
    
    def update_task_status(self, task_id, status):
        with self.lock:
            if task_id in self.tasks:
                self.tasks[task_id].status = status
                if status in (TaskStatus.SUCCESS, TaskStatus.FAILED):
                    self.tasks[task_id].completed_at = datetime.now().isoformat()
                self._prune_tasks_locked()
    
    def append_log(self, task_id, line):
        with self.lock:
            if task_id in self.tasks:
                text = line if isinstance(line, str) else str(line)
                if len(text) > TASK_LOG_MAX_LINE_CHARS:
                    text = text[:TASK_LOG_MAX_LINE_CHARS] + '...\n'
                logs = self.tasks[task_id].logs
                logs.append(text)
                if len(logs) > TASK_LOG_MAX_LINES:
                    self.tasks[task_id].logs = logs[-TASK_LOG_MAX_LINES:]
    
    def update_progress(self, task_id, percentage):
        with self.lock:
            if task_id in self.tasks:
                self.tasks[task_id].progress = min(100.0, max(0.0, percentage))
    
    def set_result(self, task_id, result_file, sharing_url='', error=''):
        with self.lock:
            if task_id in self.tasks:
                self.tasks[task_id].result_file = result_file
                self.tasks[task_id].nas_sharing_url = sharing_url
                self.tasks[task_id].error_message = error
    
    def set_project_info(self, task_id, project_name='', old_version='', new_version=''):
        """更新任务的项目名称和版本信息（用于统计）。"""
        with self.lock:
            if task_id in self.tasks:
                if project_name:
                    self.tasks[task_id].project_name = project_name
                if old_version:
                    self.tasks[task_id].old_version = old_version
                if new_version:
                    self.tasks[task_id].new_version = new_version

task_manager = TaskManager()

# 尝试导入差分生成函数
try:
    from ota_gen_diff import gen_diff_archive
    HAS_GEN_DIFF = True
except Exception as e:
    HAS_GEN_DIFF = False
    app.logger.warning(f"无法导入 ota_gen_diff 模块: {e}")

# ========== 飞书 API 集成 ==========

def _exchange_feishu_login_code(code: str):
    """将飞书网页登录授权码换成可用于通知的用户标识。"""
    if not code:
        return None, "缺少授权 code"
    if not APP_ID or not APP_SECRET or APP_ID == '这里贴你的App ID':
        return None, "缺少飞书应用配置"

    candidates = [
        (
            'https://open.feishu.cn/open-apis/authen/v1/access_token',
            {
                'app_id': APP_ID,
                'app_secret': APP_SECRET,
                'code': code,
                'grant_type': 'authorization_code',
            },
        ),
    ]

    last_error = ''
    for url, payload in candidates:
        try:
            resp = requests.post(url, json=payload, timeout=10)
            data, parse_error = _safe_json_response(resp, f"飞书换码接口 {url}")
            if data is None:
                last_error = parse_error
                continue
        except Exception as e:
            last_error = str(e)
            continue

        if data.get('code') != 0:
            last_error = data.get('msg') or data.get('error') or f'HTTP {resp.status_code}'
            continue

        info = data.get('data') or {}
        receive_id = (info.get('open_id') or info.get('user_id') or '').strip()
        if not receive_id:
            last_error = '飞书接口成功返回，但未获取到 open_id/user_id'
            continue

        receive_id_type = 'open_id' if info.get('open_id') else 'user_id'
        return {
            'receive_id': receive_id,
            'receive_id_type': receive_id_type,
            'open_id': info.get('open_id', '') or receive_id,
            'user_id': info.get('user_id', '') or '',
            'union_id': info.get('union_id', '') or '',
            'raw': info,
        }, ''

    return None, last_error or '飞书授权码换取用户标识失败'


def _safe_json_response(resp, context: str):
    """尽量从飞书返回中解析 JSON，兼容 XSSI 前缀和夹杂文本的响应。"""
    raw_text = (resp.text or '').strip()
    if not raw_text:
        return None, f"{context} 返回空响应"

    try:
        return resp.json(), ''
    except Exception as e:
        pass

    candidate = raw_text.lstrip('\ufeff')
    prefixes = (
        ")]}'",
        ")]}'",
        ")]}",
        ")]}'\n",
        ")]}'\r\n",
    )
    for prefix in prefixes:
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):].lstrip()
            break

    start = candidate.find('{')
    end = candidate.rfind('}')
    if start != -1 and end != -1 and end > start:
        candidate = candidate[start:end + 1]
        try:
            return json.loads(candidate), ''
        except Exception as e:
            return None, f"{context} JSON 解析失败: {e}; 原始响应前 300 字符: {raw_text[:300]}"

    return None, f"{context} 不是有效 JSON; 原始响应前 300 字符: {raw_text[:300]}"

def get_feishu_tenant_access_token():
    """获取飞书租户 access_token，用于发送机器人私消息。"""
    if not APP_ID or not APP_SECRET:
        return None, "缺少飞书应用配置"
    try:
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        resp = requests.post(url, json={
            'app_id': APP_ID,
            'app_secret': APP_SECRET
        }, timeout=10)
        if resp.status_code == 200:
            data, parse_error = _safe_json_response(resp, "飞书 tenant_access_token")
            if data is None:
                return None, parse_error
            if data.get('code') == 0:
                return data.get('tenant_access_token'), ''
            return None, data.get('msg') or str(data.get('code'))
        return None, f"HTTP {resp.status_code}"
    except Exception as e:
        app.logger.warning(f"获取飞书 tenant token 失败: {e}")
        return None, str(e)

def send_feishu_message(receive_id: str, text: str, task_result: dict = None, receive_id_type: str = 'open_id'):
    """发送飞书私消息给指定用户。"""
    if not FEISHU_SEND_MESSAGE_ENABLED or not receive_id:
        if not FEISHU_SEND_MESSAGE_ENABLED:
            return False, "飞书通知未启用"
        return False, "缺少 receive_id"

    receive_id_type = (receive_id_type or 'open_id').strip()
    if receive_id_type not in ('open_id', 'user_id'):
        receive_id_type = 'open_id'
    
    access_token, token_error = get_feishu_tenant_access_token()
    if not access_token:
        return False, f"获取 tenant_access_token 失败: {token_error}"
    
    try:
        # 构建消息体
        message_content = text
        if task_result:
            if task_result.get('nas_sharing_url'):
                message_content += f"\n🔗 NAS下载链接：{task_result['nas_sharing_url']}"
            if task_result.get('result_file'):
                message_content += f"\n📦 差分包：{Path(task_result['result_file']).name}"
            message_content += f"\n完成时间: {task_result.get('completed_at', 'N/A')}"
        
        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        params = {'receive_id_type': receive_id_type}
        payload = {
            'receive_id': receive_id,
            'msg_type': 'text',
            'content': json.dumps({'text': message_content})
        }
        resp = requests.post(url, json=payload, headers=headers, params=params, timeout=10)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
        data = resp.json()
        if data.get('code') == 0:
            return True, ''
        return False, data.get('msg') or str(data.get('code'))
    except Exception as e:
        app.logger.warning(f"发送飞书消息失败: {e}")
        return False, str(e)


def notify_task_result(task: Task, emit, text: str, task_result: dict = None):
    """发送任务飞书通知，并把结果写入任务日志。"""
    if not FEISHU_SEND_MESSAGE_ENABLED:
        emit("[WARN] 飞书通知未启用，已跳过发送。\n")
        return False
    if not task.open_id:
        emit("[WARN] 未获取到飞书用户标识（open_id/user_id），已跳过飞书通知。请在飞书容器内打开页面，或手动填写 open_id。\n")
        return False

    ok, reason = send_feishu_message(task.open_id, text, task_result, task.receive_id_type)
    if ok:
        emit(f"[OK] 飞书通知发送成功({task.receive_id_type}): {task.open_id[:8]}...\n")
        return True

    emit(f"[WARN] 飞书通知发送失败: {reason}\n")
    return False


def _mask_receiver_id(value: str) -> str:
    if not value:
        return ''
    if len(value) <= 8:
        return value
    return f"{value[:4]}...{value[-4:]}"

def upload_to_nas_and_share(local_file_path: str, nas_config: dict, emit=None) -> tuple:
    """上传文件到 NAS 并生成分享链接。
    
    返回: (分享链接, 错误消息)
    说明: 若上传失败，仍可能返回成功状态（带警告日志），以确保任务继续进行
    """
    if not emit:
        emit = lambda x: None
    
    try:
        local_path = Path(local_file_path)
        if not local_path.exists():
            raise RuntimeError(f"本地文件不存在: {local_file_path}")
        
        emit(f"[INFO] 开始上传文件到 NAS: {local_file_path}\n")
        
        # 导入 WebDavClient
        try:
            from prepare_ota_sign_from_nas import WebDavClient
        except ImportError as e:
            emit(f"[WARN] 无法导入WebDavClient，NAS上传功能禁用: {e}\n")
            return '', None
        
        # 获取 NAS 配置
        nas_address = nas_config.get('nas_address', '')
        nas_username = nas_config.get('nas_username', '')
        nas_password = nas_config.get('nas_password', '')
        verify_tls = bool(nas_config.get('nas_verify_tls', False))
        
        # 检查必要的配置
        if not nas_address or not nas_username or not nas_password:
            emit(f"[WARN] NAS配置不完整，跳过上传\n")
            return '', None
        
        # 构建 WebDAV URL
        protocol = 'https' if nas_address.endswith(':5001') or nas_address.endswith(':443') else 'http'
        base_url = f"{protocol}://{nas_address}/dav"
        
        # 创建 WebDAV 客户端
        client = WebDavClient(
            base_url=base_url,
            username=nas_username,
            password=nas_password,
            verify_tls=verify_tls,
            timeout=1800
        )
        
        # 生成远程路径（在 NAS 上的目标位置）
        remote_dir = "/GT智能手表事业部/软件部/软件版本文档/hzj-test/DIFF_OTA_GQF"
        file_name = local_path.name
        remote_path = f"{remote_dir}/{file_name}"
        
        emit(f"[INFO] 远程路径: {remote_path}\n")
        
        # 上传文件
        if not client.upload_file(local_path, remote_path, emit=emit):
            # 上传失败，但不中断任务
            emit(f"[WARN] 文件上传失败，但任务继续进行\n")
            # 返回成功但不含分享链接，至少本地文件已生成
            return '', None
        
        emit(f"[INFO] 文件上传完成，生成分享链接...\n")
        
        # 生成分享链接
        sharing_url = client.create_sharing_link(remote_path, emit=emit)
        
        if sharing_url:
            emit(f"[OK] NAS分享链接: {sharing_url}\n")
            return sharing_url, None
        else:
            # 向用户返回警告，但仍然认为成功（文件已上传）
            emit(f"[WARN] 分享链接生成失败，但文件已上传到 NAS\n")
            return '', None
    
    except Exception as e:
        error_msg = f"NAS上传异常: {str(e)}"
        emit(f"[WARN] {error_msg}\n")
        # 即使异常，也返回None（非致命错误）而不是错误消息
        return '', None

# 读取本地配置（包含 PEM 文件路径等）
def load_config():
    # 相对 app.py 所在目录定位，与启动时的工作目录无关
    config_file = Path(__file__).with_name("ota_local_config.json")
    if config_file.exists():
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                # 从配置读取飞书 app_id/app_secret（如果有的话）
                global APP_ID, APP_SECRET, FEISHU_SEND_MESSAGE_ENABLED, ARTIFACT_KEEP_COUNT
                global TASK_KEEP_COUNT, TASK_LOG_MAX_LINES, TASK_LOG_MAX_LINE_CHARS
                if cfg.get('feishu_app_id'):
                    APP_ID = cfg.get('feishu_app_id')
                if cfg.get('feishu_app_secret'):
                    APP_SECRET = cfg.get('feishu_app_secret')
                # 如果配置了有效的 app_id 和 app_secret，启用飞书消息功能
                FEISHU_SEND_MESSAGE_ENABLED = bool(APP_ID and APP_SECRET and APP_ID != '这里贴你的App ID')
                # 可选：运行产物/日志保留数量上限（默认 10）
                keep_count = cfg.get('cleanup_keep_count')
                if keep_count is not None:
                    try:
                        ARTIFACT_KEEP_COUNT = max(1, int(keep_count))
                    except Exception:
                        app.logger.warning(f"cleanup_keep_count 配置无效: {keep_count}")

                task_keep_count = cfg.get('task_keep_count')
                if task_keep_count is not None:
                    try:
                        TASK_KEEP_COUNT = max(1, int(task_keep_count))
                    except Exception:
                        app.logger.warning(f"task_keep_count 配置无效: {task_keep_count}")

                task_log_max_lines = cfg.get('task_log_max_lines')
                if task_log_max_lines is not None:
                    try:
                        TASK_LOG_MAX_LINES = max(100, int(task_log_max_lines))
                    except Exception:
                        app.logger.warning(f"task_log_max_lines 配置无效: {task_log_max_lines}")

                task_log_max_line_chars = cfg.get('task_log_max_line_chars')
                if task_log_max_line_chars is not None:
                    try:
                        TASK_LOG_MAX_LINE_CHARS = max(200, int(task_log_max_line_chars))
                    except Exception:
                        app.logger.warning(f"task_log_max_line_chars 配置无效: {task_log_max_line_chars}")
                # 解析 pem 路径：支持相对路径（相对于配置文件所在目录）
                pem_val = cfg.get('pem')
                if pem_val:
                    pem_p = Path(pem_val)
                    if not pem_p.is_absolute() or not pem_p.exists():
                        resolved = config_file.parent / pem_p
                        if resolved.exists():
                            cfg['pem'] = str(resolved.resolve())
                return cfg
        except Exception as e:
            app.logger.warning(f"读取配置文件失败: {e}")
    return {}


# 启动时先加载配置，再做一次轻量清理，避免历史堆积
try:
    load_config()
    cleanup_runtime_artifacts()
except Exception:
    pass


def _extract_sharing_token(sharing_url: str) -> str:
    m = re.search(r"/sharing/([^/?#]+)", sharing_url)
    if not m:
        raise RuntimeError("链接中没有分享 token")
    return m.group(1)


def _extract_archive_filename(text: str) -> str:
    if not text:
        return ""

    patterns = [
        r'"filename"\s*:\s*"(archive_[^"]+?)"',
        r"filename\s*:\s*\"(archive_[^\"]+?)\"",
        r'(archive_[^"\'<>\s]+\.(?:tgz|tar\.gz|zip|zab))',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidate = unquote(match.group(1)).strip()
            if candidate.lower().startswith("archive_"):
                return candidate

    # 兜底：也对 watch@<chip>_ota_sign.zip 类型的文件名做提取
    # 用于直接分享 OTA 签名包（非总包）的场景
    watch_patterns = [
        r'"filename"\s*:\s*"(watch@[^"]+?)"',
        r"filename\s*:\s*\"(watch@[^\"]+?)\"",
        r'(watch@[^"\'<>\s]+\.zip)',
    ]
    for pattern in watch_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return unquote(match.group(1)).strip()

    return ""


def _get_shared_archive_filename(session: requests.Session, base: str, token: str, verify_tls: bool = False):
    url = f"{base}/webapi/entry.cgi"
    params = {
        'api': 'SYNO.Core.Sharing.Session',
        'version': '1',
        'method': 'get',
        'sharing_id': token,
    }
    resp = session.get(url, params=params, verify=verify_tls, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"NAS 分享会话查询失败，状态码: {resp.status_code}")

    filename = _extract_archive_filename(resp.text or "")
    if not filename:
        return ""
    return filename


def get_nas_sharing_info(sharing_url: str, nas_address: str, username: str, password: str, verify_tls: bool = False, emit=None):
    """
    仅通过分享页 URL 自动拿下载地址：
    1) 调用 SYNO.Core.Sharing.Login 获取 sharing_sid（cookie）
    2) 使用 /fsdownload/<token>/ 直接下载（不需要文件名）
    返回 (direct_url, session)
    """
    _ = nas_address  # 兼容旧参数
    token = _extract_sharing_token(sharing_url)
    parsed = urlparse(sharing_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    session = requests.Session()

    if username and password:
        login_url = f"{base}/sharing/webapi/entry.cgi"
        params = {
            'api': 'SYNO.Core.Sharing.Login',
            'version': '1',
            'method': 'login',
            'sharing_id': token,
            'username': username,
            'passwd': password,
        }
        resp = session.get(login_url, params=params, verify=verify_tls, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"NAS 分享登录失败，状态码: {resp.status_code}")

        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"NAS 分享登录响应不是 JSON: {resp.text[:300]}") from e

        if not data.get('success'):
            raise RuntimeError(f"NAS 分享登录失败: {data.get('error')}")

        if emit:
            emit("[INFO] NAS 分享登录成功，已获取 sharing_sid\n")

    filename = None
    try:
        filename = _get_shared_archive_filename(session, base, token, verify_tls=verify_tls)
    except Exception as e:
        if emit:
            emit(f"[WARN] 登录后解析共享文件名失败，回退免文件名直链: {e}\n")

    direct_url = f"{base}/fsdownload/{token}/"
    if filename:
        direct_url = f"{direct_url}{filename}"
        if emit:
            emit(f"[INFO] 已从共享接口解析文件名: {filename}\n")
    if emit:
        emit(f"[INFO] 已生成免文件名下载直链: {direct_url}\n")

    return direct_url, session
        



# OTA 签名头部结构体格式（与 ota_gen_diff.py 保持一致）
# '<8sHI2H16sI10B': magic_1(8s) magic_2(I) head_size(H) fw_size(I) pnpid(H) pnpver(H) fw_ver(16s) fw_origin_size(I) reserved(10B)
_SIG_HEAD_STRUCT = '<8sHI2H16sI10B'


def _parse_version_from_sign_footer(zip_path: Path):
    """从 OTA 签名包的签名尾部提取固件版本号（兜底方案）。
    优先读取 zip 文件本身的签名尾部（与 gen_diff_archive 的 get_fw_version 行为一致），
    再回退到内部 .bin 成员文件的签名尾部。"""
    sig_head_size = struct.calcsize(_SIG_HEAD_STRUCT)

    def _try_extract(data: bytes, label: str):
        if len(data) < sig_head_size + 64:
            return None
        sig_hdr = data[-(sig_head_size + 64):-64]
        if len(sig_hdr) != sig_head_size:
            return None
        try:
            hdr = struct.unpack(_SIG_HEAD_STRUCT, sig_hdr)
            fw_ver = hdr[5].decode('utf-8', errors='ignore').strip('\x00').strip()
            if fw_ver and re.match(r'\d+\.\d+\.\d+\.\d+', fw_ver):
                return fw_ver
        except Exception:
            pass
        return None

    # 1) 从 zip 文件本身的签名尾部提取（与 gen_diff_archive 行为一致）
    try:
        with open(zip_path, 'rb') as f:
            data = f.read()
        ver = _try_extract(data, str(zip_path))
        if ver:
            return ver
    except Exception:
        pass

    # 2) 回退：从内部 .bin 成员文件的签名尾部提取
    try:
        with zipfile.ZipFile(str(zip_path), 'r') as zf:
            names = zf.namelist()
            priority = [
                'META/extfw/firmware_exbin_sign.bin',
                'META/extfw/fct_test_sign.bin',
                'META/bootloader_sign.bin',
            ]
            ordered = []
            for p in priority:
                if p in names:
                    ordered.append(p)
            for n in names:
                if n not in ordered and n.lower().endswith('.bin'):
                    ordered.append(n)
            for name in ordered:
                try:
                    data = zf.read(name)
                except Exception:
                    continue
                ver = _try_extract(data, name)
                if ver:
                    return ver
    except Exception:
        pass

    return None


def parse_version_from_ota_zip(zip_path: Path):
    """提取 OTA 签名包中的版本号，优先级：META/fw_info → 签名尾部 → None"""
    try:
        with zipfile.ZipFile(str(zip_path), 'r', zipfile.ZIP_DEFLATED) as zf:
            if 'META/fw_info' in zf.namelist():
                content = zf.read('META/fw_info')
                m = re.search(rb'(\d+\.\d+\.\d+\.\d+)', content)
                if m:
                    return m.group(1).decode('utf-8', errors='ignore')
    except Exception:
        pass

    # 兜底：从 zip 内二进制的签名尾部提取
    return _parse_version_from_sign_footer(zip_path)


def parse_version_from_filename(*names):
    """从多个文件名候选中提取 x.x.x.x 版本号，作为 parse_version_from_ota_zip 的兜底。
    按参数顺序依次尝试，返回第一个匹配到的版本号。
    例如 archive_toulouse_1.4.0.1_202604302026.tgz -> 1.4.0.1"""
    for name in names:
        if not name:
            continue
        normalized = Path(name).name
        # 去掉常见扩展名及 _ota_sign 后缀
        normalized = re.sub(r'(_ota_sign)?\.(tar\.gz|tgz|zip|tar|zab|pkg)$', '', normalized, flags=re.IGNORECASE)
        m = re.search(r'(\d+\.\d+\.\d+\.\d+)', normalized)
        if m:
            return m.group(1)
    return None


def parse_version_from_url(url: str):
    """从 URL 路径中提取 x.x.x.x 版本号。
    例如 https://nas/fsdownload/token/archive_toulouse_1.4.0.1_xxx.tgz -> 1.4.0.1"""
    if not url:
        return None
    try:
        path = urlparse(url).path
        name = Path(unquote(path)).name
        if name:
            return parse_version_from_filename(name)
    except Exception:
        pass
    return None


def resolve_synology_direct_url(url: str, emit, auth=None, verify_tls=False, timeout=30, session=None) -> str:
    """
    支持把 /sharing/<token> 链接解析为 /fsdownload/<token>/<filename>。
    如果不是 sharing 链接或解析失败，则返回原始链接。
    """
    try:
        parsed = urlparse(url)
        path = parsed.path or ""
    except Exception:
        return url

    if '/fsdownload/' in path:
        return url

    m = re.search(r"/sharing/([^/?#]+)", path)
    if not m:
        return url

    token = m.group(1)
    emit(f"[INFO] 识别分享链接，尝试解析直链 token={token}\n")

    filename = ""
    if session is not None:
        try:
            filename = _get_shared_archive_filename(session, f"{parsed.scheme}://{parsed.netloc}", token, verify_tls=verify_tls)
        except Exception as e:
            emit(f"[WARN] 共享接口解析文件名失败: {e}\n")

    if not filename:
        try:
            req = session.get if session is not None else requests.get
            kwargs = {'timeout': timeout, 'verify': verify_tls}
            if session is None and auth is not None:
                kwargs['auth'] = auth
            resp = req(url, **kwargs)
            html = resp.text or ""
        except Exception as e:
            emit(f"[WARN] 拉取分享页失败，回退原链接: {e}\n")
            return url

        filename = _extract_archive_filename(html)

    if not filename:
        emit("[WARN] 未从分享页解析到文件名，回退原链接\n")
        return url

    filename = unquote(filename)
    direct_url = f"{parsed.scheme}://{parsed.netloc}/fsdownload/{token}/{filename}"
    emit(f"[INFO] 已解析下载直链: {direct_url}\n")
    return direct_url


def download_with_progress(url: str, dst_path: Path, emit, auth=None, verify_tls=False, nas_address=None, nas_username=None, nas_password=None, timeout=1800):
    """下载 NAS 链接到本地，并输出统一进度日志。"""
    session = None

    # 先尝试把分享页解析成文件直链
    if nas_address and nas_username and nas_password and '/sharing/' in url:
        try:
            emit("[INFO] 识别到分享页链接，开始自动解析下载直链...\n")
            url, session = get_nas_sharing_info(
                url,
                nas_address,
                nas_username,
                nas_password,
                verify_tls,
                emit=emit,
            )
            emit(f"[INFO] 分享页已解析为直链: {url}\n")
        except Exception as e:
            raise RuntimeError(f"自动解析分享页失败: {e}")

    # 对直链再做一次轻量兜底解析
    real_url = resolve_synology_direct_url(url, emit, auth=auth, verify_tls=verify_tls, session=session)
    emit(f"[INFO] 最终下载链接: {real_url}\n")
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    req = session.get if session is not None else requests.get
    req_kwargs = {
        'stream': True,
        'timeout': timeout,
        'verify': verify_tls,
    }
    if session is None and auth is not None:
        req_kwargs['auth'] = auth

    def _extract_filename_from_response(resp, fallback_url: str) -> str:
        content_disposition = resp.headers.get('content-disposition') or ''
        match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.IGNORECASE)
        if match:
            return Path(unquote(match.group(1).strip().strip('"'))).name

        match = re.search(r'filename="?([^";]+)"?', content_disposition, re.IGNORECASE)
        if match:
            return Path(unquote(match.group(1).strip())).name

        response_name = Path(unquote(urlparse(resp.url or '').path)).name
        if response_name and response_name.lower() not in {'sharing', 'fsdownload'}:
            return response_name

        fallback_name = Path(unquote(urlparse(fallback_url).path)).name
        if fallback_name and fallback_name.lower() not in {'sharing', 'fsdownload'}:
            return fallback_name

        return ''

    with req(real_url, **req_kwargs) as resp:
        if resp.status_code != 200:
            body_snippet = ""
            try:
                body_snippet = (resp.text or "")[:300]
            except Exception:
                pass
            raise RuntimeError(f"下载失败，状态码 {resp.status_code}: {real_url} {body_snippet}")

        ctype = (resp.headers.get('content-type') or '').lower()
        if 'text/html' in ctype:
            token_match = re.search(r"/sharing/([^/?#]+)", url)
            token_tip = token_match.group(1) if token_match else "<token>"
            raise RuntimeError(
                "下载链接返回的是 HTML 网页而不是文件（API 解析或链接格式可能有问题）。"
                f"若要使用直链，格式应为: https://{nas_address or '10.2.100.85:5001'}/fsdownload/{token_tip}/<文件名>"
            )

        real_filename = _extract_filename_from_response(resp, real_url)
        final_dst_path = dst_path
        if real_filename:
            safe_filename = re.sub(r'[^A-Za-z0-9._@\-]', '_', Path(real_filename).name)
            if safe_filename:
                final_dst_path = dst_path.with_name(safe_filename)
                emit(f"[INFO] 源文件名: {safe_filename}\n")

        total = int(resp.headers.get('content-length', 0))
        downloaded = 0
        last_print_pct = -1.0

        with open(final_dst_path, 'wb') as fw:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                fw.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = downloaded * 100.0 / total
                    if pct - last_print_pct >= 5.0 or pct >= 99.5:
                        last_print_pct = pct
                        emit(f"[PROGRESS] {pct:.1f}%\n")

    emit(f"[OK] 已下载到 {final_dst_path}\n")
    return final_dst_path


def infer_project_name_from_name(name: str) -> str:
    """从文件名里推断项目名，如 archive_pike_2.3.1.1_xxx.tgz -> pike。"""
    normalized_name = Path(name).name
    normalized_name = re.sub(r'(\.tar\.gz|\.tgz|\.zip|\.tar|\.zab)$', '', normalized_name, flags=re.IGNORECASE)
    normalized_name = re.sub(r'_ota_sign$', '', normalized_name, flags=re.IGNORECASE)

    explicit_patterns = [
        rf'^archive_OTA_CLOUD_(?P<project>{PROJECT_NAME_CAPTURE})_(?P<version>\d+(?:\.\d+){{3}})(?:_\d{{8,}})?$',
        rf'^archive_CLOUD_(?P<project>{PROJECT_NAME_CAPTURE})_(?P<version>\d+(?:\.\d+){{3}})(?:_\d{{8,}})?$',
        rf'^archive_OTA_(?P<project>{PROJECT_NAME_CAPTURE})_(?P<version>\d+(?:\.\d+){{3}})(?:_\d{{8,}})?$',
        rf'^archive_(?P<project>{PROJECT_NAME_CAPTURE})_(?P<version>\d+(?:\.\d+){{3}})(?:_\d{{8,}})?$',
    ]
    for pattern in explicit_patterns:
        match = re.match(pattern, normalized_name, re.IGNORECASE)
        if match:
            return match.group('project')

    # watch@<chip>_ota_sign.zip → 提取芯片名作为项目名
    # 注意：_ota_sign 后缀以及 .zip 扩展名已在前面被 strip 掉了
    watch_match = re.match(r'^watch@(?P<chip>[A-Za-z0-9]+)$', normalized_name, re.IGNORECASE)
    if watch_match:
        chip = watch_match.group('chip')
        # 尝试将芯片名映射为更友好的项目名（如 apollo4 → apollo4）
        return chip.lower()

    parts = normalized_name.split('_')
    ignored_tokens = {'archive', 'ota', 'cloud'}
    for index, part in enumerate(parts):
        if re.fullmatch(r"\d+(?:\.\d+){3}", part):
            for candidate_index in range(index - 1, -1, -1):
                candidate = parts[candidate_index].strip()
                if candidate and candidate.lower() not in ignored_tokens:
                    return candidate
            break

    return "naslink"


def infer_project_name_from_local_upload_names(old_name: str, new_name: str, emit=None) -> str:
    """从本地上传的总包文件名推断项目名，优先使用 archive_*.tgz / archive_*.tar.gz。"""
    candidates = []
    for raw_name in (old_name, new_name):
        if not raw_name:
            continue
        name = Path(raw_name).name
        lower_name = name.lower()
        if not lower_name.startswith('archive_'):
            continue
        if not lower_name.endswith(('.tgz', '.tar.gz', '.tar', '.zab')):
            continue
        candidates.append(name)

    for candidate in candidates:
        project_name = infer_project_name_from_name(candidate)
        if project_name != 'naslink':
            return project_name

    return 'localupload'


def infer_project_name_from_url(url: str) -> str:
    """从链接路径里的文件名推断项目名。"""
    try:
        path = unquote(urlparse(url).path)
        name = Path(path).name
    except Exception:
        return "naslink"

    return infer_project_name_from_name(name)


def infer_project_name_from_nas_urls(old_url: str, new_url: str, emit=None) -> str:
    """优先从 NAS 分享页解析真实文件名，再推断项目名。"""
    for raw_url in (old_url, new_url):
        if not raw_url:
            continue

        parsed_url = raw_url
        try:
            if '/sharing/' in urlparse(raw_url).path:
                resolved_url = resolve_synology_direct_url(raw_url, emit or (lambda *_: None))
                parsed_url = resolved_url or raw_url
        except Exception:
            parsed_url = raw_url

        project_name = infer_project_name_from_url(parsed_url)
        if project_name != 'naslink':
            return project_name

    return 'naslink'


def maybe_extract_ota_sign(input_path: Path, emit) -> Path:
    """
    若 input_path 已是 ota_sign.zip 则直接返回；
    若是总包（tgz/zip 等）则递归查找并提取 watch@<chip>_ota_sign.zip。
    """
    emit(f"[INFO] 开始扫描本地包中的 ota_sign: {input_path.name}\n")
    if OTA_SIGN_ZIP_NAME_RE.match(input_path.name):
        emit(f"[INFO] 输入文件已是目标 ota_sign 包: {input_path.name}\n")
        return input_path

    # 若下载文件本身就是 OTA 签名包（只是扩展名或文件名不同），直接使用。
    try:
        with zipfile.ZipFile(str(input_path), 'r') as zf:
            names = set(zf.namelist())
            if 'META/fw_info' in names:
                emit(f"[INFO] 检测到输入文件本体即 OTA 签名包: {input_path.name}\n")
                return input_path
            # 兜底：zip 内不含嵌套 ota_sign.zip 但存在 META/ 目录 → 本身即为 OTA 签名包
            has_meta = any(n.startswith('META/') or n.startswith('META\\') for n in names)
            has_nested_ota = any(OTA_SIGN_ZIP_NAME_RE.match(Path(n).name) for n in names)
            if has_meta and not has_nested_ota:
                emit(f"[INFO] META/ 目录存在且无嵌套 ota_sign，检测到输入文件本体即 OTA 签名包: {input_path.name}\n")
                return input_path
    except Exception:
        pass

    def _is_archive(p: Path) -> bool:
        n = p.name.lower()
        return n.endswith('.zip') or n.endswith('.tgz') or n.endswith('.tar.gz') or n.endswith('.tar') or n.endswith('.zab')

    def _is_ota_sign_zip_name(name: str) -> bool:
        return bool(OTA_SIGN_ZIP_NAME_RE.match(Path(name).name))

    def _extract_member_to_file(data: bytes, out_file: Path) -> Path:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with open(out_file, 'wb') as fw:
            fw.write(data)
        return out_file

    def _build_ota_sign_output_path(base_input: Path, source_archive_name: str) -> Path:
        source_name = Path(source_archive_name or '').name or base_input.name
        source_name = re.sub(r'^(?:nested|found)_\d+_', '', source_name, flags=re.IGNORECASE)
        source_stem = re.sub(r'(\.tar\.gz|\.tgz|\.zip|\.tar|\.zab)$', '', source_name, flags=re.IGNORECASE)
        safe_stem = re.sub(r'[^A-Za-z0-9._@\-]', '_', source_stem).strip('._') or base_input.stem
        return base_input.with_name(f"{safe_stem}_ota_sign.zip")

    with tempfile.TemporaryDirectory(prefix='nas_link_unpack_') as temp_dir:
        temp_root = Path(temp_dir)
        queue = [input_path]
        seen = set()
        idx = 0

        while queue and idx < 80:
            cur = queue.pop(0)
            idx += 1
            key = str(cur)
            if key in seen:
                continue
            seen.add(key)

            emit(f"[INFO] 扫描第 {idx} 层压缩包: {cur.name}\n")

            # 先按 zip 尝试（包括部分改后缀文件）
            try:
                with zipfile.ZipFile(str(cur), 'r') as zf:
                    names = zf.namelist()
                    emit(f"[INFO] 解压目录检查完成: {cur.name}, 文件数 {len(names)}\n")
                    for name in names:
                        if _is_ota_sign_zip_name(name):
                            emit(f"[INFO] 在压缩包内命中 ota_sign: {cur.name} -> {name}\n")
                            extracted = _extract_member_to_file(zf.read(name), temp_root / f"found_{idx}_{Path(name).name}")
                            final_out = _build_ota_sign_output_path(input_path, cur.name)
                            shutil.copy2(extracted, final_out)
                            return final_out

                    # 扫描嵌套压缩包
                    for name in names:
                        low = name.lower()
                        if low.endswith(('.zip', '.tgz', '.tar.gz', '.tar', '.zab')):
                            emit(f"[INFO] 发现嵌套压缩包: {name}\n")
                            nested = _extract_member_to_file(zf.read(name), temp_root / f"nested_{idx}_{Path(name).name}")
                            queue.append(nested)
                    continue
            except Exception:
                pass

            # 再按 tar 尝试
            try:
                with tarfile.open(str(cur), 'r:*') as tf:
                    members = tf.getmembers()
                    emit(f"[INFO] 解包目录检查完成: {cur.name}, 文件数 {len(members)}\n")
                    for m in members:
                        if not m.isfile():
                            continue
                        if _is_ota_sign_zip_name(m.name):
                            emit(f"[INFO] 在压缩包内命中 ota_sign: {cur.name} -> {m.name}\n")
                            fr = tf.extractfile(m)
                            if fr is None:
                                continue
                            data = fr.read()
                            extracted = _extract_member_to_file(data, temp_root / f"found_{idx}_{Path(m.name).name}")
                            final_out = _build_ota_sign_output_path(input_path, cur.name)
                            shutil.copy2(extracted, final_out)
                            return final_out

                    for m in members:
                        if not m.isfile():
                            continue
                        low = m.name.lower()
                        if low.endswith(('.zip', '.tgz', '.tar.gz', '.tar', '.zab')):
                            emit(f"[INFO] 发现嵌套压缩包: {m.name}\n")
                            fr = tf.extractfile(m)
                            if fr is None:
                                continue
                            nested = _extract_member_to_file(fr.read(), temp_root / f"nested_{idx}_{Path(m.name).name}")
                            queue.append(nested)
            except Exception:
                pass

    raise RuntimeError("未在提供的包/总包中找到 watch@<chip>_ota_sign.zip")

# ========== 后台任务执行函数 ==========

def execute_version_diff_task(task_id, project_name, base_version, target_version, release_type):
    """后台执行版本号差分任务。"""
    task = task_manager.get_task(task_id)
    if not task:
        return
    
    task_manager.update_task_status(task_id, TaskStatus.RUNNING)
    cfg = load_config()
    
    # 记录项目信息用于统计
    task_manager.set_project_info(task_id, project_name=project_name, old_version=base_version, new_version=target_version)
    
    def emit(line: str):
        task_manager.append_log(task_id, line)
        try:
            sys.stdout.buffer.write(line.encode('utf-8', errors='replace'))
            sys.stdout.buffer.flush()
        except Exception:
            pass

    def emit_summary(line: str):
        """只把阶段性摘要写到任务日志里，避免把差分细节刷给用户。"""
        task_manager.append_log(task_id, line)
        try:
            sys.stdout.buffer.write(line.encode('utf-8', errors='replace'))
            sys.stdout.buffer.flush()
        except Exception:
            pass

    def should_show_process_line(line: str) -> bool:
        if '[PROGRESS]' in line:
            return True
        keep_tokens = (
            '开始下载旧版本',
            '开始下载新版本',
            '已准备好 OTA 差分输入文件',
            '开始生成差分 OTA 包',
            '差分 OTA 生成完成',
            '差分包位置',
            '开始上传文件到 NAS',
            '文件已上传到 NAS',
            '版本号差分任务执行成功',
        )
        return any(token in line for token in keep_tokens)
    
    try:
        emit(f"[INFO] 任务 {task_id} 开始执行版本号差分\n")
        emit(f"[INFO] 项目: {project_name}, {base_version} -> {target_version}\n\n")
        emit(f"[INFO] 通知目标: type={task.receive_id_type}, id={_mask_receiver_id(task.open_id) or 'N/A'}\n")
        
        script_path = "prepare_ota_sign_from_nas.py"
        if not os.path.exists(script_path):
            raise RuntimeError(f"脚本文件不存在: {script_path}")
        
        # 解析项目名和变体
        project_base = project_name
        variant_dir = ""
        if "_" in project_name:
            parts = project_name.split("_", 1)
            project_base = parts[0]
            variant_dir = parts[1]
        
        cmd = [
            'python', '-u', script_path,
            '--project', project_base,
            '--from-ver', base_version,
            '--to-ver', target_version,
            '--type', release_type,
            '--no-open-tool',
            '--debug',
        ]
        if variant_dir:
            cmd.extend(['--variant-dir', variant_dir])
        
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding='utf-8',
            errors='ignore',
            env=env
        )
        
        # 解析输出
        output_file = None
        for line in process.stdout:
            if '[INFO] 下载进度:' in line:
                continue
            if should_show_process_line(line):
                emit_summary(line)
            # 提取差分包输出路径
            if '[INFO] 差分包位置:' in line:
                m = re.search(r'差分包位置:\s*(.+)', line)
                if m:
                    output_file = m.group(1).strip()
            elif '差分 OTA 生成完成:' in line:
                m = re.search(r'差分 OTA 生成完成:\s*(.+)', line)
                if m:
                    output_file = m.group(1).strip()
            # 更新进度（简化版）
            if '[PROGRESS]' in line:
                m = re.search(r'(\d+(?:\.\d+)?)%', line)
                if m:
                    pct = float(m.group(1))
                    task_manager.update_progress(task_id, min(55.0, pct * 0.55))

            if '已准备好 OTA 差分输入文件' in line:
                task_manager.update_progress(task_id, 60.0)
            elif '开始生成差分 OTA 包' in line:
                task_manager.update_progress(task_id, 75.0)
            elif '差分 OTA 生成完成' in line or '差分包位置' in line:
                task_manager.update_progress(task_id, 90.0)
        
        process.wait(timeout=1800)
        if not output_file and diff_output_path.exists():
            output_file = str(diff_output_path)
        
        if process.returncode == 0:
            emit("✅ 版本号差分任务执行成功!\n")
            task_manager.set_result(task_id, output_file or '')
            
            # 上传文件到 NAS 并生成分享链接（在标记 SUCCESS 之前，確保前端能看到上传日志）
            if output_file and Path(output_file).exists():
                task_manager.update_progress(task_id, 95.0)
                emit("[INFO] 开始上传文件到 NAS...\n")
                sharing_url, upload_error = upload_to_nas_and_share(output_file, cfg, emit=emit)
                if not upload_error and sharing_url:
                    task_manager.set_result(task_id, output_file, sharing_url)
                    emit(f"[OK] 文件已上传到 NAS，分享链接: {sharing_url}\n")
            
            task_manager.update_task_status(task_id, TaskStatus.SUCCESS)
            task_manager.update_progress(task_id, 100.0)
            notify_task_result(
                task,
                emit,
                f"✅ 版本号差分任务完成\n项目: {project_name}\n{base_version} -> {target_version}",
                task.to_dict(),
            )
        else:
            raise RuntimeError(f"脚本执行失败 (返回码: {process.returncode})")
    
    except Exception as e:
        error_msg = str(e)
        emit(f"❌ 任务执行失败: {error_msg}\n")
        task_manager.set_result(task_id, '', '', error_msg)
        task_manager.update_task_status(task_id, TaskStatus.FAILED)
        notify_task_result(
            task,
            emit,
            f"❌ 版本号差分任务失败\n项目: {project_name}\n错误: {error_msg}",
        )
    finally:
        cleanup_runtime_artifacts(emit=emit)

def execute_nas_link_diff_task(task_id, old_url, new_url):
    """后台执行 NAS 链接差分任务。"""
    task = task_manager.get_task(task_id)
    if not task:
        return
    
    task_manager.update_task_status(task_id, TaskStatus.RUNNING)
    cfg = load_config()
    
    def emit(line: str):
        task_manager.append_log(task_id, line)
        try:
            sys.stdout.buffer.write(line.encode('utf-8', errors='replace'))
            sys.stdout.buffer.flush()
        except Exception:
            pass
    
    try:
        emit(f"[INFO] 任务 {task_id} 开始执行 NAS 链接差分\n")
        emit(f"[INFO] 通知目标: type={task.receive_id_type}, id={_mask_receiver_id(task.open_id) or 'N/A'}\n")
        
        if not HAS_GEN_DIFF:
            raise RuntimeError("ota_gen_diff 模块不可用")
        
        pem_path = cfg.get('pem')
        if not pem_path:
            raise RuntimeError("未配置 PEM 文件路径")
        
        # 推断项目名：先解析分享页里的真实文件名，再兜底到原始 URL
        project_name = infer_project_name_from_nas_urls(old_url, new_url, emit)
        
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path("work") / "ota_input" / project_name / f"nas_link_{ts}"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        old_dl_path = output_dir / "old" / "old_input.pkg"
        new_dl_path = output_dir / "new" / "new_input.pkg"
        old_dl_path.parent.mkdir(parents=True, exist_ok=True)
        new_dl_path.parent.mkdir(parents=True, exist_ok=True)
        
        nas_username = cfg.get('nas_username')
        nas_password = cfg.get('nas_password')
        nas_address = cfg.get('nas_address', '10.2.100.85:5001')
        verify_tls = bool(cfg.get('nas_verify_tls', False))
        auth = (nas_username, nas_password) if nas_username and nas_password else None
        
        emit("[INFO] 开始下载旧版本...\n")
        old_dl_path = download_with_progress(old_url, old_dl_path, emit, auth=auth, verify_tls=verify_tls,
                             nas_address=nas_address, nas_username=nas_username, nas_password=nas_password)
        task_manager.update_progress(task_id, 30.0)
        
        emit("[INFO] 开始下载新版本...\n")
        new_dl_path = download_with_progress(new_url, new_dl_path, emit, auth=auth, verify_tls=verify_tls,
                             nas_address=nas_address, nas_username=nas_username, nas_password=nas_password)
        task_manager.update_progress(task_id, 60.0)

        detected_project_name = infer_project_name_from_name(old_dl_path.name)
        if detected_project_name == 'naslink':
            detected_project_name = infer_project_name_from_name(new_dl_path.name)
        if detected_project_name != 'naslink' and detected_project_name != project_name:
            emit(f"[INFO] 已根据真实下载文件名识别项目名: {detected_project_name}\n")
            project_name = detected_project_name
        
        emit("[INFO] 提取旧版本 ota_sign.zip ...\n")
        old_ota_path = maybe_extract_ota_sign(old_dl_path, emit)
        
        emit("[INFO] 提取新版本 ota_sign.zip ...\n")
        new_ota_path = maybe_extract_ota_sign(new_dl_path, emit)

        extracted_project_name = infer_project_name_from_name(old_ota_path.name)
        if extracted_project_name == 'naslink':
            extracted_project_name = infer_project_name_from_name(new_ota_path.name)
        if extracted_project_name != 'naslink' and extracted_project_name != project_name:
            emit(f"[INFO] 已根据提取出的总包名称识别项目名: {extracted_project_name}\n")
            project_name = extracted_project_name
        
        old_ver = (parse_version_from_ota_zip(old_ota_path)
                   or parse_version_from_filename(old_ota_path.name, old_dl_path.name)
                   or parse_version_from_url(old_url) or 'old')
        new_ver = (parse_version_from_ota_zip(new_ota_path)
                   or parse_version_from_filename(new_ota_path.name, new_dl_path.name)
                   or parse_version_from_url(new_url) or 'new')
        diff_output_path = output_dir / f"{project_name}_diff_{old_ver}_to_{new_ver}.zip"
        
        # 记录项目信息用于统计
        task_manager.set_project_info(task_id, project_name=project_name, old_version=old_ver, new_version=new_ver)
        
        emit(f"[INFO] 开始生成差分 OTA 包...\n")
        result = gen_diff_archive(
            pem_path,
            str(old_ota_path),
            str(new_ota_path),
            str(diff_output_path),
            None,
        )
        task_manager.update_progress(task_id, 95.0)
        
        if result or diff_output_path.exists():
            emit("✅ NAS 链接差分 OTA 包生成成功!\n")
            emit(f"[INFO] 差分包位置: {diff_output_path}\n")
            task_manager.set_result(task_id, str(diff_output_path))
            
            # 上传文件到 NAS 并生成分享链接（在标记 SUCCESS 之前，确保前端能看到上传日志）
            emit("[INFO] 开始上传文件到 NAS...\n")
            sharing_url, upload_error = upload_to_nas_and_share(str(diff_output_path), cfg, emit=emit)
            if not upload_error and sharing_url:
                task_manager.set_result(task_id, str(diff_output_path), sharing_url)
                emit(f"[OK] 文件已上传到 NAS，分享链接: {sharing_url}\n")
            
            task_manager.update_task_status(task_id, TaskStatus.SUCCESS)
            notify_task_result(
                task,
                emit,
                "✅ NAS 链接差分任务完成",
                task.to_dict(),
            )
        else:
            raise RuntimeError("差分 OTA 包生成失败")
    
    except Exception as e:
        error_msg = str(e)
        emit(f"❌ 任务执行失败: {error_msg}\n")
        task_manager.set_result(task_id, '', '', error_msg)
        task_manager.update_task_status(task_id, TaskStatus.FAILED)
        notify_task_result(
            task,
            emit,
            f"❌ NAS 链接差分任务失败\n错误: {error_msg}",
        )
    finally:
        cleanup_runtime_artifacts(emit=emit)

def execute_local_upload_diff_task(task_id, old_file_path, new_file_path, project_name):
    """后台执行本地上传差分任务。"""
    task = task_manager.get_task(task_id)
    if not task:
        return
    
    task_manager.update_task_status(task_id, TaskStatus.RUNNING)
    cfg = load_config()
    
    def emit(line: str):
        task_manager.append_log(task_id, line)
        try:
            sys.stdout.buffer.write(line.encode('utf-8', errors='replace'))
            sys.stdout.buffer.flush()
        except Exception:
            pass
    
    try:
        emit(f"[INFO] 任务 {task_id} 开始执行本地上传差分\n")
        emit(f"[INFO] 通知目标: type={task.receive_id_type}, id={_mask_receiver_id(task.open_id) or 'N/A'}\n")
        
        if not HAS_GEN_DIFF:
            raise RuntimeError("ota_gen_diff 模块不可用")
        
        pem_path = cfg.get('pem')
        if not pem_path:
            raise RuntimeError("未配置 PEM 文件路径")

        if not project_name or project_name == 'localupload':
            detected_project_name = infer_project_name_from_local_upload_names(
                Path(old_file_path).name,
                Path(new_file_path).name,
            )
            if detected_project_name != 'localupload':
                project_name = detected_project_name
                emit(f"[INFO] 已根据本地上传文件名识别项目名: {project_name}\n")
        
        emit(f"[INFO] 项目名: {project_name}\n")
        task_manager.update_progress(task_id, 20.0)
        
        old_upload_path = Path(old_file_path)
        new_upload_path = Path(new_file_path)
        emit(f"[INFO] 本地上传原始文件: 旧包={old_upload_path.name}, 新包={new_upload_path.name}\n")
        
        emit("[INFO] 提取旧版本 ota_sign.zip ...\n")
        old_ota_path = maybe_extract_ota_sign(old_upload_path, emit)
        task_manager.update_progress(task_id, 40.0)
        emit(f"[INFO] 旧版本 ota_sign 已定位: {old_ota_path.name}\n")
        
        emit("[INFO] 提取新版本 ota_sign.zip ...\n")
        new_ota_path = maybe_extract_ota_sign(new_upload_path, emit)
        task_manager.update_progress(task_id, 60.0)
        emit(f"[INFO] 新版本 ota_sign 已定位: {new_ota_path.name}\n")
        
        old_ver = (parse_version_from_ota_zip(old_ota_path)
                   or parse_version_from_filename(old_ota_path.name, old_upload_path.name) or 'old')
        new_ver = (parse_version_from_ota_zip(new_ota_path)
                   or parse_version_from_filename(new_ota_path.name, new_upload_path.name) or 'new')
        emit(f"[INFO] 解析到版本号: {old_ver} -> {new_ver}\n")
        
        # 记录项目信息用于统计
        task_manager.set_project_info(task_id, project_name=project_name, old_version=old_ver, new_version=new_ver)
        
        output_dir = old_upload_path.parent
        diff_output_path = output_dir / f"{project_name}_diff_{old_ver}_to_{new_ver}.zip"
        
        emit(f"[INFO] 开始生成差分 OTA 包...\n")
        result = gen_diff_archive(
            pem_path,
            str(old_ota_path),
            str(new_ota_path),
            str(diff_output_path),
            None,
        )
        task_manager.update_progress(task_id, 95.0)
        
        if result or diff_output_path.exists():
            emit("✅ 本地文件差分 OTA 包生成成功!\n")
            emit(f"[INFO] 差分包位置: {diff_output_path}\n")
            task_manager.set_result(task_id, str(diff_output_path))
            
            # 上传文件到 NAS 并生成分享链接（在标记 SUCCESS 之前，确保前端能看到上传日志）
            emit("[INFO] 开始上传文件到 NAS...\n")
            sharing_url, upload_error = upload_to_nas_and_share(str(diff_output_path), cfg, emit=emit)
            if not upload_error and sharing_url:
                task_manager.set_result(task_id, str(diff_output_path), sharing_url)
                emit(f"[OK] 文件已上传到 NAS，分享链接: {sharing_url}\n")
            
            task_manager.update_task_status(task_id, TaskStatus.SUCCESS)
            notify_task_result(
                task,
                emit,
                "✅ 本地上传差分任务完成",
                task.to_dict(),
            )
        else:
            raise RuntimeError("差分 OTA 包生成失败")
    
    except Exception as e:
        error_msg = str(e)
        emit(f"❌ 任务执行失败: {error_msg}\n")
        task_manager.set_result(task_id, '', '', error_msg)
        task_manager.update_task_status(task_id, TaskStatus.FAILED)
        notify_task_result(
            task,
            emit,
            f"❌ 本地上传差分任务失败\n错误: {error_msg}",
        )
    finally:
        cleanup_runtime_artifacts(emit=emit)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/debug')
def debug():
    cfg = {}
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    feishu_sdk_urls = []
    if isinstance(cfg, dict) and cfg.get('feishu_sdk_local_path'):
        feishu_sdk_urls.append(cfg.get('feishu_sdk_local_path'))
    feishu_sdk_urls.append(url_for('static', filename='feishu-h5-js-sdk.js'))
    feishu_sdk_urls.extend([
        'https://lf1-cdn-tos.bytegoofy.com/goofy/union-fe/feishu/h5-js-sdk.js',
        'https://lf26-cdn-tos.bytescm.com/obj/static/lark-op/h5-js-sdk-1.0.0.js',
    ])
    return render_template('feishu_test.html', feishu_sdk_urls=feishu_sdk_urls)


@app.route('/feishu-test')
def feishu_test_page():
    cfg = {}
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    feishu_sdk_urls = []
    if isinstance(cfg, dict) and cfg.get('feishu_sdk_local_path'):
        feishu_sdk_urls.append(cfg.get('feishu_sdk_local_path'))
    feishu_sdk_urls.append(url_for('static', filename='feishu-h5-js-sdk.js'))
    feishu_sdk_urls.extend([
        'https://lf1-cdn-tos.bytegoofy.com/goofy/union-fe/feishu/h5-js-sdk.js',
        'https://lf26-cdn-tos.bytescm.com/obj/static/lark-op/h5-js-sdk-1.0.0.js',
    ])
    return render_template('feishu_test.html', feishu_sdk_urls=feishu_sdk_urls)

@app.route('/api/feishu/config', methods=['GET'])
def feishu_config():
    """返回前端获取飞书容器授权码所需的公开配置。"""
    try:
        # 确保读取本地配置后 APP_ID 已更新
        cfg = load_config()
    except Exception:
        cfg = {}
    enabled = bool(APP_ID and APP_SECRET and APP_ID != '这里贴你的App ID')
    return jsonify({
        'success': True,
        'enabled': enabled,
        'send_message_enabled': bool(FEISHU_SEND_MESSAGE_ENABLED),
        'app_id': APP_ID if enabled else '',
        'sdk_local_path': (cfg.get('feishu_sdk_local_path') or '') if isinstance(cfg, dict) else '',
        'default_receive_id': (cfg.get('feishu_default_receive_id') or '') if isinstance(cfg, dict) else '',
        'default_receive_id_type': (cfg.get('feishu_default_receive_id_type') or 'open_id') if isinstance(cfg, dict) else 'open_id',
    })

@app.route('/api/feishu/auth-config', methods=['POST'])
def feishu_auth_config():
    """生成飞书 JSSDK 鉴权签名（h5sdk.config 所需参数）。
    
    请求体: {"url": "当前页面完整URL（不含#后内容）"}
    返回: {success, data: {appId, timestamp, nonceStr, signature}}
    """
    try:
        payload = request.get_json(silent=True) or {}
        page_url = (payload.get('url') or '').strip()
        if not page_url:
            return jsonify({'success': False, 'msg': '缺少 url 参数'}), 400

        if not APP_ID or not APP_SECRET:
            return jsonify({'success': False, 'msg': '缺少飞书应用配置'}), 400

        # 1. 获取 tenant_access_token
        token, token_error = get_feishu_tenant_access_token()
        if not token:
            return jsonify({'success': False, 'msg': f'获取 tenant_access_token 失败: {token_error}'}), 500

        # 2. 用 tenant_access_token 获取 jsapi_ticket
        try:
            ticket_resp = requests.get(
                'https://open.feishu.cn/open-apis/jssdk/ticket/get',
                headers={'Authorization': f'Bearer {token}'},
                timeout=10,
            )
            ticket_data = ticket_resp.json()
            if ticket_data.get('code') != 0:
                return jsonify({'success': False, 'msg': f'获取 jsapi_ticket 失败: {ticket_data.get("msg")}'}), 500
            jsapi_ticket = ticket_data.get('data', {}).get('ticket', '')
            if not jsapi_ticket:
                return jsonify({'success': False, 'msg': 'jsapi_ticket 为空'}), 500
        except Exception as e:
            return jsonify({'success': False, 'msg': f'请求 jsapi_ticket 异常: {str(e)}'}), 500

        # 3. 生成签名
        nonce_str = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        timestamp = int(time.time())  # 飞书 JSSDK 要求整数时间戳
        sign_str = f'jsapi_ticket={jsapi_ticket}&noncestr={nonce_str}&timestamp={timestamp}&url={page_url}'
        signature = hashlib.sha1(sign_str.encode('utf-8')).hexdigest()

        app.logger.info(f'[feishu-auth] sign_str: {sign_str[:80]}...')
        app.logger.info(f'[feishu-auth] signature: {signature}')
        app.logger.info(f'[feishu-auth] ticket prefix: {jsapi_ticket[:20]}...')

        return jsonify({
            'success': True,
            'data': {
                'appId': APP_ID,
                'timestamp': timestamp,  # 返回整数
                'nonceStr': nonce_str,
                'signature': signature,
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)}), 500

@app.route('/api/feishu/oauth-url', methods=['GET'])
def feishu_oauth_url():
    """生成飞书 OAuth 网页授权 URL，用于非容器环境或桥接不可用时获取用户身份。
    
    用户在飞书内点击此链接 → 飞书授权页 → 回调到当前页面带上 code 参数
    """
    try:
        if not APP_ID or not APP_SECRET:
            return jsonify({'success': False, 'msg': '缺少飞书应用配置'}), 400

        # 从 Referer 或查询参数推断回调地址
        redirect_uri = request.args.get('redirect_uri', '').strip()
        if not redirect_uri:
            redirect_uri = request.headers.get('Referer', '')
        if not redirect_uri:
            redirect_uri = request.host_url.rstrip('/') + '/feishu-test'

        # 飞书 OAuth 授权页 URL
        auth_url = (
            f'https://open.feishu.cn/open-apis/authen/v1/authorize'
            f'?app_id={APP_ID}'
            f'&redirect_uri={requests.utils.quote(redirect_uri, safe="")}'
        )

        return jsonify({
            'success': True,
            'data': {
                'auth_url': auth_url,
                'redirect_uri': redirect_uri,
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)}), 500

@app.route('/api/feishu/login', methods=['POST'])
@app.route('/api/feishu/code2openid', methods=['POST'])
def feishu_login():
    """通过飞书授权码换取用户标识。
    在飞书容器内调用 tt.requestAuthCode 后，将 code 传到此接口即可自动获取 open_id / user_id。
    """
    try:
        payload = request.get_json(silent=True) or {}
        code = (payload.get('code') or payload.get('auth_code') or '').strip()
        if not code:
            return jsonify({'success': False, 'msg': '缺少授权 code'}), 400

        auth_info, error = _exchange_feishu_login_code(code)
        if not auth_info:
            return jsonify({'success': False, 'msg': error}), 400

        return jsonify({
            'success': True,
            'open_id': auth_info.get('open_id', ''),
            'user_id': auth_info.get('user_id', ''),
            'union_id': auth_info.get('union_id', ''),
            'receive_id': auth_info.get('receive_id', ''),
            'receive_id_type': auth_info.get('receive_id_type', 'open_id'),
        })
    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)}), 500


@app.route('/api/feishu/test', methods=['POST'])
def feishu_test_send():
    """向当前飞书用户发送一条测试通知。"""
    try:
        cfg = load_config()
        payload = request.get_json(silent=True) or {}
        form_data = request.form or {}

        receive_id = (
            payload.get('receive_id')
            or payload.get('open_id')
            or form_data.get('receive_id')
            or form_data.get('open_id')
            or ''
        ).strip()
        receive_id_type = (
            payload.get('receive_id_type')
            or form_data.get('receive_id_type')
            or 'open_id'
        ).strip() or 'open_id'
        message = (
            payload.get('message')
            or payload.get('text')
            or form_data.get('message')
            or form_data.get('text')
            or ''
        ).strip()

        if not message:
            message = f"飞书通知测试\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n来源: 版本差分包工具"

        if not receive_id and isinstance(cfg, dict):
            fallback_id = (cfg.get('feishu_default_receive_id') or '').strip()
            fallback_type = (cfg.get('feishu_default_receive_id_type') or '').strip()
            if fallback_id:
                receive_id = fallback_id
                receive_id_type = fallback_type or receive_id_type or 'open_id'

        if not receive_id:
            return jsonify({
                'success': False,
                'msg': '缺少飞书用户标识，请先在飞书容器内获取身份，或在配置中设置 feishu_default_receive_id',
            }), 400

        ok, reason = send_feishu_message(receive_id, message, None, receive_id_type)
        if not ok:
            return jsonify({
                'success': False,
                'msg': reason,
                'receive_id': receive_id,
                'receive_id_type': receive_id_type,
            }), 400

        return jsonify({
            'success': True,
            'msg': '测试消息发送成功',
            'receive_id': receive_id,
            'receive_id_type': receive_id_type,
            'message': message,
        })
    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)}), 500

# ========== 异步任务 API ==========

@app.route('/api/tasks', methods=['POST'])
def create_task():
    """创建异步差分任务。
    
    请求示例:
    {
        "mode": "version",  // "version", "nas_link", "local_upload"
        "open_id": "用户标识（可选，用于飞书消息回复）",
        "receive_id_type": "open_id 或 user_id（可选，默认 open_id）",
        "projectName": "pike",  // 仅 version 模式
        "baseVersion": "2.2.0.1",
        "targetVersion": "2.3.0.1",
        "releaseType": "release"
    }
    
    或用 FormData（local_upload）:
    - oldLocalFile: File
    - newLocalFile: File
    - projectName: string
    - open_id: string
    - receive_id_type: string

    若请求未提供用户标识，可在 ota_local_config.json 配置：
    - feishu_default_receive_id
    - feishu_default_receive_id_type (open_id 或 user_id)
    """
    try:
        cfg = load_config()
        data = request.get_json(silent=True) or {}
        form_data = request.form or {}
        # 兼容 JSON 与 multipart/form-data 两种提交方式
        mode = (data.get('mode') or form_data.get('mode') or 'version').strip()
        open_id = (data.get('open_id') or data.get('receive_id') or form_data.get('open_id') or form_data.get('receive_id') or '').strip()
        receive_id_type = (data.get('receive_id_type') or form_data.get('receive_id_type') or 'open_id').strip() or 'open_id'

        # 前端未拿到飞书身份时，允许后端用本地配置兜底接收人，保证真实用户可收到通知
        if not open_id:
            fallback_id = (cfg.get('feishu_default_receive_id') or '').strip() if isinstance(cfg, dict) else ''
            fallback_type = (cfg.get('feishu_default_receive_id_type') or '').strip() if isinstance(cfg, dict) else ''
            if fallback_id:
                open_id = fallback_id
                receive_id_type = fallback_type or receive_id_type or 'open_id'

        merged_data = dict(data)
        if open_id:
            merged_data['open_id'] = open_id
        merged_data['receive_id_type'] = receive_id_type
        
        # 创建任务
        task = task_manager.create_task(mode, merged_data)
        
        if mode == 'version':
            # 版本号差分
            project_name = data.get('projectName', '').strip()
            base_version = data.get('baseVersion', '').strip()
            target_version = data.get('targetVersion', '').strip()
            release_type = data.get('releaseType', '')
            
            if not all([project_name, base_version, target_version, release_type]):
                return jsonify({"success": False, "error": "缺少必要字段"}), 400
            
            # 后台执行
            task_manager.executor.submit(
                execute_version_diff_task,
                task.task_id, project_name, base_version, target_version, release_type
            )
        
        elif mode == 'nas_link':
            # NAS 链接差分
            old_url = data.get('oldNasUrl', '').strip()
            new_url = data.get('newNasUrl', '').strip()
            
            if not all([old_url, new_url]):
                return jsonify({"success": False, "error": "缺少必要字段"}), 400
            
            task_manager.executor.submit(
                execute_nas_link_diff_task,
                task.task_id, old_url, new_url
            )
        
        elif mode == 'local_upload':
            # 本地文件上传
            old_file = request.files.get('oldLocalFile')
            new_file = request.files.get('newLocalFile')
            project_name = ''
            
            if old_file is None or new_file is None:
                return jsonify({"success": False, "error": "缺少文件"}), 400

            project_name = infer_project_name_from_local_upload_names(
                old_file.filename,
                new_file.filename,
            )
            
            # 保存文件
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            upload_dir = Path("work") / "uploads" / task.task_id
            upload_dir.mkdir(parents=True, exist_ok=True)
            
            def _safe_name(filename, fallback):
                n = Path(filename or '').name.strip()
                n = re.sub(r'[^A-Za-z0-9._@\-]', '_', n)
                return n or fallback
            old_path = upload_dir / f"old_{_safe_name(old_file.filename, 'upload.pkg')}"
            new_path = upload_dir / f"new_{_safe_name(new_file.filename, 'upload.pkg')}"
            old_file.save(str(old_path))
            new_file.save(str(new_path))
            
            task_manager.executor.submit(
                execute_local_upload_diff_task,
                task.task_id, str(old_path), str(new_path), project_name
            )
        
        else:
            return jsonify({"success": False, "error": "不支持的模式"}), 400
        
        return jsonify({
            "success": True,
            "task_id": task.task_id,
            "status": task.status.value
        })
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/tasks/<task_id>', methods=['GET'])
def get_task_status(task_id):
    """查询任务状态。"""
    task = task_manager.get_task(task_id)
    if not task:
        return jsonify({"success": False, "error": "任务不存在"}), 404
    
    return jsonify({
        "success": True,
        "task": task.to_dict()
    })

# ========== 统计页面 ==========

@app.route('/stats')
def stats_page():
    """差分项目统计页面。"""
    return render_template('stats.html')

@app.route('/api/stats')
def api_stats():
    """返回差分任务统计数据。
    
    聚合内存中所有任务记录，提供：
    - 总览：总任务数、成功/失败数、成功率
    - 项目排行：各项目差分次数、成功次数
    - 模式分布：version/nas_link/local_upload 的占比
    - 最近任务列表：最近 20 条任务记录
    """
    with task_manager.lock:
        all_tasks = list(task_manager.tasks.values())
    
    total = len(all_tasks)
    succeeded = sum(1 for t in all_tasks if t.status == TaskStatus.SUCCESS)
    failed = sum(1 for t in all_tasks if t.status == TaskStatus.FAILED)
    cancelled = sum(1 for t in all_tasks if t.status == TaskStatus.CANCELLED)
    running = sum(1 for t in all_tasks if t.status == TaskStatus.RUNNING) 
    
    success_rate = round(succeeded / total * 100, 1) if total > 0 else 0
    
    # 项目使用统计
    project_stats = {}
    for t in all_tasks:
        if t.status != TaskStatus.SUCCESS:
            continue
        pn = t.project_name.strip()
        if not pn:
            # 尝试从日志中解析项目名
            for log_line in t.logs:
                m = re.search(r'项目名[：:]\s*(\S+)', log_line)
                if m:
                    pn = m.group(1)
                    break
                m = re.search(r'识别项目名[：:]\s*(\S+)', log_line)
                if m:
                    pn = m.group(1)
                    break
        if not pn:
            pn = '未识别'
        if pn not in project_stats:
            project_stats[pn] = {'count': 0, 'versions': set()}
        project_stats[pn]['count'] += 1
        ver_label = ''
        if t.old_version and t.new_version:
            ver_label = f'{t.old_version} → {t.new_version}'
        elif t.new_version:
            ver_label = t.new_version
        if ver_label:
            project_stats[pn]['versions'].add(ver_label)
    
    # 转换为可序列化格式
    project_ranking = []
    for pn, info in sorted(project_stats.items(), key=lambda x: -x[1]['count']):
        project_ranking.append({
            'name': pn,
            'count': info['count'],
            'latest_versions': sorted(info['versions'])[-5:],
        })
    
    # 模式分布
    mode_dist = {}
    for t in all_tasks:
        mode_label = {'version': '版本号差分', 'nas_link': 'NAS链接差分', 'local_upload': '本地文件差分'}.get(t.mode, t.mode)
        mode_dist[mode_label] = mode_dist.get(mode_label, 0) + 1
    
    # 最近任务列表
    recent_tasks = []
    sorted_tasks = sorted(all_tasks, key=lambda t: t.created_at, reverse=True)[:20]
    for t in sorted_tasks:
        pn = t.project_name or '—'
        recent_tasks.append({
            'task_id': t.task_id,
            'mode': t.mode,
            'mode_label': {'version': '版本号', 'nas_link': 'NAS链接', 'local_upload': '本地上传'}.get(t.mode, t.mode),
            'project_name': pn,
            'old_version': t.old_version or '—',
            'new_version': t.new_version or '—',
            'status': t.status.value,
            'status_label': {
                'pending': '等待中', 'running': '执行中', 'success': '成功', 'failed': '失败', 'cancelled': '已取消'
            }.get(t.status.value, t.status.value),
            'created_at': t.created_at,
            'open_id': t.open_id[:8] + '...' if t.open_id else '—',
        })
    
    return jsonify({
        'success': True,
        'data': {
            'overview': {
                'total': total,
                'succeeded': succeeded,
                'failed': failed,
                'cancelled': cancelled,
                'running': running,
                'success_rate': success_rate,
            },
            'project_ranking': project_ranking,
            'mode_distribution': [{'name': k, 'count': v} for k, v in mode_dist.items()],
            'recent_tasks': recent_tasks,
        }
    })

@app.route('/generate', methods=['POST'])
def generate():
    def generate_response():
        log_lines = []

        def emit(line: str):
            log_lines.append(line)
            # 同步输出到服务端控制台，便于实时调试。
            try:
                # 使用 sys.stdout.buffer 确保 UTF-8 正确输出（解决中文乱码）
                sys.stdout.buffer.write(line.encode('utf-8', errors='replace'))
                sys.stdout.buffer.flush()
            except Exception:
                # 备选方案：直接 print
                try:
                    print(line, end='', flush=True)
                except Exception:
                    pass
            return line

        project_name = ""
        base_version = ""
        target_version = ""
        release_type = ""

        try:
            d = request.json
            if not d:
                yield emit("❌ 提交失败：未收到数据\n")
                return
            mode = d.get('mode') or 'version'
            cfg = load_config()

            if mode == 'version':
                project_name = d.get('projectName') or ""
                base_version = d.get('baseVersion') or ""
                target_version = d.get('targetVersion') or ""
                release_type = d.get('releaseType') or ""

                if not all([project_name, base_version, target_version, release_type]):
                    yield emit("❌ 提交失败：缺少必要字段\n")
                    return

                time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                type_text = "Release" if release_type == "release" else "Monkey"
                text = (
                    f"模式：版本号差分\n"
                    f"项目名称：{project_name}\n"
                    f"基础版本：{base_version}\n"
                    f"目标版本：{target_version}\n"
                    f"发布类型：{type_text}\n"
                    f"生成时间：{time}\n"
                )
                path = f"generated_files/配置_{project_name}_{target_version}.txt"
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text)
                yield emit(f"[INFO] 配置文件已生成: {path}\n")
                yield emit("[INFO] 开始执行版本号差分流程...\n\n")

                script_path = "prepare_ota_sign_from_nas.py"
                if not os.path.exists(script_path):
                    yield emit(f"❌ 脚本文件不存在: {script_path}\n")
                    return

                try:
                    # 解析项目名和变体（如 "milan_64m" -> project="milan", variant_dir="64m"）
                    project_base = project_name
                    variant_dir = ""
                    if "_" in project_name:
                        parts = project_name.split("_", 1)
                        project_base = parts[0]
                        variant_dir = parts[1]

                    cmd = [
                        'python', '-u', script_path,
                        '--project', project_base,
                        '--from-ver', base_version,
                        '--to-ver', target_version,
                        '--type', release_type,
                        '--no-open-tool',
                        '--debug',
                    ]
                    
                    if variant_dir:
                        cmd.extend(['--variant-dir', variant_dir])

                    env = os.environ.copy()
                    env['PYTHONUNBUFFERED'] = '1'
                    env['PYTHONIOENCODING'] = 'utf-8'  # 确保子进程使用 UTF-8 编码

                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        encoding='utf-8',
                        errors='ignore',  # 改用 ignore，避免乱码显示
                        env=env
                    )

                    for line in process.stdout:
                        if '[INFO] 下载进度:' in line:
                            continue
                        yield emit(line)

                    process.wait(timeout=1800)
                    if process.returncode == 0:
                        yield emit("\n✅ 版本号差分流程执行成功！\n")
                    else:
                        yield emit(f"\n❌ 脚本执行失败 (返回码: {process.returncode})\n")

                except subprocess.TimeoutExpired:
                    process.kill()
                    yield emit("❌ 脚本执行超时（超过 30 分钟）\n")
                except Exception as e:
                    yield emit(f"❌ 执行脚本时出错: {str(e)}\n")
                    import traceback
                    yield emit(traceback.format_exc())

            elif mode == 'nas_link':
                old_nas_url = (d.get('oldNasUrl') or "").strip()
                new_nas_url = (d.get('newNasUrl') or "").strip()
                release_type = 'nas_link'
                base_version = 'link_old'
                target_version = 'link_new'

                project_name = infer_project_name_from_nas_urls(old_nas_url, new_nas_url, emit)

                if not all([old_nas_url, new_nas_url]):
                    yield emit("❌ 提交失败：NAS 链接差分缺少必要字段\n")
                    return

                if not HAS_GEN_DIFF:
                    yield emit("❌ ota_gen_diff 模块不可用，无法生成差分 OTA 包\n")
                    return

                pem_path = cfg.get('pem')
                if not pem_path:
                    yield emit("❌ 未配置 PEM 文件路径，请在 ota_local_config.json 中配置 'pem'\n")
                    return

                yield emit("[INFO] 开始执行 NAS 链接差分流程...\n")

                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_dir = Path("work") / "ota_input" / project_name / f"nas_link_{ts}"
                output_dir.mkdir(parents=True, exist_ok=True)

                old_dl_path = output_dir / "old" / "old_input.pkg"
                new_dl_path = output_dir / "new" / "new_input.pkg"
                old_dl_path.parent.mkdir(parents=True, exist_ok=True)
                new_dl_path.parent.mkdir(parents=True, exist_ok=True)

                nas_username = cfg.get('nas_username')
                nas_password = cfg.get('nas_password')
                nas_address = cfg.get('nas_address', '10.2.100.85:5001')
                verify_tls = bool(cfg.get('nas_verify_tls', False))
                auth = (nas_username, nas_password) if nas_username and nas_password else None

                try:
                    old_dl_path = download_with_progress(old_nas_url, old_dl_path, emit, auth=auth, verify_tls=verify_tls, 
                                         nas_address=nas_address, nas_username=nas_username, nas_password=nas_password)
                    new_dl_path = download_with_progress(new_nas_url, new_dl_path, emit, auth=auth, verify_tls=verify_tls,
                                         nas_address=nas_address, nas_username=nas_username, nas_password=nas_password)
                except Exception as e:
                    yield emit(f"❌ 下载 NAS 链接失败: {e}\n")
                    return

                detected_project_name = infer_project_name_from_name(old_dl_path.name)
                if detected_project_name == 'naslink':
                    detected_project_name = infer_project_name_from_name(new_dl_path.name)
                if detected_project_name != 'naslink' and detected_project_name != project_name:
                    yield emit(f"[INFO] 已根据真实下载文件名识别项目名: {detected_project_name}\n")
                    project_name = detected_project_name

                try:
                    yield emit("[INFO] 提取旧版本 ota_sign.zip ...\n")
                    old_ota_path = maybe_extract_ota_sign(old_dl_path, emit)
                    yield emit(f"[INFO] 旧版本 ota_sign 路径: {old_ota_path}\n")

                    yield emit("[INFO] 提取新版本 ota_sign.zip ...\n")
                    new_ota_path = maybe_extract_ota_sign(new_dl_path, emit)
                    yield emit(f"[INFO] 新版本 ota_sign 路径: {new_ota_path}\n")

                    extracted_project_name = infer_project_name_from_name(old_ota_path.name)
                    if extracted_project_name == 'naslink':
                        extracted_project_name = infer_project_name_from_name(new_ota_path.name)
                    if extracted_project_name != 'naslink' and extracted_project_name != project_name:
                        yield emit(f"[INFO] 已根据提取出的总包名称识别项目名: {extracted_project_name}\n")
                        project_name = extracted_project_name
                except Exception as e:
                    yield emit(f"❌ 从总包提取 ota_sign 失败: {e}\n")
                    return

                old_ver = (parse_version_from_ota_zip(old_ota_path)
                           or parse_version_from_filename(old_ota_path.name, old_dl_path.name)
                           or parse_version_from_url(old_nas_url) or 'old')
                new_ver = (parse_version_from_ota_zip(new_ota_path)
                           or parse_version_from_filename(new_ota_path.name, new_dl_path.name)
                           or parse_version_from_url(new_nas_url) or 'new')
                diff_output_path = output_dir / f"{project_name}_diff_{old_ver}_to_{new_ver}.zip"

                yield emit(f"[INFO] 开始生成差分 OTA 包: {diff_output_path}\n")
                try:
                    result = gen_diff_archive(
                        pem_path,
                        str(old_ota_path),
                        str(new_ota_path),
                        str(diff_output_path),
                        None,
                    )
                    if result:
                        yield emit("✅ NAS 链接差分 OTA 包生成成功!\n")
                        yield emit(f"[INFO] 差分包位置: {diff_output_path}\n")
                    else:
                        if diff_output_path.exists():
                            yield emit("⚠️ 差分包已生成，但校验存在告警（不影响产物输出）\n")
                            yield emit(f"[INFO] 差分包位置: {diff_output_path}\n")
                        else:
                            yield emit("❌ 差分 OTA 包生成失败，请检查日志\n")
                except Exception as e:
                    yield emit(f"❌ 生成差分 OTA 包时出错: {str(e)}\n")
                    import traceback
                    yield emit(traceback.format_exc())

            else:
                yield emit(f"❌ 不支持的模式: {mode}\n")
                return
                
        except Exception as e:
            yield emit(f"❌ 处理请求时出错：{str(e)}\n")
            import traceback
            yield emit(traceback.format_exc())

    return Response(stream_with_context(generate_response()), 
                   mimetype='text/plain; charset=utf-8')


@app.route('/generate_local', methods=['POST'])
def generate_local():
    def generate_response():
        def emit(line: str):
            try:
                sys.stdout.buffer.write(line.encode('utf-8', errors='replace'))
                sys.stdout.buffer.flush()
            except Exception:
                try:
                    print(line, end='', flush=True)
                except Exception:
                    pass
            return line

        try:
            cfg = load_config()
            if not HAS_GEN_DIFF:
                yield emit("❌ ota_gen_diff 模块不可用，无法生成差分 OTA 包\n")
                return

            pem_path = cfg.get('pem')
            if not pem_path:
                yield emit("❌ 未配置 PEM 文件路径，请在 ota_local_config.json 中配置 'pem'\n")
                return

            old_file = request.files.get('oldLocalFile')
            new_file = request.files.get('newLocalFile')
            project_name = infer_project_name_from_local_upload_names(
                old_file.filename if old_file else '',
                new_file.filename if new_file else '',
            )

            if old_file is None or new_file is None:
                yield emit("❌ 提交失败：请上传旧版本和新版本 ota_sign.zip\n")
                return

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = Path("work") / "ota_input" / project_name / f"local_upload_{ts}"
            output_dir.mkdir(parents=True, exist_ok=True)

            def _safe_upload_name(name: str, fallback: str) -> str:
                n = Path(name or "").name.strip()
                if not n:
                    return fallback
                # 只保留安全字符，避免路径或特殊字符问题
                n = re.sub(r"[^A-Za-z0-9._@\-]", "_", n)
                return n or fallback

            old_name = _safe_upload_name(old_file.filename if old_file else "", "old_upload.pkg")
            new_name = _safe_upload_name(new_file.filename if new_file else "", "new_upload.pkg")

            old_upload_path = output_dir / old_name
            new_upload_path = output_dir / new_name

            old_file.save(str(old_upload_path))
            new_file.save(str(new_upload_path))
            yield emit(f"[INFO] 已接收旧版本文件: {old_file.filename or old_upload_path.name}\n")
            yield emit(f"[INFO] 已接收新版本文件: {new_file.filename or new_upload_path.name}\n")
            yield emit("[INFO] 将自动解析输入文件；若为 archive_*.zab/zip/tgz 总包，会提取其中 watch@<chip>_ota_sign.zip 参与差分\n")

            # 统一复用提取逻辑：即便用户传了总包也可尝试提取到 ota_sign.zip
            yield emit("[INFO] 提取旧版本 ota_sign.zip ...\n")
            old_ota_path = maybe_extract_ota_sign(old_upload_path, emit)
            yield emit(f"[INFO] 旧版本 ota_sign 路径: {old_ota_path}\n")

            yield emit("[INFO] 提取新版本 ota_sign.zip ...\n")
            new_ota_path = maybe_extract_ota_sign(new_upload_path, emit)
            yield emit(f"[INFO] 新版本 ota_sign 路径: {new_ota_path}\n")

            old_ver = (parse_version_from_ota_zip(old_ota_path)
                       or parse_version_from_filename(old_ota_path.name, old_upload_path.name) or 'old')
            new_ver = (parse_version_from_ota_zip(new_ota_path)
                       or parse_version_from_filename(new_ota_path.name, new_upload_path.name) or 'new')
            diff_output_path = output_dir / f"{project_name}_diff_{old_ver}_to_{new_ver}.zip"

            yield emit(f"[INFO] 开始生成差分 OTA 包: {diff_output_path}\n")
            result = gen_diff_archive(
                pem_path,
                str(old_ota_path),
                str(new_ota_path),
                str(diff_output_path),
                None,
            )

            if result:
                yield emit("✅ 本地文件差分 OTA 包生成成功!\n")
                yield emit(f"[INFO] 差分包位置: {diff_output_path}\n")
            else:
                if diff_output_path.exists():
                    yield emit("⚠️ 差分包已生成，但校验存在告警（不影响产物输出）\n")
                    yield emit(f"[INFO] 差分包位置: {diff_output_path}\n")
                else:
                    yield emit("❌ 差分 OTA 包生成失败，请检查日志\n")
        except Exception as e:
            yield emit(f"❌ 处理本地上传请求时出错：{str(e)}\n")
            import traceback
            yield emit(traceback.format_exc())

    return Response(stream_with_context(generate_response()),
                    mimetype='text/plain; charset=utf-8')


@app.route('/inspect_local_zip_version', methods=['POST'])
def inspect_local_zip_version():
    upload = request.files.get('file')
    if upload is None:
        return jsonify({"success": False, "message": "未收到文件"}), 400

    try:
        with tempfile.TemporaryDirectory(prefix='ota_version_probe_') as temp_dir:
            temp_root = Path(temp_dir)
            raw_name = Path(upload.filename or 'upload.pkg').name
            safe_name = re.sub(r"[^A-Za-z0-9._@\-]", "_", raw_name) or 'upload.pkg'
            src_path = temp_root / safe_name
            upload.save(str(src_path))

            # 允许直接上传 ota_sign.zip，也允许上传 archive_*.zab/tgz/zip 总包。
            try:
                ota_path = maybe_extract_ota_sign(src_path, lambda _line: None)
            except Exception:
                ota_path = src_path

            version = parse_version_from_ota_zip(ota_path)
            if version:
                return jsonify({"success": True, "version": version})
            return jsonify({"success": False, "message": "未解析到版本号"})
    except Exception as e:
        return jsonify({"success": False, "message": f"解析失败: {e}"}), 500

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    # 修改后（仅监听本地，不冲突）
    app.run(host='127.0.0.1', port=5000)