"""Stage measurements, never an inferred source-publication-to-screen SLA."""
import math
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, FiniteFloat, StrictBool, ValidationError, field_validator

Duration = Annotated[FiniteFloat, Field(ge=0)]
InputKind = Literal['rss_network', 'provided_items']


class TimingBase(BaseModel):
    input_kind: InputKind | None = None
    measurement_error: Literal['invalid_timing'] | None = None

    @field_validator('*', mode='after')
    @classmethod
    def utc(cls, value):
        return value.astimezone(timezone.utc) if isinstance(value, datetime) else value


class RunTiming(TimingBase):
    list_started_at: AwareDatetime | None = None
    list_finished_at: AwareDatetime | None = None
    list_duration_ms: Duration | None = None
    list_succeeded: StrictBool | None = None


class ItemTiming(TimingBase):
    list_observed_at: AwareDatetime | None = None
    detail_started_at: AwareDatetime | None = None
    detail_finished_at: AwareDatetime | None = None
    detail_duration_ms: Duration | None = None
    detail_succeeded: StrictBool | None = None
    save_started_at: AwareDatetime | None = None
    save_returned_at: AwareDatetime | None = None
    save_duration_ms: Duration | None = None
    observed_to_save_ms: Duration | None = None


def clean_timing(value, model):
    if not value:
        return {}
    try:
        # Extras are dropped, preventing bodies/driver errors/credentials from
        # leaking into the operational metadata. Bad telemetry never retries writes.
        return model.model_validate(value).model_dump(mode='json', exclude_none=True)
    except (ValidationError, TypeError, ValueError):
        return {'measurement_error': 'invalid_timing'}


def distribution(values):
    ordered = sorted(values)
    def percentile(fraction):
        return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 3) if ordered else None
    return {'samples': len(ordered), 'p50_ms': percentile(.5), 'p95_ms': percentile(.95),
            'p99_ms': percentile(.99), 'sufficient_for_percentiles': len(ordered) >= 100}


def build_baseline(data, hours):
    sources = []
    for config in data['sources']:
        source = config['source']
        runs = [row for row in data['runs'] if row['source'] == source]
        items = [row for row in data['items'] if row['source'] == source]
        list_values, detail_values, save_values, total_values = [], [], [], []
        failures = {'list': 0, 'detail': 0, 'unknown_write': 0, 'invalid_timing': 0}
        for row in runs:
            if row['mode'] != 'automatic_batch_import':
                continue
            timing = clean_timing(row.get('timing'), RunTiming)
            if timing.get('measurement_error'):
                failures['invalid_timing'] += 1
            if timing.get('input_kind') != 'rss_network':
                continue
            if timing.get('list_succeeded') is False:
                failures['list'] += 1
            if timing.get('list_succeeded') and timing.get('list_started_at') and timing.get('list_finished_at') and timing.get('list_duration_ms') is not None:
                list_values.append(timing['list_duration_ms'])
        automatic_items = 0
        for row in items:
            if row['mode'] != 'automatic_batch_import':
                continue
            automatic_items += 1
            timing = clean_timing(row.get('timing'), ItemTiming)
            if timing.get('measurement_error'):
                failures['invalid_timing'] += 1
            if row.get('write_outcome_unknown'):
                failures['unknown_write'] += 1
            if timing.get('input_kind') != 'rss_network':
                continue
            if timing.get('detail_succeeded') is False:
                failures['detail'] += 1
            if timing.get('detail_succeeded') and timing.get('detail_started_at') and timing.get('detail_finished_at') and timing.get('detail_duration_ms') is not None:
                detail_values.append(timing['detail_duration_ms'])
            if row['status'] != 'inserted' or row.get('write_outcome_unknown'):
                continue
            if timing.get('save_started_at') and timing.get('save_returned_at') and timing.get('save_duration_ms') is not None:
                save_values.append(timing['save_duration_ms'])
            if timing.get('list_observed_at') and timing.get('save_returned_at') and timing.get('observed_to_save_ms') is not None:
                total_values.append(timing['observed_to_save_ms'])
        sources.append({**config, 'run_rows': len(runs), 'automatic_item_rows': automatic_items,
            'manual_or_history_item_rows': sum(row['mode'] != 'automatic_batch_import' for row in items),
            'unmeasured_item_rows': sum(not row.get('timing') or bool(clean_timing(row.get('timing'), ItemTiming).get('measurement_error')) for row in items),
            'list_fetch_and_parse': distribution(list_values), 'detail_fetch_and_parse': distribution(detail_values),
            'confirmed_save_call': distribution(save_values), 'observed_in_run_to_save_return': distribution(total_values),
            'failures': failures,
            'public_to_page': {'status': 'not_measured', 'p95_ms': None,
                'reason': '尚无独立的首次公开观测和浏览器显示回执；不能用历史发布时间或刷新时间替代。'}})
    return {'scope': 'collection_stage_baseline', 'checked_at': datetime.now(timezone.utc),
            'window_hours': hours, 'sample_limit_per_query': 1000, 'truncated': data['truncated'],
            'target_public_to_page_p95_seconds': 60, 'target_status': 'not_measured',
            'frontend_list_refresh_seconds': 60, 'minimum_auto_interval_seconds': 60,
            'sources': sources,
            'notes': ['仅统计新版本记录的计时，旧数据保持未测量。',
                      '分位数采用最近秩；少于100个样本不作实时达标结论。',
                      '自动新插入的观测到保存与人工/历史回补、已有记录分开。',
                      '无效计时与提交未知诊断统计自动运行；损坏元数据无法判断输入类型，不纳入耗时样本。',
                      '耗时来自同进程单调时钟；UTC时间是观测时点，不是原站首次公开时间。']}
