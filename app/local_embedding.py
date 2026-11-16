"""Pinned local E5 inference. Only the explicit download command uses a network."""
import hashlib,json,math,os,re,time
from pathlib import Path
from uuid import uuid4
from app.core.config import PROJECT_ROOT
from app.annotation_model import digest

MODEL='intfloat/multilingual-e5-small'
REVISION='614241f622f53c4eeff9890bdc4f31cfecc418b3'
DIMENSIONS=384
FILES={
 'onnx/model_qint8_avx512_vnni.onnx':('model.onnx',118346824,'dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88'),
 'tokenizer.json':('tokenizer.json',17082730,'0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39')}
CACHE=PROJECT_ROOT/'.runtime/embedding-models'/REVISION
SPEC={'model':MODEL,'revision':REVISION,'dimensions':DIMENSIONS,'model_sha256':FILES['onnx/model_qint8_avx512_vnni.onnx'][2],'tokenizer_sha256':FILES['tokenizer.json'][2],
      'pooling':'attention_mask_mean','normalization':'l2','query_prefix':'query: ','passage_prefix':'passage: ','max_tokens':512,
      'chunk_version':'unicode_token_bounded_v1','chunk_chars':1000,'chunk_tokens':384,'chunk_overlap_chars':120,'content_role':'main_text'}
PROFILE_ID=digest(SPEC)

def sha256_file(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def verify(cache=CACHE):
    cache=Path(cache)
    for _,(name,size,expected) in FILES.items():
        path=cache/name
        if not path.is_file() or path.stat().st_size!=size or sha256_file(path)!=expected:raise ValueError('Local embedding artifacts missing or different; run explicit download')
    return cache

def download(cache=CACHE):
    import httpx
    cache=Path(cache).resolve();cache.mkdir(parents=True,exist_ok=True);downloaded=0
    with httpx.Client(timeout=httpx.Timeout(60,connect=15),follow_redirects=True,trust_env=False,headers={'User-Agent':'PrismQuant-local-embedding/1.0'}) as client:
        for remote,(name,size,expected) in FILES.items():
            target=cache/name
            if target.exists():
                if target.stat().st_size!=size or sha256_file(target)!=expected:raise ValueError('Existing artifact differs; not overwriting')
                continue
            temporary=cache/(name+'.'+uuid4().hex+'.partial');started=time.monotonic();sha=hashlib.sha256();received=0
            try:
                with client.stream('GET',f'https://huggingface.co/{MODEL}/resolve/{REVISION}/{remote}',params={'download':'true'}) as response:
                    response.raise_for_status()
                    with temporary.open('xb') as file:
                        for chunk in response.iter_bytes(262144):
                            received+=len(chunk)
                            if received>size or time.monotonic()-started>600:raise ValueError('Artifact download exceeds finite bounds')
                            file.write(chunk);sha.update(chunk)
                        file.flush();os.fsync(file.fileno())
                if received!=size or sha.hexdigest()!=expected:raise ValueError('Artifact integrity check failed')
                os.replace(temporary,target);downloaded+=1
                print(json.dumps({'artifact':name,'bytes':size,'sha256_verified':True}),flush=True)
            finally:temporary.unlink(missing_ok=True)
    verify(cache)
    return {'model':MODEL,'revision':REVISION,'files_downloaded':downloaded,'paid_api_calls':0}

class LocalEmbedding:
    """No hub client, remote code, truncation, paid fallback or source uploads."""
    def __init__(self,cache=CACHE):
        path=verify(cache)
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np=np;self.tokenizer=Tokenizer.from_file(str(path/'tokenizer.json'))
        self.tokenizer.no_truncation();self.tokenizer.enable_padding(pad_id=1,pad_token='<pad>')
        options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
        self.session=ort.InferenceSession(str(path/'model.onnx'),sess_options=options,providers=['CPUExecutionProvider'])
        self.inputs={value.name for value in self.session.get_inputs()}
        if not self.inputs.issubset({'input_ids','attention_mask','token_type_ids'}) or not {'input_ids','attention_mask'}.issubset(self.inputs):raise ValueError('Unexpected local embedding inputs')
        self.tokenizer.no_padding()
    def token_count(self,value,role='passage'):
        if role not in ('query','passage'):raise ValueError('Invalid embedding role')
        return len(self.tokenizer.encode(SPEC[role+'_prefix']+value).ids)
    def embed(self,texts,role='passage'):
        if role not in ('query','passage') or not 1<=len(texts)<=8:raise ValueError('Embedding batch must contain 1..8 texts')
        if any(not isinstance(value,str) or not value.strip() or len(value)>2000 for value in texts):raise ValueError('Invalid embedding text')
        encoded=[self.tokenizer.encode(SPEC[role+'_prefix']+value) for value in texts]
        if any(len(value.ids)>SPEC['max_tokens'] for value in encoded):raise ValueError('Text exceeds tokenizer bound; no silent truncation')
        np=self.np;length=max(len(value.ids) for value in encoded)
        ids=np.array([value.ids+[1]*(length-len(value.ids)) for value in encoded],dtype=np.int64)
        mask=np.array([[1]*len(value.ids)+[0]*(length-len(value.ids)) for value in encoded],dtype=np.int64)
        inputs={'input_ids':ids,'attention_mask':mask}
        if 'token_type_ids' in self.inputs:inputs['token_type_ids']=np.zeros_like(ids)
        hidden=self.session.run(None,inputs)[0]
        if hidden.ndim!=3 or hidden.shape[:2]!=ids.shape or hidden.shape[2]!=DIMENSIONS:raise ValueError('Unexpected local embedding output')
        masked=hidden.astype(np.float64)*mask[:,:,None];pooled=masked.sum(axis=1)/mask.sum(axis=1)[:,None];norms=np.linalg.norm(pooled,axis=1)
        if not np.isfinite(pooled).all() or not np.isfinite(norms).all() or (norms<1e-10).any():raise ValueError('Non-finite or zero embedding')
        return (pooled/norms[:,None]).astype(np.float32).tolist()

def chunks(body,token_count):
    if not isinstance(body,str) or not body.strip() or len(body)>100000:raise ValueError('Main text must contain 1..100000 characters')
    start=0;result=[]
    while start<len(body):
        end=min(len(body),start+SPEC['chunk_chars'])
        if token_count(body[start:end])>SPEC['chunk_tokens']:
            low=start+1;high=end
            while low<high:
                middle=(low+high+1)//2
                if token_count(body[start:middle])<=SPEC['chunk_tokens']:low=middle
                else:high=middle-1
            end=low
        while end>start and token_count(body[start:end])>SPEC['chunk_tokens']:end-=1
        if end<=start:raise ValueError('Cannot split within tokenizer bound')
        if end<len(body):
            matches=list(re.finditer(r'\n\n|[。！？.!?]\s',body[start:end]))
            boundary=matches[-1].end() if matches else 0
            if boundary>(end-start)*.6:end=start+boundary
        value=body[start:end];tokens=token_count(value)
        if tokens>SPEC['chunk_tokens']:raise ValueError('Chunk exceeds tokenizer bound')
        result.append({'start':start,'end':end,'text':value,'tokens':tokens,'text_sha256':hashlib.sha256(value.encode()).hexdigest()})
        if len(result)>2000:raise ValueError('Too many chunks')
        if end==len(body):break
        start=end-min(SPEC['chunk_overlap_chars'],(end-start)//5)
    return result

def normalized(vector):
    if not isinstance(vector,(list,tuple)) or len(vector)!=DIMENSIONS or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector):raise ValueError('Vector dimensions or values invalid')
    norm=math.sqrt(sum(v*v for v in vector))
    if not .999<=norm<=1.001:raise ValueError('Expected normalized vector')
    return [float(v) for v in vector]
