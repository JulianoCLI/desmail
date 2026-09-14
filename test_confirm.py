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
    def timeout(d, inbox, url, wait, confirmation, attempt, session=None):
        attempt['navigation_attempted'] = True
        return False, 'timeout', {'verified': False, 'reason': 'driver_error'}
    with patch.object(api, '_read_body', return_value=(URL, '')), patch.object(api, '_close_modal'), \
         patch.object(api, '_log_event'), patch.object(api, '_open_confirm_isolated', side_effect=timeout) as opened:
        result = api._confirm_one(Mock(), s, dict(mid='m'), 0)
        s['attempts']['m']['next_retry'] = 0
        # Replay: uma unica tentativa extra apos timeout; sem replay_done, nao ha terceira.
        replay = api._confirm_one(Mock(), s, dict(mid='m'), 0)
        assert not replay.get('skipped') and not replay.get('verified')
        assert s['attempts']['m'].get('replay_done')
        assert api._confirm_one(Mock(), s, dict(mid='m'), 0).get('skipped')
    assert not result['opened'] and not result['verified'] and result['state'] == 'unknown'
    assert not s['opened'] and opened.call_count == 2

def test_generic_success_is_unknown():
    d = Mock()
    d.execute_async_script.return_value = 'Email confirmado com sucesso. Successfully verified'
    assert api._verify_confirmation(d) == (False, '')

@pytest.mark.parametrize('reason', ['success_visible', 'token_expired', 'token_used',
                                  'webgl_unsupported', 'unexpected_origin', 'success_not_observed'])
def test_verification_diagnostics_allowlist(reason):
    d = Mock()
    d.execute_async_script.return_value = reason
    diagnostic = {}
    verified, evidence = api._verify_confirmation(d, diagnostic)
    assert verified is (reason == 'success_visible')
    assert evidence == (api._CONFIRM_SUCCESS if verified else '')
    assert diagnostic['reason'] == reason
    assert diagnostic['started_at'] <= diagnostic['observed_at']
    assert diagnostic['elapsed_ms'] >= 0

def test_verification_diagnostics_discard_untrusted_output_and_errors():
    d = Mock()
    diagnostic = {}
    d.execute_async_script.return_value = {'body': 'private@example.test token=secret'}
    assert api._verify_confirmation(d, diagnostic) == (False, '')
    assert diagnostic['reason'] == 'success_not_observed'
    d.execute_async_script.side_effect = RuntimeError('private@example.test token=secret')
    assert api._verify_confirmation(d, diagnostic) == (False, '')
    assert diagnostic['reason'] == 'driver_error'
    assert 'secret' not in str(diagnostic) and '@' not in str(diagnostic)

@pytest.mark.parametrize('operation', ['remove', 'release', 'auto-remove'])
def test_removal_retains_redacted_diagnostic(operation):
    s = dict(_sid='s', email='private@example.test', auto_confirm=True,
             opened={'m'}, verified=set(), attempts={}, bodies={'m': 'secret'},
             confirmation={'mid': 'm', 'verified': False, 'reason': 'token_expired',
                           'evidence': '', 'observed_at': 1234})
    with patch.dict(api.sessions, {'s': s}, clear=True), patch.dict(api._DRV, d=None), \
         patch.object(api, '_EVENTS', api.deque(maxlen=500)), patch.object(api, '_enqueue_delete'):
        before = api.email_status('s')
        assert not before['monitoring']
        if operation == 'remove':
            api.remove('s')
        elif operation == 'release':
            api.release(api.ReleaseReq(session_id='s'))
        else:
            api._drop('s', 'auto-remove')
        assert 's' not in api.sessions
        event = api.events('s', 100)['events'][-1]
        assert event['confirmation'] == s['confirmation']
        assert event['state'] == before['state'] and event['opened'] and not event['verified']
        assert event['mid'] == 'm' and event['stage'] == operation
        assert 'secret' not in str(event) and 'private@' not in str(event)
        for _ in range(501):
            api._log_event(stage='test')
        assert len(api._EVENTS) == 500
        assert not api.events('s', 500)['events']

def test_monitoring_does_not_forget_terminal_attempt_when_inbox_changes():
    s = dict(auto_confirm=True, attempts={'old': {'state': 'unknown', 'navigation_attempted': True}},
             messages=[{'mid': 'new'}])
    assert not api._monitoring(s)

@pytest.mark.parametrize('reason, observation_s, dwell_s', [
    ('success_visible', 1, 6), ('token_expired', 1, 6),
    ('success_not_observed', 14.5, 6), ('webgl_unsupported', 1, 0)])
def test_observation_before_close_correlated_and_wait_overlaps(reason, observation_s, dwell_s):
    clock = [0.0]
    d = RecoveryDriver({'inbox': (api.URL, True)})
    d.timeouts = Mock(script=47)
    d.set_script_timeout = Mock()
    d.get = lambda url: None
    d.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    def observe(*args):
        assert clock[0] == 0  # No blind sleep before observation.
        clock[0] += observation_s
        return reason
    d.execute_async_script = observe
    s = dict(_sid='s', email='private@example.test', opened=set(), verified=set(), attempts={},
             auto_confirm=True, bodies={'m': URL}, messages=[{'mid': 'm'}])
    close = d.close
    def checked_close():
        assert s['confirmation']['reason'] == reason
        assert s['confirmation']['observed_at'] is not None
        close()
    d.close = checked_close
    with patch.dict(api.sessions, {'s': s}, clear=True), patch.dict(api._DRV, d=d, inbox_handle='inbox'), \
         patch.object(api, '_EVENTS', api.deque(maxlen=500)), patch.object(api, '_validate_destination'), \
         patch.object(api.time, 'sleep', side_effect=d.sleep), \
         patch.object(api.time, 'monotonic', side_effect=lambda: clock[0]):
        result = api.open_message(api.OpenReq(session_id='s', mid='m', wait_s=dwell_s))
        assert clock[0] == max(observation_s, dwell_s)
        assert result['confirmation'] == api.email_status('s')['confirmation']
        assert result['verified'] is (reason == 'success_visible')
        assert not api.email_status('s')['monitoring']
        event = next(e for e in api.events('s', 100)['events'] if e['stage'] == 'tab-verify')
        assert event['mid'] == 'm' and event['confirmation']['reason'] == reason
        assert 'synthetic' not in str(event) and 'private@' not in str(event)
        repeated = api.open_message(api.OpenReq(session_id='s', mid='m'))
        assert repeated['skipped'] and repeated['confirmation'] == result['confirmation']
        api.remove('s')
        retained = api.events('s', 100)['events'][-1]
        assert retained['confirmation'] == result['confirmation']
        assert retained['verified'] == result['verified']

def test_exact_success_and_timeout_restoration():
    d = Mock()
    d.timeouts.script = 47
    d.execute_async_script.return_value = {'reason': 'success_visible', 'href': '', 'title': '', 'status': None, 'sample': []}
    assert api._verify_confirmation(d) == (True, 'E-mail confirmado com sucesso. Sua conta já está liberada.')
    assert [c.args[0] for c in d.set_script_timeout.call_args_list] == [15, 47]
    d.execute_async_script.side_effect = TimeoutError('synthetic')
    assert api._verify_confirmation(d) == (False, '')
    assert d.set_script_timeout.call_args.args == (47,)
    assert not d.get.called and not d.open.called

def test_log_never_touches_driver():
    class Forbidden:
        def __getattribute__(self, name):
            raise AssertionError('driver access: ' + name)
    with patch.dict(api._DRV, d=Forbidden(), inbox_handle='cached'), patch.object(api, '_EVENTS', api.deque()):
        api._log_event(stage='test', elapsed_ms=12)
        event = api.events('', 100)['events'][0]
        assert event['inbox_handle'] == 'cached' and event['elapsed_ms'] == 12

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
        current_url = api.URL
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
        def execute_script(self, script): return 1 if script == 'return 1' else True
        def close(self): self.window_handles.remove(self.current_window_handle)
    d = Driver()
    with patch.object(api, '_log_event'), \
         patch.object(api, '_navigate_confirmation', side_effect=lambda driver, url, wait, attempt: driver.open(url)):
        opened, status, ev = api._open_confirm_isolated(d, 'inbox', 'http://127.0.0.1/test', 0)
    assert opened is (not timeout) and not ev['verified']
    assert d.window_handles == ['inbox', 'user'] and d.current_window_handle == 'inbox'
    assert d.visits == [('confirm', 'http://127.0.0.1/test')]

def test_verification_dom_contract_offline():
    import json
    import subprocess
    script = r'''
const assert = require('node:assert/strict');
const verify = new Function(SCRIPT);
const success = SUCCESS;
let now = 0;
global.performance = {now: () => now, getEntriesByType: () => []};
global.setTimeout = (fn, delay) => { now += delay; fn(); };
global.getComputedStyle = el => el.style;
function run(text, hidden = false, host = 'pokepixel.nietore.com', protocol = 'https:', port = '') {
  now = 0;
  global.location = {hostname: host, protocol, port};
  global.document = {title: '', querySelectorAll: () => {
    const el = {innerText: text, parentElement: null,
      querySelectorAll: () => [],
      style: {display: hidden ? 'none' : 'block', visibility: 'visible', opacity: '1'},
      getClientRects: () => hidden ? [] : [{}]};
    return [el];
  }};
  let result;
  verify(success, 'pokepixel.nietore.com', value => result = value);
  assert(now <= 15000);
  return result;
}
const r1 = run(success);
assert.equal(r1.reason, 'success_visible');
assert.deepEqual(r1.sample, []);
assert.equal(run(success.replace(' Sua', '\n  Sua')).reason, 'success_visible');
assert.equal(run(success, true).reason, 'success_not_observed');
assert.equal(run(success, false, 'pokepixel.nietore.com.evil.test').reason, 'unexpected_origin');
assert.equal(run(success, false, 'pokepixel.nietore.com', 'http:').reason, 'unexpected_origin');
assert.equal(run(success, false, 'pokepixel.nietore.com', 'https:', '444').reason, 'unexpected_origin');
for (const [text, reason] of [['Token expirado.', 'token_expired'],
    ['Token já utilizado.', 'token_used'], ['Token já foi utilizado.', 'token_used'],
    ['Your browser does not support WebGL', 'webgl_unsupported']]) {
  assert.equal(run(text).reason, reason);
  assert.equal(run(text, true).reason, 'success_not_observed');
}
run('');
const hidden = {innerText: success, parentElement: null, querySelectorAll: () => [],
  style: {display: 'block', visibility: 'visible', opacity: '0'}, getClientRects: () => [{}]};
const parent = {...hidden, style: {...hidden.style, opacity: '1'}, querySelectorAll: () => [hidden]};
global.document = {title: '', querySelectorAll: () => [parent, hidden]};
global.location = {hostname: 'pokepixel.nietore.com', protocol: 'https:', port: '', href: 'https://pokepixel.nietore.com/'};
now = 0;
verify(success, 'pokepixel.nietore.com', value => assert.equal(value.reason, 'success_not_observed'));
for (const text of ['', 'Token inválido.', success.toLowerCase(), success.replace('já', 'ja'), 'Não: ' + success]) {
  assert.equal(run(text).reason, 'success_not_observed');
  assert.equal(now, 14500);
}
'''.replace('SCRIPT', json.dumps(api._VERIFY_JS)).replace('SUCCESS', json.dumps(api._CONFIRM_SUCCESS))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr

@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_evidence_saved_before_tab_closes(cleanup_fails):
    s = dict(email='offline@example.test', opened=set(), verified=set(), attempts={}, bodies={'m': URL})
    d = Mock()
    d.window_handles = ['inbox']
    d.current_url = api.URL
    d.execute_script.return_value = True
    def new_window(kind):
        d.window_handles = ['inbox', 'confirmation']
        return 'confirmation'
    d.switch_to.new_window.side_effect = new_window
    def close():
        assert s['confirmation']['evidence'] == api._CONFIRM_SUCCESS
        assert s['confirmation']['verified_at'] == 1234
        if cleanup_fails:
            raise RuntimeError('synthetic cleanup failure')
    d.close.side_effect = close
    with patch.dict(api.sessions, {'s': s}, clear=True), patch.dict(api._DRV, inbox_handle='inbox'), \
         patch.object(api, '_navigate_confirmation'), patch.object(api, '_ensure_connected', return_value=True), \
         patch.object(api, '_verify_confirmation', return_value=(True, api._CONFIRM_SUCCESS)), \
         patch.object(api.time, 'time', return_value=1234):
        result = api._confirm_one(d, s, {'mid': 'm'}, 0)
        assert result['verified'] and result['verified_at'] == 1234
        assert api.email_status('s')['confirmation'] == s['confirmation']
        assert api.email_status('s')['state'] == 'verified'
    d.close.assert_called_once()

def test_full_capacity_preserves_uncertain_sessions():
    from fastapi import HTTPException
    sessions = {str(i): dict(email=f'{i}@example.test', opened={'m'}, verified=set(), expires_at=0)
                for i in range(api.MAX_BOXES)}
    with patch.dict(api.sessions, sessions, clear=True), patch.object(api, '_enqueue_delete') as delete, \
         patch.object(api, '_driver_locked') as driver:
        with pytest.raises(HTTPException) as error:
            api.create(api.CreateReq())
        assert error.value.status_code == 409
        assert api.sessions == sessions
    assert not delete.called and not driver.called


class RecoveryDriver:
    def __init__(self, tabs):
        self.tabs = tabs
        self.window_handles = list(tabs)
        self.current_window_handle = self.window_handles[-1]
        self.switch_to = self
        self.visits = []

    @property
    def current_url(self):
        return self.tabs[self.current_window_handle][0]

    def window(self, handle):
        assert handle in self.window_handles
        self.current_window_handle = handle

    def execute_script(self, script):
        return 1 if script == 'return 1' else self.tabs[self.current_window_handle][1]

    def new_window(self, kind):
        self.tabs['confirmation'] = ('about:blank', False)
        self.window_handles.append('confirmation')
        self.current_window_handle = 'confirmation'
        return 'confirmation'

    def get(self, url):
        self.visits.append((self.current_window_handle, url))
        raise TimeoutError('synthetic navigation uncertainty')

    def close(self):
        self.window_handles.remove(self.current_window_handle)


@pytest.mark.parametrize('stale', [None, 'gone'])
def test_stale_inbox_unique_recovery_and_uncertain_navigation(stale):
    d = RecoveryDriver({'inbox': (api.URL, True), 'user': ('https://example.test/', True)})
    s = dict(email='offline@example.test', auto_confirm=True, opened=set(), verified=set(),
             attempts={}, bodies={'m': URL}, messages=[{'mid': 'm'}])
    with patch.dict(api._DRV, d=d, inbox_handle=stale), patch.dict(api.sessions, s=s), \
         patch.object(api, '_validate_destination'):
        result = api._confirm_one(d, s, {'mid': 'm'}, 0)
        assert api._DRV['inbox_handle'] == 'inbox'
        assert not result['opened'] and s['attempts']['m']['navigation_attempted']
        s['attempts']['m']['next_retry'] = 0
        # Replay: uma tentativa extra apos timeout; driver morre de novo.
        replay = api._confirm_one(d, s, {'mid': 'm'}, 0)
        assert not replay['opened'] and not replay['verified']
        assert s['attempts']['m'].get('replay_done')
        # Terceira chamada: replay ja feito, skip.
        assert api._confirm_one(d, s, {'mid': 'm'}, 0).get('skipped')
        assert not api.email_status('s')['monitoring']
    assert d.current_window_handle == 'inbox'


@pytest.mark.parametrize('tabs', [
    {'user': ('https://example.test/', True)},
    {'user': ('http://smailpro.com/temporary-email', True)},
    {'user': ('https://smailpro.com.evil.test/temporary-email', True)},
    {'user': ('https://smailpro.com:444/temporary-email', True)},
    {'user': ('https://smailpro.com/other', True)},
    {'user': (api.URL, False)},
])
def test_missing_inbox_preflight_retries_bounded_without_navigation(tabs):
    d = RecoveryDriver(tabs)
    s = dict(email='offline@example.test', auto_confirm=True, opened=set(), verified=set(),
             attempts={}, bodies={'m': URL}, messages=[{'mid': 'm'}])
    with patch.dict(api._DRV, d=d, inbox_handle='gone'), patch.dict(api.sessions, s=s):
        for count in range(api.MAX_ATTEMPTS):
            result = api._confirm_one(d, s, {'mid': 'm'}, 0)
            att = s['attempts']['m']
            assert not result['opened'] and not att.get('navigation_attempted')
            assert att['count'] == count + 1
            assert api.email_status('s')['monitoring'] is (count + 1 < api.MAX_ATTEMPTS)
            assert api._confirm_one(d, s, {'mid': 'm'}, 0)['skipped']
            att['next_retry'] = 0
        assert not api._confirm_targets(s, s['messages'])
        assert api._DRV['inbox_handle'] == 'gone'
    assert not d.visits and d.window_handles == list(tabs)


def test_multiple_valid_inboxes_stop_without_navigation():
    d = RecoveryDriver({'one': (api.URL, True), 'two': (api.URL, True)})
    s = dict(email='offline@example.test', auto_confirm=True, opened=set(), verified=set(),
             attempts={}, bodies={'m': URL}, messages=[{'mid': 'm'}])
    with patch.dict(api._DRV, d=d, inbox_handle='gone'), patch.dict(api.sessions, s=s):
        result = api._confirm_one(d, s, {'mid': 'm'}, 0)
        assert result['state'] == 'ambiguous' and not result['opened']
        assert not s['attempts']['m'].get('navigation_attempted')
        assert not api.email_status('s')['monitoring']
        s['attempts']['m']['next_retry'] = 0
        assert api._confirm_one(d, s, {'mid': 'm'}, 0)['skipped']
        assert api._DRV['inbox_handle'] == 'gone'
    assert not d.visits and d.window_handles == ['one', 'two']


def test_ambiguous_links_stop_automatic_worker():
    s = dict(email='offline@example.test', auto_confirm=True, opened=set(), verified=set(),
             attempts={'m': {'state': 'ambiguous', 'count': 1}}, messages=[{'mid': 'm'}])
    with patch.dict(api.sessions, {'s': s}, clear=True), patch.object(api, '_query_inboxes') as query:
        assert not api.email_status('s')['monitoring']
        api._worker_tick()
        assert not query.called


def test_destination_preflight_does_not_mark_navigation():
    d = RecoveryDriver({'inbox': (api.URL, True)})
    s = dict(email='offline@example.test', auto_confirm=True, opened=set(), verified=set(), attempts={}, bodies={'m': URL})
    with patch.dict(api._DRV, d=d, inbox_handle='inbox'), patch.dict(api.sessions, s=s), \
         patch.object(api, '_validate_destination', side_effect=ValueError('DNS bloqueado')):
        result = api._confirm_one(d, s, {'mid': 'm'}, 0)
        assert not api.email_status('s')['monitoring']
    assert not result['opened'] and not s['attempts']['m'].get('navigation_attempted')
    assert not d.visits and d.window_handles == ['inbox']


def test_navigation_marker_survives_cleanup_exception():
    d = RecoveryDriver({'inbox': (api.URL, True)})
    s = dict(email='offline@example.test', auto_confirm=True, opened=set(), verified=set(),
             attempts={}, bodies={'m': URL}, messages=[{'mid': 'm'}])
    def navigate(url):
        assert s['attempts']['m']['navigation_attempted']
        assert api.email_status('s')['monitoring']
        d.visits.append((d.current_window_handle, url))
        raise TimeoutError('synthetic')
    d.get = navigate
    ensure_calls = [RuntimeError('cleanup failed'), True]
    with patch.dict(api._DRV, d=d, inbox_handle='inbox'), patch.dict(api.sessions, s=s), \
         patch.object(api, '_validate_destination'), \
         patch.object(api, '_ensure_connected', side_effect=ensure_calls):
        with pytest.raises(RuntimeError, match='cleanup failed'):
            api._confirm_one(d, s, {'mid': 'm'}, 0)
        s['attempts']['m']['next_retry'] = 0
        # Replay apos timeout; _ensure_connected agora funciona.
        replay = api._confirm_one(d, s, {'mid': 'm'}, 0)
        assert not replay['opened'] and not replay['verified']
        assert s['attempts']['m'].get('replay_done')
        # Terceira chamada: replay ja feito, skip.
        assert api._confirm_one(d, s, {'mid': 'm'}, 0).get('skipped')
        assert not api.email_status('s')['monitoring']
    assert d.visits == [('confirmation', URL)]


def test_recovery_without_current_window_updates_under_lock():
    class ClosedCurrent(RecoveryDriver):
        @property
        def current_window_handle(self):
            if self._current == 'closed':
                raise RuntimeError('no such window')
            return self._current

        @current_window_handle.setter
        def current_window_handle(self, value):
            self._current = value

    class LockedState(dict):
        def __setitem__(self, key, value):
            assert api._LOCK._is_owned()
            super().__setitem__(key, value)

    d = ClosedCurrent({'inbox': (api.URL, True)})
    d.current_window_handle = 'closed'
    with patch.object(api, '_DRV', LockedState(inbox_handle='gone')):
        assert api._resolve_inbox_handle(d, 'gone') == 'inbox'
        assert api._DRV['inbox_handle'] == 'inbox'


def test_verify_js_returns_object_with_evidence():
    import json, subprocess
    script = r'''
const assert = require('node:assert/strict');
const verify = new Function(SCRIPT);
const success = SUCCESS;
let now = 0;
global.performance = {now: () => now, getEntriesByType: () => [{responseStatus: 200}]};
global.setTimeout = (fn, delay) => { now += delay; fn(); };
global.getComputedStyle = el => el.style;
function run(text, opts = {}) {
  now = 0;
  const host = opts.host || 'pokepixel.nietore.com';
  const protocol = opts.protocol || 'https:';
  const port = opts.port || '';
  global.location = {hostname: host, protocol, port, href: protocol + '//' + host + '/'};
  const el = {innerText: text, parentElement: null,
    querySelectorAll: () => [],
    style: {display: opts.hidden ? 'none' : 'block', visibility: 'visible', opacity: '1'},
    getClientRects: () => opts.hidden ? [] : [{}]};
  global.document = {querySelectorAll: () => [el], title: opts.title || 'Test Page'};
  let result;
  verify(success, 'pokepixel.nietore.com', value => result = value);
  return result;
}
// 1. success_visible: sample vazio, status = 200
const r1 = run(success);
assert.equal(r1.reason, 'success_visible');
assert.deepEqual(r1.sample, []);
assert.equal(r1.status, 200);
assert.equal(typeof r1.href, 'string');
assert.equal(typeof r1.title, 'string');
// 2. token_used: sample preenchido
const r2 = run('Token já utilizado.');
assert.equal(r2.reason, 'token_used');
assert.ok(Array.isArray(r2.sample));
assert.ok(r2.sample.length > 0 && r2.sample.length <= 5);
assert.ok(r2.sample[0].length <= 200);
// 3. sample vazio em unexpected_origin
const r3 = run(success, {host: 'evil.test'});
assert.equal(r3.reason, 'unexpected_origin');
assert.deepEqual(r3.sample, []);
// 4. status = null quando getEntriesByType falha
global.performance.getEntriesByType = () => { throw new Error('not supported'); };
const r4 = run('Token expirado.');
assert.equal(r4.status, null);
assert.equal(r4.reason, 'token_expired');
// 5. status = null quando nao ha entrada
global.performance.getEntriesByType = () => [];
const r5 = run(success);
assert.equal(r5.status, null);
assert.equal(r5.reason, 'success_visible');
// 6. sample limitado a 5 itens: 9 blocos de texto + 1 "Token ja utilizado."
const blocks = Array.from({length: 9}, (_, i) => ({
  innerText: 'texto ' + i, parentElement: null, querySelectorAll: () => [],
  style: {display: 'block', visibility: 'visible', opacity: '1'},
  getClientRects: () => [{}]}));
const tokenEl = {innerText: 'Token já utilizado.', parentElement: null, querySelectorAll: () => [],
  style: {display: 'block', visibility: 'visible', opacity: '1'},
  getClientRects: () => [{}]};
global.document = {querySelectorAll: () => [...blocks, tokenEl], title: ''};
global.location = {hostname: 'pokepixel.nietore.com', protocol: 'https:', port: '', href: 'https://pokepixel.nietore.com/'};
now = 0;
let r6;
verify(success, 'pokepixel.nietore.com', value => r6 = value);
assert.equal(r6.reason, 'token_used');
assert.equal(r6.sample.length, 5);
'''.replace('SCRIPT', json.dumps(api._VERIFY_JS)).replace('SUCCESS', json.dumps(api._CONFIRM_SUCCESS))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_verify_confirmation_handles_malformed_returns():
    d = Mock()
    d.timeouts.script = 47
    # String solta (formato antigo)
    d.execute_async_script.return_value = 'success_not_observed'
    diag = {}
    assert api._verify_confirmation(d, diag) == (False, '')
    assert diag['reason'] == 'success_not_observed'
    # None
    d.execute_async_script.return_value = None
    diag = {}
    assert api._verify_confirmation(d, diag) == (False, '')
    assert diag['reason'] == 'success_not_observed'
    # Dict com reason inventado
    d.execute_async_script.return_value = {'reason': 'fake_reason'}
    diag = {}
    assert api._verify_confirmation(d, diag) == (False, '')
    assert diag['reason'] == 'success_not_observed'


def test_verify_confirmation_redacts_email_and_tokens_in_evidence():
    d = Mock()
    d.timeouts.script = 47
    d.execute_async_script.return_value = {
        'reason': 'success_not_observed', 'href': 'https://site.com/verify?verify_email_token=ABC123',
        'title': 'Confirme alvo@gmail.com por favor', 'status': 404,
        'sample': ['Clique aqui para alvo@gmail.com', 'Token: verify_email_token=XYZ789', 'ok']
    }
    diag = {}
    api._verify_confirmation(d, diag)
    assert diag['reason'] == 'destination_error'
    assert 'ABC123' not in diag['href']
    assert 'alvo@gmail.com' not in diag['title']
    assert '[EMAIL]' in diag['title']
    assert 'XYZ789' not in str(diag['sample'])
    assert '[REDACTED]' in diag['sample'][1]
    assert diag['status'] == 404
    assert len(diag['sample']) == 3
