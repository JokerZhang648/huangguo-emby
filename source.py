import errno
import io
import json
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from http.client import HTTPException, IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SITE = "https://huangguoai.com"
IMAGE_BASE = "https://pic.qnlycc.cn/"
CDN_DOMAINS = ("qnlycc.cn", "tjyyjk.cn", "hzjvlo.cn", "drleao.cn")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class SourceError(Exception):
    def __init__(self, message: str, status: int | None = None, *, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def transient_disconnect(error: Exception) -> bool:
    reason = error.reason if isinstance(error, URLError) else error
    return (
        isinstance(reason, (TimeoutError, ConnectionResetError, ConnectionAbortedError,
                            ssl.SSLEOFError, IncompleteRead))
        or isinstance(reason, ssl.SSLError) and getattr(reason, "reason", None) == "UNEXPECTED_EOF_WHILE_READING"
    )


def describe_connection_failure(error: Exception) -> str:
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, socket.gaierror):
        return f"DNS 解析失败，错误码 {reason.errno}"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"TLS 证书验证失败，错误码 {reason.verify_code}"
    if isinstance(reason, ssl.SSLError):
        return f"TLS 握手失败，{getattr(reason, 'reason', None) or type(reason).__name__}"
    if isinstance(reason, TimeoutError):
        return "连接超时"
    if isinstance(reason, OSError):
        if reason.errno is not None:
            symbol = errno.errorcode.get(reason.errno, "系统连接错误")
            return f"{symbol}，errno {reason.errno}"
        # http.client creates a bare OSError when a configured proxy refuses CONNECT.
        if match := re.match(r"Tunnel connection failed: ([0-9]{3})\b", str(reason)):
            return f"代理隧道返回 HTTP {match[1]}"
    return type(reason).__name__


def host_matches(url: str, domains: tuple[str, ...], subdomains: bool = False) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and any(host == name or subdomains and host.endswith("." + name) for name in domains)
        )
    except ValueError:
        return False


class ScopedRedirects(HTTPRedirectHandler):
    max_redirections = 4
    max_repeats = 2

    def __init__(self, permitted):
        self.permitted = permitted

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.permitted(newurl):
            raise SourceError("上游重定向到了未配置的域名")
        redirect = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirect is not None and urlsplit(req.full_url).hostname != urlsplit(newurl).hostname:
            for header in list(redirect.headers):
                if header.lower().startswith("x-"):
                    redirect.remove_header(header)
        return redirect


VIDEO_ID_PATH = re.compile(r"/video/([1-9][0-9]{0,15})/?")


def extract_video_ids(html: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in VIDEO_ID_PATH.finditer(html):
        seen[match.group(1)] = None
    return list(seen)


class CatalogEpisodes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.counts: dict[str, int] = {}
        self.video_id = None
        self.in_episode_label = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "div" and "hg-drama-card" in classes:
            self.video_id = attributes.get("data-track-item-id")
            self.in_episode_label = False
        elif tag == "span" and "hg-drama-card__episode" in classes:
            self.in_episode_label = True

    def handle_data(self, data):
        if self.video_id and self.in_episode_label:
            count = re.search(r"([1-9][0-9]{0,4})\s*集", data)
            if count:
                self.counts.setdefault(self.video_id, int(count[1]))

    def handle_endtag(self, tag):
        if tag == "span":
            self.in_episode_label = False


def extract_catalog_entries(html: str) -> dict[str, int | None]:
    cards = CatalogEpisodes()
    cards.feed(html)
    return {video_id: cards.counts.get(video_id) for video_id in extract_video_ids(html)}


@dataclass(frozen=True)
class Playback:
    url: str
    headers: dict[str, str]


def read_bounded(response, limit: int, label: str) -> bytes:
    try:
        data = response.read(limit + 1)
    except (HTTPException, OSError, ValueError) as exc:
        raise SourceError(f"{label}读取中断", retryable=transient_disconnect(exc)) from exc
    if len(data) > limit:
        raise SourceError(f"{label}超过大小限制")
    return data


class Huangguo:
    def __init__(self, extra_cdn_domains: tuple[str, ...] = ()):
        self.cdn_domains = CDN_DOMAINS + extra_cdn_domains
        self.site_opener = build_opener(
            ScopedRedirects(lambda url: host_matches(url, ("huangguoai.com", "www.huangguoai.com")))
        )
        self.cdn_opener = build_opener(ScopedRedirects(self.is_cdn))
        self.headers = {"User-Agent": USER_AGENT, "Origin": SITE, "Referer": SITE + "/"}

    def is_cdn(self, url: str) -> bool:
        return host_matches(url, self.cdn_domains, subdomains=True)

    def catalog_section(self) -> str:
        section = os.environ.get("HUANGGUO_CATALOG", "original").strip().lower()
        if section not in ("original", "newest"):
            raise SourceError("HUANGGUO_CATALOG 须为 original 或 newest")
        return section

    @staticmethod
    def list_path(section: str, page_number: int) -> str:
        if section == "original":
            return "/original/" if page_number == 1 else f"/original/{page_number}/"
        return "/newest" if page_number == 1 else f"/newest/{page_number}/"

    @staticmethod
    def list_label(section: str) -> str:
        return "黄果原创页" if section == "original" else "最近上新页"

    def _fetch_list_page(self, section: str, page_number: int, pause) -> dict[str, int | None]:
        base_label = self.list_label(section)
        label = base_label if page_number == 1 else f"{base_label}第 {page_number} 页"
        path = self.list_path(section, page_number)
        pause()
        try:
            with self._open(self.site_opener, SITE + path, self.headers, label) as response:
                html = read_bounded(response, 3 * 1024 * 1024, label)
        except SourceError as exc:
            # 黄果原创列表超出末页会直接 404，与 /newest 不同
            if exc.status == 404 and page_number > 1:
                return {}
            raise
        return extract_catalog_entries(html.decode("utf-8", errors="replace"))

    def _open(self, opener, url: str, headers: dict, label: str, body: bytes | None = None):
        host = urlsplit(url).hostname
        for attempt, delay in enumerate((0, 1, 2, 4)):
            if delay:
                time.sleep(delay)
            try:
                return opener.open(Request(url, data=body, headers=headers), timeout=20)
            except HTTPError as exc:
                exc.close()
                err = SourceError(
                    f"{label}返回 HTTP {exc.code}", exc.code, retryable=exc.code in (408, 502, 503, 504)
                )
                if attempt == 3 or not err.retryable:
                    raise err from None
            except (URLError, OSError, ValueError) as exc:
                reason = describe_connection_failure(exc)
                err = SourceError(
                    f"{label}连接失败（{host}，{reason}）", retryable=transient_disconnect(exc)
                )
                if attempt == 3 or not err.retryable:
                    raise err from None
        raise SourceError(f"{label}连接失败（{host}）")  # pragma: no cover

    def catalog_entries(self, minimum: int, pause) -> dict[str, int | None]:
        section = self.catalog_section()
        label = self.list_label(section)
        entries: dict[str, int | None] = {}
        page_number = 1
        while minimum <= 0 or len(entries) < minimum:
            page_entries = self._fetch_list_page(section, page_number, pause)
            before = len(entries)
            for video_id, episode_count in page_entries.items():
                if entries.get(video_id) is None:
                    entries[video_id] = episode_count
            if len(entries) == before:
                if minimum > 0:
                    raise SourceError(f"{label}第 {page_number} 页没有更多剧目，目前共 {before} 部")
                if page_number == 1:
                    raise SourceError(f"{label}没有剧目")
                break
            page_number += 1
        return dict(list(entries.items())[:minimum]) if minimum > 0 else entries

    def catalog(self, pause) -> list[str]:
        return list(self.catalog_entries(0, pause))

    def newest(self, minimum: int, pause) -> list[str]:
        return list(self.catalog_entries(minimum, pause))

    def _api(self, path: str, label: str, post: bool = False) -> dict:
        headers = {**self.headers, "Accept": "application/json"}
        if post:
            headers["Content-Type"] = "application/json"
        with self._open(self.site_opener, SITE + path, headers, label, b"{}" if post else None) as response:
            data = read_bounded(response, 2 * 1024 * 1024, label)
        try:
            response_data = json.loads(data)
        except (UnicodeError, ValueError):
            raise SourceError(f"{label}未返回有效 JSON") from None
        if not isinstance(response_data, dict) or response_data.get("status") not in (1, "1"):
            raise SourceError(f"{label}返回业务失败")
        result = response_data.get("data")
        if not isinstance(result, dict):
            raise SourceError(f"{label}响应缺少有效数据")
        return result

    def detail(self, video_id: str) -> dict:
        result = self._api(f"/api/videos/{video_id}", "短剧详情")
        if str(result.get("id")) != video_id:
            raise SourceError("短剧详情 ID 不匹配")
        return result

    def playback(self, video_id: str, episode: int) -> Playback:
        data = self._api(f"/api/videos/{video_id}/episodes/{episode}/playback", "播放接口", post=True)
        if isinstance(data.get("data"), dict):
            data = data["data"]
        streams = data.get("streams") or {}
        cdn = data.get("cdn") or {}
        if not isinstance(streams, dict) or not isinstance(cdn, dict):
            raise SourceError("播放响应格式异常")
        url = streams.get("main") or streams.get("hevc") or data.get("video_url")
        if not isinstance(url, str) or not self.is_cdn(url):
            raise SourceError("播放响应缺少受支持的 CDN 地址")
        if str(data.get("manifest_method") or "GET").upper() != "GET":
            raise SourceError("上游要求使用暂不支持的清单请求方法")
        headers = {**self.headers, "Referer": f"{SITE}/video/{video_id}/", "Accept": "*/*"}
        key = cdn.get("authKey") or cdn.get("auth_key")
        value = cdn.get("authValue") or cdn.get("auth_value")
        if isinstance(key, str) and isinstance(value, str) and value:
            if not re.fullmatch(r"x-[a-z0-9-]+", key, re.IGNORECASE) or not value.isascii() or not value.isprintable():
                raise SourceError("播放接口返回了无效的 CDN 请求头")
            headers[key] = value
        return Playback(url, headers)

    def open_media(self, url: str, headers: dict[str, str]):
        if not self.is_cdn(url):
            raise SourceError("播放资源来自未配置的 CDN")
        return self._open(self.cdn_opener, url, headers, "媒体 CDN")

    def poster(self, cover_url: str) -> bytes:
        url = urljoin(IMAGE_BASE, cover_url)
        if not self.is_cdn(url):
            raise SourceError("封面来自未配置的 CDN")
        for delay in (1, 2, None):
            try:
                with self._open(self.cdn_opener, url, self.headers, "封面 CDN") as response:
                    data = read_bounded(response, 8 * 1024 * 1024, "封面")
            except SourceError as exc:
                if delay is None or not exc.retryable:
                    raise
                time.sleep(delay)
            else:
                return poster_jpeg(data)


def poster_jpeg(data: bytes) -> bytes:
    from Crypto.Cipher import AES
    from PIL import Image, ImageOps, UnidentifiedImageError

    def encode(body: bytes) -> bytes:
        with Image.open(io.BytesIO(body)) as source:
            if source.width * source.height > 24_000_000:
                raise SourceError("封面尺寸超过限制")
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1200, 1800))
            output = io.BytesIO()
            image.save(output, "JPEG", quality=90, optimize=True)
            return output.getvalue()

    try:
        return encode(data)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        if not data or len(data) % AES.block_size:
            raise SourceError("封面响应无法识别为图片") from None
    # The public web client uses these format constants for CDN images.
    decrypted = AES.new(b"f5d965df75336270", AES.MODE_CBC, b"97b60394abc2fbe1").decrypt(data)
    try:
        return encode(decrypted)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise SourceError("封面解码失败") from None
