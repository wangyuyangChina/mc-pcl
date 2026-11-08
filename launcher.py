#!/usr/bin/env python3
"""Local Minecraft launcher backend for the static web UI."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ.get("MC_PCL_HOME", Path.home() / ".mc-pcl")).expanduser().resolve()
GAME_DIR = DATA_DIR / "game"
LIBRARIES_DIR = GAME_DIR / "libraries"
ASSETS_DIR = GAME_DIR / "assets"
VERSIONS_DIR = GAME_DIR / "versions"
CONFIG_FILE = DATA_DIR / "config.json"
AUTH_FILE = DATA_DIR / "auth.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
METADATA_DIR = DATA_DIR / "metadata"
JAVA_DIR = DATA_DIR / "java"
MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
JAVA_RUNTIME_INDEX_URL = (
    "https://piston-meta.mojang.com/v1/products/java-runtime/"
    "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"
)
ALLOWED_DOWNLOAD_HOSTS = {
    "piston-meta.mojang.com",
    "piston-data.mojang.com",
    "launcher.mojang.com",
    "resources.download.minecraft.net",
    "libraries.minecraft.net",
    "repo.maven.apache.org",
    "repo1.maven.org",
}
MICROSOFT_DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBOX_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XBOX_XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MINECRAFT_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MINECRAFT_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"
MINECRAFT_ENTITLEMENTS_URL = "https://api.minecraftservices.com/entitlements/mcstore"

_tasks: dict[str, dict] = {}
_task_lock = threading.Lock()
_auth_flow: dict | None = None
_auth_lock = threading.Lock()


class LauncherError(Exception):
    pass


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as error:
        raise LauncherError(f"无法读取 {path.name}：{error}") from error


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


def _safe_path(base: Path, relative: str) -> Path:
    candidate = (base / relative).resolve()
    if candidate != base.resolve() and base.resolve() not in candidate.parents:
        raise LauncherError("下载清单包含无效路径，已停止安装。")
    return candidate


def _validate_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        raise LauncherError("下载地址不是允许的官方 HTTPS 地址。")


def _request_json(url: str, expected_sha1: str | None = None, timeout: int = 30):
    _validate_url(url)
    request = Request(url, headers={"User-Agent": "MCPCL/0.1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise LauncherError(f"读取 Mojang 官方数据失败：{error}") from error
    if expected_sha1 and hashlib.sha1(data).hexdigest() != expected_sha1.lower():
        raise LauncherError("官方元数据 SHA-1 校验失败。")
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LauncherError(f"官方元数据格式无效：{error}") from error


def _download_file(
    url: str,
    destination: Path,
    expected_sha1: str | None,
    expected_size: int | None,
    task_id: str | None = None,
):
    _validate_url(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".part-{task_id or 'download'}")
    for attempt in range(6):
        digest = hashlib.sha1()
        attempt_bytes = 0
        try:
            request = Request(url, headers={"User-Agent": "MCPCL/0.1"})
            with urlopen(request, timeout=60) as response, temporary.open("wb") as output:
                while True:
                    if task_id and _task_cancelled(task_id):
                        raise LauncherError("下载已取消。")
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    attempt_bytes += len(chunk)
                    if task_id:
                        _add_task_bytes(task_id, len(chunk))
            if expected_sha1 and digest.hexdigest().lower() != expected_sha1.lower():
                raise LauncherError(f"SHA-1 校验失败：{destination.name}")
            if expected_size is not None and temporary.stat().st_size != expected_size:
                raise LauncherError(f"文件大小校验失败：{destination.name}")
            temporary.replace(destination)
            return
        except Exception as error:
            temporary.unlink(missing_ok=True)
            if task_id:
                _subtract_task_bytes(task_id, attempt_bytes)
            if isinstance(error, HTTPError):
                retryable = error.code == 429 or 500 <= error.code < 600
            elif isinstance(error, (URLError, TimeoutError, OSError)):
                retryable = True
            else:
                retryable = isinstance(error, LauncherError) and (
                    "SHA-1 校验失败" in str(error) or "文件大小校验失败" in str(error)
                )
            if task_id and _task_cancelled(task_id):
                raise LauncherError("下载已取消。") from error
            if retryable and attempt < 5:
                if task_id:
                    _update_task(
                        task_id,
                        stage=f"连接中断，正在重试下载：{destination.name}（{attempt + 2}/6）",
                    )
                time.sleep(min(2 ** attempt, 8))
                continue
            if isinstance(error, LauncherError) and not retryable:
                raise
            raise LauncherError(
                f"下载失败（已尝试 {attempt + 1} 次）：{error}"
            ) from error


def _new_task(kind: str, version_id: str):
    task_id = str(uuid.uuid4())
    with _task_lock:
        _tasks[task_id] = {
            "id": task_id,
            "kind": kind,
            "version": version_id,
            "status": "queued",
            "stage": "排队中",
            "completed": 0,
            "total": 0,
            "bytes": 0,
            "currentBytes": 0,
            "error": None,
            "processId": None,
            "_cancel": threading.Event(),
        }
    return task_id


def _task_cancelled(task_id: str):
    with _task_lock:
        task = _tasks.get(task_id)
        return task is None or task["_cancel"].is_set()


def _update_task(task_id: str, **updates):
    with _task_lock:
        if task_id in _tasks:
            _tasks[task_id].update(updates)


def _add_task_bytes(task_id: str, amount: int):
    with _task_lock:
        if task_id in _tasks:
            _tasks[task_id]["bytes"] += amount
            _tasks[task_id]["currentBytes"] += amount


def _subtract_task_bytes(task_id: str, amount: int):
    with _task_lock:
        if task_id in _tasks:
            _tasks[task_id]["bytes"] = max(0, _tasks[task_id]["bytes"] - amount)
            _tasks[task_id]["currentBytes"] = max(0, _tasks[task_id]["currentBytes"] - amount)


def _complete_task_item(task_id: str):
    with _task_lock:
        if task_id in _tasks:
            _tasks[task_id]["completed"] += 1


def _public_task(task: dict):
    return {key: value for key, value in task.items() if not key.startswith("_")}


def _cancel_point(task_id: str):
    if _task_cancelled(task_id):
        raise LauncherError("操作已取消。")


def _detect_os():
    name = platform.system().lower()
    if name == "darwin":
        return "osx"
    if name == "windows":
        return "windows"
    if name == "linux":
        return "linux"
    raise LauncherError(f"暂不支持此操作系统：{platform.system()}")


def _detect_arch():
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if machine in ("x86", "i386", "i686"):
        return "x86"
    return machine


def _java_info(java_path: str | None = None):
    executable = java_path or shutil.which("java")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = result.stderr + result.stdout
    match = re.search(r'version "(?:1\.)?(\d+)', output)
    if not match:
        return None
    return {"path": executable, "major": int(match.group(1)), "version": match.group(0).split('"')[1]}


def _load_config():
    config = _read_json(CONFIG_FILE, {})
    if not isinstance(config, dict):
        config = {}
    env_client_id = os.environ.get("MC_PCL_CLIENT_ID", "").strip()
    if env_client_id:
        config["microsoftClientId"] = env_client_id
    return config


def _load_auth():
    auth = _read_json(AUTH_FILE, {})
    return auth if isinstance(auth, dict) else {}


def _profile_summary():
    auth = _load_auth()
    profile = auth.get("profile")
    if not isinstance(profile, dict):
        return None
    return {"name": profile.get("name"), "id": profile.get("id")}


def _cached_official_json(url: str, cache_path: Path, expected_sha1: str | None = None):
    try:
        value = _request_json(url, expected_sha1)
        _write_json(cache_path, value)
        return value, False
    except LauncherError as error:
        cached = _read_json(cache_path)
        if cached is None:
            raise
        sys.stderr.write(f"[MCPCL] 官方数据暂不可用，使用缓存：{error}\n")
        return cached, True


def _catalog_manifest():
    manifest, stale = _cached_official_json(
        MANIFEST_URL, METADATA_DIR / "version-manifest.json"
    )
    if not isinstance(manifest, dict) or not isinstance(manifest.get("versions"), list):
        raise LauncherError("官方版本清单无效，且没有可用缓存。")
    return manifest, stale


def _version_metadata(version_id: str):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", version_id):
        raise LauncherError("版本号格式无效。")
    manifest, manifest_stale = _catalog_manifest()
    entry = next((item for item in manifest["versions"] if item.get("id") == version_id), None)
    if not entry:
        raise LauncherError("官方版本清单中找不到此版本。")
    if (
        entry.get("type") not in {"release", "snapshot", "old_beta", "old_alpha"}
        or not isinstance(entry.get("url"), str)
        or not is_sha1(entry.get("sha1"))
    ):
        raise LauncherError("官方版本元数据不完整。")
    cache_id = hashlib.sha256(version_id.encode("utf-8")).hexdigest()
    metadata, stale = _cached_official_json(
        entry["url"], METADATA_DIR / f"version-{cache_id}.json", entry["sha1"]
    )
    if not isinstance(metadata, dict) or metadata.get("id") != version_id:
        raise LauncherError("官方版本元数据 ID 与请求版本不一致。")
    return metadata, stale or manifest_stale


def _rule_matches(rule: dict, os_name: str, arch: str):
    os_rule = rule.get("os", {})
    if "name" in os_rule and os_rule["name"] != os_name:
        return False
    if "arch" in os_rule and not re.search(os_rule["arch"], arch):
        return False
    if "version" in os_rule and not re.search(os_rule["version"], platform.release()):
        return False
    for feature, expected in rule.get("features", {}).items():
        if expected is not False:
            return False
    return True


def _is_allowed(item: dict, os_name: str, arch: str):
    rules = item.get("rules")
    if not rules:
        return True
    allowed = False
    for rule in rules:
        if _rule_matches(rule, os_name, arch):
            allowed = rule.get("action") == "allow"
    return allowed


def _library_artifact(library: dict):
    downloads = library.get("downloads", {})
    artifact = downloads.get("artifact")
    if artifact and isinstance(artifact.get("path"), str) and isinstance(artifact.get("url"), str):
        return artifact
    name = library.get("name", "")
    parts = name.split(":")
    if len(parts) < 3:
        return None
    group, artifact_name, version, *classifier = parts
    file_name = f"{artifact_name}-{version}{('-' + classifier[0]) if classifier else ''}.jar"
    relative = f"{group.replace('.', '/')}/{artifact_name}/{version}/{file_name}"
    base_url = library.get("url", "https://libraries.minecraft.net/")
    return {"path": relative, "url": base_url.rstrip("/") + "/" + relative}


def _collect_install_items(metadata: dict, task_id: str):
    os_name, arch = _detect_os(), _detect_arch()
    items = []
    native_archives = []
    classpath = []

    client = metadata.get("downloads", {}).get("client")
    if not client or not isinstance(client.get("url"), str):
        raise LauncherError("此版本没有可下载的官方客户端文件。")
    items.append((
        "client",
        client["url"],
        client.get("sha1"),
        client.get("size"),
        VERSIONS_DIR / metadata["id"] / f"{metadata['id']}.jar",
    ))

    for library in metadata.get("libraries", []):
        if not isinstance(library, dict) or not _is_allowed(library, os_name, arch):
            continue
        artifact = _library_artifact(library)
        if artifact:
            _validate_url(artifact["url"])
            artifact_path = _safe_path(LIBRARIES_DIR, artifact["path"])
            items.append(("library", artifact["url"], artifact.get("sha1"), artifact.get("size"), artifact_path))
            artifact_name = artifact.get("path", "").rsplit("/", 1)[-1]
            is_native = "-natives-" in artifact_name or artifact_name.endswith("-natives.jar")
            if is_native:
                native_archives.append(artifact_path)
            else:
                classpath.append(artifact_path)
        natives = library.get("natives", {})
        classifier_key = natives.get(os_name)
        if os_name == "osx":
            classifier_key = classifier_key.replace("${arch}", "arm64" if arch == "arm64" else "64") if classifier_key else None
        elif classifier_key:
            classifier_key = classifier_key.replace("${arch}", "32" if arch == "x86" else "64")
        classifier = library.get("downloads", {}).get("classifiers", {}).get(classifier_key) if classifier_key else None
        if classifier and isinstance(classifier.get("path"), str) and isinstance(classifier.get("url"), str):
            _validate_url(classifier["url"])
            native_path = _safe_path(LIBRARIES_DIR, classifier["path"])
            items.append(("native", classifier["url"], classifier.get("sha1"), classifier.get("size"), native_path))
            native_archives.append(native_path)

    asset_index = metadata.get("assetIndex")
    if not isinstance(asset_index, dict) or not is_sha1(asset_index.get("sha1")):
        raise LauncherError("此版本缺少有效的资源索引。")
    index_path = ASSETS_DIR / "indexes" / f"{asset_index['id']}.json"
    items.append(("asset_index", asset_index["url"], asset_index["sha1"], asset_index.get("size"), index_path))

    logging_file = metadata.get("logging", {}).get("client", {}).get("file")
    if logging_file and isinstance(logging_file.get("url"), str):
        log_path = ASSETS_DIR / "log_configs" / logging_file.get("id", "client.xml")
        items.append(("logging", logging_file["url"], logging_file.get("sha1"), logging_file.get("size"), log_path))

    return items, native_archives, classpath, asset_index, index_path


def is_sha1(value):
    return isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{40}", value) is not None


def _download_one(task_id: str, item: tuple):
    kind, url, sha1, size, destination = item
    _cancel_point(task_id)
    if destination.exists() and sha1 and _sha1_file(destination) == sha1.lower():
        if size is None or destination.stat().st_size == size:
            if size:
                _add_task_bytes(task_id, size)
            _complete_task_item(task_id)
            return
    if destination.exists():
        destination.unlink(missing_ok=True)
    _update_task(task_id, stage=f"下载{_kind_name(kind)}：{destination.name}")
    _download_file(url, destination, sha1, size, task_id)
    _complete_task_item(task_id)


def _kind_name(kind: str):
    return {
        "client": "客户端",
        "library": "游戏依赖",
        "native": "原生库",
        "asset_index": "资源索引",
        "asset": "游戏资源",
        "logging": "日志配置",
        "java": "Java 运行时",
    }.get(kind, "文件")


def _sha1_file(path: Path):
    digest = hashlib.sha1()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_natives(archives: list[Path], destination: Path, task_id: str):
    destination.mkdir(parents=True, exist_ok=True)
    for archive in archives:
        _cancel_point(task_id)
        if not archive.exists():
            raise LauncherError(f"原生库文件缺失：{archive.name}")
        with zipfile.ZipFile(archive) as source:
            for name in source.namelist():
                if name.startswith("META-INF/") or name.endswith("/"):
                    continue
                target = _safe_path(destination, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(name) as input_file, target.open("wb") as output_file:
                    shutil.copyfileobj(input_file, output_file)


def _build_virtual_assets(index_data: dict, asset_index_id: str, task_id: str):
    if not index_data.get("virtual") and not index_data.get("map_to_resources"):
        return
    virtual_root = ASSETS_DIR / "virtual" / asset_index_id
    resource_root = ASSETS_DIR / "resources" if index_data.get("map_to_resources") else virtual_root
    for name, asset in index_data.get("objects", {}).items():
        _cancel_point(task_id)
        digest = asset.get("hash")
        source = ASSETS_DIR / "objects" / digest[:2] / digest
        destination = _safe_path(resource_root, name)
        if not source.is_file():
            raise LauncherError(f"缺少游戏资源：{name}")
        if destination.exists() and destination.stat().st_size == source.stat().st_size:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _install_version(task_id: str, version_id: str):
    try:
        _update_task(task_id, status="running", stage="读取官方版本信息")
        metadata, _ = _version_metadata(version_id)
        _cancel_point(task_id)

        items, native_archives, classpath, asset_index, index_path = _collect_install_items(metadata, task_id)
        known_bytes = sum(item[3] for item in items if isinstance(item[3], int))
        _update_task(
            task_id,
            total=len(items),
            completed=0,
            bytes=0,
            totalBytes=known_bytes,
            stage="下载 Minecraft 客户端和依赖",
        )
        VERSIONS_DIR.joinpath(version_id).mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_download_one, task_id, item) for item in items]
            for future in as_completed(futures):
                _cancel_point(task_id)
                future.result()

        _cancel_point(task_id)
        if not index_path.exists():
            raise LauncherError("资源索引下载后不存在。")
        index_data = _read_json(index_path)
        objects = index_data.get("objects", {})
        asset_items = []
        for name, asset in objects.items():
            asset_hash = asset.get("hash")
            if not is_sha1(asset_hash):
                raise LauncherError(f"资源索引包含无效哈希：{name}")
            asset_path = ASSETS_DIR / "objects" / asset_hash[:2] / asset_hash
            asset_url = f"https://resources.download.minecraft.net/{asset_hash[:2]}/{asset_hash}"
            asset_items.append(("asset", asset_url, asset_hash, asset.get("size"), asset_path))

        with _task_lock:
            task = _tasks[task_id]
            task["total"] += len(asset_items)
            task["totalBytes"] += sum(item[3] for item in asset_items if isinstance(item[3], int))
            task["stage"] = "下载游戏资源文件"
        with ThreadPoolExecutor(max_workers=48) as pool:
            futures = [pool.submit(_download_one, task_id, item) for item in asset_items]
            for future in as_completed(futures):
                _cancel_point(task_id)
                future.result()

        _cancel_point(task_id)
        _update_task(task_id, stage="解压平台原生库")
        _extract_natives(native_archives, VERSIONS_DIR / version_id / "natives", task_id)
        _update_task(task_id, stage="整理游戏资源")
        _build_virtual_assets(index_data, asset_index["id"], task_id)
        version_file = VERSIONS_DIR / version_id / f"{version_id}.json"
        version_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        _update_task(task_id, status="completed", stage="完整安装完成")
    except Exception as error:
        cancelled = _task_cancelled(task_id)
        _update_task(
            task_id,
            status="cancelled" if cancelled else "failed",
            stage="已取消" if cancelled else "安装失败",
            error=None if cancelled else str(error),
        )


def _start_install(version_id: str):
    task_id = _new_task("install", version_id)
    thread = threading.Thread(target=_install_version, args=(task_id, version_id), daemon=True)
    thread.start()
    return task_id


def _installed_versions():
    installed = []
    if not VERSIONS_DIR.exists():
        return installed
    for version_folder in VERSIONS_DIR.iterdir():
        if not version_folder.is_dir():
            continue
        version_id = version_folder.name
        metadata_file = version_folder / f"{version_id}.json"
        client_file = version_folder / f"{version_id}.jar"
        if metadata_file.is_file() and client_file.is_file():
            installed.append({"id": version_id, "javaVersion": _read_json(metadata_file, {}).get("javaVersion")})
    return sorted(installed, key=lambda version: version["id"], reverse=True)


def _installed_java():
    installed = []
    if not JAVA_DIR.is_dir():
        return installed
    for runtime_root in JAVA_DIR.iterdir():
        metadata_file = runtime_root / "runtime.json"
        if not runtime_root.is_dir() or not metadata_file.is_file():
            continue
        metadata = _read_json(metadata_file, {})
        if not isinstance(metadata, dict):
            continue
        relative_executable = metadata.get("javaPath")
        if not isinstance(relative_executable, str):
            continue
        executable = _safe_path(runtime_root, relative_executable)
        java = _java_info(str(executable))
        if java and java["major"] == metadata.get("major"):
            installed.append({
                "major": java["major"],
                "version": java["version"],
                "component": metadata.get("component"),
                "path": java["path"],
            })
    return installed


def _select_java(required_major: int | None):
    candidates = []
    system_java = _java_info()
    if system_java:
        candidates.append(system_java)
    candidates.extend(_installed_java())
    compatible = [
        java for java in candidates
        if required_major is None or java["major"] >= required_major
    ]
    if not compatible:
        return None
    return max(compatible, key=lambda java: java["major"])


def _runtime_platform_key():
    os_name, arch = _detect_os(), _detect_arch()
    platforms = {
        ("osx", "arm64"): "mac-os-arm64",
        ("osx", "x86_64"): "mac-os",
        ("windows", "arm64"): "windows-arm64",
        ("windows", "x86_64"): "windows-x64",
        ("windows", "x86"): "windows-x86",
        ("linux", "x86_64"): "linux",
        ("linux", "x86"): "linux-i386",
    }
    key = platforms.get((os_name, arch))
    if not key:
        raise LauncherError(f"Mojang 暂未提供此平台的 Java 运行时：{os_name} {arch}")
    return key


def _runtime_manifest(major: int, component: str):
    if not isinstance(major, int) or not 8 <= major <= 99:
        raise LauncherError("Java 主版本号无效。")
    if component not in {
        "jre-legacy",
        "java-runtime-alpha",
        "java-runtime-beta",
        "java-runtime-gamma",
        "java-runtime-delta",
        "java-runtime-epsilon",
        "java-runtime-gamma-snapshot",
    }:
        raise LauncherError("Minecraft 元数据中的 Java 运行时类型无效。")
    index, _ = _cached_official_json(
        JAVA_RUNTIME_INDEX_URL,
        METADATA_DIR / "java-runtime-index.json",
    )
    if not isinstance(index, dict):
        raise LauncherError("Mojang Java 运行时总清单格式无效。")
    platform_runtimes = index.get(_runtime_platform_key(), {})
    if not isinstance(platform_runtimes, dict):
        raise LauncherError("Mojang Java 运行时清单中没有此平台。")
    runtimes = platform_runtimes.get(component, [])
    matching = []
    for runtime in runtimes:
        runtime_version = runtime.get("version", {}).get("name", "")
        version_match = re.match(r"(?:1\.)?(\d+)", runtime_version)
        if version_match and int(version_match.group(1)) == major:
            matching.append(runtime)
    if not matching:
        raise LauncherError(f"Mojang 官方清单没有适用于此设备的 Java {major} 运行时。")
    selected = max(
        matching,
        key=lambda runtime: runtime.get("version", {}).get("released", ""),
    )
    manifest_info = selected.get("manifest", {})
    if (
        not is_sha1(manifest_info.get("sha1"))
        or not isinstance(manifest_info.get("url"), str)
        or not isinstance(manifest_info.get("size"), int)
    ):
        raise LauncherError("Mojang Java 运行时清单信息无效。")
    manifest, _ = _cached_official_json(
        manifest_info["url"],
        METADATA_DIR / f"java-runtime-{manifest_info['sha1']}.json",
        manifest_info["sha1"],
    )
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise LauncherError("Mojang Java 文件清单为空或格式无效。")
    return selected, files


def _extract_runtime_links(files: dict, staging: Path):
    for relative, entry in files.items():
        if entry.get("type") == "directory":
            _safe_path(staging, relative).mkdir(parents=True, exist_ok=True)
    for relative, entry in files.items():
        if entry.get("type") != "link":
            continue
        link_path = _safe_path(staging, relative)
        target = entry.get("target")
        if not isinstance(target, str):
            raise LauncherError(f"Java 清单中的链接目标无效：{relative}")
        target_path = PurePosixPath(target)
        resolved_target = (link_path.parent / Path(*target_path.parts)).resolve()
        if resolved_target != staging.resolve() and staging.resolve() not in resolved_target.parents:
            raise LauncherError(f"Java 清单包含越界链接：{relative}")
        link_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            link_path.symlink_to(target, target_is_directory=resolved_target.is_dir())
        except OSError:
            if not resolved_target.is_file():
                raise LauncherError(f"无法创建 Java 运行时链接：{relative}")
            shutil.copyfile(resolved_target, link_path)


def _install_java(task_id: str, major: int, component: str):
    staging = JAVA_DIR / f".install-{task_id}"
    try:
        _update_task(task_id, status="running", stage="读取 Mojang Java 运行时清单")
        selected, files = _runtime_manifest(major, component)
        runtime_name = selected.get("version", {}).get("name", "")
        safe_runtime_name = re.sub(r"[^A-Za-z0-9._-]", "_", runtime_name)
        runtime_root = JAVA_DIR / f"{component}-{major}-{safe_runtime_name}"
        if (runtime_root / "runtime.json").is_file():
            info = _read_json(runtime_root / "runtime.json", {})
            java_path = info.get("javaPath")
            executable = _safe_path(runtime_root, java_path) if isinstance(java_path, str) else None
            java = _java_info(str(executable)) if executable else None
            if java and java["major"] == major:
                _update_task(task_id, status="completed", stage=f"Java {major} 已准备就绪")
                return
            raise LauncherError("已存在不完整的 Java 运行时目录，请先移除该目录后重试。")

        staging.mkdir(parents=True, exist_ok=False)
        items = []
        executable_candidates = []
        for relative, entry in files.items():
            destination = _safe_path(staging, relative)
            file_type = entry.get("type")
            if file_type == "directory":
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if file_type == "link":
                target = entry.get("target")
                if isinstance(target, str) and relative.replace("\\", "/").endswith(("/bin/java", "/bin/java.exe")):
                    executable_candidates.append(relative)
                continue
            if file_type != "file":
                raise LauncherError(f"Java 清单包含不支持的文件类型：{relative}")
            raw = entry.get("downloads", {}).get("raw", {})
            if (
                not isinstance(raw.get("url"), str)
                or not is_sha1(raw.get("sha1"))
                or not isinstance(raw.get("size"), int)
                or raw["size"] < 0
            ):
                raise LauncherError(f"Java 清单文件信息无效：{relative}")
            _validate_url(raw["url"])
            items.append(("java", raw["url"], raw["sha1"], raw["size"], destination))
            if relative.replace("\\", "/").endswith(("/bin/java", "/bin/java.exe")):
                executable_candidates.append(relative)

        total_bytes = sum(item[3] for item in items)
        _update_task(
            task_id,
            total=len(items),
            completed=0,
            bytes=0,
            totalBytes=total_bytes,
            stage=f"下载 Mojang Java {major}",
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_download_one, task_id, item) for item in items]
            for future in as_completed(futures):
                _cancel_point(task_id)
                future.result()
        _cancel_point(task_id)
        _extract_runtime_links(files, staging)

        executable_relative = next(
            (path for path in executable_candidates if _safe_path(staging, path).is_file()),
            None,
        )
        if not executable_relative:
            raise LauncherError("Java 运行时中找不到可执行的 java 程序。")
        executable = _safe_path(staging, executable_relative)
        for relative, entry in files.items():
            if entry.get("type") == "file" and entry.get("executable"):
                file_path = _safe_path(staging, relative)
                file_path.chmod(file_path.stat().st_mode | 0o111)
        java = _java_info(str(executable))
        if not java or java["major"] != major:
            raise LauncherError(f"下载的 Java 运行时版本不匹配，预期 Java {major}。")
        _write_json(staging / "runtime.json", {
            "major": major,
            "component": component,
            "version": java["version"],
            "javaPath": executable_relative,
        })
        _cancel_point(task_id)
        runtime_root.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(runtime_root)
        _update_task(task_id, status="completed", stage=f"Java {major} 下载并安装完成")
    except Exception as error:
        cancelled = _task_cancelled(task_id)
        if staging.is_dir():
            shutil.rmtree(staging)
        _update_task(
            task_id,
            status="cancelled" if cancelled else "failed",
            stage="已取消" if cancelled else "Java 安装失败",
            error=None if cancelled else str(error),
        )


def _start_java_install(major: int, component: str):
    task_id = _new_task("java", f"Java {major}")
    thread = threading.Thread(target=_install_java, args=(task_id, major, component), daemon=True)
    thread.start()
    return task_id


def _api_system():
    config = _load_config()
    return {
        "platform": platform.system(),
        "architecture": platform.machine(),
        "java": _java_info(),
        "javaRuntimes": _installed_java(),
        "dataDirectory": str(DATA_DIR),
        "clientIdConfigured": bool(config.get("microsoftClientId")),
        "profile": _profile_summary(),
        "installedVersions": _installed_versions(),
    }


def _post_json(url: str, body: dict, headers: dict | None = None):
    encoded = json.dumps(body).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "User-Agent": "MCPCL/0.1"}
    if headers:
        request_headers.update(headers)
    request = Request(url, data=encoded, headers=request_headers, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except HTTPError as error:
        try:
            detail = error.read().decode("utf-8", errors="replace")
        except OSError:
            detail = str(error)
        raise LauncherError(f"微软/Xbox 服务请求失败（HTTP {error.code}）：{detail[:300]}") from error
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise LauncherError(f"微软/Xbox 服务连接失败：{error}") from error


def _auth_start():
    global _auth_flow
    client_id = _load_config().get("microsoftClientId", "")
    if not re.fullmatch(r"[A-Fa-f0-9-]{36}", client_id):
        raise LauncherError("请先在启动设置中填写你注册的 Microsoft OAuth 公共客户端 ID。")
    body = urlencode({
        "client_id": client_id,
        "scope": "XboxLive.signin offline_access",
    }).encode("utf-8")
    request = Request(
        MICROSOFT_DEVICE_CODE_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "MCPCL/0.1"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            flow = json.loads(response.read())
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise LauncherError(f"Microsoft 登录初始化失败（HTTP {error.code}）：{detail[:300]}") from error
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise LauncherError(f"Microsoft 登录初始化失败：{error}") from error
    with _auth_lock:
        _auth_flow = {
            "client_id": client_id,
            "device_code": flow["device_code"],
            "interval": int(flow.get("interval", 5)),
            "expires_at": time.time() + int(flow.get("expires_in", 900)),
        }
    return {
        "userCode": flow.get("user_code"),
        "verificationUri": flow.get("verification_uri"),
        "message": flow.get("message"),
        "expiresIn": flow.get("expires_in"),
        "interval": flow.get("interval", 5),
    }


def _token_form_request(body: dict):
    encoded = "&".join(f"{key}={_url_quote(str(value))}" for key, value in body.items()).encode("utf-8")
    request = Request(
        MICROSOFT_TOKEN_URL,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "MCPCL/0.1"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        detail = json.loads(error.read().decode("utf-8", errors="replace") or "{}")
        code = detail.get("error")
        if code in {"authorization_pending", "slow_down"}:
            return {"pending": True, "slowDown": code == "slow_down"}
        if code == "authorization_declined":
            raise LauncherError("你已取消 Microsoft 登录。") from error
        if code == "expired_token":
            raise LauncherError("登录验证码已过期，请重新开始登录。") from error
        raise LauncherError(f"Microsoft 登录失败：{detail.get('error_description', code)}") from error
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise LauncherError(f"Microsoft 登录请求失败：{error}") from error
    return result


def _url_quote(value: str):
    return quote(value, safe="")


def _finish_minecraft_login(ms_tokens: dict):
    xbl = _post_json(XBOX_AUTH_URL, {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"d={ms_tokens['access_token']}",
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    })
    user_hash = xbl.get("DisplayClaims", {}).get("xui", [{}])[0].get("uhs")
    xsts = _post_json(XBOX_XSTS_URL, {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
        "RelyingParty": "http://xboxlive.com",
        "TokenType": "JWT",
    })
    xsts_hash = xsts.get("DisplayClaims", {}).get("xui", [{}])[0].get("uhs")
    xsts_claims = xsts.get("DisplayClaims", {}).get("xui", [{}])[0]
    if not user_hash or not xsts_hash or user_hash != xsts_hash:
        raise LauncherError("Xbox 用户身份校验失败。")
    minecraft = _post_json(MINECRAFT_LOGIN_URL, {
        "identityToken": f"XBL3.0 x={user_hash};{xsts['Token']}",
    })
    mc_token = minecraft.get("access_token")
    if not mc_token:
        raise LauncherError("Minecraft 服务未返回游戏访问令牌。")

    auth_request = Request(MINECRAFT_ENTITLEMENTS_URL, headers={"Authorization": f"Bearer {mc_token}"})
    try:
        with urlopen(auth_request, timeout=30) as response:
            entitlements = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise LauncherError(f"无法验证 Minecraft 游戏授权：{error}") from error
    if not entitlements.get("items"):
        raise LauncherError("此 Microsoft 账号没有可用的 Minecraft Java 游戏授权。")

    profile_request = Request(MINECRAFT_PROFILE_URL, headers={"Authorization": f"Bearer {mc_token}"})
    try:
        with urlopen(profile_request, timeout=30) as response:
            profile = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise LauncherError(f"无法读取 Minecraft 玩家资料：{error}") from error
    if not profile.get("id") or not profile.get("name"):
        raise LauncherError("该账号尚未创建 Minecraft Java 玩家资料。")

    auth = {
        "profile": {"id": profile["id"], "name": profile["name"], "xuid": xsts_claims.get("xid", "")},
        "minecraftAccessToken": mc_token,
        "minecraftExpiresAt": time.time() + int(minecraft.get("expires_in", 3600)),
        "microsoftRefreshToken": ms_tokens.get("refresh_token"),
        "microsoftClientId": _load_config().get("microsoftClientId"),
    }
    _write_json(AUTH_FILE, auth)
    return auth["profile"]


def _auth_poll():
    global _auth_flow
    with _auth_lock:
        flow = _auth_flow
    if not flow:
        raise LauncherError("当前没有等待中的 Microsoft 登录。")
    if time.time() >= flow["expires_at"]:
        with _auth_lock:
            _auth_flow = None
        raise LauncherError("登录验证码已过期，请重新开始登录。")

    result = _token_form_request({
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": flow["client_id"],
        "device_code": flow["device_code"],
    })
    if result.get("pending"):
        if result.get("slowDown"):
            flow["interval"] += 5
        return {"pending": True, "interval": flow["interval"]}
    profile = _finish_minecraft_login(result)
    with _auth_lock:
        _auth_flow = None
    return {"pending": False, "profile": profile}


def _refresh_minecraft_token(auth: dict):
    if auth.get("minecraftAccessToken") and auth.get("minecraftExpiresAt", 0) > time.time() + 60:
        return auth
    refresh_token = auth.get("microsoftRefreshToken")
    client_id = auth.get("microsoftClientId") or _load_config().get("microsoftClientId")
    if not refresh_token or not client_id:
        raise LauncherError("请先使用 Microsoft 账号登录 Minecraft Java 版。")
    tokens = _token_form_request({
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "XboxLive.signin offline_access",
    })
    if tokens.get("pending"):
        raise LauncherError("Microsoft 账号授权需要重新登录。")
    profile = _finish_minecraft_login(tokens)
    updated = _load_auth()
    if updated.get("profile") != profile:
        raise LauncherError("刷新后的 Minecraft 玩家资料不一致，请重新登录。")
    return updated


def _launch_arguments(metadata: dict, version_id: str, auth: dict):
    required_java = metadata.get("javaVersion", {}).get("majorVersion")
    java = _select_java(required_java if isinstance(required_java, int) else None)
    if not java:
        if isinstance(required_java, int):
            raise LauncherError(
                f"Minecraft {version_id} 需要 Java {required_java} 或更高版本；"
                "请在下载中心下载兼容的 Java 运行时。"
            )
        raise LauncherError("未找到 Java。请在下载中心下载兼容的 Java 运行时。")
    if isinstance(required_java, int) and java["major"] < required_java:
        raise LauncherError(
            f"Minecraft {version_id} 需要 Java {required_java} 或更高版本；"
            f"当前检测到 Java {java['major']}。"
        )

    version_dir = VERSIONS_DIR / version_id
    client_jar = version_dir / f"{version_id}.jar"
    if not client_jar.is_file():
        raise LauncherError("此版本尚未完整安装，请先在下载中心执行完整安装。")
    os_name, arch = _detect_os(), _detect_arch()
    classpath = []
    natives = version_dir / "natives"
    for library in metadata.get("libraries", []):
        if not isinstance(library, dict) or not _is_allowed(library, os_name, arch):
            continue
        artifact = _library_artifact(library)
        if artifact and not artifact.get("path", "").endswith("-natives.jar"):
            path = _safe_path(LIBRARIES_DIR, artifact["path"])
            if not path.is_file():
                raise LauncherError(f"缺少游戏依赖：{artifact['path']}，请重新安装版本。")
            classpath.append(str(path))
    classpath.append(str(client_jar))

    asset_index = metadata.get("assetIndex", {}).get("id", "")
    if not asset_index:
        raise LauncherError("版本元数据中没有资源索引。")
    profile = auth["profile"]
    values = {
        "auth_player_name": profile["name"],
        "version_name": version_id,
        "game_directory": str(GAME_DIR),
        "assets_root": str(ASSETS_DIR),
        "assets_index_name": asset_index,
        "auth_uuid": profile["id"],
        "auth_access_token": auth["minecraftAccessToken"],
        "auth_session": f"token:{auth['minecraftAccessToken']}:{profile['id']}",
        "user_type": "msa",
        "version_type": metadata.get("type", "release"),
        "user_properties": "{}",
        "auth_xuid": profile.get("xuid", ""),
        "clientid": auth.get("microsoftClientId") or _load_config().get("microsoftClientId", ""),
        "resolution_width": "1280",
        "resolution_height": "720",
        "natives_directory": str(natives),
        "launcher_name": "MCPCL",
        "launcher_version": "0.1",
        "classpath": os.pathsep.join(classpath),
        "library_directory": str(LIBRARIES_DIR),
        "classpath_separator": os.pathsep,
        "library_directory": str(LIBRARIES_DIR),
        "assets_root": str(ASSETS_DIR),
        "game_assets": str(
            ASSETS_DIR / "resources"
            if metadata.get("assetIndex", {}).get("map_to_resources")
            else ASSETS_DIR / "virtual" / asset_index
        ),
        "version_name": version_id,
    }
    logging = metadata.get("logging", {}).get("client", {}).get("argument")
    logging_file = metadata.get("logging", {}).get("client", {}).get("file", {}).get("id")
    if logging and logging_file:
        values["path"] = str(ASSETS_DIR / "log_configs" / logging_file)

    def replace(value: str):
        return re.sub(r"\$\{([^}]+)}", lambda match: values.get(match.group(1), ""), value)

    def apply_arguments(entries):
        result = []
        for entry in entries:
            if isinstance(entry, str):
                result.append(replace(entry))
            elif isinstance(entry, dict) and _is_allowed(entry, os_name, arch):
                value = entry.get("value", [])
                for argument in value if isinstance(value, list) else [value]:
                    result.append(replace(argument))
        return result

    arguments = metadata.get("arguments", {})
    game_args = apply_arguments(arguments.get("game", []))
    jvm_args = apply_arguments(arguments.get("jvm", []))
    if not arguments and metadata.get("minecraftArguments"):
        import shlex

        game_args = [replace(argument) for argument in shlex.split(metadata["minecraftArguments"])]
        jvm_args = [
            f"-Djava.library.path={natives}",
            f"-Dminecraft.launcher.brand=MCPCL",
            f"-Dminecraft.launcher.version=0.1",
            "-cp",
            os.pathsep.join(classpath),
        ]
    memory = 4
    jvm_user_args = ""
    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        memory = max(2, min(16, int(settings.get("memory", 4))))
        jvm_user_args = settings.get("jvmArguments", "")
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    jvm_args.extend([f"-Xmx{memory}G", f"-Xms{min(2, memory)}G"])
    if jvm_user_args:
        jvm_args.extend(line.strip() for line in jvm_user_args.splitlines() if line.strip())

    command = [java["path"], *jvm_args, metadata.get("mainClass", "net.minecraft.client.main.Main"), *game_args]
    return command


def _start_launch(version_id: str):
    task_id = _new_task("launch", version_id)
    thread = threading.Thread(target=_launch_game, args=(task_id, version_id), daemon=True)
    thread.start()
    return task_id


def _launch_game(task_id: str, version_id: str):
    try:
        _update_task(task_id, status="running", stage="检查 Java 与账号授权")
        auth = _refresh_minecraft_token(_load_auth())
        version_file = VERSIONS_DIR / version_id / f"{version_id}.json"
        metadata = _read_json(version_file)
        if not isinstance(metadata, dict):
            raise LauncherError("找不到已安装的版本信息，请先完成安装。")
        command = _launch_arguments(metadata, version_id, auth)
        log_path = DATA_DIR / "logs" / f"game-{version_id}-{int(time.time())}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        output = log_path.open("ab")
        process = subprocess.Popen(
            command,
            cwd=GAME_DIR,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=(platform.system() != "Windows"),
        )
        output.close()
        _update_task(
            task_id,
            status="completed",
            stage="游戏已启动",
            processId=process.pid,
            logFile=str(log_path),
        )
    except Exception as error:
        _update_task(task_id, status="failed", stage="启动失败", error=str(error))


class LauncherHandler(SimpleHTTPRequestHandler):
    server_version = "MCPCL/0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[MCPCL] {self.address_string()} {fmt % args}\n")

    def _send_json(self, status: int, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise LauncherError("请求长度无效。") from error
        if length > 16_384:
            raise LauncherError("请求内容过大。")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as error:
            raise LauncherError("请求 JSON 格式无效。") from error

    def _check_origin(self):
        host = self.headers.get("Host", "").split(":")[0].strip("[]").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            self._send_json(403, {"error": "仅允许本机访问。"})
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                self._send_json(403, {"error": "跨站请求被拒绝。"})
                return False
        return True

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path.startswith("/api/"):
            if not self._check_origin():
                return
            try:
                if path == "/api/system":
                    return self._send_json(200, _api_system())
                if path == "/api/catalog/versions":
                    manifest, stale = _catalog_manifest()
                    return self._send_json(200, {"manifest": manifest, "cached": stale})
                if path.startswith("/api/catalog/versions/"):
                    version_id = path.rsplit("/", 1)[-1]
                    metadata, stale = _version_metadata(version_id)
                    return self._send_json(200, {"metadata": metadata, "cached": stale})
                if path == "/api/installed":
                    return self._send_json(200, {"versions": _installed_versions()})
                if path.startswith("/api/tasks/"):
                    task_id = path.rsplit("/", 1)[-1]
                    with _task_lock:
                        task = _tasks.get(task_id)
                        if not task:
                            return self._send_json(404, {"error": "找不到此任务。"})
                        return self._send_json(200, _public_task(task))
                if path == "/api/auth/profile":
                    return self._send_json(200, {"profile": _profile_summary()})
                if path == "/api/config":
                    config = _load_config()
                    return self._send_json(200, {"clientId": config.get("microsoftClientId", "")})
                if path == "/api/settings":
                    return self._send_json(200, _read_json(SETTINGS_FILE, {"memory": 4, "jvmArguments": ""}))
                return self._send_json(404, {"error": "未找到此 API。"})
            except LauncherError as error:
                return self._send_json(400, {"error": str(error)})
        return super().do_GET()

    def do_POST(self):
        if not self._check_origin():
            return
        path = unquote(urlparse(self.path).path)
        try:
            body = self._read_body()
            if path == "/api/config":
                client_id = body.get("clientId", "").strip()
                if client_id and not re.fullmatch(r"[A-Fa-f0-9-]{36}", client_id):
                    return self._send_json(400, {"error": "Microsoft OAuth 客户端 ID 格式无效。"})
                config = _read_json(CONFIG_FILE, {})
                if not isinstance(config, dict):
                    config = {}
                if os.environ.get("MC_PCL_CLIENT_ID"):
                    return self._send_json(400, {"error": "当前通过 MC_PCL_CLIENT_ID 环境变量配置，不能在页面覆盖。"})
                config["microsoftClientId"] = client_id
                _write_json(CONFIG_FILE, config)
                return self._send_json(200, {"ok": True})
            if path == "/api/settings":
                memory = body.get("memory")
                jvm_arguments = body.get("jvmArguments", "")
                if not isinstance(memory, int) or not 2 <= memory <= 16:
                    return self._send_json(400, {"error": "游戏内存必须在 2–16 GB 之间。"})
                if not isinstance(jvm_arguments, str) or len(jvm_arguments) > 1000:
                    return self._send_json(400, {"error": "Java 参数格式无效。"})
                arguments = [line.strip() for line in jvm_arguments.splitlines() if line.strip()]
                if any(not argument.startswith("-") or re.search(r"\s", argument) for argument in arguments):
                    return self._send_json(400, {"error": "每行必须是一个以 - 开头且不含空格的 Java 参数。"})
                _write_json(SETTINGS_FILE, {"memory": memory, "jvmArguments": jvm_arguments})
                return self._send_json(200, {"ok": True})
            if path == "/api/install":
                version_id = body.get("version")
                if not isinstance(version_id, str):
                    return self._send_json(400, {"error": "请选择 Minecraft 版本。"})
                task_id = _start_install(version_id)
                return self._send_json(202, {"taskId": task_id})
            if path == "/api/java/install":
                version_id = body.get("version")
                if not isinstance(version_id, str):
                    return self._send_json(400, {"error": "请选择需要匹配 Java 的 Minecraft 版本。"})
                metadata, _ = _version_metadata(version_id)
                java_version = metadata.get("javaVersion", {})
                major = java_version.get("majorVersion")
                component = java_version.get("component")
                if not isinstance(major, int) or not isinstance(component, str):
                    return self._send_json(400, {"error": "所选版本没有有效的 Java 运行时要求。"})
                task_id = _start_java_install(major, component)
                return self._send_json(202, {"taskId": task_id})
            if path == "/api/launch":
                version_id = body.get("version")
                if not isinstance(version_id, str):
                    return self._send_json(400, {"error": "请选择已安装版本。"})
                task_id = _start_launch(version_id)
                return self._send_json(202, {"taskId": task_id})
            if path == "/api/auth/start":
                return self._send_json(200, _auth_start())
            if path == "/api/auth/poll":
                return self._send_json(200, _auth_poll())
            return self._send_json(404, {"error": "未找到此 API。"})
        except LauncherError as error:
            return self._send_json(400, {"error": str(error)})
        except (KeyError, TypeError, ValueError) as error:
            return self._send_json(400, {"error": f"请求内容无效：{error}"})

    def do_DELETE(self):
        if not self._check_origin():
            return
        path = unquote(urlparse(self.path).path)
        if path.startswith("/api/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
            with _task_lock:
                task = _tasks.get(task_id)
                if not task:
                    return self._send_json(404, {"error": "找不到此任务。"})
                task["_cancel"].set()
                return self._send_json(200, {"ok": True})
        if path == "/api/auth/profile":
            AUTH_FILE.unlink(missing_ok=True)
            return self._send_json(200, {"ok": True})
        self._send_json(404, {"error": "未找到此 API。"})


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        log_path = DATA_DIR / "logs" / "launcher.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log_file
    port = int(os.environ.get("MC_PCL_PORT", _free_port()))
    server = ThreadingHTTPServer(("127.0.0.1", port), LauncherHandler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"方块启动器运行中：{url}")
    print(f"游戏目录：{GAME_DIR}")
    print("按 Ctrl+C 退出。")
    if not os.environ.get("MC_PCL_NO_BROWSER"):
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
