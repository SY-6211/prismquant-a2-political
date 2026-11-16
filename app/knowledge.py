"""Local CPU index worker and semantic retrieval. No generation or source fetching."""
import argparse
import asyncio
from functools import lru_cache
import json
from threading import Lock
from uuid import UUID

from app.core.config import Settings
from app.core.database import create_read_engine
from app.local_embedding import LocalEmbedding, chunks, download, normalized
from app.repositories import agent_analysis, knowledge as repo

CPU = Lock()


class Busy(Exception):
    pass


@lru_cache(maxsize=1)
def encoder():
    # Missing files fail closed. Never download implicitly during a search/startup.
    return LocalEmbedding()


def write(settings, fn, *args):
    engine = agent_analysis.write_engine(settings)
    try:
        with engine.begin() as c:
            return fn(c, *args)
    finally:
        engine.dispose()


def read(settings, fn, *args):
    engine = create_read_engine(settings)
    try:
        with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
            return fn(c, *args)
    finally:
        engine.dispose()


def index_one(settings):
    if not CPU.acquire(blocking=False):
        raise Busy()
    target = None
    try:
        target = write(settings, repo.claim)
        if target is None:
            return {'state': 'empty'}
        if target.get('skipped'):
            return {'state': 'skipped'}
        model = encoder()
        parts = chunks(target['body'], model.token_count)
        cached = read(settings, repo.cached, list({p['text_sha256'] for p in parts}))
        missing = {p['text_sha256']: p['text'] for p in parts if p['text_sha256'] not in cached}
        keys = list(missing)
        for start in range(0, len(keys), 8):
            batch = keys[start:start + 8]
            cached.update(zip(batch, model.embed([missing[key] for key in batch])))
        result = write(settings, repo.publish, target, parts, [cached[p['text_sha256']] for p in parts])
        return {**result, 'embedded_chunks': len(missing), 'cached_chunks': len(parts) - len(missing), 'paid_api_calls': 0}
    except Exception:
        if target and not target.get('skipped'):
            write(settings, repo.fail, target)
        raise
    finally:
        CPU.release()


def rank(rows, vector, limit):
    import numpy as np
    if not rows:
        return []
    scores = np.asarray([normalized(row['embedding']) for row in rows], dtype=np.float32) @ np.asarray(normalized(vector), dtype=np.float32)
    # Stable ties; candidates arrive ordered by event and source offset.
    indices = sorted(range(len(rows)), key=lambda i: (-float(scores[i]), i))[:limit]
    return [{**{k: v for k, v in rows[i].items() if k != 'embedding'}, 'cosine_similarity': float(scores[i])} for i in indices]


def search(settings, person_id, query, limit=5, since=None, until=None):
    if not isinstance(query, str) or not query.strip() or len(query) > 500 or not 1 <= limit <= 10:
        raise ValueError('Invalid semantic query')
    if since and until and since >= until:
        raise ValueError('Invalid publication date range')
    if not CPU.acquire(blocking=False):
        raise Busy()
    try:
        vector = encoder().embed([query.strip()], role='query')[0]
        def retrieve(c):
            rows = repo.candidates(c, person_id, since, until)
            return {'items': rank(rows, vector, limit), 'candidate_chunks': len(rows), 'index': repo.status(c)}
        result = read(settings, retrieve)
        return {**result, 'retrieval': 'semantic_exact_cosine', 'generated_answer': False,
                'score_is_probability': False, 'paid_api_calls': 0}
    finally:
        CPU.release()


def tick(settings):
    state = write(settings, repo.heartbeat)
    if not state['enabled']:
        return
    write(settings, repo.enqueue, 100)
    # Bounded batches, then yield to requests and the next poll. No failed-job retry.
    for _ in range(3):
        try:
            if index_one(settings)['state'] == 'empty':
                break
        except Busy:
            break


async def loop(settings):
    while True:
        try:
            await asyncio.to_thread(tick, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The failed version is recorded; no network or paid fallback/retry.
            pass
        await asyncio.sleep(settings.knowledge_poll_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('download', 'status', 'enable', 'disable', 'enqueue'):
        sub.add_parser(name)
    build = sub.add_parser('build'); build.add_argument('--limit', type=int, choices=range(1, 301), default=300, metavar='1..300')
    retry = sub.add_parser('retry'); retry.add_argument('--version-id', type=UUID, required=True)
    query = sub.add_parser('search'); query.add_argument('--person-id', type=UUID, required=True); query.add_argument('--query', required=True)
    args = parser.parse_args(argv)
    try:
        settings = Settings()
        if args.action == 'download':
            value = download()
        elif args.action == 'status':
            value = read(settings, repo.status)
        elif args.action in ('enable', 'disable'):
            value = write(settings, repo.control, args.action == 'enable')
        elif args.action == 'enqueue':
            value = write(settings, repo.enqueue, 300)
        elif args.action == 'retry':
            value = write(settings, repo.reset_unsent, args.version_id)
        elif args.action == 'search':
            value = search(settings, args.person_id, args.query)
        else:
            value = {'queue': write(settings, repo.enqueue, args.limit), 'items': [], 'paid_api_calls': 0}
            for _ in range(args.limit):
                result = index_one(settings)
                if result['state'] == 'empty':
                    break
                value['items'].append(result)
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception:
        print('本地索引操作未完成，请检查依赖、模型校验、迁移和任务状态；未调用付费模型。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
