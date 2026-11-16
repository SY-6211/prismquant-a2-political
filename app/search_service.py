"""Prepare local SearXNG deployment files; never install software or start containers implicitly."""
import argparse,json,secrets,shutil
from app.core.config import PROJECT_ROOT


def prepare():
    root=PROJECT_ROOT/'.runtime'/'searxng';root.mkdir(parents=True,exist_ok=True)
    settings=root/'config'/'settings.yml';settings.parent.mkdir(exist_ok=True)
    if not settings.exists():
        settings.write_text('use_default_settings: true\nserver:\n  secret_key: "'+secrets.token_hex(32)+'"\n  limiter: false\n  bind_address: "0.0.0.0"\nsearch:\n  formats:\n    - html\n    - json\n',encoding='utf8')
    compose=root/'compose.json'
    if not compose.exists():
        spec={'services':{'searxng':{'image':'docker.io/searxng/searxng:latest','ports':['127.0.0.1:8888:8080'],
              'volumes':['./config:/etc/searxng','./cache:/var/cache/searxng'],'restart':'unless-stopped'}}}
        compose.write_text(json.dumps(spec,indent=2),encoding='utf8')
    return dict(files_prepared=True,docker_available=bool(shutil.which('docker')),service_started=False,
                endpoint='http://127.0.0.1:8888',model_requests=0,configuration_is_template=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['prepare','status']);a=p.parse_args()
    value=prepare() if a.action=='prepare' else dict(docker_available=bool(shutil.which('docker')),configuration_exists=(PROJECT_ROOT/'.runtime/searxng/compose.json').exists(),service_started_by_this_command=False)
    print(json.dumps(value));return 0


if __name__=='__main__':raise SystemExit(main())
