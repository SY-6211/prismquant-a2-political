"""Allowlisted, checksum-verifiable source release. Excludes runtime state and secrets."""
import argparse,hashlib,json,re,zipfile
from pathlib import Path
from app.core.config import PROJECT_ROOT

DOCS=('README.md','TEAM_HANDOFF.md','DEPLOYMENT.md','INTEGRATION.md','ACCEPTANCE.md','KNOWN_LIMITS.md','RESEARCH_ANALYSIS.md','RESEARCH_WORKFLOW.md','SOURCE_NAVIGATION.md','SEARCH_CONNECTOR.md','RESEARCH_GAP_FLOW.md','FINANCIAL_AI_SOURCES.md','RESEARCH_SYNTHESIS.md','RESEARCH_FOCUS.md','CHAT_FRONTEND_HANDOFF.md')
ROOT_FILES=(*DOCS,'.env.example','.gitignore','alembic.ini','start.ps1','scheduler.ps1','deploy.ps1','openapi.json','release-acceptance.json')
DIRECTORIES=('app','frontend','migrations','tests')
SECRET=re.compile(rb'(?:sk-or-v1-[a-f0-9]{64}|sk-proj-[A-Za-z0-9_-]{40,}|pqchat_[A-Za-z0-9_-]{43})')


def files(root):
    selected=[root/name for name in ROOT_FILES]
    selected+=list(root.glob('requirements*.txt'))
    for name in DIRECTORIES:
        selected.extend(x for x in (root/name).rglob('*') if x.is_file()
            and '__pycache__' not in x.parts and x.suffix not in ('.pyc','.log','.sqlite3')
            and not (name=='tests' and x.suffix in ('.cjs','.png','.jsonl')))
    unique=sorted(set(selected))
    for path in unique:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):raise ValueError('Release source path invalid')
        if any(part.startswith('.') for part in path.relative_to(root).parts) and path.name not in ('.env.example','.gitignore'):raise ValueError('Hidden data cannot enter release')
    return unique


def build(root,target):
    root=Path(root);target=Path(target)
    if target.exists():raise ValueError('Release already exists')
    manifest={'module':'prismquant-person-monitor','version':'0.1.0','format':1,
        'scope':'standalone_local_delivery','supported_platform':'Windows / Python 3.12 / PostgreSQL 18',
        'contains_database':False,'contains_credentials':False,'remote_deployment_verified':False,
        'm2':'deferred','files':[]}
    content=[]
    for path in files(root):
        data=path.read_bytes();name=path.relative_to(root).as_posix()
        if SECRET.search(data):raise ValueError('Credential-like value in release source')
        if path.name in DOCS and re.search(r'同学|C:\\Users\\xu|C:/Users/xu',data.decode('utf8')):raise ValueError('Personal or informal delivery documentation')
        manifest['files'].append({'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()});content.append((name,data))
    encoded=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
    with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in content:archive.writestr('political-monitor/'+name,data)
        archive.writestr('political-monitor/RELEASE_MANIFEST.json',encoded)
    verify(target)
    return {'status':'verified','files':len(content),'bytes':target.stat().st_size,
        'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'contains_database':False,'contains_credentials':False}


def verify(target):
    with zipfile.ZipFile(target) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)) or any(not n.startswith('political-monitor/') or '..' in Path(n).parts or '\\' in n for n in names):raise ValueError('Unsafe release archive paths')
        manifest=json.loads(archive.read('political-monitor/RELEASE_MANIFEST.json'))
        expected={'political-monitor/'+r['path'] for r in manifest['files']}|{'political-monitor/RELEASE_MANIFEST.json'}
        if set(names)!=expected:raise ValueError('Archive contains unlisted files')
        for item in manifest['files']:
            data=archive.read('political-monitor/'+item['path'])
            if len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']:raise ValueError('Release file hash mismatch')
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('build','verify'));parser.add_argument('--file',required=True,type=Path);args=parser.parse_args()
    try:
        value=build(PROJECT_ROOT,args.file) if args.action=='build' else {'status':'verified','files':len(verify(args.file)['files'])}
        print(json.dumps(value,indent=2));return 0
    except Exception:print(json.dumps({'status':'failed','message':'检查交付文件、白名单、敏感信息和输出路径。'}));return 1


if __name__=='__main__':raise SystemExit(main())
