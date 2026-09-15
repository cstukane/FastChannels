"""Discovery credential routing and safe generic-autofill diagnostics."""
import importlib
import itertools
import sys
import unittest
from contextlib import ExitStack
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask

from app.scrapers.discovery_tve import DiscoveryTVEScraper


class DiscoveryAutofillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        worker = ModuleType('app.worker')
        worker.flask_app = Flask(__name__)
        store = ModuleType('app.config_store')
        store.persist_source_cache_updates = Mock()
        with patch.dict(sys.modules, {'app.worker': worker, 'app.config_store': store}):
            cls.discovery = importlib.import_module('app.tve.browser_login.discovery')
            cls.common = importlib.import_module('app.tve.browser_login.common')

    def _run_discovery(self, mso_id='AlticeOne', username='synthetic-user', password='synthetic-password', autofill_result=True):
        mod = self.discovery
        scraper = DiscoveryTVEScraper(config={})
        scraper._cache = {}
        target = 'https://adobe.test/authenticate?SAMLRequest=synthetic-secret'
        scraper._discovery_session_redirect = Mock(return_value=(target, None))
        scraper._discovery_finish_login = Mock()
        page = Mock(url='https://auth.watch.hgtv.com/gauth-sync#code=synthetic-code')
        browser = Mock()
        browser.return_value.__enter__ = Mock(return_value=Mock(pages=[page]))
        browser.return_value.__exit__ = Mock(return_value=False)
        camoufox = ModuleType('camoufox.sync_api')
        camoufox.Camoufox = browser
        redis, status = Mock(), Mock()
        redis.exists.return_value = False
        order = []
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {'camoufox.sync_api': camoufox}))
            stack.enter_context(patch('os.makedirs'))
            stack.enter_context(patch.object(mod.time, 'monotonic', side_effect=itertools.count(100)))
            for name in ('_prime_google_session', '_relay_input_and_screenshot', '_harvest_and_save_xfinity_cookies'):
                stack.enter_context(patch.object(mod, name, return_value=False))
            stack.enter_context(patch.object(mod, '_settle_after_mvpd_navigation', side_effect=lambda *a, **k: order.append('settled') or True))
            generic = stack.enter_context(patch.object(mod, '_try_autofill_credentials', side_effect=lambda *a, **k: order.append('generic') or autofill_result))
            xfinity = stack.enter_context(patch.object(mod, '_autofill_xfinity_credentials', return_value=True))
            stack.enter_context(patch.object(mod, 'persist_source_cache_updates'))
            mod._run_discovery_browser_assisted_login(
                redis, status, SimpleNamespace(id=1), SimpleNamespace(username=username, password=password),
                scraper, mso_id, 'Synthetic provider',
            )
        self.assertTrue(scraper._discovery_session_redirect.call_args.kwargs['browser_assisted'])
        self.assertEqual(scraper._discovery_finish_login.call_args.args[2], 'synthetic-code')
        self.assertEqual(status.call_args.args[0], 'success')
        return SimpleNamespace(page=page, generic=generic, xfinity=xfinity, redis=redis, order=order, target=target)

    def test_alticeone_saved_credentials_use_generic_after_settle(self):
        result = self._run_discovery()
        result.generic.assert_called_once_with(
            result.page, 'synthetic-user', 'synthetic-password', r=result.redis,
            stop_key=self.discovery.MVPD_BROWSER_LOGIN_STOP_KEY,
            input_key=self.discovery.MVPD_BROWSER_LOGIN_INPUT_KEY,
            navigation_already_settled=True,
        )
        result.xfinity.assert_not_called()
        self.assertEqual(result.order, ['settled', 'generic'])

    def test_xfinity_keeps_existing_specific_helper(self):
        result = self._run_discovery(mso_id='Comcast_SSO')
        result.xfinity.assert_called_once_with(
            result.page, 'synthetic-user', 'synthetic-password', r=result.redis,
            stop_key=self.discovery.MVPD_BROWSER_LOGIN_STOP_KEY,
            input_key=self.discovery.MVPD_BROWSER_LOGIN_INPUT_KEY,
        )
        result.generic.assert_not_called()

    def test_missing_or_partial_credentials_skip_autofill(self):
        for username, password in [('', ''), ('synthetic-user', ''), ('', 'synthetic-password')]:
            with self.subTest(username_present=bool(username), password_present=bool(password)):
                result = self._run_discovery(username=username, password=password)
                result.generic.assert_not_called()
                result.xfinity.assert_not_called()

    def test_no_matching_login_form_continues_normal_completion_poll(self):
        # False is the helper's best-effort result for SSO/account pickers or
        # captcha-first forms. It must not be interpreted as a login failure.
        result = self._run_discovery(autofill_result=False)
        result.generic.assert_called_once()

    def test_browser_owned_saml_target_is_passed_through_unchanged(self):
        result = self._run_discovery()
        result.page.goto.assert_called_once_with(result.target, wait_until='domcontentloaded', timeout=30000)

    def test_shared_helper_submission_does_not_log_credentials(self):
        page = Mock(url='https://provider.test/login;jsessionid=synthetic-session?SAMLRequest=synthetic-saml')
        password, username = Mock(), Mock()
        password.count.return_value = username.count.return_value = 1
        password.first.input_value.side_effect = ['', 'synthetic-password']
        username.first.input_value.side_effect = ['', 'synthetic-user']
        page.locator.side_effect = lambda selector: password if 'password' in selector else username
        with self.assertLogs(self.common.logger, level='INFO') as logs:
            self.assertTrue(self.common._try_autofill_credentials(page, 'synthetic-user', 'synthetic-password', navigation_already_settled=True))
        password.first.press.assert_called_once_with('Enter')
        self.assertIn('filled and submitted credentials', '\n'.join(logs.output))
        self.assertNotIn('synthetic-', '\n'.join(logs.output))

    def test_shared_helper_browser_exception_does_not_expose_secrets(self):
        page = Mock()
        page.locator.side_effect = RuntimeError('synthetic-password synthetic-saml')
        with self.assertLogs(self.common.logger, level='INFO') as logs:
            self.assertFalse(self.common._try_autofill_credentials(page, 'synthetic-user', 'synthetic-password', navigation_already_settled=True))
        self.assertIn('RuntimeError', '\n'.join(logs.output))
        self.assertNotIn('synthetic-', '\n'.join(logs.output))

    def test_shared_helper_account_picker_does_not_fill_or_log_url(self):
        page = Mock(url='https://provider.test/picker;jsessionid=synthetic-session')
        page.locator.return_value.count.return_value = 0
        with patch.object(self.common.time, 'monotonic', side_effect=itertools.count(100)):
            with self.assertLogs(self.common.logger, level='INFO') as logs:
                self.assertFalse(self.common._try_autofill_credentials(page, 'synthetic-user', 'synthetic-password', wait_seconds=5, navigation_already_settled=True))
        page.locator.return_value.first.press_sequentially.assert_not_called()
        self.assertNotIn('synthetic-', '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
