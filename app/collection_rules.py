"""Read-only configuration eligibility. This module never starts a collector."""

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, AwareDatetime

from app.batch_ecb import PERSON_ID as ECB_PERSON, SOURCE_ID as ECB_SOURCE
from app.preview_ecb import ECB_FEED_URL
from app.preview_trump import PERSON_ID as TRUMP_PERSON, SOURCE_ID as TRUMP_SOURCE, TRUMP_FEED_URL

# Only these already implemented adapters may be considered by a future scheduler.
SUPPORTED = {
    'ecb': (ECB_SOURCE, ECB_PERSON, ECB_FEED_URL, 'official'),
    'trump': (TRUMP_SOURCE, TRUMP_PERSON, TRUMP_FEED_URL, 'third_party_archive'),
}
MIN_AUTO_INTERVAL_SECONDS = 60
DEFAULT_BATCH_LIMIT = 3
MAX_BATCH_LIMIT = 5


class RuleReason(BaseModel):
    code: str
    message: str


class RuleDecision(BaseModel):
    source: Literal['ecb', 'trump']
    source_id: UUID
    person_id: UUID
    source_name: str | None
    person_name: str | None
    provenance_type: str | None
    poll_interval_seconds: int | None
    source_enabled: bool | None
    monitoring_enabled: bool | None
    link_enabled: bool | None
    status: Literal['eligible', 'disabled', 'invalid_configuration']
    eligible: bool
    reasons: list[RuleReason]


class RuleCheck(BaseModel):
    mode: Literal['collection_rule_check'] = 'collection_rule_check'
    checked_at: AwareDatetime
    scope: Literal['configuration_and_switches'] = 'configuration_and_switches'
    automatic_started: Literal[False] = False
    changes_saved: Literal[False] = False
    network_requested: Literal[False] = False
    min_auto_interval_seconds: int = MIN_AUTO_INTERVAL_SECONDS
    default_batch_limit: int = DEFAULT_BATCH_LIMIT
    max_batch_limit: int = MAX_BATCH_LIMIT
    eligible_count: int
    items: list[RuleDecision]


def evaluate_rule(source: str, context: dict) -> RuleDecision:
    if source not in SUPPORTED:
        raise ValueError('Unsupported collector')
    sid, pid, url, provenance = SUPPORTED[source]
    reasons = []
    def reject(code, message):
        reasons.append(RuleReason(code=code, message=message))
    if context.get('source_id') != sid:
        reject('source_missing_or_mismatched', '受支持的来源配置缺失或 ID 不匹配。')
    if context.get('person_id') != pid:
        reject('person_missing_or_mismatched', '目标人物配置缺失或 ID 不匹配。')
    if context.get('link_source_id') != sid or context.get('link_person_id') != pid:
        reject('association_missing_or_mismatched', '目标人物与来源的关联缺失或不匹配。')
    if context.get('feed_url') != url:
        reject('feed_url_mismatched', '订阅地址与已实现的固定来源入口不一致。')
    if context.get('provenance_type') != provenance or context.get('fetch_method') != 'rss':
        reject('adapter_mismatched', '来源类别或读取方式与已实现的采集器不一致。')
    interval = context.get('poll_interval_seconds')
    if type(interval) is not int or interval < MIN_AUTO_INTERVAL_SECONDS:
        reject('interval_invalid', '自动采集读取间隔必须至少为 60 秒。')
    flags = ('source_enabled', 'monitoring_enabled', 'link_enabled')
    if any(type(context.get(key)) is not bool for key in flags):
        reject('switch_state_unknown', '监控开关状态缺失或无效，不能允许自动采集。')
    invalid = bool(reasons)
    for key, message in (
        ('source_enabled', '来源开关关闭。'), ('monitoring_enabled', '人物监控开关关闭。'),
        ('link_enabled', '人物与来源的关联开关关闭。'),
    ):
        if context.get(key) is False:
            reject(key + '_off', message)
    eligible = not reasons
    if eligible:
        reject('configuration_allowed', '配置核对通过且三个开关均打开；仍须由后续调度检查执行时间、互斥及故障暂停状态。')
    return RuleDecision(
        source=source, source_id=sid, person_id=pid, source_name=context.get('source_name'),
        person_name=context.get('person_name'), provenance_type=context.get('provenance_type'),
        poll_interval_seconds=interval if type(interval) is int else None,
        **{key: context.get(key) if type(context.get(key)) is bool else None for key in flags},
        status='invalid_configuration' if invalid else ('eligible' if eligible else 'disabled'),
        eligible=eligible, reasons=reasons,
    )


def check_rules(settings, source=None):
    from app.repositories.collection_rules import read_contexts
    if source is not None and source not in SUPPORTED:
        raise ValueError('Unsupported source')
    keys = [source] if source else list(SUPPORTED)
    contexts = read_contexts(settings, keys)
    items = [evaluate_rule(key, contexts[key]) for key in keys]
    return RuleCheck(checked_at=datetime.now(timezone.utc), eligible_count=sum(item.eligible for item in items), items=items)
