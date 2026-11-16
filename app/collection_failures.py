from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime


def retry_time(value, now=None):
    now = now or datetime.now(timezone.utc)
    if not value:
        return None
    try:
        value = value.strip()
        if value.isascii() and value.isdigit():
            # Avoid unbounded numeric parsing; invalid/overflow headers use backoff.
            stamp = now + timedelta(seconds=int(value)) if len(value) <= 10 else None
        else:
            stamp = parsedate_to_datetime(value)
        if stamp is None or stamp.utcoffset() is None:
            return None
        return max(now, stamp.astimezone(timezone.utc)).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def http_failure(exc):
    raw = exc.response.headers.get('Retry-After')
    stamp = retry_time(raw)
    return {'failure_kind': 'http', 'http_status': exc.response.status_code,
            'retry_not_before': stamp, 'retry_after_invalid': bool(raw and stamp is None)}


def recovery_policy(report, previous, interval, now):
    if report.get('run_record_status') in ('start_failed', 'finalization_failed') or report.get('write_outcome_unknown') or any(i.get('write_outcome_unknown') for i in report.get('items', [])):
        return previous + 1, None, '写入或运行记录结果需人工核对。'
    kind, code = report.get('failure_kind'), report.get('http_status')
    if report.get('retry_after_invalid'):
        return previous + 1, None, 'Retry-After 无法安全解析，需人工核对等待要求。'
    if kind == 'network' or (kind == 'http' and (code == 429 or 500 <= code <= 599)):
        count = previous + 1
        wait = max(interval, min(21600, 60 * 2 ** min(count - 1, 9)))
        deadline = now + timedelta(seconds=wait)
        raw = report.get('retry_not_before')
        if raw:
            try:
                stamp = datetime.fromisoformat(raw)
                if stamp.utcoffset() is not None:
                    deadline = max(deadline, stamp)
            except ValueError:
                pass
        return count, deadline, None
    if report.get('status') in ('stopped', 'interrupted'):
        return previous + 1, None, '来源权限、入口、配置或执行中断需人工核对。'
    return 0, None, None
