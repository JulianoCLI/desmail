"""Abertura offline, somente tokens sinteticos: python -m pytest -q."""
import json
import asyncio
import subprocess
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

import pytest
import app as api
import tui

URL = 'https://pokepixel.nietore.com/play/?verify_email_token=synthetic-A_b%2f%25=44'


@pytest.mark.parametrize('body, expected', [
    (f'<a href="{URL}">Confirmar</a>', URL),
    (f"<a href='{URL}'>Confirmar</a>", URL),
    (f'Copie: {URL}', URL),
    (f'<a href="{URL}&amp;lang=pt">Confirmar</a>', URL + '&lang=pt'),
    (f'{URL}&amp;lang=pt', URL + '&lang=pt'),
    (f'<a href="{URL}&amp;amp;x=1">Confirmar</a>', URL + '&amp;x=1'),
    (f'<a href="{URL}!">Confirmar</a>', URL + '!'),
    (f'<a href="{URL}">token visual\ncontinua</a>', URL),
])
def test_exact_extraction(body, expected):
    assert api._select_link(api._extract_links(body)) == expected


@pytest.mark.parametrize('url', [
    'https://pokepixel.nietore.com/',
    'https://pokepixel.nietore.com/play/',
    URL.replace('https://', 'https://user:pass@'),
    URL.replace('/play/', '/reset/'),
    URL.replace('/play/', '/unsubscribe/'),
    URL + '&action=reset',
    URL + '&unsubscribe=1',
    URL.split('=')[0] + '=',
    URL.split('=')[0] + '=%20',
    URL + '&verify_email_token=other',
    f'<a href="{URL}\ncontinued">Confirmar</a>',
])
def test_reject_invalid_confirmation(url):
    assert api._select_link(api._extract_links(url)) == ''


def test_multiple_domains_are_ambiguous_and_text_is_not_joined():
    body = 'https://fake.test/?verify_email_token=wrong\n' + URL + '\ncontinued'
    assert api._select_link(api._extract_links(body)) is None


def test_manual_open_exact_ids():
    ui = tui.DesmailApp()
    sid, mid = 's+&%#/?', 'm+&%#/?='
    s = dict(session_id=sid, email='offline@example.test', verified=set())
    def request(method, path, data, **kwargs):
        assert method == 'POST' and path == '/email/open'
        assert data == dict(session_id=sid, mid=mid, revalidate=False)
        return dict(opened=True, verified=False, status='sem evidencia')
    with patch.object(tui, 'req', side_effect=request), \
         patch.object(ui, '_say') as say:
        ui._do_open_msg(s, mid)
    logs = ' '.join(c.args[0] for c in say.call_args_list)
    assert 'sem evidencia' in logs
    assert URL not in logs and not s['verified']


@pytest.mark.parametrize('response, message', [
    (dict(error='sonjj-http-503'), 'sonjj-http-503'),
    (dict(status='sem link no corpo'), 'sem link no corpo'),
])
def test_manual_failure_does_not_open(response, message):
    ui = tui.DesmailApp()
    with patch.object(tui, 'req', return_value=response), \
         patch.object(ui, '_say') as say:
        ui._do_open_msg(dict(session_id='s'), 'm')
    assert message in ' '.join(c.args[0] for c in say.call_args_list)


@pytest.mark.parametrize('payload', [dict(ok=False, error='sonjj-http-503'), dict(ok=True, body=123)])
def test_body_failure_is_explicit(payload):
    driver = Mock()
    driver.execute_async_script.return_value = json.dumps(payload)
    body, error = api._read_body(driver, 'offline@example.test', 'm')
    assert body == '' and error


def test_auto_uses_same_exact_link_without_false_verification():
    s = dict(email='offline@example.test', opened=set(), verified=set(), attempts={})
    with patch.object(api, '_read_body', return_value=(f'<a href="{URL}">Confirmar</a>', '')), \
         patch.object(api, '_close_modal'), patch.object(api, '_log_event'), \
         patch.object(api, '_open_confirm_isolated', return_value=(True, 'aberto', {'verified': False})) as browser:
        result = api._confirm_one(Mock(), s, dict(mid='m', subject='Confirmar'), 6)
    assert browser.call_count == 1 and browser.call_args.args[2] == URL
    assert result['opened'] and not result['verified'] and not s['verified']


def test_read_body_javascript_encoding_and_failures():
    driver = Mock()
    driver.execute_async_script.return_value = '{}'
    api._read_body(driver, 'offline@example.test', 'm')
    script = driver.execute_async_script.call_args.args[0]
    harness = r'''
const assert = require('node:assert/strict');
const script = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  const address = 'offline+tag@example.test', mid = 'm+&%#/?=', payload = 'p+&%#/?=';
  const body = '<a href="https://pokepixel.nietore.com/play/?verify_email_token=synthetic-A_b%2f">Confirmar</a>';
  for (const mode of ['ok', 'message-http', 'sonjj-http', 'json', 'network', 'empty', 'large', 'type']) {
    global.window = {Alpine: true};
    global.document = {querySelector: () => ({})};
    global.Alpine = {$data: () => ({captcha: async () => 'fake', checkTypeEmail: () => 'google'})};
    let count = 0;
    global.fetch = async (url) => {
      count++;
      const u = new URL(url, 'https://smailpro.com');
      if (count === 1) {
        assert.equal(u.searchParams.get('email'), address);
        assert.equal(u.searchParams.get('mid'), mid);
        if (mode === 'network') throw Error('offline');
        return {ok: mode !== 'message-http', status: 503, text: async () => payload};
      }
      assert.equal(u.pathname, '/v1/temp_gmail/message');
      assert.equal(u.searchParams.get('payload'), payload);
      return {ok: mode !== 'sonjj-http', status: 503, text: async () => mode === 'json' ? '{' : JSON.stringify({
        body: mode === 'empty' ? '' : mode === 'large' ? 'x'.repeat(200001) : mode === 'type' ? 123 : body
      })};
    };
    const result = await new Promise(resolve => new Function(script)(address, mid, raw => resolve(JSON.parse(raw))));
    assert.equal(result.ok, mode === 'ok', mode);
    if (result.ok) assert.equal(result.body, body);
    else assert.ok(result.error);
    assert.equal(count, ['message-http', 'network'].includes(mode) ? 1 : 2);
  }
})().catch(error => {console.error(error); process.exitCode = 1});
'''
    result = subprocess.run(['node', '-e', harness], input=json.dumps(script),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('focus', [True, False])
def test_enter_reads_selected_message_and_opens_once(focus):
    async def run():
        ui = tui.DesmailApp()
        s = dict(session_id='sid+&%', email='offline@example.test')
        mid = 'mid+&%#/?='
        state = dict(email=s['email'], status='connected', opened=set(), verified=set(), attempts={},
                     messages=[dict(mid=mid, subject='Confirmar')])
        def request(method, path, *args, **kwargs):
            if path == '/sessions':
                return []
            assert method == 'POST' and path == '/email/open'
            return api.open_message(api.OpenReq(**args[0]))
        with patch.dict(api.sessions, {s['session_id']: state}, clear=True), \
             patch.object(tui, 'req', side_effect=request), \
             patch.object(api, '_driver_locked', return_value=Mock()), \
             patch.object(api, '_read_body', return_value=(f'<a href="{URL}">Confirmar</a>', '')), \
             patch.object(api, '_close_modal'), \
             patch.object(api, '_page_delete', side_effect=AssertionError('delete forbidden')), \
             patch.object(api, '_open_confirm_isolated', return_value=(True, 'sem evidencia', {'verified': False})) as browser:
            async with ui.run_test() as pilot:
                ui._poll_timer.stop()
                await ui.workers.wait_for_complete()
                await ui.refresh_accts([s])
                ui._accept(s, dict(state='ok', messages=[dict(mid=mid, subject='Confirmar')]), 1)
                ui._render_cache()
                if focus:
                    ui.query_one('#inbox', tui.DataTable).focus()
                else:
                    ui.screen.set_focus(None)
                await pilot.press('enter', 'enter')
                for _ in range(50):
                    if browser.called:
                        break
                    await pilot.pause(0.02)
                assert browser.call_count == 1 and browser.call_args.args[2] == URL
                assert api.sessions[s['session_id']] is state
                assert not state['verified'] and state['opened'] == {mid}
    asyncio.run(run())


def test_stale_event_never_opens_replacement_and_errors_visible():
    async def run():
        ui = tui.DesmailApp()
        s = dict(session_id='s', email='prefix@example.test')
        with patch.object(tui, 'req', return_value=[]):
            async with ui.run_test() as pilot:
                ui._poll_timer.stop()
                await ui.workers.wait_for_complete()
                await ui.refresh_accts([s])
                ui._accept(s, dict(state='ok', messages=[dict(mid='old')]), 1)
                ui._render_cache()
                table = ui.query_one('#inbox', tui.DataTable)
                old = table.coordinate_to_cell_key((0, 0)).row_key
                ui._accept(s, dict(state='ok', messages=[dict(mid='new')]), 2)
                ui._render_cache()
                with patch.object(tui, 'req', side_effect=OSError('offline body')) as request:
                    # Enter nao tem handler duplicado: a guarda de linha obsoleta vive em _open_key.
                    ui._open_key(old)
                    assert not request.called
                    ui.screen.set_focus(None)
                    await pilot.press('enter')
                    for _ in range(50):
                        await pilot.pause(0.02)
                        if not ui._opening: break
                    assert request.call_count == 1
                log = ui.query_one('#log', tui.Log)
                assert log.region.height > 0 and table.region.height > 0
                assert any('offline body' in line for line in log.lines)
                assert any('mensagem antiga' in line for line in log.lines)
    asyncio.run(run())
