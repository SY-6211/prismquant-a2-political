"""Extract one pinned archive post fully in memory. This command never writes data."""

import hashlib
import json
from datetime import datetime, timezone
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml.common import DefusedXmlException
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.preview_trump import PERSON_ID, SOURCE_ID, fetch_feed_items, load_source
from app.preview_trump_post import archive_url, fetch_post

TARGET_URL = "https://www.trumpstruth.org/statuses/42396"
PARSER_VERSION = "trump_archive_v1"


def find_target(items: list[dict]) -> dict | None:
    for item in items:
        try:
            if archive_url(item.get("url") or "") == TARGET_URL:
                return {**item, "url": TARGET_URL}
        except ValueError:
            continue
    return None


def inline_quotes(items: list[dict]) -> list[dict]:
    return [{"position": number, "text": item["text"], "attribution": None}
            for number, item in enumerate(items)]


def prepare_extraction(detail: dict, candidate: dict) -> dict:
    """Map the full extractor's result to data for a future writer, not a preview."""
    if detail.get("extraction_mode") != "full":
        raise ValueError("A bounded preview cannot be used for full extraction")
    result = {
        "extraction_complete": False, "ingestion_eligible": False,
        "ingestion_reason": detail["reason"], "record": None, "archive_post": None,
    }
    if not detail.get("extraction_complete") or detail["verification_status"] != "archive_only":
        return result
    # Reject omitted sections even if an upstream caller accidentally marks it complete.
    posts = [detail["post"], *detail["quoted_posts"]]
    if (detail["quoted_posts_omitted"] or detail["archive_media_notes_omitted"]
            or any(post["inline_quotes_omitted"] for post in posts)
            or any("text" not in post["post_text"] for post in posts)
            or any("text" not in item for post in posts for item in post["inline_quotes"])
            or any("text" not in item for item in detail["archive_media_notes"])):
        raise ValueError("Full extraction contains omitted or bounded content")
    if archive_url(detail["url"]) != archive_url(candidate["url"]):
        raise ValueError("Extraction and RSS addresses must match")
    post = detail["post"]
    body = post["post_text"]["text"]
    record = {
        "source_id": str(SOURCE_ID), "person_id": str(PERSON_ID), "title": candidate.get("title"),
        "content_type": "public_statement", "provenance_type": "third_party_archive",
        "verification_status": "archive_only", "original_url": post["original_url"],
        "retrieved_url": detail["url"], "published_at": candidate.get("published_at"),
        "published_raw": candidate.get("published_raw"), "page_date": None, "language": None,
        "body_text": body, "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest() if body else None,
        "speaker_evidence": (
            f"第三方归档标注作者 {post['archive_author']}、账号 {post['archive_handle']}；"
            f"归档页面 {detail['url']} 与 {'归档历史列表' if candidate.get('metadata_origin') == 'archive_listing' else 'RSS'} 标注匹配。未访问或验证原帖。"
        ),
    }
    quotes = []
    for number, quoted in enumerate(detail["quoted_posts"]):
        quotes.append({
            "position": number, "archive_url": quoted["archive_url"],
            "parent_archive_url": quoted["parent_archive_url"], "original_url": quoted["original_url"],
            "archive_author": quoted["archive_author"], "archive_handle": quoted["archive_handle"],
            "published_at": quoted["page_post_published_at"], "published_raw": quoted["page_post_published_raw"],
            "text": quoted["post_text"]["text"], "image_count": quoted["image_count"],
            "video_count": quoted["video_count"], "inline_quotes": inline_quotes(quoted["inline_quotes"]),
        })
    notes = []
    for number, note in enumerate(detail["archive_media_notes"]):
        notes.append({"position": number, **{
            key: note[key] for key in ("kind", "label", "text", "attachment_ref", "owner_archive_url", "association_status")
        }})
    archive = {
        "archive_author": post["archive_author"], "archive_handle": post["archive_handle"],
        "archive_post_type": candidate.get("archive_post_type"),
        "archive_removed_raw": candidate.get("archive_removed_raw"),
        "title_origin": "archive_listing" if candidate.get("metadata_origin") == "archive_listing" else "archive_rss",
        "page_post_published_at": post["page_post_published_at"], "page_post_published_raw": post["page_post_published_raw"],
        "archive_captured_at": detail["page_capture_at"], "archive_capture_raw": detail["page_capture_raw"],
        "image_count": post["image_count"], "video_count": post["video_count"],
        "inline_quotes": inline_quotes(post["inline_quotes"]), "quoted_posts": quotes,
        "archive_media_notes": notes, "parser_version": PARSER_VERSION,
    }
    # Complete extraction can succeed for a media/quote post while the first writer
    # deliberately does not support saving it. Never fill its body from other sections.
    eligible = bool(body and body.strip() and record["title"] and record["title"].strip()
                    and archive["archive_post_type"] == "original" and not archive["quoted_posts"]
                    and not archive["inline_quotes"])
    result.update(
        extraction_complete=True, ingestion_eligible=eligible, record=record, archive_post=archive,
        ingestion_reason="完整提取完成，符合首版有文字原创帖的入库范围；尚未保存。" if eligible else
        "完整提取完成，但缺少主帖文字、标题或包含引用/转发，不符合首版入库范围；未保存。",
    )
    return result


def main() -> int:
    try:
        source = load_source(Settings())
        with httpx.Client(
            timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=False,
            headers={"User-Agent": "PrismQuant-FYP/0.1 (manual full archive extraction)"},
        ) as client:
            candidate = find_target(fetch_feed_items(client))
            if candidate is None:
                print("当前 RSS 未找到已确认的目标帖子，已停止；未改选其他帖子或保存内容。")
                return 2
            detail = fetch_post(client, candidate, include_full=True)
        result = prepare_extraction(detail, candidate)
    except httpx.TimeoutException:
        print("第三方归档请求超时，完整提取未完成；未保存内容。")
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"第三方归档返回 HTTP {exc.response.status_code}，完整提取未完成；未保存内容。")
        return 1
    except httpx.RequestError:
        print("无法连接第三方归档站，请检查网络；未保存内容。")
        return 1
    except (ValueError, ParseError, DefusedXmlException):
        print("来源、详情结构或完整提取限制不符合要求，未返回可入库结果；未保存内容。")
        return 1
    except SQLAlchemyError:
        print("无法读取本地来源配置，请检查数据库；未显示连接凭证。")
        return 1
    success = result["extraction_complete"] and result["ingestion_eligible"]
    print(json.dumps({
        "status": "ok" if success else "needs_review", "mode": "manual_full_archive_extraction",
        "source_enabled": source["enabled"], "monitoring_enabled": source["monitoring_enabled"],
        "link_enabled": source["link_enabled"], "fetched_at": datetime.now(timezone.utc).isoformat(),
        "saved": False, **result,
    }, ensure_ascii=False, indent=2))
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
