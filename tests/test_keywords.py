import unittest
from uuid import uuid4
from unittest.mock import patch
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.keyword_rules import Rule,evaluate
from app.core.config import Settings
from app.main import create_app


class KeywordTests(unittest.TestCase):
    def rule(self,**kwargs):return Rule(name='Tariffs',topic='trade',groups=[['tariff','tariffs','关税']],**kwargs)

    def test_bilingual_and_fullwidth_normalization(self):
        for body in ('ＴＡＲＩＦＦＳ increase','新的关税措施'):
            self.assertEqual(evaluate(self.rule(),body)['status'],'matched')

    def test_ascii_word_boundaries_and_nonmatching_not_irrelevant(self):
        value=evaluate(Rule(name='AI',topic='ai',groups=[['AI']]),'said rain')
        self.assertEqual(value['status'],'no_keyword_match');self.assertEqual(value['semantic_relevance'],'not_assessed')

    def test_regex_input_is_literal(self):
        value=Rule(name='literal',topic='test',groups=[['(a+)+$']])
        self.assertEqual(evaluate(value,'aaaaaaa')['status'],'no_keyword_match')

    def test_context_and_exclusion_are_local(self):
        rule=self.rule(context_any=['trade'],exclude_any=['video game'],window=30)
        self.assertEqual(evaluate(rule,'trade tariff')['status'],'matched')
        self.assertEqual(evaluate(rule,'video game trade tariff')['status'],'no_keyword_match')
        self.assertEqual(evaluate(rule,'trade '+'x'*100+' tariff')['status'],'no_keyword_match')

    def test_person_scope(self):
        person=uuid4();rule=self.rule(person_ids=[person])
        self.assertEqual(evaluate(rule,'tariff',uuid4())['status'],'out_of_scope')
        self.assertEqual(evaluate(rule,'tariff',person)['status'],'matched')

    def test_negation_and_quotes_are_review_hints(self):
        for body in ('We will not raise tariffs.','He said “tariffs will rise”.','不增加关税'):
            value=evaluate(self.rule(),body)
            self.assertEqual(value['status'],'needs_review');self.assertFalse(value['notification_sent'])

    def test_all_groups_and_synonyms(self):
        rule=Rule(name='policy',topic='trade',groups=[['tariff','关税'],['China','中国']],match_mode='all')
        self.assertEqual(evaluate(rule,'tariff only')['status'],'no_keyword_match')
        self.assertEqual(evaluate(rule,'中国关税')['status'],'matched')

    def test_long_and_empty_text_are_unassessed(self):
        for body in ('','a'*100001):self.assertEqual(evaluate(self.rule(),body)['status'],'unassessed')

    def test_evidence_is_bounded_and_normalized_offsets_reproduce_term(self):
        body='ＴＡＲＩＦＦ '*40;value=evaluate(self.rule(),body)
        self.assertEqual(len(value['hits']),20);self.assertEqual(value['hits_omitted'],20)
        self.assertEqual(value['hits'][0]['normalized_start'],0)

    def test_invalid_rules_rejected(self):
        for groups in ([[]],[['a']*31],[['AI','ａｉ']],[['\n']],[]):
            with self.assertRaises(ValidationError):Rule(name='x',topic='x',groups=groups)
        with self.assertRaises(ValidationError):Rule(name=' ',topic='x',groups=[['x']])

    def test_deterministic_rule_hash(self):
        self.assertEqual(self.rule().digest(),self.rule().digest())

    def test_http_admin_disabled_and_wrong_token_never_touch_database(self):
        for key,status in (('',503),('x'*32,401)):
            with TestClient(create_app(Settings(_env_file=None,keyword_admin_token=key))) as client,patch('app.api.keywords.create_engine') as database:
                self.assertEqual(client.get('/api/v1/keyword-rules').status_code,status)
                self.assertEqual(client.post('/api/v1/keyword-rules',json={'definition':self.rule().model_dump(mode='json'),'note':'test'}).status_code,status)
                database.assert_not_called()

    def test_valid_auth_and_contract_validation(self):
        with TestClient(create_app(Settings(_env_file=None,keyword_admin_token='x'*32))) as client,patch('app.api.keywords.execute',return_value=[]) as execute:
            headers={'Authorization':'Bearer '+'x'*32}
            self.assertEqual(client.get('/api/v1/keyword-rules',headers=headers).status_code,200)
            self.assertEqual(client.post(f'/api/v1/keyword-rules/{uuid4()}/versions/{uuid4()}/preview',headers=headers,json={'limit':201}).status_code,422)
            self.assertEqual(execute.call_count,1)

    def test_short_admin_token_rejected(self):
        with self.assertRaises(ValidationError):Settings(_env_file=None,keyword_admin_token='short')
