"""One explicit ECB RSS preview; no persistence, scheduling or person attribution."""

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from uuid import uuid5
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.database import create_read_engine
from app.seed_people import NAMESPACE

ECB_FEED_URL = "https://www.ecb.europa.eu/rss/press.html"
MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 5


def published_utc(value: str | None) -> str | None:
    if not value:
        return None
    try:
        stamp = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    # Do not invent a timezone or substitute the acquisition time.
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_feed_items(content: bytes) -> list[dict]:
    if len(content) > MAX_BYTES:
        raise ValueError("RSS response exceeds preview size limit")
    root = fromstring(content, forbid_dtd=True)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Expected an RSS channel")
    items = []
    for entry in root.findall("./channel/item"):
        title = (entry.findtext("title") or "").strip()
        link = (entry.findtext("link") or "").strip()
        parsed_link = urlparse(link)
        if parsed_link.scheme not in ("http", "https") or not parsed_link.hostname:
            link = None
        published_raw = entry.findtext("pubDate") or entry.findtext("{http://purl.org/dc/elements/1.1/}date")
        items.append({
            "title": title or None,
            "published_at": published_utc(published_raw),
            "published_raw": published_raw,
            "url": link,
        })
    # Order by known publication dates, keeping undated entries at the end.
    items.sort(
        key=lambda item: datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
        if item["published_at"] else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return items


def parse_preview(content: bytes) -> list[dict]:
    return parse_feed_items(content)[:MAX_ITEMS]


def load_source(settings: Settings) -> dict:
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT name, feed_url, enabled FROM sources WHERE source_id=:id"
            ), {"id": uuid5(NAMESPACE, "ecb-press-rss")}).mappings().one_or_none()
    finally:
        engine.dispose()
    if row is None or row["feed_url"] != ECB_FEED_URL:
        raise ValueError("ECB source missing or URL differs from the confirmed feed")
    return dict(row)


def fetch_feed_items(client: httpx.Client) -> list[dict]:
    # Fixed official endpoint only; do not follow redirects or fetch item pages.
    with client.stream("GET", ECB_FEED_URL) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Expected an HTTP 200 RSS response")
        content = bytearray()
        for chunk in response.iter_bytes(chunk_size=65536):
            content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise ValueError("RSS response exceeds preview size limit")
    return parse_feed_items(bytes(content))


def fetch_preview(client: httpx.Client) -> list[dict]:
    return fetch_feed_items(client)[:MAX_ITEMS]


def main() -> int:
    try:
        source = load_source(Settings())
        with httpx.Client(
            timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=False,
            headers={"User-Agent": "PrismQuant-FYP/0.1 (manual RSS preview)", "Accept": "application/rss+xml, application/xml, text/xml"},
        ) as client:
            items = fetch_preview(client)
    except httpx.TimeoutException:
        print("欧洲央行 RSS 请求超时；预览未完成，没有保存内容。")
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"欧洲央行 RSS 返回 HTTP {exc.response.status_code}；预览未完成，没有保存内容。")
        return 1
    except httpx.RequestError:
        print("无法连接欧洲央行 RSS，请检查网络或代理；没有保存内容。")
        return 1
    except (ParseError, DefusedXmlException, ValueError):
        print("来源配置或 RSS 格式不符合预览要求；没有保存内容。")
        return 1
    except SQLAlchemyError:
        print("无法读取本地来源配置，请检查数据库；未显示连接凭证。")
        return 1
    print(json.dumps({
        "status": "ok", "mode": "manual_preview", "source_name": source["name"],
        "source_enabled": source["enabled"], "fetched_at": datetime.now(timezone.utc).isoformat(),
        "attribution": "整个欧洲央行订阅的预览，尚未按人物筛选；不能直接归为拉加德发言。",
        "items": items,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
