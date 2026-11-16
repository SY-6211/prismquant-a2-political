"""Read-only local delivery checks. Never launch collectors or paid generation."""
import argparse,importlib.metadata,json
from datetime import datetime,timezone
from pathlib import Path
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from app.core.config import PROJECT_ROOT,Settings
from app.core.database import create_read_engine


def inspect(settings):
    expected=ScriptDirectory.from_config(Config(str(PROJECT_ROOT/'alembic.ini'))).get_current_head()
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'status':'needs_configuration','expected_migration':expected,
        'checks':{},'model_requests':0,'source_requests':0,'m2':'deferred',
        'acceptance_scope':'standalone_local_delivery','remote_deployment':'not_configured',
        'platform_identity_integration':'not_connected','publication_to_display_verified':False,
        'final_agent_score':None}
    dependencies={}
    for package in ('fastapi','sqlalchemy','alembic','psycopg','trafilatura','onnxruntime','tokenizers'):
        try:dependencies[package]=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:dependencies[package]=None
    result['dependencies']=dependencies
    result['checks']['base_dependencies']=all(dependencies[k] for k in ('fastapi','sqlalchemy','alembic','psycopg'))
    result['checks']['loopback_binding']=settings.host=='127.0.0.1'
    engine=create_read_engine(settings)
    try:
        with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
            revision=c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
            result['migration']=revision;result['checks']['migration']=revision==expected
            result['database']={'status':'ok','public_records':c.execute(text('SELECT count(*) FROM public_records')).scalar_one(),
                'saved_qa_results':c.execute(text('SELECT count(*) FROM agent_qa_results')).scalar_one(),
                'keyword_human_submissions':c.execute(text("SELECT count(*) FROM annotation_labels WHERE provenance='human_verified'")).scalar_one(),
                'qa_human_review_submissions':c.execute(text('SELECT count(*) FROM agent_quality_reviews')).scalar_one()}
            result['checks']['database']=True
    except Exception:
        result['checks']['database']=False;result['database']={'status':'unavailable_or_unmigrated'}
    finally:engine.dispose()
    result['status']='ready_with_limits' if all(result['checks'].values()) else 'needs_configuration'
    result['limits']=['Local Windows deployment only; no remote server or platform code provided.',
        'Paid continuous analysis is not enabled by this check or the startup scripts.',
        'Independent human quality and true publication-to-display latency remain unverified.',
        'Source coverage is limited; candidate catalog entries are not all active collectors.']
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path);args=p.parse_args()
    try:
        if args.out and args.out.exists():raise ValueError('Output exists')
        result=inspect(Settings())
        if args.out:
            with args.out.open('x',encoding='utf8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['status']=='ready_with_limits' else 1
    except Exception:print(json.dumps({'status':'failed','message':'检查配置、数据库迁移及输出路径；未输出凭证。'}));return 1


if __name__=='__main__':raise SystemExit(main())
