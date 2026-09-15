"""Offline regression tests; all credentials and HTTP responses are synthetic."""
import importlib
import json
import sys
import time
import unittest
from contextlib import ExitStack
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import requests
from flask import Flask

from app.scrapers.aenetworks_tve import _NETWORKS
from app.scrapers.amcn_tve import AMCNetworksTVEScraper, CHANNELS
from app.scrapers.discovery_tve import DiscoveryTVEScraper, SESSION_CACHE_KEY
from app.tve import adobe_pass as adobe


def response(payload=None, *, text='', status=200, url='https://example.test/page', location=None):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = (json.dumps(payload) if payload is not None else text).encode()
    if location:
        result.headers['location'] = location
    return result


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.scraper = DiscoveryTVEScraper(config={})
        self.scraper._cache = {}
        self.session = Mock()
        self.partners = [
            {'id': 'legacy', 'name': 'Optimum', 'flows': [{'external_partner_id': 'Cablevision'}]},
            {'id': 'modern', 'name': 'Optimum TV', 'flows': [{'external_partner_id': 'AlticeOne'}]},
        ]

    def test_exact_alticeone_partner_beats_legacy_name(self):
        self.session.get.return_value = response(self.partners)
        self.assertEqual(self.scraper._discovery_partner_id(self.session, 'device', 'Optimum', 'AlticeOne'), 'modern')

    def test_alticeone_never_falls_back_to_cablevision(self):
        self.session.get.return_value = response(self.partners[:1])
        with self.assertRaisesRegex(adobe.TVEAuthError, 'no exact AlticeOne'):
            self.scraper._discovery_partner_id(self.session, 'device', 'Optimum TV', 'AlticeOne')

    def test_browser_owns_authenticate_redirects_and_saml_form(self):
        target = 'https://api.auth.adobe.com/api/v1/authenticate?mso_id=AlticeOne&reg_code=synthetic-secret'
        self.session.get.side_effect = [response({}), response(self.partners), response({'target_url': target})]
        with self.assertLogs('app.scrapers.discovery_tve', level='INFO') as logs:
            url, page = self.scraper._discovery_session_redirect(self.session, 'device', 'AlticeOne', 'Optimum TV', browser_assisted=True)
        self.assertEqual(url, target)
        self.assertIsNone(page)
        self.assertEqual(self.session.get.call_count, 3)  # no scripted Adobe GET
        self.assertEqual(self.session.get.call_args.kwargs['params']['partner_id'], 'modern')
        self.assertNotIn('synthetic-secret', '\n'.join(logs.output))
        self.assertEqual(self.scraper.cache, {})  # handoff is not authorization

    def test_scripted_cox_redirect_path_is_preserved(self):
        self.session.get.side_effect = [response({}), response({'target_url': 'https://adobe.test/start'}),
            response(status=302, location='https://adobe.test/saml'),
            response(status=302, location='https://login.cox.com/login')]
        url, page = self.scraper._discovery_session_redirect(self.session, 'device', 'Cox', 'Cox')
        self.assertEqual(url, 'https://login.cox.com/login')
        self.assertEqual(page.status_code, 302)

    def test_successful_finish_persists_discovery_session_cookies(self):
        self.session.get.side_effect = [response({'token': 'synthetic-gauth'}), response({})]
        self.session.post.return_value = response({'data': {'attributes': {'token': 'synthetic-login'}}})
        self.session.cookies = requests.cookies.RequestsCookieJar()
        self.session.cookies.set('st', 'synthetic-cookie')
        self.scraper._discovery_finish_login(self.session, 'device', 'synthetic-code')
        self.assertEqual(self.scraper._pending_cache_updates[SESSION_CACHE_KEY]['cookies'], {'st': 'synthetic-cookie'})

    def test_denied_finish_never_caches_session(self):
        self.session.get.side_effect = [response({'token': 'synthetic-gauth'}), response(status=403)]
        self.session.post.return_value = response({})
        with self.assertRaises(adobe.TVEAuthError):
            self.scraper._discovery_finish_login(self.session, 'device', 'code')
        self.assertEqual(self.scraper.cache, {})


class StatementTests(unittest.TestCase):
    def setUp(self):
        adobe._STATEMENT_CACHE.clear()
        self.session = Mock(headers={})

    def tearDown(self):
        adobe._STATEMENT_CACHE.clear()

    def test_attribute_order_nonce_and_nested_config(self):
        data = {'props': {'pageProps': {'config': {'adobeSoftwareStatement': {'history': 'eyJsynthetic-history', 'lifetime': 'eyJsynthetic-lifetime'}}}}}
        self.session.get.return_value = response(text="<script nonce='abc' type='application/json' id='__NEXT_DATA__'>" + json.dumps(data) + '</script>')
        with self.assertLogs('app.tve.adobe_pass', level='INFO') as logs:
            self.assertEqual(adobe.discover_aenetworks_software_statement('history', self.session), 'eyJsynthetic-history')
        self.assertNotIn('eyJsynthetic', '\n'.join(logs.output))

    def test_quoted_and_spaced_bundle_mapping(self):
        for mapping in ['adobeSoftwareStatement = { history: "eyJsynthetic" }', '"adobeSoftwareStatement": {"history": "eyJsynthetic"}', "adobeSoftwareStatement: {'history': 'eyJsynthetic'}"]:
            with self.subTest(mapping=mapping):
                self.assertEqual(adobe._extract_adobe_statement(mapping, 'history'), 'eyJsynthetic')

    def test_cannot_cross_map_boundary_or_choose_other_brand(self):
        self.assertIsNone(adobe._extract_adobe_statement('adobeSoftwareStatement={lifetime:"eyJother"}; x={history:"eyJwrong"}', 'history'))

    def test_fallback_can_reach_lifetime_after_history_and_aetv_fail(self):
        self.session.get.side_effect = [response(status=503), response(text='<html></html>'), response(text='<script>config={adobeSoftwareStatement:{history:"eyJhistory"}}</script>')]
        self.assertEqual(adobe.discover_aenetworks_software_statement('history', self.session), 'eyJhistory')
        self.assertEqual(self.session.get.call_args.args[0], adobe.AENETWORKS_LIVE_PAGES['lifetime'])

    def test_bundle_http_error_is_not_used_as_statement(self):
        self.session.get.side_effect = [response(text='<script src="/_next/static/app.js"></script>'),
            response(text='adobeSoftwareStatement={history:"eyJerror-body"}', status=403),
            response(text='<script>adobeSoftwareStatement={history:"eyJvalid"}</script>')]
        self.assertEqual(adobe.discover_aenetworks_software_statement('history', self.session), 'eyJvalid')

    def test_failed_discovery_does_not_cache_wrong_brand(self):
        self.session.get.return_value = response(text='<script>adobeSoftwareStatement={lifetime:"eyJlifetime"}</script>')
        with self.assertRaisesRegex(adobe.TVEAuthError, 'Could not discover'):
            adobe.discover_aenetworks_software_statement('history', self.session)
        self.assertEqual(adobe._STATEMENT_CACHE, {})

    def test_invalidation_fetches_fresh_statement(self):
        self.session.get.return_value = response(text='<script>adobeSoftwareStatement={history:"eyJfirst"}</script>')
        adobe.discover_aenetworks_software_statement('history', self.session)
        adobe.discover_aenetworks_software_statement('history', self.session)
        self.assertEqual(self.session.get.call_count, 1)
        adobe.invalidate_aenetworks_software_statement('history')
        self.session.get.return_value = response(text='<script>adobeSoftwareStatement={history:"eyJsecond"}</script>')
        self.assertEqual(adobe.discover_aenetworks_software_statement('history', self.session), 'eyJsecond')


class HistoryAuthorizationTests(unittest.TestCase):
    def test_correct_brand_resource_and_real_denial_are_preserved(self):
        for brand in ('history', 'lifetime', 'aetv', 'fyi'):
            for status in (200, 403):
                with self.subTest(brand=brand, status=status):
                    network = _NETWORKS[brand]
                    client = adobe.AdobePassCoxClient(requestor_id=network.requestor_id, resource=network.resource, software_statement='synthetic')
                    client.ctx.authn_token = ''.join(f'<{tag}>{value}</{tag}>' for tag, value in {
                        'simpleTokenMsoID': 'AlticeOne', 'simpleSamlNameID': 'secret-user',
                        'simpleSamlSessionIndex': 'secret-session', 'simpleTokenAuthenticationGuid': 'secret-guid'}.items())
                    client._post_lenient = Mock(return_value=response(text='<error><notAuthorized/><details>secret-provider-body</details></error>', status=status))
                    client._short_authorize = Mock()
                    with self.assertLogs('app.tve.adobe_pass', level='INFO') as logs:
                        with self.assertRaises(adobe.TVENotAuthorizedError) as caught:
                            client.authorize()
                    sent = client._post_lenient.call_args.kwargs['data']
                    self.assertEqual(sent['requestor_id'], network.requestor_id)
                    self.assertEqual(sent['resource_id'], network.resource)
                    self.assertEqual(sent['mso_id'], 'AlticeOne')
                    client._short_authorize.assert_not_called()
                    self.assertFalse(client.ctx.authz_token)
                    self.assertNotIn('secret-', '\n'.join(logs.output) + str(caught.exception))


class AMCTests(unittest.TestCase):
    def setUp(self):
        self.scraper = AMCNetworksTVEScraper(config={})
        self.scraper._cache = {}
        self.channel = CHANNELS['amc']
        self.session = Mock()
        self.profile = {'profiles': {'AlticeOne': {'attributes': {'userID': {'value': 'synthetic-user'}}}}}

    def test_fresh_browser_session_without_cache_or_authenticate_get(self):
        client = Mock(ctx=SimpleNamespace(access_token='synthetic-bearer'))
        client.session.post.return_value = response({'code': 'synthetic-code', 'url': '/api/v1/authenticate?secret=synthetic'})
        with patch('app.scrapers.amcn_tve.AdobePassCoxClient', return_value=client):
            result = self.scraper._adobe_session_redirect(self.channel, 'statement', 'device', 'AlticeOne', browser_assisted=True)
        client.session.get.assert_not_called()
        self.assertIn('/api/v1/authenticate?', result[2])
        self.assertEqual(client.session.post.call_args.kwargs['data']['mvpd'], 'AlticeOne')
        self.assertEqual(self.scraper.cache, {})

    def test_scripted_cox_path_preserved(self):
        client = Mock(ctx=SimpleNamespace(access_token='synthetic-bearer'))
        client.session.post.return_value = response({'code': 'synthetic-code', 'url': '/authenticate'})
        client.session.get.return_value = response(status=302, location='https://login.cox.com/login')
        with patch('app.scrapers.amcn_tve.AdobePassCoxClient', return_value=client):
            result = self.scraper._adobe_session_redirect(self.channel, 'statement', 'device', 'Cox')
        self.assertEqual(result[2], 'https://login.cox.com/login')

    def test_pending_profile_does_not_authorize(self):
        self.session.get.return_value = response({'profiles': {}})
        with self.assertRaises(adobe.TVEAuthError):
            self.scraper._adobe_decision_finish(self.session, self.channel, 'code', 'AlticeOne', {})
        self.session.post.assert_not_called()

    def test_matching_grant_returns_real_decision_token_and_expiry(self):
        self.session.get.return_value = response(self.profile)
        self.session.post.return_value = response({'decisions': [{'resource': 'AMC', 'authorized': True,
            'token': {'serializedToken': 'synthetic-token', 'notAfter': 1234567890000}}]})
        result = self.scraper._adobe_decision_finish(self.session, self.channel, 'code', 'AlticeOne', {})
        self.assertEqual(result, ('synthetic-token', 'synthetic-user', 1234567890000))

    def test_profile_http_error_does_not_log_code_or_authorize(self):
        self.session.get.return_value = response(status=403, url='https://adobe.test/profiles/code/synthetic-secret')
        with self.assertRaises(adobe.TVEAuthError) as caught:
            self.scraper._adobe_decision_finish(self.session, self.channel, 'synthetic-secret', 'AlticeOne', {})
        self.assertNotIn('synthetic-secret', str(caught.exception))
        self.session.post.assert_not_called()

    def test_explicit_denial_is_entitlement_error(self):
        self.session.get.return_value = response(self.profile)
        self.session.post.return_value = response({'decisions': [{'resource': 'AMC', 'authorized': False}]})
        with self.assertRaises(adobe.TVENotAuthorizedError):
            self.scraper._adobe_decision_finish(self.session, self.channel, 'code', 'AlticeOne', {})
        self.assertEqual(self.session.post.call_args.kwargs['json'], {'resources': ['AMC']})

    def test_missing_wrong_resource_or_token_is_not_a_denial(self):
        self.session.get.return_value = response(self.profile)
        for decision in ([], [{'resource': 'IFC', 'authorized': True, 'token': {'serializedToken': 'wrong'}}], [{'resource': 'AMC', 'authorized': True}]):
            with self.subTest(decision=decision):
                self.session.post.return_value = response({'decisions': decision})
                with self.assertRaises(adobe.TVEAuthError) as caught:
                    self.scraper._adobe_decision_finish(self.session, self.channel, 'code', 'AlticeOne', {})
                self.assertNotIsInstance(caught.exception, adobe.TVENotAuthorizedError)

    def test_cache_is_scoped_to_requestor_and_mvpd(self):
        self.scraper._save_adobe_session_cache(self.channel, 'AlticeOne', 'synthetic-code', 'synthetic-bearer')
        self.scraper._save_adobe_auth_cache(self.channel, 'AlticeOne', 'synthetic-token', 'synthetic-user', int((time.time()+300)*1000))
        self.assertIsNotNone(self.scraper._cached_adobe_session(self.channel, 'AlticeOne'))
        self.assertIsNone(self.scraper._cached_adobe_session(self.channel, 'Cablevision'))
        self.assertIsNone(self.scraper._cached_adobe_session(CHANNELS['ifc'], 'AlticeOne'))
        self.assertIsNone(self.scraper._cached_adobe_auth(CHANNELS['ifc'], 'AlticeOne'))

    def test_cached_session_denial_does_not_restart_authentication(self):
        self.scraper._save_adobe_session_cache(self.channel, 'AlticeOne', 'code', 'bearer')
        self.scraper._adobe_decision_finish = Mock(side_effect=adobe.TVENotAuthorizedError('denied'))
        self.scraper._adobe_session_redirect = Mock()
        with self.assertRaises(adobe.TVENotAuthorizedError):
            self.scraper._adobe_decision_token(self.channel, SimpleNamespace(config={'selected_mso_id': 'AlticeOne'}), 'device')
        self.scraper._adobe_session_redirect.assert_not_called()


class BrowserWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import the actual browser flows without starting the production worker,
        # Redis, scheduler or database. HTTP and browser boundaries are mocked.
        worker = ModuleType('app.worker')
        worker.flask_app = Flask(__name__)
        config_store = ModuleType('app.config_store')
        config_store.persist_source_cache_updates = Mock()
        config_store.persist_source_config_updates = Mock()
        with patch.dict(sys.modules, {'app.worker': worker, 'app.config_store': config_store}):
            cls.amcn = importlib.import_module('app.tve.browser_login.amcn')
            cls.discovery = importlib.import_module('app.tve.browser_login.discovery')

    def _run_amc_browser(self, *, denied=False, stopped=False):
        mod = self.amcn
        scraper = AMCNetworksTVEScraper(config={})
        scraper._cache = {}
        client = Mock(ctx=SimpleNamespace(access_token='synthetic-bearer'))
        scraper._adobe_session_redirect = Mock(return_value=(client, 'synthetic-code', 'https://adobe.test/authenticate', {}, None))
        scraper._adobe_decision_finish = Mock(return_value=('synthetic-token', 'synthetic-user', int((time.time()+300)*1000)))
        if denied:
            scraper._adobe_decision_finish.side_effect = adobe.TVENotAuthorizedError('AMC: denied')
        page = Mock(url='https://idpssoalt.alticeusa.com/login')
        context = Mock(pages=[page])
        browser = Mock()
        browser.return_value.__enter__ = Mock(return_value=context)
        browser.return_value.__exit__ = Mock(return_value=False)
        camoufox = ModuleType('camoufox.sync_api')
        camoufox.Camoufox = browser
        redis = Mock()
        redis.exists.return_value = stopped
        status = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {'camoufox.sync_api': camoufox}))
            stack.enter_context(patch('os.makedirs'))
            for name in ('_prime_google_session', '_relay_input_and_screenshot'):
                stack.enter_context(patch.object(mod, name, return_value=False))
            stack.enter_context(patch.object(mod, '_settle_after_mvpd_navigation', return_value=True))
            stack.enter_context(patch.object(mod, 'persist_source_config_updates'))
            persist = stack.enter_context(patch.object(mod, 'persist_source_cache_updates'))
            stack.enter_context(patch.object(mod, '_record_tve_login_error'))
            mod._run_amcn_browser_assisted_login(redis, status, SimpleNamespace(id=1), SimpleNamespace(config={}, username='', password=''), scraper, 'device', 'AlticeOne', {'amc': CHANNELS['amc']})
        return scraper, page, persist, status

    def test_amc_cold_start_polls_and_persists_only_after_authorization(self):
        scraper, page, persist, status = self._run_amc_browser()
        self.assertTrue(scraper._adobe_session_redirect.call_args.kwargs['browser_assisted'])
        page.goto.assert_called_once_with('https://adobe.test/authenticate', wait_until='domcontentloaded', timeout=30000)
        self.assertIn('adobe_session:AMC', persist.call_args.args[1])
        self.assertIn('adobe_auth:AMC', persist.call_args.args[1])
        self.assertEqual(status.call_args.args[0], 'success')

    def test_amc_denial_does_not_persist_authorized_cache(self):
        scraper, page, persist, status = self._run_amc_browser(denied=True)
        self.assertEqual(scraper._pending_cache_updates, {})
        self.assertEqual(persist.call_args.args[1], {})
        self.assertEqual(status.call_args.args[0], 'error')

    def test_amc_cancelled_before_registration_does_not_authenticate(self):
        scraper, page, persist, status = self._run_amc_browser(stopped=True)
        scraper._adobe_session_redirect.assert_not_called()
        page.goto.assert_not_called()
        self.assertEqual(scraper._pending_cache_updates, {})

    def test_discovery_browser_passes_callback_code_to_real_finish_boundary(self):
        mod = self.discovery
        scraper = DiscoveryTVEScraper(config={})
        scraper._cache = {}
        scraper._discovery_session_redirect = Mock(return_value=('https://adobe.test/authenticate', None))
        scraper._discovery_finish_login = Mock()
        page = Mock(url='https://auth.watch.hgtv.com/gauth-sync#code=synthetic-secret')
        browser = Mock()
        browser.return_value.__enter__ = Mock(return_value=Mock(pages=[page]))
        browser.return_value.__exit__ = Mock(return_value=False)
        camoufox = ModuleType('camoufox.sync_api')
        camoufox.Camoufox = browser
        redis, status = Mock(), Mock()
        redis.exists.return_value = False
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {'camoufox.sync_api': camoufox}))
            stack.enter_context(patch('os.makedirs'))
            for name in ('_prime_google_session', '_relay_input_and_screenshot'):
                stack.enter_context(patch.object(mod, name, return_value=False))
            stack.enter_context(patch.object(mod, '_settle_after_mvpd_navigation', return_value=True))
            stack.enter_context(patch.object(mod, 'persist_source_cache_updates'))
            with self.assertLogs('app.tve.browser_login.discovery', level='INFO') as logs:
                mod._run_discovery_browser_assisted_login(redis, status, SimpleNamespace(id=1), SimpleNamespace(username='', password=''), scraper, 'AlticeOne', 'Optimum TV')
        self.assertTrue(scraper._discovery_session_redirect.call_args.kwargs['browser_assisted'])
        self.assertEqual(scraper._discovery_finish_login.call_args.args[2], 'synthetic-secret')
        self.assertEqual(status.call_args.args[0], 'success')
        self.assertNotIn('synthetic-secret', '\n'.join(logs.output) + str(status.call_args_list))


if __name__ == '__main__':
    unittest.main()
