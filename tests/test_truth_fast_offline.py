import copy
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import SecretStr

from app.ecb_observation_support import ObserverLock, read_journal
from app.event_model import EventSnapshot, event_id_for
from app.preview_trump_fast import decode_line, main, replay
from app.truth_fast_access import AccessManifest, DataCredentials, readiness
from app.truth_fast_contract import normalize_frame

UID = '900000000000000001'  # Synthetic identity only, never a verified Trump account ID.
RECEIVED = datetime(2026,11,7,10,0,1,tzinfo=timezone.utc)


def frame(**changes):
    return {'platform':'truth','username':'realDonaldTrump','display_name':'SYNTHETIC TEST ACTOR',
            'user_id':UID,'key':'60001','timestamp':'2026-11-07T10:00:00Z','seen_at':'2026-11-07T10:00:00.500Z',
            'text':'SYNTHETIC own words. Not a real public statement.',**changes}


def export(value=None, **changes):
    return {'received_at':RECEIVED.isoformat(),'frame':frame() if value is None else value,**changes}


class FastContractTests(unittest.TestCase):
    def test_native_identity_is_compatible_with_existing_event_key(self):
        value = normalize_frame(frame(),UID,RECEIVED)
        self.assertEqual(value.event_id,event_id_for('truth_social','60001'))
        self.assertEqual(value.verification_status,'provider_reported_unverified')
        self.assertFalse(value.database_saved)
        self.assertFalse(value.publication_latency_verified)
        self.assertTrue(value.eligible_under_existing_content_scope)

    def test_handle_and_stable_account_id_must_both_match(self):
        for changes in ({'username':'someoneElse'}, {'user_id':'900000000000000002'},
                        {'username':'Donald J. Trump'}, {'user_id':int(UID)}):
            row = decode_line(json.dumps(export(frame(**changes))).encode(),UID)
            self.assertEqual(row['code'],'identity_mismatch')
            self.assertNotIn('event',row)

    def test_original_text_is_full_and_quote_authors_are_never_inherited(self):
        raw = frame(text='Own commentary. '*1000,is_quote=True,quoted={
            'key':'60002','text':'Words of another person.','quoted':{'key':'60003','text':'Nested unknown words.'}})
        value = normalize_frame(raw,UID,RECEIVED)
        self.assertEqual(value.main_text,raw['text'])
        self.assertEqual(len(value.quoted_content),2)
        self.assertIsNone(value.quoted_content[0].author)
        self.assertFalse(value.eligible_under_existing_content_scope)

    def test_retruth_text_is_only_quoted_content(self):
        value = normalize_frame(frame(is_retruth=True,retruth_of_id='60002',text='Other original author words.'),UID,RECEIVED)
        self.assertIsNone(value.main_text)
        self.assertEqual(value.quoted_content[0].text,'Other original author words.')
        self.assertIsNone(value.quoted_content[0].author)
        self.assertFalse(value.eligible_under_existing_content_scope)

    def test_media_ownership_unknown_remains_unknown_and_is_not_main_text(self):
        value = normalize_frame(frame(text=None,media=[{'kind':'image','url':'https://example.invalid/image.png',
            'poster_url':'','created_at':''}]),UID,RECEIVED)
        self.assertIsNone(value.main_text)
        self.assertIsNone(value.media_references[0].source)
        self.assertFalse(value.eligible_under_existing_content_scope)

    def test_reference_urls_cannot_inject_credentials_or_scripts(self):
        for url in ('javascript:alert(1)','https://name:secret@example.invalid/a','https://example.invalid/a?key=PRIVATE',''):
            result = decode_line(json.dumps(export(frame(media=[{'kind':'image','url':url}]))).encode(),UID)
            self.assertEqual(result,{'code':'schema_error'})
            self.assertNotIn('PRIVATE',str(result))

    def test_invalid_quote_depth_flags_and_links_fail_closed(self):
        third = {'key':'60004','text':'Third unsupported nesting'}
        variants = [frame(quoted={'key':'60002','text':'Unflagged'}),frame(is_quote=True),
                    frame(is_retruth=True),frame(is_retruth='true',retruth_of_id='60002'),
                    frame(is_quote=True,quoted={'key':'60002','url':'https://truthsocial.com/@other/99999'}),
                    frame(is_quote=True,quoted={'key':'60002','quoted':{'key':'60003','quoted':third}})]
        for raw in variants:
            self.assertEqual(decode_line(json.dumps(export(raw)).encode(),UID)['code'],'schema_error')

    def test_equivalent_timestamps_and_new_provider_receipt_do_not_change_content_hash(self):
        a = normalize_frame(frame(),UID,RECEIVED)
        b = normalize_frame(frame(timestamp='2026-11-07T18:00:00+08:00',seen_at='2026-11-07T10:01:00Z'),UID,RECEIVED)
        self.assertEqual(a.content_hash(),b.content_hash())
        self.assertTrue(b.time_warnings)
        self.assertNotEqual(a.content_hash(),normalize_frame(frame(text='Revised own words.'),UID,RECEIVED).content_hash())

    def test_unknown_time_stays_unknown_and_naive_time_is_invalid(self):
        a = normalize_frame(frame(timestamp=None,seen_at=''),UID,RECEIVED)
        self.assertIsNone(a.reported_published_at)
        self.assertIsNone(a.provider_detected_at)
        self.assertEqual(decode_line(json.dumps(export(frame(timestamp='2026-11-07T10:00:00'))).encode(),UID)['code'],'schema_error')

    def test_new_fields_require_content_review_without_retaining_unknown_values(self):
        a = normalize_frame(frame(unrecognized_content='DO_NOT_LOG_SECRET'),UID,RECEIVED)
        self.assertEqual(a.unknown_field_count,1)
        self.assertFalse(a.eligible_under_existing_content_scope)
        self.assertNotIn('DO_NOT_LOG_SECRET',a.model_dump_json())

    def test_preview_cannot_be_saved_as_an_existing_official_or_archive_snapshot(self):
        a = normalize_frame(frame(),UID,RECEIVED)
        with self.assertRaises(ValueError):
            EventSnapshot.model_validate(a.model_dump())

    def test_duplicate_json_members_nonfinite_values_wrong_platform_and_oversize_are_rejected(self):
        self.assertEqual(decode_line(b'{"frame":{},"frame":{}}',UID)['code'],'schema_error')
        self.assertEqual(decode_line(b'{"frame":NaN}',UID)['code'],'schema_error')
        self.assertEqual(decode_line(json.dumps(export(frame(platform='x'))).encode(),UID)['code'],'unsupported_platform')
        self.assertEqual(decode_line(b'x'*(256*1024+1),UID)['code'],'frame_too_large')


class AccessCheckTests(unittest.TestCase):
    def credentials(self, **values):
        return DataCredentials(_env_file=None,**values)

    def test_default_config_is_incomplete_and_never_live(self):
        result = readiness(AccessManifest(),self.credentials())
        self.assertEqual(result['status'],'incomplete')
        self.assertIn('provider_not_selected',result['missing'])
        self.assertFalse(result['network_requested'])
        self.assertFalse(result['live_enabled'])

    def test_model_key_and_full_websocket_url_are_rejected_without_echo(self):
        for change in ({'rest_key':'sk-or-v1-FAKE_TEST_ONLY'},{'ws_path':'wss://truth.1322.io/private?key=SECRET'}):
            with self.assertRaises(ValueError) as exc:
                self.credentials(**change)
            self.assertNotIn('FAKE_TEST_ONLY',str(exc.exception))
            self.assertNotIn('key=SECRET',str(exc.exception))

    def test_personal_use_insufficient_budget_and_missing_credentials_remain_incomplete(self):
        manifest = AccessManifest(provider='1322',expected_user_id=UID,identity_evidence='Synthetic evidence',
            license_scope='personal',license_reference='Synthetic license',quote_monthly_usd='450',budget_monthly_usd='10',budget_reference='Synthetic cap')
        result = readiness(manifest,self.credentials())
        for code in ('group_use_license_not_documented','budget_or_quote_not_documented','websocket_credential_missing'):
            self.assertIn(code,result['missing'])

    def test_complete_declaration_is_not_verified_access_or_permission(self):
        manifest = AccessManifest(provider='1322',expected_user_id=UID,identity_evidence='Synthetic evidence',
            license_scope='group_internal',license_reference='Synthetic license',quote_monthly_usd='0',budget_monthly_usd='0',budget_reference='Synthetic free trial')
        result = readiness(manifest,self.credentials(rest_key='SYNTHETIC_REST',ws_key='SYNTHETIC_WS',ws_path='test-path'))
        self.assertEqual(result['status'],'configuration_complete_unverified')
        self.assertFalse(result['configuration_is_authorization_proof'])
        self.assertFalse(result['live_enabled'])
        self.assertNotIn('SYNTHETIC_REST',json.dumps(result))
        self.assertNotIn('test-path',json.dumps(result))

    def test_official_truth_api_does_not_reuse_third_party_protocol(self):
        result = readiness(AccessManifest(provider='truth_api'),self.credentials())
        self.assertIn('official_adapter_not_implemented',result['missing'])


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root/'export.jsonl'

    def write(self, rows):
        self.input.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows),encoding='utf-8')

    def run_replay(self, **kwargs):
        return replay(self.input,self.root/'out',UID,synthetic=True,**kwargs)

    def test_duplicate_revisions_and_return_to_old_content_preserve_observations(self):
        self.write([export(),export(),export(frame(text='Changed text.')),export()])
        result = self.run_replay()
        self.assertEqual(result['counts'],{'normalized':1,'duplicate':1,'revision':1,'version_returned':1})
        self.assertEqual(result['content_versions'],2)
        rows,_ = read_journal(Path(result['directory'])/'observations.jsonl')
        self.assertEqual([r['version_number'] for r in rows],[1,1,2,1])

    def test_resumed_replay_does_not_repeat_processed_lines(self):
        self.write([export(),export(frame(key='60002'))])
        first = self.run_replay(max_frames=1)
        second = self.run_replay(resume=first['directory'])
        third = self.run_replay(resume=first['directory'])
        self.assertEqual(first['status'],'partial')
        self.assertEqual(second['processed_this_segment'],1)
        self.assertEqual(third['processed_this_segment'],0)
        self.assertEqual(third['events'],2)

    def test_replay_refuses_changed_input_or_account_identity(self):
        self.write([export()])
        result = self.run_replay()
        with self.assertRaises(ValueError):
            replay(self.input,self.root/'out','900000000000000002',resume=result['directory'],synthetic=True)
        self.write([export(frame(text='Changed input file'))])
        with self.assertRaises(ValueError):
            self.run_replay(resume=result['directory'])

    def test_stale_checkpoint_and_incomplete_journal_tail_recover(self):
        self.write([export(),export(frame(key='60002'))])
        result = self.run_replay(max_frames=1)
        directory = Path(result['directory'])
        (directory/'checkpoint.json').write_text('{"next_line":1}',encoding='utf-8')
        with (directory/'observations.jsonl').open('ab') as stream:
            stream.write(b'{"incomplete":')
        resumed = self.run_replay(resume=directory)
        self.assertTrue(resumed['recovered_partial_journal'])
        self.assertEqual(resumed['processed_total'],2)

    def test_interior_or_valid_json_content_corruption_fails_closed(self):
        self.write([export(),export(frame(key='60002'))])
        result = self.run_replay()
        journal = Path(result['directory'])/'observations.jsonl'
        rows,_ = read_journal(journal)
        rows[0]['event']['main_text'] = 'Tampered with intact JSON'
        journal.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        with self.assertRaises(ValueError):
            self.run_replay(resume=result['directory'])

    def test_gap_does_not_claim_reconnect_restored_coverage(self):
        gap = {'kind':'gap','received_at':'2026-11-07T10:05:00Z','gap_started_at':'2026-11-07T10:01:00Z','gap_ended_at':'2026-11-07T10:05:00Z'}
        self.write([export(),gap,export(frame(key='60002'))])
        result = self.run_replay()
        self.assertEqual(result['counts']['gap'],1)
        self.assertFalse(result['coverage_complete'])
        self.assertFalse(result['publication_latency_verified'])

    def test_same_replay_directory_is_locked(self):
        self.write([export()])
        result = self.run_replay()
        with ObserverLock(Path(result['directory'])/'replay.lock'):
            with self.assertRaises(ValueError):
                self.run_replay(resume=result['directory'])

    def test_invalid_rows_never_echo_raw_secrets_to_stdout(self):
        self.input.write_text('RAW_SECRET_NOT_JSON\n',encoding='utf-8')
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(['replay','--input',str(self.input),'--expected-user-id',UID,'--output',str(self.root/'out'),'--synthetic'])
        self.assertEqual(code,0)
        self.assertNotIn('RAW_SECRET_NOT_JSON',stdout.getvalue())
        result = json.loads(stdout.getvalue())
        self.assertEqual(result['counts']['schema_error'],1)

    def test_offline_replay_and_access_checks_do_not_touch_database_or_network(self):
        self.write([export()])
        with patch('socket.socket',side_effect=AssertionError('No sockets')), \
             patch('app.core.database.database_url',side_effect=AssertionError('No database')):
            result = self.run_replay()
            check = readiness(AccessManifest(),DataCredentials(_env_file=None))
        self.assertFalse(result['network_requested'])
        self.assertFalse(check['network_requested'])

    def test_input_and_segment_bounds_are_enforced(self):
        self.write([export()])
        for value in (0,1001,True):
            with self.assertRaises(ValueError):
                self.run_replay(max_frames=value)
        self.input.write_bytes(b'x'*(8*1024*1024+1))
        with self.assertRaises(ValueError):
            self.run_replay()
