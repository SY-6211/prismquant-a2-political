"""Trim an expanded read at paragraph/sentence/word boundaries, retaining the matched span."""
import hashlib,re


def align(source,anchor):
    if not source['start']<=anchor['start']<anchor['end']<=source['end']:raise ValueError('Expanded read lost matching span')
    text=source['text'];left_anchor=anchor['start']-source['start'];right_anchor=anchor['end']-source['start']
    left=0;right=len(text);left_kind=right_kind='window_edge'
    for kind,positions in (
        ('paragraph',[m.end() for m in re.finditer(r'\n+',text)]),
        ('sentence',[m.end() for m in re.finditer(r'[.!?。！？](?:\s+|$)',text)]),
        ('word',[m.end() for m in re.finditer(r'\s+',text)])):
        values=[n for n in positions if 0<n<=left_anchor]
        if values:left=values[0];left_kind=kind;break
    for kind,positions in (
        ('paragraph',[m.start() for m in re.finditer(r'\n+',text)]),
        ('sentence',[m.end() for m in re.finditer(r'[.!?。！？](?:\s+|$)',text)]),
        ('word',[m.start() for m in re.finditer(r'\s+',text)])):
        values=[n for n in positions if right_anchor<=n<len(text)]
        if values:right=values[-1];right_kind=kind;break
    # Source edges are complete and need no trimming.
    if source['start']==0:left=0;left_kind='source_start'
    if source['end']==source.get('total_characters'):right=len(text);right_kind='source_end'
    value={**source,'text':text[left:right],'start':source['start']+left,'end':source['start']+right}
    value['text_sha256']=hashlib.sha256(value['text'].encode()).hexdigest()
    value['truncated']=value['start']>0 or value['end']<source.get('total_characters',value['end'])
    value['context_window']={'matched_start':anchor['start'],'matched_end':anchor['end'],
                             'left_boundary':left_kind,'right_boundary':right_kind,'overlap_is_not_corroboration':True}
    return value
