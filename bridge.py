import hashlib
import json
import logging
import math
import os
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from library import BEIJING_TIME, ID_PATTERN, Library
from source import Huangguo, Playback, SourceError, read_bounded


LOG = logging.getLogger(__name__)
PLAY_ROUTE = re.compile(rf"/play/({ID_PATTERN})/([1-9][0-9]{{0,4}})/index\.m3u8")
ASSET_ROUTE = re.compile(r"/asset/([a-zA-Z0-9_-]{24})/([a-f0-9]{32})\.[a-z0-9]+")
URI_ATTR = re.compile(r'(?<=[,:])URI="([^"\r\n]+)"')
HLS_TYPE = "application/vnd.apple.mpegurl"


@dataclass(frozen=True)
class Settings:
    public_url: str
    media: Path
    state: Path
    minimum: int
    interval: float
    delay: float
    extra_cdn_domains: tuple[str, ...]

    @classmethod
    def read(cls):
        public_url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if public_url:
            url = urlsplit(public_url)
            if (url.scheme not in ("http", "https") or not url.hostname or url.hostname == "nas_ip"
                    or url.path or url.query or url.fragment or url.username is not None):
                raise ValueError("PUBLIC_BASE_URL 须为可访问的 http(s) 根地址，且无路径或查询串")
            if url.port == 0:
                raise ValueError("PUBLIC_BASE_URL 的端口必须大于 0")
        else:
            public_url = "http://127.0.0.1:8787"
        minimum = int(os.environ.get("LATEST_COUNT", "0"))
        interval = float(os.environ.get("SYNC_INTERVAL_MINUTES", "60"))
        delay = float(os.environ.get("SYNC_DELAY_SECONDS", "1"))
        if minimum < 0:
            raise ValueError("LATEST_COUNT 不能为负数")
        if not math.isfinite(interval) or interval <= 0 or not math.isfinite(delay) or delay < 0:
            raise ValueError("同步间隔必须大于 0，请求间隔必须大于或等于 0")
        domains = tuple(
            domain.strip().lower() for domain in os.environ.get("EXTRA_CDN_DOMAINS", "").split(",")
            if domain.strip()
        )
        if any(not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", domain) for domain in domains):
            raise ValueError("EXTRA_CDN_DOMAINS 用逗号分隔域名，无需填写协议")
        media = Path(os.environ.get("MEDIA_DIR", "media")).resolve()
        state = Path(os.environ.get("STATE_DIR", "state")).resolve()
        if media == state or state.is_relative_to(media):
            raise ValueError("STATE_DIR 须位于 Emby 媒体目录之外")
        return cls(public_url, media, state, minimum, interval, delay, domains)


@dataclass(frozen=True)
class Resource:
    url: str
    kind: str

    @property
    def playlist(self):
        return self.kind == "playlist" or urlsplit(self.url).path.lower().endswith(".m3u8")

    @property
    def extension(self):
        if self.playlist:
            return "m3u8"
        if self.kind == "key":
            return "key"
        suffix = Path(urlsplit(self.url).path).suffix.removeprefix(".").lower()
        return suffix if re.fullmatch(r"[a-z0-9]{1,8}", suffix) else "ts"


@dataclass
class Session:
    token: str
    playback: Playback
    created: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    resources: dict[str, Resource] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, resource: Resource) -> str:
        digest = hashlib.sha256(f"{resource.kind}\n{resource.url}".encode("utf-8")).hexdigest()[:32]
        with self.lock:
            if digest not in self.resources and len(self.resources) >= 8000:
                raise SourceError("播放会话的资源数量超过限制")
            self.resources[digest] = resource
        return f"/asset/{self.token}/{digest}.{resource.extension}"

    def lookup(self, digest: str) -> Resource | None:
        with self.lock:
            return self.resources.get(digest)


class Sessions:
    def __init__(self, source: Huangguo):
        self.source = source
        self.by_token: OrderedDict[str, Session] = OrderedDict()
        self.recent: dict[tuple[str, int], str] = {}
        self.lock = threading.Lock()

    def _prune(self, now: float):
        while self.by_token:
            oldest = next(iter(self.by_token.values()))
            if len(self.by_token) <= 64 and now - oldest.touched < 3600:
                break
            self.by_token.popitem(last=False)
        self.recent = {key: token for key, token in self.recent.items() if token in self.by_token}

    def root(self, video_id: str, episode: int, refresh: bool = False) -> Session:
        key = (video_id, episode)
        with self.lock:
            now = time.monotonic()
            self._prune(now)
            cached = self.by_token.get(self.recent.get(key, ""))
            if cached and not refresh and now - cached.created < 30:
                cached.touched = now
                self.by_token.move_to_end(cached.token)
                return cached
        playback = self.source.playback(video_id, episode)
        session = Session(secrets.token_urlsafe(18), playback)
        with self.lock:
            self.by_token[session.token] = session
            self.recent[key] = session.token
            self._prune(time.monotonic())
        return session

    def get(self, token: str) -> Session | None:
        with self.lock:
            now = time.monotonic()
            self._prune(now)
            if session := self.by_token.get(token):
                session.touched = now
                self.by_token.move_to_end(token)
            return session


def rewrite_playlist(raw: bytes, url: str, session: Session, source: Huangguo, public_url: str) -> bytes:
    try:
        lines = raw.decode("utf-8-sig").strip().splitlines()
    except UnicodeError:
        raise SourceError("CDN 清单的编码无效") from None
    if not lines or lines[0] != "#EXTM3U":
        raise SourceError("CDN 响应不是 HLS 清单")

    def rewrite(ref: str, kind: str) -> str:
        target = urljoin(url, ref)
        if not source.is_cdn(target):
            raise SourceError("HLS 清单包含未配置的 CDN")
        return public_url + session.register(Resource(target, kind))

    playlist_tags = ("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:", "#EXT-X-RENDITION-REPORT:")
    key_tags = ("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")
    output = []
    next_playlist = False
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("#"):
            if line.startswith("#EXT-X-STREAM-INF:"):
                next_playlist = True
            kind = "playlist" if line.startswith(playlist_tags) else "key" if line.startswith(key_tags) else "media"
            line = URI_ATTR.sub(lambda match: f'URI="{rewrite(match[1], kind)}"', line)
        elif line:
            line = rewrite(line, "playlist" if next_playlist else "media")
            next_playlist = False
        output.append(line)
    return ("\n".join(output) + "\n").encode("utf-8")


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, source: Huangguo, library: Library, public_url: str):
        super().__init__(address, Handler)
        self.source = source
        self.library = library
        self.public_url = public_url
        self.sessions = Sessions(source)
        self.request_slots = threading.BoundedSemaphore(32)


class Handler(BaseHTTPRequestHandler):
    server: BridgeServer

    def setup(self):
        self.request.settimeout(30)
        super().setup()

    def log_message(self, format, *args):
        return

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.server.request_slots.acquire(blocking=False):
            self._reply(503, b"Bridge busy\n", "text/plain")
            return
        try:
            request_url = urlsplit(self.path)
            path = request_url.path
            if path == "/healthz":
                self._reply(200, b"ok\n", "text/plain")
            elif path == "/status":
                body = json.dumps(self.server.library.status(), ensure_ascii=False, indent=2).encode("utf-8")
                self._reply(200, body + b"\n", "application/json; charset=utf-8")
            elif match := PLAY_ROUTE.fullmatch(path):
                self._play(match[1], int(match[2]), direct=request_url.query == "direct=1")
            elif match := ASSET_ROUTE.fullmatch(path):
                self._asset(match[1], match[2])
            else:
                self._reply(404, b"Not found\n", "text/plain")
        except SourceError as exc:
            LOG.warning("点播失败：%s", exc)
            self._reply(502, str(exc).encode("utf-8") + b"\n", "text/plain; charset=utf-8")
        except (HTTPException, OSError, ValueError):
            # Client disconnected or upstream broke after response headers.
            self.close_connection = True
        finally:
            self.server.request_slots.release()

    def _reply(self, status: int, body: bytes, mime: str):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _playlist(self, session: Session, resource: Resource):
        with self.server.source.open_media(resource.url, session.playback.headers) as response:
            raw = read_bounded(response, 2 * 1024 * 1024, "HLS 清单")
            data = rewrite_playlist(raw, response.url, session, self.server.source, self.server.public_url)
        self._reply(200, data, HLS_TYPE)

    def _play(self, video_id: str, episode: int, direct: bool = False):
        if not self.server.library.allows_playback(video_id, episode):
            self._reply(404, b"Episode not in library\n", "text/plain")
            return
        session = self.server.sessions.root(video_id, episode)
        if direct:
            url = session.playback.url
            if not url.isascii() or not url.isprintable():
                raise SourceError("CDN 播放地址格式异常")
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        try:
            self._playlist(session, Resource(session.playback.url, "playlist"))
        except SourceError as exc:
            if exc.status not in (401, 403):
                raise
            session = self.server.sessions.root(video_id, episode, refresh=True)
            self._playlist(session, Resource(session.playback.url, "playlist"))

    def _asset(self, token: str, digest: str):
        session = self.server.sessions.get(token)
        resource = session.lookup(digest) if session else None
        if resource is None:
            self._reply(404, b"Playback asset expired or unavailable\n", "text/plain")
        elif resource.playlist:
            self._playlist(session, resource)
        else:
            self._bytes(session, resource)

    def _bytes(self, session: Session, resource: Resource):
        headers = session.playback.headers.copy()
        if byte_range := self.headers.get("Range"):
            if not re.fullmatch(r"bytes=(?:[0-9]{1,20}-[0-9]{0,20}|-[0-9]{1,20})", byte_range):
                self._reply(400, b"Invalid Range\n", "text/plain")
                return
            headers["Range"] = byte_range
        try:
            response = self.server.source.open_media(resource.url, headers)
        except SourceError as exc:
            if exc.status == 416:
                self._reply(416, b"Range not satisfiable\n", "text/plain")
                return
            raise
        with response:
            self.send_response(response.status)
            self.send_header("Cache-Control", "no-store")
            for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                if value := response.headers.get(header):
                    self.send_header(header, value)
            self.end_headers()
            if self.command != "HEAD":
                while chunk := response.read(64 * 1024):
                    self.wfile.write(chunk)


def sync_loop(library: Library, interval: float):
    while True:
        try:
            ok = library.sync()
        except Exception:
            LOG.exception("同步没有完成")
            library.update_status(phase="degraded", errors=["同步发生异常，请查看容器日志"])
            ok = False
        wait_minutes = interval if ok else min(interval, 5)
        next_sync = datetime.now(BEIJING_TIME) + timedelta(minutes=wait_minutes)
        library.update_status(next_sync_bjt=next_sync.isoformat(timespec="seconds"))
        time.sleep(wait_minutes * 60)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        settings = Settings.read()
        source = Huangguo(settings.extra_cdn_domains)
        library = Library(source, settings.media, settings.state, settings.minimum, settings.delay, settings.public_url)
        server = BridgeServer(("0.0.0.0", 8787), source, library, settings.public_url)
    except (ValueError, OSError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
    if not os.environ.get("PUBLIC_BASE_URL", "").strip():
        LOG.warning("请设置 Emby 能访问的 PUBLIC_BASE_URL，当前分集入口只能在本机使用")
    catalog = Huangguo(settings.extra_cdn_domains).catalog_section()
    catalog_name = "黄果原创" if catalog == "original" else "最近上新"
    if settings.minimum <= 0:
        LOG.info("服务已启动，将同步%s中的全部剧目，计划每 %g 分钟扫描", catalog_name, settings.interval)
    else:
        LOG.info("服务已启动，同步%s前 %d 部，计划每 %g 分钟扫描", catalog_name, settings.minimum, settings.interval)
    with server:
        threading.Thread(target=sync_loop, args=(library, settings.interval), daemon=True).start()
        server.serve_forever()


def load_bridge_env() -> None:
    """宿主机直接运行 bridge.py 时载入 bridge.env（容器内由 Compose env_file 注入）。"""
    path = Path(__file__).resolve().parent / "bridge.env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def check_upstream() -> int:
    try:
        source = Huangguo()
        section = source.catalog_section()
        source.newest(1, lambda: None)
    except SourceError as exc:
        print(exc, file=sys.stderr)
        return 1
    label = Huangguo.list_label(section)
    print(f"{label}可访问，已读取剧目")
    return 0


if __name__ == "__main__":
    load_bridge_env()
    if sys.argv[1:] == ["--check-upstream"]:
        sys.exit(check_upstream())
    main()
