"""Offline behavioral checks: python -m pytest -q."""
import asyncio
import threading
import io
import json
import subprocess
import urllib.error
from unittest.mock import patch

import pytest
from fastapi import BackgroundTasks, HTTPException
import app as api
import tui


@pytest.fixture(autouse=True)
def isolated():
    api.sessions.clear()
    api._DRV.update(d=None, inbox_handle=None, status="ok", error="")
    api._POLL_STATE.update(busy=False, owner=None, since=0, token=None)
    with patch.object(api, "new_driver", side_effect=AssertionError("external driver forbidden")):
        yield
    api.sessions.clear()
    api._DRV["d"] = None


def session(sid="a"):
    s = dict(email=f"{sid}@example.test", seen=set(), opened=set(), verified=set(),
             attempts={}, status="connected", error="", _sid=sid)
    api.sessions[sid] = s
    return s


def test_guard_age_and_identity():
    token = api._poll_begin("poll")
    api._POLL_STATE["since"] -= 10000
    assert not api._poll_begin("poll")
    api._poll_end("poll")
    assert api._poll_state_info()["busy"]
    api._poll_end(token)
    next_token = api._poll_begin("poll")
    api._poll_end(token)
    assert api._poll_state_info()["busy"]
    api._poll_end(next_token)


def test_retry_limit_and_cooldown():
    s = session()
    m = dict(mid="m", subject="Confirmar")
    with patch.object(api, "_read_body", return_value=("", "HTTP 503")) as read, \
         patch.object(api, "_close_modal"), patch.object(api.time, "monotonic", return_value=100) as clock:
        for n in range(api.MAX_ATTEMPTS):
            clock.return_value = 100 + n * api.RETRY_COOLDOWN_S
            api._confirm_one(None, s, m, 6)
            api._confirm_one(None, s, m, 6)
        clock.return_value += 10000
        api._confirm_one(None, s, m, 6)
        assert read.call_count == api.MAX_ATTEMPTS


def test_missing_empty_and_error_preserve_cache():
    s = session()
    message = dict(mid="m", subject="oi")
    good = dict(ok=True, emails=[dict(address=s["email"], messages=[message])])
    assert api._inbox_result(s, good)["messages"] == [message]
    for data, state in [(dict(ok=True, emails=[]), "missing"),
                        (dict(ok=False, error="HTTP 503"), "error")]:
        result = api._inbox_result(s, data)
        assert result["state"] == state and result["stale"]
        assert result["messages"] == [message]
    good["emails"][0]["messages"] = []
    result = api._inbox_result(s, good)
    assert result["state"] == "empty" and not result["stale"]


def test_disconnected_query_never_recreates():
    s = session()
    api._on_driver_failure("lost")
    result = api.check("a")
    assert result["state"] == "disconnected"
    assert s["status"] == "disconnected"
    with pytest.raises(HTTPException) as error:
        api.body("a", "m")
    assert error.value.status_code == 503


def test_inbox_returns_before_confirmation():
    s = session()
    tasks = BackgroundTasks()
    data = dict(ok=True, emails=[dict(address=s["email"], messages=[dict(mid="m", subject="Confirmar")])])
    with patch.object(api, "_driver_locked", return_value=object()), \
         patch.object(api, "_page_loop_all", return_value=data), \
         patch.object(api, "_confirm_one", return_value={}) as confirm:
        result = api.poll(api.PollReq(session_id="a"), tasks)
        assert result["count"] == 1
        assert confirm.call_count == 0
        asyncio.run(tasks())
        assert confirm.call_count == 1


def test_slow_timer_single_flight_and_immediate_render():
    async def run():
        started, release = threading.Event(), threading.Event()
        counts = {"sweep": 0, "confirm": 0}
        s = dict(session_id="a", email="a@example.test")
        def request(method, path, *args):
            if path == "/sessions":
                return [s]
            if path == "/email/sweep":
                counts["sweep"] += 1
                started.set()
                assert release.wait(5)
                return dict(results=[dict(session_id="a", state="ok", messages=[dict(mid="m", subject="Confirmar")])])
            if path == "/email/autoconfirm":
                counts["confirm"] += 1
                assert app.query_one("#inbox", tui.DataTable).row_count == 1
                return dict(confirmed=[])
            raise AssertionError(path)
        app = tui.DesmailApp()
        with patch.object(tui, "req", side_effect=request), patch.object(tui, "POLL_S", 0.03):
            async with app.run_test() as pilot:
                try:
                    assert await asyncio.to_thread(started.wait, 3)
                    await pilot.pause(0.15)
                    assert counts["sweep"] == 1
                finally:
                    release.set()
                await pilot.pause(0.2)
                assert counts["confirm"] >= 1
                app._poll_timer.stop()
                await app.workers.wait_for_complete()
    asyncio.run(run())


def test_concurrent_guard_and_wrong_release():
    entered, release = threading.Event(), threading.Event()
    def owner():
        token = api._poll_begin("sweep")
        entered.set()
        assert release.wait(3)
        api._poll_end(token)
    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert entered.wait(3)
        api._poll_end("force")
        assert api.sweep(api.SweepReq(), BackgroundTasks())["skipped"]
        assert api._poll_state_info()["busy"]
    finally:
        release.set()
        thread.join(3)
    assert not api._poll_state_info()["busy"]


def test_tui_http_error_late_results_and_selection():
    async def run():
        app = tui.DesmailApp()
        ss = [dict(session_id=sid, email=f"{sid}@example.test") for sid in ("a", "b")]
        with patch.object(tui, "req", return_value=[]):
            async with app.run_test() as pilot:
                app._poll_timer.stop()
                await app.workers.wait_for_complete()
                await app.refresh_accts(ss)
                for s in ss:
                    app._accept(s, dict(state="ok", messages=[dict(mid=s["session_id"], subject="oi")]), 10)
                app._render_cache()
                table = app.query_one("#inbox", tui.DataTable)
                table.move_cursor(row=1)
                app.query_one("#accts", tui.ListView).index = 1
                app._sequence = 10
                def request(method, path, *args):
                    if path == "/sessions":
                        return ss
                    raise urllib.error.HTTPError(path, 503, "unavailable", {}, io.BytesIO(b"HTTP 503"))
                with patch.object(tui, "req", side_effect=request):
                    await app._poll_cycle()
                assert table.row_count == 2 and table.cursor_row == 1
                assert app._sel_sid() == "b"
                for s in ss:
                    cached = app._cache[s["session_id"]]
                    assert cached["state"] == "error" and cached["stale"]
                    assert "503" in cached["error"]
                    app._accept(s, dict(state="empty", messages=[]), 9)
                    assert app._cache[s["session_id"]] == cached
                app._accept(ss[0], dict(state="missing", error="ausente"), 12)
                app._render_cache()
                assert table.row_count == 2
                await app.refresh_accts([ss[1]])
                app._accept(ss[0], dict(state="ok", messages=[dict(mid="late")]), 13)
                assert "a" not in app._cache and table.row_count == 1
    asyncio.run(run())


def test_native_loop_http_failure_is_not_cached_success():
    # Execute actual injected JS in Node with fake DOM/fetch; no network.
    harness = r'''
const assert = require('node:assert/strict');
const script = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  for (const mode of ['http', 'network', 'unobserved', 'empty']) {
    const original = async () => {
      if (mode === 'network') throw Error('offline');
      return {ok: mode !== 'http', status: mode === 'http' ? 503 : 200};
    };
    global.window = {fetch: original, Alpine: true};
    global.document = {querySelector: selector => selector};
    global.Alpine = {$data: selector => selector.includes('inbox()') ? {
      executeLoop: async () => {
        if (mode !== 'unobserved') try { await window.fetch('/fake'); } catch {}
      }
    } : {emails: [{address: 'a@example.test', messages: []}]}};
    const result = await new Promise(resolve => new Function(script)(raw => resolve(JSON.parse(raw))));
    assert.equal(result.ok, mode === 'empty');
    if (mode === 'http') assert.equal(result.error, 'inbox-http-503');
    assert.equal(window.fetch, original);
  }
})().catch(error => {console.error(error); process.exitCode = 1});
'''
    result = subprocess.run(["node", "-e", harness], input=json.dumps(api._LOOP_ALL_JS),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_get_read_only_and_post_never_deletes():
    s = session()
    data = dict(ok=True, emails=[dict(address=s["email"], messages=[dict(mid="m", subject="Confirmar")])])
    with patch.object(api, "_driver_locked", return_value=object()), \
         patch.object(api, "_page_loop_all", return_value=data), \
         patch.object(api, "_confirm_one", return_value={"verified": True}) as confirm, \
         patch.object(api, "_page_delete", side_effect=AssertionError("delete forbidden")):
        assert api.check("a")["count"] == 1
        assert not confirm.called
        result = api.autoconfirm(api.ConfirmReq(session_id="a", delete_after=True))
        assert result["confirmed"] == [{"verified": True}] and not result["deleted"]
        assert api.sessions["a"] is s


def test_background_guard_preserves_session_identity():
    s = session()
    token = api._poll_begin("poll")
    session()  # same sid, different object: old snapshot must not confirm
    with patch.object(api, "_driver_locked", return_value=object()), \
         patch.object(api, "_confirm_one", side_effect=AssertionError("old session")):
        api._confirm_cached(token, [("a", s, [dict(mid="m", subject="Confirmar")])], 6)
    assert not api._poll_state_info()["busy"]


def test_cancel_waits_for_http_thread():
    async def run():
        started, release = threading.Event(), threading.Event()
        def slow():
            started.set()
            assert release.wait(3)
        app = tui.DesmailApp()
        task = asyncio.create_task(app._to_thread(slow))
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


def test_manual_auto_cache_dedup_progress_and_revalidation():
    s = session()
    s['messages'] = [dict(mid='m', subject='Confirmar')]
    s['inbox_state'] = 'ok'
    started, release = threading.Event(), threading.Event()
    url = 'https://pokepixel.nietore.com/play/?verify_email_token=synthetic'
    def navigate(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return True, 'sem evidencia', {'verified': False}
    with patch.object(api, '_driver_locked', return_value=object()), \
         patch.object(api, '_read_body', return_value=(url, '')) as read, \
         patch.object(api, '_close_modal'), patch.object(api, '_log_event'), \
         patch.object(api, '_open_confirm_isolated', side_effect=navigate) as opened:
        assert api.body('a', 'm')['best'] == url
        worker = threading.Thread(target=api.autoconfirm, args=(api.ConfirmReq(session_id='a'),))
        worker.start()
        try:
            assert started.wait(3)
            assert api.open_message(api.OpenReq(session_id='a', mid='m'))['state'] == 'in_progress'
            assert api.check('a')['messages'] == s['messages']
            assert api.body('a', 'm')['best'] == url
        finally:
            release.set()
            worker.join(3)
        assert not worker.is_alive()
        assert api.open_message(api.OpenReq(session_id='a', mid='m'))['skipped']
        s['attempts']['m']['next_retry'] = 0
        api.open_message(api.OpenReq(session_id='a', mid='m', revalidate=True))
        assert opened.call_count == 2 and read.call_count == 1
        assert s['opened'] == {'m'} and not s['verified']


def test_body_error_retry_then_cache_survives_disconnect():
    s = session()
    with patch.object(api, '_driver_locked', return_value=object()), patch.object(api, '_close_modal'), \
         patch.object(api, '_read_body', side_effect=[('', 'HTTP 503'), ('cached', '')]) as read:
        assert api.body('a', 'm')['error'] == 'HTTP 503'
        assert not s['bodies']
        assert api.body('a', 'm')['body_snippet'] == 'cached'
        api._on_driver_failure('offline')
        assert api.body('a', 'm')['body_snippet'] == 'cached'
        assert read.call_count == 2


def test_background_batch_is_bounded_and_releases_lock():
    s = session()
    msgs = [dict(mid=str(i), subject='Confirmar') for i in range(100)]
    acquired = threading.Event()
    def confirm(*args):
        return dict(state='unknown')
    with patch.object(api, '_driver_locked', return_value=object()), \
         patch.object(api, '_confirm_one', side_effect=confirm) as called:
        api._confirm_cached(None, [('a', s, msgs)], 0)
        assert called.call_count == 1
    def probe():
        with api._LOCK: acquired.set()
    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(2)
    assert acquired.is_set()


def test_tui_poll_continues_during_slow_confirmation():
    async def run():
        started, release = threading.Event(), threading.Event()
        counts = dict(poll=0, confirm=0)
        s = dict(session_id='a', email='a@example.test')
        def request(method, path, *args):
            if path == '/sessions': return [s]
            if path == '/email/sweep':
                counts['poll'] += 1
                return dict(results=[dict(session_id='a', state='ok', messages=[dict(mid='m')])])
            if path == '/email/autoconfirm':
                counts['confirm'] += 1
                started.set()
                assert release.wait(5)
                return dict(confirmed=[])
            raise AssertionError(path)
        ui = tui.DesmailApp()
        with patch.object(tui, 'req', side_effect=request), patch.object(tui, 'POLL_S', 0.05):
            async with ui.run_test() as pilot:
                try:
                    assert await asyncio.to_thread(started.wait, 3)
                    await pilot.pause(0.2)
                    assert counts['poll'] > 1 and counts['confirm'] == 1
                    assert ui.query_one('#inbox', tui.DataTable).row_count == 1
                finally:
                    ui._poll_timer.stop()
                    release.set()
                await ui.workers.wait_for_complete()
    asyncio.run(run())
