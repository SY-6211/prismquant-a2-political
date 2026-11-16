import hashlib
import json
from uuid import uuid4
from sqlalchemy import text
from app.keyword_rules import Rule,evaluate,ENGINE_VERSION


class Conflict(ValueError):pass
class Missing(ValueError):pass


def saved_preview(conn,rule_id,preview_id):
    row=conn.execute(text('SELECT p.* FROM keyword_previews p JOIN keyword_versions v USING(version_id) '
        'WHERE v.rule_id=:r AND p.preview_id=:p'),{'r':rule_id,'p':preview_id}).mappings().one_or_none()
    if row is None:raise Missing('Preview not found for this rule')
    return dict(row)


def publications(conn,rule_id):
    one(conn,rule_id)
    rows=[dict(r) for r in conn.execute(text('SELECT * FROM keyword_publications WHERE rule_id=:r '
        'ORDER BY created_at DESC,publication_id DESC LIMIT 101'),{'r':rule_id}).mappings()]
    return {'items':rows[:100],'truncated':len(rows)>100}


def one(conn,rule_id,lock=False):
    row=conn.execute(text('SELECT * FROM keyword_policies WHERE rule_id=:id'+(' FOR UPDATE' if lock else '')),{'id':rule_id}).mappings().one_or_none()
    if row is None:raise Missing('Rule not found')
    return dict(row)


def version(conn,rule_id,version_id):
    row=conn.execute(text('SELECT * FROM keyword_versions WHERE rule_id=:r AND version_id=:v'),{'r':rule_id,'v':version_id}).mappings().one_or_none()
    if row is None:raise Missing('Version not found for this rule')
    return dict(row)


def listing(conn,limit=20,after=None):
    return [dict(r) for r in conn.execute(text('SELECT * FROM keyword_policies WHERE '
        '(CAST(:after AS uuid) IS NULL OR rule_id>:after) ORDER BY rule_id LIMIT :limit'),{'after':after,'limit':limit+1}).mappings()]


def history(conn,rule_id,before=None,limit=20):
    one(conn,rule_id)
    return [dict(r) for r in conn.execute(text('SELECT * FROM keyword_versions WHERE rule_id=:r '
        'AND (CAST(:before AS integer) IS NULL OR number<:before) ORDER BY number DESC LIMIT :n'),{'r':rule_id,'before':before,'n':limit+1}).mappings()]


def draft(conn,definition,actor,note,rule_id=None,expected=None):
    definition=Rule.model_validate(definition)
    for pid in definition.person_ids:
        if not conn.execute(text('SELECT 1 FROM people WHERE person_id=:p'),{'p':pid}).scalar_one_or_none():raise Missing('Unknown person')
    if rule_id is None:
        rule_id=uuid4();number=1;revision=1
        conn.execute(text('INSERT INTO keyword_policies(rule_id,revision) VALUES (:r,1)'),{'r':rule_id})
    else:
        current=one(conn,rule_id,True)
        if current['revision']!=expected:raise Conflict('Rule changed; reload before saving')
        number=conn.execute(text('SELECT max(number)+1 FROM keyword_versions WHERE rule_id=:r'),{'r':rule_id}).scalar_one()
        revision=current['revision']+1
        conn.execute(text('UPDATE keyword_policies SET revision=:n WHERE rule_id=:r'),{'n':revision,'r':rule_id})
    vid=uuid4()
    conn.execute(text('INSERT INTO keyword_versions(version_id,rule_id,number,definition,digest,note,actor) '
        'VALUES (:v,:r,:n,CAST(:d AS jsonb),:hash,:note,:actor)'),{'v':vid,'r':rule_id,'n':number,'d':definition.model_dump_json(),
        'hash':definition.digest(),'note':note,'actor':actor})
    return {'rule_id':rule_id,'version_id':vid,'number':number,'revision':revision,'published':False}


def preview(conn,rule_id,version_id,actor,limit=100,since=None,until=None):
    value=version(conn,rule_id,version_id);rule=Rule.model_validate(value['definition'])
    # Full saved main text only. Media notes and quoted posts are never concatenated.
    rows=conn.execute(text('SELECT record_id,person_id,body_text,content_hash,published_at FROM public_records '
        'WHERE (CAST(:since AS timestamptz) IS NULL OR published_at>=:since) '
        'AND (CAST(:until AS timestamptz) IS NULL OR published_at<:until) '
        'AND (CAST(:people AS uuid[]) IS NULL OR person_id=ANY(CAST(:people AS uuid[]))) '
        'ORDER BY published_at DESC NULLS LAST,record_id DESC LIMIT :limit'),{'since':since,'until':until,'people':rule.person_ids or None,'limit':limit+1}).mappings().all()
    samples=[];counts={};digest=hashlib.sha256();budget=1000000
    for row in rows[:limit]:
        body_hash=hashlib.sha256(row['body_text'].encode()).hexdigest()
        digest.update((str(row['record_id'])+body_hash).encode())
        result=evaluate(rule,row['body_text'] if len(row['body_text'])<=budget else '',row['person_id'])
        if result['status']!='unassessed':budget-=len(row['body_text'])
        counts[result['status']]=counts.get(result['status'],0)+1
        samples.append({'record_id':str(row['record_id']),'body_sha256':body_hash,'published_at':str(row['published_at']),**result})
    report={'rule_digest':rule.digest(),'engine_version':ENGINE_VERSION,'sample_digest':digest.hexdigest(),
        'sampled':len(samples),'limit':limit,'truncated':len(rows)>limit,'since':str(since) if since else None,
        'until':str(until) if until else None,'counts':counts,'samples':samples,'semantic_relevance':'not_assessed'}
    pid=uuid4()
    conn.execute(text('INSERT INTO keyword_previews(preview_id,version_id,engine_version,report,actor) '
        'VALUES (:p,:v,:e,CAST(:report AS jsonb),:a)'),{'p':pid,'v':version_id,'e':ENGINE_VERSION,'report':json.dumps(report,ensure_ascii=False),'a':actor})
    return {'preview_id':pid,**report}


def publish(conn,rule_id,version_id,preview_id,expected,actor,note):
    current=one(conn,rule_id,True)
    if current['revision']!=expected:raise Conflict('Rule changed; reload before publishing')
    value=version(conn,rule_id,version_id)
    receipt=conn.execute(text("SELECT report FROM keyword_previews WHERE preview_id=:p AND version_id=:v "
        "AND engine_version=:e AND created_at>clock_timestamp()-interval '24 hours'"),{'p':preview_id,'v':version_id,'e':ENGINE_VERSION}).scalar_one_or_none()
    if not receipt or receipt['rule_digest']!=value['digest'] or not any(receipt['counts'].get(key,0) for key in ('matched','needs_review','no_keyword_match')):
        raise Conflict('Preview this exact version on saved records before publishing (receipt expires in 24h)')
    conn.execute(text('UPDATE keyword_policies SET published_version=:v,revision=revision+1 WHERE rule_id=:r'),{'r':rule_id,'v':version_id})
    conn.execute(text('INSERT INTO keyword_publications(publication_id,rule_id,version_id,preview_id,actor,note) '
        'VALUES (:id,:r,:v,:p,:a,:n)'),{'id':uuid4(),'r':rule_id,'v':version_id,'p':preview_id,'a':actor,'n':note})
    return {'rule_id':rule_id,'published_version':version_id,'revision':expected+1,'notification_sent':False}
