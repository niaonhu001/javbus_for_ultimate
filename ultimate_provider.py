from __future__ import annotations

import base64
import math
import os
import random
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, quote, urlparse, parse_qs

from bs4 import BeautifulSoup
from curl_cffi import requests, CurlOpt

from protocol.base import ProtocolProvider

JAVBUS_CONFIG_KEY = "javbus"
JAVBUS_PLUGIN_ID = "video.javbus"
JAVBUS_PLATFORM = "JavBus"
JAVBUS_HOST_ID_PREFIX = "BUS"

DEFAULT_DOMAIN = "https://www.javbus.com"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_TOKEN_MASK_VALUES = {"", "********", "******", "__KEEP__"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int, minimum: int = 1, maximum: int = 10000) -> int:
    try:
        parsed = int(float(value))
    except Exception:
        parsed = default
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def _normalize_domain(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    return text or DEFAULT_DOMAIN


def _is_javbus_cdn_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    return "javbus.com" in lower or "pics.dmm.co.jp" in lower


def _parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """将 'key1=val1; key2=val2' 解析为 dict。"""
    result: Dict[str, str] = {}
    if not cookie_str:
        return result
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            key, _, val = part.partition("=")
            result[key.strip()] = val.strip()
    return result


def _extract_js_var(html: str, var_name: str) -> str:
    """从页面 JS 代码中提取变量值，例如 var gid = 12345;"""
    m = re.search(rf"var\s+{re.escape(var_name)}\s*=\s*['\"]?(.*?)['\"]?\s*;", html)
    if m:
        return m.group(1).strip()
    return ""


def _abs_url(url: str, domain: str) -> str:
    """将相对路径转为绝对 URL。"""
    if not url:
        return url
    url = url.strip()
    if url.startswith("http://") or url.startswith("https://") or url.startswith("//"):
        return url
    if url.startswith("/"):
        return f"{domain.rstrip('/')}{url}"
    return f"{domain.rstrip('/')}/{url}"


class JavBusProvider(ProtocolProvider):
    """JavBus 协议化适配器（直接爬取模式）。

    直接请求 www.javbus.com 并解析 HTML 获取数据。
    磁力链接通过 AJAX 接口 uncledatoolsbyajax.php 获取。
    """

    def normalize_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = dict(payload or {})
        normalized: Dict[str, Any] = {}
        normalized["enabled"] = _as_bool(raw.get("enabled"), True)
        normalized["domain"] = _normalize_domain(raw.get("domain"))
        normalized["cookie_string"] = str(raw.get("cookie_string") or "").strip()
        normalized["user_agent"] = str(raw.get("user_agent") or "").strip() or DEFAULT_USER_AGENT
        normalized["movie_type"] = str(raw.get("movie_type") or "normal").strip().lower()
        if normalized["movie_type"] not in ("normal", "uncensored"):
            normalized["movie_type"] = "normal"
        normalized["timeout_seconds"] = _as_int(raw.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 600)
        normalized["proxy"] = str(raw.get("proxy") or "").strip()
        return normalized

    def serialize_public_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self.normalize_config(config)
        public = dict(normalized)
        public.pop("cookie_string", None)
        public["cookie_configured"] = bool(str((config or {}).get("cookie_string") or "").strip())
        return public

    def get_query_status(self, config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self.normalize_config(config)
        enabled = _as_bool(normalized.get("enabled"), True)
        domain = str(normalized.get("domain") or "").strip()
        configured = bool(enabled and domain)
        return {
            "configured": configured,
            "message": "" if configured else "JavBus 未启用或站点域名未配置。",
            "missing_fields": [] if domain else ["domain"],
        }

    # ---------- 内部工具 ----------

    def _build_session(self, config: Dict[str, Any]) -> requests.Session:
        session = requests.Session()
        # 设置连接超时 8 秒（网站被墙/不可达时快速失败）
        session.curl_options = {CurlOpt.CONNECTTIMEOUT: 8}
        session.headers.update({
            "User-Agent": str(config.get("user_agent") or DEFAULT_USER_AGENT),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
            "Referer": "https://www.javbus.com/",
            "DNT": "1",
        })
        # 注入 Cookie（来自配置）
        cookie_str = str(config.get("cookie_string") or "").strip()
        parsed_cookies = _parse_cookie_string(cookie_str) if cookie_str else {}
        for key, val in parsed_cookies.items():
            session.cookies.set(key, val, domain=".javbus.com")
        # 确保存在 existmag=all，否则磁力链接可能为空
        if "existmag" not in parsed_cookies:
            session.cookies.set("existmag", "all", domain=".javbus.com")
        proxy = str(config.get("proxy") or "").strip()
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        return session

    def _request(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        url: str,
        *,
        retry: int = 2,
        accept_json: bool = False,
    ):
        """发送 GET 请求，支持重试和 JSON 响应。"""
        timeout = _as_int(config.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 600)
        for attempt in range(retry):
            try:
                response = session.get(
                    url,
                    impersonate="chrome",
                    timeout=timeout,
                    allow_redirects=True,
                )
                if response.status_code == 200:
                    if accept_json:
                        try:
                            return response.json()
                        except Exception:
                            return response.text
                    return response.text
                if response.status_code in (403, 503):
                    if attempt < retry - 1:
                        time.sleep(1)
                    continue
                if response.status_code == 404:
                    return None
            except Exception:
                if attempt < retry - 1:
                    time.sleep(1)
                    continue
                raise RuntimeError(f"JavBus 请求失败（重试 {retry} 次后）: {url}")
        return None

    def _domain(self, config: Dict[str, Any]) -> str:
        return str(config.get("domain") or DEFAULT_DOMAIN).rstrip("/")

    def _download_file(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        url: str,
        save_path: str,
    ) -> bool:
        try:
            headers = {}
            if _is_javbus_cdn_url(url):
                headers["Referer"] = "https://www.javbus.com/"
            timeout = _as_int(config.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 600)
            response = session.get(
                url, headers=headers, impersonate="chrome",
                timeout=timeout, stream=True,
            )
            if response.status_code >= 400:
                return False
            os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
            with open(save_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        handle.write(chunk)
            return True
        except Exception:
            return False

    # ---------- 页面解析 ----------

    def _parse_search_item(self, link_tag) -> Optional[Dict[str, Any]]:
        """解析搜索结果卡片（a.movie-box）。"""
        if not link_tag:
            return None
        href = link_tag.get("href", "")
        movie_id = href.rstrip("/").rsplit("/", 1)[-1] if "/" in href else ""
        if not movie_id:
            return None

        # 封面
        img_tag = link_tag.select_one(".photo-frame img")
        cover_url = str(img_tag.get("src", "")).strip() if img_tag else ""

        # 标题（来自 img 的 title 或 alt）
        title = str(img_tag.get("title", "") or img_tag.get("alt", "")).strip() if img_tag else ""

        # 日期
        date_tag = link_tag.select_one(".photo-info .date")
        date = str(date_tag.get_text(strip=True)) if date_tag else ""

        return {
            "video_id": movie_id,
            "title": title,
            "code": movie_id,
            "cover_url": cover_url,
            "date": date,
        }

    def _parse_search_results(self, html: str) -> List[Dict[str, Any]]:
        """解析搜索结果的 HTML，返回 video summary 列表。"""
        soup = BeautifulSoup(html, "lxml")
        results: List[Dict[str, Any]] = []
        for item in soup.select("a.movie-box"):
            parsed = self._parse_search_item(item)
            if parsed:
                results.append(parsed)
        return results

    def _parse_detail_page(self, html: str, movie_id: str) -> Optional[Dict[str, Any]]:
        """解析影片详情页 HTML。"""
        soup = BeautifulSoup(html, "lxml")

        # 标题
        title_tag = soup.select_one("div.container h3")
        title = str(title_tag.get_text(strip=True)) if title_tag else ""

        # 封面大图
        cover_tag = soup.select_one("a.bigImage")
        cover_url = ""
        if cover_tag:
            cover_url = str(cover_tag.get("href", "")).strip()
        if not cover_url:
            cover_img = soup.select_one("a.bigImage img")
            if cover_img:
                cover_url = str(cover_img.get("src", "")).strip()

        # 信息面板
        info_panel = soup.select_one(".col-md-3.info")
        code = movie_id
        date = ""
        video_length = None
        director = ""
        producer = ""
        publisher = ""
        series = ""

        if info_panel:
            paragraphs = info_panel.select("p")
            for p in paragraphs:
                text = p.get_text(" ", strip=True)
                if "識別碼:" in text or "识别码:" in text:
                    # 优先从 span 元素中提取番号
                    code_spans = p.select("span")
                    if code_spans:
                        # 取最后一个非空 span 的内容
                        for span in reversed(code_spans):
                            span_text = span.get_text(strip=True)
                            if span_text:
                                code = span_text
                                break
                    # 其次从冒号后的文本提取
                    if code == movie_id and ":" in text:
                        colon_text = text.split(":", 1)[-1].strip()
                        if colon_text:
                            code = colon_text
                    continue
                if "發行日期:" in text or "发行日期:" in text:
                    date = text.replace("發行日期:", "").replace("发行日期:", "").strip()
                    continue
                if "長度:" in text or "长度:" in text:
                    length_match = re.search(r"(\d+)", text)
                    if length_match:
                        video_length = int(length_match.group(1))
                    continue
                if "導演:" in text or "导演:" in text:
                    a_tag = p.select_one("a")
                    if a_tag:
                        director = a_tag.get_text(strip=True)
                    continue
                if "製作商:" in text or "制作商:" in text:
                    a_tag = p.select_one("a")
                    if a_tag:
                        producer = a_tag.get_text(strip=True)
                    continue
                if "發行商:" in text or "发行商:" in text:
                    a_tag = p.select_one("a")
                    if a_tag:
                        publisher = a_tag.get_text(strip=True)
                    continue
                if "系列:" in text or "系列:" in text:
                    a_tag = p.select_one("a")
                    if a_tag:
                        series = a_tag.get_text(strip=True)
                    continue

        # 演员
        actors: List[str] = []
        for star_span in soup.select("span.star-toggle"):
            name = star_span.get_text(strip=True)
            if name:
                actors.append(name)
        # 也尝试 a.star 链接
        if not actors:
            for star_a in soup.select("a[href*='/star/']"):
                name = star_a.get_text(strip=True)
                if name:
                    actors.append(name)

        # 类别标签 - 清理空白和非打印字符
        tags: List[str] = []
        for genre_tag in soup.select("span.genre a[href*='/genre/']"):
            name = genre_tag.get_text(strip=True)
            # 移除零宽字符和不可见控制字符
            name = re.sub(r'[\u200b-\u200f\u2028-\u202f\u2060-\u2064\ufeff\u00a0]', '', name)
            name = name.strip()
            if name:
                tags.append(name)

        # 预览截图 - 优先使用 a.sample-box 的 href（高清图），降级使用 img.src（缩略图）
        thumbnail_images: List[str] = []
        for sample_a in soup.select("a.sample-box"):
            # href 指向高清原图（如 https://pics.dmm.co.jp/digital/video/abc/abc-1.jpg）
            href = str(sample_a.get("href", "")).strip()
            if href and href not in thumbnail_images:
                thumbnail_images.append(href)
        # 如果 href 没有获取到，降级使用 img 的 src（缩略图）
        if not thumbnail_images:
            for sample_a in soup.select("a.sample-box"):
                img_tag = sample_a.select_one("img")
                if img_tag:
                    src = str(img_tag.get("src", "")).strip()
                    if src and src not in thumbnail_images:
                        thumbnail_images.append(src)

        # 提取 JS 变量（用于磁力 AJAX）
        gid = _extract_js_var(html, "gid")
        uc = _extract_js_var(html, "uc") or "0"
        img_var = _extract_js_var(html, "img")

        return {
            "video_id": movie_id,
            "code": code,
            "title": title,
            "date": date,
            "video_length": video_length,
            "director": director,
            "producer": producer,
            "publisher": publisher,
            "series": series,
            "actors": actors,
            "tags": tags,
            "cover_url": cover_url,
            "thumbnail_images": thumbnail_images,
            "gid": gid,
            "uc": uc,
            "img": img_var,
        }

    def _parse_magnets(self, html: str) -> List[Dict[str, Any]]:
        """解析磁力链接 AJAX 返回的 HTML 表格。"""
        soup = BeautifulSoup(html, "lxml")
        magnets: List[Dict[str, Any]] = []
        for tr in soup.select("tr"):
            tds = tr.select("td")
            if len(tds) < 3:
                continue
            # 第一列：磁力链接
            magnet_a = tds[0].select_one("a")
            if not magnet_a:
                continue
            link = str(magnet_a.get("href", "")).strip()
            if not link.startswith("magnet:"):
                continue
            title = magnet_a.get_text(strip=True)

            # 第二列：大小
            size_a = tds[1].select_one("a")
            size = str(size_a.get_text(strip=True)) if size_a else ""

            # 第三列：日期
            date_span = tds[2].select_one("span")
            share_date = str(date_span.get_text(strip=True)) if date_span else ""

            # 是否高清 / 字幕（从 title / CSS class 判断）
            title_lower = title.lower()
            is_hd = "hd" in title_lower or "高清" in title
            has_subtitle = "sub" in title_lower or "字幕" in title or "字幕" in str(tr.get("class", []))

            magnets.append({
                "link": link,
                "title": title,
                "size": size,
                "is_hd": is_hd,
                "has_subtitle": has_subtitle,
                "share_date": share_date,
            })
        return magnets

    def _fetch_magnets(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        gid: str,
        uc: str,
        img: str,
    ) -> List[Dict[str, Any]]:
        """通过 AJAX 获取磁力链接。"""
        domain = self._domain(config)
        floor = math.floor(random.random() * 1000 + 1)
        url = (
            f"{domain}/ajax/uncledatoolsbyajax.php"
            f"?gid={gid}&lang=zh&img={quote(img)}&uc={uc}&floor={floor}"
        )
        html = self._request(session, config, url)
        if not html:
            return []
        return self._parse_magnets(str(html))

    def _parse_star_id_from_detail(self, html: str) -> Dict[str, str]:
        """从详情页提取演员 ID -> 名称映射。"""
        soup = BeautifulSoup(html, "lxml")
        result: Dict[str, str] = {}
        for star_a in soup.select("a[href*='/star/']"):
            href = star_a.get("href", "")
            sid = href.rstrip("/").rsplit("/", 1)[-1] if "/" in href else ""
            name = star_a.get_text(strip=True)
            if sid and name:
                result[sid] = name
        return result

    # ---------- 数据转换 ----------

    def _to_video_summary(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """搜索结果项转为宿主统一视频摘要格式。"""
        return {
            "video_id": item.get("video_id", ""),
            "title": item.get("title", ""),
            "code": item.get("code", ""),
            "cover_url": item.get("cover_url", ""),
            "date": item.get("date", ""),
            "platform": JAVBUS_PLATFORM,
            "host_id": f'{JAVBUS_HOST_ID_PREFIX}{item.get("video_id", "")}',
        }

    # ---------- 协议入口 ----------

    def execute(self, capability: str, params: Dict[str, Any], context: Dict[str, Any], config: Dict[str, Any]):
        normalized = self.normalize_config(config)
        if not _as_bool(normalized.get("enabled"), True):
            raise RuntimeError("JavBus 插件未启用。")

        if capability == "health.query.status":
            return self.get_query_status(config)

        session = self._build_session(normalized)

        if capability == "catalog.search":
            return self._handle_search(session, normalized, params)
        if capability == "catalog.detail":
            return self._handle_detail(session, normalized, params)
        if capability == "person.search":
            return self._handle_person_search(session, normalized, params)
        if capability == "person.works":
            return self._handle_person_works(session, normalized, params)
        if capability == "asset.cover.fetch":
            return self._handle_cover_fetch(session, normalized, params)
        if capability == "asset.preview.resolve":
            return self._handle_preview_resolve(session, normalized, params)

        if capability == "playback.proxy.url":
            return self._handle_proxy_url(session, normalized, params)

        if capability == "playback.proxy.stream":
            return self._handle_proxy_stream(session, normalized, params)

        if capability == "transport.http.request":
            return self._handle_http_request(session, normalized, params)

        raise ValueError(f"unsupported capability: {capability}")

    # ---------- 能力实现 ----------

    def _handle_search(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        keyword = str(params.get("keyword") or params.get("query") or "").strip()
        page = _as_int(params.get("page"), 1, 1, 10000)
        max_pages = _as_int(params.get("max_pages"), 1, 1, 100)
        domain = self._domain(config)

        all_videos: List[Dict] = []
        has_next = False

        for p in range(page, page + max_pages):
            # JavBus 搜索 URL：/search/{keyword} 或 /search/{keyword}/{page}
            encoded_keyword = quote(keyword)
            if p == 1:
                search_url = f"{domain}/search/{encoded_keyword}"
            else:
                search_url = f"{domain}/search/{encoded_keyword}/{p}"

            html = self._request(session, config, search_url)
            if not html:
                break

            items = self._parse_search_results(str(html))
            if not items:
                break

            for item in items:
                # 将封面图的相对路径转为绝对 URL
                cover = str(item.get("cover_url") or "").strip()
                if cover:
                    item["cover_url"] = _abs_url(cover, domain)
                all_videos.append(self._to_video_summary(item))

            # 检查是否有下一页：翻页链接是否存在
            soup = BeautifulSoup(str(html), "lxml")
            next_link = soup.select_one(f'a[href*="/search/{encoded_keyword}/{p + 1}"]')
            has_next = next_link is not None
            if not has_next:
                break

        return {
            "page": page,
            "has_next": has_next,
            "videos": all_videos,
            "keyword": keyword,
        }

    def _handle_detail(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        video_id = str(params.get("video_id") or params.get("id") or "").strip()
        if not video_id:
            raise RuntimeError("catalog.detail 缺少 video_id 参数。")

        # 移除 host_id 前缀
        raw_id = video_id
        prefix_upper = JAVBUS_HOST_ID_PREFIX.upper()
        if raw_id.upper().startswith(prefix_upper):
            raw_id = raw_id[len(prefix_upper):]

        domain = self._domain(config)
        url = f"{domain}/{raw_id}"
        html = self._request(session, config, url)
        if not html:
            return {"video_id": video_id, "found": False}

        detail = self._parse_detail_page(str(html), raw_id)
        if not detail:
            return {"video_id": video_id, "found": False}

        # 将相对路径转为绝对 URL
        cover = str(detail.get("cover_url") or "").strip()
        if cover:
            detail["cover_url"] = _abs_url(cover, domain)
        thumbs = detail.get("thumbnail_images") or []
        if thumbs:
            detail["thumbnail_images"] = [_abs_url(str(t), domain) for t in thumbs]

        # 获取磁力链接
        magnets: List[Dict] = []
        if detail.get("gid"):
            try:
                magnets = self._fetch_magnets(
                    session, config,
                    gid=detail["gid"],
                    uc=detail["uc"],
                    img=detail.get("img", ""),
                )
            except Exception:
                pass

        detail["magnets"] = magnets
        return {"videos": [detail]}

    def _handle_person_search(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> List[Dict]:
        """搜索演员。

        JavBus 没有专用的演员搜索页面，策略：
        1. 用关键词搜索视频
        2. 取前 5 个结果的详情页，从中提取演员名和 ID
        """
        actor_name = str(params.get("actor_name") or params.get("keyword") or "").strip()
        if not actor_name:
            return []

        domain = self._domain(config)
        search_url = f"{domain}/search/{quote(actor_name)}"
        html = self._request(session, config, search_url)
        if not html:
            return []

        items = self._parse_search_results(str(html))
        if not items:
            return []

        # 取前 5 个结果获取详情以提取演员
        seen_actors: Dict[str, str] = {}
        for item in items[:5]:
            mid = item.get("video_id", "")
            if not mid:
                continue
            try:
                detail_html = self._request(session, config, f"{domain}/{mid}")
                if detail_html:
                    stars = self._parse_star_id_from_detail(str(detail_html))
                    for sid, sname in stars.items():
                        if sid not in seen_actors:
                            seen_actors[sid] = sname
            except Exception:
                continue

        # 如果没提取到演员，把搜索关键词本身作为演员名返回
        if not seen_actors:
            seen_actors[actor_name] = actor_name

        actors = [
            {"actor_id": sid, "name": name, "avatar": ""}
            for sid, name in seen_actors.items()
        ]
        return actors

    def _handle_person_works(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """获取演员作品列表。

        JavBus 演员页：/star/{actor_id}?page={n}
        """
        actor_id = str(params.get("actor_id") or "").strip()
        page = _as_int(params.get("page"), 1, 1, 10000)
        max_pages = _as_int(params.get("max_pages"), 1, 1, 100)
        domain = self._domain(config)

        if not actor_id:
            return {"works": [], "page": page, "has_more": False}

        all_works: List[Dict] = []
        has_more = False

        for p in range(page, page + max_pages):
            if p == 1:
                star_url = f"{domain}/star/{actor_id}"
            else:
                star_url = f"{domain}/star/{actor_id}/{p}"

            html = self._request(session, config, star_url)
            if not html:
                break

            items = self._parse_search_results(str(html))
            if not items:
                break

            for item in items:
                summary = self._to_video_summary(item)
                all_works.append({
                    "video_id": summary["video_id"],
                    "title": summary["title"],
                    "code": summary["code"],
                    "cover_url": summary["cover_url"],
                    "date": summary.get("date", ""),
                    "platform": JAVBUS_PLATFORM,
                })

            soup = BeautifulSoup(str(html), "lxml")
            next_link = soup.select_one(f'a[href*="/star/{actor_id}/{p + 1}"]')
            has_more = next_link is not None
            if not has_more:
                break

        return {
            "works": all_works,
            "page": page,
            "has_more": has_more,
        }

    def _handle_cover_fetch(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        video_id = str(params.get("album_id") or params.get("video_id") or "").strip()
        save_path = str(params.get("save_path") or "").strip()
        if not video_id or not save_path:
            raise RuntimeError("asset.cover.fetch 缺少 video_id 或 save_path。")

        raw_id = video_id
        prefix_upper = JAVBUS_HOST_ID_PREFIX.upper()
        if raw_id.upper().startswith(prefix_upper):
            raw_id = raw_id[len(prefix_upper):]

        domain = self._domain(config)
        url = f"{domain}/{raw_id}"
        html = self._request(session, config, url)
        if not html:
            return {"detail": {"video_id": video_id, "found": False}, "success": False}

        detail = self._parse_detail_page(str(html), raw_id)
        if not detail:
            return {"detail": {"video_id": video_id, "found": False}, "success": False}

        cover_url = detail.get("cover_url", "")
        if not cover_url:
            return {"detail": {"video_id": video_id, "found": True}, "success": False}

        ok = self._download_file(session, config, cover_url, save_path)
        result_detail = {
            "video_id": detail["video_id"],
            "code": detail["code"],
            "title": detail.get("title", ""),
            "cover_url": cover_url,
            "cover_path": save_path if ok else "",
            "platform": JAVBUS_PLATFORM,
        }
        return {"detail": result_detail, "success": ok}

    def _handle_preview_resolve(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> List[str]:
        video_id = str(params.get("album_id") or params.get("video_id") or "").strip()
        if not video_id:
            return []

        raw_id = video_id
        prefix_upper = JAVBUS_HOST_ID_PREFIX.upper()
        if raw_id.upper().startswith(prefix_upper):
            raw_id = raw_id[len(prefix_upper):]

        domain = self._domain(config)
        url = f"{domain}/{raw_id}"
        html = self._request(session, config, url)
        if not html:
            return []

        detail = self._parse_detail_page(str(html), raw_id)
        if not detail:
            return []

        return detail.get("thumbnail_images", [])

    def _handle_proxy_url(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ):
        """处理 playback.proxy.url — 代理图片/资源请求。"""
        method = str(params.get("method") or "GET").upper()
        query_string = str(params.get("query_string") or "").strip()
        body_url = str(params.get("body_url") or "").strip()
        incoming_headers = dict(params.get("incoming_headers") or {})

        # 从 query_string 中提取目标 URL（base64 编码）
        target_url = body_url
        if not target_url and query_string:
            parsed = parse_qs(query_string)
            url_param = parsed.get("url", [])
            if url_param:
                encoded = url_param[0]
                try:
                    target_url = base64.b64decode(encoded).decode("utf-8")
                except Exception:
                    target_url = encoded

        if not target_url:
            raise ValueError("proxy.url: missing target URL")

        timeout = _as_int(config.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 600)

        # 带 Cookie/Referer 请求目标资源
        req_headers = {}
        if incoming_headers.get("Range"):
            req_headers["Range"] = incoming_headers["Range"]
        if _is_javbus_cdn_url(target_url):
            req_headers["Referer"] = "https://www.javbus.com/"

        response = session.get(
            target_url,
            headers=req_headers,
            impersonate="chrome",
            timeout=timeout,
            stream=False,
        )
        return response

    def _handle_proxy_stream(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ):
        """处理 playback.proxy.stream — 代理流式请求。"""
        method = str(params.get("method") or "GET").upper()
        domain = str(params.get("domain") or "").strip()
        path = str(params.get("path") or "").strip()
        query_string = str(params.get("query_string") or "").strip()
        incoming_referer = str(params.get("incoming_referer") or "").strip()

        # JavBus 没有在线播放功能，返回 501
        return type("ProxyResponse", (), {
            "content": b"JavBus does not support streaming",
            "status_code": 501,
            "headers": [("Content-Type", "text/plain")],
        })()

    def _handle_http_request(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ):
        """处理 transport.http.request — 通用 HTTP 请求。"""
        method = str(params.get("method") or "GET").upper()
        url = str(params.get("url") or "").strip()
        req_headers = dict(params.get("headers") or {})
        stream = bool(params.get("stream", False))
        timeout = _as_int(
            params.get("timeout") or config.get("timeout_seconds"),
            DEFAULT_TIMEOUT_SECONDS, 1, 600,
        )
        allow_redirects = bool(params.get("allow_redirects", True))

        if not url:
            raise ValueError("http.request: missing URL")

        # 对 JavBus CDN 资源自动补充 Referer
        if _is_javbus_cdn_url(url) and "Referer" not in req_headers:
            req_headers["Referer"] = "https://www.javbus.com/"

        response = session.request(
            method=method,
            url=url,
            headers=req_headers,
            impersonate="chrome",
            timeout=timeout,
            stream=stream,
            allow_redirects=allow_redirects,
        )
        return response
