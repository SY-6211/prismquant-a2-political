"""Finite host-directed research skills. No model planner, shell, URL or writes."""
import re
from app.agent_tools import MAX_CALLS
from app.agent_intent import route,REVISION

VERSION='person_research_plan_v2'


def strategy_for(question,requested='auto'):
    value=route(question,requested)
    return value['strategy'],value['selected_by']


def retrieve(session,question,body,memory):
    routing=route(question['content'],body.research_mode,memory)
    strategy,selected_by=routing['strategy'],routing['selected_by']
    plan={'version':VERSION,'strategy':strategy,'selected_by':selected_by,'routing':routing,'model_planner_calls':0,
          'tool_call_limit':MAX_CALLS,'source_limit':body.limit,'read_only':True,'steps':[],
          'omitted_primary_candidates':0,'extra_reads':0,'stop_reason':'completed'}
    if memory.get('unresolved_followup') and not body.query:
        plan['stop_reason']='clarification_needed'
        return [],{'mode':'memory_clarification','queries':[],'candidate_count':0,'skipped':0,
                   'semantic_vector_search':False,'exhaustive':False,'characters_per_source':0},plan
    def invoke(tool,args,purpose):
        if session.closed or session.calls>=MAX_CALLS:
            plan['stop_reason']='tool_budget';return {'status':'stopped'}
        value=session.invoke(tool,args)
        plan['steps'].append({'number':len(plan['steps'])+1,'tool':tool,'purpose':purpose,
                              'status':value.get('status'),'code':value.get('code')})
        if value.get('status')=='stopped':plan['stop_reason']='tool_budget'
        return value
    page=invoke('hybrid_search',{'person_id':str(question['person_id']),'question':memory['retrieval_hint'],
       'question_truncated':len(question['content'])>500,'query':body.query or '',
       'since':body.since,'until':body.until,'limit':body.limit},'find_relevant_versions')
    if page.get('status')!='ok':raise ValueError('Research retrieval unavailable')
    rows=page['items'];sources=[];skipped=0
    revision=bool(REVISION.search(question['content']))
    primary=min(body.limit,3 if strategy=='evidence' else 4 if strategy=='summary' or revision else 5 if strategy=='research' else 6)
    plan['omitted_primary_candidates']=max(0,len(rows)-primary)
    def read(row,start,length,purpose):
        value=invoke('read_version',{'event_id':str(row['event_id']),'version_id':str(row['version_id']),
            'start':start,'length':length,'research_only':True,'since':body.since,'until':body.until},purpose)
        source=value.get('source')
        if value.get('status')!='ok':return None
        if str(source['event_id'])!=str(row['event_id']) or str(source['version_id'])!=str(row['version_id']):raise ValueError('Research version identity differs')
        if getattr(session.reader,'is_reserved',lambda s:False)(source):return None
        if str(source['person_id'])!=str(question['person_id']):raise ValueError('Research person differs')
        return source
    for row in rows[:primary]:
        source=read(row,row['start_offset'],row['end_offset']-row['start_offset'],'read_matching_segment')
        if source is None:skipped+=1;continue
        if source['snapshot_hash']!=row['snapshot_hash'] or source['text_sha256']!=row['text_sha256'] or source['end']!=row['end_offset']:
            raise ValueError('Research source digest differs')
        sources.append(source)
    originals=list(sources)
    if strategy=='evidence':
        for source in originals[:2]:
            if source.get('total_characters',source['end'])<=len(source['text']):continue
            start=max(0,source['start']-500)
            expanded=read(source,start,2000,'expand_original_context')
            if expanded is None:skipped+=1;continue
            if expanded['snapshot_hash']!=source['snapshot_hash']:raise ValueError('Expanded version differs')
            overlap=source['text'][:max(0,min(source['end'],expanded['end'])-source['start'])]
            if expanded['text'][source['start']-start:source['start']-start+len(overlap)]!=overlap:raise ValueError('Expanded text differs')
            sources[sources.index(source)]=expanded;plan['extra_reads']+=1
    elif strategy=='research':
        from app.source_context import align
        for source in originals[:2]:
            total=source.get('total_characters',source['end'])
            if source['start']==0 and source['end']==total:continue
            start=max(0,source['start']-900)
            expanded=read(source,start,min(3200,total-start),'expand_research_context')
            if expanded is None:skipped+=1;continue
            if expanded['snapshot_hash']!=source['snapshot_hash']:raise ValueError('Expanded research version differs')
            a=source['start']-expanded['start'];b=source['end']-expanded['start']
            if expanded['text'][a:b]!=source['text']:raise ValueError('Expanded research text differs')
            aligned=align(expanded,source)
            session.segments[aligned['source_ref']]=aligned
            sources[sources.index(source)]=aligned;plan['extra_reads']+=1
    elif strategy=='summary' and originals:
        source=originals[0];total=source.get('total_characters',source['end'])
        ranges=[]
        if source['start']>0:ranges.append((0,min(1500,source['start'])))
        if source['end']<total:ranges.append((source['end'],min(1000,total-source['end'])))
        for start,length in ranges[:2]:
            if len(sources)>=body.limit:plan['stop_reason']='source_limit';break
            extra=read(source,start,length,'read_summary_context')
            if extra is None:skipped+=1;continue
            if extra['snapshot_hash']!=source['snapshot_hash']:raise ValueError('Summary version differs')
            sources.append(extra);plan['extra_reads']+=1
    if revision and originals and len(sources)<body.limit and not session.closed and session.calls+2<=MAX_CALLS:
        anchor=originals[0]
        versions=invoke('list_versions',{'event_id':str(anchor['event_id']),'limit':3},'inspect_saved_revisions')
        plan['revision_check']={'saved_versions_returned':len(versions.get('items',[])),'exhaustive':False}
        if versions.get('status')=='ok':
            for row in versions['items']:
                if str(row['version_id'])==str(anchor['version_id']):continue
                start=min(anchor['start'],max(0,(row.get('main_text_characters') or 0)-1500))
                old=read(row,start,1500,'read_saved_revision')
                if old:
                    old['historical_version']=True;sources.append(old);plan['extra_reads']+=1;break
                skipped+=1
                if session.calls>=MAX_CALLS:break
    metadata={**page['retrieval'],'skipped':skipped,'candidate_count':len(rows),'limit':body.limit,
              'characters_per_source':3200 if strategy=='research' else 2000 if strategy=='evidence' else 1500 if strategy=='summary' else 1500 if revision else 1000,
              'includes_historical_versions':any(s.get('historical_version') for s in sources)}
    return sources,metadata,plan
