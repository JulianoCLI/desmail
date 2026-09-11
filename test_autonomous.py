"""API autonoma offline; nunca cria caixas externas."""
import asyncio
import json
import threading
import time
from unittest.mock import Mock, patch

import pytest
from fastapi import BackgroundTasks
import app as api

# Capturada antes da fixture `isolated` bloquear new_driver.
_REAL_NEW_DRIVER = api.new_driver


class TestClient:
    __test__ = False

    def __init__(self, app):
        self.app = app

    def __enter__(self):
        self.runner = asyncio.Runner()
        self.life = self.app.router.lifespan_context(self.app)
        self.runner.run(self.life.__aenter__())
        return self

    def __exit__(self, *args):
        self.runner.run(self.life.__aexit__(*args))
        self.runner.close()

    def request(self, method, path, payload=None, params=None):
        from urllib.parse import urlencode
        async def run():
            messages = []
            async def receive():
                return {'type': 'http.request', 'body': json.dumps(payload).encode(), 'more_body': False}
            async def send(message):
                messages.append(message)
            await self.app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                            'method': method, 'scheme': 'http', 'path': path, 'root_path': '',
                            'query_string': urlencode(params or {}).encode(),
                            'headers': [(b'content-type', b'application/json')]}, receive, send)
            body = b''.join(m.get('body', b'') for m in messages).decode()
            return type('Response', (), {'status_code': messages[0]['status'], 'text': body,
                                        'json': lambda self: json.loads(body)})()
        return self.runner.run(run())

    def post(self, path, json):
        return self.request('POST', path, json)

    def get(self, path, params):
        return self.request('GET', path, params=params)


@pytest.fixture(autouse=True)
def isolated():
    with patch.dict(api.sessions, {}, clear=True), patch.dict(api._DRV, d=None, inbox_handle=None), \
         patch.object(api, 'new_driver', side_effect=AssertionError('external forbidden')), \
         patch.object(api, '_log_event'):
        api._POLL_STATE.update(busy=False, owner=None, since=0, token=None)
        api._DELETE_QUEUE.clear()
        yield
        api._DELETE_QUEUE.clear()


def create(client, **kwargs):
    with patch.object(api, '_driver_locked', return_value=Mock()), \
         patch.object(api, '_alp_valid_list', return_value=[]), \
         patch.object(api, '_page_create', return_value={'ok': True, 'email': 'offline@example.test'}):
        response = client.post('/email/create', json=kwargs)
    assert response.status_code == 200, response.text
    return response.json()['session_id']


def test_lifecycle_empty_create_status():
    with patch.object(api, '_query_inboxes', side_effect=AssertionError('empty startup')), \
         TestClient(api.app) as client:
        assert api.app.state.worker.is_alive()
        sid = create(client)
        result = client.get('/email/status', params={'session_id': sid}).json()
        assert result['state'] == 'waiting_email' and result['monitoring']
        assert result['auto_confirm'] and result['timeout_seconds'] == 600
        assert client.get('/email/status', params={'session_id': 'missing'}).status_code == 404
        worker = api.app.state.worker
    assert not worker.is_alive()


@pytest.mark.parametrize('payload', [
    {'timeout_seconds': 0}, {'timeout_seconds': 3601}, {'timeout_seconds': True},
    {'expected_hosts': ['https://example.org']}, {'expected_hosts': ['*.example.org']},
    {'expected_hosts': ['127.0.0.1']}, {'expected_hosts': ['localhost']},
    {'expected_hosts': ['example.org:443']}, {'expected_hosts': []},
])
def test_create_validation(payload):
    with TestClient(api.app) as client:
        assert client.post('/email/create', json=payload).status_code == 422


def test_tick_body_open_once_without_tui():
    with TestClient(api.app) as client:
        sid = create(client, expected_hosts=['Example.ORG'])
        s = api.sessions[sid]
        data = dict(ok=True, emails=[dict(address=s['email'], messages=[dict(mid='m')])])
        with patch.object(api, '_query_inboxes', return_value=data), \
             patch.object(api, '_driver_locked', return_value=Mock()), \
             patch.object(api, '_read_body', return_value=('https://example.org/verify?t=synthetic', '')) as read, \
             patch.object(api, '_close_modal'), \
             patch.object(api, '_open_confirm_isolated', return_value=(True, 'aberta', {'verified': False})) as opened:
            api._worker_tick()
            api._worker_tick()
        result = client.get('/email/status', params={'session_id': sid}).json()
        assert result['state'] == 'opened' and not result['verified']
        assert read.call_count == opened.call_count == 1
        assert s['bodies']['m'] and result['expected_hosts'] == ['example.org']


def test_auto_false_and_expiration_preserve_box():
    with TestClient(api.app) as client:
        sid = create(client, auto_confirm=False)
        s = api.sessions[sid]
        with patch.object(api, '_query_inboxes') as query:
            api._worker_tick()
            assert not query.called
            s['deadline'] = time.monotonic() - 1
            api._worker_tick()
        assert client.get('/email/status', params={'session_id': sid}).json()['state'] == 'expired'
        assert api.sessions[sid] is s


def test_background_owns_guard_until_done():
    with TestClient(api.app) as client:
        sid = create(client)
        s = api.sessions[sid]
        data = dict(ok=True, emails=[dict(address=s['email'], messages=[dict(mid='m')])])
        tasks = BackgroundTasks()
        with patch.object(api, '_query_inboxes', return_value=data):
            api.poll(api.PollReq(session_id=sid), tasks)
        try:
            assert api._poll_state_info()['busy']
            with patch.object(api, '_query_inboxes') as query:
                api._worker_tick()
                assert not query.called
            assert api.open_message(api.OpenReq(session_id=sid, mid='m'))['skipped']
        finally:
            api._poll_end(api._POLL_STATE['token'])


def test_worker_disconnect_and_deadline_during_body():
    with TestClient(api.app) as client:
        sid = create(client)
        api._worker_tick()
        assert api.email_status(sid)['state'] == 'disconnected'
        s = api.sessions[sid]
        s['status'] = 'connected'
        def body(*args):
            s['deadline'] = time.monotonic() - 1
            return 'https://example.org/verify?t=synthetic', ''
        with patch.object(api, '_read_body', side_effect=body), patch.object(api, '_close_modal'), \
             patch.object(api, '_open_confirm_isolated') as opened:
            result = api._confirm_one(Mock(), s, {'mid': 'm'}, 0)
        assert result['state'] == 'expired' and not opened.called
        assert s['bodies']['m']


def test_expected_target_mismatch_never_opens():
    with TestClient(api.app) as client:
        sid = create(client, expected_hosts=['example.org'])
        s = api.sessions[sid]
        with patch.object(api, '_read_body', return_value=('https://sub.example.org/verify?t=synthetic', '')), \
             patch.object(api, '_close_modal'), patch.object(api, '_open_confirm_isolated') as opened:
            api._confirm_one(Mock(), s, {'mid': 'm'}, 0)
        assert not opened.called and 'expected_hosts' in api.email_status(sid)['error']


@pytest.mark.parametrize('url', ['http://example.org/verify', 'https://example.org:444/verify',
                                'https://127.0.0.1/verify', 'https://[::1]/verify'])
def test_navigation_preflight_blocks(url):
    with pytest.raises(ValueError):
        api._validate_destination(url)


def test_dns_all_records_must_be_public():
    records = [(2, 1, 6, '', ('93.184.216.34', 443)), (2, 1, 6, '', ('10.0.0.1', 443))]
    with patch.object(api.socket, 'getaddrinfo', return_value=records):
        with pytest.raises(ValueError, match='DNS'):
            api._validate_destination('https://example.org/verify')


def test_navigation_timeout_closes_only_created_tab():
    class Driver:
        def __init__(self):
            self.window_handles = ['inbox', 'other']
            self.current_window_handle = 'inbox'
            self.switch_to = self
        def new_window(self, kind):
            self.window_handles.append('confirmation')
            self.current_window_handle = 'confirmation'
            return 'confirmation'
        def window(self, handle): self.current_window_handle = handle
        def close(self): self.window_handles.remove(self.current_window_handle)
        def open(self, url): raise TimeoutError('synthetic')
        def sleep(self, seconds): pass
        def execute_script(self, script): return ''
    d = Driver()
    with patch.object(api, '_validate_destination'):
        opened, status, ev = api._open_confirm_isolated(d, 'inbox', 'https://example.org/verify', 0)
    assert not opened and not ev['verified'] and 'TimeoutError' in status
    assert d.window_handles == ['inbox', 'other'] and d.current_window_handle == 'inbox'


def test_release_frees_session_without_waiting_provider():
    with TestClient(api.app) as client:
        sid = create(client)
        email = api.sessions[sid]["email"]
        with patch.object(api, "_page_delete", side_effect=AssertionError("release nao espera provedor")):
            response = client.post("/email/release", json={"session_id": sid})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body == {"ok": True, "released": True, "email": email}
        assert sid not in api.sessions
        assert email in list(api._DELETE_QUEUE)
        assert client.get("/email/status", params={"session_id": sid}).status_code == 404
        missing = client.post("/email/release", json={"session_id": sid}).json()
        assert missing == {"ok": True, "released": False}


def test_delete_queue_worker_never_blocks_create():
    d = Mock()
    d.execute_script.return_value = 1
    # Para qualquer worker de exclusao residual de testes anteriores.
    old = api._DELETE_WORKER.get("thread")
    if old is not None and old.is_alive():
        try:
            api._DELETE_WORKER.get("stop").set()
        except Exception:
            pass
        old.join(5)
    api._DELETE_WORKER["thread"] = None
    # Lock ocupado por OUTRA thread: worker devolve o item e nao chama o provedor.
    api._DELETE_QUEUE.append("offline@example.test")
    real_lock = api._LOCK
    holder_acquired, holder_release = threading.Event(), threading.Event()
    def holder():
        real_lock.acquire()
        try:
            holder_acquired.set()
            assert holder_release.wait(5)
        finally:
            real_lock.release()
    h = threading.Thread(target=holder)
    h.start()
    assert holder_acquired.wait(5)
    stop = threading.Event()
    timer = threading.Timer(1.2, stop.set)
    try:
        with patch.dict(api._DRV, d=d), \
             patch.object(api, "_page_delete") as delete:
            timer.start()
            api._delete_worker_loop(stop)
            assert not delete.called
            assert list(api._DELETE_QUEUE) == ["offline@example.test"]
    finally:
        timer.cancel()
        holder_release.set()
        h.join(5)
    # Stop ja setado: loop sai sem consumir.
    with patch.dict(api._DRV, d=d), patch.object(api, "_page_delete") as delete:
        stop = threading.Event()
        stop.set()
        api._delete_worker_loop(stop)
        assert not delete.called
        assert list(api._DELETE_QUEUE) == ["offline@example.test"]
    # Lock livre: consome a fila e chama o provedor uma vez.
    with patch.dict(api._DRV, d=d), \
         patch.object(api, "_page_delete", return_value={"ok": True}) as delete:
        stop = threading.Event()
        t = threading.Thread(target=api._delete_worker_loop, args=(stop,))
        t.start()
        try:
            for _ in range(100):
                if not api._DELETE_QUEUE or delete.called:
                    break
                time.sleep(0.05)
        finally:
            stop.set()
            t.join(5)
        assert not t.is_alive()
        assert delete.call_args.args == (d, "offline@example.test")
        assert not api._DELETE_QUEUE


def test_lifespan_waits_for_worker_and_manual_does_not_overlap():
    async def run():
        started, release = threading.Event(), threading.Event()
        s = dict(email='offline@example.test', auto_confirm=True, deadline=time.monotonic() + 600,
                 seen=set(), opened=set(), verified=set(), attempts={}, status='connected',
                 messages=[{'mid': 'm'}], _sid='s')
        api.sessions['s'] = s
        def read(*args):
            started.set()
            assert release.wait(5)
            return 'https://example.org/verify?t=synthetic', ''
        data = dict(ok=True, emails=[dict(address=s['email'], messages=s['messages'])])
        with patch.object(api, 'POLL_INTERVAL_S', 0.01), \
             patch.object(api, '_query_inboxes', return_value=data), \
             patch.object(api, '_driver_locked', return_value=Mock()), \
             patch.object(api, '_read_body', side_effect=read), patch.object(api, '_close_modal'), \
             patch.object(api, '_open_confirm_isolated', return_value=(True, 'aberta', {'verified': False})) as opened:
            life = api.lifespan(api.app)
            await life.__aenter__()
            try:
                assert await asyncio.to_thread(started.wait, 3)
                assert api.email_status('s')['state'] == 'reading_body'
                assert api.open_message(api.OpenReq(session_id='s', mid='m'))['skipped']
                closing = asyncio.create_task(life.__aexit__(None, None, None))
                await asyncio.sleep(0.03)
                assert not closing.done()
            finally:
                release.set()
            await closing
            assert opened.call_count == 1 and not api.app.state.worker.is_alive()
            assert not api._poll_state_info()['busy']
    asyncio.run(run())


def test_captcha_retry_uses_seleniumbase_methods():
    """_read_body_with_retry chama uc_gui_handle_captcha e solve_captcha do SB."""
    call_count = [0]
    def body_side_effect(d, address, mid):
        call_count[0] += 1
        if call_count[0] == 1:
            return "", "captcha:CAPTCHA timeout"
        return "<a href='https://example.org/verify?t=ok'>Confirmar</a>", ""
    d = Mock()
    with patch.object(api, '_read_body', side_effect=body_side_effect), \
         patch.object(api, '_close_modal'), \
         patch.object(api, '_try_solve_captcha', return_value=True) as solve:
        b, err, retries = api._read_body_with_retry(d, "a@test.com", "m1", "handle1")
    assert b  # body loaded on retry
    assert not err
    assert retries == 1
    assert solve.call_count >= 1


def test_captcha_retry_falls_back_to_refresh():
    """Se solve_captcha nao resolve, faz refresh da pagina."""
    call_count = [0]
    def body_side_effect(d, address, mid):
        call_count[0] += 1
        if call_count[0] <= 2:
            return "", "captcha:CAPTCHA timeout"
        return "<a href='https://example.org/verify?t=ok'>Confirmar</a>", ""
    d = Mock()
    with patch.object(api, '_read_body', side_effect=body_side_effect), \
         patch.object(api, '_close_modal'), \
         patch.object(api, '_try_solve_captcha', return_value=False), \
         patch.object(api, '_refresh_inbox_page', return_value=True) as refresh:
        b, err, retries = api._read_body_with_retry(d, "a@test.com", "m1", "handle1")
    assert b
    assert not err
    assert retries == 2
    assert refresh.call_count >= 1


def test_captcha_no_retry_on_non_captcha_error():
    """Erros que nao sao CAPTCHA nao devem causar retry."""
    with patch.object(api, '_read_body', return_value=("", "HTTP 503")), \
         patch.object(api, '_close_modal'), \
         patch.object(api, '_try_solve_captcha') as solve:
        b, err, retries = api._read_body_with_retry(Mock(), "a@test.com", "m1", "h")
    assert not b
    assert err == "HTTP 503"
    assert retries == 0
    assert not solve.called


def test_cached_body_delegates_to_retry():
    """_cached_body usa _read_body_with_retry em vez de _read_body direto."""
    with patch.object(api, '_read_body_with_retry', return_value=("body", "", 0)) as retry, \
         patch.object(api, '_close_modal'):
        s = {"bodies": {}, "email": "a@test.com"}
        b, err = api._cached_body(Mock(), s, "m1")
    assert b == "body"
    assert retry.call_count == 1


class FakeProc:
    """Processo falso para exercitar o reap sem tocar em processos reais."""

    def __init__(self, pid, name, ppid, cmdline=None, kids=()):
        self.pid, self._name, self._cmd = pid, name, cmdline or []
        self.info = {"pid": pid, "ppid": ppid, "name": name}
        self._kids, self.killed = list(kids), False

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmd

    def children(self, recursive=False):
        return self._kids

    def kill(self):
        self.killed = True


def _fake_psutil(procs, alive_pids):
    mod = Mock()
    mod.process_iter.return_value = procs
    mod.pid_exists.side_effect = lambda p: p in alive_pids
    return mod


def test_reap_mata_driver_orfao_e_preserva_driver_com_pai_vivo():
    kid = FakeProc(31, "chrome.exe", 30)
    orfao = FakeProc(30, "uc_driver.exe", 999, kids=[kid])   # pai morto
    ativo = FakeProc(40, "uc_driver.exe", 1234)              # pai vivo
    fake = _fake_psutil([orfao, ativo], alive_pids={1234})
    with patch.dict('sys.modules', {'psutil': fake}), \
         patch.object(api, '_OWNED_PIDS', set()):
        assert api.reap_orphan_drivers() == 1
    assert orfao.killed and kid.killed
    assert not ativo.killed


def test_reap_mata_chrome_de_automacao_e_nunca_o_chrome_pessoal():
    auto_kid = FakeProc(51, "chrome.exe", 50, ["chrome.exe", "--type=renderer"])
    auto = FakeProc(50, "chrome.exe", 999,
                    ["chrome.exe", "--remote-debugging-port=9222"], kids=[auto_kid])
    pessoal = FakeProc(60, "chrome.exe", 999, ["chrome.exe", "--profile-directory=Default"])
    fake = _fake_psutil([auto, pessoal], alive_pids=set())
    with patch.dict('sys.modules', {'psutil': fake}), \
         patch.object(api, '_OWNED_PIDS', set()):
        assert api.reap_orphan_drivers() == 1
    assert auto.killed and auto_kid.killed
    assert not pessoal.killed, "chrome pessoal do usuario nunca pode ser encerrado"


def test_reap_preserva_processos_da_propria_instancia():
    meu = FakeProc(70, "uc_driver.exe", 999)
    fake = _fake_psutil([meu], alive_pids=set())
    with patch.dict('sys.modules', {'psutil': fake}), \
         patch.object(api, '_OWNED_PIDS', {70}):
        assert api.reap_orphan_drivers() == 0
    assert not meu.killed


def test_shutdown_encerra_processos_rastreados():
    alvo = FakeProc(80, "chrome.exe", 1)
    fake = Mock()
    fake.Process.return_value = alvo
    with patch.dict('sys.modules', {'psutil': fake}), \
         patch.object(api, '_OWNED_PIDS', {80}), \
         patch.dict(api._DRV, d=None, inbox_handle=None):
        api._shutdown()
    assert alvo.killed
    assert not api._OWNED_PIDS, "lista de PIDs deve ficar vazia apos shutdown"


def test_idle_watch_desligado_por_padrao_e_fecha_quando_ocioso():
    with patch.object(api, 'IDLE_SHUTDOWN_S', 0):
        assert api._start_idle_watch() is None  # opt-in: default nao interfere
    with patch.object(api, 'IDLE_SHUTDOWN_S', 1), patch.object(api, '_shutdown') as shut, \
         patch.dict(api._DRV, d=Mock()), patch.dict(api.sessions, {}, clear=True):
        api._IDLE["since"] = 0.0
        stop = threading.Event()
        t = threading.Thread(target=api._idle_watch_loop, args=(stop,), daemon=True)
        t.start()
        try:
            for _ in range(40):
                if shut.called:
                    break
                time.sleep(0.25)
        finally:
            stop.set()
            t.join(3)
    assert shut.called


def test_idle_watch_nao_fecha_com_sessao_ativa():
    with patch.object(api, 'IDLE_SHUTDOWN_S', 1), patch.object(api, '_shutdown') as shut, \
         patch.dict(api._DRV, d=Mock()), \
         patch.dict(api.sessions, {"s": {"email": "a@b.c"}}, clear=True):
        api._IDLE["since"] = 0.0
        stop = threading.Event()
        t = threading.Thread(target=api._idle_watch_loop, args=(stop,), daemon=True)
        t.start()
        try:
            time.sleep(3)
        finally:
            stop.set()
            t.join(3)
    assert not shut.called


def _drain_queue(timeout=6):
    """Roda o worker de exclusao ate a fila esvaziar ou estabilizar."""
    stop = threading.Event()
    t = threading.Thread(target=api._delete_worker_loop, args=(stop,), daemon=True)
    t.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not api._DELETE_QUEUE:
            break
        time.sleep(0.05)
    stop.set()
    t.join(5)


def test_delete_falho_volta_para_fila_e_nao_vaza_vaga():
    """Cota do smailpro e de 5 simultaneas: descartar delete falho prende a vaga."""
    d = Mock()
    d.execute_script.return_value = 1
    api._DELETE_RETRIES.clear()
    api._DELETE_QUEUE.append("presa@example.test")
    with patch.dict(api._DRV, d=d), \
         patch.object(api, "_page_delete",
                      return_value={"ok": False, "error": "caixa ainda listada"}) as delete:
        _drain_queue(timeout=2)
        assert delete.called, "worker precisa tentar excluir no provedor"
        assert "presa@example.test" in list(api._DELETE_QUEUE), \
            "delete falho nao pode sumir: a vaga ficaria ocupada para sempre"
    api._DELETE_QUEUE.clear()
    api._DELETE_RETRIES.clear()


def test_delete_desiste_apos_limite_sem_loop_infinito():
    d = Mock()
    d.execute_script.return_value = 1
    api._DELETE_RETRIES.clear()
    api._DELETE_QUEUE.append("ruim@example.test")
    with patch.dict(api._DRV, d=d), \
         patch.object(api, "MAX_DELETE_RETRIES", 2), \
         patch.object(api, "_page_delete", return_value={"ok": False, "error": "x"}) as delete:
        _drain_queue(timeout=12)
    assert delete.call_count <= 3, "nao pode retentar para sempre"
    assert not api._DELETE_QUEUE, "apos o limite a fila precisa drenar"
    api._DELETE_RETRIES.clear()


def test_delete_com_driver_fora_nao_descarta_endereco():
    """Sem driver, a exclusao nao acontece: o endereco tem de ficar pendente."""
    api._DELETE_RETRIES.clear()
    api._DELETE_QUEUE.append("offline@example.test")
    with patch.dict(api._DRV, d=None):
        _drain_queue(timeout=2)
    assert "offline@example.test" in list(api._DELETE_QUEUE)
    api._DELETE_QUEUE.clear()
    api._DELETE_RETRIES.clear()


def test_page_delete_reporta_falha_quando_caixa_continua_listada():
    d = Mock()
    d.execute_async_script.return_value = json.dumps(
        {"ok": False, "remaining": 5, "error": "caixa ainda listada"})
    assert api._page_delete(d, "a@b.c")["ok"] is False
    d.execute_async_script.return_value = json.dumps({"ok": True, "remaining": 3})
    ok = api._page_delete(d, "a@b.c")
    assert ok["ok"] is True and ok["remaining"] == 3


def test_free_provider_slots_preserva_caixas_em_uso():
    """Libera apenas caixas sem sessao ativa; nunca derruba caixa em uso."""
    d = Mock()
    boxes = ["a@x.test", "b@x.test", "c@x.test", "d@x.test", "e@x.test"]
    api.sessions.clear()
    api.sessions["s1"] = {"email": "a@x.test"}
    api.sessions["s2"] = {"email": "b@x.test"}
    apagados = []

    def fake_delete(_d, address):
        apagados.append(address)
        return {"ok": True, "remaining": 0}

    with patch.object(api, "_page_delete", side_effect=fake_delete), \
         patch.object(api, "_alp_valid_list", return_value=boxes):
        freed = api._free_provider_slots(d, boxes)
    assert freed >= 1, "precisa liberar ao menos uma vaga"
    assert "a@x.test" not in apagados and "b@x.test" not in apagados, \
        "caixa de sessao ativa nunca pode ser excluida"
    assert set(apagados) <= {"c@x.test", "d@x.test", "e@x.test"}
    api.sessions.clear()


def test_free_provider_slots_nao_faz_nada_abaixo_da_cota():
    d = Mock()
    with patch.object(api, "_page_delete") as delete:
        assert api._free_provider_slots(d, ["a@x.test"]) == 0
    assert not delete.called


def test_create_libera_vaga_quando_provedor_esta_cheio():
    """Cota cheia por caixas de execucoes antigas: create limpa antes de criar."""
    cheio = [f"velha{i}@x.test" for i in range(api.PROVIDER_QUOTA)]
    with TestClient(api.app) as client:
        with patch.object(api, "_driver_locked", return_value=Mock()), \
             patch.object(api, "_alp_valid_list", return_value=cheio), \
             patch.object(api, "_free_provider_slots", return_value=1) as freed, \
             patch.object(api, "_page_create",
                          return_value={"ok": True, "email": "nova@example.test"}):
            response = client.post("/email/create", json={})
        assert response.status_code == 200, response.text
        assert freed.called, "create precisa liberar vaga quando a cota esta cheia"


def test_quota_endpoint_nao_abre_navegador_e_mostra_orfas():
    d = Mock()
    d.execute_script.return_value = 1
    api.sessions.clear()
    api.sessions["s1"] = {"email": "usada@x.test"}
    with patch.dict(api._DRV, d=d), \
         patch.object(api, "_alp_valid_list", return_value=["usada@x.test", "orfa@x.test"]), \
         patch.object(api, "new_driver", side_effect=AssertionError("nao pode abrir navegador")):
        out = api.quota()
    assert out["quota"] == api.PROVIDER_QUOTA
    assert out["provider_boxes"] == 2 and out["free"] == api.PROVIDER_QUOTA - 2
    assert out["orphan_boxes"] == ["orfa@x.test"]
    assert out["stale"] is False
    api.sessions.clear()

    with patch.dict(api._DRV, d=None), \
         patch.object(api, "new_driver", side_effect=AssertionError("nao pode abrir navegador")):
        sem = api.quota()
    assert sem["stale"] is True and sem["free"] is None


def test_new_driver_aplica_flags_de_memoria():
    # _REAL_NEW_DRIVER e capturada no import, antes da fixture bloquear new_driver.
    fake = Mock()
    with patch.object(api, 'Driver', return_value=fake) as ctor, \
         patch.object(api, '_track_driver_procs'):
        _REAL_NEW_DRIVER()
    args = ctor.call_args.kwargs["chromium_arg"].split(",")
    assert set(api._MEM_FLAGS) <= set(args)
    assert ctor.call_args.kwargs["headless"] is True
