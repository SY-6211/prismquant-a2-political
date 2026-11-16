"""No model or embedding calls. Private reports never turn user feedback into truth."""
import hashlib
import json
from uuid import uuid4
from sqlalchemy import text
from app.annotation_model import digest
from app.quality_model import VERSION, Review, audit, metrics
from app.repositories import chat, qa

# This is the same event/version holdout boundary used by production retrieval.
RESERVED = """EXISTS(SELECT 1 FROM annotation_samples a WHERE a.split='blind' AND a.origin='source'
 AND (a.event_key=v.event_id::text OR a.snapshot->>'event_version_id'=v.version_id::text))"""


def answer(c, user, conversation, run_id):
    run = qa.detail(c, user, conversation, run_id)
    if run['result_id'] is None:
        raise chat.Conflict('尚未保存回答结果，不能评价准备中的任务。')
    run['response_hash'] = c.execute(text('SELECT response_hash FROM agent_qa_results WHERE result_id=:r'), {'r': run['result_id']}).scalar_one()
    return run


def feedback(c, user, conversation, run_id, body):
    run = answer(c, user, conversation, run_id)
    # Serialize per-user writes: retries remain idempotent and the daily cap is atomic.
    c.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': 'pq_quality_user:'+str(user)})
    old = c.execute(text('SELECT * FROM agent_answer_feedback WHERE user_id=:u AND client_request_id=:r'),
                    {'u': user, 'r': body.client_request_id}).mappings().one_or_none()
    if old:
        if old['run_id'] != run_id or old['kind'] != body.kind or old['note'] != body.note:
            raise chat.Conflict('该反馈标识已保存其他内容，请先核对历史。')
        return {**dict(old), 'created': False, 'model_requests': 0}
    if c.execute(text("SELECT count(*) FROM agent_answer_feedback WHERE user_id=:u AND created_at>clock_timestamp()-interval '1 day'"), {'u': user}).scalar_one() >= 120:
        raise chat.Conflict('今日反馈已达120条，请先复核已有记录。')
    row = c.execute(text('INSERT INTO agent_answer_feedback(feedback_id,user_id,run_id,result_id,client_request_id,kind,note) '
                         'VALUES (:id,:u,:r,:result,:request,:kind,:note) RETURNING *'),
                    {'id': uuid4(), 'u': user, 'r': run_id, 'result': run['result_id'],
                     'request': body.client_request_id, 'kind': body.kind, 'note': body.note}).mappings().one()
    return {**dict(row), 'created': True, 'model_requests': 0}


def feedback_history(c, user, conversation, run_id):
    answer(c, user, conversation, run_id)
    return {'items': [dict(r) for r in c.execute(text('SELECT feedback_id,kind,note,created_at FROM agent_answer_feedback '
        'WHERE user_id=:u AND run_id=:r ORDER BY created_at DESC,feedback_id DESC LIMIT 20'), {'u': user, 'r': run_id}).mappings()],
        'interpretation': 'personal_feedback_not_ground_truth', 'model_requests': 0}


def case(c, case_id, user=None):
    row = c.execute(text('SELECT * FROM agent_quality_cases WHERE case_id=:id AND '
        '(CAST(:u AS uuid) IS NULL OR user_id=:u)'), {'id': case_id, 'u': user}).mappings().one_or_none()
    if row is None:
        raise chat.Missing()
    # A later holdout reservation also removes already-frozen cases from these paths.
    reserved = c.execute(text("SELECT EXISTS(SELECT 1 FROM event_versions v WHERE v.version_id=ANY(CAST(:ids AS uuid[])) AND " + RESERVED + ')'),
                         {'ids': [x['version_id'] for x in row['packet']['pool']]}).scalar_one()
    if reserved:
        raise chat.Missing()
    if digest(row['packet']) != row['packet_hash']:
        raise chat.Invalid('评测快照校验失败。')
    return dict(row)


def create_case(c, user, conversation, run_id):
    run = answer(c, user, conversation, run_id)
    if run['status'] not in ('completed', 'abstained') or not run['answer']:
        raise chat.Conflict('评测案例需要已保存的有效回答或明确弃答。')
    c.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': 'pq_quality_user:'+str(user)})
    old = c.execute(text('SELECT case_id FROM agent_quality_cases WHERE user_id=:u AND run_id=:r'), {'u': user, 'r': run_id}).scalar_one_or_none()
    if old:
        return {'created': False, **case(c, old, user)}
    if c.execute(text('SELECT count(*) FROM agent_quality_cases WHERE user_id=:u'), {'u': user}).scalar_one() >= 200:
        raise chat.Conflict('本地每人最多200个冻结案例。')
    context = run['context']
    retrieved = list(dict.fromkeys(s['version_id'] for s in context['sources']))
    # Same reported-date scope, accepted originals/speeches. Include retrieved versions,
    # then fill with recent eligible versions; this is a CLOSED, visible candidate pool.
    rows = [dict(r) for r in c.execute(text("""SELECT v.version_id,v.event_id,v.person_id,v.payload,v.snapshot_hash
      FROM event_versions v WHERE v.person_id=:p AND v.review_status='accepted'
      AND v.payload->>'post_type' IN ('original','speech')
      AND v.payload->>'content_type' IN ('public_statement','institution_announcement')
      AND char_length(coalesce(v.payload->>'main_text','')) BETWEEN 1 AND 100000
      AND NOT """ + RESERVED + """
      AND (v.version_id=ANY(CAST(:ids AS uuid[])) OR (
        (CAST(:since AS timestamptz) IS NULL OR (v.payload->>'published_at')::timestamptz>=:since)
        AND (CAST(:until AS timestamptz) IS NULL OR (v.payload->>'published_at')::timestamptz<:until)))
      ORDER BY (v.version_id=ANY(CAST(:ids AS uuid[]))) DESC, v.created_at DESC,v.version_id DESC LIMIT 12"""),
        {'p': context['person_id'], 'ids': retrieved, 'since': context['retrieval'].get('since'), 'until': context['retrieval'].get('until')}).mappings()]
    if not set(retrieved).issubset({str(x['version_id']) for x in rows}):
        raise chat.Conflict('原检索资料包含保留盲测或不适用版本，不能加入开发评测。')
    pool = []
    for row in rows:
        payload = row['payload']; body = payload['main_text']
        pool.append({'version_id': str(row['version_id']), 'event_id': str(row['event_id']),
            'title': payload['title'], 'body': body, 'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
            'snapshot_hash': row['snapshot_hash'], 'published_at': payload.get('published_at'),
            'provenance_type': payload['provenance_type'], 'verification_status': payload['verification_status'],
            'retrieved_url': payload.get('retrieved_url')})
    if sum(len(x['body']) for x in pool) > 120000:
        raise chat.Conflict('候选全文超过12万字符，请缩小资料日期范围后准备新回答。')
    by_version={x['version_id']: x for x in pool}
    for source in context['sources']:
        frozen=by_version[source['version_id']]
        if (source['snapshot_hash']!=frozen['snapshot_hash'] or source['text_sha256']!=hashlib.sha256(source['text'].encode()).hexdigest()
                or frozen['body'][source['start']:source['end']]!=source['text']):
            raise chat.Invalid('检索片段与保存正文不匹配，需先核对。')
    packet = {'version': VERSION, 'run_id': str(run_id), 'result_id': str(run['result_id']),
        'person_id': context['person_id'], 'question': context['question'], 'context': context,
        'context_hash': run['context_hash'], 'answer': run['answer'], 'response_hash': run['response_hash'],
        'answer_payload_sha256': digest(run['answer']),
        'status': run['status'], 'model_id': run['model_id'], 'prompt_version': run['prompt_version'],
        'execution_kind': run['execution_kind'], 'retrieved_version_ids': retrieved, 'pool': pool,
        'automatic_audit': audit(run), 'evaluation_scope': 'developer_visible_closed_pool_not_blind',
        'candidate_selection': 'retrieved_versions_then_recent_same_person_and_date_scope_max12',
        'outside_pool_recall': None, 'publication_to_display_verified': False}
    identity = uuid4()
    c.execute(text('INSERT INTO agent_quality_cases(case_id,user_id,run_id,result_id,packet,packet_hash) '
        'VALUES (:id,:u,:r,:result,CAST(:packet AS jsonb),:hash)'),
        {'id': identity, 'u': user, 'r': run_id, 'result': run['result_id'], 'packet': json.dumps(packet, ensure_ascii=False), 'hash': digest(packet)})
    return {'created': True, **case(c, identity, user)}


def latest(c, case_id):
    return [dict(r) for r in c.execute(text('SELECT DISTINCT ON(reviewer) * FROM agent_quality_reviews '
        'WHERE case_id=:id ORDER BY reviewer,revision DESC'), {'id': case_id}).mappings()]


def review(c, actor, case_id, body):
    value = case(c, case_id)
    c.execute(text('SELECT case_id FROM agent_quality_cases WHERE case_id=:id FOR UPDATE'), {'id': case_id})
    body.validate_packet(value['packet'])
    revision = c.execute(text('SELECT coalesce(max(revision),0) FROM agent_quality_reviews WHERE case_id=:id AND reviewer=:r'), {'id': case_id, 'r': actor}).scalar_one()
    if revision != body.expected_revision:
        raise chat.Conflict('你的标注已变化，请先读取自己的最新版本。')
    identity = uuid4()
    c.execute(text('INSERT INTO agent_quality_reviews(review_id,case_id,reviewer,revision,label) '
        'VALUES (:id,:case,:actor,:revision,CAST(:label AS jsonb))'),
        {'id': identity, 'case': case_id, 'actor': actor, 'revision': revision+1, 'label': body.model_dump_json()})
    return {'review_id': identity, 'revision': revision+1, 'model_requests': 0}


def reviewer_case(c, actor, case_id):
    value = case(c, case_id)
    own = [r for r in latest(c, case_id) if r['reviewer'] == actor]
    # Never show another reviewer's judgment before the independent assessment.
    return {'case_id': case_id, 'packet_hash': value['packet_hash'], 'packet': value['packet'],
            'own_review': own[0] if own else None, 'model_requests': 0}


def resolution(c, value):
    rows = latest(c, value['case_id'])
    if not rows: return {'state': 'unreviewed', 'reviewers': 0}
    labels = [Review.model_validate(row['label']) for row in rows]
    for label in labels: label.validate_packet(value['packet'])
    state = 'provisional' if len(rows) == 1 else 'consensus' if len({digest(label.judgment()) for label in labels}) == 1 else 'disputed'
    return {'state': state, 'reviewers': len(rows), 'review_ids': [str(r['review_id']) for r in rows],
            **({'metrics': metrics(value['packet'], labels[0])} if state == 'consensus' else {})}


def report(c, user):
    rows = [dict(r) for r in c.execute(text("""SELECT r.run_id,r.context,r.context_hash,r.prompt_version,r.model_id,
      a.status,a.answer,a.response_hash,a.execution_kind FROM agent_qa_runs r
      JOIN agent_chat_conversations v ON v.conversation_id=r.conversation_id JOIN agent_qa_results a USING(run_id)
      WHERE v.user_id=:u ORDER BY r.created_at DESC,r.run_id DESC LIMIT 101"""), {'u': user}).mappings()]
    audits = [{'run_id': str(r['run_id']), 'prompt_version': r['prompt_version'], 'execution_kind': r['execution_kind'], **audit(r)} for r in rows[:100]]
    feedback_rows = [dict(r) for r in c.execute(text('SELECT DISTINCT ON(run_id) run_id,kind FROM agent_answer_feedback WHERE user_id=:u ORDER BY run_id,created_at DESC,feedback_id DESC'), {'u': user}).mappings()]
    kinds = {}
    for row in feedback_rows: kinds[row['kind']] = kinds.get(row['kind'], 0)+1
    cases = []; real = []; diagnostics = []; by_prompt = {}
    for identity in c.execute(text('SELECT case_id FROM agent_quality_cases WHERE user_id=:u ORDER BY created_at DESC,case_id DESC LIMIT 200'), {'u': user}).scalars():
        try: value = case(c, identity, user)
        except chat.Missing: continue
        resolved = resolution(c, value); packet = value['packet']
        cases.append({'case_id': str(identity), 'run_id': str(value['run_id']), 'packet_hash': value['packet_hash'],
            'prompt_version': packet['prompt_version'], 'candidate_count': len(packet['pool']), **resolved})
        if resolved['state'] == 'consensus':
            (diagnostics if packet['execution_kind'] == 'synthetic_fixture' else real).append(resolved['metrics'])
            if packet['execution_kind'] != 'synthetic_fixture': by_prompt.setdefault(packet['prompt_version'], []).append(resolved['metrics'])
    def aggregate(items):
        keys = ('precision_in_pool', 'recall_in_pool', 'supported', 'person_correct', 'time_correct', 'useful')
        return {'cases': len(items), **{key: {'value': sum(float(x[key]) for x in items if x[key] is not None) / sum(x[key] is not None for x in items) if any(x[key] is not None for x in items) else None,
            'defined_cases': sum(x[key] is not None for x in items)} for key in keys}}
    corpus = dict(c.execute(text("SELECT count(*) AS samples,count(*) FILTER(WHERE origin='source') AS source_samples FROM annotation_samples WHERE split IN ('train','dev')")).mappings().one())
    corpus['review_submissions'] = c.execute(text("SELECT count(*) FROM annotation_labels l JOIN annotation_samples s USING(sample_id) WHERE s.split IN ('train','dev') AND l.provenance='human_verified'")).scalar_one()
    return {'version': VERSION, 'sampled_answers': len(audits), 'answer_window_truncated': len(rows)>100,
        'automatic_checks': {'passed': sum(a['state']=='passed' for a in audits), 'partial': sum(a['state']=='partial' for a in audits), 'failed': sum(a['state']=='failed' for a in audits),
            'no_answer': sum(a['state']=='no_answer' for a in audits), 'items': audits, 'semantic_support': 'not_assessed'},
        'personal_feedback': {'distinct_answers': len(feedback_rows), 'latest_kind_counts': kinds, 'is_ground_truth': False},
        'cases': cases, 'human_consensus_metrics': aggregate(real), 'synthetic_diagnostics': aggregate(diagnostics),
        'by_prompt_version': {key: aggregate(items) for key, items in by_prompt.items()}, 'keyword_corpus_progress': corpus,
        'final_agent_score': None, 'sufficient_for_final_acceptance': False, 'model_requests': 0,
        'limitations': ['Independent people must submit their own judgments; different credentials do not prove physical independence.',
            'Closed pool, not global recall. Reported-date scope is not a historical point-in-time replay.',
            'Prompt groups have different questions; these are descriptive groups, not a controlled model comparison.',
            'User feedback is not a human reference label. Current cases are developer-visible, not an unseen holdout.',
            'True publication-to-display latency and investment performance are not verified; no final score is assigned.']}
