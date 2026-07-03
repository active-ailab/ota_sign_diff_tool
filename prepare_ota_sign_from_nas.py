#!/usr/bin/env python3
"""
从 NAS 自动准备 OTA 差分所需的 watch@<chip>_ota_sign.zip

特性：
- 按项目名 + 版本号，在 WebDAV NAS 上自动定位两个版本目录
- 支持 Release / Monkey 两种包结构
- Monkey 支持「两层 archive_xxx.tgz -> archive_OTA_CLOUD_xxx.tgz -> ota_sign.zip」
- 可选 variant 目录（例如 pamir_64m / pamir_32m），通过命令行参数配置
- 下载 / 解压完成后，把两个 ota_sign.zip 统一放到一个输出目录，并可自动打开资源管理器和本地 OTA 升级助手
"""

import argparse
import os
import sys
import tarfile
import zipfile
import shutil
import tempfile
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import requests
import urllib3
from xml.etree import ElementTree as ET
from urllib.parse import quote, unquote, urlparse
import re

# 关闭 NAS 自签名证书的告警
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    # 直接复用 ota_gen_diff.py 里的核心差分逻辑
    from ota_gen_diff import gen_diff_archive, get_fw_version  # type: ignore[import]
except Exception as e:
    gen_diff_archive = None
    get_fw_version = None
    import sys
    if "ota_diff_apply" in str(e):
        print(f"[WARN] 提示: 缺少 ota_diff_apply.py 文件，无法生成差分 OTA 包", file=sys.stderr)
    elif not isinstance(e, ModuleNotFoundError) or "ota_gen_diff" in str(e):
        print(f"[WARN] 导入 ota_gen_diff 失败: {e}", file=sys.stderr)


# 本地可选配置文件（开发者各自配置 pem 路径 / 是否自动打开 GUI 等）
LOCAL_CFG_PATH = Path(__file__).with_name("ota_local_config.json")
OUTPUT_META_NAME = ".ota_sign_meta.json"


def load_local_config() -> dict:
    if LOCAL_CFG_PATH.exists():
        try:
            with open(LOCAL_CFG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] 读取本地配置 {LOCAL_CFG_PATH} 失败: {e}")
    return {}


def load_output_meta(output_dir: Path) -> dict:
    meta_path = output_dir / OUTPUT_META_NAME
    if not meta_path.exists():
        return {}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[WARN] 读取版本元数据失败: {meta_path} - {e}")
        return {}


def save_output_meta(output_dir: Path, data: dict) -> None:
    meta_path = output_dir / OUTPUT_META_NAME
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] 写入版本元数据失败: {meta_path} - {e}")


def infer_version_from_ota_zip(ota_zip_path: Path) -> Optional[str]:
    """从 ota_sign.zip 内部 META/firmware_sign.bin 尝试提取实际固件版本。"""
    # 优先使用 META/fw_info，通常能直接读到如 2.3.1.1 的版本号
    try:
        with zipfile.ZipFile(str(ota_zip_path), "r", zipfile.ZIP_DEFLATED) as zf:
            names = set(zf.namelist())
            if "META/fw_info" in names:
                fw_info = zf.read("META/fw_info")
                m = re.search(rb"(\d+\.\d+\.\d+\.\d+)", fw_info)
                if m:
                    return m.group(1).decode("utf-8", errors="ignore")
            if "META/firmware_sign.bin" not in names:
                return None
            data = zf.read("META/firmware_sign.bin")
    except Exception:
        return None

    if get_fw_version is None:
        return None

    tmp_file = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tf:
            tf.write(data)
            tmp_file = tf.name
        ver = get_fw_version(tmp_file)
        if isinstance(ver, str):
            ver = ver.strip("\x00\r\n ")
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", ver):
                return ver
    except Exception:
        return None
    finally:
        if tmp_file and os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except Exception:
                pass
    return None


# === 基础 NAS 配置（如有需要可改成从 JSON / 环境变量读取） ===
NAS_BASE_URL = "https://10.2.100.85:5006"
NAS_USERNAME = "cs-guoqifa"
NAS_PASSWORD = "Huami2020"

BASE_DIR_PREFIX = "/GT智能手表事业部/软件部/软件版本文档"


@dataclass
class WebDavItem:
    name: str
    is_dir: bool
    size: int
    href: str


class WebDavClient:
    def __init__(self, base_url: str, username: str, password: str, verify_tls: bool = False, timeout: int = 1800):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.verify_tls = verify_tls
        self._username = username
        self._password = password
        self._last_sid = None
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.verify = verify_tls

    def _build_url(self, path: str) -> str:
        # 逐段 URL 编码，兼容中文目录名
        encoded_path = "/".join(quote(part, safe="") for part in path.lstrip("/").split("/"))
        return f"{self.base_url}/{encoded_path}" if encoded_path else self.base_url

    def list_directory(self, path: str, debug: bool = False) -> List[WebDavItem]:
        """列出目录内容（仅一层，Depth=1）"""
        url = self._build_url(path)
        try:
            response = self.session.request(
                "PROPFIND",
                url,
                timeout=self.timeout,
                headers={"Depth": "1"},
            )
        except Exception as e:
            print(f"[ERROR] 列目录失败: {path} - {e}")
            return []

        if debug:
            print(f"[DEBUG] PROPFIND {path} -> status={response.status_code}, url={url[:80]}...")

        if response.status_code != 207:
            print(f"[WARN] 列目录返回状态码 {response.status_code}: {path}")
            if response.status_code >= 400 and response.text:
                print(f"[DEBUG] 响应片段: {response.text[:500]}")
            return []

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as e:
            print(f"[ERROR] 解析 PROPFIND 响应 XML 失败: {e}")
            if debug:
                print(f"[DEBUG] 响应片段: {response.content[:500]}")
            return []
        ns = {"d": "DAV:", "e": "urn:schemas-microsoft-com:davprops"}
        items: List[WebDavItem] = []

        for resp in root.findall("d:response", ns):
            href_elem = resp.find("d:href", ns)
            prop_stat = resp.find("d:propstat", ns)
            if href_elem is None or prop_stat is None:
                continue

            href = href_elem.text or ""
            props = prop_stat.find("d:prop", ns)
            if props is None:
                continue

            name_elem = props.find("d:displayname", ns)
            size_elem = props.find("d:getcontentlength", ns)
            iscoll_elem = props.find("d:resourcetype/d:collection", ns)

            # Synology WebDAV 的 href 末尾通常有一个 '/', 直接 split 会得到空串
            # 且 href 通常是 URL 编码（空格为 %20），需要先 unquote 还原再作为目录/文件名使用
            if name_elem is not None and name_elem.text:
                name = name_elem.text
            else:
                name = unquote((href or "").rstrip("/").split("/")[-1])
            if not name or name == "/":
                continue

            is_dir = iscoll_elem is not None
            size = int(size_elem.text) if size_elem is not None else 0

            items.append(WebDavItem(name=name, is_dir=is_dir, size=size, href=href))

        # 先目录后文件，再按名称排序
        items.sort(key=lambda x: (not x.is_dir, x.name))
        if debug and len(items) == 0:
            snippet = (response.content[:800] if isinstance(response.content, bytes) else response.text or "")[:800]
            if isinstance(snippet, bytes):
                snippet = snippet.decode("utf-8", errors="replace")
            print(f"[DEBUG] 未解析到任何项，响应前 800 字符:\n{snippet}")
        return items

    def download_file(self, remote_path: str, local_path: Path) -> bool:
        """通过 WebDAV 直接 GET 下载文件"""
        url = self._build_url(remote_path)
        print(f"[INFO] 下载: {remote_path}", flush=True)
        try:
            resp = self.session.get(url, timeout=self.timeout, stream=True)
        except Exception as e:
            print(f"[ERROR] 下载失败: {remote_path} - {e}")
            return False

        if resp.status_code != 200:
            print(f"[ERROR] 下载失败，状态码 {resp.status_code}: {remote_path}")
            try:
                print(resp.text[:500])
            except Exception:
                pass
            return False

        local_path.parent.mkdir(parents=True, exist_ok=True)
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        last_print_pct = -1.0
        # 在不知道总大小时，按每 100MB 打一行
        milestone_bytes = 100 * 1024 * 1024 if total == 0 else None
        next_milestone = milestone_bytes

        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 64):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = downloaded * 100.0 / total
                    # 每增加 5% 打印一次，避免刷屏
                    if pct - last_print_pct >= 5.0 or pct >= 99.5:
                        last_print_pct = pct
                        mb_done = downloaded / 1024 / 1024
                        mb_total = total / 1024 / 1024
                        # 输出两种格式的进度，方便前端解析
                        print(f"[PROGRESS] {pct:.1f}%", flush=True)
                        print(f"[INFO] 下载进度: {pct:.1f}% ({mb_done:.1f}/{mb_total:.1f} MB)", flush=True)
                elif milestone_bytes is not None and downloaded >= (next_milestone or 0):
                    mb_done = downloaded / 1024 / 1024
                    print(f"[INFO] 已下载约 {mb_done:.1f} MB ...", flush=True)
                    next_milestone += milestone_bytes

        size_mb = local_path.stat().st_size / 1024 / 1024
        print(f"[OK] 已下载到 {local_path} ({size_mb:.2f} MB)")
        return True

    def upload_file(self, local_path: Path, remote_path: str, emit=None) -> bool:
        """上传文件到 NAS：优先尝试 WebDAV PUT，若返回 405 则自动回退到 FileStation Upload API"""
        if not emit:
            emit = lambda x: None
        
        url = self._build_url(remote_path)
        emit(f"[INFO] 上传: {remote_path}\n")
        
        if not local_path.exists():
            emit(f"[ERROR] 本地文件不存在: {local_path}\n")
            return False
        
        try:
            file_size = local_path.stat().st_size
            emit(f"[INFO] 文件大小: {file_size / 1024 / 1024:.2f} MB\n")
            
            # 尝试先创建父目录
            remote_dir = remote_path.rsplit('/', 1)[0] if '/' in remote_path else '/'
            try:
                mkcol_url = self._build_url(remote_dir)
                emit(f"[INFO] 尝试创建目录: {remote_dir}\n")
                mkcol_resp = self.session.request('MKCOL', mkcol_url, timeout=30)
                if mkcol_resp.status_code in (201, 405):  # 405 = 已存在
                    emit(f"[INFO] 目录已存在或已创建\n")
            except Exception as e:
                emit(f"[WARN] 创建目录异常: {e}\n")
            
            # 上传文件
            with open(local_path, 'rb') as f:
                resp = self.session.put(url, data=f, timeout=self.timeout)
            
            if resp.status_code in (200, 201, 204):
                emit(f"[OK] 文件上传成功: {remote_path}\n")
                return True
            elif resp.status_code == 405:
                emit(f"[WARN] WebDAV PUT 返回 405（该路径未开放 WebDAV 写权限），改用 FileStation API 上传...\n")
                return self.upload_via_filestation(local_path, remote_path, emit=emit)
            else:
                emit(f"[ERROR] 上传失败，状态码 {resp.status_code}: {remote_path}\n")
                try:
                    emit(f"[DEBUG] 响应: {resp.text[:300]}\n")
                except Exception:
                    pass
                return False
        except Exception as e:
            emit(f"[ERROR] 上传异常: {remote_path} - {e}\n")
            return False

    def _syno_login(self, emit=None) -> str:
        """登录 Synology DSM 返回 session ID。兼容不同 DSM 版本的 API 入口。
        
        注意：此处不能用 self.session（已绑定 WebDAV Basic Auth），
        Synology Auth API 不接受同时带 Basic Auth 头 + query 参数的双重认证。
        """
        if not emit:
            emit = lambda x: None

        parsed = urlparse(self.base_url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        # 使用独立 session 避免 Basic Auth 头干扰
        login_session = requests.Session()
        login_session.verify = self.verify_tls

        candidates = [
            (f"{base}/webapi/auth.cgi", "7"),
            (f"{base}/webapi/auth.cgi", "6"),
            (f"{base}/webapi/entry.cgi", "7"),
            (f"{base}/webapi/entry.cgi", "6"),
            (f"{base}/webapi/auth.cgi", "3"),
            (f"{base}/webapi/entry.cgi", "3"),
        ]

        errors = []
        for login_url, ver in candidates:
            try:
                resp = login_session.get(
                    login_url,
                    params={
                        'api': 'SYNO.API.Auth',
                        'version': ver,
                        'method': 'login',
                        'account': self._username,
                        'passwd': self._password,
                        'session': 'FileStation',
                        'format': 'sid',
                    },
                    timeout=30,
                )

                try:
                    data = resp.json()
                except Exception:
                    snippet = (resp.text or "")[:200]
                    errors.append(f"{login_url} v{ver}: 非 JSON 响应 HTTP {resp.status_code} {snippet}")
                    continue

                if data.get('success') and data.get('data', {}).get('sid'):
                    sid = data['data']['sid']
                    emit(f"[INFO] Synology 登录成功: {login_url} (v{ver})\n")
                    self._last_sid = sid
                    # 将登录成功的 cookie 同步到主 session，后续 FileStation 调用可以用 cookie 认证
                    self.session.cookies.update(login_session.cookies)
                    return sid

                err_code = data.get('error', {}).get('code') if isinstance(data.get('error'), dict) else data.get('error')
                errors.append(f"{login_url} v{ver}: code={err_code}")
            except Exception as e:
                errors.append(f"{login_url} v{ver}: {e}")

        raise RuntimeError("Synology 登录失败，已尝试多个 API 入口: " + " | ".join(errors[:4]))

    def upload_via_filestation(self, local_path: Path, remote_path: str, emit=None) -> bool:
        """通过 Synology FileStation Upload API 上传文件（WebDAV 不可用时的备选方案）
        
        注意：FileStation API 使用 _sid cookie 认证，会临时移除 session 的 Basic Auth 头。
        """
        if not emit:
            emit = lambda x: None
        
        saved_auth = None
        sid = None
        try:
            parsed = urlparse(self.base_url)
            base = f"{parsed.scheme}://{parsed.netloc}"

            # 尝试登录获取 sid
            try:
                emit(f"[INFO] 登录 Synology DSM ...\n")
                sid = self._syno_login(emit=emit)
            except Exception as e:
                emit(f"[WARN] Synology 登录失败: {e}\n")
                emit(f"[INFO] 回退使用 WebDAV Basic Auth 直传 FileStation...\n")
                sid = None

            remote_dir = remote_path.rsplit('/', 1)[0] if '/' in remote_path else '/'
            file_name = remote_path.rsplit('/', 1)[-1]

            emit(f"[INFO] FileStation 上传到: {remote_dir}/{file_name}\n")
            api_url = f"{base}/webapi/entry.cgi"

            # 如果登录成功拿到了 sid，临时移除 Basic Auth（用 cookie 认证更可靠）
            # 如果登录失败，保留 Basic Auth 作兜底
            if sid:
                saved_auth = self.session.auth
                self.session.auth = None

            # 兼容不同 DSM 行为：尝试多个 API 版本，并同时尝试带/不带 _sid（cookie 会由 session 保留）
            attempts = [
                ('3', True),
                ('2', True),
                ('3', False),
                ('2', False),
            ]
            last_err = ""

            for ver, with_sid in attempts:
                params = {
                    'api': 'SYNO.FileStation.Upload',
                    'version': ver,
                    'method': 'upload',
                }
                if with_sid:
                    params['_sid'] = sid

                emit(f"[INFO] 尝试 FileStation 上传: version={ver}, with_sid={with_sid}\n")
                with open(local_path, 'rb') as f:
                    resp = self.session.post(
                        api_url,
                        params=params,
                        data={
                            'path': remote_dir,
                            'create_parents': 'true',
                            'overwrite': 'true',
                        },
                        files={'file': (file_name, f, 'application/octet-stream')},
                        verify=self.verify_tls,
                        timeout=self.timeout,
                    )

                try:
                    data = resp.json()
                except Exception:
                    snippet = (resp.text or "")[:300]
                    last_err = f"HTTP {resp.status_code} 非 JSON 响应: {snippet}"
                    emit(f"[WARN] FileStation 非 JSON 响应: {last_err}\n")
                    continue

                if data.get('success'):
                    emit(f"[OK] FileStation 上传成功: {remote_path}\n")
                    return True

                err = data.get('error') or {}
                err_code = err.get('code') if isinstance(err, dict) else err
                last_err = f"code={err_code}, raw={err}"

                if err_code == 414:
                    emit("[WARN] FileStation 返回 code=414，常见于路径无效/无权限/共享目录受限，尝试其他上传参数...\n")
                else:
                    emit(f"[WARN] FileStation 上传失败: {last_err}\n")

            emit(f"[ERROR] FileStation 上传最终失败: {last_err}\n")
            return False
        except Exception as e:
            emit(f"[ERROR] FileStation 上传异常: {e}\n")
            return False
        finally:
            # 恢复 Basic Auth（WebDAV 后续操作需要）
            if saved_auth is not None:
                self.session.auth = saved_auth

    def create_sharing_link(self, remote_path: str, emit=None) -> str:
        """创建文件的分享链接（调用 Synology FileStation API）
        
        注意：Sharing API 使用 _sid cookie 认证，会临时移除 session 的 Basic Auth 头。
        返回: 分享链接 URL，失败返回空字符串
        """
        if not emit:
            emit = lambda x: None
        
        saved_auth = None
        try:
            # 提取 NAS 基础 URL
            parsed = urlparse(self.base_url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            
            # 调用 Synology FileStation API 创建分享链接
            api_url = f"{base}/webapi/entry.cgi"
            
            # remote_path 通常是 /path/to/file，需要保持前导 /
            share_path = remote_path if remote_path.startswith('/') else f"/{remote_path}"

            # 先确保有 DSM sid（某些 DSM 对分享 API 不接受 Basic Auth）
            sid = self._last_sid
            if not sid:
                try:
                    sid = self._syno_login(emit=emit)
                except Exception as e:
                    emit(f"[WARN] 创建分享链接前登录失败: {e}\n")
                    sid = None

            emit(f"[INFO] 创建分享链接: {share_path.lstrip('/')}\n")

            # 临时移除 Basic Auth，Sharing API 依赖 cookie/sid 认证
            saved_auth = self.session.auth
            self.session.auth = None

            # 兼容 DSM 差异：API 名称/版本/参数格式会不同
            attempts = [
                ('SYNO.FileStation2.Sharing', '3', 'paths_json'),
                ('SYNO.FileStation2.Sharing', '3', 'path_single'),
                ('SYNO.FileStation.Sharing', '3', 'paths_json'),
                ('SYNO.FileStation.Sharing', '3', 'path_single'),
                ('SYNO.FileStation.Sharing', '1', 'paths_json'),
                ('SYNO.FileStation.Sharing', '1', 'path_single'),
            ]

            last_err = ''
            for api_name, ver, style in attempts:
                payload = {
                    'api': api_name,
                    'version': ver,
                    'method': 'create',
                }
                if sid:
                    payload['_sid'] = sid
                if style == 'paths_json':
                    payload['paths'] = json.dumps([share_path], ensure_ascii=False)
                else:
                    payload['path'] = share_path

                emit(f"[INFO] 尝试创建分享: api={api_name}, ver={ver}, style={style}\n")
                resp = self.session.post(api_url, data=payload, timeout=20)
                if resp.status_code != 200:
                    last_err = f"HTTP {resp.status_code}"
                    continue

                try:
                    data = resp.json()
                except Exception:
                    snippet = (resp.text or '')[:300]
                    last_err = f"非 JSON 响应: {snippet}"
                    continue

                if data.get('success'):
                    sharings = data.get('data', {}).get('sharing', [])
                    if sharings:
                        sharing_id = sharings[0].get('id', '')
                        if sharing_id:
                            sharing_url = f"{base}/sharing/{sharing_id}"
                            emit(f"[OK] 分享链接已生成: {sharing_url}\n")
                            return sharing_url
                    # SYNO.FileStation.Sharing 常见返回: data.links
                    links = data.get('data', {}).get('links') or []
                    if links:
                        sharing_id = links[0].get('id', '')
                        if sharing_id:
                            sharing_url = f"{base}/sharing/{sharing_id}"
                            emit(f"[OK] 分享链接已生成: {sharing_url}\n")
                            return sharing_url
                    # 有些 DSM 返回 link/url 字段
                    maybe_url = data.get('data', {}).get('url') or data.get('data', {}).get('link')
                    if maybe_url:
                        emit(f"[OK] 分享链接已生成: {maybe_url}\n")
                        return maybe_url

                err = data.get('error') or {}
                err_code = err.get('code') if isinstance(err, dict) else err
                last_err = f"code={err_code}, raw={err}"

            emit(f"[WARN] 创建分享链接失败: {last_err}\n")
        
        except Exception as e:
            emit(f"[WARN] 创建分享链接异常: {e}\n")
        
        finally:
            # 恢复 Basic Auth（WebDAV 后续操作需要）
            if saved_auth is not None:
                self.session.auth = saved_auth
        return ""


def _normalize_version_dots(s: str) -> str:
    """把全角点、Unicode 句号等统一成半角点，便于和 NAS 目录名对比"""
    s = s.replace("\uff0e", ".").replace("．", ".")
    return s


def find_version_dir(
    client: WebDavClient,
    project_root: str,
    version_str: str,
    debug: bool = False,
    verbose_errors: bool = True,
) -> Optional[str]:
    """在 project_root 下找到名称中匹配 version_str 的目录名（包含一定模糊匹配能力）"""
    items = client.list_directory(project_root, debug=debug)
    norm_ver = _normalize_version_dots(version_str)
    # 先直接用子串匹配（目录名也做一次点号归一化，避免全角点导致匹配失败）
    candidates = [
        it
        for it in items
        if it.is_dir and norm_ver in _normalize_version_dots(it.name)
    ]
    if not candidates:
        # 再尝试基于数字序列的模糊匹配（处理全角点等情况）
        digits = [ch for ch in norm_ver if ch.isdigit()]
        if digits:
            pattern_str = r"".join(re.escape(d) + r"\D*" for d in digits)
            pattern = re.compile(pattern_str)
            candidates = [it for it in items if it.is_dir and pattern.search(it.name)]

    if not candidates:
        n_total = len(items)
        n_dirs = sum(1 for it in items if it.is_dir)
        if verbose_errors:
            print(f"[ERROR] 在 {project_root} 下未找到包含版本号 '{version_str}' 的目录（共列出 {n_total} 项，其中 {n_dirs} 个为目录）")
            for it in items:
                if it.is_dir:
                    print(f"  - {it.name}")
        return None
    if len(candidates) > 1:
        print(f"[WARN] 发现多个包含版本号 '{version_str}' 的目录，优先选择第一个：")
        for it in candidates:
            print(f"  - {it.name}")
    return candidates[0].name


def join_path(*parts: str) -> str:
    cleaned = [p.strip("/ ") for p in parts if p and p.strip("/ ")]
    return "/" + "/".join(cleaned)


def find_subdir_case_insensitive(
    client: WebDavClient, base_path: str, keyword: str
) -> Optional[str]:
    """在 base_path 下找到名称包含 keyword（忽略大小写）的子目录名"""
    keyword_lower = keyword.lower()
    for item in client.list_directory(base_path):
        if item.is_dir and keyword_lower in item.name.lower():
            return item.name
    return None


def resolve_variant_base(
    client: WebDavClient,
    version_base: str,
    variant_dir: Optional[str],
    debug: bool = False,
) -> str:
    """
    解析版本目录下的变体目录（如 32m/64m, milan_32m/pamir_64m）。
    若找不到匹配，回退到 version_base 本身，不直接失败。
    """
    if not variant_dir:
        return version_base

    raw = variant_dir.strip().lower()
    aliases = {raw}
    if "_" in raw:
        aliases.add(raw.split("_", 1)[1])
    # 允许 64 <-> 64m 的弱匹配
    if raw.endswith("m") and raw[:-1].isdigit():
        aliases.add(raw[:-1])
    if raw.isdigit():
        aliases.add(raw + "m")

    items = client.list_directory(version_base, debug=debug)
    dirs = [it for it in items if it.is_dir]
    if not dirs:
        print(f"[WARN] 在 {version_base} 下未发现子目录，跳过变体匹配（variant={variant_dir}）")
        return version_base

    # 先精确匹配，再包含匹配
    for it in dirs:
        low = it.name.lower()
        if low in aliases:
            matched = join_path(version_base, it.name)
            print(f"[INFO] 变体目录匹配成功（精确）: {matched}")
            return matched

    for it in dirs:
        low = it.name.lower()
        if any(a in low for a in aliases):
            matched = join_path(version_base, it.name)
            print(f"[INFO] 变体目录匹配成功（模糊）: {matched}")
            return matched

    print(f"[WARN] 在 {version_base} 下未找到变体目录 '{variant_dir}'，将直接在版本目录查找 Release/Monkey")
    return version_base


def candidate_project_roots(actual_project_name: str) -> List[str]:
    project_base = join_path(BASE_DIR_PREFIX, actual_project_name)
    return [
        join_path(project_base, "用户固件"),
        join_path(project_base, "用户版本"),
        project_base,
    ]


def select_project_root(
    client: WebDavClient,
    actual_project_name: str,
    from_ver: str,
    to_ver: str,
    debug: bool = False,
) -> Optional[str]:
    """
    选择可用的项目根目录：优先选择同时包含 from/to 两个版本目录的路径。
    兼容：
    - /项目/用户固件/版本目录
    - /项目/用户版本/版本目录
    - /项目/版本目录（没有“用户固件/用户版本”层）
    """
    roots = candidate_project_roots(actual_project_name)

    scored: List[Tuple[int, str]] = []
    for root in roots:
        old_hit = find_version_dir(client, root, from_ver, debug=debug, verbose_errors=False)
        new_hit = find_version_dir(client, root, to_ver, debug=debug, verbose_errors=False)
        score = (1 if old_hit else 0) + (1 if new_hit else 0)
        scored.append((score, root))
        if score == 2:
            return root

    scored.sort(key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] > 0:
        best_root = scored[0][1]
        print(f"[WARN] 未找到同时匹配 from/to 的项目根目录，回退使用: {best_root}")
        return best_root

    return None


def find_release_ota_sign(
    client: WebDavClient, version_base: str
) -> Optional[str]:
    """
    在 version_base 下按规则查找 Release 目录中的 watch@<chip>_ota_sign.zip
    返回远程路径（WebDAV 路径）
    """
    release_dir_name = find_subdir_case_insensitive(client, version_base, "release")
    search_base = join_path(version_base, release_dir_name) if release_dir_name else version_base

    items = client.list_directory(search_base)
    candidates: List[WebDavItem] = []
    for it in items:
        if it.is_dir:
            continue
        name_lower = it.name.lower()
        if re.match(r"^watch@[^/\\]+_ota_sign\.zip$", name_lower) or (
            name_lower.endswith(".zip") and "ota_sign" in name_lower
        ):
            candidates.append(it)

    if not candidates:
        print(f"[ERROR] 在 {search_base} 下未找到 ota_sign zip 文件")
        return None

    if len(candidates) > 1:
        print("[WARN] 发现多个 ota_sign zip，优先选择第一个：")
        for it in candidates:
            print(f"  - {it.name}")

    return join_path(search_base, candidates[0].name)


def find_monkey_archive_tgz(
    client: WebDavClient, version_base: str
) -> Optional[str]:
    """
    在 version_base 下找到 Monkey 目录里的 archive_*.tgz（优先选体积最大的）
    返回远程路径（WebDAV 路径）
    """
    monkey_dir_name = find_subdir_case_insensitive(client, version_base, "monkey")
    if not monkey_dir_name:
        print(f"[ERROR] 在 {version_base} 下未找到 Monkey 目录")
        return None

    monkey_base = join_path(version_base, monkey_dir_name)
    items = client.list_directory(monkey_base)
    candidates = [
        it
        for it in items
        if (not it.is_dir)
        and it.name.lower().startswith("archive_")
        and it.name.lower().endswith(".tgz")
    ]
    if not candidates:
        print(f"[ERROR] 在 {monkey_base} 下未找到 archive_*.tgz")
        return None

    # 选 size 最大的
    best = max(candidates, key=lambda x: x.size)
    return join_path(monkey_base, best.name)


def extract_version_from_name(name: str) -> Optional[str]:
    """从文件名里提取版本号（x.x.x.x），用于更准确的输出命名。"""
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", name)
    if m:
        return m.group(1)
    return None


def extract_tgz(tgz_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tgz_path, "r:gz") as tar:
        tar.extractall(out_dir)
    return out_dir


def find_inner_ota_sign_from_monkey_archive(archive_tgz: Path, work_dir: Path) -> Optional[Path]:
    """
    适配 pamir/pike 的 Monkey 结构：
    archive_xxx_版本.tgz -> (目录) -> archive_OTA_CLOUD_xxx_版本.tgz -> (目录) -> ... -> *ota_sign.zip
    """
    level1_dir = work_dir / "level1"
    extract_tgz(archive_tgz, level1_dir)

    # 在 level1 里找 archive_OTA_CLOUD_*.tgz
    inner_tgz: Optional[Path] = None
    for p in level1_dir.rglob("*.tgz"):
        name_lower = p.name.lower()
        if "archive_ota_cloud" in name_lower:
            inner_tgz = p
            break
    if inner_tgz is None:
        print(f"[ERROR] 在 {level1_dir} 下未找到 archive_OTA_CLOUD_*.tgz")
        return None

    level2_dir = work_dir / "level2"
    extract_tgz(inner_tgz, level2_dir)

    # 在 level2 里找 ota_sign zip
    for p in level2_dir.rglob("*.zip"):
        name_lower = p.name.lower()
        if re.match(r"^watch@[^/\\]+_ota_sign\.zip$", name_lower) or (
            name_lower.endswith(".zip") and "ota_sign" in name_lower
        ):
            return p

    print(f"[ERROR] 在 {level2_dir} 下未找到 ota_sign zip")
    return None


def prepare_ota_sign_for_version(
    client: WebDavClient,
    project_root: str,
    version_str: str,
    package_type: str,
    variant_dir: Optional[str],
    temp_root: Path,
    label: str,
    debug: bool = False,
) -> Tuple[Optional[Path], Optional[str]]:
    """
    为某个版本（from/to 之一）准备 ota_sign.zip，返回(本地路径, 实际版本号)
    package_type: "release" or "monkey"
    """
    version_dir_name = find_version_dir(client, project_root, version_str, debug=debug)
    if not version_dir_name:
        return None, None

    version_base = join_path(project_root, version_dir_name)
    version_base = resolve_variant_base(client, version_base, variant_dir, debug=debug)

    print(f"[INFO] [{label}] 版本目录: {version_base}", flush=True)

    if package_type == "release":
        remote_ota_sign = find_release_ota_sign(client, version_base)
        if not remote_ota_sign:
            return None, None
        local_zip = temp_root / f"{label}_release_ota_sign.zip"
        if not client.download_file(remote_ota_sign, local_zip):
            return None, None
        # release 模式通常目录版本就是对外展示版本
        actual_ver = extract_version_from_name(Path(remote_ota_sign).name) or version_str
        return local_zip, actual_ver

    if package_type == "monkey":
        remote_archive = find_monkey_archive_tgz(client, version_base)
        if not remote_archive:
            return None, None

        local_archive = temp_root / f"{label}_monkey_archive.tgz"
        if not client.download_file(remote_archive, local_archive):
            return None, None

        work_dir = temp_root / f"{label}_monkey_work"
        ota_sign = find_inner_ota_sign_from_monkey_archive(local_archive, work_dir)
        # 本地临时文件名不带版本号，需从 NAS 真实包名提取版本号
        actual_ver = extract_version_from_name(Path(remote_archive).name) or version_str
        return ota_sign, actual_ver

    print(f"[ERROR] 未知包类型: {package_type}")
    return None, None


def open_explorer(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform.startswith("darwin"):
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}"')
    except Exception as e:
        print(f"[WARN] 打开资源管理器失败: {e}")


def launch_ota_tool(exe_path: Optional[Path]) -> None:
    if not exe_path:
        return
    try:
        if not exe_path.exists():
            print(f"[WARN] OTA 升级助手不存在: {exe_path}")
            return
        import subprocess

        # 某些版本的 OTA 升级助手依赖自身目录下的配置文件，必须在其安装目录作为工作目录启动
        subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent))
    except Exception as e:
        print(f"[WARN] 启动 OTA 升级助手失败: {e}")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 NAS 自动准备 OTA 差分所需的 ota_sign.zip"
    )
    parser.add_argument("--project", required=True, help="项目名，例如 Pike / Pamir / Milan / Windermere")
    parser.add_argument("--from-ver", required=True, help="旧版本号关键字，例如 2.2.0.1")
    parser.add_argument("--to-ver", required=True, help="新版本号关键字，例如 2.3.1.1")
    parser.add_argument(
        "--type",
        choices=["release", "monkey"],
        required=True,
        help="包类型：release 或 monkey",
    )
    parser.add_argument(
        "--variant-dir",
        help="版本目录下的可选子目录，例如 pamir_64m / pamir_32m；大多数项目可不填",
    )
    parser.add_argument(
        "--output-dir",
        help="最终输出目录（默认：work/ota_input/{project}/{from}_to_{to}/{type}）",
    )
    parser.add_argument(
        "--ota-tool",
        help="本地 OTA 升级助手路径；不指定则优先从 ota_local_config.json 读取，最后回退到默认路径",
    )
    parser.add_argument(
        "--pem",
        help="用于生成差分 OTA 包的 ECDSA 私钥 PEM 路径；不填写则只下载/准备 ota_sign.zip",
    )
    parser.add_argument(
        "--diff-name",
        help="差分 OTA 输出文件名（默认：{project}_diff_{from}_to_{to}.zip）",
    )
    parser.add_argument(
        "--no-open-tool",
        action="store_true",
        help="仅准备 ota_sign.zip，不自动打开 OTA 升级助手",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="跳过 NAS 下载，直接复用 output-dir 中已存在的 old/new ota_sign.zip",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="打印 WebDAV 调试信息（列目录为空时输出响应片段）",
    )
    return parser.parse_args(argv)


def find_project_dir(client: WebDavClient, project_input: str, debug: bool = False) -> Optional[str]:
    """
    在 BASE_DIR_PREFIX 下查找项目目录（大小写不敏感匹配）
    返回找到的实际目录名，或 None 如果未找到
    """
    items = client.list_directory(BASE_DIR_PREFIX, debug=debug)
    project_lower = project_input.lower()
    
    # 先尝试精确匹配（忽略大小写）
    candidates = [
        it.name for it in items 
        if it.is_dir and it.name.lower() == project_lower
    ]
    
    if candidates:
        if len(candidates) > 1:
            print(f"[WARN] 发现多个项目名匹配项 '{project_input}'，使用第一个：{candidates[0]}")
        return candidates[0]
    
    # 如果没有精确匹配，尝试部分匹配（包含项目名的目录）
    candidates = [
        it.name for it in items 
        if it.is_dir and project_lower in it.name.lower()
    ]
    
    if candidates:
        if len(candidates) > 1:
            print(f"[WARN] 发现多个包含 '{project_input}' 的项目目录，使用第一个：{candidates[0]}")
        return candidates[0]
    
    print(f"[ERROR] 在 {BASE_DIR_PREFIX} 下未找到项目'{project_input}'（不区分大小写）")
    return None


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    # 读取本地可选配置（例如 pem / ota_tool / auto_open_tool）
    local_cfg = load_local_config()

    project = args.project.strip().rstrip("/")
    from_ver = args.from_ver
    to_ver = args.to_ver
    package_type = args.type
    variant_dir = args.variant_dir

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = Path("work") / "ota_input" / project / f"{from_ver}_to_{to_ver}" / package_type
    output_dir.mkdir(parents=True, exist_ok=True)

    # OTA 工具路径优先级：命令行 > 本地配置 > 默认值
    if args.ota_tool:
        ota_tool_path = Path(args.ota_tool)
    elif isinstance(local_cfg.get("ota_tool"), str):
        ota_tool_path = Path(local_cfg["ota_tool"])
    else:
        ota_tool_path = Path(r"D:\ota_assistant1.0.2.3\ota_assistant\OTA升级助手.exe")

    client = WebDavClient(
        base_url=NAS_BASE_URL,
        username=NAS_USERNAME,
        password=NAS_PASSWORD,
        verify_tls=False,
        timeout=1800,
    )

    temp_root = Path(tempfile.mkdtemp(prefix="ota_prepare_"))
    print(f"[INFO] 临时工作目录: {temp_root}", flush=True)

    try:
        old_actual_ver = None
        new_actual_ver = None

        final_old = output_dir / f"{project}_old_{package_type}_ota_sign.zip"
        final_new = output_dir / f"{project}_new_{package_type}_ota_sign.zip"

        if args.skip_download:
            if not final_old.exists() or not final_new.exists():
                print("[FATAL] --skip-download 已开启，但本地 ota_sign 文件不存在：")
                if not final_old.exists():
                    print(f"  - 缺少旧版本文件: {final_old}")
                if not final_new.exists():
                    print(f"  - 缺少新版本文件: {final_new}")
                print("[HINT] 先执行一次不带 --skip-download 的命令，或把文件放到上述路径")
                return 1

            old_ota = final_old
            new_ota = final_new
            print("[INFO] 已跳过下载，复用本地 ota_sign 文件")

            meta = load_output_meta(output_dir)
            old_actual_ver = meta.get("old_actual_ver") if isinstance(meta, dict) else None
            new_actual_ver = meta.get("new_actual_ver") if isinstance(meta, dict) else None

            # monkey 优先尝试从 ota_sign.zip 内容提取真实版本，确保命名稳定
            if package_type == "monkey":
                old_actual_ver = infer_version_from_ota_zip(final_old) or old_actual_ver
                new_actual_ver = infer_version_from_ota_zip(final_new) or new_actual_ver

            if old_actual_ver:
                print(f"[INFO] 复用旧版本实际包版本: {old_actual_ver}")
            if new_actual_ver:
                print(f"[INFO] 复用新版本实际包版本: {new_actual_ver}")
        else:
            # 查找正确的项目目录名（大小写不敏感）
            actual_project_name = find_project_dir(client, project, debug=getattr(args, "debug", False))
            if not actual_project_name:
                print(f"[FATAL] 无法找到项目 '{project}'，请检查项目名拼写")
                return 1

            project_root = select_project_root(
                client,
                actual_project_name=actual_project_name,
                from_ver=from_ver,
                to_ver=to_ver,
                debug=getattr(args, "debug", False),
            )
            if not project_root:
                print(f"[FATAL] 在项目 '{actual_project_name}' 下未找到可用的版本目录根路径（已尝试 用户固件 / 用户版本 / 项目根）")
                for root in candidate_project_roots(actual_project_name):
                    print(f"  - 已尝试: {root}")
                return 1
            print(f"[INFO] 项目根目录: {project_root}", flush=True)

            old_ota, old_actual_ver = prepare_ota_sign_for_version(
                client=client,
                project_root=project_root,
                version_str=from_ver,
                package_type=package_type,
                variant_dir=variant_dir,
                temp_root=temp_root / "old",
                label="old",
                debug=getattr(args, "debug", False),
            )
            if not old_ota:
                print("[FATAL] 旧版本 ota_sign 准备失败")
                return 1

            new_ota, new_actual_ver = prepare_ota_sign_for_version(
                client=client,
                project_root=project_root,
                version_str=to_ver,
                package_type=package_type,
                variant_dir=variant_dir,
                temp_root=temp_root / "new",
                label="new",
                debug=getattr(args, "debug", False),
            )
            if not new_ota:
                print("[FATAL] 新版本 ota_sign 准备失败")
                return 1

        if old_actual_ver and old_actual_ver != from_ver:
            print(f"[INFO] 旧版本命名将使用实际包版本: {old_actual_ver} (目录版本: {from_ver})")
        if new_actual_ver and new_actual_ver != to_ver:
            print(f"[INFO] 新版本命名将使用实际包版本: {new_actual_ver} (目录版本: {to_ver})")

        if not args.skip_download:
            shutil.copy2(old_ota, final_old)
            shutil.copy2(new_ota, final_new)
            save_output_meta(
                output_dir,
                {
                    "project": project,
                    "package_type": package_type,
                    "from_ver_input": from_ver,
                    "to_ver_input": to_ver,
                    "old_actual_ver": old_actual_ver,
                    "new_actual_ver": new_actual_ver,
                },
            )

        print("\n[OK] 已准备好 OTA 差分输入文件：")
        print(f"  旧版本 ota_sign: {final_old}")
        print(f"  新版本 ota_sign: {final_new}")

        # 如提供 PEM（命令行或本地配置）且已成功导入 gen_diff_archive，则自动生成差分 OTA 包
        pem_path = args.pem or local_cfg.get("pem")
        if pem_path and gen_diff_archive is not None:
            old_ver_for_name = old_actual_ver or from_ver
            new_ver_for_name = new_actual_ver or to_ver
            diff_name = (
                args.diff_name
                if args.diff_name
                else f"{project}_diff_{old_ver_for_name}_to_{new_ver_for_name}.zip"
            )
            diff_path = output_dir / diff_name
            print(f"\n[INFO] 开始生成差分 OTA 包: {diff_path}")
            ok = gen_diff_archive(
                pem_path,
                str(final_old),
                str(final_new),
                str(diff_path),
                None,
            )
            if ok:
                print(f"[OK] 差分 OTA 生成完成: {diff_path}")
            else:
                print("[ERROR] 差分 OTA 生成失败，请检查日志和参数")

        open_explorer(output_dir)

        # 是否自动打开 GUI OTA 工具：
        # - 命令行 --no-open-tool 为最高优先级
        # - 否则看本地配置 auto_open_tool（默认 True）
        auto_open_tool_cfg = local_cfg.get("auto_open_tool")
        auto_open_tool = True if auto_open_tool_cfg is None else bool(auto_open_tool_cfg)

        if not args.no_open_tool and auto_open_tool:
            launch_ota_tool(ota_tool_path)

        return 0
    finally:
        try:
            shutil.rmtree(temp_root)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

