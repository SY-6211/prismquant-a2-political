"""Preview one Lagarde speech after checking its official detail-page attribution."""

import json
import re
from datetime import date, datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid5
from xml.etree.ElementTree import ParseError

import httpx
from bs4 import BeautifulSoup
from defusedxml.common import DefusedXmlException
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.preview_ecb import MAX_BYTES, fetch_feed_items, load_source
from app.seed_people import NAMESPACE

PREVIEW_CHARACTERS = 1200
SPEAKER_PATTERN = re.compile(
    r"^(?:speech|statement|remarks|introductory statement|opening remarks|keynote speech|welcome address)"
    r"\s+by\s+Christine\s+Lagarde(?:\s*,|\s*$)", re.IGNORECASE,
)


def speech_url(value: str) -> str:
    parts = urlsplit(value)
    path = re.sub(r"/+", "/", parts.path)
    if (
        parts.scheme != "https" or parts.netloc.lower() != "www.ecb.europa.eu"
        or parts.query
        or not re.fullmatch(r"/press/key/date/\d{4}/html/ecb\.sp[\w.~\-]+\.en\.html", path)
    ):
        raise ValueError("Not an allowed official English speech URL")
    return urlunsplit(("https", "www.ecb.europa.eu", path, "", ""))


def select_candidate(items: list[dict]) -> dict | None:
    for item in items:  # newest first; scan the full feed, not just five items
        if not re.match(r"^Christine\s+Lagarde\s*:", item.get("title") or "", re.IGNORECASE):
            continue
        try:
            url = speech_url(item.get("url") or "")
        except ValueError:
            continue
        return {**item, "url": url}
    return None


def parse_speech(content: bytes, url: str, *, include_body: bool = False) -> dict:
    verified_url = speech_url(url)
    if len(content) > MAX_BYTES:
        raise ValueError("Speech response exceeds size limit")
    soup = BeautifulSoup(content, "html.parser")
    main = soup.find("main")
    title = main.find("h1") if main else None
    subtitle = main.select_one(".ecb-pressContentSubtitle") if main else None
    category = main.select_one(".title li") if main else None
    evidence = " ".join(subtitle.stripped_strings) if subtitle else None
    result = {
        "url": verified_url,
        "title": title.get_text(" ", strip=True) if title else None,
        "speaker": None, "person_id": None,
        "speaker_evidence": evidence,
        "page_date": None,
        "verification_status": "needs_review",
        "reason": "详情页未提供可确认的拉加德发言人说明或讲话分类。",
        "body_characters": 0, "body_preview": None,
    }
    if not (
        evidence and SPEAKER_PATTERN.match(evidence)
        and category and category.get_text(strip=True).upper() == "SPEECH"
        and result["title"]
    ):
        return result
    section = subtitle.find_parent(class_="section")
    if section is None:
        result["reason"] = "未找到独立正文区域，需人工检查页面结构。"
        return result
    date_meta = soup.select_one('meta[property="article:published_time"]')
    if date_meta:
        try:
            result["page_date"] = date.fromisoformat(date_meta.get("content", "")).isoformat()
        except ValueError:
            pass
    # Work only inside the speech section. Keep no navigation, consent or scripts.
    for node in section.select("script, style, nav, footer, aside, form, .ecb-pressContentSubtitle, .ecb-publicationDate, .content-box, .related-topics"):
        node.decompose()
    blocks = []
    block_names = {"h2", "h3", "p", "li"}
    for node in section.find_all(list(block_names)):
        if any(parent.name in block_names for parent in node.parents if parent is not section):
            continue
        value = node.get_text(" ", strip=True)
        if value:
            blocks.append(value)
    body = "\n\n".join(blocks)
    if len(body) < 80:
        result["reason"] = "提取的正文过短，需人工检查。"
        return result
    result.update({
        "speaker": "Christine Lagarde",
        "person_id": str(uuid5(NAMESPACE, "christine-lagarde")),
        "verification_status": "verified_page_attribution",
        "reason": "官方详情页讲话分类及发言人说明匹配；仅核对归属，不代表内容事实已独立核实。",
        "body_characters": len(body),
        "body_preview": body[:PREVIEW_CHARACTERS],
        "body_preview_truncated": len(body) > PREVIEW_CHARACTERS,
    })
    if include_body:
        result["body_text"] = body
    return result


def fetch_speech(client: httpx.Client, url: str, *, include_body: bool = False) -> dict:
    url = speech_url(url)  # also validates feed-supplied URLs before requesting
    with client.stream("GET", url) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Expected HTTP 200")
        content = bytearray()
        for chunk in response.iter_bytes(chunk_size=65536):
            content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise ValueError("Speech response exceeds size limit")
    return parse_speech(bytes(content), url, include_body=include_body)


def main() -> int:
    try:
        source = load_source(Settings())
        with httpx.Client(
            timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=False,
            headers={"User-Agent": "PrismQuant-FYP/0.1 (manual speech preview)"},
        ) as client:
            candidate = select_candidate(fetch_feed_items(client))
            if candidate is None:
                print("当前订阅未找到合适的拉加德讲话候选；未获取详情或保存内容。")
                return 2
            detail = fetch_speech(client, candidate["url"])
    except httpx.TimeoutException:
        print("官方来源请求超时，预览未完成；未保存内容。")
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"官方来源返回 HTTP {exc.response.status_code}，预览未完成；未保存内容。")
        return 1
    except httpx.RequestError:
        print("无法连接官方来源，请检查网络或代理；未保存内容。")
        return 1
    except (ValueError, ParseError, DefusedXmlException):
        print("来源配置、RSS 或详情地址不符合预览要求；未保存内容。")
        return 1
    except SQLAlchemyError:
        print("无法读取本地来源配置，请检查数据库；未显示连接凭证。")
        return 1
    print(json.dumps({
        "status": "ok" if detail["verification_status"] == "verified_page_attribution" else "needs_review",
        "mode": "manual_speech_preview", "source_enabled": source["enabled"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "rss_title": candidate["title"], "rss_published_at": candidate["published_at"],
        "detail": detail,
    }, ensure_ascii=False, indent=2))
    return 0 if detail["verification_status"] == "verified_page_attribution" else 2


if __name__ == "__main__":
    raise SystemExit(main())
