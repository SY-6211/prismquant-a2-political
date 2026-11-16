"""SSE hints only; clients fetch canonical records/versions through read-only APIs."""
import asyncio
import json
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.repositories.content_stream import read_batch

router = APIRouter(prefix='/api/v1/content-stream', tags=['内容实时更新'])
CURSOR = re.compile(r'^([0-9a-f]{32}):(0|[1-9][0-9]{0,18})$')


def parse_cursor(value):
    if value is None:
        return None
    match = CURSOR.fullmatch(value)
    if match is None or int(match[2]) > 9223372036854775807:
        raise HTTPException(422, detail={'code': 'invalid_stream_cursor', 'message': '更新游标无效，请重新同步列表。'})
    return match[1], int(match[2])


def cursor(epoch, position):
    return f'{epoch}:{position}'


def frame(kind, data, identity=None):
    value = f'event: {kind}\n'
    if identity is not None:
        value += f'id: {identity}\n'
    return value + 'data: ' + json.dumps(jsonable_encoder(data), ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n\n'


def reset_reason(batch, after):
    if after is None:
        return None
    if after[0] != batch['epoch']:
        return 'epoch_changed'
    if after[1] < batch['floor']:
        return 'retention_gap'
    if after[1] > batch['head']:
        return 'cursor_ahead'
    return None


async def events(request, initial, after, *, poll_seconds=1, lifetime=300):
    batch = initial
    reason = reset_reason(batch, after)
    if after is None or reason:
        after = (batch['epoch'], batch['head'])
    yield 'retry: 3000\n\n'
    yield frame('reset' if reason else 'ready', {'reason': reason, 'cursor': cursor(*after)}, cursor(*after))
    deadline = time.monotonic() + lifetime
    heartbeat = time.monotonic()
    while time.monotonic() < deadline:
        if await request.is_disconnected():
            return
        reason = reset_reason(batch, after)
        if reason:
            after = (batch['epoch'], batch['head'])
            yield frame('reset', {'reason': reason, 'cursor': cursor(*after)}, cursor(*after))
        else:
            for item in batch['items']:
                after = (batch['epoch'], item['position'])
                payload = {key: value for key, value in item.items() if key != 'position'}
                yield frame('content', payload, cursor(*after))
        if time.monotonic()-heartbeat >= 15:
            yield frame('heartbeat', {'server_time': datetime.now(timezone.utc)})
            heartbeat = time.monotonic()
        # Catch up immediately in bounded pages; release DB connections before yielding.
        if not batch['items'] or after[1] >= batch['head']:
            await asyncio.sleep(poll_seconds)
        try:
            batch = await run_in_threadpool(read_batch, request.app.state.settings, after)
        except (SQLAlchemyError, ValueError):
            yield frame('unavailable', {'message': '更新连接暂不可用，页面将定时同步。'})
            return


@router.get('/state', summary='获取实时更新起点（启动时先读起点，再读内容）')
def stream_state(request: Request):
    try:
        batch = read_batch(request.app.state.settings)
    except (SQLAlchemyError, ValueError):
        raise HTTPException(503, detail={'code': 'stream_unavailable', 'message': '实时更新暂不可用。'})
    return {'cursor': cursor(batch['epoch'], batch['head']), 'replay_floor': cursor(batch['epoch'], batch['floor']),
            'retained_limit': 10000, 'heartbeat_seconds': 15, 'poll_seconds': 1}


@router.get('', summary='订阅已提交的新增内容和版本提示', response_class=StreamingResponse,
            responses={200: {'content': {'text/event-stream': {}}}, 503: {'description': '数据库暂不可用'}})
async def stream(request: Request, after: str | None = Query(default=None, max_length=52),
                 last_event_id: str | None = Header(default=None, max_length=52)):
    position = parse_cursor(last_event_id if last_event_id is not None else after)
    try:
        initial = await run_in_threadpool(read_batch, request.app.state.settings, position)
    except (SQLAlchemyError, ValueError):
        raise HTTPException(503, detail={'code': 'stream_unavailable', 'message': '实时更新暂不可用。'})
    return StreamingResponse(events(request, initial, position), media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache, no-store', 'X-Accel-Buffering': 'no'})
