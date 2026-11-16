"""Single-process API with bounded private logs; no development reloader."""
import logging
from logging.handlers import RotatingFileHandler
import uvicorn
from app.core.config import PROJECT_ROOT,Settings


def main():
    folder=PROJECT_ROOT/'.runtime/stack';folder.mkdir(parents=True,exist_ok=True)
    handler=RotatingFileHandler(folder/'api.log',maxBytes=2*1024*1024,backupCount=3,encoding='utf8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    for name in ('uvicorn','uvicorn.error'):
        logger=logging.getLogger(name);logger.handlers=[handler];logger.setLevel(logging.INFO);logger.propagate=False
    settings=Settings()
    uvicorn.run('app.main:app',host=settings.host,port=settings.port,reload=False,workers=1,
                access_log=False,log_config=None,proxy_headers=False)


if __name__=='__main__':main()
