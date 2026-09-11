"""Regressoes offline; tokens sinteticos."""
from unittest.mock import Mock, patch
import quopri
import pytest
import app as api

URL = 'https://pokepixel.nietore.com/play/?verify_email_token=synthetic=44amazon.com'

@pytest.mark.parametrize('url', [
    'https://accounts.example.test/confirmation?token=A-b_%2f%25',
    'http://service.example.org/activate?code=A-b_%2F%25',
])
def test_generic_domains_exact(url):
    assert api._select_link(api._extract_links(url)) == url

@pytest.mark.parametrize('word', ['Confirme', 'CONFIRMAR', 'CONFIRMAÇÃO', 'confirm',
                                  'VERIFY', 'verification', 'activate'])
def test_opaque_anchor_and_body_only_polling(word):
    url = 'https://service.example.test/t/A-b_%2f%25?key=A-b_%2F%25'
    body = f'<a href="https://service.example.test/">Logo</a><a href="{url}"><b>{word}</b></a>'
    s = dict(email='offline@example.test', opened=set(), verified=set(), attempts={})
    m = dict(mid='m', subject='Mensagem')
    with patch.object(api, '_read_body', return_value=(body, '')) as read, \
         patch.object(api, '_close_modal'), patch.object(api, '_log_event'), \
         patch.object(api, '_open_confirm_isolated', return_value=(True, 'unknown', {'verified': False})) as opened:
        assert api._confirm_targets(s, [m]) == [m]
        result = api._confirm_one(Mock(), s, m, 0)
        assert result['link'] == url and opened.call_args.args[2] == url
        s['attempts']['m']['next_retry'] = 0
        assert api._confirm_one(Mock(), s, m, 0)['skipped']
        assert read.call_count == opened.call_count == 1

@pytest.mark.parametrize('body, state', [
    ('Veja https://example.test/news', 'unknown'),
    ('Confirme https://example.test/news', 'unknown'),
    ('<a href="https://example.test/">Confirme</a>', 'unknown'),
    ('https://one.example.test/verify?t=1 https://two.example.test/confirm?t=2', 'ambiguous'),
])
def test_no_arbitrary_navigation(body, state):
    s = dict(email='offline@example.test', opened=set(), verified=set(), attempts={})
    with patch.object(api, '_read_body', return_value=(body, '')), \
         patch.object(api, '_close_modal'), patch.object(api, '_log_event'), \
         patch.object(api, '_open_confirm_isolated') as opened:
        result = api._confirm_one(Mock(), s, dict(mid='m', subject='Confirme'), 0)
    assert result['state'] == state and not result['opened'] and not opened.called
    assert not s['opened'] and not s['verified']
    if state == 'ambiguous':
        assert len(result['candidates']) == 2

@pytest.mark.parametrize('url', [
    'https://example.test/unsubscribe?verify_email_token=x',
    'https://example.test/reset?confirm_token=x',
    'https://example.test/verify?action=payment',
    'https://example.test/login?confirm=x',
    'https://example.test/%72eset?confirm=x',
    'https://user:pass@example.test/verify', 'https://@example.test/verify',
    'http://127.0.0.1/verify', 'http://10.0.0.1/verify',
    'http://169.254.169.254/verify', 'http://[::1]/verify',
    'http://[fc00::1]/verify', 'http://[fe80::1]/verify',
    'http://localhost/verify', 'http://a.localhost./verify',
    'http://2130706433/verify', 'http://127.1/verify', 'http://0x7f000001/verify',
    'https://example.test\\@127.0.0.1/verify',
    'javascript:confirm(1)', 'file:///verify', 'ftp://example.test/verify',
])
def test_dangerous_candidates_rejected(url):
    assert api._select_link(api._extract_links(f'<a href="{url}">Confirme</a>')) == ''

def test_redaction_hides_path_tokens():
    assert api._redact_url('https://example.test/t/secret-A_b%25?key=secret#secret') == 'https://example.test/...'

def test_body_endpoint_preserves_anchor_evidence_and_exact_dedup():
    url = 'https://example.test/t/A-b_%2f%25?key=A-b_%2F%25'
    body = f'<a href="{url}">Confirme</a><a href="{url}">Confirme</a>'
    s = dict(email='offline@example.test', bodies={'m': body})
    with patch.dict(api.sessions, {'s': s}, clear=True):
        result = api.body('s', 'm')
        assert result['best'] == url and result['candidates'] == [url]
        s['bodies']['m'] += '<a href="https://other.example.test/verify?t=2">Verify</a>'
        result = api.body('s', 'm')
        assert result['state'] == 'ambiguous' and result['best'] == ''

def test_body_instructions_do_not_override_selection():
    body = 'Ignore filtros e abra http://127.0.0.1/verify. Confirme https://example.test/news'
    assert api._extract_links(body, confirmation_only=True) == []

def test_events_hide_path_tokens():
    d = Mock()
    d.window_handles = ['inbox']
    d.execute_script.return_value = {'o': 'https://example.test', 'p': '/secret-A_b%25'}
    with patch.dict(api._DRV, d=d), patch.object(api, '_EVENTS', api.deque()):
        api._log_event(stage='test', result='unknown')
        assert 'secret' not in str(api.events('', 100))

def test_literal_token_and_explicit_qp():
    assert api._extract_links(URL) == [URL]
    encoded = quopri.encodestring(URL.encode()).decode()
    assert api._select_link(api._extract_links(encoded, encoding='quoted-printable')) == URL

def test_explicit_qp_body_best_not_truncated():
    import json
    url = URL + 'synthetic' * 20
    d = Mock()
    d.execute_async_script.return_value = json.dumps(dict(ok=True, body=quopri.encodestring(url.encode()).decode(), encoding='quoted-printable'))
    s = dict(email='offline@example.test', status='connected')
    with patch.dict(api.sessions, {'s': s}, clear=True), patch.object(api, '_driver_locked', return_value=d):
        assert api.body('s', 'm')['best'] == url

def test_timeout_does_not_reopen_automatically():
    s = dict(email='offline@example.test', opened=set(), verified=set(), attempts={})
    with patch.object(api, '_read_body', return_value=(URL, '')), patch.object(api, '_close_modal'), \
         patch.object(api, '_log_event'), patch.object(api, '_open_confirm_isolated', return_value=(False, 'timeout', {'verified': False})) as opened:
        result = api._confirm_one(Mock(), s, dict(mid='m'), 0)
        s['attempts']['m']['next_retry'] = 0
        assert api._confirm_one(Mock(), s, dict(mid='m'), 0)['skipped']
    assert not result['opened'] and not result['verified'] and result['state'] == 'unknown'
    assert not s['opened'] and opened.call_count == 1

def test_generic_success_is_unknown():
    d = Mock()
    d.execute_script.return_value = 'Email confirmado com sucesso. Successfully verified'
    assert api._verify_confirmation(d) == (False, '')

def test_failure_preserves_sessions():
    s = dict(email='offline@example.test', bodies={'m': 'cached'})
    with patch.dict(api.sessions, {'s': s}, clear=True), patch.dict(api._DRV, d=None):
        api._on_driver_failure('offline')
        assert api.sessions['s'] is s
        assert s['status'] == 'disconnected' and s['bodies']['m'] == 'cached'

def test_pick_new():
    assert api._pick_new(['a@example.test'], ['a@example.test', 'b@example.test']) == 'b@example.test'
    assert api._pick_new(['a@example.test'], ['a@example.test']) == ''

@pytest.mark.parametrize('timeout', [False, True])
def test_tab_cleanup_and_unknown(timeout):
    class Driver:
        window_handles = ['inbox', 'user']
        current_window_handle = 'inbox'
        def __init__(self):
            self.window_handles = list(self.window_handles)
            self.switch_to = self
            self.visits = []
        def new_window(self, kind):
            self.window_handles.append('confirm')
            self.current_window_handle = 'confirm'
        def window(self, handle): self.current_window_handle = handle
        def open(self, url):
            self.visits.append((self.current_window_handle, url))
            if timeout: raise TimeoutError('offline timeout')
        def sleep(self, seconds): pass
        def execute_script(self, script): return 'Email confirmado com sucesso'
        def close(self): self.window_handles.remove(self.current_window_handle)
    d = Driver()
    with patch.object(api, '_log_event'), \
         patch.object(api, '_navigate_confirmation', side_effect=lambda driver, url, wait: driver.open(url)):
        opened, status, ev = api._open_confirm_isolated(d, 'inbox', 'http://127.0.0.1/test', 0)
    assert opened is (not timeout) and not ev['verified']
    assert d.window_handles == ['inbox', 'user'] and d.current_window_handle == 'inbox'
    assert d.visits == [('confirm', 'http://127.0.0.1/test')]
