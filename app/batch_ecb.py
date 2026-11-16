"""Read-only context and deterministic selection for bounded manual ECB batches."""

import hashlib
import re
from datetime import datetime, timezone
from uuid import uuid5

from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_read_engine
from app.preview_ecb import ECB_FEED_URL, published_utc
from app.preview_ecb_speech import SPEAKER_PATTERN, speech_url
from app.seed_people import NAMESPACE

SOURCE_ID = uuid5(NAMESPACE, "ecb-press-rss")
PERSON_ID = uuid5(NAMESPACE, "christine-lagarde")


def load_context(settings: Settings) -> dict:
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT s.name, s.feed_url, s.provenance_type, s.fetch_method, "
                "s.enabled AS source_enabled, p.monitoring_enabled, ps.enabled AS link_enabled "
                "FROM sources s JOIN person_sources ps ON ps.source_id=s.source_id "
                "JOIN people p ON p.person_id=ps.person_id "
                "WHERE s.source_id=:source AND p.person_id=:person"
            ), {"source": SOURCE_ID, "person": PERSON_ID}).mappings().one_or_none()
    finally:
        engine.dispose()
    if row is None or row["feed_url"] != ECB_FEED_URL or row["provenance_type"] != "official" or row["fetch_method"] != "rss":
        raise ValueError("Official feed and person association must match")
    return dict(row)


def read_existing(settings: Settings, urls: list[str]) -> dict[str, dict]:
    if not urls:
        return {}
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT record_id, retrieved_url, person_id, title, content_type, provenance_type, "
                "verification_status, body_text, content_hash, speaker_evidence "
                "FROM public_records WHERE source_id=:source AND retrieved_url=ANY(CAST(:urls AS text[]))"
            ), {"source": SOURCE_ID, "urls": urls}).mappings()
            return {row["retrieved_url"]: dict(row) for row in rows}
    finally:
        engine.dispose()


def prepare_candidates(items: list[dict]) -> dict:
    groups = {}
    stats = {"feed_items": len(items), "rss_duplicates": 0, "out_of_scope": 0,
             "candidate_count": 0, "precheck_needs_review": 0, "skipped_existing": 0,
             "selected": 0, "not_selected_due_limit": 0}
    for item in items:
        try:
            url = speech_url(item.get("url") or "")
        except ValueError:
            stats["out_of_scope"] += 1
            continue
        if url in groups:
            stats["rss_duplicates"] += 1
        groups.setdefault(url, []).append({**item, "url": url})
    candidates = []
    precheck = []
    for url, entries in groups.items():
        if not any(re.match(r"^Christine\s+Lagarde\s*:", item.get("title") or "", re.IGNORECASE) for item in entries):
            stats["out_of_scope"] += 1
            continue
        signatures = {(item.get("title"), item.get("published_at"), item.get("published_raw")) for item in entries}
        if len(signatures) != 1:
            stats["precheck_needs_review"] += 1
            precheck.append({"url": url, "status": "needs_review", "reason": "同一详情地址的 RSS 元数据冲突，未请求页面。"})
            continue
        candidates.append(entries[0])
    candidates.sort(key=lambda item: item["url"])
    candidates.sort(key=lambda item: datetime.fromisoformat(published_utc(item.get("published_at")))
                    if published_utc(item.get("published_at")) else datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    stats["candidate_count"] = len(candidates)
    return {"candidates": candidates, "selection": stats, "precheck_results": precheck}


def existing_complete(row: dict) -> bool:
    body = row.get("body_text")
    return bool(
        row.get("person_id") == PERSON_ID and row.get("content_type") == "public_statement"
        and row.get("provenance_type") == "official"
        and row.get("verification_status") == "verified_page_attribution"
        and isinstance(row.get("title"), str) and row["title"].strip()
        and isinstance(body, str) and len(body.strip()) >= 80
        and row.get("content_hash") == hashlib.sha256(body.encode("utf-8")).hexdigest()
        and isinstance(row.get("speaker_evidence"), str) and SPEAKER_PATTERN.match(row["speaker_evidence"])
    )


def select_unseen(prepared: dict, existing: dict[str, dict], limit: int, *, complete=existing_complete) -> dict:
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Batch limit must be 1–5")
    stats = dict(prepared["selection"])
    precheck = list(prepared["precheck_results"])
    unseen = []
    for item in prepared["candidates"]:
        row = existing.get(item["url"])
        if row is None:
            unseen.append(item)
        elif complete(row):
            stats["skipped_existing"] += 1
            precheck.append({"url": item["url"], "record_id": str(row["record_id"]), "status": "skipped_existing",
                             "reason": "本地已有完整记录，本次未重新读取网页或检查远程内容变化。"})
        else:
            stats["precheck_needs_review"] += 1
            precheck.append({"url": item["url"], "status": "needs_review",
                             "reason": "本地记录的正文、哈希或归属信息需核对，本次未请求详情或修复数据。"})
    selected = unseen[:limit]
    stats.update(selected=len(selected), not_selected_due_limit=max(0, len(unseen) - limit))
    return {"candidates": selected, "selection": stats, "precheck_results": precheck}
