"""Shared host pacing for ECB collectors already protected by SourceLease."""
import time
from sqlalchemy import text
from app.batch_ecb import SOURCE_ID
from app.repositories.ecb_discovery import ensure_state


def pace(conn, require, *, sleep=time.sleep):
    while True:
        with conn.begin():
            require()
            ensure_state(conn)
            wait = conn.execute(text('SELECT GREATEST(0,extract(epoch FROM last_host_request_at + '
                'make_interval(secs=>host_delay_seconds)-clock_timestamp())) FROM ecb_discovery_state WHERE source_id=:id'),
                {'id':SOURCE_ID}).scalar_one() or 0
            if wait<=0:
                conn.execute(text('UPDATE ecb_discovery_state SET last_host_request_at=clock_timestamp() WHERE source_id=:id'),{'id':SOURCE_ID})
                return
        sleep(min(1.0,float(wait)))
