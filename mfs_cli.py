#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MFS 影视终端
===========
命令行影视搜索与播放客户端，基于苹果CMS V10 API规范。
支持多API源聚合搜索、自定义源管理。

Author: Magic
Version: 1.1.3
License: MIT

Usage:
    python mfs_cli.py search <keyword> [--page N]
    python mfs_cli.py detail <vod_id> --source <source_name>
    python mfs_cli.py play <vod_id> [--episode N] [--player browser|mpv|vlc] --source <source_name>
    python mfs_cli.py history
    python mfs_cli.py config
    python mfs_cli.py add-source <name> <url>
    python mfs_cli.py remove-source <name>
    python mfs_cli.py list-sources
    python mfs_cli.py toggle-source <name>
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, unquote, urlparse

import click
import httpx
from loguru import logger
from pydantic import BaseModel, Field, field_validator, ValidationError
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.align import Align
from rich.text import Text

# =============================================================================
# 常量与配置
# =============================================================================

APP_NAME: str = "hn4k-cli"
APP_VERSION: str = "1.1.0"
DEFAULT_API_BASE: str = "https://www.hongniuzy2.com/api.php/provide/vod/"
# 配置文件与脚本同目录，便于迁移与版本控制
_SCRIPT_DIR: Path = Path(__file__).resolve().parent
CONFIG_DIR: Path = _SCRIPT_DIR / "config"
CONFIG_FILE: Path = CONFIG_DIR / "config.json"
HISTORY_FILE: Path = CONFIG_DIR / "history.json"
LOG_FILE: Path = CONFIG_DIR / "app.log"

DEFAULT_CONFIG: Dict[str, Any] = {
    "api_sources": [
        {
            "name": "默认源",
            "url": DEFAULT_API_BASE,
            "enabled": True,
        }
    ],
    "default_player": "browser",   # browser | mpv | vlc
    "timeout": 15,
    "max_retries": 3,
    "page_size": 20,
    # 图像显示由 PowerShell ConvertTo-Sixel 处理，无需额外配置
    # 前提: pwsh 7.4+ + Microsoft.PowerShell.ConsoleGuiTools 模块
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}

# =============================================================================
# 自定义异常体系
# =============================================================================

class HN4KError(Exception):
    """根异常，所有业务异常的基类。"""
    pass


class NetworkError(HN4KError):
    """网络层异常（超时、DNS失败、连接重置等）。"""
    pass


class APIError(HN4KError):
    """API返回异常（非200状态码、JSON解析失败、业务码错误）。"""
    def __init__(self, message: str, status_code: Optional[int] = None, raw_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.raw_body = raw_body


class ParseError(HN4KError):
    """数据解析异常（字段缺失、格式不符）。"""
    pass


class PlayerError(HN4KError):
    """播放器调用异常（未安装、进程启动失败）。"""
    pass


# =============================================================================
# Pydantic 数据模型层
# =============================================================================

class VodItem(BaseModel):
    """影片列表项模型。"""
    vod_id: int = Field(..., description="影片唯一标识")
    vod_name: str = Field(..., description="影片名称")
    vod_pic: Optional[str] = Field(None, description="封面图URL")
    vod_remarks: Optional[str] = Field(None, description="更新备注/清晰度")
    vod_year: Optional[str] = Field(None, description="年份")
    vod_area: Optional[str] = Field(None, description="地区")
    vod_type: Optional[str] = Field(None, description="类型")
    vod_class: Optional[str] = Field(None, description="分类标签")
    vod_content: Optional[str] = Field(None, description="剧情简介")
    vod_actor: Optional[str] = Field(None, description="演员")
    vod_director: Optional[str] = Field(None, description="导演")
    vod_play_from: Optional[str] = Field(None, description="播放源标识")
    vod_play_url: Optional[str] = Field(None, description="原始播放地址串")
    source_name: Optional[str] = Field("default", description="所属API源名称")

    @field_validator("vod_content", mode="before")
    @classmethod
    def strip_html(cls, v: Any) -> Any:
        """去除简介中的HTML标签。"""
        if isinstance(v, str):
            return re.sub(r"<[^>]+>", "", v).strip()
        return v


class SearchResponse(BaseModel):
    """搜索/列表接口响应模型。"""
    code: int = Field(1, description="业务状态码，1为成功")
    msg: str = Field("数据返回成功", description="状态描述")
    page: int = Field(1, description="当前页码")
    pagecount: int = Field(0, description="总页数")
    limit: int = Field(20, description="每页条数")
    total: int = Field(0, description="总记录数")
    list: List[VodItem] = Field(default_factory=list, description="影片列表")

    @field_validator("list", mode="before")
    @classmethod
    def ensure_list(cls, v: Any) -> Any:
        """兼容空值或None返回空列表。"""
        if v is None:
            return []
        return v


class Episode(BaseModel):
    """解析后的单集模型。"""
    name: str = Field(..., description="集数名称，如'第01集'、'HD'")
    url: str = Field(..., description="播放直链或m3u8地址")
    source: str = Field("default", description="所属播放源")


class VodDetail(BaseModel):
    """影片详情（含解析后剧集，按播放源分组）。"""
    vod_id: int
    vod_name: str
    vod_pic: Optional[str] = None
    vod_remarks: Optional[str] = None
    vod_year: Optional[str] = None
    vod_area: Optional[str] = None
    vod_type: Optional[str] = None
    vod_actor: Optional[str] = None
    vod_director: Optional[str] = None
    vod_content: Optional[str] = None
    episodes: List[Episode] = Field(default_factory=list, description="全部剧集平铺列表")
    sources: List[str] = Field(default_factory=list, description="可用播放源名称列表")
    episodes_by_source: Dict[str, List[Episode]] = Field(
        default_factory=dict, description="按播放源分组的剧集映射"
    )
    source_name: Optional[str] = Field("default", description="所属API源名称")


# =============================================================================
# 配置管理
# =============================================================================

class ConfigManager:
    """本地JSON配置持久化管理器。"""

    def __init__(self) -> None:
        self._config: Dict[str, Any] = {}
        self._ensure_dirs()
        self._load()

    def _ensure_dirs(self) -> None:
        """确保配置目录存在。"""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        """从磁盘加载配置，缺失键自动合并默认值。兼容旧版 api_base 配置。"""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self._config = {**DEFAULT_CONFIG, **loaded}
                # 兼容迁移: 旧版单 api_base → 新版 api_sources 列表
                if "api_base" in self._config and "api_sources" not in loaded:
                    old_base = self._config.pop("api_base")
                    self._config["api_sources"] = [
                        {"name": "默认源", "url": old_base, "enabled": True}
                    ]
                    self.save()
                    logger.info(f"配置已自动迁移: api_base → api_sources")
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"配置文件损坏，使用默认配置: {exc}")
                self._config = DEFAULT_CONFIG.copy()
                self.save()
        else:
            self._config = DEFAULT_CONFIG.copy()
            self.save()

    def save(self) -> None:
        """持久化到磁盘。"""
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.error(f"配置保存失败: {exc}")

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._config[key] = value
        self.save()

    def all(self) -> Dict[str, Any]:
        return self._config.copy()

    # --- API 源管理 ---

    def add_source(self, name: str, url: str) -> None:
        """添加新API源。"""
        sources = self._config.get("api_sources", [])
        if any(s["name"] == name for s in sources):
            raise HN4KError(f"源名称 [{name}] 已存在")
        sources.append({"name": name, "url": url.rstrip("/") + "/", "enabled": True})
        self._config["api_sources"] = sources
        self.save()

    def remove_source(self, name: str) -> None:
        """删除指定API源。默认源不允许删除，仅可禁用。"""
        sources = self._config.get("api_sources", [])
        target = next((s for s in sources if s["name"] == name), None)
        if not target:
            raise HN4KError(f"未找到源: {name}")
        # 默认源（第一个源）不允许删除
        if sources and sources[0]["name"] == name:
            raise HN4KError(f"默认源 [{name}] 不允许删除，可使用 toggle-source 禁用")
        new_sources = [s for s in sources if s["name"] != name]
        self._config["api_sources"] = new_sources
        self.save()

    def toggle_source(self, name: str) -> bool:
        """启用/禁用指定API源，返回切换后的状态。"""
        sources = self._config.get("api_sources", [])
        for s in sources:
            if s["name"] == name:
                s["enabled"] = not s.get("enabled", True)
                self._config["api_sources"] = sources
                self.save()
                return s["enabled"]
        raise HN4KError(f"未找到源: {name}")

    def list_sources(self) -> List[Dict[str, Any]]:
        """返回所有API源列表。"""
        return list(self._config.get("api_sources", []))


# =============================================================================
# HTTP 客户端（服务层）
# =============================================================================

class CMSClient:
    """苹果CMS API HTTP客户端，含重试、超时、日志。支持多源聚合搜索。"""

    def __init__(self, config: ConfigManager):
        self.cfg = config
        self.api_sources: List[Dict[str, Any]] = config.get("api_sources", [])
        self.timeout: int = config.get("timeout", 15)
        self.max_retries: int = config.get("max_retries", 3)
        self.headers: Dict[str, str] = {
            "User-Agent": config.get("user_agent", DEFAULT_CONFIG["user_agent"]),
            "Accept": "application/json",
        }

    def _request(self, method: str, endpoint: str, base_url: str, **kwargs: Any) -> Any:
        """
        底层请求方法，含指数退避重试。

        :param base_url: API 基础地址
        :raises NetworkError: 网络层不可恢复错误
        :raises APIError: API返回异常
        """
        url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.debug(f"[{method}] {url} (attempt {attempt})")
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    resp = client.request(method, url, headers=self.headers, **kwargs)

                if resp.status_code != 200:
                    raise APIError(
                        f"HTTP {resp.status_code}",
                        status_code=resp.status_code,
                        raw_body=resp.text[:500],
                    )

                try:
                    data = resp.json()
                except json.JSONDecodeError as exc:
                    raise APIError(
                        f"JSON解析失败: {exc}",
                        status_code=resp.status_code,
                        raw_body=resp.text[:500],
                    ) from exc

                return data

            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
                last_exc = exc
                logger.warning(f"网络异常，第{attempt}次重试: {exc}")
                import time
                time.sleep(1.5 ** attempt)  # 指数退避

            except APIError:
                raise
            except Exception as exc:
                last_exc = exc
                logger.error(f"未预期异常: {exc}")
                raise NetworkError(f"请求失败: {exc}") from exc

        raise NetworkError(f"超过最大重试次数({self.max_retries}): {last_exc}")

    def _search_single(self, source: Dict[str, Any], keyword: str, page: int) -> SearchResponse:
        """单个API源的搜索。"""
        params = {
            "ac": "videolist",
            "wd": keyword,
            "pg": page,
        }
        raw = self._request("GET", "", base_url=source["url"], params=params)
        try:
            resp = SearchResponse.model_validate(raw)
            # 标记来源
            for item in resp.list:
                item.source_name = source["name"]
            return resp
        except ValidationError as exc:
            logger.error(f"源 [{source['name']}] 响应校验失败: {exc}")
            raise APIError(f"API响应结构异常: {exc}") from exc

    def search(self, keyword: str, page: int = 1) -> SearchResponse:
        """
        聚合搜索：并发请求所有启用的API源，合并结果。

        :param keyword: 搜索关键词
        :param page: 页码，从1开始
        :return: 结构化聚合搜索响应
        :raises APIError: 所有源均返回异常时抛出
        """
        sources = [s for s in self.api_sources if s.get("enabled", True)]
        if not sources:
            raise APIError("没有启用的API源，请先添加或启用源")

        all_items: List[VodItem] = []
        max_pagecount = 1
        total = 0
        errors: List[str] = []

        # 并发搜索所有启用源
        max_workers = min(len(sources), 5)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_source = {
                executor.submit(self._search_single, src, keyword, page): src
                for src in sources
            }
            for future in as_completed(future_to_source):
                src = future_to_source[future]
                try:
                    resp = future.result()
                    all_items.extend(resp.list)
                    max_pagecount = max(max_pagecount, resp.pagecount)
                    total += resp.total
                except HN4KError as exc:
                    err_msg = f"源 [{src['name']}] 失败: {exc}"
                    logger.warning(err_msg)
                    errors.append(err_msg)

        if not all_items and errors:
            raise APIError(f"所有源搜索失败: {'; '.join(errors[:3])}")

        return SearchResponse(
            code=1,
            msg=f"聚合搜索完成 (来自 {len(sources)} 个源)",
            page=page,
            pagecount=max_pagecount,
            limit=20,
            total=total,
            list=all_items,
        )

    def detail(self, vod_id: Union[int, str], source_name: Optional[str] = None) -> VodDetail:
        """
        获取影片详情。优先使用指定的源，否则遍历所有启用源查找。

        :param vod_id: 影片ID
        :param source_name: 指定API源名称（推荐）
        :return: 含解析后剧集的详情对象
        """
        sources = [s for s in self.api_sources if s.get("enabled", True)]

        if source_name:
            source = next((s for s in sources if s["name"] == source_name), None)
            if not source:
                raise APIError(f"未找到源: {source_name}")
            return self._detail_single(source, vod_id)

        # 未指定源时，遍历所有启用源查找
        last_err: Optional[Exception] = None
        for src in sources:
            try:
                return self._detail_single(src, vod_id)
            except HN4KError as exc:
                last_err = exc
                logger.debug(f"源 [{src['name']}] 未找到ID {vod_id}: {exc}")
                continue

        raise APIError(f"影片ID {vod_id} 在所有启用源中均未找到: {last_err}")

    def _detail_single(self, source: Dict[str, Any], vod_id: Union[int, str]) -> VodDetail:
        """从指定源获取详情。"""
        params = {
            "ac": "videolist",
            "ids": str(vod_id),
        }
        raw = self._request("GET", "", base_url=source["url"], params=params)
        try:
            resp = SearchResponse.model_validate(raw)
        except ValidationError as exc:
            raise APIError(f"详情响应结构异常: {exc}") from exc

        if not resp.list:
            raise APIError(f"影片ID {vod_id} 在源 [{source['name']}] 中不存在或已下架")

        vod = resp.list[0]
        episodes, sources, by_source = self._parse_play_url(vod)

        return VodDetail(
            vod_id=vod.vod_id,
            vod_name=vod.vod_name,
            vod_pic=vod.vod_pic,
            vod_remarks=vod.vod_remarks,
            vod_year=vod.vod_year,
            vod_area=vod.vod_area,
            vod_type=vod.vod_type,
            vod_actor=vod.vod_actor,
            vod_director=vod.vod_director,
            vod_content=vod.vod_content,
            episodes=episodes,
            sources=sources,
            episodes_by_source=by_source,
            source_name=source["name"],
        )

    @staticmethod
    def _parse_play_url(vod: VodItem) -> Tuple[List[Episode], List[str], Dict[str, List[Episode]]]:
        """
        解析苹果CMS播放地址串，按播放源分组。

        格式规范:
            单源: 第1集$url1#第2集$url2
            多源: 源A$$$源B  且  urlA#urlA2$$$urlB#urlB2

        :return: (全部剧集列表, 播放源名称列表, 按源分组的映射)
        """
        episodes: List[Episode] = []
        sources: List[str] = []
        by_source: Dict[str, List[Episode]] = {}

        play_from = vod.vod_play_from or ""
        play_url = vod.vod_play_url or ""

        if not play_url:
            return episodes, sources, by_source

        source_names = [s.strip() for s in play_from.split("$$$") if s.strip()]
        source_chunks = [c.strip() for c in play_url.split("$$$") if c.strip()]

        # 源名称与URL块对齐
        for idx, chunk in enumerate(source_chunks):
            source_name = source_names[idx] if idx < len(source_names) else f"源{idx + 1}"
            sources.append(source_name)
            by_source[source_name] = []

            # 每集用 '#' 分隔，每集内部用 '$' 分隔名称与URL
            for ep in chunk.split("#"):
                ep = ep.strip()
                if "$" not in ep:
                    continue
                # 兼容某些源使用多$的情况，只分割第一个
                name, url = ep.split("$", 1)
                name = name.strip() or "未知集数"
                url = url.strip()
                if url:
                    episode = Episode(name=name, url=url, source=source_name)
                    episodes.append(episode)
                    by_source[source_name].append(episode)

        return episodes, sources, by_source


# =============================================================================
# 播放器抽象层
# =============================================================================

class PlayerBackend:
    """播放器后端抽象基类。"""
    name: str = "abstract"

    def play(self, url: str, title: Optional[str] = None) -> None:
        raise NotImplementedError


class BrowserPlayer(PlayerBackend):
    """系统默认浏览器播放。"""
    name: str = "browser"

    def play(self, url: str, title: Optional[str] = None) -> None:
        """
        使用webbrowser模块打开播放页。
        对于m3u8直链，构造一个带HLS.js的极简播放页以提升兼容性。
        """
        parsed = urlparse(url)
        is_m3u8 = parsed.path.endswith(".m3u8") or ".m3u8" in parsed.path

        if is_m3u8:
            # 生成本地HTML播放器页面，解决浏览器直接打开m3u8可能无法播放的问题
            html_content = self._build_hls_player(url, title or "MFS Player")
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
                f.write(html_content)
                local_path = f.name
            logger.info(f"启动浏览器播放 m3u8: {local_path}")
            webbrowser.open(f"file://{local_path}")
        else:
            logger.info(f"启动浏览器打开: {url}")
            webbrowser.open(url)

    @staticmethod
    def _build_hls_player(m3u8_url: str, title: str) -> str:
        """构造内嵌HLS.js的本地HTML播放器。"""
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{ margin:0; background:#000; display:flex; align-items:center; justify-content:center; height:100vh; }}
  video {{ width:100%; max-width:1200px; aspect-ratio:16/9; }}
</style>
</head>
<body>
<video id="video" controls autoplay playsinline></video>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.8/dist/hls.min.js"></script>
<script>
  const video = document.getElementById('video');
  const src = '{m3u8_url}';
  if (Hls.isSupported()) {{
    const hls = new Hls();
    hls.loadSource(src);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => video.play());
  }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
    video.src = src;
    video.addEventListener('loadedmetadata', () => video.play());
  }}
</script>
</body>
</html>"""


class ExternalPlayer(PlayerBackend):
    """调用外部播放器进程（mpv / vlc）。"""
    name: str = "external"

    def __init__(self, binary: str):
        self.binary = binary

    def play(self, url: str, title: Optional[str] = None) -> None:
        import shutil
        import subprocess

        cmd_path = shutil.which(self.binary)
        if not cmd_path:
            raise PlayerError(f"未在PATH中找到播放器: {self.binary}")

        cmd = [cmd_path, url]
        if title and self.binary == "mpv":
            cmd.extend([f"--force-media-title={title}"])

        logger.info(f"启动外部播放器: {' '.join(cmd)}")
        try:
            # 脱离当前进程组，避免阻塞终端
            if sys.platform == "win32":
                subprocess.Popen(cmd, shell=False)
            else:
                subprocess.Popen(cmd, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise PlayerError(f"播放器启动失败: {exc}") from exc


class PlayerFactory:
    """播放器工厂。"""

    _registry: Dict[str, Callable[[], PlayerBackend]] = {
        "browser": BrowserPlayer,
        "mpv": lambda: ExternalPlayer("mpv"),
        "vlc": lambda: ExternalPlayer("vlc"),
    }

    @classmethod
    def create(cls, name: str) -> PlayerBackend:
        if name not in cls._registry:
            raise PlayerError(f"不支持的播放器: {name}。可选: {list(cls._registry.keys())}")
        return cls._registry[name]()


# =============================================================================
# 终端图像渲染器（PowerShell Sixel）
# =============================================================================

class ImageRenderer:
    """
    Windows PowerShell Sixel 终端图像渲染器。

    前提准备（仅需一次）:
        1. 安装 PowerShell 7.4+ (pwsh)
        2. 安装 Sixel 模块:
           Install-Module Microsoft.PowerShell.ConsoleGuiTools -Force -Scope CurrentUser
        3. 终端需支持 Sixel (Windows Terminal 1.22+ / WezTerm / iTerm2)

    工作原理:
        调用 pwsh -Command "Invoke-WebRequest -Uri '<url>' -UseBasicParsing | ConvertTo-Sixel | Out-Host"
        由 PowerShell 下载图片并转换为 Sixel 转义序列直接输出到终端。
    """

    def __init__(self, console: Console, config: ConfigManager):
        self.console = console
        self.cfg = config

    def render(self, image_url: Optional[str], title: Optional[str] = None) -> bool:
        """
        通过 PowerShell ConvertTo-Sixel 在终端内显示远程图片。

        :param image_url: 图片远程地址
        :param title: 图片上方标题（可选）
        :return: 是否成功渲染
        """
        if not image_url:
            return False

        if title:
            self.console.print(f"[dim]{title}[/dim]")

        # 转义 URL 中的双引号，防止 PowerShell 注入
        safe_url = image_url.replace('"', '`"')

        # 构建 PowerShell 命令，复用用户提供的模式
        cmd = [
            "pwsh", "-NoProfile", "-NonInteractive", "-Command",
            f'Invoke-WebRequest -Uri "{safe_url}" -UseBasicParsing | ConvertTo-Sixel | Out-Host'
        ]

        try:
            # 捕获 stderr 用于诊断；stdout 直接透传会丢失 Sixel 序列的实时性，
            # 因此先全量捕获再写入原始 stdout，确保转义序列不被 Rich 过滤
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                encoding="utf-8",
                errors="replace",
            )

            if result.returncode == 0:
                # 将 Sixel 转义序列直接写入原始 stdout，绕过 Rich Console 的格式化
                sys.stdout.write(result.stdout)
                sys.stdout.flush()
                return True

            # 失败时给出准备工作提示
            err = result.stderr.lower()
            if "convertto-sixel" in err or "无法识别" in err or "not recognized" in err:
                self.console.print(
                    "[yellow]⚠ 封面显示需 PowerShell 7.4+ 及 ConvertTo-Sixel cmdlet[/yellow]"
                )
                self.console.print(
                    "[dim]   准备工作: Install-Module Microsoft.PowerShell.ConsoleGuiTools -Force -Scope CurrentUser[/dim]"
                )
                self.console.print(
                    "[dim]   终端要求: Windows Terminal 1.22+ / WezTerm / 支持 Sixel 的终端[/dim]"
                )
            else:
                logger.warning(f"Sixel 渲染失败: {result.stderr[:200]}")
            return False

        except FileNotFoundError:
            self.console.print(
                "[yellow]⚠ 未找到 pwsh (PowerShell 7+)，封面显示已跳过[/yellow]"
            )
            self.console.print(
                "[dim]   请安装 PowerShell 7: winget install Microsoft.PowerShell[/dim]"
            )
            return False
        except Exception as exc:
            logger.warning(f"封面渲染异常: {exc}")
            return False


# =============================================================================
# 历史记录管理
# =============================================================================

class HistoryManager:
    """JSON文件持久化的播放历史。"""

    def __init__(self) -> None:
        self._data: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                if not isinstance(self._data, list):
                    self._data = []
            except (json.JSONDecodeError, OSError):
                self._data = []

    def save(self) -> None:
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data[-200:], f, ensure_ascii=False, indent=2)  # 保留最近200条
        except OSError as exc:
            logger.error(f"历史记录保存失败: {exc}")

    def add(self, vod_id: int, vod_name: str, episode_name: str, url: str, player: str, source_name: str = "") -> None:
        from datetime import datetime, timezone
        entry = {
            "vod_id": vod_id,
            "vod_name": vod_name,
            "episode": episode_name,
            "url": url,
            "player": player,
            "source_name": source_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        # 去重：同一影片同一集只保留最新
        self._data = [
            d for d in self._data
            if not (d.get("vod_id") == vod_id and d.get("episode") == episode_name)
        ]
        self._data.append(entry)
        self.save()

    def remove(self, vod_id: int, episode: str) -> bool:
        """删除指定历史记录。返回是否成功删除。"""
        original_len = len(self._data)
        self._data = [
            d for d in self._data
            if not (d.get("vod_id") == vod_id and d.get("episode") == episode)
        ]
        if len(self._data) < original_len:
            self.save()
            return True
        return False

    def clear(self) -> None:
        """清空所有历史记录。"""
        self._data = []
        self.save()

    def list(self, limit: int = 20) -> List[Dict[str, Any]]:
        return list(reversed(self._data[-limit:]))


# =============================================================================
# 终端UI层
# =============================================================================

class TerminalUI:
    """Rich终端交互封装 — 菜单驱动型TUI。"""

    def __init__(self, config: ConfigManager) -> None:
        self.console = Console()
        self.cfg = config
        self.img_renderer = ImageRenderer(self.console, config)

    def clear(self) -> None:
        """清屏（跨平台兼容）。"""
        self.console.clear()

    def print_banner(self) -> None:
        """打印艺术字应用横幅。"""
        art = """
[bold bright_cyan]███╗   ███╗███████╗███████╗[/bold bright_cyan]
[bold bright_cyan]████╗ ████║██╔════╝██╔════╝[/bold bright_cyan]
[bold bright_cyan]██╔████╔██║█████╗  ███████╗[/bold bright_cyan]
[bold bright_cyan]██║╚██╔╝██║██╔══╝  ╚════██║[/bold bright_cyan]
[bold bright_cyan]██║ ╚═╝ ██║██║     ███████║[/bold bright_cyan]
[bold bright_cyan]╚═╝     ╚═╝╚═╝     ╚══════╝[/bold bright_cyan]
        [bold white]影视终端[/bold white]  [dim]v{APP_VERSION}[/dim]
        """.format(APP_VERSION=APP_VERSION)
        self.console.print(art, justify="center")
        self.console.print("[dim]─" * 32, justify="center")

    def print_main_menu(self) -> str:
        """打印居中的紧凑主菜单。"""
        self.console.print()

        menu_table = Table(
            box=box.SIMPLE,
            show_header=False,
            show_edge=False,
            pad_edge=False,
            padding=(0, 1),
            width=52,
        )
        menu_table.add_column("", justify="center", width=4, style="bold bright_yellow")
        menu_table.add_column("", style="bold white", min_width=14)
        menu_table.add_column("", style="dim", min_width=22)

        menu_table.add_row("1", "◈ 搜索", "关键词聚合检索全网资源")
        menu_table.add_row("2", "◉ 历史", "最近播放记录")
        menu_table.add_row("3", "◊ 配置", "API / 播放器 / 源管理")
        menu_table.add_row("4", "◎ 关于", "版本与快捷键")
        menu_table.add_row("0", "○ 退出", "安全退出")

        panel = Panel(
            Align.center(menu_table),
            title="[bold bright_cyan] 主菜单 [/bold bright_cyan]",
            border_style="cyan",
            box=box.ROUNDED,
            padding=(1, 3),
            width=58,
        )
        self.console.print(Align.center(panel))
        self.console.print()

        choices = ["0", "1", "2", "3", "4"]
        choice = Prompt.ask(
            "[bold bright_green]›[/bold bright_green] 输入选项",
            choices=choices,
            show_choices=False,
        )
        return choice

    def print_search_results(self, resp: SearchResponse, page: int) -> Optional[Dict[str, Any]]:
        """
        渲染搜索结果，返回用户选择的影片信息 {"vod_id": int, "source_name": str}。
        """
        if not resp.list:
            self.console.print("[yellow]未找到相关影片[/yellow]")
            Prompt.ask("[dim]按 Enter 返回...[/dim]", default="")
            return None

        self.console.print(
            f"[bold bright_cyan]搜索结果[/bold bright_cyan]  [dim]第 {page}/{resp.pagecount or 1} 页 · 共 {resp.total} 条 · 来自 {len({item.source_name for item in resp.list})} 个源[/dim]"
        )
        self.console.print("[dim]─" * 50)

        table = Table(
            box=box.SIMPLE,
            show_header=True,
            show_lines=False,
            header_style="bold dim",
            border_style="cyan",
            pad_edge=False,
            padding=(0, 1),
        )
        table.add_column("#", justify="center", width=3, style="bold bright_yellow")
        table.add_column("ID", justify="right", width=7, style="dim")
        table.add_column("影片名称", min_width=16, style="bold white")
        table.add_column("来源", width=8, style="magenta")
        table.add_column("备注", min_width=8, style="cyan")
        table.add_column("年份", width=5, style="green")
        table.add_column("地区", width=6, style="blue")

        for idx, vod in enumerate(resp.list, 1):
            table.add_row(
                str(idx),
                str(vod.vod_id),
                vod.vod_name,
                vod.source_name or "─",
                vod.vod_remarks or "─",
                vod.vod_year or "─",
                vod.vod_area or "─",
            )

        self.console.print(table)
        self.console.print()

        choices = [str(i) for i in range(1, len(resp.list) + 1)]
        nav = []
        if resp.pagecount and resp.pagecount > 1:
            if page < resp.pagecount:
                nav.append("n")
            if page > 1:
                nav.append("p")
        nav.append("q")
        choices.extend(nav)

        parts = ["[dim]序号=影视[/dim]"]
        if "n" in nav:
            parts.append("[dim]n=下页[/dim]")
        if "p" in nav:
            parts.append("[dim]p=上页[/dim]")
        parts.append("[dim]q=返回[/dim]")

        choice = Prompt.ask(
            f"[bold bright_green]›[/bold bright_green] 输入 ({' '.join(parts)})",
            choices=choices,
            show_choices=False,
        )

        if choice == "q":
            return None
        if choice == "n":
            return {"action": "next"}
        if choice == "p":
            return {"action": "prev"}

        selected = resp.list[int(choice) - 1]
        return {"vod_id": selected.vod_id, "source_name": selected.source_name}

    def print_detail(self, detail: VodDetail) -> None:
        """渲染影片详情，含终端封面图。"""
        # 封面渲染
        if detail.vod_pic:
            self.img_renderer.render(detail.vod_pic, title="")
            self.console.print()

        self.console.print(
            f"[bold bright_cyan]{detail.vod_name}[/bold bright_cyan]  [dim]ID:{detail.vod_id}[/dim]"
        )
        self.console.print("[dim]─" * 40)

        meta_parts = []
        if detail.vod_year:
            meta_parts.append(f"[green]{detail.vod_year}[/green]")
        if detail.vod_area:
            meta_parts.append(f"[blue]{detail.vod_area}[/blue]")
        if detail.vod_type:
            meta_parts.append(f"[magenta]{detail.vod_type}[/magenta]")
        if detail.vod_remarks:
            meta_parts.append(f"[yellow]{detail.vod_remarks}[/yellow]")
        if detail.source_name:
            meta_parts.append(f"[bold magenta][{detail.source_name}][/bold magenta]")

        if meta_parts:
            self.console.print(" · ".join(meta_parts))
            self.console.print()

        if detail.vod_director:
            self.console.print(f"[dim]导演[/dim] {detail.vod_director}")
        if detail.vod_actor:
            self.console.print(f"[dim]演员[/dim] {detail.vod_actor}")
        if detail.vod_director or detail.vod_actor:
            self.console.print()

        content = detail.vod_content or "暂无简介"
        if len(content) > 280:
            content = content[:280] + "..."
        self.console.print(f"[dim]{content}[/dim]")
        self.console.print()
        self.console.print(
            f"[dim]可用源[/dim]  [cyan]{', '.join(detail.sources) or '─'}[/cyan]"
        )

    def select_source(self, detail: VodDetail) -> Optional[str]:
        """交互式选择播放源。"""
        if not detail.sources:
            self.console.print("[red]无可用播放源[/red]")
            Prompt.ask("[dim]按 Enter 返回...[/dim]", default="")
            return None

        if len(detail.sources) == 1:
            self.console.print(f"[dim]▸ 自动选择源: [cyan]{detail.sources[0]}[/cyan][/dim]")
            self.console.print()
            return detail.sources[0]

        self.console.print("[bold bright_yellow]可用播放源[/bold bright_yellow]")
        self.console.print("[dim]─" * 30)

        table = Table(
            box=box.SIMPLE,
            show_header=True,
            show_lines=False,
            header_style="bold dim",
            pad_edge=False,
            padding=(0, 1),
        )
        table.add_column("#", justify="center", width=3, style="bold bright_yellow")
        table.add_column("源", min_width=10, style="bold white")
        table.add_column("集数", width=6, style="cyan")
        table.add_column("格式", min_width=12, style="dim")

        for idx, src in enumerate(detail.sources, 1):
            ep_count = len(detail.episodes_by_source.get(src, []))
            fmt = "m3u8" if "m3u8" in src.lower() else "mp4"
            table.add_row(str(idx), src, str(ep_count), fmt)

        self.console.print(table)
        self.console.print()

        choices = [str(i) for i in range(1, len(detail.sources) + 1)]
        choices.append("q")
        choice = Prompt.ask(
            "[bold bright_green]›[/bold bright_green] 选源 [dim](q=返回)[/dim]",
            choices=choices,
            show_choices=False,
        )
        if choice == "q":
            return None
        return detail.sources[int(choice) - 1]

    def select_episode(self, detail: VodDetail, source: str) -> Optional[Episode]:
        """在指定源下选择剧集。"""
        episodes = detail.episodes_by_source.get(source, [])
        if not episodes:
            self.console.print(f"[red]源 [{source}] 下无可用剧集[/red]")
            Prompt.ask("[dim]按 Enter 返回...[/dim]", default="")
            return None

        self.console.print(
            f"[bold bright_cyan][{source}] 剧集[/bold bright_cyan]  [dim]共 {len(episodes)} 集[/dim]"
        )
        self.console.print("[dim]─" * 40)

        table = Table(
            box=box.SIMPLE,
            show_header=True,
            show_lines=False,
            header_style="bold dim",
            pad_edge=False,
            padding=(0, 1),
        )
        table.add_column("#", justify="center", width=3, style="bold bright_yellow")
        table.add_column("集数", min_width=12, style="bold white")
        table.add_column("地址", min_width=35, no_wrap=True, style="dim")

        for idx, ep in enumerate(episodes, 1):
            preview = ep.url[:45] + "..." if len(ep.url) > 45 else ep.url
            table.add_row(str(idx), ep.name, preview)

        self.console.print(table)
        self.console.print()

        choices = [str(i) for i in range(1, len(episodes) + 1)]
        choices.append("q")
        choice = Prompt.ask(
            "[bold bright_green]›[/bold bright_green] 选集 [dim](q=返回)[/dim]",
            choices=choices,
            show_choices=False,
        )
        if choice == "q":
            return None
        return episodes[int(choice) - 1]

    def print_history(self, records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        渲染播放历史记录，支持交互选择。
        返回 {"action": "play", "vod_id": int, "source_name": str} 或
              {"action": "delete", "vod_id": int, "episode": str} 或
              {"action": "clear"} 或 None。
        """
        if not records:
            self.console.print("[dim]暂无播放历史[/dim]")
            return None

        self.console.print("[bold bright_cyan]播放历史[/bold bright_cyan]  [dim]最近记录[/dim]")
        self.console.print("[dim]─" * 50)

        table = Table(
            box=box.SIMPLE,
            show_header=True,
            show_lines=False,
            header_style="bold dim",
            pad_edge=False,
            padding=(0, 1),
        )
        table.add_column("#", justify="center", width=3, style="bold bright_yellow")
        table.add_column("时间", width=16, style="dim")
        table.add_column("影片", min_width=14, style="bold white")
        table.add_column("集数", width=10, style="cyan")
        table.add_column("API源", width=8, style="magenta")
        table.add_column("播放器", width=6, style="green")

        for idx, r in enumerate(records, 1):
            ts = r.get("timestamp", "")[:16].replace("T", " ")
            table.add_row(
                str(idx),
                ts,
                r.get("vod_name", "─"),
                r.get("episode", "─"),
                r.get("source_name", "─"),
                r.get("player", "─"),
            )
        self.console.print(table)
        self.console.print()

        choices = [str(i) for i in range(1, len(records) + 1)]
        choices.extend(["d", "c", "q"])
        choice = Prompt.ask(
            "[bold bright_green]›[/bold bright_green] [dim]序号=跳转播放 / d=删除单条 / c=清空全部 / q=返回[/dim]",
            choices=choices,
            show_choices=False,
        )
        if choice == "q":
            return None
        if choice == "c":
            return {"action": "clear"}
        if choice == "d":
            del_choices = [str(i) for i in range(1, len(records) + 1)]
            del_choices.append("q")
            sel = Prompt.ask(
                "[bold bright_green]›[/bold bright_green] [dim]输入要删除的序号 (q=取消)[/dim]",
                choices=del_choices,
                show_choices=False,
            )
            if sel == "q":
                return None
            r = records[int(sel) - 1]
            return {"action": "delete", "vod_id": r.get("vod_id"), "episode": r.get("episode")}

        r = records[int(choice) - 1]
        return {"action": "play", "vod_id": r.get("vod_id"), "source_name": r.get("source_name")}

    def print_config(self, cfg: Dict[str, Any]) -> None:
        """渲染配置项。"""
        self.console.print("[bold bright_cyan]当前配置[/bold bright_cyan]")
        self.console.print("[dim]─" * 40)

        table = Table(
            box=box.SIMPLE,
            show_header=True,
            show_lines=False,
            header_style="bold dim",
            pad_edge=False,
            padding=(0, 1),
        )
        table.add_column("键", style="bold cyan", min_width=16)
        table.add_column("值", style="white")
        table.add_column("说明", style="dim")

        desc_map = {
            "api_sources": "API 源列表",
            "default_player": "默认播放器",
            "timeout": "超时秒数",
            "max_retries": "重试次数",
            "page_size": "每页条数",
            "user_agent": "请求 UA",
        }
        for k, v in cfg.items():
            if k == "api_sources":
                v_str = f"{len(v)} 个源"
            else:
                v_str = str(v)
            table.add_row(k, v_str, desc_map.get(k, "─"))
        self.console.print(table)
        self.console.print()

    def print_sources(self, sources: List[Dict[str, Any]]) -> None:
        """渲染API源列表。"""
        if not sources:
            self.console.print("[yellow]暂无配置API源[/yellow]")
            return

        self.console.print("[bold bright_cyan]API 源列表[/bold bright_cyan]")
        self.console.print("[dim]─" * 50)

        table = Table(
            box=box.SIMPLE,
            show_header=True,
            show_lines=False,
            header_style="bold dim",
            border_style="cyan",
            pad_edge=False,
            padding=(0, 1),
        )
        table.add_column("#", justify="center", width=3, style="bold bright_yellow")
        table.add_column("名称", min_width=10, style="bold white")
        table.add_column("地址", min_width=30, no_wrap=True, style="dim")
        table.add_column("状态", width=6, style="green")

        for idx, src in enumerate(sources, 1):
            enabled = src.get("enabled", True)
            status = "[green]启用[/green]" if enabled else "[red]禁用[/red]"
            table.add_row(str(idx), src["name"], src["url"], status)

        self.console.print(table)
        self.console.print()


# =============================================================================
# CLI 命令定义
# =============================================================================

# 全局单例（懒加载）
_config_mgr: Optional[ConfigManager] = None
_history_mgr: Optional[HistoryManager] = None
_ui: Optional[TerminalUI] = None


def _init_singletons() -> Tuple[ConfigManager, HistoryManager, TerminalUI]:
    global _config_mgr, _history_mgr, _ui
    if _config_mgr is None:
        _config_mgr = ConfigManager()
        _history_mgr = HistoryManager()
        _ui = TerminalUI(_config_mgr)
        # 配置日志：只写入文件，不污染终端
        logger.remove()  # 移除默认的 stderr 输出
        logger.add(
            LOG_FILE,
            rotation="1 week",
            retention="1 month",
            level="INFO",
            encoding="utf-8",
        )
    return _config_mgr, _history_mgr, _ui


@click.group(invoke_without_command=True)
@click.option("--version", is_flag=True, help="显示版本号")
@click.pass_context
def cli(ctx: click.Context, version: bool) -> None:
    """MFS 影视终端 — 命令行影视客户端。支持多源聚合搜索。"""
    if version:
        click.echo(f"{APP_NAME} v{APP_VERSION}")
        sys.exit(0)

    cfg, hist, ui = _init_singletons()
    ctx.ensure_object(dict)
    ctx.obj["cfg"] = cfg
    ctx.obj["hist"] = hist
    ctx.obj["ui"] = ui

    # 无子命令时进入菜单驱动的交互模式
    if ctx.invoked_subcommand is None:
        _interactive_menu(ctx)


# ---------------------------------------------------------------------------
# search 子命令
# ---------------------------------------------------------------------------
@cli.command(name="search")
@click.argument("keyword")
@click.option("--page", "-p", default=1, type=int, help="页码")
@click.pass_context
def cmd_search(ctx: click.Context, keyword: str, page: int) -> None:
    """聚合搜索影片（所有启用源同时搜索）。"""
    cfg: ConfigManager = ctx.obj["cfg"]
    ui: TerminalUI = ctx.obj["ui"]
    client = CMSClient(cfg)

    try:
        resp = client.search(keyword, page=page)
        ui.print_search_results(resp, page)
    except HN4KError as exc:
        logger.error(f"搜索失败: {exc}")
        ui.console.print(f"[red]✗ 搜索失败: {exc}[/red]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# detail 子命令
# ---------------------------------------------------------------------------
@cli.command(name="detail")
@click.argument("vod_id", type=int)
@click.option("--source", "-s", type=str, default=None, help="指定API源名称")
@click.pass_context
def cmd_detail(ctx: click.Context, vod_id: int, source: Optional[str]) -> None:
    """查看影片详情。建议指定 --source 以提高速度。"""
    cfg: ConfigManager = ctx.obj["cfg"]
    ui: TerminalUI = ctx.obj["ui"]
    client = CMSClient(cfg)

    try:
        detail = client.detail(vod_id, source_name=source)
        ui.print_detail(detail)
    except HN4KError as exc:
        logger.error(f"获取详情失败: {exc}")
        ui.console.print(f"[red]✗ 获取详情失败: {exc}[/red]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# play 子命令
# ---------------------------------------------------------------------------
@cli.command(name="play")
@click.argument("vod_id", type=int)
@click.option("--episode", "-e", type=int, default=None, help="直接指定集数序号")
@click.option("--player", "-p", type=click.Choice(["browser", "mpv", "vlc"]), default=None, help="播放器")
@click.option("--source", "-s", type=str, default=None, help="指定API源名称（推荐）")
@click.pass_context
def cmd_play(ctx: click.Context, vod_id: int, episode: Optional[int], player: Optional[str], source: Optional[str]) -> None:
    """播放影片。默认使用浏览器。建议指定 --source。"""
    cfg: ConfigManager = ctx.obj["cfg"]
    ui: TerminalUI = ctx.obj["ui"]
    hist: HistoryManager = ctx.obj["hist"]
    client = CMSClient(cfg)

    try:
        detail = client.detail(vod_id, source_name=source)
    except HN4KError as exc:
        logger.error(f"获取详情失败: {exc}")
        ui.console.print(f"[red]✗ 获取详情失败: {exc}[/red]")
        sys.exit(1)

    if not detail.episodes:
        ui.console.print("[red]✗ 无可用播放地址。[/red]")
        sys.exit(1)

    # 先选源，再选集
    ui.print_detail(detail)
    play_source = ui.select_source(detail)
    if play_source is None:
        return

    if episode is not None:
        episodes = detail.episodes_by_source.get(play_source, [])
        if episode < 1 or episode > len(episodes):
            ui.console.print(f"[red]✗ 集数序号越界 (1-{len(episodes)})[/red]")
            sys.exit(1)
        ep = episodes[episode - 1]
    else:
        ep = ui.select_episode(detail, play_source)
        if ep is None:
            return

    # 确定播放器
    player_name = player or cfg.get("default_player", "browser")
    try:
        backend = PlayerFactory.create(player_name)
        backend.play(ep.url, title=f"{detail.vod_name} - {ep.name}")
        hist.add(detail.vod_id, detail.vod_name, ep.name, ep.url, player_name, source_name=detail.source_name or "")
        ui.console.print(f"[green]▶ 正在用 {player_name} 播放: {detail.vod_name} — {ep.name}[/green]")
    except HN4KError as exc:
        logger.error(f"播放失败: {exc}")
        ui.console.print(f"[red]✗ 播放失败: {exc}[/red]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# history 子命令
# ---------------------------------------------------------------------------
@cli.command(name="history")
@click.pass_context
def cmd_history(ctx: click.Context) -> None:
    """查看播放历史。"""
    ui: TerminalUI = ctx.obj["ui"]
    hist: HistoryManager = ctx.obj["hist"]
    records = hist.list(limit=20)
    ui.print_history(records)


# ---------------------------------------------------------------------------
# config 子命令
# ---------------------------------------------------------------------------
@cli.command(name="config")
@click.option("--set", "set_kv", type=(str, str), multiple=True, help="设置配置项 KEY VALUE")
@click.option("--get", "get_key", type=str, default=None, help="获取配置项")
@click.pass_context
def cmd_config(ctx: click.Context, set_kv: List[Tuple[str, str]], get_key: Optional[str]) -> None:
    """管理本地配置。"""
    cfg: ConfigManager = ctx.obj["cfg"]
    ui: TerminalUI = ctx.obj["ui"]

    if set_kv:
        for k, v in set_kv:
            # 尝试类型转换
            if v.lower() in ("true", "false"):
                v = v.lower() == "true"
            elif v.isdigit():
                v = int(v)
            cfg.set(k, v)
        ui.console.print(f"[green]✓ 已更新 {len(set_kv)} 项配置[/green]")

    if get_key:
        val = cfg.get(get_key)
        ui.console.print(f"[cyan]{get_key}[/cyan] = {val}")
        return

    if not set_kv and not get_key:
        ui.print_config(cfg.all())


# ---------------------------------------------------------------------------
# add-source 子命令
# ---------------------------------------------------------------------------
@cli.command(name="add-source")
@click.argument("name")
@click.argument("url")
@click.pass_context
def cmd_add_source(ctx: click.Context, name: str, url: str) -> None:
    """添加自定义API源。URL需以 /provide/vod/ 结尾。"""
    cfg: ConfigManager = ctx.obj["cfg"]
    ui: TerminalUI = ctx.obj["ui"]
    try:
        cfg.add_source(name, url)
        ui.console.print(f"[green]✓ 已添加API源: {name}[/green]")
        ui.console.print(f"[dim]  URL: {url}[/dim]")
    except HN4KError as exc:
        ui.console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# remove-source 子命令
# ---------------------------------------------------------------------------
@cli.command(name="remove-source")
@click.argument("name")
@click.pass_context
def cmd_remove_source(ctx: click.Context, name: str) -> None:
    """删除指定API源。"""
    cfg: ConfigManager = ctx.obj["cfg"]
    ui: TerminalUI = ctx.obj["ui"]
    try:
        cfg.remove_source(name)
        ui.console.print(f"[green]✓ 已删除API源: {name}[/green]")
    except HN4KError as exc:
        ui.console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# list-sources 子命令
# ---------------------------------------------------------------------------
@cli.command(name="list-sources")
@click.pass_context
def cmd_list_sources(ctx: click.Context) -> None:
    """列出所有配置的API源。"""
    cfg: ConfigManager = ctx.obj["cfg"]
    ui: TerminalUI = ctx.obj["ui"]
    sources = cfg.list_sources()
    ui.print_sources(sources)


# ---------------------------------------------------------------------------
# toggle-source 子命令
# ---------------------------------------------------------------------------
@cli.command(name="toggle-source")
@click.argument("name")
@click.pass_context
def cmd_toggle_source(ctx: click.Context, name: str) -> None:
    """启用/禁用指定API源。"""
    cfg: ConfigManager = ctx.obj["cfg"]
    ui: TerminalUI = ctx.obj["ui"]
    try:
        new_state = cfg.toggle_source(name)
        status = "启用" if new_state else "禁用"
        ui.console.print(f"[green]✓ 源 [{name}] 已{status}[/green]")
    except HN4KError as exc:
        ui.console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)


# =============================================================================
# 交互式搜索主循环
# =============================================================================

def _interactive_menu(ctx: click.Context) -> None:
    """菜单驱动的交互式TUI主循环。"""
    cfg: ConfigManager = ctx.obj["cfg"]
    ui: TerminalUI = ctx.obj["ui"]
    hist: HistoryManager = ctx.obj["hist"]
    client = CMSClient(cfg)

    while True:
        ui.clear()
        ui.print_banner()
        choice = ui.print_main_menu()

        if choice == "0":
            ui.console.print("[dim]感谢使用 · 再见[/dim]")
            break

        elif choice == "1":
            _do_search_flow(ui, client, cfg, hist)

        elif choice == "2":
            _do_history_flow(ui, client, cfg, hist)

        elif choice == "3":
            _do_config_flow(ui, cfg)

        elif choice == "4":
            _do_about_flow(ui)


def _do_search_flow(ui: TerminalUI, client: CMSClient, cfg: ConfigManager, hist: HistoryManager) -> None:
    """搜索影片完整流程。"""
    ui.clear()
    ui.print_banner()
    keyword = Prompt.ask("[bold cyan]请输入搜索关键词[/bold cyan] (直接回车返回菜单)")
    if not keyword.strip():
        return

    page = 1
    while True:
        ui.clear()
        ui.print_banner()
        try:
            resp = client.search(keyword, page=page)
        except HN4KError as exc:
            ui.console.print(f"[red]✗ 搜索失败: {exc}[/red]")
            Prompt.ask("[dim]按 Enter 返回...[/dim]", default="")
            return

        result = ui.print_search_results(resp, page)
        if result is None:
            return  # 用户选择退出/返回
        if isinstance(result, dict) and result.get("action") == "next":
            if resp.pagecount and page < resp.pagecount:
                page += 1
            else:
                ui.console.print("[yellow]已经是最后一页。[/yellow]")
                Prompt.ask("[dim]按 Enter 继续...[/dim]", default="")
            continue
        if isinstance(result, dict) and result.get("action") == "prev":
            if page > 1:
                page -= 1
            else:
                ui.console.print("[yellow]已经是第一页。[/yellow]")
                Prompt.ask("[dim]按 Enter 继续...[/dim]", default="")
            continue

        # 进入详情/播放流程
        vod_id = result["vod_id"]
        source_name = result.get("source_name")
        _do_play_flow(ui, client, cfg, hist, vod_id, source_name)
        if not Confirm.ask("是否继续搜索?", default=True):
            break


def _do_history_flow(ui: TerminalUI, client: CMSClient, cfg: ConfigManager, hist: HistoryManager) -> None:
    """历史记录交互流程：跳转播放 / 删除单条 / 清空全部。"""
    while True:
        ui.clear()
        ui.print_banner()
        records = hist.list(limit=20)
        result = ui.print_history(records)

        if result is None:
            break

        if result["action"] == "clear":
            if Confirm.ask("[red]确认清空全部播放历史?[/red]", default=False):
                hist.clear()
                ui.console.print("[green]✓ 已清空播放历史[/green]")
            else:
                ui.console.print("[dim]已取消[/dim]")
            Prompt.ask("[dim]按 Enter 继续...[/dim]", default="")
            continue

        if result["action"] == "delete":
            ok = hist.remove(result["vod_id"], result["episode"])
            if ok:
                ui.console.print(f"[green]✓ 已删除记录[/green]")
            else:
                ui.console.print("[yellow]记录不存在或已删除[/yellow]")
            Prompt.ask("[dim]按 Enter 继续...[/dim]", default="")
            continue

        if result["action"] == "play":
            _do_play_flow(ui, client, cfg, hist, result["vod_id"], result.get("source_name"))
            if not Confirm.ask("是否继续查看历史?", default=True):
                break

def _do_play_flow(ui: TerminalUI, client: CMSClient, cfg: ConfigManager, hist: HistoryManager, vod_id: int, source_name: Optional[str] = None) -> None:
    """单部影片的详情→选源→选集→播放流程。"""
    try:
        detail = client.detail(vod_id, source_name=source_name)
    except HN4KError as exc:
        ui.console.print(f"[red]✗ 获取详情失败: {exc}[/red]")
        Prompt.ask("[dim]按 Enter 返回...[/dim]", default="")
        return

    ui.clear()
    ui.print_banner()
    # 封面已在 print_detail 中渲染，此处直接调用
    ui.print_detail(detail)

    if not detail.sources:
        ui.console.print("[red]✗ 无可用播放源。[/red]")
        Prompt.ask("[dim]按 Enter 返回...[/dim]", default="")
        return

    source = ui.select_source(detail)
    if source is None:
        return

    ep = ui.select_episode(detail, source)
    if ep is None:
        return

    player_name = cfg.get("default_player", "browser")
    try:
        backend = PlayerFactory.create(player_name)
        backend.play(ep.url, title=f"{detail.vod_name} - {ep.name}")
        hist.add(detail.vod_id, detail.vod_name, ep.name, ep.url, player_name, source_name=detail.source_name or "")
        ui.console.print()
        ui.console.print()
        ui.console.print(
            f"[bold bright_green]▶[/bold bright_green] "
            f"[bold white]{detail.vod_name}[/bold white]  [dim]— {ep.name}[/dim]"
        )
        ui.console.print(f"[dim]播放器 {player_name} · 源 {source} · API {detail.source_name or '─'}[/dim]")
    except HN4KError as exc:
        ui.console.print(f"[red]✗ 播放失败: {exc}[/red]")

    Prompt.ask("[dim]按 Enter 继续...[/dim]", default="")


def _do_config_flow(ui: TerminalUI, cfg: ConfigManager) -> None:
    """配置管理流程 — 交互式源管理。"""
    while True:
        ui.clear()
        ui.print_banner()
        ui.print_config(cfg.all())
        ui.console.print()
        ui.console.print("[dim]如果您没有自己的源资源，可以使用本项目提供的以下源:[/dim]")
        ui.console.print("  [cyan]4K专属源: https://raw.giteeusercontent.com/magic-fss/tv-source/raw/master/source_4K.json[/cyan]")
        ui.console.print("  [cyan]综合通用源: https://raw.giteeusercontent.com/magic-fss/tv-source/raw/master/source_hub.json[/cyan]")
        ui.console.print()
        ui.console.print("[bold bright_yellow]源管理[/bold bright_yellow]")
        sources = cfg.list_sources()
        ui.print_sources(sources)
        ui.console.print()
        choice = Prompt.ask(
            "[bold bright_green]›[/bold bright_green] [dim]a=添加源 / d=删除源 / t=开关源 / Enter=返回[/dim]",
            choices=["", "a", "d", "t"],
            show_choices=False,
            default="",
        )
        if choice == "":
            break

        elif choice == "a":
            name = Prompt.ask("源名称")
            url = Prompt.ask("源URL (如 https://example.com/api.php/provide/vod/)")
            if not name or not url:
                continue
            # 可用性检测
            ui.console.print(f"[dim]正在检测源 [{name}] 可用性...[/dim]")
            try:
                test_client = CMSClient(cfg)
                test_url = url.rstrip("/") + "/"
                test_client._request("GET", "", base_url=test_url, params={"ac": "videolist", "wd": "test", "pg": 1})
                ui.console.print(f"[green]✓ 源 [{name}] 可用[/green]")
            except HN4KError as exc:
                ui.console.print(f"[yellow]⚠ 源 [{name}] 检测异常: {exc}[/yellow]")
                if not Confirm.ask("仍要添加此源?", default=False):
                    continue
            try:
                cfg.add_source(name, url)
                ui.console.print(f"[green]✓ 已添加: {name}[/green]")
            except HN4KError as exc:
                ui.console.print(f"[red]✗ {exc}[/red]")
            Prompt.ask("[dim]按 Enter 继续...[/dim]", default="")

        elif choice == "d":
            if not sources:
                ui.console.print("[yellow]暂无源可删除[/yellow]")
                Prompt.ask("[dim]按 Enter 继续...[/dim]", default="")
                continue
            # 过滤掉默认源（第一个），仅显示可删除的自定义源
            deletable = sources[1:] if len(sources) > 1 else []
            if not deletable:
                ui.console.print("[yellow]只有默认源，无法删除（可禁用）[/yellow]")
                Prompt.ask("[dim]按 Enter 继续...[/dim]", default="")
                continue
            # 显示可删除源（序号从2开始对应原列表）
            ui.console.print("[dim]以下源可删除（默认源不可删）:[/dim]")
            for idx, src in enumerate(deletable, 2):
                status = "[green]启用[/green]" if src.get("enabled", True) else "[red]禁用[/red]"
                ui.console.print(f"  {idx}. {src['name']} [{status}]")
            ui.console.print()
            choices = [str(i) for i in range(2, len(sources) + 1)]
            choices.append("q")
            sel = Prompt.ask(
                "[bold bright_green]›[/bold bright_green] [dim]输入序号删除 (q=取消)[/dim]",
                choices=choices,
                show_choices=False,
            )
            if sel == "q":
                continue
            idx = int(sel) - 1
            name = sources[idx]["name"]
            url = sources[idx]["url"]
            ui.console.print(f"[red]即将删除源: {name}[/red]")
            ui.console.print(f"[dim]{url}[/dim]")
            if not Confirm.ask("确认彻底删除?", default=False):
                ui.console.print("[dim]已取消删除[/dim]")
                Prompt.ask("[dim]按 Enter 继续...[/dim]", default="")
                continue
            try:
                cfg.remove_source(name)
                ui.console.print(f"[green]✓ 已彻底删除源 [{name}][/green]")
            except HN4KError as exc:
                ui.console.print(f"[red]✗ {exc}[/red]")
            Prompt.ask("[dim]按 Enter 继续...[/dim]", default="")

        elif choice == "t":
            if not sources:
                ui.console.print("[yellow]暂无源可切换[/yellow]")
                Prompt.ask("[dim]按 Enter 继续...[/dim]", default="")
                continue
            choices = [str(i) for i in range(1, len(sources) + 1)]
            choices.append("q")
            sel = Prompt.ask(
                "[bold bright_green]›[/bold bright_green] [dim]输入序号切换 (q=取消)[/dim]",
                choices=choices,
                show_choices=False,
            )
            if sel == "q":
                continue
            idx = int(sel) - 1
            name = sources[idx]["name"]
            try:
                state = cfg.toggle_source(name)
                ui.console.print(f"[green]✓ [{name}] 已{'启用' if state else '禁用'}[/green]")
            except HN4KError as exc:
                ui.console.print(f"[red]✗ {exc}[/red]")
            Prompt.ask("[dim]按 Enter 继续...[/dim]", default="")


def _do_about_flow(ui: TerminalUI) -> None:
    """关于信息流程。"""
    ui.clear()
    ui.print_banner()
    about_text = f"""[bold bright_cyan]MFS 影视终端[/bold bright_cyan]  [dim]v{APP_VERSION}[/dim]

[cyan]命令行影视搜索与播放客户端[/cyan]

[dim]功能[/dim]
  · 苹果CMS V10 API 兼容
  · 多API源聚合搜索与独立管理
  · 多源分组选择与播放
  · 浏览器 / mpv / vlc 后端
  · 本地配置与历史持久化
  · 指数退避重试与异常分级

[dim]快捷键[/dim]
  主菜单   数字键
  搜索页   n 下页 · p 上页 · q 返回
  源/剧集  q 返回

[dim]源管理命令[/dim]
  add-source    添加API源
  remove-source 删除API源
  list-sources  列出所有源
  toggle-source 启用/禁用源

[dim]项目[/dim]
 https://github.com/magic-fss/mfs-video-terminal.git
"""
    ui.console.print(Panel(about_text, border_style="cyan", box=box.SIMPLE, padding=(1, 2)))
    Prompt.ask("[dim]按 Enter 返回主菜单...[/dim]", default="")


# =============================================================================
# 入口
# =============================================================================

if __name__ == "__main__":
    cli()