"""Queue contracts. No arbitrary URLs, Python functions or model calls in jobs."""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

Lane = Literal['raw', 'history', 'media', 'analysis']
LANES = ('raw', 'history', 'media', 'analysis')
SOURCE_NAMES = ('ecb', 'trump')


class CollectPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: Literal['ecb', 'trump']
    limit: StrictInt = Field(default=3, ge=1, le=5)
    discovery: Literal['rss','ecb_index'] = 'rss'

    @model_validator(mode='after')
    def channel_source(self):
        if self.discovery=='ecb_index' and self.source!='ecb':
            raise ValueError('ECB discovery is only valid for the ECB source')
        return self


class HistoryPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: Literal['ecb', 'trump']
    since: str
    until: str
    entry: dict


def normalized_payload(kind, payload):
    if kind == 'collect_source':
        value = CollectPayload.model_validate(payload).model_dump()
        if value['discovery']=='rss': value.pop('discovery')
        return value, 'raw', 100
    if kind != 'history_item':
        raise ValueError('Media/analysis handlers have not been implemented')
    from app.backfill import validate_plan, source_tools
    value = HistoryPayload.model_validate(payload).model_dump()
    if (date.fromisoformat(value['until']) - date.fromisoformat(value['since'])).days > 366:
        raise ValueError('History range exceeds 367 days')
    allowed = {'url','title','published_at','published_raw','metadata_origin','original_url',
               'archive_author','archive_post_type','archive_removed_raw'}
    value['entry'] = {key: val for key, val in value['entry'].items() if key in allowed}
    validate_plan({'version': 1, 'source': value['source'], 'since': value['since'], 'until': value['until'],
                   'candidates': [value['entry']]})
    batch, _ = source_tools(value['source'])
    prepared = batch.prepare_candidates([value['entry']])
    if len(prepared['candidates']) != 1 or prepared['selection']['precheck_needs_review']:
        raise ValueError('History item is unsupported or needs review')
    value['entry'] = prepared['candidates'][0]
    return value, 'history', 10


def payload_key(kind, payload, request_id):
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Request ID must contain 1..128 characters')
    encoded = json.dumps([kind, payload, request_id], sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode('utf-8')) > 32768:
        raise ValueError('Task payload exceeds 32 KiB')
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class Outcome:
    disposition: Literal['succeeded', 'deferred', 'retry', 'needs_review', 'cancelled']
    code: str
    delay_seconds: int = 60
    not_before: datetime | None = None
    result: dict = field(default_factory=dict)
    stop_worker: bool = False


def outcome_for_report(report):
    """Conservative classification: uncertain writes and broken records never replay blindly."""
    status = report.get('status')
    summary = report.get('summary') or {}
    if (report.get('run_record_status') in ('start_failed', 'finalization_failed')
            or report.get('write_outcome_unknown') or any(i.get('write_outcome_unknown') for i in report.get('items', []))):
        return Outcome('needs_review', 'write_or_run_unknown')
    if status in ('blocked_unfinished_run', 'paused', 'invalid_configuration', 'blocked_plan_needs_review'):
        return Outcome('needs_review', status)
    if status == 'disabled':
        return Outcome('cancelled', 'source_disabled')
    if status in ('skipped_busy', 'not_due', 'backoff', 'blocked_source_pause_or_backoff'):
        stamp = report.get('retry_not_before')
        stamp = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
        if stamp is not None and stamp.utcoffset() is None:
            return Outcome('needs_review', 'invalid_retry_time')
        return Outcome('deferred', status, delay_seconds=60, not_before=stamp)
    if report.get('retry_after_invalid'):
        return Outcome('needs_review', 'invalid_retry_after')
    code, kind = report.get('http_status'), report.get('failure_kind')
    if kind == 'network' or (kind == 'http' and isinstance(code, int) and (code == 429 or 500 <= code <= 599)):
        stamp = report.get('retry_not_before')
        stamp = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
        if stamp is not None and stamp.utcoffset() is None:
            return Outcome('needs_review', 'invalid_retry_time')
        return Outcome('retry', 'transient_source_failure', not_before=stamp)
    if status in ('completed', 'no_candidates', 'no_new_candidates') and not summary.get('failed'):
        return Outcome('succeeded', status)
    if status == 'completed_with_issues' and not summary.get('failed') and not summary.get('interrupted'):
        # The finite batch finished; individual content review lives in collection_run_items.
        return Outcome('succeeded', 'completed_with_content_review')
    return Outcome('needs_review', 'collector_needs_review')
