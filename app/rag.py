"""Bounded hybrid retrieval in the reader's single PostgreSQL snapshot."""
import hashlib
import re
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from app import knowledge
from app.local_embedding import PROFILE_ID
from app.repositories import knowledge as index

VERSION = 'person_hybrid_rrf_v1'
WINDOW = 60
RRF_K = 60
TOPICS = [
    (('关税','贸易','tariff','trade'), ('tariff','trade')),
    (('利率','降息','加息','interest','rate cut'), ('interest rate','rate cut','monetary policy')),
    (('通胀','inflation'), ('inflation',)),
    (('人工智能','金融风险','artificial intelligence',' ai '), ('artificial intelligence','financial risk')),
    (('股市','stock market','stocks'), ('stock market','stocks','economic')),
    (('选举','election'), ('election',)),
    (('能源','energy'), ('energy',)),
    (('半导体','芯片','semiconductor'), ('semiconductor','chip')),
]
STOP = set('a an the is are was were of to in on at with for from by and or about what when how why did does do has have he she his her this that these those please according saved statements said say compare'.split())


def terms_for(question, explicit=None):
    if explicit:
        return [explicit.strip().lower()]
    q = question.lower(); values = []
    for triggers, terms in TOPICS:
        if any(t in q for t in triggers):
            values.extend(terms)
    values.extend(word for word in re.findall(r'[a-z][a-z0-9-]{2,39}', q) if word not in STOP)
    # Literal CJK phrases supplement semantic matching; no remote query expansion.
    values.extend(re.findall(r'[\u3400-\u9fff]{2,20}', q))
    return list(dict.fromkeys(v.lower() for v in values))[:12]


def fuse(lexical, semantic, limit, explicit=None):
    scores = {}; candidates = {}; ranks = {}
    for channel, rows in (('lexical', lexical), ('semantic', semantic)):
        seen = set()
        for row in rows:
            key = (str(row['event_id']), str(row['version_id']))
            if key in seen:
                continue
            seen.add(key); rank = len(seen)
            scores[key] = scores.get(key, 0.) + 1. / (RRF_K + rank)
            ranks.setdefault(key, {})[channel] = rank
            if key not in candidates or (channel == 'semantic' and (not explicit or explicit.lower() in row['text'].lower())):
                candidates[key] = row
    # On an RRF tie, prefer the semantic rank for cross-language questions;
    # lexical-only ties remain deterministic. This is not a confidence estimate.
    keys = sorted(scores, key=lambda key: (-scores[key], ranks[key].get('semantic', WINDOW+1), ranks[key].get('lexical', WINDOW+1), key))[:limit]
    return [{**{k: v for k, v in candidates[key].items() if k != 'embedding'},
             'rrf_score': scores[key], 'channel_ranks': ranks[key]} for key in keys]


def retrieve(reader, args):
    c = reader.connection
    terms = terms_for(args.question, args.query)
    params = {'person': args.person_id, 'since': args.since, 'until': args.until,
              'head': reader.head, 'q': args.query or '', 'terms': terms}
    latest = index.LATEST.replace('FROM event_observations\n', 'FROM event_observations WHERE observation_id<=:head\n')
    scope = index.ELIGIBLE + ''' AND v.person_id=:person
      AND (CAST(:since AS timestamptz) IS NULL OR (v.payload->>'published_at')::timestamptz>=:since)
      AND (CAST(:until AS timestamptz) IS NULL OR (v.payload->>'published_at')::timestamptz<:until)
      AND (:q='' OR strpos(lower(coalesce(v.payload->>'main_text','')),lower(:q))>0 OR strpos(lower(v.payload->>'title'),lower(:q))>0)'''
    eligible = c.execute(text(latest+'SELECT count(*) FROM current_versions o JOIN event_versions v USING(event_id,version_id) WHERE '+scope), params).scalar_one()
    lexical = []
    if terms:
        lexical = [dict(r) for r in c.execute(text(latest+'''SELECT v.event_id,v.version_id,v.person_id,v.snapshot_hash,
          greatest(coalesce(h.first_hit,1)-201,0) AS start_offset,
          greatest(coalesce(h.first_hit,1)-201,0)+char_length(substring(v.payload->>'main_text' FROM greatest(coalesce(h.first_hit,1)-201,0)+1 FOR 1000)) AS end_offset,
          substring(v.payload->>'main_text' FROM greatest(coalesce(h.first_hit,1)-201,0)+1 FOR 1000) AS text
          FROM current_versions o JOIN event_versions v USING(event_id,version_id)
          CROSS JOIN LATERAL(SELECT count(*) FILTER(WHERE strpos(lower(coalesce(v.payload->>'title','')||' '||coalesce(v.payload->>'main_text','')),term)>0) AS hits,
            min(nullif(strpos(lower(coalesce(v.payload->>'main_text','')),term),0)) AS first_hit
            FROM unnest(CAST(:terms AS text[])) term) h
          WHERE '''+scope+''' AND h.hits>0
          ORDER BY h.hits DESC,o.observed_at DESC,v.event_id LIMIT 61'''), params).mappings()]
    lexical_truncated = len(lexical) > WINDOW; lexical = lexical[:WINDOW]
    semantic = []; semantic_state = 'available'; ready_versions = None; semantic_truncated = False
    semantic_query = args.question[:500]; query_truncated = args.question_truncated or len(args.question) > 500
    if not knowledge.CPU.acquire(blocking=False):
        semantic_state = 'local_cpu_busy'
    else:
        try:
            model = knowledge.encoder()
            while semantic_query and model.token_count(semantic_query, 'query') > 512:
                semantic_query = semantic_query[:-16]; query_truncated = True
            if not semantic_query.strip():
                raise ValueError('Empty bounded semantic query')
            vector = model.embed([semantic_query], role='query')[0]
            # Optional vector tables may be absent on an older deployment. A
            # savepoint preserves the read transaction for explicit lexical fallback.
            with c.begin_nested():
                rows = index.candidates(c, args.person_id, args.since, args.until, reader.head, args.query)
            ready_versions = len({r['version_id'] for r in rows})
            semantic = knowledge.rank(rows, vector, min(len(rows), 10000))
            seen = set(); distinct = []
            for row in semantic:
                if row['version_id'] not in seen:
                    seen.add(row['version_id']); distinct.append(row)
            semantic_truncated = len(distinct) > WINDOW; semantic = distinct[:WINDOW]
            if not rows:
                semantic_state = 'no_ready_index_in_scope'
        except Exception:
            semantic_state = 'local_vector_unavailable'
        finally:
            knowledge.CPU.release()
    items = fuse(lexical, semantic, args.limit, args.query)
    fields=('event_id','version_id','person_id','snapshot_hash','start_offset','end_offset','text','rrf_score','channel_ranks')
    items=[{**{k:row[k] for k in fields},'text_sha256':hashlib.sha256(row['text'].encode()).hexdigest()} for row in items]
    return {'status': 'ok', 'items': items, 'retrieval': {
        'version': VERSION, 'mode': 'hybrid_rrf' if semantic_state == 'available' else 'hybrid_lexical_fallback',
        'queries': terms, 'query_truncated': query_truncated,
        'semantic_state': semantic_state, 'semantic_vector_search': bool(semantic),
        'profile_id': PROFILE_ID if semantic else None, 'eligible_versions_in_scope': eligible,
        'ready_versions_in_scope': ready_versions, 'index_coverage_complete': ready_versions == eligible if ready_versions is not None else None,
        'lexical_candidates': len(lexical), 'semantic_candidates': len(semantic), 'candidate_window': WINDOW,
        'lexical_truncated': lexical_truncated, 'semantic_truncated': semantic_truncated,
        'fusion': 'reciprocal_rank_fusion', 'rank_constant': RRF_K, 'score_is_confidence': False,
        'candidate_count': len(items), 'exhaustive': False, 'same_database_snapshot': True,
        'historical_availability_verified': False}}
