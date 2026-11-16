"""Read-only planning and full extraction checks for Trump archive previews."""

import re
from datetime import datetime, timezone

from sqlalchemy import text

from app.batch_ecb import select_unseen as select_plan
from app.core.database import create_read_engine
from app.extract_trump_post import prepare_extraction
from app.import_trump_post import ArchiveDraft, RecordDraft, snapshot_hash
from app.preview_trump import PERSON_ID, SOURCE_ID, load_source, preview_link
from app.preview_trump_post import archive_url


def load_context(settings):
    source = load_source(settings)
    return {**source, 'source_enabled': source['enabled']}


def prepare_candidates(items):
    stats = dict(feed_items=len(items), rss_duplicates=0, out_of_scope=0, candidate_count=0,
                 precheck_needs_review=0, skipped_existing=0, selected=0, not_selected_due_limit=0)
    groups, candidates, precheck = {}, [], []
    for item in items:
        try:
            url = archive_url(item.get('url') or '')
        except ValueError:
            stats['precheck_needs_review'] += 1
            precheck.append({'url': None, 'status': 'needs_review', 'reason': '归档详情地址缺失或不符合固定主机路径，未请求页面。'})
            continue
        if url in groups:
            stats['rss_duplicates'] += 1
        groups.setdefault(url, []).append({**item, 'url': url})
    keys = ('title', 'published_at', 'published_raw', 'original_url', 'archive_author',
            'archive_post_type', 'archive_removed_raw')
    for url, entries in groups.items():
        entry = entries[0]
        reason = None
        if any(any(other.get(key) != entry.get(key) for key in keys) for other in entries[1:]):
            reason = '同一归档地址的 RSS 元数据冲突。'
        elif entry.get('archive_author') != 'Donald J. Trump' or not re.fullmatch(
            r'https://(?:www\.)?truthsocial\.com/@realDonaldTrump/(?:posts/)?\d+',
            preview_link(entry.get('original_url'), original=True) or '',
        ):
            reason = 'RSS 作者或预期账号的原始地址缺失或不匹配。'
        elif entry.get('archive_post_type') not in ('original', 'quote', 'retruth'):
            reason = 'RSS 帖子类型未知或缺失。'
        elif not entry.get('title') or not entry['title'].strip():
            reason = 'RSS 标题缺失。'
        elif entry['archive_post_type'] in ('quote', 'retruth'):
            stats['out_of_scope'] += 1
            precheck.append({'url': url, 'status': 'skipped_unsupported', 'reason': '引用或转发超出首版范围，未读取详情。'})
            continue
        if reason:
            stats['precheck_needs_review'] += 1
            precheck.append({'url': url, 'status': 'needs_review', 'reason': reason + ' 未请求详情。'})
        else:
            candidates.append(entry)
    candidates.sort(key=lambda item: item['url'])
    candidates.sort(key=lambda item: datetime.fromisoformat(item['published_at']) if item.get('published_at')
                    else datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    stats['candidate_count'] = len(candidates)
    return {'candidates': candidates, 'selection': stats, 'precheck_results': precheck}


def read_existing(settings, urls):
    if not urls:
        return {}
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text(
                'SELECT r.*, CASE WHEN a.record_id IS NULL THEN NULL ELSE to_jsonb(a) END AS archive_post '
                'FROM public_records r LEFT JOIN archive_post_details a USING(record_id) '
                'WHERE r.source_id=:source AND r.retrieved_url=ANY(CAST(:urls AS text[]))'
            ), {'source': SOURCE_ID, 'urls': urls}).mappings()
            return {row['retrieved_url']: dict(row) for row in rows}
    finally:
        engine.dispose()


def existing_complete(row):
    try:
        archive = row.get('archive_post')
        if not archive:
            return False
        record = RecordDraft.model_validate({key: row[key] for key in RecordDraft.model_fields})
        draft = ArchiveDraft.model_validate({key: archive[key] for key in ArchiveDraft.model_fields})
        return (record.source_id == SOURCE_ID and record.person_id == PERSON_ID
                and (not record.published_at or not draft.page_post_published_at
                     or record.published_at == draft.page_post_published_at)
                and snapshot_hash(record, draft) == archive['snapshot_hash'])
    except (ValueError, KeyError, TypeError):
        return False


def select_unseen(prepared, existing, limit):
    return select_plan(prepared, existing, limit, complete=existing_complete)


def classify(detail, candidate):
    extraction = prepare_extraction(detail, candidate)
    if not extraction['extraction_complete']:
        return 'needs_review', extraction['ingestion_reason']
    if not extraction['ingestion_eligible']:
        return 'skipped_unsupported', extraction['ingestion_reason']
    # Use the same strict drafts as the single writer, without calling a writer.
    RecordDraft.model_validate(extraction['record'])
    ArchiveDraft.model_validate(extraction['archive_post'])
    return 'ready_for_import', extraction['ingestion_reason']


def bounded(value):
    """Display full extraction in bounded sections; never return writable drafts."""
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            if key == 'text':
                output.update(text_preview=item[:1200] if item else item,
                              text_characters=len(item) if item else 0,
                              text_preview_truncated=bool(item and len(item) > 1200))
            elif isinstance(item, list):
                output[key] = [bounded(part) for part in item[:5]]
                # Full extraction uses *_omitted=0; do not override display omission counts.
                output[key + '_preview_omitted'] = max(0, len(item) - 5)
            else:
                output[key] = bounded(item)
        return output
    return value
