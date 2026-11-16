"""No network requests; candidates are not scheduler sources or person links."""
import hashlib,json
from uuid import uuid4,uuid5,NAMESPACE_URL
from sqlalchemy import text
from app.source_catalog_model import Review

class Missing(Exception):pass
class Conflict(Exception):pass

def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def import_manifest(c,manifest):
    fingerprint=digest(manifest)
    import_id=uuid5(NAMESPACE_URL,'pq-source-catalog:'+fingerprint)
    inserted=c.execute(text('INSERT INTO source_catalog_imports(import_id,snapshot_hash,manifest) VALUES (:id,:hash,CAST(:value AS jsonb)) ON CONFLICT(snapshot_hash) DO NOTHING RETURNING import_id'),{'id':import_id,'hash':fingerprint,'value':json.dumps(manifest,ensure_ascii=False)}).scalar_one_or_none()
    # Lock the import row to serialize concurrent replays and audit the immutable payload.
    stored=c.execute(text('SELECT manifest FROM source_catalog_imports WHERE import_id=:id FOR UPDATE'),{'id':import_id}).scalar_one()
    if stored!=manifest:raise Conflict('导入快照不匹配。')
    if inserted:
        for entry in manifest['entries']:
            candidate=uuid5(import_id,str(entry['number']))
            c.execute(text('INSERT INTO source_catalog_candidates(candidate_id,import_id,number,name,source_type,original,triage) VALUES (:id,:batch,:number,:name,:type,CAST(:original AS jsonb),CAST(:triage AS jsonb))'),{'id':candidate,'batch':import_id,'number':entry['number'],'name':entry['name'],'type':entry['source_type'],'original':json.dumps(entry['original'],ensure_ascii=False),'triage':json.dumps(entry['triage'],ensure_ascii=False)})
    return {'import_id':str(import_id),'created':bool(inserted),'candidate_count':len(manifest['entries']),'original_label_count':manifest['original_label_count'],'collection_enabled':False}

SELECT="""SELECT c.*,coalesce(r.revision,0) AS revision,r.review,r.actor,r.created_at AS reviewed_at,
coalesce(r.review->>'decision','needs_review') AS decision
FROM source_catalog_candidates c LEFT JOIN LATERAL
(SELECT * FROM source_catalog_reviews r WHERE r.candidate_id=c.candidate_id ORDER BY revision DESC LIMIT 1) r ON true """

def one(c,candidate_id):
    value=c.execute(text(SELECT+'WHERE c.candidate_id=:id'),{'id':candidate_id}).mappings().one_or_none()
    if value is None:raise Missing('候选来源不存在。')
    return dict(value)

def listing(c,limit=20,cursor=None,q=None,source_type=None,decision=None,issue=None,import_id=None):
    current=one(c,cursor) if cursor else None
    term='%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%' if q else None
    sql='SELECT * FROM ('+SELECT+''') v WHERE
    (CAST(:term AS text) IS NULL OR name ILIKE :term ESCAPE '\\' OR original::text ILIKE :term ESCAPE '\\')
    AND (CAST(:type AS text) IS NULL OR source_type=:type)
    AND (CAST(:decision AS text) IS NULL OR decision=:decision)
    AND (CAST(:issue AS text) IS NULL OR triage->'issues' ? :issue)
    AND (CAST(:batch AS uuid) IS NULL OR import_id=:batch)
    AND (CAST(:cursor AS uuid) IS NULL OR (import_id,number,candidate_id)>(CAST(:cbatch AS uuid),:number,CAST(:cursor AS uuid)))
    ORDER BY import_id,number,candidate_id LIMIT :limit'''
    return [dict(v) for v in c.execute(text(sql),{'term':term,'type':source_type,'decision':decision,'issue':issue,'batch':import_id,'cursor':cursor,'cbatch':current['import_id'] if current else None,'number':current['number'] if current else 0,'limit':limit+1}).mappings()]

def overview(c):
    rows=c.execute(text(SELECT)).mappings().all()
    def counts(fn):
        result={}
        for row in rows:
            for key in fn(row):result[key]=result.get(key,0)+1
        return result
    imports=[dict(v) for v in c.execute(text("SELECT import_id,snapshot_hash,created_at,manifest->>'workbook_name' AS workbook_name,manifest->>'workbook_sha256' AS workbook_sha256,manifest->'original_label_count' AS original_label_count FROM source_catalog_imports ORDER BY created_at DESC")).mappings()]
    return {'candidate_count':len(rows),'reviewed_count':sum(v['revision']>0 for v in rows),'types':counts(lambda v:[v['source_type']]),'decisions':counts(lambda v:[v['decision']]),'issues':counts(lambda v:v['triage']['issues']), 'imports':imports,'collection_enabled':False,'notice':'候选目录不等于已接入来源。自动初筛和原表核实标签均不代表本系统已验证入口、权限或更新速度。'}

def review(c,candidate_id,body:Review,actor):
    # Lock a stable candidate row: no lost updates across simultaneous reviewers.
    if not c.execute(text('SELECT candidate_id FROM source_catalog_candidates WHERE candidate_id=:id FOR UPDATE'),{'id':candidate_id}).scalar_one_or_none():raise Missing('候选来源不存在。')
    current=one(c,candidate_id)
    if current['revision']!=body.expected_revision:raise Conflict('审核版本已变化，请重新读取后提交。')
    if not actor.strip() or len(actor)>120:raise ValueError('Invalid reviewer')
    c.execute(text('INSERT INTO source_catalog_reviews(review_id,candidate_id,revision,review,actor) VALUES (:id,:candidate,:revision,CAST(:body AS jsonb),:actor)'),{'id':uuid4(),'candidate':candidate_id,'revision':current['revision']+1,'body':body.model_dump_json(exclude={'expected_revision'}),'actor':actor})
    return one(c,candidate_id)

def history(c,candidate_id,limit=20,before=None):
    one(c,candidate_id)
    return [dict(v) for v in c.execute(text('SELECT * FROM source_catalog_reviews WHERE candidate_id=:id AND (CAST(:before AS integer) IS NULL OR revision<:before) ORDER BY revision DESC LIMIT :limit'),{'id':candidate_id,'before':before,'limit':limit+1}).mappings()]
