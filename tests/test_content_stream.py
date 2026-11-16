import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.api.content_stream import events, frame, parse_cursor, reset_reason
from app.core.config import Settings
from app.main import create_app

EPOCH = 'a'*32


class StreamTests(unittest.TestCase):
    def test_cursor_validation_limits_and_64bit_precision(self):
        self.assertEqual(parse_cursor(EPOCH+':9223372036854775807'), (EPOCH,9223372036854775807))
        self.assertIsNone(parse_cursor(None))
        for value in ['', EPOCH+':01', EPOCH+':-1', EPOCH+':9223372036854775808', 'z'*32+':1', EPOCH+':1\nid: 99']:
            with self.subTest(value=value), self.assertRaises(HTTPException):parse_cursor(value)

    def test_frame_cannot_inject_sse_newlines(self):
        value=frame('content', {'title':'hello\nid: 999\ndata: evil'})
        self.assertEqual(value.count('\ndata: '),1)
        self.assertEqual(json.loads(value.split('data: ',1)[1])['title'],'hello\nid: 999\ndata: evil')

    def test_reset_boundaries(self):
        batch={'epoch':EPOCH,'head':100,'floor':50}
        self.assertIsNone(reset_reason(batch,(EPOCH,50)))
        self.assertEqual(reset_reason(batch,(EPOCH,49)),'retention_gap')
        self.assertEqual(reset_reason(batch,(EPOCH,101)),'cursor_ahead')
        self.assertEqual(reset_reason(batch,('b'*32,50)),'epoch_changed')

    def test_state_and_pre_header_errors_hide_database_details(self):
        with TestClient(create_app(Settings(_env_file=None))) as client:
            with patch('app.api.content_stream.read_batch',return_value={'epoch':EPOCH,'head':2,'floor':0}):
                self.assertEqual(client.get('/api/v1/content-stream/state').json()['cursor'],EPOCH+':2')
            with patch('app.api.content_stream.read_batch',side_effect=SQLAlchemyError('secret database URL')):
                for path in ['/api/v1/content-stream/state','/api/v1/content-stream']:
                    response=client.get(path);self.assertEqual(response.status_code,503)
                    self.assertNotIn('secret',response.text)
            self.assertEqual(client.get('/api/v1/content-stream?after=invalid').status_code,422)

    def test_native_reconnect_header_takes_precedence_and_no_buffer_headers(self):
        async def finite(*args):yield ': test\n\n'
        with TestClient(create_app(Settings(_env_file=None))) as client, patch('app.api.content_stream.read_batch',return_value={}) as read, patch('app.api.content_stream.events',side_effect=finite):
            response=client.get('/api/v1/content-stream?after='+EPOCH+':1',headers={'Last-Event-ID':EPOCH+':2'})
            self.assertEqual(read.call_args.args[1],(EPOCH,2))
            self.assertEqual(response.headers['x-accel-buffering'],'no')
            self.assertIn('no-store',response.headers['cache-control'])
            self.assertIn('text/event-stream',response.headers['content-type'])

    def test_stream_replays_multiple_pages_and_stops_on_disconnect(self):
        async def run():
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=None)),is_disconnected=AsyncMock(side_effect=[False,False,True]))
            batch={'epoch':EPOCH,'head':2,'floor':0,'items':[{'position':1,'kind':'new'}]}
            next_batch={**batch,'items':[{'position':2,'kind':'revision'}]}
            with patch('app.api.content_stream.read_batch',return_value=next_batch):
                values=[v async for v in events(request,batch,(EPOCH,0),poll_seconds=0)]
            self.assertEqual(sum('event: content' in v for v in values),2)
            self.assertIn('id: '+EPOCH+':2',values[-1])
        asyncio.run(run())

    def test_stream_emits_reset_and_degrades_after_database_failure(self):
        async def run():
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=None)),is_disconnected=AsyncMock(return_value=False))
            batch={'epoch':EPOCH,'head':2,'floor':1,'items':[]}
            with patch('app.api.content_stream.read_batch',side_effect=SQLAlchemyError('private')):
                values=[v async for v in events(request,batch,(EPOCH,0),poll_seconds=0)]
            self.assertIn('event: reset',values[1]);self.assertIn('retention_gap',values[1])
            self.assertIn('event: unavailable',values[-1]);self.assertNotIn('private',''.join(values))
        asyncio.run(run())

    def test_cors_accepts_event_source_reconnect_header(self):
        with TestClient(create_app(Settings(_env_file=None,cors_origins=['http://localhost:5173']))) as client:
            response=client.options('/api/v1/content-stream',headers={'Origin':'http://localhost:5173','Access-Control-Request-Method':'GET','Access-Control-Request-Headers':'Last-Event-ID'})
            self.assertEqual(response.status_code,200)
