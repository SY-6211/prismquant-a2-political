"""A separate bounded outbox; reading reminders never processes source content."""
import json
from uuid import uuid4
from sqlalchemy import text
from app.keyword_rules import Rule,evaluate
from app.reminder_model import body_hash
from app.repositories import keywords,chat,agent_analysis

ORDER="CASE n.priority WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END"
def lock(c):c.execute(text('SELECT pg_advisory_xact_lock(626184315)'))

def configure(c,rule_id,cooldown=300,dedup=86400,preset_key=None):
    if type(cooldown) is not int or type(dedup) is not int or not 0<=cooldown<=86400 or not max(60,cooldown)<=dedup<=604800:raise ValueError('Invalid reminder windows')
    lock(c);policy=keywords.one(c,rule_id)
    if policy['published_version'] is None:raise ValueError('Publish a previewed rule first')
    rule=Rule.model_validate(keywords.version(c,rule_id,policy['published_version'])['definition'])
    if rule.notification=='none':raise ValueError('Rule has notifications disabled')
    if c.execute(text('SELECT count(*) FROM reminder_channels WHERE enabled AND rule_id<>:id'),{'id':rule_id}).scalar_one()>=20:raise ValueError('At most 20 active reminder rules')
    c.execute(text('INSERT INTO reminder_channels(rule_id,cooldown_seconds,dedup_seconds,preset_key) VALUES (:id,:cooldown,:dedup,:key) ON CONFLICT(rule_id) DO UPDATE SET enabled=true,cooldown_seconds=EXCLUDED.cooldown_seconds,dedup_seconds=EXCLUDED.dedup_seconds'),{'id':rule_id,'cooldown':cooldown,'dedup':dedup,'key':preset_key})
    return {'configured':str(rule_id),'model_requests':0}

def enable(c):
    lock(c);state=c.execute(text('SELECT * FROM reminder_state WHERE singleton FOR UPDATE')).mappings().one()
    if state['enabled'] and not state['pause_reason']:return dict(state)
    if not c.execute(text('SELECT count(*) FROM reminder_channels WHERE enabled')).scalar_one():raise ValueError('Configure a published rule first')
    clock=c.execute(text('SELECT epoch,position FROM content_stream_clock WHERE singleton')).mappings().one()
    c.execute(text('UPDATE reminder_state SET enabled=true,epoch=:epoch,cursor_position=:position,pause_reason=NULL WHERE singleton'),dict(clock))
    return {'enabled':True,'starts_after':clock['position'],'model_requests':0}

def disable(c):
    lock(c);c.execute(text('UPDATE reminder_state SET enabled=false WHERE singleton'));return {'enabled':False}

def transition(c,identity,status,decision,actor,note):
    c.execute(text('UPDATE reminders SET status=:status,revision=revision+1,updated_at=clock_timestamp() WHERE reminder_id=:id'),{'id':identity,'status':status})
    c.execute(text('INSERT INTO reminder_actions(action_id,reminder_id,actor,decision,note) VALUES (:id,:reminder,:actor,:decision,:note)'),{'id':uuid4(),'reminder':identity,'actor':actor,'decision':decision,'note':note})

def supersede(c,rule_id,event_id,version_id):
    rows=c.execute(text("SELECT reminder_id FROM reminders WHERE rule_id=:rule AND event_id=:event AND version_id<>:version AND status IN ('active','pending_review','grouped','duplicate') FOR UPDATE"),{'rule':rule_id,'event':event_id,'version':version_id}).scalars().all()
    for identity in rows:transition(c,identity,'superseded','supersede','content-processor','来源出现新版本，原提醒保留供追溯。')
    return len(rows)

def process(c):
    if not c.execute(text('SELECT pg_try_advisory_xact_lock(626184315)')).scalar_one():return {'status':'busy','created':0,'model_requests':0}
    state=dict(c.execute(text('SELECT * FROM reminder_state WHERE singleton FOR UPDATE')).mappings().one())
    c.execute(text('UPDATE reminder_state SET checked_at=clock_timestamp() WHERE singleton'))
    if not state['enabled'] or state['pause_reason']:return {'status':'disabled_or_paused','created':0,'model_requests':0}
    clock=c.execute(text('SELECT epoch,position FROM content_stream_clock WHERE singleton')).mappings().one()
    changes=c.execute(text('SELECT * FROM content_changes WHERE position>:cursor AND position<=:head ORDER BY position LIMIT 50'),{'cursor':state['cursor_position'],'head':clock['position']}).mappings().all()
    if state['epoch']!=clock['epoch'] or state['cursor_position']>clock['position'] or (not changes and clock['position']>state['cursor_position']) or any(row['position']!=state['cursor_position']+i+1 for i,row in enumerate(changes)):
        c.execute(text("UPDATE reminder_state SET pause_reason='content_stream_gap' WHERE singleton"));return {'status':'paused_gap','created':0,'model_requests':0}
    rules=[]
    for row in c.execute(text('SELECT ch.*,p.published_version,v.definition,v.digest FROM reminder_channels ch JOIN keyword_policies p USING(rule_id) JOIN keyword_versions v ON v.version_id=p.published_version WHERE ch.enabled ORDER BY ch.rule_id LIMIT 21')).mappings():
        rule=Rule.model_validate(row['definition'])
        if rule.digest()!=row['digest']:raise ValueError('Published rule digest differs')
        if rule.notification!='none':rules.append((dict(row),rule))
    if len(rules)>20:raise ValueError('Too many reminder rules')
    if not rules:return {'status':'no_published_channels','created':0,'model_requests':0}
    stats={'created':0,'pending_review':0,'grouped':0,'duplicate':0,'superseded':0,'changes':0};cursor=state['cursor_position'];work=0
    for change in changes:
        row=c.execute(text('SELECT v.*, (SELECT o.version_id FROM event_observations o WHERE o.event_id=v.event_id ORDER BY o.observed_at DESC,o.observation_id DESC LIMIT 1) AS latest_version FROM event_versions v WHERE v.event_id=:event AND v.version_id=:version'),{'event':change['event_id'],'version':change['version_id']}).mappings().one()
        if row['latest_version']!=row['version_id']:cursor=change['position'];continue
        relevant=[(ch,rule) for ch,rule in rules if not rule.person_ids or str(row['person_id']) in {str(p) for p in rule.person_ids}]
        payload=row['payload'];body=payload.get('main_text') or '';cost=min(len(body),100001)*len(relevant)
        if work and work+cost>2000000:break
        work+=cost;cursor=change['position'];stats['changes']+=1
        reserved=agent_analysis.context_has_reserved(c,{'sources':[{'event_id':str(row['event_id']),'version_id':str(row['version_id'])}]})
        for channel,rule in relevant:
            stats['superseded']+=supersede(c,channel['rule_id'],row['event_id'],row['version_id'])
            if row['review_status']!='accepted' or row['person_id'] is None or reserved or payload.get('post_type') not in ('original','speech'):continue
            if c.execute(text('SELECT 1 FROM reminders WHERE rule_id=:rule AND version_id=:version'),{'rule':channel['rule_id'],'version':row['version_id']}).scalar_one_or_none():continue
            result=evaluate(rule,body,row['person_id'])
            if result['status'] not in ('matched','needs_review'):continue
            status='pending_review' if result['status']=='needs_review' or rule.notification=='in_app_after_review' else 'active'
            fingerprint=body_hash(body);parent=None
            if status=='active':
                params={'rule':channel['rule_id'],'person':row['person_id'],'hash':fingerprint,'dedup':channel['dedup_seconds'],'cooldown':channel['cooldown_seconds']}
                duplicate=c.execute(text("SELECT reminder_id FROM reminders WHERE rule_id=:rule AND person_id=:person AND status IN ('active','superseded') AND body_hash=:hash AND created_at>=clock_timestamp()-make_interval(secs=>:dedup) ORDER BY created_at DESC,reminder_id DESC LIMIT 1"),params).mappings().one_or_none()
                cooling=c.execute(text("SELECT reminder_id FROM reminders WHERE rule_id=:rule AND person_id=:person AND status='active' AND created_at>clock_timestamp()-make_interval(secs=>:cooldown) ORDER BY created_at DESC,reminder_id DESC LIMIT 1"),params).mappings().one_or_none()
                if duplicate:status='duplicate';parent=duplicate['reminder_id']
                elif cooling:status='grouped';parent=cooling['reminder_id']
            evidence={'rule_name':rule.name,'topic':rule.topic,'rule_digest':rule.digest(),'engine_result':result,'source_title':payload['title'],'body_preview':body[:600],'preview_truncated':len(body)>600,'original_url':payload.get('original_url'),'retrieved_url':payload['retrieved_url'],'published_at':payload.get('published_at'),'provenance_type':row['provenance_type'],'verification_status':row['verification_status'],'snapshot_hash':row['snapshot_hash'],'claim_support':'not_assessed'}
            inserted=c.execute(text('INSERT INTO reminders(reminder_id,rule_id,rule_version_id,event_id,version_id,person_id,priority,body_hash,status,group_id,evidence) VALUES (:id,:rule,:rule_version,:event,:version,:person,:priority,:hash,:status,:parent,CAST(:evidence AS jsonb)) ON CONFLICT(rule_id,version_id) DO NOTHING'),{'id':uuid4(),'rule':channel['rule_id'],'rule_version':channel['published_version'],'event':row['event_id'],'version':row['version_id'],'person':row['person_id'],'priority':rule.severity,'hash':fingerprint,'status':status,'parent':parent,'evidence':json.dumps(evidence,ensure_ascii=False)})
            if inserted.rowcount:stats['created']+=1;stats[status]=stats.get(status,0)+1
    totals={key:state['stats'].get(key,0)+value for key,value in stats.items()}
    c.execute(text('UPDATE reminder_state SET cursor_position=:cursor,stats=CAST(:stats AS jsonb) WHERE singleton'),{'cursor':cursor,'stats':json.dumps(totals)})
    return {'status':'processed',**stats,'cursor':cursor,'model_requests':0}

def one(c,identity):
    row=c.execute(text('SELECT n.*,p.name_zh,p.name_original,(SELECT count(*) FROM reminders child WHERE child.group_id=n.reminder_id AND child.status IN (\'grouped\',\'duplicate\')) AS grouped_count,(SELECT count(*) FROM reminder_feedback f WHERE f.reminder_id=n.reminder_id) AS feedback_count FROM reminders n JOIN people p USING(person_id) WHERE n.reminder_id=:id'),{'id':identity}).mappings().one_or_none()
    if row is None:raise chat.Missing()
    return dict(row)

def listing(c,person_id=None,status='active',priority=None,limit=30,cursor=None):
    anchor=one(c,cursor) if cursor else None
    rank={'high':3,'medium':2,'low':1}
    rows=c.execute(text(f'SELECT n.reminder_id FROM reminders n WHERE (CAST(:status AS text) IS NULL OR n.status=:status) AND (CAST(:person AS uuid) IS NULL OR n.person_id=:person) AND (CAST(:priority AS text) IS NULL OR n.priority=:priority) AND (CAST(:cursor AS uuid) IS NULL OR ({ORDER},n.created_at,n.reminder_id)<(:rank,CAST(:time AS timestamptz),CAST(:cursor AS uuid))) ORDER BY {ORDER} DESC,n.created_at DESC,n.reminder_id DESC LIMIT :limit'),{'status':status,'person':person_id,'priority':priority,'cursor':cursor,'rank':rank[anchor['priority']] if anchor else 0,'time':anchor['created_at'] if anchor else None,'limit':limit+1}).scalars().all()
    return [one(c,identity) for identity in rows]

def summary(c):
    state=dict(c.execute(text('SELECT * FROM reminder_state WHERE singleton')).mappings().one())
    counts=dict(c.execute(text('SELECT status,count(*) FROM reminders GROUP BY status')).all())
    recent=c.execute(text("SELECT coalesce(checked_at>clock_timestamp()-interval '3 minutes',false) FROM reminder_state WHERE singleton")).scalar_one()
    return {**state,'counts':counts,'processor_active':recent,'configured_rules':c.execute(text('SELECT count(*) FROM reminder_channels WHERE enabled')).scalar_one(),'model_requests':0,'delivery':'in_app_only','read_state':'browser_local'}

def detail(c,identity):
    value=one(c,identity);value['actions']=[dict(row) for row in c.execute(text('SELECT decision,note,created_at FROM reminder_actions WHERE reminder_id=:id ORDER BY created_at,action_id LIMIT 100'),{'id':identity}).mappings()]
    value['grouped_items']=[one(c,row) for row in c.execute(text('SELECT reminder_id FROM reminders WHERE group_id=:id ORDER BY created_at DESC LIMIT 50'),{'id':identity}).scalars()]
    return value

def feedback(c,user_id,body):
    one(c,body.reminder_id)
    row=c.execute(text('INSERT INTO reminder_feedback(feedback_id,reminder_id,user_id,client_request_id,verdict,note) VALUES (:id,:reminder,:user,:request,:verdict,:note) ON CONFLICT(user_id,client_request_id) DO NOTHING RETURNING feedback_id'),{'id':uuid4(),'reminder':body.reminder_id,'user':user_id,'request':body.client_request_id,'verdict':body.verdict,'note':body.note}).scalar_one_or_none()
    saved=c.execute(text('SELECT * FROM reminder_feedback WHERE user_id=:user AND client_request_id=:request'),{'user':user_id,'request':body.client_request_id}).mappings().one()
    if saved['reminder_id']!=body.reminder_id or saved['verdict']!=body.verdict or saved['note']!=body.note:raise chat.Conflict('相同反馈标识已用于其他内容。')
    return {'feedback_id':saved['feedback_id'],'created':row is not None,'status':'submitted_for_review','model_requests':0}

def review(c,identity,decision,note,expected_revision,actor='local-reviewer'):
    if decision not in ('approve','false_positive','restore') or not note.strip() or len(note)>500:raise ValueError('Invalid correction')
    lock(c);row=c.execute(text('SELECT * FROM reminders WHERE reminder_id=:id FOR UPDATE'),{'id':identity}).mappings().one_or_none()
    if row is None:raise chat.Missing()
    if row['revision']!=expected_revision:raise chat.Conflict('提醒状态已改变，请重新读取。')
    if decision!='false_positive':
        expected='pending_review' if decision=='approve' else 'corrected'
        if row['status']!=expected:raise chat.Conflict('此提醒不适用于该审核操作。')
        latest=c.execute(text('SELECT version_id FROM event_observations WHERE event_id=:event ORDER BY observed_at DESC,observation_id DESC LIMIT 1'),{'event':row['event_id']}).scalar_one()
        if latest!=row['version_id']:raise chat.Conflict('来源已有新版本，不能恢复旧提醒。')
    transition(c,identity,'corrected' if decision=='false_positive' else 'active',decision,actor,note.strip())
    return one(c,identity)
