"""Local, database-independent helpers for ECB observation evidence."""
import json
import os
from pathlib import Path

LOCK_PATH = Path(__file__).resolve().parents[1] / '.runtime/ecb-observer.lock'


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
    temporary.replace(path)


def read_json(path, limit=8*1024*1024):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError('Evidence file exceeds limit')
    result = json.loads(path.read_text(encoding='utf8'))
    if not isinstance(result,dict):
        raise ValueError('Expected an evidence object')
    return result


class ObserverLock:
    """One observer per checkout, also shared by the finite legacy command."""
    def __init__(self,path=LOCK_PATH):
        self.path=Path(path); self.file=None

    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        handle=self.path.open('a+b')
        if not self.path.stat().st_size:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.file=handle
        except OSError:
            handle.close()
        return self

    def __exit__(self,*_):
        if self.file:
            self.file.close(); self.file=None


def read_journal(path):
    """Retain a valid prefix after power loss; interior corruption is an error."""
    path=Path(path)
    if not path.exists():return [],False
    if path.stat().st_size>32*1024*1024:raise ValueError('Journal exceeds limit')
    data=path.read_bytes(); lines=data.splitlines(keepends=True); rows=[]; partial=False
    for index,line in enumerate(lines):
        if index==len(lines)-1 and not line.endswith(b'\n'):
            partial=True; break
        row=json.loads(line)
        if not isinstance(row,dict):raise ValueError('Invalid journal record')
        rows.append(row)
    return rows,partial
