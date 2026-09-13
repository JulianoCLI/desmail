import json, threading, urllib.request
from textual import work
from textual.binding import Binding
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, ListItem, ListView, Log, Static

BASE = "http://127.0.0.1:8000"
POLL_S = 15

def req(method, path, data=None, timeout=120):
    r = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(data).encode() if data else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as h:
        return json.loads(h.read().decode())

def clipboard(s):
    import subprocess, shutil
    if shutil.which("clip"):
        subprocess.run(["clip"], input=s.encode(), check=False)
    elif shutil.which("xclip"):
        subprocess.run(["xclip", "-selection", "clipboard"], input=s.encode(), check=False)
    elif shutil.which("xsel"):
        subprocess.run(["xsel", "-ib"], input=s.encode(), check=False)
    else:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(s)
        r.update()
        r.destroy()

def _http_err(e):
    import urllib.error as ue
    if isinstance(e, ue.HTTPError):
        try:
            return e.code, e.read().decode()[:300]
        except Exception:
            return e.code, str(e)[:200]
    return None, str(e)[:300]

class CreateForm(ModalScreen[dict | None]):
    def __init__(self, opts):
        super().__init__()
        self.opts = opts
        self.provider = "google"
    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Label("Email Type (free apenas, resto premium bloqueado):")
            with Horizontal():
                yield Button("Google", id="pg-google", variant="primary")
                yield Button("Microsoft", id="pg-microsoft")
                yield Button("Temp Mail", id="pg-other")
            yield Label("Domain (free):")
            yield Input(value="gmail.com", id="dom")
            yield Label("", id="hint")
            yield Label("Username: Random | Account: Alias | Server: 1 (fixos free)")
            with Horizontal():
                yield Button("Generate", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")
    def on_mount(self):
        self._paint()
    def _paint(self):
        doms = self.opts.get(self.provider, {}).get("domains", [])
        try:
            self.query_one("#hint", Label).update(" | ".join(doms))
        except Exception:
            pass
        if not (self.query_one("#dom", Input).value or "").strip():
            try:
                self.query_one("#dom", Input).value = doms[0] if doms else ""
            except Exception:
                pass
        for k in ("google", "microsoft", "other"):
            try:
                self.query_one(f"#pg-{k}", Button).variant = "primary" if k == self.provider else "default"
            except Exception:
                pass
    def on_button_pressed(self, e: Button.Pressed):
        bid = e.button.id or ""
        if bid.startswith("pg-"):
            self.provider = bid[3:]
            doms = self.opts.get(self.provider, {}).get("domains", [])
            try:
                self.query_one("#dom", Input).value = doms[0] if doms else ""
            except Exception:
                pass
            self._paint()
            return
        if bid == "ok":
            dom = (self.query_one("#dom", Input).value or "").strip().lower()
            doms = self.opts.get(self.provider, {}).get("domains", [])
            if dom not in doms:
                dom = doms[0] if doms else dom
            self.dismiss({"provider": self.provider, "domain": dom, "server": "1"})
        else:
            self.dismiss(None)

def _state_label(d):
    # Etapa 6: Detectado / Link aberto / Confirmado / Falhou.
    if d.get("verified"):
        return "Confirmado"
    if d.get("state") == "in_progress":
        return "Em andamento"
    if d.get("opened"):
        return "Link aberto (sem evidencia)"
    if d.get("link_found"):
        return "Falhou"
    if d.get("body_loaded") is False and not d.get("link_found"):
        return "Falhou"
    return "Detectado"

class DesmailApp(App):
    TITLE = "Desmail 2.3.0"
    CSS = "#left{width:38;}#inbox{height:55%;}#log{height:45%;}#menu{height:3;}#form{padding:2;}"
    BINDINGS = [Binding("enter", "open_message", "Abrir msg", priority=True),
                ("v", "revalidate", "Revalidar msg"),
                ("c", "create", "Criar+copiar"), ("n", "repeat", "Repetir"),
                ("r", "refresh", "Inbox+confirm"),
                ("a", "auto", "Auto-confirm"), ("y", "copy", "Copiar email"),
                ("d", "remove", "Remover"),
                ("1", "f_accts", "Sessoes"), ("2", "f_inbox", "Inbox"),
                ("3", "f_log", "Log"), ("q", "quit", "Sair")]
    def __init__(self):
        super().__init__()
        self.ss: list[dict] = []
        self.last: dict | None = None
        self._clock = threading.Lock()
        self._creating = False
        self._poll_lock = threading.Lock()
        self._cache = {}
        self._versions = {}
        self._sequence = 0
        self._opening = set()
        self._opened = set()
        self._confirming = set()
        self._verified_sessions = set()
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("[c]Criar+copiar [n]Repetir [r]Inbox+confirm [a]Auto-confirm [y]Copiar [Enter]Abrir msg | auto-confirm ON sweep 15s",
                     id="menu")
        with Horizontal():
            with Vertical(id="left"):
                yield Static("Sessoes")
                yield ListView(id="accts")
            with Vertical(id="right"):
                yield DataTable(id="inbox")
                yield Log(id="log", highlight=True)
        yield Footer()
    def on_mount(self):
        t = self.query_one("#inbox", DataTable)
        # Etapa 6: identidade interna (session_id, mid); mostra so prefixo.
        t.add_columns("Conta", "De", "Assunto", "MID", "SID")
        t.cursor_type = "row"
        self.write_log(f"API {BASE} | sweep {POLL_S}s (1 loop p/ todas) + auto-confirm ON")
        self._poll_all()
        self._poll_timer = self.set_interval(POLL_S, self._poll_all)
    def _say(self, s):
        try:
            self.call_from_thread(self.write_log, s)
        except RuntimeError:
            pass
    def _load(self):
        try:
            self.call_from_thread(self._poll_all)
        except RuntimeError:
            if self.is_running:
                self._poll_all()
    def action_create(self):
        try:
            opts = req("GET", "/options")
        except Exception as e:
            self.write_log(f"API fora: {e}")
            return
        self.push_screen(CreateForm(opts), self._on_create)
    def _on_create(self, res):
        if not res:
            return
        self.last = res
        self._try_create(res, repeat=False)
    def action_repeat(self):
        if not self.last:
            self.write_log("nada p/ repetir: c cria primeiro")
            return
        self._try_create(self.last, repeat=True)
    def _try_create(self, res, repeat):
        with self._clock:
            if self._creating:
                self.write_log("criacao em andamento, aguarde...")
                return
            self._creating = True
        self.write_log(f"{'repetindo' if repeat else 'criando'} {res['provider']} {res['domain']}...")
        threading.Thread(target=self._do_create, args=(res,), daemon=True).start()
    def _do_create(self, res):
        try:
            try:
                j = req("POST", "/email/create", res, timeout=120)
            except Exception as e:
                code, detail = _http_err(e)
                self._say(f"falha {code or ''}: {detail} (tente n p/ repetir)".strip())
                return
            try:
                clipboard(j["email"])
                cp = " (copiado)"
            except Exception as e:
                cp = f" (clip falhou: {e})"
            self._say(f"OK {j['email']} sid={j['session_id']}{cp}")
            self._load()
        finally:
            with self._clock:
                self._creating = False
    def action_copy(self):
        s = self.sel()
        if not s:
            self.write_log("sem sessao")
            return
        try:
            clipboard(s["email"])
            self.write_log(f"{s['email']} copiado")
        except Exception as e:
            self.write_log(f"clip falhou: {e}")
    def sel(self) -> dict | None:
        try:
            lv = self.query_one("#accts", ListView)
            if lv.index is not None and 0 <= lv.index < len(self.ss):
                return self.ss[lv.index]
        except Exception:
            pass
        return self.ss[0] if self.ss else None
    def _sel_sid(self):
        s = self.sel()
        return s["session_id"] if s else None
    def action_refresh(self):
        # Manual e timer compartilham single flight; render antes do POST confirm.
        s = self.sel()
        if not s:
            self.write_log("sem sessao: c cria")
            return
        self._poll_all(s["session_id"])
    @work()
    async def _poll_all(self, sid=None):
        if not self._poll_lock.acquire(blocking=False):
            return
        try:
            await self._poll_cycle(sid)
        except NoMatches:
            if self.is_running:
                raise
        finally:
            self._poll_lock.release()
    async def _poll_cycle(self, sid=None):
        self._sequence += 1
        version = self._sequence
        try:
            ss = await self._to_thread(req, "GET", "/sessions", None, 30)
            await self.refresh_accts(ss)
            for s in ss:
                evidence = s.get('confirmation') or {}
                if evidence.get('verified_at') and s['session_id'] not in self._verified_sessions:
                    self._verified_sessions.add(s['session_id'])
                    self.write_log(f"{s['email']}: Confirmado [{evidence['verified_at']}] {evidence['evidence']}")
        except Exception as e:
            self.write_log(f"sessoes: {e}")
            for s in self.ss:
                self._accept(s, {"state": "error", "error": _http_err(e)[1]}, version)
            self._render_cache()
            return
        selected = [s for s in ss if sid is None or s["session_id"] == sid]
        if not selected:
            return
        try:
            path = "/email/poll" if sid else "/email/sweep"
            payload = {"wait_s": 6, "auto_confirm": False}
            if sid:
                payload["session_id"] = sid
            sw = await self._to_thread(req, "POST", path, payload, 300)
            if sw.get("skipped"):
                return
            results = [dict(sw, session_id=sid)] if sid else sw["results"]
            by_sid = {r["session_id"]: r for r in results}
            for s in selected:
                self._accept(s, by_sid.get(s["session_id"], {"state": "missing", "error": "resultado ausente"}), version)
        except Exception as e:
            _, detail = _http_err(e)
            for s in selected:
                self._accept(s, {"state": "error", "error": detail}, version)
        self._render_cache()
        self._auto_confirm(selected)
    @work()
    async def _auto_confirm(self, selected):
        for s in selected:
            sid = s["session_id"]
            if sid in self._confirming:
                continue
            if self._cache.get(s["session_id"], {}).get("state") not in ("ok", "empty"):
                continue
            self._confirming.add(sid)
            self.write_log(f"{s['email']}: confirmacao em andamento...")
            try:
                j = await self._to_thread(req, "POST", "/email/autoconfirm",
                                          {"session_id": s["session_id"], "wait_s": 6}, 300)
                for d in j.get("confirmed", []):
                    self.write_log(f"{s['email']} {_state_label(d)} [{d.get('status')}]")
                if j.get("skipped"):
                    self.write_log("confirmacao ocupada; proximo ciclo continua")
            except Exception as e:
                self.write_log(f"confirmacao: {_http_err(e)[1]}")
            finally:
                self._confirming.discard(sid)
    def _accept(self, s, result, version):
        sid = s["session_id"]
        if sid not in {x["session_id"] for x in self.ss} or version < self._versions.get(sid, -1):
            return
        self._versions[sid] = version
        if result.get("skipped"):
            return
        state = result.get("state", "error")
        if state in ("ok", "empty") and isinstance(result.get("messages"), list):
            self._cache[sid] = dict(result, stale=False)
        else:
            self._cache[sid] = dict(self._cache.get(sid, {"messages": []}),
                                    state=state, error=result.get("error", "resposta invalida"), stale=True)
    def _render_cache(self):
        t = self.query_one("#inbox", DataTable)
        try:
            selected = t.coordinate_to_cell_key(t.cursor_coordinate).row_key
        except Exception:
            selected = None
        t.clear()
        for s in self.ss:
            if s["session_id"] in self._cache:
                self._render(s, self._cache[s["session_id"]])
        for index in range(t.row_count):
            if t.coordinate_to_cell_key((index, 0)).row_key == selected:
                t.move_cursor(row=index)
                break
    async def _to_thread(self, fn, *a):
        import asyncio
        task = asyncio.create_task(asyncio.to_thread(fn, *a))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancelamento nao encerra urllib: manter single flight ate a thread sair.
            try:
                await task
            finally:
                raise
    def action_auto(self):
        s = self.sel()
        if not s:
            self.write_log("sem sessao: c cria")
            return
        self.write_log(f"auto-confirm {s['email']}...")
        self._poll_all(s["session_id"])
    def _render(self, s, j):
        # Etapa 6: identidade (session_id, mid); exibe so prefixo.
        t = self.query_one("#inbox", DataTable)
        conta = s["email"].split("@")[0]
        msgs = j.get("messages", []) or []
        if j.get("stale"):
            self.write_log(f"{s['email']}: {j.get('state')} — dados obsoletos ({j.get('error', '')})")
        for m in msgs:
            key = (str(s["session_id"]), str(m.get("mid") or ""))
            if key in t.rows:
                continue
            t.add_row(conta, (m.get("from") or "")[:40], (m.get("subject") or "")[:70],
                      key[1], key[0], key=key)
        # Etapa 6: "caixa vazia" x "falha ao consultar".
        if not msgs and not j.get("stale"):
            if j.get("_fetch_error"):
                self.write_log(f"{s['email']}: falha ao consultar ({j['_fetch_error']})")
            else:
                self.write_log(f"{s['email']}: caixa vazia")
        elif msgs:
            self.write_log(f"{s['email']}: {len(msgs)} msg(s)")
    def action_remove(self):
        s = self.sel()
        if not s:
            return
        threading.Thread(target=self._do_remove, args=(s,), daemon=True).start()
    def _do_remove(self, s):
        try:
            req("DELETE", f"/email/{s['session_id']}", timeout=60)
            self._say(f"{s['email']} removida")
        except Exception as e:
            self._say(f"rm falhou: {e}")
            return
        self._load()
    def on_data_table_row_selected(self, e: DataTable.RowSelected):
        self._open_key(e.row_key)
    def check_action(self, action, parameters):
        if action in ("open_message", "revalidate") and isinstance(self.screen, ModalScreen):
            return False
        return True
    def action_open_message(self):
        try:
            t = self.query_one("#inbox", DataTable)
            self._open_key(t.coordinate_to_cell_key(t.cursor_coordinate).row_key)
        except Exception:
            self.write_log("sem mensagem selecionada")
    def action_revalidate(self):
        t = self.query_one("#inbox", DataTable)
        if t.row_count:
            self._open_key(t.coordinate_to_cell_key(t.cursor_coordinate).row_key, True)
    def _open_key(self, row_key, revalidate=False):
        t = self.query_one("#inbox", DataTable)
        if row_key not in t.rows:
            self.write_log("mensagem antiga; selecione novamente")
            return
        sid, mid = row_key.value
        if not mid or not sid:
            return
        s = next((x for x in self.ss if x.get("session_id") == sid), None)
        if not s:
            return
        key = (sid, mid)
        if key in self._opening or (key in self._opened and not revalidate):
            self.write_log("mensagem em andamento ou ja aberta; v revalida com cooldown")
            return
        self._opening.add(key)
        self.write_log("carregando corpo / aguardando driver...")
        threading.Thread(target=self._do_open_msg, args=(s, mid, revalidate), daemon=True).start()
    def _do_open_msg(self, s, mid, revalidate=False):
        key = (s["session_id"], mid)
        try:
            j = req("POST", "/email/open", dict(session_id=key[0], mid=mid, revalidate=revalidate), timeout=120)
            if j.get("opened"):
                self._opened.add(key)
            self._say(f"{_state_label(j)}: {j.get('status') or j.get('error') or j.get('state', 'unknown')}")
        except Exception as e:
            self._say(f"body/abertura falhou: {_http_err(e)[1]}")
        finally:
            self._opening.discard(key)
    async def refresh_accts(self, ss, keep=None):
        # Etapa 6: preserva selecao da lateral durante refresh.
        prev = keep
        if prev is None:
            try:
                cur = self.sel()
                prev = cur["session_id"] if cur else None
            except Exception:
                prev = None
        self.ss = ss
        self._cache = {sid: r for sid, r in self._cache.items() if sid in {s['session_id'] for s in ss}}
        if [s['session_id'] for s in ss] == getattr(self, '_account_ids', None):
            return
        self._account_ids = [s['session_id'] for s in ss]
        lv = self.query_one("#accts", ListView)
        await lv.clear()
        idx = 0
        for i, s in enumerate(ss):
            await lv.append(ListItem(Label(s["email"])))
            if prev and s.get("session_id") == prev:
                idx = i
        try:
            lv.index = idx if ss else None
        except Exception:
            pass
        if not ss:
            self.write_log("vazio: c cria")
        self._render_cache()
    def action_f_accts(self):
        self.query_one("#accts", ListView).focus()
    def action_f_inbox(self):
        self.query_one("#inbox", DataTable).focus()
    def action_f_log(self):
        self.query_one("#log", Log).focus()
    def write_log(self, s: str):
        self.query_one("#log", Log).write_line(s)

if __name__ == "__main__":
    DesmailApp().run()
