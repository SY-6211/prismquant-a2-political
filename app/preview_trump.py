"""One manual third-party archive RSS preview. No original-site requests or writes."""

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid5
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.database import create_read_engine
from app.preview_ecb import MAX_BYTES, MAX_ITEMS, published_utc
from app.seed_people import NAMESPACE

TRUMP_FEED_URL = "https://www.trumpstruth.org/feed"
SOURCE_ID = uuid5(NAMESPACE, "trumps-truth-rss")
PERSON_ID = uuid5(NAMESPACE, "donald-trump")
DC = "{http://purl.org/dc/elements/1.1/}"
TRUTH = "{https://truthsocial.com/ns}"
ARCHIVE = "{https://www.trumpstruth.org/rss-extensions}"


def validate_source(row) -> dict:
    if row is None or (
        row["feed_url"] != TRUMP_FEED_URL or row["provenance_type"] != "third_party_archive"
        or row["fetch_method"] != "rss" or row["person_id"] != PERSON_ID
    ):
        raise ValueError("Expected configured third-party archive and Trump association")
    return dict(row)


def load_source(settings: Settings) -> dict:
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT s.source_id, s.name, s.publisher_name, s.feed_url, s.provenance_type, "
                "s.fetch_method, s.enabled, p.person_id, p.monitoring_enabled, ps.enabled AS link_enabled "
                "FROM sources s JOIN person_sources ps ON ps.source_id=s.source_id "
                "JOIN people p ON p.person_id=ps.person_id "
                "WHERE s.source_id=:source AND p.person_id=:person"
            ), {"source": SOURCE_ID, "person": PERSON_ID}).mappings().one_or_none()
            return validate_source(row)
    finally:
        engine.dispose()


def preview_link(value: str | None, *, original: bool = False) -> str | None:
    """Display links only; no link in the feed is followed by this command."""
    if not value or re.search(r"\s", value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    hosts = {"truthsocial.com", "www.truthsocial.com"} if original else {"www.trumpstruth.org"}
    if parts.scheme != "https" or parts.netloc.lower() not in hosts or parts.query:
        return None
    if not original and not re.fullmatch(r"/statuses/\d+", parts.path):
        return None
    return urlunsplit(("https", parts.netloc.lower(), parts.path, "", ""))


def parse_feed_items(content: bytes) -> list[dict]:
    if len(content) > MAX_BYTES:
        raise ValueError("Archive response exceeds preview size limit")
    root = fromstring(content, forbid_dtd=True)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Expected RSS channel")
    items = []
    for entry in root.findall("./channel/item"):
        raw = entry.findtext("pubDate") or entry.findtext(DC + "date")
        items.append({
            "title": (entry.findtext("title") or "").strip() or None,
            "published_at": published_utc(raw), "published_raw": raw,
            "url": preview_link(entry.findtext("link")),
            "original_url": preview_link(entry.findtext(TRUTH + "originalUrl"), original=True),
            "archive_author": entry.findtext(DC + "creator"),
            "archive_post_type": entry.findtext(ARCHIVE + "postType"),
            "archive_removed_raw": entry.findtext(ARCHIVE + "removed"),
            "verification_status": "archive_only",
        })
    items.sort(
        key=lambda item: datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
        if item["published_at"] else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return items


def parse_preview(content: bytes) -> list[dict]:
    return parse_feed_items(content)[:MAX_ITEMS]


def fetch_feed_items(client: httpx.Client) -> list[dict]:
    with client.stream("GET", TRUMP_FEED_URL) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Expected HTTP 200")
        content = bytearray()
        for chunk in response.iter_bytes(chunk_size=65536):
            content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise ValueError("Archive response exceeds preview size limit")
    return parse_feed_items(bytes(content))


def fetch_preview(client: httpx.Client) -> list[dict]:
    return fetch_feed_items(client)[:MAX_ITEMS]


def main() -> int:
    try:
        source = load_source(Settings())
        with httpx.Client(
            timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=False,
            headers={"User-Agent": "PrismQuant-FYP/0.1 (manual archive RSS preview)",
                     "Accept": "application/rss+xml, application/xml, text/xml"},
        ) as client:
            items = fetch_preview(client)
    except httpx.TimeoutException:
        print("第三方归档 RSS 请求超时，预览未完成；未保存内容。")
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"第三方归档 RSS 返回 HTTP {exc.response.status_code}，预览未完成；未保存内容。")
        return 1
    except httpx.RequestError:
        print("无法连接第三方归档站，请检查网络；未保存内容。")
        return 1
    except (ValueError, ParseError, DefusedXmlException):
        print("来源配置或归档 RSS 格式不符合预览要求；未保存内容。")
        return 1
    except SQLAlchemyError:
        print("无法读取本地来源配置，请检查数据库；未显示连接凭证。")
        return 1
    print(json.dumps({
        "status": "ok", "mode": "manual_archive_preview",
        "source_id": str(SOURCE_ID), "source_name": source["name"],
        "publisher_name": source["publisher_name"], "provenance_type": "third_party_archive",
        "source_enabled": source["enabled"], "monitoring_enabled": source["monitoring_enabled"],
        "link_enabled": source["link_enabled"], "configured_person_id": str(PERSON_ID),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "attribution": "第三方归档 RSS 预览，非特朗普或 Truth Social 官方接口；作者、帖子类型和删除标记均为归档站提供。未访问原帖或核对作者、全文和转发关系；标题可能由归档站生成。",
        "items": items,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
