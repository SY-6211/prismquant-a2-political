import io
import json
from contextlib import redirect_stderr
from datetime import datetime,timezone
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.ecb_fast import main
from app.ecb_fast_http import parse_index
from app.main import create_app
from app.task_types import normalized_payload,payload_key
from test_ecb_observer import HEADER,OLD,NEW,chunk


def metadata(**changes):
    return {'header':HEADER,'sort':['!pub_timestamp'],'chunk_size':250,'total_records':10,**changes}


class ECBFastContractTests(unittest.TestCase):
    def test_strict_index_schema_order_and_native_url_filter(self):
        entries,coverage = parse_index(json.dumps(metadata()),json.dumps(chunk([NEW,OLD])))
        self.assertEqual(len(entries),2)
        self.assertEqual(coverage['scope'],'first_index_chunk_only')
        self.assertFalse(coverage['publication_time_verified'])
        for meta,values in ((metadata(sort=['pub_timestamp']),chunk([OLD])),
                            (metadata(chunk_size=0),chunk([OLD])),(metadata(),[]),
                            (metadata(header=HEADER+[HEADER[0]]),chunk([OLD])),
                            (metadata(),chunk([OLD])+[1])):
            with self.subTest(meta=meta),self.assertRaises(ValueError):
                parse_index(json.dumps(meta),json.dumps(values))

    def test_unordered_or_non_lagarde_rows_never_become_target_content(self):
        for stamp in (True,float('nan'),None,'2026-11-07'):
            invalid=chunk([OLD]);invalid[0]=stamp
            with self.assertRaises(ValueError):
                parse_index(json.dumps(metadata()),json.dumps(invalid))
        with self.assertRaises(ValueError):
            parse_index('[]',json.dumps(chunk([OLD])))
        values = chunk([NEW,OLD])
        values[0],values[len(HEADER)] = values[len(HEADER)],values[0]
        with self.assertRaises(ValueError):
            parse_index(json.dumps(metadata()),json.dumps(values))
        values = chunk([OLD])
        values[1]='Other Speaker'
        entries,_ = parse_index(json.dumps(metadata()),json.dumps(values))
        self.assertEqual(entries,[])

    def test_new_mode_is_ecb_only_and_old_queue_hash_contract_stays_stable(self):
        old = {'source':'ecb','limit':3}
        value,lane,priority = normalized_payload('collect_source',old)
        self.assertEqual(value,old)
        self.assertEqual(payload_key('collect_source',value,'test'),payload_key('collect_source',old,'test'))
        value,_,_ = normalized_payload('collect_source',{**old,'discovery':'ecb_index'})
        self.assertEqual(value['discovery'],'ecb_index')
        with self.assertRaises(ValueError):
            normalized_payload('collect_source',{'source':'trump','discovery':'ecb_index'})

    def test_cli_requires_explicit_finite_operation_and_review_note(self):
        for args in ([],['--once','--resume-source'],['--resume-source'],['--resume-source','--note',' ','--commit']):
            with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                main(args)


class ECBDiscoveryApiTests(unittest.TestCase):
    def setUp(self):
        self.client=TestClient(create_app(Settings(_env_file=None,environment='test')))
        self.addCleanup(self.client.close)

    @patch('app.api.ecb_discovery.read_query')
    def test_api_pagination_and_invalid_filters_are_bounded_and_readonly(self,reader):
        reader.return_value=[{'url':OLD},{'url':NEW}]
        result=self.client.get('/api/v1/ecb-discovery/candidates?limit=1').json()
        self.assertEqual(result['next_cursor'],OLD)
        reader.reset_mock()
        for tail in ('?limit=0','?limit=101','?status=invalid'):
            self.assertEqual(self.client.get('/api/v1/ecb-discovery/candidates'+tail).status_code,422)
        reader.assert_not_called()
        self.assertEqual(self.client.post('/api/v1/ecb-discovery').status_code,405)

    @patch('app.api.ecb_discovery.read_query',side_effect=OperationalError('private',{},Exception('SECRET')))
    def test_database_error_does_not_leak_configuration(self,reader):
        response=self.client.get('/api/v1/ecb-discovery')
        self.assertEqual(response.status_code,503)
        self.assertNotIn('SECRET',response.text)
