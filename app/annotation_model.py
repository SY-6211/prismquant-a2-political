"""Corpus contracts, conservative duplicate grouping and reproducible keyword metrics."""
import hashlib
import json
import re
import unicodedata
import zlib
from collections import defaultdict
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

GROUPING_VERSION='event_exact_sketch_v1'
METRIC_VERSION='keyword_baseline_v1'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),default=str).encode()).hexdigest()


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',revalidate_instances='always')


class Evidence(Strict):
    start:int=Field(ge=0)
    end:int=Field(gt=0)
    text:str=Field(min_length=1,max_length=2000)


class Label(Strict):
    person_correct:bool
    relevant:bool|None
    topics:list[str]=Field(default_factory=list,max_length=20)
    importance:Literal['low','medium','high']
    is_new:bool|None
    should_notify:bool|None
    boundary:Literal['main_text','quoted_text','media_description','uncertain']
    needs_review:bool=False
    evidence:list[Evidence]=Field(default_factory=list,max_length=10)
    cases:list[Literal['paraphrase','keyword_false_positive','negation','quote','old_news','namesake','cross_language','media','prompt_injection']]=Field(default_factory=list,max_length=10)
    note:str=Field(min_length=1,max_length=1000)

    @model_validator(mode='after')
    def consistency(self):
        if not self.note.strip():raise ValueError('Explain the label')
        if any(not t.strip() or len(t)>80 for t in self.topics) or len(set(self.topics))!=len(self.topics):raise ValueError('Invalid or duplicate topic')
        if self.relevant and (not self.person_correct or not self.topics or not self.evidence):
            raise ValueError('Positive relevance needs correct person, topics and exact evidence')
        if self.should_notify and (not self.relevant or self.needs_review):raise ValueError('Uncertain/irrelevant content cannot authorize a notification label')
        return self

    def validate_evidence(self,body):
        for item in self.evidence:
            if item.end<=item.start or body[item.start:item.end]!=item.text:
                raise ValueError('Evidence must exactly match the captured main text')

    def judgment(self):
        value=self.model_dump(exclude={'evidence','note','cases'})
        value['topics']=sorted(value['topics'])
        return value


def canonical(body):
    return ''.join(c for c in unicodedata.normalize('NFKC',body).casefold() if c.isalnum())


def sketch(value):
    # Full text is considered; retain a small deterministic sketch, not an early excerpt.
    return set(sorted({zlib.crc32(value[i:i+5].encode()) for i in range(max(0,len(value)-4))})[:128])


def grouped_splits(samples):
    """Union event/lineage/exact/near duplicates BEFORE chronological group splitting."""
    if len(samples)>2000 or sum(len(s['snapshot']['body']) for s in samples)>5000000:
        raise ValueError('Corpus exceeds 2,000 samples or 5,000,000 characters')
    parent=list(range(len(samples)))
    def find(a):
        while parent[a]!=a:parent[a]=parent[parent[a]];a=parent[a]
        return a
    def union(a,b):parent[find(b)]=find(a)
    keys={};sketches=[];near_pairs=0
    for i,sample in enumerate(samples):
        body=canonical(sample['snapshot']['body']);sketches.append(sketch(body))
        identifiers=[('text',hashlib.sha256(body.encode()).hexdigest())]
        if sample.get('event_key'):identifiers.append(('event',sample['event_key']))
        if sample.get('group_hint'):identifiers.append(('manual',sample['group_hint']))
        for key in identifiers:
            if key in keys:union(i,keys[key])
            else:keys[key]=i
    for i,left in enumerate(sketches):
        if len(left)<20:continue
        for j in range(i):
            right=sketches[j]
            if len(right)>=20 and len(left & right)/min(len(left),len(right))>=.8:
                union(i,j);near_pairs+=1
    groups=defaultdict(list)
    for i,sample in enumerate(samples):groups[find(i)].append(sample)
    if len(groups)<3:raise ValueError('Need at least three independent groups for train/dev/blind')
    def stamp(sample):return sample['snapshot'].get('published_at') or ''
    ordered=sorted(groups.values(),key=lambda rows:(max(stamp(s) for s in rows),min(str(s['sample_id']) for s in rows)))
    train_end=max(1,int(len(ordered)*.5));dev_end=min(len(ordered)-1,max(train_end+1,int(len(ordered)*2/3)))
    assignment=[];counts=defaultdict(int)
    for n,rows in enumerate(ordered):
        split='train' if n<train_end else 'dev' if n<dev_end else 'blind'
        group=digest(sorted(str(s['sample_id']) for s in rows))
        for sample in rows:
            assignment.append({'sample_id':str(sample['sample_id']),'group_id':group,'split':split,
                               'body_sha256':sample['snapshot']['body_sha256']})
            counts[split]+=1
    per_person=defaultdict(lambda:defaultdict(int))
    split_for={row['sample_id']:row['split'] for row in assignment}
    for sample in samples:per_person[str(sample['snapshot'].get('person_id'))][split_for[str(sample['sample_id'])]]+=1
    return assignment,{'grouping_version':GROUPING_VERSION,'groups':len(groups),'counts':dict(counts),
        'by_person_split':{key:dict(value) for key,value in per_person.items()},
        'near_duplicate_pairs':near_pairs,'manifest_sha256':digest(sorted(assignment,key=lambda v:v['sample_id'])),
        'near_duplicate_detection':'heuristic; manual lineage/group review still required',
        'time_basis':'group maximum reported publication time; unknown times sort first, not a strict global time cutoff'}


def confusion(rows):
    tp=fp=fn=tn=abstained=0
    for truth,prediction in rows:
        if prediction is None:abstained+=1
        if truth and prediction:tp+=1
        elif truth:fn+=1
        elif prediction:fp+=1
        elif prediction is not None:tn+=1
    precision=tp/(tp+fp) if tp+fp else None
    recall=tp/(tp+fn) if tp+fn else None
    f1=2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None
    return {'tp':tp,'fp':fp,'fn':fn,'tn':tn,'abstained':abstained,'precision':precision,'recall':recall,'f1':f1,
            'support_positive':tp+fn,'samples':len(rows)}


def summarize(items,topics):
    by_person=defaultdict(list);binary=[];per_topic={topic:[] for topic in topics}
    for item in items:
        label=item['label'];predicted=item['predicted_topics'];unknown=item['unassessed']
        pair=(bool(label['relevant']),None if unknown else bool(predicted))
        binary.append(pair);by_person[str(item['person_id'])].append(pair)
        for topic in topics:per_topic[topic].append((topic in label['topics'] and bool(label['relevant']),None if unknown else topic in predicted))
    metrics={topic:confusion(rows) for topic,rows in per_topic.items()}
    defined=[value['f1'] for value in metrics.values() if value['f1'] is not None]
    return {'relevance':confusion(binary),'by_person':{key:confusion(value) for key,value in by_person.items()},
            'by_topic':metrics,'macro_topic_f1':sum(defined)/len(defined) if defined else None,
            'macro_topics_defined':len(defined),'macro_topics_total':len(metrics)}
