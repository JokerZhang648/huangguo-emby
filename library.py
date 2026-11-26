import json
import logging
import os
import re
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from source import Huangguo, SourceError


LOG = logging.getLogger(__name__)
BEIJING_TIME = timezone(timedelta(hours=8))
ID_PATTERN = r"[1-9][0-9]{0,15}"
HUANGGUO_SUBDIR = os.environ.get("HUANGGUO_SUBDIR", "huangguo")
SERIES_DIR = re.compile(rf" \[HG-{ID_PATTERN}\]$")


@dataclass(frozen=True)
class SeriesAddition:
    title: str
    new_series: bool
    episodes: tuple[int, ...]


def migrate_series_dirs(media_root: Path, library_dir: Path) -> None:
    library_dir.mkdir(parents=True, exist_ok=True)
    if not media_root.is_dir():
        return
    for path in list(media_root.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        if path.name == HUANGGUO_SUBDIR or not SERIES_DIR.search(path.name):
            continue
        target = library_dir / path.name
        if target.exists():
            LOG.warning("跳过迁移 %s：%s 下已有同名目录", path.name, HUANGGUO_SUBDIR)
            continue
        path.rename(target)
        LOG.info("已将剧目目录迁入 %s：%s", HUANGGUO_SUBDIR, path.name)


def clean_text(value) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", value).strip()


def file_title(title: str) -> str:
    name = re.sub(r'[\\/:"*?<>|\s]+', " ", title)
    return name.encode("utf-8")[:110].decode("utf-8", errors="ignore").strip(" .") or "黄果短剧"


def atomic_write(path: Path, data: bytes) -> None:
    try:
        if path.read_bytes() == data:
            return
    except FileNotFoundError:
        pass
    with tempfile.NamedTemporaryFile(prefix=".writing-", suffix=".tmp", dir=path.parent, delete=False) as file:
        scratch = Path(file.name)
        try:
            file.write(data)
        except BaseException:
            file.close()
            scratch.unlink(missing_ok=True)
            raise
    try:
        scratch.chmod(0o644)
        scratch.replace(path)
    finally:
        scratch.unlink(missing_ok=True)


def xml_bytes(root: ET.Element) -> bytes:
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def names(rows) -> list[str]:
    if not isinstance(rows, list):
        return []
    found: dict[str, None] = {}
    for row in rows:
        if isinstance(row, dict):
            row = row.get("name") or row.get("title") or row.get("value")
        if name := clean_text(row):
            found[name] = None
    return list(found)


def tvshow_nfo(video_id: str, title: str, detail: dict, finished: bool) -> bytes:
    root = ET.Element("tvshow")
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "plot").text = clean_text(detail.get("description") or detail.get("desc"))
    ET.SubElement(root, "status").text = "Ended" if finished else "Continuing"
    ET.SubElement(root, "uniqueid", type="huangguo", default="true").text = video_id
    for tag in names(detail.get("tags")):
        ET.SubElement(root, "genre").text = tag
    for credit in names(detail.get("actors")):
        ET.SubElement(ET.SubElement(root, "actor"), "name").text = credit
    return xml_bytes(root)


def episode_nfo(number: int, title: str) -> bytes:
    root = ET.Element("episodedetails")
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "season").text = "1"
    ET.SubElement(root, "episode").text = str(number)
    return xml_bytes(root)


def episode_titles(detail: dict) -> dict[int, str]:
    rows = detail.get("episodes")
    if not isinstance(rows, list):
        raise SourceError("详情缺少分集列表")
    episodes = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("status", 1) not in (1, "1"):
            continue
        index = row.get("ep_num", row.get("episode", row.get("index")))
        if re.fullmatch(r"[1-9][0-9]{0,4}", str(index)):
            number = int(index)
            episodes[number] = clean_text(row.get("title")) or f"第 {number} 集"
    if not episodes:
        raise SourceError("详情暂无可发布的正片分集")
    return episodes


class Library:
    def __init__(self, source: Huangguo, media_root: Path, state: Path, minimum: int, delay: float,
                 public_url: str | None = None, *, daily_sync: bool = False):
        self.source = source
        self.media_root = media_root.resolve()
        self.media = self.media_root / HUANGGUO_SUBDIR
        self.minimum = minimum
        self.delay = delay
        self.daily_sync = daily_sync
        base_url = public_url or os.environ.get("PUBLIC_BASE_URL", "").strip()
        self.public_url = (base_url or "http://127.0.0.1:8787").rstrip("/")
        migrate_series_dirs(self.media_root, self.media)
        state.mkdir(parents=True, exist_ok=True)
        self.index_path = state / "index.json"
        try:
            self.records = json.loads(self.index_path.read_bytes())
            if not isinstance(self.records, dict):
                raise ValueError("index is not an object")
        except FileNotFoundError:
            self.records = {}
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError("同步索引无法读取，请检查 state/index.json") from exc
        self.lock = threading.RLock()
        target = minimum if minimum > 0 else 0
        self.progress = {
            "phase": "waiting",
            "latest_target": target,
            "latest_ready": 0,
            "last_sync_bjt": None,
            "next_sync_bjt": None,
        }

    def status(self) -> dict:
        with self.lock:
            return {
                **self.progress,
                "series_total": len(self.records),
                "episodes_total": sum(len(record["episodes"]) for record in self.records.values()),
            }

    def update_status(self, **info):
        with self.lock:
            self.progress.update(info)

    def episode_snapshot(self) -> dict[str, frozenset[int]]:
        with self.lock:
            return {video_id: frozenset(record["episodes"]) for video_id, record in self.records.items()}

    def additions_since(self, snapshot: dict[str, frozenset[int]]) -> list[SeriesAddition]:
        with self.lock:
            additions = []
            for video_id, record in self.records.items():
                previous = snapshot.get(video_id)
                episodes = set(record["episodes"]).difference(previous or ())
                if episodes:
                    title = record.get("title") or record["folder"].removesuffix(f" [HG-{video_id}]")
                    additions.append(SeriesAddition(title, previous is None, tuple(sorted(episodes))))
            return additions

    def allows_playback(self, video_id: str, number: int) -> bool:
        with self.lock:
            return number in self.records.get(video_id, {}).get("episodes", ())

    def _save(self, video_id: str, record: dict):
        # Only the sync thread writes the index. Readers see a record after its files are ready.
        candidate = {**self.records, video_id: record}
        data = json.dumps(candidate, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        atomic_write(self.index_path, data)
        with self.lock:
            self.records[video_id] = record

    def _folder(self, video_id: str, record: dict, title: str) -> Path:
        suffix = f" [HG-{video_id}]"
        existing = record.get("folder", "")
        if existing.endswith(suffix) and "/" not in existing and "\\" not in existing:
            return self.media / existing
        for path in self.media.iterdir():
            if path.name.endswith(suffix) and path.is_dir() and not path.is_symlink():
                return path
        return self.media / (file_title(title) + suffix)

    def _pause(self):
        time.sleep(self.delay)

    def _write_stream_files(self, video_id: str, folder: Path, episodes: list[int], replace: bool):
        season = folder / "Season 01"
        if folder.parent != self.media or folder.is_symlink() or season.is_symlink():
            raise SourceError("媒体目录指向外部位置")
        prefix = folder.name.removesuffix(f" [HG-{video_id}]")
        for number in episodes:
            path = season / f"{prefix} S01E{number:02d}.strm"
            if replace or not path.is_file():
                url = f"{self.public_url}/play/{video_id}/{number}/index.m3u8?direct=1\n"
                atomic_write(path, url.encode("utf-8"))

    def _repair_stream_files(self) -> list[str]:
        errors = []
        for video_id, record in list(self.records.items()):
            folder = self.media / record["folder"]
            if not (folder / "Season 01").is_dir():
                continue
            replace = record.get("redirect_base_url") != self.public_url
            try:
                self._write_stream_files(video_id, folder, record["episodes"], replace)
                if replace:
                    updated = {**record, "redirect_base_url": self.public_url}
                    updated.pop("public_url", None)
                    self._save(video_id, updated)
            except (SourceError, OSError) as exc:
                message = f"黄果 ID {video_id} 的播放入口：{exc}"
                errors.append(message)
                LOG.warning("%s", message)
        return errors

    def _publish(self, video_id: str, record: dict, catalog_count: int | None):
        self._pause()
        detail = self.source.detail(video_id)
        title = clean_text(detail.get("title") or detail.get("name"))
        if not title:
            raise SourceError("短剧详情缺少标题")
        episodes = episode_titles(detail)
        finished = detail.get("is_finished") in (1, "1", "true")
        # Keep the first folder name so a title edit does not create a second Emby item.
        folder = self._folder(video_id, record, title)
        season = folder / "Season 01"
        if folder.is_symlink() or season.is_symlink():
            raise SourceError("媒体目录指向外部位置")
        poster = folder / "poster.jpg"
        cover = clean_text(detail.get("cover") or detail.get("image"))
        new_poster = None
        if not poster.is_file() or cover and cover != record.get("cover"):
            if not cover:
                raise SourceError("短剧详情缺少封面")
            self._pause()
            new_poster = self.source.poster(cover)
        season.mkdir(parents=True, exist_ok=True)
        if new_poster is not None:
            atomic_write(poster, new_poster)
        atomic_write(folder / "tvshow.nfo", tvshow_nfo(video_id, title, detail, finished))

        episode_numbers = sorted(set(episodes) | set(record.get("episodes", ())))
        prefix = folder.name.removesuffix(f" [HG-{video_id}]")
        for number, episode_title in sorted(episodes.items(), reverse=True):
            stem = f"{prefix} S01E{number:02d}"
            atomic_write(season / (stem + ".nfo"), episode_nfo(number, episode_title))
        self._write_stream_files(
            video_id, folder, episode_numbers, replace=record.get("redirect_base_url") != self.public_url,
        )
        self._save(video_id, {
            "folder": folder.name,
            "title": title,
            "cover": cover or record.get("cover", ""),
            "finished": finished,
            "episodes": episode_numbers,
            "catalog_episode_count": min(catalog_count, max(episodes)) if catalog_count is not None else None,
            "redirect_base_url": self.public_url,
            "checked_at": time.time(),
        })

    def _due(self, record: dict, catalog_count: int | None, now: float) -> bool:
        if not record:
            return True
        folder = self.media / record["folder"]
        if not ((folder / "poster.jpg").is_file() and (folder / "tvshow.nfo").is_file()
                and (folder / "Season 01").is_dir() and record["episodes"]):
            return True
        known_count = record.get("catalog_episode_count")
        if known_count is None:
            known_count = max(record["episodes"])
        if catalog_count is not None and catalog_count != known_count:
            return True
        age = now - record.get("checked_at", 0)
        if self.daily_sync and catalog_count is not None:
            # A changed catalog count already triggers an immediate detail refresh.
            return age >= 7 * 86400
        if record["finished"]:
            return age >= 24 * 3600
        return age >= 6 * 3600

    def _ready(self, ids: list[str]) -> int:
        return sum(
            bool(
                record and record["episodes"]
                and record.get("redirect_base_url") == self.public_url
                and (self.media / record["folder"] / "poster.jpg").is_file()
            )
            for video_id in ids
            if (record := self.records.get(video_id))
        )

    def sync(self) -> bool:
        self.update_status(phase="syncing", errors=[], processed=0, to_process=0, next_sync_bjt=None)
        errors = self._repair_stream_files()
        try:
            entries = self.source.catalog_entries(self.minimum, self._pause)
        except SourceError as exc:
            self.update_status(phase="degraded", errors=(errors + [str(exc)])[-8:])
            LOG.warning("同步失败：%s", exc)
            return False
        latest = list(entries)
        catalog_total = len(latest)
        target = self.minimum if self.minimum > 0 else catalog_total
        latest_set = set(latest)
        now = time.time()
        existing = [video_id for video_id in self.records if video_id not in latest_set]
        # 目录列表靠前的先处理，再扫历史目录里已没有的剧
        candidates = [
            video_id for video_id in latest + existing
            if self._due(self.records.get(video_id, {}), entries.get(video_id), now)
        ]
        self.update_status(
            latest_target=target, catalog_total=catalog_total, to_process=len(candidates),
            latest_ready=self._ready(latest), errors=errors[-8:],
        )
        for index, video_id in enumerate(candidates, start=1):
            try:
                self._publish(video_id, self.records.get(video_id, {}), entries.get(video_id))
            except (SourceError, OSError) as exc:
                message = f"黄果 ID {video_id}：{exc}"
                errors.append(message)
                LOG.warning("%s", message)
            self.update_status(processed=index, latest_ready=self._ready(latest), errors=errors[-8:])
        ready = self._ready(latest)
        complete = ready >= target and not errors
        self.update_status(
            phase="ready" if complete else "degraded",
            latest_ready=ready,
            last_sync_bjt=datetime.now(BEIJING_TIME).isoformat(timespec="seconds"),
        )
        LOG.info(
            "目录列表 %d 部就绪 %d/%d，库内共 %d 部，本轮处理 %d 部，失败 %d 部",
            catalog_total, ready, target, len(self.records), len(candidates), len(errors),
        )
        return complete
