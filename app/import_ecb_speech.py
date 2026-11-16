"""Explicitly save the one previously previewed official speech; no scheduling."""

import hashlib
import json
from datetime import date, datetime, timezone
from uuid import UUID, uuid5

import httpx
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.database import database_url
from app.preview_ecb import ECB_FEED_URL, fetch_feed_items, load_source, published_utc
from app.preview_ecb_speech import fetch_speech, speech_url
from app.seed_people import NAMESPACE

SOURCE_ID = uuid5(NAMESPACE, "ecb-press-rss")
PERSON_ID = uuid5(NAMESPACE, "christine-lagarde")
# Pin this first import to the page already previewed, rather than a changing newest item.
TARGET_URL = "https://www.ecb.europa.eu/press/key/date/2026/html/ecb.sp261017~7015ea3099.en.html"


def find_feed_entry(items: list[dict], url: str) -> dict | None:
    target = speech_url(url)
    for item in items:
        try:
            if speech_url(item.get("url") or "") == target:
                return item
        except ValueError:
            continue
    return None


def _save_speech_record(
    connection: Connection, detail: dict, feed_entry: dict | None, first_seen_at: datetime,
    *, source_id: UUID = SOURCE_ID, person_id: UUID = PERSON_ID,
) -> dict:
    """Caller owns the transaction. Insert once; preserve existing content on conflict."""
    body = detail.get("body_text")
    evidence = detail.get("speaker_evidence")
    if (
        detail.get("verification_status") != "verified_page_attribution"
        or detail.get("person_id") != str(person_id)
        or not isinstance(body, str) or len(body.strip()) < 80
        or detail.get("body_characters") != len(body)
        or not isinstance(evidence, str) or not evidence.strip()
        or not detail.get("title") or not detail["title"].strip()
        or first_seen_at.utcoffset() is None
    ):
        raise ValueError("Full body and confirmed page attribution are required")
    url = speech_url(detail["url"])
    if feed_entry is not None and find_feed_entry([feed_entry], url) is None:
        raise ValueError("Feed metadata must belong to the same speech")
    # Hold settings and association stable until the caller commits. Manual import
    # deliberately works with disabled switches, but never changes those switches.
    source = connection.execute(text(
        "SELECT s.provenance_type, s.feed_url, s.fetch_method "
        "FROM sources s JOIN person_sources ps ON ps.source_id=s.source_id "
        "JOIN people p ON p.person_id=ps.person_id "
        "WHERE s.source_id=:source_id AND p.person_id=:person_id FOR SHARE OF s, ps, p"
    ), {"source_id": source_id, "person_id": person_id}).mappings().one_or_none()
    if source is None or (
        source["provenance_type"] != "official" or source["feed_url"] != ECB_FEED_URL
        or source["fetch_method"] != "rss"
    ):
        raise ValueError("Official source and person association must be checked")
    raw_time = feed_entry.get("published_raw") if feed_entry else None
    stamp = published_utc(raw_time)
    values = {
        "source_id": source_id, "person_id": person_id, "title": detail["title"],
        "provenance_type": source["provenance_type"], "url": url,
        "published_at": datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None,
        "page_date": date.fromisoformat(detail["page_date"]) if detail.get("page_date") else None,
        "published_raw": raw_time, "first_seen_at": first_seen_at.astimezone(timezone.utc),
        "body_text": body, "speaker_evidence": evidence,
        "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    inserted = connection.execute(text(
        "INSERT INTO public_records (source_id, person_id, title, content_type, provenance_type, "
        "original_url, retrieved_url, published_at, page_date, published_raw, first_seen_at, "
        "language, body_text, speaker_evidence, verification_status, content_hash) "
        "VALUES (:source_id, :person_id, :title, 'public_statement', :provenance_type, "
        ":url, :url, :published_at, :page_date, :published_raw, :first_seen_at, "
        "'en', :body_text, :speaker_evidence, 'verified_page_attribution', :content_hash) "
        "ON CONFLICT (source_id, retrieved_url) DO NOTHING RETURNING record_id"
    ), values).scalar_one_or_none()
    if inserted is not None:
        action = "inserted"
        record_id = inserted
    else:
        existing = connection.execute(text(
            "SELECT record_id, content_hash, body_text, person_id, content_type, provenance_type, "
            "verification_status, speaker_evidence, title, original_url, published_at, "
            "published_raw, page_date, language FROM public_records "
            "WHERE source_id=:source_id AND retrieved_url=:url FOR UPDATE"
        ), values).mappings().one()
        record_id = existing["record_id"]
        unchanged = (
            existing["content_hash"] == values["content_hash"] and existing["body_text"] == body
            and existing["person_id"] == person_id and existing["content_type"] == "public_statement"
            and existing["provenance_type"] == "official"
            and existing["verification_status"] == "verified_page_attribution"
            and existing["speaker_evidence"] == evidence
            and existing["title"] == values["title"] and existing["original_url"] == url
            and existing["published_at"] == values["published_at"]
            and existing["published_raw"] == values["published_raw"]
            and existing["page_date"] == values["page_date"] and existing["language"] == "en"
        )
        action = "already_exists" if unchanged else "needs_review"
    return {
        "status": "needs_review" if action == "needs_review" else "ok",
        "action": action, "record_id": str(record_id), "title": detail["title"],
        "retrieved_url": url, "body_characters": len(body),
        "message": "已保存全文。" if action == "inserted" else (
            "相同内容已存在，未重复添加或修改。" if action == "already_exists" else
            "已有记录与当前正文或归属信息不同，需人工核对；原记录未覆盖。"
        ),
    }


def save_speech(connection: Connection, detail: dict, feed_entry: dict | None, first_seen_at: datetime,
                *, source_id: UUID = SOURCE_ID, person_id: UUID = PERSON_ID,
                discovery_transport='rss', observation_metadata=None, observed_at=None) -> dict:
    from app.event_model import snapshot_from_record
    from app.repositories.events import save_snapshot

    if discovery_transport not in ('rss','web_index'):
        raise ValueError('Unsupported official discovery channel')
    # The legacy row and event history either both commit or both roll back.
    with connection.begin_nested():
        result = _save_speech_record(connection, detail, feed_entry, first_seen_at,
                                    source_id=source_id, person_id=person_id)
        raw = feed_entry.get('published_raw') if feed_entry else None
        snapshot = snapshot_from_record({
            'title': detail['title'], 'body_text': detail['body_text'], 'content_type': 'public_statement',
            'original_url': speech_url(detail['url']), 'retrieved_url': speech_url(detail['url']),
            'published_at': published_utc(raw), 'published_raw': raw, 'page_date': detail.get('page_date'),
            'language': 'en', 'person_id': person_id, 'speaker_evidence': detail['speaker_evidence'],
            'provenance_type': 'official', 'verification_status': 'verified_page_attribution',
        }, review_status='needs_review' if result['action'] == 'needs_review' else 'accepted')
        event = save_snapshot(connection, source_id, discovery_transport, snapshot, observed_at or first_seen_at,
                              record_id=UUID(result['record_id']), link_record=result['action'] != 'needs_review',
                              metadata=observation_metadata)
    return {**result, **event}


def main() -> int:
    engine = None
    try:
        settings = Settings()
        source = load_source(settings)
        with httpx.Client(
            timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=False,
            headers={"User-Agent": "PrismQuant-FYP/0.1 (manual single speech import)"},
        ) as client:
            entry = find_feed_entry(fetch_feed_items(client), TARGET_URL)
            detail = fetch_speech(client, TARGET_URL, include_body=True)
        if detail["verification_status"] != "verified_page_attribution":
            print("讲话归属或正文需要人工核对，未保存内容。")
            return 2
        seen_at = datetime.now(timezone.utc)
        engine = create_engine(
            database_url(settings), poolclass=NullPool, hide_parameters=True,
            connect_args={"connect_timeout": 5, "options": "-c lock_timeout=5000 -c statement_timeout=10000"},
        )
        with engine.begin() as connection:
            result = save_speech(connection, detail, entry, seen_at)
    except httpx.TimeoutException:
        print("官方来源请求超时，未保存内容。")
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"官方来源返回 HTTP {exc.response.status_code}，未保存内容。")
        return 1
    except httpx.RequestError:
        print("无法连接官方来源，请检查网络；未保存内容。")
        return 1
    except ValueError:
        print("来源配置、人物关联、页面或正文不符合入库要求，未保存内容。")
        return 1
    except Exception:
        print("单篇讲话入库失败，请检查来源响应、数据库连接及表结构；未显示连接凭证。")
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps({
        "mode": "manual_single_speech_import", **result,
        "source_enabled": source["enabled"],
        "rss_entry_found": entry is not None,
    }, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "needs_review" else 0


if __name__ == "__main__":
    raise SystemExit(main())
