"""Preview one archive detail page, keeping post text, quotes and media notes separate."""

import json
import re
from datetime import datetime, timezone
from xml.etree.ElementTree import ParseError

import httpx
from bs4 import BeautifulSoup, Comment
from defusedxml.common import DefusedXmlException
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.preview_ecb import MAX_BYTES, published_utc
from app.preview_trump import PERSON_ID, fetch_preview as fetch_rss_preview, load_source, preview_link

PREVIEW_CHARACTERS = 1200
MAX_QUOTED_POSTS = 5
MAX_MEDIA_NOTES = 5
MAX_FULL_PARTS = 100


def archive_url(value: str) -> str:
    url = preview_link(value)
    if url is None:
        raise ValueError("Expected allowed archive status URL")
    return url


def select_candidate(items: list[dict]) -> dict | None:
    for item in items:  # The RSS preview already returns its newest five entries.
        try:
            return {**item, "url": archive_url(item.get("url") or "")}
        except ValueError:
            continue
    return None


def plain_text(node) -> str | None:
    if node is None:
        return None
    clone = BeautifulSoup(str(node), "html.parser")
    for unwanted in clone.select("script, style, nav, footer, form, aside"):
        unwanted.decompose()
    return clone.get_text(" ", strip=True) or None


def bounded(value: str | None) -> dict:
    return {
        "text_preview": value[:PREVIEW_CHARACTERS] if value else None,
        "text_characters": len(value) if value else 0,
        "text_preview_truncated": bool(value and len(value) > PREVIEW_CHARACTERS),
    }


def full_text(node) -> str | None:
    """Normalize HTML whitespace while retaining paragraphs and explicit breaks."""
    if node is None:
        return None
    clone = BeautifulSoup(str(node), "html.parser")
    for unwanted in clone.select("script, style, nav, footer, form, aside"):
        unwanted.decompose()
    for value in list(clone.find_all(string=True)):
        if isinstance(value, Comment):
            value.extract()
        else:
            value.replace_with(re.sub(r"\s+", " ", str(value)))
    for br in clone.find_all("br"):
        br.replace_with("\n")
    for block in clone.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre"]):
        block.insert_before("\n\n")
        block.insert_after("\n\n")
    value = "\n".join(line.strip() for line in clone.get_text().splitlines()).strip()
    return re.sub(r"\n{3,}", "\n\n", value) or None


def text_result(node, *, include_full: bool) -> dict:
    if include_full:
        value = full_text(node)
        return {"text": value, "text_characters": len(value) if value else 0}
    return bounded(plain_text(node))


def own_content(status, *, include_full: bool = False) -> dict:
    # Read only this status's own header/body, never a nested quoted status.
    header = status.find(class_="status__header", recursive=False)
    body = status.find(class_="status__body", recursive=False)
    author = header.select_one(".status-info__account-name") if header else None
    meta = header.select_one(".status-info__meta") if header else None
    handle = None
    self_url = None
    time_node = meta.find("time") if meta else None
    if meta:
        for link in meta.find_all("a"):
            label = plain_text(link) or ""
            if label.startswith("@"):
                handle = label
            candidate = preview_link(link.get("href"))
            if candidate:
                self_url = candidate
    original = header.select_one("a.status__external-link") if header else None
    original_url = preview_link(original.get("href"), original=True) if original else None
    content = body.find(class_="status__content", recursive=False) if body else None
    text = {"text": None, "text_characters": 0} if include_full else bounded(None)
    inline_quotes = []
    if content:
        clone = BeautifulSoup(str(content), "html.parser")
        for unwanted in clone.select(".status, .status__attachments"):
            unwanted.decompose()
        # Unattributed HTML quotes are not merged into the author's own text.
        quotes = clone.find_all("blockquote")
        if include_full and len(quotes) > MAX_FULL_PARTS:
            raise ValueError("Too many inline quotes for complete extraction")
        for quote in quotes:
            if quote.find_parent("blockquote") is None:
                inline_quotes.append(text_result(quote, include_full=include_full))
        for quote in clone.find_all("blockquote"):
            quote.decompose()
        text = text_result(clone, include_full=include_full)
    attachments = body.find(class_="status__attachments", recursive=False) if body else None
    image_count = len(attachments.select(".status-attachment--image")) if attachments else 0
    video_count = len(attachments.select(".status-attachment--video")) if attachments else 0
    raw_time = time_node.get("datetime") if time_node else None
    result = {
        "archive_url": self_url, "archive_author": plain_text(author), "archive_handle": handle,
        "original_url": original_url, "page_post_published_at": published_utc(raw_time),
        "page_post_published_raw": raw_time,
        "post_text": text,
        "inline_quotes": inline_quotes if include_full else inline_quotes[:MAX_QUOTED_POSTS],
        "inline_quotes_omitted": 0 if include_full else max(0, len(inline_quotes) - MAX_QUOTED_POSTS),
        "image_count": image_count, "video_count": video_count,
        "body_region_present": body is not None,
    }
    if include_full:
        unrecognized = BeautifulSoup(str(body), "html.parser") if body else None
        if unrecognized:
            for recognized in unrecognized.select(".status__content, .status__attachments, .status, .status__footer"):
                recognized.decompose()
        result["unrecognized_body_text"] = bool(full_text(unrecognized))
    return result


def parse_post(content: bytes, candidate: dict, *, include_full: bool = False) -> dict:
    url = archive_url(candidate["url"])
    if len(content) > MAX_BYTES:
        raise ValueError("Archive detail exceeds size limit")
    soup = BeautifulSoup(content, "html.parser")
    containers = soup.select(".main-body__content")
    container = containers[0] if len(containers) == 1 else None
    roots = [node for node in container.select(".status") if node.find_parent(class_="status") is None] if container else []
    result = {
        "url": url, "verification_status": "needs_review",
        "reason": "归档详情页结构缺失或存在多个主帖，需人工核对。",
        "post": None, "quoted_posts": [], "quoted_posts_omitted": 0,
        "archive_media_notes": [], "archive_media_notes_omitted": 0,
        "page_capture_at": None, "page_capture_raw": None,
    }
    if include_full:
        result.update(extraction_mode="full", extraction_complete=False)
    if len(roots) != 1:
        return result
    root = roots[0]
    post = own_content(root, include_full=include_full)
    result["post"] = post
    nested = root.select(".status")
    if include_full and len(nested) > MAX_FULL_PARTS:
        raise ValueError("Too many quoted posts for complete extraction")
    known = {id(root): post}
    for quoted in nested if include_full else nested[:MAX_QUOTED_POSTS]:
        record = own_content(quoted, include_full=include_full)
        record["parent_archive_url"] = known.get(id(quoted.find_parent(class_="status")), {}).get("archive_url")
        known[id(quoted)] = record
        result["quoted_posts"].append(record)
    result["quoted_posts_omitted"] = 0 if include_full else max(0, len(nested) - MAX_QUOTED_POSTS)
    details = container.select_one(".status-details")
    full_issues = []
    notes = details.select(".status-details-attachment__body") if details else []
    if include_full and len(notes) > MAX_FULL_PARTS:
        raise ValueError("Too many media notes for complete extraction")
    for note in notes if include_full else notes[:MAX_MEDIA_NOTES]:
        label = plain_text(note.find(["h4", "h5", "h6"])) or ""
        label_lower = label.casefold()
        kind = (
            "archive_image_description" if "image description" in label_lower else
            "archive_transcript" if "transcript" in label_lower else
            "unavailable" if "no file information" in label_lower else "other_archive_note"
        )
        note_node = None if kind == "unavailable" else note.select_one(".status-details-attachment__text")
        item = {
            "kind": kind, "label": label or None, **text_result(note_node, include_full=include_full),
            "attribution": "归档站提供的附件说明或转写，未核对媒体或发言人；不属于主帖文字。",
        }
        if include_full:
            parent = note.find_parent(class_="status-details-attachment")
            item.update(attachment_ref=parent.get("id") if parent else None,
                        owner_archive_url=None, association_status="unresolved")
            if kind == "other_archive_note" or (kind != "unavailable" and not item["text"]):
                full_issues.append("附件说明类型未知或对应文字区域缺失")
        result["archive_media_notes"].append(item)
    result["archive_media_notes_omitted"] = 0 if include_full else max(0, len(notes) - MAX_MEDIA_NOTES)
    if details:
        for row in details.select(".status-details-table tr"):
            key = plain_text(row.select_one(".status-details-table__key"))
            time_node = row.find("time")
            if key == "Capture Date" and time_node:
                raw = time_node.get("datetime")
                result.update(page_capture_raw=raw, page_capture_at=published_utc(raw))
    expected_original = preview_link(candidate.get("original_url"), original=True)
    reasons = full_issues
    if include_full and any(item["unrecognized_body_text"] for item in [post, *result["quoted_posts"]]):
        reasons.append("帖子正文包含未识别的文字区域")
    if post["archive_url"] != url:
        reasons.append("详情页主帖地址与 RSS 不一致")
    if (post["archive_author"] != "Donald J. Trump" or post["archive_handle"] != "@realDonaldTrump"
            or candidate.get("archive_author") != "Donald J. Trump"):
        reasons.append("归档页面或 RSS 作者标注不匹配")
    if (not expected_original or post["original_url"] != expected_original or not re.fullmatch(
        r"https://(?:www\.)?truthsocial\.com/@realDonaldTrump/(?:posts/)?\d+", expected_original,
    )):
        reasons.append("主帖原始地址缺失或不匹配")
    if (candidate.get("published_at") and post["page_post_published_at"]
            and candidate["published_at"] != post["page_post_published_at"]):
        reasons.append("RSS 与页面主帖发布时间不一致")
    post_type = candidate.get("archive_post_type")
    if post_type not in ("original", "quote", "retruth"):
        reasons.append("归档帖子类型未知")
    if post_type in ("quote", "retruth") and not nested:
        reasons.append("未找到被引用或转发的帖子区域")
    if post_type == "original" and nested:
        reasons.append("原帖类型与嵌套引用结构不一致")
    if not post["body_region_present"] or not (
        post["post_text"]["text_characters"] or post["image_count"] or post["video_count"] or nested or post["inline_quotes"]
    ):
        reasons.append("未找到主帖文字、附件或引用内容")
    if reasons:
        result["reason"] = "；".join(reasons) + "，需人工核对。"
    else:
        result.update(
            verification_status="archive_only",
            reason=("归档页面与历史列表标注匹配；仅依据第三方归档，未访问原帖或独立核实内容。"
                    if candidate.get("metadata_origin") == "archive_listing" else
                    "归档页面与 RSS 标注匹配；仅预览第三方归档，未访问原帖或独立核实内容。"),
        )
        if include_full:
            result["extraction_complete"] = True
    return result


def fetch_post(client: httpx.Client, candidate: dict, *, include_full: bool = False) -> dict:
    url = archive_url(candidate["url"])
    with client.stream("GET", url) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Expected HTTP 200")
        content = bytearray()
        for chunk in response.iter_bytes(chunk_size=65536):
            content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise ValueError("Archive detail exceeds size limit")
    return parse_post(bytes(content), {**candidate, "url": url}, include_full=include_full)


def main() -> int:
    try:
        source = load_source(Settings())
        with httpx.Client(
            timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=False,
            headers={"User-Agent": "PrismQuant-FYP/0.1 (manual archive post preview)"},
        ) as client:
            candidate = select_candidate(fetch_rss_preview(client))
            if candidate is None:
                print("当前订阅前五条未找到允许的归档详情地址；未保存内容。")
                return 2
            detail = fetch_post(client, candidate)
    except httpx.TimeoutException:
        print("第三方归档请求超时，详情预览未完成；未保存内容。")
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"第三方归档返回 HTTP {exc.response.status_code}，详情预览未完成；未保存内容。")
        return 1
    except httpx.RequestError:
        print("无法连接第三方归档站，请检查网络；未保存内容。")
        return 1
    except (ValueError, ParseError, DefusedXmlException):
        print("来源配置、RSS 或归档详情格式不符合要求；未保存内容。")
        return 1
    except SQLAlchemyError:
        print("无法读取本地来源配置，请检查数据库；未显示连接凭证。")
        return 1
    success = detail["verification_status"] == "archive_only"
    print(json.dumps({
        "status": "ok" if success else "needs_review", "mode": "manual_archive_post_preview",
        "source_name": source["name"], "provenance_type": "third_party_archive",
        "source_enabled": source["enabled"], "monitoring_enabled": source["monitoring_enabled"],
        "link_enabled": source["link_enabled"], "configured_person_id": str(PERSON_ID),
        "fetched_at": datetime.now(timezone.utc).isoformat(), "rss_item": candidate,
        "attribution": "主帖文字、引用帖子和归档附件说明分别展示；归档标注的作者不等于原帖验证。图片说明、视频转写和被引用内容不能合并为特朗普本人发言。",
        "detail": detail,
    }, ensure_ascii=False, indent=2))
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
