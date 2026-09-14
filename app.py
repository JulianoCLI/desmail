import atexit
import asyncio
from contextlib import asynccontextmanager
import ipaddress
import json
import os
import quopri
import re
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from collections import deque
from html.parser import HTMLParser
from urllib.parse import parse_qsl, unquote, urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from seleniumbase import Driver
from typing import Literal

@asynccontextmanager
async def lifespan(application):
    if getattr(application.state, 'worker', None) is not None and application.state.worker.is_alive():
        raise RuntimeError('worker ja iniciado neste processo')
    reap_orphan_drivers()
    stop = threading.Event()
    worker = threading.Thread(target=_worker_loop, args=(stop,), name="desmail-monitor")
    application.state.worker = worker
    worker.start()
    idle_stop = _start_idle_watch()
    try:
        yield
    finally:
        stop.set()
        if idle_stop is not None:
            idle_stop.set()
        await asyncio.to_thread(worker.join)
        ds = _DELETE_WORKER.get("stop")
        if ds is not None:
            ds.set()
        with _LOCK:
            _shutdown()


app = FastAPI(version="2.3.0", lifespan=lifespan)
# sid -> {email, provider, domain, seen:set, opened:set, verified:set,
#         attempts:dict, status, error}
sessions = {}
_DRV = {"d": None, "inbox_handle": None, "status": "ok", "error": ""}
_LOCK = threading.RLock()
# Exclusao real: somente token da operacao pode liberar o guard.
_POLL_MTX = threading.Lock()
_POLL_STATE = {"busy": False, "owner": None, "since": 0.0}
_EVENTS = deque(maxlen=500)
MAX_BOXES = 4
MAX_ATTEMPTS = 3
RETRY_COOLDOWN_S = 30
POLL_INTERVAL_S = 15
# Fila de exclusão assíncrona: release responde sem esperar o provedor.
_DELETE_QUEUE = deque()
_DELETE_WORKER = {"thread": None, "stop": None}
_DELETE_RETRIES = {}
MAX_DELETE_RETRIES = 5
# Cota do smailpro: 5 caixas SIMULTANEAS por IP. Excluir devolve a vaga, entao
# manter menos caixas vivas que o teto deixa folga para o create seguinte.
PROVIDER_QUOTA = 5
# Fecha o Chrome apos N segundos sem sessao ativa. 0 = nunca (opt-in).
IDLE_SHUTDOWN_S = int(os.environ.get("DESMAIL_IDLE_SHUTDOWN_S", "0") or 0)
_IDLE = {"since": 0.0}
# PIDs do chrome/uc_driver desta instancia: garante encerrar so o que abrimos.
_OWNED_PIDS = set()
_DRIVER_PROC_NAMES = ("uc_driver.exe", "chromedriver.exe", "uc_driver", "chromedriver")

URL = "https://smailpro.com/temporary-email"
BARE_RE = re.compile(r"https?://[^\s<>\")']+", re.I)
NOISE = ("smailpro.com", "cloudflare.com", "sonjj.com", "w3.org", "schema.org",
         "facebook.com", "twitter.com", "youtube.com", "t.me", "amazon.com",
         "google.com/maps", "play.google.com", "chrome.google.com",
         "ychecker.com", "ugener.com", "cardgener.com", "apps.apple.com",
         "microsoftedge", "mozilla.org", "amazon.", "analytics.",
         "buysellads", "unpkg.com", "cloudflareinsights", "alpinejs")
CONFIRM_RE = re.compile(r"confirm|verify|verifica|activat", re.I)
_EXCLUDE_RE = re.compile(r"unsubscribe|opt-?out|optout|reset|forgot|recover|login|signin|sign-in|payment|pagamento|logo", re.I)

FREE = {
    "google": {"label": "Google", "domains": ["gmail.com", "googlemail.com"],
               "servers": [{"name": "server-1", "pcs": 641, "free": True}]},
    "microsoft": {"label": "Microsoft", "domains": ["outlook.com", "hotmail.com"],
                  "servers": [{"name": "server-1", "pcs": 3729, "free": True}]},
    "other": {"label": "Temp Mail", "domains": ["random.com", "singapore.edu.pl",
              "paris.edu.pl", "france.edu.pl"], "servers": []},
}
PREMIUM_DOMAINS = {"deweyart.com", "mesetar.com", "sambolias.com", "gurantan.com",
                   "amiced.com", "spyboys.com", "dinwold.net", "nakeit.com",
                   "newszig.com", "sousor.com", "bakulab.com", "amokrun.com", "tomspal.com"}

PROVIDERS = {"google", "microsoft", "other"}


class CreateReq(BaseModel):
    provider: str = "google"
    domain: str = "gmail.com"
    server: str = "1"
    auto_confirm: bool = Field(default=True, strict=True)
    expected_hosts: list[str] | None = Field(default=None, min_length=1, max_length=20)
    confirm_host: str | None = Field(default=None, min_length=3, max_length=253)
    success_text: str | None = Field(default=None, min_length=8, max_length=300)
    timeout_seconds: int = Field(default=600, ge=1, le=3600, strict=True)

    @field_validator("expected_hosts")
    @classmethod
    def validate_hosts(cls, hosts):
        if hosts is None:
            return None
        normalized = []
        for host in hosts:
            host = host.lower()
            if (len(host) > 253 or '.' not in host or not _ok_url('https://' + host)
                    or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                           for label in host.split('.'))):
                raise ValueError('expected_hosts exige dominios DNS exatos, sem URL/porta/wildcard/IP')
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError('expected_hosts nao aceita IP')
            normalized.append(host)
        return list(dict.fromkeys(normalized))

    @field_validator("confirm_host")
    @classmethod
    def validate_confirm_host(cls, host):
        if host is None:
            return None
        host = host.lower()
        if (len(host) > 253 or '.' not in host or not _ok_url('https://' + host)
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                       for label in host.split('.'))):
            raise ValueError('confirm_host exige dominio DNS exato, sem URL/porta/wildcard/IP')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError('confirm_host nao aceita IP')
        return host


class ConfirmReq(BaseModel):
    session_id: str
    wait_s: int = 6
    delete_after: bool = False


class OpenReq(BaseModel):
    session_id: str = Field(min_length=1)
    mid: str = Field(min_length=1)
    wait_s: int = Field(default=6, ge=0, le=30)
    revalidate: bool = False
    link: str | None = Field(default=None, min_length=10, max_length=2048)


class PollReq(BaseModel):
    session_id: str
    wait_s: int = 6
    auto_confirm: bool = True


class SweepReq(BaseModel):
    wait_s: int = 6
    auto_confirm: bool = True


class ReleaseReq(BaseModel):
    session_id: str = Field(min_length=1)


@app.get("/")
def root():
    return {"ok": True, "api_version": app.version, "single_driver": True, "max_boxes": MAX_BOXES,
            "routes": ["GET /options", "POST /email/create",
                       "GET /sessions", "GET /quota",
                       "GET /email/check", "GET /email/status",
                       "POST /email/poll", "GET /email/body",
                        "GET /email/debug", "GET /email/events", "POST /email/open",
                       "POST /email/autoconfirm", "POST /email/sweep",
                       "POST /email/release",
                       "DELETE /email/{sid}", "POST /admin/shutdown",
                       "GET /test/local-confirm"]}


@app.get("/options")
def options():
    return FREE


@app.get("/test/local-confirm", response_class=HTMLResponse)
def local_confirm():
    # Pagina local para testar isolamento de abas sem consumir tokens reais.
    return ("<html><head><title>Confirm</title></head><body>"
            "<h1>Email confirmado com sucesso</h1>"
            "<p>Conta confirmada.</p></body></html>")


# ---------- utilidades puras (testaveis sem navegador) ----------

def _valid_addr(a):
    return bool(a) and "@" in a and not a.lower().startswith("random@") \
        and "." in a.split("@")[-1]


def _pick_new(before, after):
    """Retorna o mais novo endereco valido que nao estava em before."""
    old = set(before or [])
    fresh = [a for a in (after or []) if _valid_addr(a) and a not in old]
    return fresh[-1] if fresh else ""


def _decode_qp_if_needed(s, encoding=""):
    if not s or encoding.lower() != "quoted-printable":
        return s or ""
    try:
        return quopri.decodestring(s.encode("utf-8")).decode("utf-8")
    except Exception:
        return s


class _HrefParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []
        self.text_urls = []
        self.labels = {}
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        self.anchor = None
        for k, v in attrs:
            if k.lower() == "href" and v and v.lower().startswith("http"):
                self.hrefs.append(v)
                self.anchor = v
                self.labels.setdefault(v, "")

    def handle_endtag(self, tag):
        if tag.lower() == "a":
            self.anchor = None

    def handle_data(self, data):
        self.text_urls.extend(m.group(0) for m in BARE_RE.finditer(data))
        if self.anchor:
            self.labels[self.anchor] += data


def _normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text)
                   if not unicodedata.combining(c)).lower()


def _ok_url(u):
    if not isinstance(u, str) or any(c.isspace() or ord(c) < 32 or c == '\\' for c in u):
        return False
    try:
        p = urlparse(u)
        host = (p.hostname or "").lower().rstrip('.')
        p.port  # Rejeita porta invalida antes de qualquer navegacao.
        if p.scheme not in ("http", "https") or not host or '@' in p.netloc:
            return False
        if host == 'localhost' or host.endswith(('.localhost', '.local')):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            # Browser aceita IPv4 abreviado/decimal/hex; rejeitar formas alternativas.
            if not re.fullmatch(r'[a-z0-9.-]+', host) or not re.search(r'[a-z]', host.split('.')[-1]) or host.startswith('0x'):
                return False
    except Exception:
        return False
    return not any(n in host for n in NOISE)


def _is_homepage(u):
    try:
        p = urlparse(u)
    except Exception:
        return True
    return (p.path or "/") == "/" and not p.query


def _is_excluded(u):
    p = urlparse(u)
    return bool(_EXCLUDE_RE.search(_normalized(unquote(p.path + ';' + p.params + '?' + p.query))))


def _extract_links(body, encoding="", confirmation_only=False):
    """Decodifica transporte explicitamente; HTML resolve entidades uma vez."""
    par = _HrefParser()
    par.feed(_decode_qp_if_needed(body or "", encoding))
    par.close()
    cands = par.hrefs + par.text_urls
    seen, out = set(), []
    for u in cands:
        if u and u not in seen and _ok_url(u):
            seen.add(u)
            if not confirmation_only or _confirmation_candidate(u, par.labels.get(u, '')):
                out.append(u)
    return out


def _confirmation_candidate(u, label=''):
    if not _ok_url(u) or _is_homepage(u) or _is_excluded(u) or _EXCLUDE_RE.search(_normalized(label)):
        return False
    p = urlparse(u)
    query = parse_qsl(p.query, keep_blank_values=True)
    tokens = [v for k, v in query if k == 'verify_email_token']
    if tokens and (len(tokens) != 1 or not tokens[0].strip()):
        return False
    return bool(CONFIRM_RE.search(_normalized(label + ' ' + unquote(p.path + '?' + p.query))))


def _select_link(cands):
    """URL unica, vazio sem evidencia, None em empate; nunca reserializa token."""
    links = list(dict.fromkeys(u for u in cands or [] if _confirmation_candidate(u)))
    return links[0] if len(links) == 1 else (None if links else '')


def _redact_url(u):
    # Tokens genericos podem estar no caminho, query ou fragmento.
    try:
        p = urlparse(u or "")
        if not p.netloc:
            return ""
        return f"{p.scheme}://{p.hostname}/..."
    except Exception:
        return ""


_TOKEN_RE = re.compile(r'(?i)(verify_email_token|token|key|code)=[^\s&]+')
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')


def _redact_text(s):
    """Remove tokens de query e enderecos de e-mail de texto arbitrario de pagina externa."""
    if not isinstance(s, str):
        return ''
    s = _TOKEN_RE.sub(r'\1=[REDACTED]', s)
    s = _EMAIL_RE.sub('[EMAIL]', s)
    return s


def _confirm_targets(s, msgs, include_cooldown=False):
    return [m for m in (msgs or [])
             if m.get("mid") not in s.get("opened", set())
             and not s.get("attempts", {}).get(m.get("mid"), {}).get("navigation_attempted")
            and m.get("mid") not in s.get("verified", set())
            and s.get("attempts", {}).get(m.get("mid"), {}).get("count", 0) < MAX_ATTEMPTS
             and s.get("attempts", {}).get(m.get("mid"), {}).get("state") not in ('ambiguous', 'blocked')
             and (include_cooldown or time.monotonic() >= s.get("attempts", {}).get(m.get("mid"), {}).get("next_retry", 0))]


# ---------- eventos de diagnostico (Etapa 1, sem segredos) ----------

def _log_event(session_id="", mid="", stage="", result="", removal_reason="", elapsed_ms=None,
               confirmation=None, snapshot=None):
    # Somente memoria: diagnostico nunca espera WebDriver/rede.
    _EVENTS.append({"t": time.time(), "sid": session_id, "mid": str(mid or ""),
                    "stage": stage, "result": str(result or "")[:200],
                    "inbox_handle": _DRV.get("inbox_handle"), "handles": _DRV.get("handles"),
                    "origin": _DRV.get("origin", ""), "path": "",
                    "elapsed_ms": elapsed_ms,
                    "removal_reason": removal_reason,
                    **({'confirmation': dict(confirmation)} if confirmation is not None else {}),
                    **(snapshot or {})})


def _on_driver_failure(reason):
    # Falha do driver preserva sessoes como desconectadas. Nunca limpa.
    for s in sessions.values():
        s["status"] = "disconnected"
        s["error"] = reason
    _DRV["d"] = None
    _DRV["inbox_handle"] = None
    _DRV["status"] = "error"
    _DRV["error"] = reason
    _log_event(stage="driver-failure", result=reason,
               removal_reason="sessoes preservadas como desconectadas")


# ---------- ciclo de vida dos processos do navegador ----------

def _iter_driver_procs():
    """uc_driver/chromedriver visiveis, com PID e PID do pai."""
    try:
        import psutil
    except Exception:
        return []
    out = []
    for p in psutil.process_iter(["pid", "ppid", "name"]):
        try:
            if (p.info.get("name") or "") in _DRIVER_PROC_NAMES:
                out.append(p)
        except Exception:
            continue
    return out


def _is_automation_chrome(proc):
    """Chrome raiz iniciado por automacao (tem --remote-debugging-port).

    O navegador pessoal do usuario nunca recebe essa flag: sem ela, nao tocar."""
    try:
        cmd = proc.cmdline()
    except Exception:
        return False
    if not cmd or not any("--remote-debugging-port" in a for a in cmd):
        return False
    return not any(a.startswith("--type=") for a in cmd)  # so o processo raiz


def reap_orphan_drivers():
    """Mata uc_driver/chromedriver (e chrome de automacao) com pai ja morto.

    Processo orfao segue segurando RAM e porta TCP apos a API ser encerrada a
    forca. Com pai vivo, e de outra instancia legitima: preservar."""
    try:
        import psutil
    except Exception:
        return 0

    def kill_tree(proc):
        kids = []
        try:
            kids = proc.children(recursive=True)
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            return False
        for c in kids:
            try:
                c.kill()
            except Exception:
                pass
        return True

    killed = 0
    for p in _iter_driver_procs():
        try:
            if p.pid in _OWNED_PIDS:
                continue
            ppid = p.info.get("ppid")
            if ppid and psutil.pid_exists(ppid):
                continue  # pai vivo: driver de outra instancia, preservar
            killed += bool(kill_tree(p))
        except Exception:
            continue
    # Chrome de automacao cujo driver morreu: os renderers filhos ficam vivos
    # pendurados nele, entao so matar o driver nao libera a RAM.
    for p in psutil.process_iter(["pid", "ppid", "name"]):
        try:
            if (p.info.get("name") or "") not in ("chrome.exe", "chrome"):
                continue
            if p.pid in _OWNED_PIDS:
                continue
            ppid = p.info.get("ppid")
            if ppid and psutil.pid_exists(ppid):
                continue
            if not _is_automation_chrome(p):
                continue  # chrome pessoal do usuario: nunca encerrar
            killed += bool(kill_tree(p))
        except Exception:
            continue
    if killed:
        _log_event(stage="reap-orphans", result=f"{killed} processo(s) orfao(s) encerrado(s)")
    return killed


def _track_driver_procs(d=None):
    """Registra PIDs de chrome/uc_driver desta instancia.

    O UC Mode lanca o Chrome antes de acoplar o driver, entao `service.process`
    costuma vir vazio; os processos aparecem como filhos deste Python."""
    try:
        import psutil
    except Exception:
        return
    names = set(_DRIVER_PROC_NAMES) | {"chrome.exe", "chrome"}
    try:
        for c in psutil.Process().children(recursive=True):
            try:
                if c.name() in names:
                    _OWNED_PIDS.add(c.pid)
            except Exception:
                continue
    except Exception:
        pass
    try:
        pid = d.service.process.pid  # caminho direto quando disponivel
    except Exception:
        pid = None
    if pid:
        try:
            proc = psutil.Process(pid)
            _OWNED_PIDS.add(pid)
            for c in proc.children(recursive=True):
                _OWNED_PIDS.add(c.pid)
        except Exception:
            pass


def _kill_owned_procs():
    """Encerra chrome/driver desta instancia. Fallback do quit() educado."""
    try:
        import psutil
    except Exception:
        _OWNED_PIDS.clear()
        return
    for pid in list(_OWNED_PIDS):
        try:
            p = psutil.Process(pid)
            for c in p.children(recursive=True):
                try:
                    c.kill()
                except Exception:
                    pass
            p.kill()
        except Exception:
            pass
    _OWNED_PIDS.clear()


# ---------- driver unico ----------

# Flags de memoria. Economia de RAM nunca pode custar a confirmacao: o mesmo
# Chrome que le o inbox tambem abre a pagina de confirmacao, que pode exigir
# recursos graficos. Flags de particionamento de renderer (--process-per-site,
# site-per-process) foram medidas e NAO tiveram efeito: ficam de fora.
#
# Removidas apos teste (2026-09), todas por quebrarem o destino:
#   --blink-settings=imagesEnabled=false : altera o fingerprint do renderer e
#       faz o Cloudflare Turnstile do smailpro rejeitar o token.
#   --disable-gpu, --disable-software-rasterizer, --disable-accelerated-2d-canvas
#       : derrubam o WebGL. A pagina de confirmacao (pokepixel) exige WebGL e
#       para em "Your browser does not support WebGL" sem consumir o token.
#       Economizavam ~83MB; a confirmacao vale mais que os 83MB.
_MEM_FLAGS = (
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
    "--mute-audio",
    "--renderer-process-limit=2",
)


def new_driver():
    # ponytail: sem uc_gui_click_captcha (PyAutoGUI nao funciona headless);
    # o Turnstile invisivel da pagina resolve sozinho no fluxo generate()
    d = Driver(uc=True, headless=True, chromium_arg=",".join(_MEM_FLAGS))
    _track_driver_procs(d)
    d.uc_open_with_reconnect(URL, reconnect_time=4)
    d.sleep(3)
    return d


def _ensure_connected(d):
    """True se o WebDriver responde, reconectando se a sessao caiu.

    UC Mode derruba a sessao WebDriver de proposito em algumas navegacoes
    (CDP Mode). O Chrome continua vivo: `reconnect()` recria a sessao e os
    handles das abas permanecem validos. Sem isso, uma simples desconexao
    seria confundida com navegador morto."""
    if d is None:
        return False
    try:
        return d.execute_script("return 1") == 1
    except Exception:
        pass
    try:
        d.reconnect()
    except Exception:
        return False
    try:
        return d.execute_script("return 1") == 1
    except Exception:
        return False


def _driver_locked(create=False):
    # 1 chrome para todas as sessoes. Presume _LOCK adquirido.
    # Nunca limpa sessoes: em falha marca desconectadas.
    d = _DRV.get("d")
    if d is not None:
        try:
            if _ensure_connected(d):
                return d
            raise RuntimeError("health-check invalido")
        except Exception as e:
            try:
                d.quit()
            except Exception:
                pass
            _on_driver_failure(f"health-check:{str(e)[:120]}")
    if not create:
        raise HTTPException(503, "driver desconectado; consulta nao recria navegador")
    d = new_driver()
    _DRV["d"] = d
    _DRV["status"] = "ok"
    _DRV["error"] = ""
    try:
        _DRV["inbox_handle"] = d.current_window_handle
    except Exception:
        _DRV["inbox_handle"] = None
    _log_event(stage="driver-create", result="ok")
    return d


def _shutdown():
    d = _DRV.get("d")
    _DRV["d"] = None
    _DRV["inbox_handle"] = None
    if d is not None:
        try:
            d.quit()
        except Exception:
            pass
    # Garante que nada desta instancia fique orfao (quit pode falhar).
    _kill_owned_procs()


_IDLE_TICK_S = 1.0


def _idle_watch_loop(stop):
    """Fecha o Chrome apos IDLE_SHUTDOWN_S sem sessao. Reabre no proximo create."""
    while not stop.wait(_IDLE_TICK_S):
        try:
            if not IDLE_SHUTDOWN_S or _DRV.get("d") is None:
                _IDLE["since"] = 0.0
                continue
            if sessions:
                _IDLE["since"] = 0.0
                continue
            now = time.monotonic()
            if not _IDLE["since"]:
                _IDLE["since"] = now
                continue
            if now - _IDLE["since"] < IDLE_SHUTDOWN_S:
                continue
            if not _LOCK.acquire(blocking=False):
                continue
            try:
                if not sessions and _DRV.get("d") is not None:
                    _shutdown()
                    _log_event(stage="idle-shutdown",
                               result=f"chrome fechado apos {IDLE_SHUTDOWN_S}s ocioso")
                _IDLE["since"] = 0.0
            finally:
                _LOCK.release()
        except Exception:
            pass


def _start_idle_watch():
    if not IDLE_SHUTDOWN_S:
        return None
    stop = threading.Event()
    threading.Thread(target=_idle_watch_loop, args=(stop,),
                     name="desmail-idle", daemon=True).start()
    return stop


def _delete_worker_loop(stop):
    # Exclusão em segundo plano: nunca bloqueia create/poll.
    # Cada caixa nao excluida ocupa 1 das 5 vagas do smailpro, entao falha
    # volta para a fila em vez de ser descartada: descartar vaza a cota.
    while not stop.wait(0.5):
        try:
            address = _DELETE_QUEUE.popleft()
        except IndexError:
            continue
        try:
            if not _LOCK.acquire(blocking=False):
                _DELETE_QUEUE.appendleft(address)
                continue
            done = False
            try:
                d = _DRV.get("d")
                if d is not None and d.execute_script("return 1") == 1:
                    done = bool(_page_delete(d, address).get("ok"))
            except Exception:
                done = False
            finally:
                _LOCK.release()
            if not done:
                # Driver fora do ar ou delete rejeitado: tenta de novo depois.
                tries = _DELETE_RETRIES.get(address, 0) + 1
                if tries <= MAX_DELETE_RETRIES:
                    _DELETE_RETRIES[address] = tries
                    _DELETE_QUEUE.append(address)
                    stop.wait(2)
                else:
                    _DELETE_RETRIES.pop(address, None)
                    _log_event(stage="provider-delete",
                               result="desistiu apos %d tentativas; vaga presa: %s"
                                      % (MAX_DELETE_RETRIES, address))
            else:
                _DELETE_RETRIES.pop(address, None)
        except Exception:
            pass


def _ensure_delete_worker():
    w = _DELETE_WORKER.get("thread")
    if w is not None and w.is_alive():
        return
    stop = threading.Event()
    t = threading.Thread(target=_delete_worker_loop, args=(stop,),
                         name="desmail-delete", daemon=True)
    _DELETE_WORKER["thread"] = t
    _DELETE_WORKER["stop"] = stop
    t.start()


def _enqueue_delete(address):
    if address:
        _DELETE_QUEUE.append(address)
        _ensure_delete_worker()


atexit.register(_shutdown)


def _poll_begin(owner="poll"):
    with _POLL_MTX:
        st = _POLL_STATE
        if st["busy"]:
            return False
        st["busy"] = True
        st["owner"] = owner
        st["since"] = time.monotonic()
        st["token"] = object()
        return st["token"]


def _poll_state_info():
    with _POLL_MTX:
        st = _POLL_STATE
        return {"busy": st["busy"], "owner": st["owner"],
                "age": round(time.monotonic() - st["since"], 1) if st["busy"] else 0.0}


def _poll_skipped():
    # Etapa 5: pulo benigno — 200 com skipped:true, nunca 409.
    # 409 em cascata poluía o log do uvicorn e parecia erro.
    info = _poll_state_info()
    return {"skipped": True, "owner": info["owner"], "age": info["age"]}


def _poll_end(token):
    with _POLL_MTX:
        st = _POLL_STATE
        if token is st.get("token") and st["busy"]:
            st["busy"] = False
            st["owner"] = None
            st["since"] = 0.0
            st["token"] = None


def _expired(s):
    return 'deadline' in s and time.monotonic() >= s['deadline']


def _monitor_state(s):
    if s.get('status') == 'disconnected':
        return 'disconnected'
    if s.get('verified'):
        return 'verified'
    if s.get('opened'):
        return 'opened'
    if s.get('phase') in ('reading_body', 'opening_link'):
        return s['phase']
    if _expired(s):
        return 'expired'
    states = [a.get('state') for a in list(s.get('attempts', {}).values())]
    if 'ambiguous' in states:
        return 'ambiguous'
    if states or s.get('error'):
        return 'unknown'
    return 'waiting_email'


def _monitoring(s):
    if not s.get('auto_confirm') or _expired(s) or _monitor_state(s) in ('opened', 'verified', 'disconnected'):
        return False
    if any(a.get('in_progress') for a in list(s.get('attempts', {}).values())):
        return True
    if any(a.get('navigation_attempted') for a in list(s.get('attempts', {}).values())):
        return False
    messages = s.get('messages') or [{'mid': mid} for mid in s.get('attempts', {})]
    return not messages or bool(_confirm_targets(s, messages, include_cooldown=True))


@app.get('/email/status')
def email_status(session_id: str = Query(...)):
    s = sessions.get(session_id)
    if s is None:
        raise HTTPException(404, 'sessao inexistente')
    state = _monitor_state(s)
    return {'session_id': session_id, 'email': s['email'], 'state': state,
            'auto_confirm': s.get('auto_confirm', False),
            'expected_hosts': s.get('expected_hosts'), 'timeout_seconds': s.get('timeout_seconds', 600),
            'monitoring': _monitoring(s),
            'opened': bool(s.get('opened')), 'verified': bool(s.get('verified')),
            'confirmation': s.get('confirmation'),
            'confirm_host': s.get('confirm_host'), 'success_text': s.get('success_text'),
            'error': s.get('confirm_error') or s.get('error', ''),
            'messages': list(s.get('messages', [])), 'inbox_state': s.get('inbox_state'),
            'expires_at': s.get('expires_at')}


def _worker_tick():
    token = _poll_begin('worker')
    if not token:
        return
    try:
        if not _LOCK.acquire(blocking=False):
            return
        try:
            active = [(sid, s) for sid, s in list(sessions.items())
                      if _monitoring(s)]
            if not active:
                return
            data = _query_inboxes()
            targets = []
            for sid, s in active:
                result = _inbox_result(s, data)
                if result['state'] == 'ok':
                    targets.append((sid, s, result['messages']))
        finally:
            _LOCK.release()
        _confirm_cached(token, targets, 6)
    finally:
        _poll_end(token)


def _worker_loop(stop):
    while not stop.wait(POLL_INTERVAL_S):
        try:
            _worker_tick()
        except Exception as exc:
            for s in list(sessions.values()):
                if s.get('auto_confirm'):
                    s['confirm_error'] = 'worker:' + type(exc).__name__


def _click(d, sel):
    try:
        d.click(sel)
        return True
    except Exception:
        return False


# Usa o proprio componente Alpine create() da pagina: selectEmailType +
# generate(). generate() resolve o Turnstile invisivel e chama
# GET /app/create?username=random&type=alias&domain=&server=1 com x-captcha.
_CREATE_JS = """
var provider = arguments[0];
var domain = arguments[1];
var cb = arguments[arguments.length - 1];
(async () => {
  try {
    var el = document.querySelector('[x-data="create()"]');
    if (!el || !window.Alpine) { cb(JSON.stringify({ok: false, error: 'no-create-comp'})); return; }
    try {
      var st = Alpine.store('modals');
      ['modalCreate','modalLimit','modalPremium','modalMessage','modalList','modalHistory'].forEach(function(id){ try { st.close(id); } catch (e) {} });
    } catch (e) {}
    var el2 = document.querySelector('[x-data="TemporaryEmail()"]');
    var t = Alpine.$data(el2);
    var isValid = function(a){ return a && a.indexOf('@') > 0 && a.toLowerCase().indexOf('random@') !== 0; };
    var before = t.emails.map(function(e){ return e.address; }).filter(isValid);
    var c = Alpine.$data(el);
    c.selectEmailType(provider);
    await new Promise(function(r){ setTimeout(r, 500); });
    c.query = {username: 'random', type: 'alias', domain: domain, server: '1'};
    if (c.setInput) c.setInput();
    await c.generate();
    var start = Date.now();
    while (Date.now() - start < 12000) {
      await new Promise(function(r){ setTimeout(r, 1000); });
      try {
        var after = t.emails.map(function(e){ return e.address; }).filter(isValid);
        var fresh = after.filter(function(a){ return before.indexOf(a) < 0; });
        if (fresh.length) {
          try { await t.selectEmail(fresh[fresh.length - 1], false); } catch (e) {}
          cb(JSON.stringify({ok: true, email: fresh[fresh.length - 1], all: after}));
          return;
        }
      } catch (e) {}
    }
    cb(JSON.stringify({ok: false, error: 'sem caixa nova'}));
  } catch (e) { cb(JSON.stringify({ok: false, error: String((e && e.message) || e)})); }
})();
"""


def _refresh_turnstile(d):
    """Forca reload da pagina smailpro e espera Alpine ficar pronto.
    Usa disconnect/reconnect do UC mode para limpar estado do Turnstile."""
    try:
        d.disconnect()
    except Exception:
        pass
    d.sleep(2)
    try:
        d.reconnect(0)
    except Exception:
        pass
    d.sleep(1)
    try:
        d.execute_script("window.location.reload(true)")
    except Exception:
        try:
            d.get(URL)
        except Exception:
            return False
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        d.sleep(1.5)
        try:
            ready = d.execute_script(
                'try{var el=document.querySelector(\'[x-data="create()"]\');'
                'return !!(el && window.Alpine)}catch(e){return false}')
            if ready:
                return True
        except Exception:
            pass
    return False


def _page_create(d, provider, domain):
    """Cria caixa no smailpro. Se o Turnstile travar, recarrega e tenta de novo (ate 3x).
    Se o driver ficar instavel, retorna erro para o caller recriar."""
    for attempt in range(3):
        try:
            d.set_script_timeout(45)
        except Exception:
            pass
        try:
            raw = d.execute_async_script(_CREATE_JS, provider, domain)
            data = json.loads(raw) if raw else {}
            if isinstance(data, dict) and data.get("ok") and isinstance(data.get("email"), str):
                return data
            err = str(data.get("error", "")) if isinstance(data, dict) else "bad-json"
            if "script timeout" not in err:
                return data if isinstance(data, dict) else {"ok": False, "error": "bad-json"}
        except Exception as e:
            err = str(e)
            if "script timeout" not in err:
                return {"ok": False, "error": err[:200]}
        # script timeout: recarrega via JS para resetar Turnstile sem overhead UC.
        _log_event(stage="create-refresh", result=f"attempt {attempt + 1}")
        _refresh_turnstile(d)
    return {"ok": False, "error": "create-timeout", "_need_driver_rebuild": True}


def _alp_valid_list(d):
    try:
        raw = d.execute_script(
            'try{const el=document.querySelector(\'[x-data="TemporaryEmail()"]\');'
            "const t=Alpine.$data(el);"
            "return JSON.stringify(t.emails.map(e=>e.address));}catch(e){return null}")
        arr = json.loads(raw) if raw else []
        return [a for a in arr if _valid_addr(a)]
    except Exception:
        return []


# Roda o loop nativo da pagina UMA vez e retorna TODAS as caixas:
# POST /app/inbox -> payload -> GET api.sonjj.com/v1/temp_*/inbox
# -> addMessage + selectEmail. Bem mais rapido que 1 chamada por sessao.
_LOOP_ALL_JS = """
var cb = arguments[arguments.length - 1];
(async () => {
  var originalFetch = window.fetch;
  var failure = '';
  var completed = 0;
  // ponytail: observa fetch do loop nativo; adaptar se provedor migrar para XHR.
  window.fetch = async function() {
    try {
      var response = await originalFetch.apply(this, arguments);
      if (!response.ok) failure = 'inbox-http-' + response.status;
      completed++;
      return response;
    } catch (e) { failure = 'inbox-network'; throw e; }
  };
  try {
    var ib = document.querySelector('[x-data="inbox()"]');
    if (!ib || !window.Alpine) { cb(JSON.stringify({ok: false, error: 'no-inbox-comp'})); return; }
    await Alpine.$data(ib).executeLoop();
    if (failure || !completed) {
      cb(JSON.stringify({ok: false, error: failure || 'inbox-refresh-unobserved'})); return;
    }
    var el = document.querySelector('[x-data="TemporaryEmail()"]');
    var t = Alpine.$data(el);
    var out = t.emails.map(function(e){
      return {address: e.address,
        messages: Array.isArray(e.messages) ? e.messages.map(function(m){
          return {mid: m.mid, from: m.textFrom, subject: m.textSubject, date: m.textDate}; }) : null}; });
    cb(JSON.stringify({ok: true, emails: out,
      sel: t.selectedEmail ? t.selectedEmail.address : null}));
  } catch (e) { cb(JSON.stringify({ok: false, error: String((e && e.message) || e)})); }
  finally { window.fetch = originalFetch; }
})();
"""


def _page_loop_all(d):
    try:
        d.set_script_timeout(90)
    except Exception:
        pass
    try:
        raw = d.execute_async_script(_LOOP_ALL_JS)
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {"ok": False, "error": "bad-json"}
    except Exception as e:
        try:
            if d.execute_script("return 1") != 1:
                _on_driver_failure("loop: driver desconectado")
        except Exception:
            _on_driver_failure("loop: driver desconectado")
        return {"ok": False, "error": str(e)[:200]}


# A cota do smailpro e de 5 caixas SIMULTANEAS por IP: excluir de verdade
# devolve a vaga. Por isso o delete confirma a remocao relendo a lista em vez
# de reportar sucesso as cegas -- delete silenciosamente falho esgota a cota.
_DELETE_JS = """
var address = arguments[0];
var cb = arguments[arguments.length - 1];
(async () => {
  try {
    var el = document.querySelector('[x-data="TemporaryEmail()"]');
    if (!el || !window.Alpine) { cb(JSON.stringify({ok: false, error: 'no-comp'})); return; }
    var t = Alpine.$data(el);
    var err = '';
    try { await t.emailManager.deleteEmail(address); }
    catch (e) { err = 'delete:' + String((e && e.message) || e); }
    var remaining = null;
    try {
      t.emails = await t.emailManager.getAllEmails();
      remaining = t.emails.map(function (e) { return e.address; });
    } catch (e) { err = err || ('list:' + String((e && e.message) || e)); }
    try {
      if (t.emails && t.emails.length) await t.selectEmail(t.emails[0].address, false);
      else await t.selectEmail(null);
    } catch (e) {}
    if (remaining === null) { cb(JSON.stringify({ok: false, error: err || 'sem-lista'})); return; }
    var gone = remaining.indexOf(address) < 0;
    cb(JSON.stringify({ok: gone, remaining: remaining.length,
                       error: gone ? '' : (err || 'caixa ainda listada')}));
  } catch (e) { cb(JSON.stringify({ok: false, error: String((e && e.message) || e)})); }
})();
"""


def _page_delete(d, address):
    """Exclui a caixa e confirma. Retorna dict com ok/remaining/error."""
    try:
        d.set_script_timeout(30)
    except Exception:
        pass
    try:
        raw = d.execute_async_script(_DELETE_JS, address)
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {"ok": False, "error": "bad-json"}
    except Exception as e:
        data = {"ok": False, "error": str(e)[:120]}
    _log_event(stage="provider-delete",
               result=("ok, restam %s" % data.get("remaining")) if data.get("ok")
               else "FALHOU: %s" % str(data.get("error", "?"))[:80])
    return data
    try:
        d.execute_async_script(_DELETE_JS, address)
    except Exception:
        pass


def _read_body(d, address, mid):
    # Corpo sem modal: captcha('message') + GET /app/message?email&mid +
    # GET api.sonjj message?payload=. Retorna (body, err).
    js = """
var address = arguments[0];
var mid = arguments[1];
var cb = arguments[arguments.length - 1];
(async () => {
  try {
    var rootEl = document.querySelector('[x-data="TemporaryEmail()"]');
    if (!rootEl || !window.Alpine) { cb(JSON.stringify({ok: false, error: 'no-comp'})); return; }
    var root = Alpine.$data(rootEl);
    var token = null;
    try { token = await root.captcha('message'); }
    catch (e) { cb(JSON.stringify({ok: false, error: 'captcha:' + String(e && e.message || e)})); return; }
    var q = '?email=' + encodeURIComponent(address) + '&mid=' + encodeURIComponent(mid);
    var pr = await fetch('/app/message' + q, {method: 'GET',
      headers: {'Content-Type': 'application/json', 'x-captcha': token}});
    var ptxt = await pr.text();
    if (!pr.ok) { cb(JSON.stringify({ok: false, error: 'message-http-' + pr.status})); return; }
    var type = 'other';
    try { type = root.checkTypeEmail(address); } catch (e) {}
    var urls = {other: 'https://api.sonjj.com/v1/temp_email/message',
      google: 'https://api.sonjj.com/v1/temp_gmail/message',
      microsoft: 'https://api.sonjj.com/v1/temp_outlook/message'};
    var url = urls[type] || urls.other;
    var cr = await fetch(url + '?payload=' + encodeURIComponent(ptxt));
    var ctxt = await cr.text();
    if (!cr.ok) { cb(JSON.stringify({ok: false, error: 'sonjj-http-' + cr.status})); return; }
    var cj = null;
    try { cj = JSON.parse(ctxt); } catch (e) { cb(JSON.stringify({ok: false, error: 'sonjj-json'})); return; }
    if (cj && typeof cj.body === 'string' && cj.body.length) {
      if (cj.body.length > 200000) { cb(JSON.stringify({ok: false, error: 'body-too-large'})); return; }
      cb(JSON.stringify({ok: true, body: cj.body, encoding: cj.encoding || ''})); return;
    }
    cb(JSON.stringify({ok: false, error: 'empty-body'}));
  } catch (e) { cb(JSON.stringify({ok: false, error: String((e && e.message) || e)})); }
})();
"""
    try:
        d.set_script_timeout(90)
    except Exception:
        pass
    try:
        raw = d.execute_async_script(js, str(address), str(mid))
        data = json.loads(raw) if raw else {}
    except Exception as e:
        return "", f"js:{str(e)[:120]}"
    if isinstance(data, dict):
        if data.get("ok") and isinstance(data.get("body"), str) and data["body"]:
            if len(data["body"]) > 200000:
                return "", "body-too-large"
            return _decode_qp_if_needed(data["body"], str(data.get("encoding", ""))), ""
        return "", str(data.get("error") or "invalid-body")[:200]
    return "", "bad-json"


def _close_modal(d):
    try:
        d.execute_script("try{Alpine.store('modals').close('modalMessage')}catch(e){}")
    except Exception:
        pass


_CAPTCHA_RE = re.compile(r"captcha[:\s]|CAPTCHA\s+timeout|turnstile", re.I)
_MAX_CAPTCHA_RETRIES = 2


def _is_captcha_error(error):
    return bool(error) and bool(_CAPTCHA_RE.search(error))


def _try_solve_captcha(d):
    """Tenta resolver CAPTCHA/Turnstile usando metodos nativos do SeleniumBase.
    Retorna True se alguma acao foi tomada."""
    # 1. uc_gui_handle_captcha: resolve Turnstile automaticamente via JS/CDP.
    try:
        d.uc_gui_handle_captcha(frame="iframe")
        _log_event(stage="captcha-solve", result="uc_gui_handle_captcha executado")
        return True
    except Exception as e:
        _log_event(stage="captcha-solve", result=f"uc_gui_handle_captcha:{type(e).__name__}")
    # 2. solve_captcha: tenta detectar e resolver qualquer CAPTCHA na pagina.
    try:
        d.solve_captcha()
        _log_event(stage="captcha-solve", result="solve_captcha executado")
        return True
    except Exception as e:
        _log_event(stage="captcha-solve", result=f"solve_captcha:{type(e).__name__}")
    return False


def _resolve_inbox_handle(d, inbox_handle):
    """Recupera somente inbox unico validado; nunca navega durante descoberta."""
    with _LOCK:
        handles = set(d.window_handles)
        try:
            original = d.current_window_handle
        except Exception:
            original = None

        def valid(handle):
            d.switch_to.window(handle)
            p = urlparse(d.current_url)
            if (p.scheme != 'https' or p.hostname != 'smailpro.com'
                    or p.port not in (None, 443) or p.username is not None
                    or p.password is not None or p.path != '/temporary-email' or p.params):
                return False
            return d.execute_script('''
                if (location.origin !== 'https://smailpro.com' ||
                    location.pathname !== '/temporary-email') return false;
                const el = document.querySelector('[x-data="TemporaryEmail()"]');
                if (!el || !window.Alpine) return false;
                const inbox = Alpine.$data(el);
                return !!inbox && Array.isArray(inbox.emails);
            ''') is True

        try:
            if inbox_handle in handles and valid(inbox_handle):
                return inbox_handle
            candidates = [h for h in handles if valid(h)]
            if len(candidates) > 1:
                raise ValueError('inbox ambiguo, abortado')
            if len(candidates) != 1:
                raise RuntimeError('inbox ausente ou ambiguo, abortado')
            inbox_handle = candidates[0]
            # Revalidar apos percorrer abas; falha de inspecao nunca prova unicidade.
            if not valid(inbox_handle):
                raise RuntimeError('inbox mudou durante recuperacao, abortado')
            _DRV['inbox_handle'] = inbox_handle
            return inbox_handle
        except Exception:
            try:
                d.switch_to.window(original)
            except Exception:
                pass
            raise


def _refresh_inbox_page(d, inbox_handle):
    """Recarrega smailpro.com na aba do inbox e espera Alpine + Turnstile resolverem.
    Retorna True se a pagina ficou pronta em ate 25s."""
    try:
        inbox_handle = _resolve_inbox_handle(d, inbox_handle)
    except Exception as e:
        _log_event(stage="captcha-refresh", result=f"switch:{str(e)[:80]}")
        return False
    try:
        # d.get() e nao d.open(): d.open() troca para CDP Mode, desconecta o
        # WebDriver e pode recarregar em outra aba que nao a do inbox.
        d.get(URL)
        d.sleep(2)
    except Exception as e:
        _log_event(stage="captcha-refresh", result=f"open:{str(e)[:80]}")
        return False
    # Apos recarregar, tenta resolver o Turnstile que apareceu.
    _try_solve_captcha(d)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            ready = d.execute_script(
                'try{'
                'var el=document.querySelector(\'[x-data="TemporaryEmail()"]\');'
                'if(!el||!window.Alpine) return false;'
                'var t=Alpine.$data(el);'
                'var ws=t&&t.webSocket;'
                'return !!(ws && (ws.readyState===1 || t.emails && t.emails.length >= 0));'
                '}catch(e){return false}')
            if ready:
                _log_event(stage="captcha-refresh", result="ok (Alpine pronto)")
                return True
        except Exception:
            pass
        d.sleep(1.5)
    _log_event(stage="captcha-refresh", result="timeout aguardando Alpine")
    return False


def _read_body_with_retry(d, address, mid, inbox_handle):
    """Le o corpo com retry: resolve CAPTCHA via SeleniumBase, senao recarrega pagina."""
    b, err = _read_body(d, address, mid)
    attempts = 0
    while _is_captcha_error(err) and attempts < _MAX_CAPTCHA_RETRIES:
        attempts += 1
        _log_event(stage="captcha-retry", result=f"tentativa {attempts}: {err[:60]}")
        _close_modal(d)
        # Primeiro tenta resolver o CAPTCHA na pagina atual.
        solved = _try_solve_captcha(d)
        if solved:
            d.sleep(2)
            b2, err2 = _read_body(d, address, mid)
            if not _is_captcha_error(err2):
                return b2, err2, attempts
        # Se nao resolveu, recarrega a pagina inteira.
        refreshed = _refresh_inbox_page(d, inbox_handle)
        if not refreshed:
            _log_event(stage="captcha-retry", result="refresh falhou; abortando")
            break
        b, err = _read_body(d, address, mid)
    return b, err, attempts


_CONFIRM_SUCCESS = 'E-mail confirmado com sucesso. Sua conta já está liberada.'
_CONFIRM_HOST = 'pokepixel.nietore.com'
_VERIFY_REASONS = ('success_visible', 'already_confirmed', 'token_expired', 'token_used',
                   'webgl_unsupported', 'unexpected_origin', 'destination_error',
                   'success_not_observed')
_VERIFY_JS = """
const expected = arguments[0], host = arguments[1], done = arguments[arguments.length - 1];
const deadline = performance.now() + 14500;
let _texts = [];
function finish(reason) {
  const sample = reason === 'success_visible' ? [] : _texts.slice(0, 5);
  let st = null;
  try { const e = performance.getEntriesByType('navigation')[0]; if (e) st = e.responseStatus; } catch (_) {}
  done({reason, href: location.href, title: (document.title || '').slice(0, 200), status: st, sample});
}
function check() {
  if (location.protocol !== 'https:' || location.hostname !== host || location.port) {
    finish('unexpected_origin'); return;
  }
  const normalize = s => s.replace(/\\s+/gu, ' ').trim();
  const visible = Array.from(document.querySelectorAll('body, body *')).filter(el => {
    if (!el.getClientRects().length || typeof el.innerText !== 'string') return false;
    // Nao aceitar ancestral cujo innerText inclui mensagem transparente/oculta.
    if (Array.from(el.querySelectorAll('*')).some(child =>
        typeof child.innerText === 'string' && normalize(child.innerText) === normalize(el.innerText))) return false;
    for (let node = el; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) return false;
    }
    return true;
  });
  _texts = visible.map(el => normalize(el.innerText)).filter(s => s);
  if (_texts.includes(expected)) { finish('success_visible'); return; }
  // ponytail: frases negativas exatas; ampliar somente com evidencia redigida do destino.
  for (const [text, reason] of [
      ['Token expirado.', 'token_expired'],
      ['Token já utilizado.', 'token_used'],
      ['Token já foi utilizado.', 'token_used'],
      ['Your browser does not support WebGL', 'webgl_unsupported']]) {
    if (_texts.includes(text)) { finish(reason); return; }
  }
  if (performance.now() >= deadline) { finish('success_not_observed'); return; }
  setTimeout(check, Math.min(250, deadline - performance.now()));
}
check();
"""


def _verify_confirmation(d, diagnostic=None, host=None, expected_text=None):
    # ponytail: contrato DOM; adicionar outro host so com evidencia propria.
    previous = None
    started, started_at = time.monotonic(), time.time()
    reason = 'success_not_observed'
    extra = {}
    h = host or _CONFIRM_HOST
    et = expected_text or _CONFIRM_SUCCESS
    try:
        previous = d.timeouts.script
        d.set_script_timeout(15)
        result = d.execute_async_script(_VERIFY_JS, et, h)
        if isinstance(result, dict):
            r = result.get('reason', '')
            if r in _VERIFY_REASONS:
                reason = r
            href = result.get('href', '')
            extra['href'] = _redact_url(href) if isinstance(href, str) else ''
            title = result.get('title', '')
            extra['title'] = _redact_text(title[:200]) if isinstance(title, str) else ''
            status = result.get('status')
            extra['status'] = status if isinstance(status, int) else None
            sample = result.get('sample', [])
            extra['sample'] = [_redact_text(s)[:200] for s in sample if isinstance(s, str)][:5]
            if reason == 'success_not_observed' and isinstance(extra.get('status'), int) and extra['status'] >= 400:
                reason = 'destination_error'
        elif isinstance(result, str):
            if result in _VERIFY_REASONS:
                reason = result
            elif ' '.join(result.split()) == et:
                reason = 'success_visible'
    except Exception:
        reason = 'driver_error'
    finally:
        if diagnostic is not None:
            diagnostic.update(reason=reason, started_at=started_at, observed_at=time.time(),
                              elapsed_ms=round((time.monotonic() - started) * 1000), **extra)
        if previous is not None:
            try:
                d.set_script_timeout(previous)
            except Exception:
                _log_event(stage='confirmation-timeout-restore', result='failed')
    return reason in ('success_visible', 'already_confirmed'), et if reason == 'success_visible' else ''


def _validate_destination(url):
    if not _ok_url(url):
        raise ValueError('URL bloqueada')
    p = urlparse(url)
    if p.scheme != 'https' or p.port not in (None, 443):
        raise ValueError('navegacao exige HTTPS porta 443')
    addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('DNS privado/reservado bloqueado')


def _navigate_confirmation(d, url, wait_s, attempt=None):
    """Navegacao leve na aba atual com JS habilitado; nenhum GET HTTP alternativo para o token.

    Usa d.get() e NAO d.open(): em UC Mode, d.open() troca para CDP Mode, o que
    (a) desconecta o WebDriver -- execute_script/window_handles passam a recusar
    conexao e parecem "driver morto" -- e (b) navega em OUTRA aba, deixando a aba
    isolada em about:blank. d.get() mantem a sessao WebDriver e a aba corretas.
    """
    _validate_destination(url)
    previous = None
    try:
        previous = d.timeouts.page_load
        d.set_page_load_timeout(30)
    except Exception:
        pass
    try:
        # Timeout/erro de transporte pode ocorrer depois do consumo do token.
        if attempt is not None:
            attempt['navigation_attempted'] = True
        d.get(url)
    finally:
        try:
            if previous is not None:
                d.set_page_load_timeout(previous)
        except Exception:
            pass
    try:
        d.execute_script("return document.readyState")
    except Exception as e:
        raise RuntimeError(f'navegacao falhou ({type(e).__name__})')


def _open_confirm_isolated(d, inbox_handle, url, wait_s=6, confirmation=None, attempt=None, session=None):
    # Etapa 2: aba criada via new_window, identificada por diff de handles.
    # Nunca navega na aba do inbox; so fecha a aba criada. Falha aborta.
    try:
        inbox_handle = _resolve_inbox_handle(d, inbox_handle)
        before = set(d.window_handles)
    except Exception as e:
        _log_event(stage="tab-create", result=f"handles:{type(e).__name__}")
        return False, "inbox ausente, ambiguo ou inacessivel, abortado", {
            "verified": False, "evidence": "", "state": "ambiguous" if isinstance(e, ValueError) else "unknown"}
    if inbox_handle and inbox_handle not in before:
        _log_event(stage="tab-create", result="aba do inbox ausente",
                   removal_reason="navegacao abortada, sessoes preservadas")
        return False, "aba do inbox ausente, abortado", {"verified": False, "evidence": ""}
    try:
        created = d.switch_to.new_window("tab")
    except Exception as e:
        _log_event(stage="tab-create", result=f"new_window:{type(e).__name__}")
        return False, "aba nao criada (driver_error)", {"verified": False, "evidence": ""}
    try:
        after = set(d.window_handles)
    except Exception as e:
        _log_event(stage="tab-create", result=f"handles-pos:{type(e).__name__}")
        return False, "handles pos-criacao inacessiveis, abortado", {"verified": False, "evidence": ""}
    newh = created if isinstance(created, str) and created in after else None
    if not newh:
        diff = after - before
        newh = diff.pop() if len(diff) == 1 else None
    if not newh or newh in before or newh not in after:
        try:
            d.switch_to.window(inbox_handle)
        except Exception:
            pass
        _log_event(stage="tab-create", result="aba ambigua, navegacao abortada")
        return False, "aba ambigua, navegacao abortada", {"verified": False, "evidence": ""}
    red = _redact_url(url)
    _DRV['handles'] = len(after)
    observed_at = None
    state = 'unknown'
    diagnostic = {'reason': 'driver_error', 'started_at': time.time()}
    correlation = {k: (attempt or {}).get(k, '') for k in ('session_id', 'mid')}
    host = (session or {}).get('confirm_host') or _CONFIRM_HOST
    et = (session or {}).get('success_text') or _CONFIRM_SUCCESS
    try:
        d.switch_to.window(newh)
        _navigate_confirmation(d, url, wait_s, attempt)
        navigated = time.monotonic()
        diagnostic['navigation_completed_at'] = time.time()
        verified, evidence = _verify_confirmation(d, diagnostic, host=host, expected_text=et)
        # token_used + navegacao nuestra = nossa 1a visita consumiu o token.
        if diagnostic.get('reason') == 'token_used' and (attempt or {}).get('navigation_attempted'):
            diagnostic['reason'] = 'already_confirmed'
            verified, evidence = True, et
        # Re-visita idempotente: segunda chance so apos navegacao real.
        if not verified and diagnostic.get('reason') in ('success_not_observed', 'driver_error') \
                and (attempt or {}).get('navigation_attempted') and not (attempt or {}).get('replay_done'):
            _log_event(**correlation, stage='tab-replay', result=diagnostic.get('reason', ''))
            try:
                _navigate_confirmation(d, url, max(0, wait_s - (time.monotonic() - navigated)), attempt)
                replay_diag = {}
                _, _ = _verify_confirmation(d, replay_diag, host=host, expected_text=et)
                if replay_diag.get('reason') == 'token_used':
                    diagnostic['reason'] = 'already_confirmed'
                    verified, evidence = True, et
                elif replay_diag.get('reason') == 'success_visible':
                    diagnostic['reason'] = 'success_visible'
                    verified, evidence = True, et
            except Exception:
                pass
        if verified:
            _DRV['origin'] = 'https://' + host
            observed_at = time.time()
        diagnostic.update(verified=verified, evidence=evidence, verified_at=observed_at,
                          host=host if diagnostic['reason'] in _VERIFY_REASONS
                          and diagnostic['reason'] not in ('unexpected_origin', 'destination_error') else None,
                          mid=correlation['mid'])
        if confirmation is not None:
            confirmation.update(diagnostic)
        _log_event(**correlation, stage='tab-verify', result=diagnostic['reason'], confirmation=diagnostic)
        # wait_s minimo apos navegacao; observacao conta como espera.
        remaining = wait_s - (time.monotonic() - navigated)
        if remaining > 0:
            time.sleep(remaining)
        status = "aberta e fechada" + (f"; evidencia: {evidence}" if verified else "; sem evidencia de confirmacao")
        _log_event(stage="tab-open", result=f"{red} verified={verified}")
    except Exception as e:
        _log_event(stage="tab-open", result=f"{red} erro:{type(e).__name__}")
        if isinstance(e, ValueError) and not (attempt or {}).get('navigation_attempted'):
            state = 'blocked'
        verified, evidence = False, ""
        diagnostic.update(reason='destination_blocked' if state == 'blocked' else 'driver_error',
                          verified=False, evidence='', verified_at=None, observed_at=time.time(),
                          mid=correlation['mid'])
        if confirmation is not None:
            confirmation.update(diagnostic)
        _log_event(**correlation, stage='tab-verify', result=diagnostic['reason'], confirmation=diagnostic)
        status = f"falha ao abrir ({diagnostic['reason']}: {type(e).__name__}); resultado unknown"
    finally:
        # WebDriver desconectado nao e o mesmo que Chrome morto: em UC Mode o
        # navegador segue vivo e so a sessao WebDriver cai. Tentar reconectar
        # antes de desistir; so pular o cleanup se nem assim responder.
        driver_alive = _ensure_connected(d)
        if driver_alive:
            try:
                if newh in set(d.window_handles):
                    d.switch_to.window(newh)
                    d.close()
                    _DRV['handles'] = len(after) - 1
            except Exception:
                status += '; cleanup da aba falhou'
            try:
                d.switch_to.window(inbox_handle)
                _DRV['origin'] = ''
            except Exception:
                pass
        else:
            _log_event(stage="tab-cleanup", result="driver sem resposta, cleanup pulado")
    return not status.startswith("falha ao abrir"), status, {**diagnostic, "verified": verified, "evidence": evidence,
                                                            "verified_at": observed_at, "state": state}


def _cached_body(d, s, mid):
    cache = s.setdefault("bodies", {})
    if mid in cache:
        return cache[mid], ""
    inbox_h = _DRV.get("inbox_handle")
    started = time.monotonic()
    try:
        b, error, captcha_retries = _read_body_with_retry(d, s["email"], mid, inbox_h)
    finally:
        _log_event(s.get('_sid', ''), mid, 'body', elapsed_ms=round((time.monotonic() - started) * 1000))
    _close_modal(d)
    if b and not error:
        cache[mid] = b
    if captcha_retries:
        s["captcha_retries"] = s.get("captcha_retries", 0) + captcha_retries
    return b, error


def _confirm_one(d, s, m, wait_s, revalidate=False, manual_link=None):
    if _expired(s):
        return {'mid': m.get('mid'), 'state': 'expired', 'opened': False,
                'verified': False, 'status': 'deadline atingido', 'skipped': True}
    mid = m.get("mid")
    att = s["attempts"].get(mid)
    if att is None:
        att = {"count": 0, "in_progress": False, "state": "detected"}
        s["attempts"][mid] = att
    # Replay: permitir reabrir apos timeout se navegou mas nao concluiu observacao.
    prev_reason = (att.get('confirmation') or {}).get('reason', '')
    can_replay = (att.get("navigation_attempted") and not att.get("replay_done")
                  and att["count"] <= MAX_ATTEMPTS
                  and prev_reason in ('success_not_observed', 'driver_error'))
    skip = not revalidate and not can_replay and (mid in s["opened"] or mid in s["verified"]
                            or att.get("navigation_attempted") or att.get('state') in ('ambiguous', 'blocked'))
    if (skip or att.get("in_progress")
            or att["count"] >= MAX_ATTEMPTS
            or time.monotonic() < att.get("next_retry", 0)):
        return {"mid": mid, "subject": m.get("subject"), "link": "",
                "detected": True, "body_loaded": False, "link_found": False,
                "opened": mid in s["opened"], "verified": mid in s["verified"], "state": att["state"],
                "status": "limite, cooldown ou tentativa ja processada", "skipped": True,
                "confirmation": att.get('confirmation')}
    att["in_progress"] = True
    att.update(session_id=s.get('_sid', ''), mid=mid)
    # Re-visita: nao reler corpo nem incrementar contagem; reabrir mesmo link.
    is_replay = can_replay and att.get("navigation_attempted") and att.get('confirmation', {}).get('link')
    if is_replay:
        link = att['confirmation']['link']
        att["state"] = "replay"
        s['phase'] = 'opening_link'
        confirmation = att.get('confirmation') or {'mid': mid}
        confirmation.setdefault('mid', mid)
        s['confirmation'] = confirmation
        started = time.monotonic()
        try:
            ok, status, ev = _open_confirm_isolated(d, _DRV.get("inbox_handle"), link,
                                                     max(0, min(wait_s, 30)), confirmation, att, session=s)
        finally:
            if 'observed_at' not in confirmation:
                confirmation['observed_at'] = time.time()
            _log_event(s.get('_sid', ''), mid, 'confirmation', elapsed_ms=round((time.monotonic() - started) * 1000))
        verified = ok and bool(ev.get("verified"))
        confirmation['link'] = link
        att['confirmation'] = dict(confirmation)
        att["state"] = ev.get('state', 'unknown')
        if ok:
            s["opened"].add(mid)
            att["state"] = "verified" if verified else "unknown"
            if verified:
                s["verified"].add(mid)
        elif att["count"] >= MAX_ATTEMPTS and att['state'] != 'ambiguous':
            att["state"] = "failed"
        _log_event(s.get("_sid", ""), mid, "confirm-attempt",
                   f"{_redact_url(link)} replay=True opened={ok} verified={verified}")
        att['replay_done'] = True
        att["in_progress"] = False
        s['phase'] = ''
        return {"mid": mid, "subject": m.get("subject"), "link": link,
                "confirmation": att['confirmation'],
                "evidence": ev.get('evidence', ''), "verified_at": ev.get('verified_at'),
                "link_ref": _redact_url(link),
                "detected": True, "body_loaded": True, "link_found": True,
                "opened": ok, "verified": verified, "state": att["state"], "status": status}
    s['phase'] = 'reading_body'
    att["state"] = "body-loading"
    att["count"] += 1
    berr, status, ok = '', '', True
    try:
        if manual_link:
            link = manual_link
            berr, body_loaded = '', True
        else:
            b, berr = _cached_body(d, s, mid)
            body_loaded = bool(b) and not berr
            candidates = _extract_links(b, confirmation_only=True) if body_loaded else []
            expected = s.get('expected_hosts')
            if expected and candidates:
                candidates = [u for u in candidates if urlparse(u).hostname.lower() in expected]
                if not candidates:
                    berr = 'expected_hosts: alvo nao corresponde'
            if len(candidates) > 1:
                att['state'] = 'ambiguous'
                return {"mid": mid, "subject": m.get("subject"), "link": "",
                        "detected": True, "body_loaded": True, "link_found": True,
                        "opened": False, "verified": False, "state": "ambiguous",
                        "candidates": candidates, "status": "links ambiguos; escolha manual via /email/body"}
            link = candidates[0] if candidates else ""
        if not link:
            terminal = att["count"] >= MAX_ATTEMPTS
            att["state"] = "failed" if terminal else "unknown"
            why = f"sem link no corpo ({berr})" if berr else "sem link no corpo"
            if terminal:
                why += f" apos {MAX_ATTEMPTS} tentativas"
            _log_event(s.get("_sid", ""), mid, "confirm-attempt", why)
            return {"mid": mid, "subject": m.get("subject"), "link": "",
                    "detected": True, "body_loaded": body_loaded,
                    "link_found": False, "opened": False, "verified": False,
                    "state": att['state'], "status": why}
        inbox_h = _DRV.get("inbox_handle")
        att["state"] = "opening"
        if _expired(s):
            att['state'] = 'unknown'
            return {'mid': mid, 'state': 'expired', 'opened': False, 'verified': False,
                    'status': 'deadline atingido antes da navegacao'}
        s['phase'] = 'opening_link'
        confirmation = {'mid': mid, 'verified': False, 'evidence': '', 'verified_at': None,
                        'reason': 'driver_error', 'started_at': time.time()}
        s['confirmation'] = confirmation
        started = time.monotonic()
        ok, status, ev = False, '', {'verified': False, 'reason': 'driver_error'}
        try:
            ok, status, ev = _open_confirm_isolated(d, inbox_h, link, max(0, min(wait_s, 30)), confirmation, att, session=s)
        finally:
            if 'observed_at' not in confirmation:
                confirmation['observed_at'] = time.time()
            confirmation['link'] = link
            att['confirmation'] = dict(confirmation)
            _log_event(s.get('_sid', ''), mid, 'confirmation', elapsed_ms=round((time.monotonic() - started) * 1000))
        verified = ok and bool(ev.get("verified"))
        att["state"] = ev.get('state', 'unknown')
        if ok:
            s["opened"].add(mid)
            att["state"] = "verified" if verified else "unknown"
            if verified:
                s["verified"].add(mid)
        else:
            if att["count"] >= MAX_ATTEMPTS and att['state'] != 'ambiguous':
                att["state"] = "failed"
        _log_event(s.get("_sid", ""), mid, "confirm-attempt",
                   f"{_redact_url(link)} opened={ok} verified={verified}")
        return {"mid": mid, "subject": m.get("subject"), "link": link,
                "confirmation": att['confirmation'],
                "evidence": ev.get('evidence', ''), "verified_at": ev.get('verified_at'),
                "link_ref": _redact_url(link),
                "detected": True, "body_loaded": True, "link_found": True,
                "opened": ok, "verified": verified, "state": att["state"], "status": status}
    except Exception as exc:
        att['state'] = 'unknown'
        berr = 'confirm:' + type(exc).__name__
        raise
    finally:
        s['phase'] = ''
        s['confirm_error'] = berr or (status if not ok else '')
        att["in_progress"] = False
        att["next_retry"] = time.monotonic() + RETRY_COOLDOWN_S
        if att["count"] >= MAX_ATTEMPTS and mid not in s["opened"] and att['state'] != 'ambiguous':
            att["state"] = "failed"


def _drop(sid, stage='remove'):
    s = sessions.pop(sid, None)
    if s is not None:
        confirmation = s.get('confirmation')
        _log_event(sid, (confirmation or {}).get('mid', ''), stage, 'ok',
                   confirmation=confirmation,
                   snapshot={'state': _monitor_state(s), 'opened': bool(s.get('opened')),
                             'verified': bool(s.get('verified')), 'monitoring': False})
    return s


def _free_provider_slots(d, existing=None, keep_free=1):
    """Libera vagas da cota do provedor excluindo caixas sem sessao ativa.

    A cota de 5 e por IP e conta caixas vivas no smailpro, nao sessoes nossas:
    caixas de execucoes anteriores continuam ocupando lugar e fazem o create
    falhar com 'sem caixa nova'. Caixas em uso por sessao ativa sao intocadas."""
    boxes = list(existing if existing is not None else _alp_valid_list(d))
    in_use = {s.get("email") for s in sessions.values()}
    descartaveis = [a for a in boxes if a not in in_use]
    alvo = len(boxes) - (PROVIDER_QUOTA - keep_free)
    if alvo <= 0 or not descartaveis:
        return 0
    freed = 0
    for address in descartaveis[:alvo]:
        if _page_delete(d, address).get("ok"):
            freed += 1
    _log_event(stage="quota-free",
               result="%d/%d vaga(s) liberada(s); %d em uso"
                      % (freed, min(alvo, len(descartaveis)), len(in_use)))
    return freed


@app.post("/email/create")
def create(req: CreateReq):
    prov = req.provider
    if prov not in PROVIDERS:
        raise HTTPException(400, f"provider invalido, use {sorted(PROVIDERS)}")
    if req.server != "1":
        raise HTTPException(403, "server-2+ premium, conta free usa server-1")
    if req.domain in PREMIUM_DOMAINS:
        raise HTTPException(403, f"{req.domain} dominio premium")
    dom = req.domain if req.domain in FREE[prov]["domains"] else FREE[prov]["domains"][0]
    # Timeout de 45s impede que o create bloqueie o DESHUB para sempre
    # quando _LOCK esta segurado por _confirm_one em driver travado.
    started = time.monotonic()
    acquired = _LOCK.acquire(blocking=True, timeout=45)
    _log_event(stage='lock-wait', result='create:acquired' if acquired else 'create:timeout',
               elapsed_ms=round((time.monotonic() - started) * 1000))
    if not acquired:
        raise HTTPException(503, "driver ocupado por outra operacao; tente novamente")
    started = time.monotonic()
    try:
        # Somente sessoes verificadas podem ceder vaga automaticamente.
        if len(sessions) >= MAX_BOXES:
            replaceable = [sid for sid, s in sessions.items() if s.get('verified')]
            if not replaceable:
                raise HTTPException(409, 'limite de sessoes; inspecione e libere uma sessao manualmente')
            oldest_sid = min(replaceable, key=lambda k: sessions[k].get("expires_at", 0))
            oldest = _drop(oldest_sid, 'auto-remove')
            _enqueue_delete(oldest.get("email"))
        d = _driver_locked(create=True)
        before = _alp_valid_list(d)
        # A cota de 5 e do provedor, nao da memoria: caixas de execucoes
        # anteriores continuam ocupando vaga e causam "sem caixa nova".
        # Libera vagas antes de tentar criar.
        if len(before) >= PROVIDER_QUOTA:
            _free_provider_slots(d, before)
            before = _alp_valid_list(d)
        res = _page_create(d, prov, dom)
        # Se o driver ficou instavel, recria e tenta de novo uma vez.
        if res.get("_need_driver_rebuild"):
            _log_event(stage="create", result="driver rebuild apos timeout")
            try:
                d.quit()
            except Exception:
                pass
            # Nao marca sessoes como disconnected: e rebuild intencional.
            _DRV["d"] = None
            _DRV["inbox_handle"] = None
            _DRV["status"] = "ok"
            _DRV["error"] = ""
            d = _driver_locked(create=True)
            before = _alp_valid_list(d)
            res = _page_create(d, prov, dom)
        if not res.get("ok") or not isinstance(res.get('email'), str) or not _valid_addr(res['email']):
            _log_event(stage="create", result=str(res.get("error", "?"))[:150])
            raise HTTPException(502, f"create falhou ({res.get('error', '?')}), tente de novo")
        email = res["email"]
        if email in set(before):
            email = _pick_new(before, res.get("all") or [])
            if not email:
                _log_event(stage="create", result="sem caixa nova")
                raise HTTPException(502, "create retornou caixa antiga, tente de novo")
        sid = uuid.uuid4().hex[:8]
        sessions[sid] = {"email": email, "provider": prov, "domain": dom,
                         "seen": set(), "opened": set(), "verified": set(),
                         "attempts": {}, "status": "connected", "error": "",
                         "_sid": sid, 'auto_confirm': req.auto_confirm,
                         'expected_hosts': req.expected_hosts, 'timeout_seconds': req.timeout_seconds,
                         'confirm_host': req.confirm_host, 'success_text': req.success_text,
                         'deadline': time.monotonic() + req.timeout_seconds,
                         'expires_at': time.time() + req.timeout_seconds}
        _log_event(sid, "", "create", f"{prov}/{dom}")
    finally:
        _log_event(stage='create-duration', elapsed_ms=round((time.monotonic() - started) * 1000))
        _LOCK.release()
    return {"session_id": sid, "email": email, "provider": prov, "domain": dom}


@app.get("/sessions")
def list_sessions():
    # Consulta de estado: nunca cria navegador (Etapa 7).
    return [{"session_id": k, "email": v["email"], "provider": v.get("provider"),
             "domain": v.get("domain"), "status": v.get("status", "connected"),
             "error": v.get("error", ""),
             "confirmation": v.get('confirmation'),
             "verified": sorted(v.get("verified", set()))} for k, v in sessions.items()]


@app.get("/quota")
def quota():
    """Vagas da cota do provedor (5 simultaneas por IP). Nunca abre navegador."""
    d = _DRV.get("d")
    boxes = None
    if d is not None:
        try:
            if d.execute_script("return 1") == 1:
                boxes = _alp_valid_list(d)
        except Exception:
            boxes = None
    in_use = sorted({s.get("email") for s in sessions.values() if s.get("email")})
    out = {"quota": PROVIDER_QUOTA, "sessions": len(sessions),
           "in_use": in_use, "pending_delete": len(_DELETE_QUEUE),
           "provider_boxes": None, "free": None, "stale": True}
    if boxes is not None:
        orfas = [a for a in boxes if a not in set(in_use)]
        out.update(provider_boxes=len(boxes), orphan_boxes=orfas,
                   free=max(0, PROVIDER_QUOTA - len(boxes)), stale=False)
    return out


def _msgs_for(data, address):
    if not isinstance(data.get("emails"), list):
        raise ValueError("emails invalidos")
    for e in data["emails"]:
        if not isinstance(e, dict):
            raise ValueError("caixa invalida")
        if e.get("address") == address:
            msgs = e.get("messages")
            if not isinstance(msgs, list) or any(not isinstance(m, dict) or not m.get("mid") for m in msgs):
                raise ValueError("messages invalidas")
            return msgs
    return None


def _inbox_result(s, data):
    state, error = "error", data.get("error", "resposta invalida")
    msgs = None
    if s.get("status") == "disconnected":
        state, error = "disconnected", s.get("error", "driver desconectado")
    elif data.get("ok"):
        try:
            msgs = _msgs_for(data, s["email"])
            state, error = ("missing", "caixa ausente no provedor") if msgs is None else ("ok" if msgs else "empty", "")
        except ValueError as e:
            error = str(e)
    if msgs is not None:
        s["messages"] = msgs
        s["seen"].update(m["mid"] for m in msgs)
    s["inbox_state"], s["error"] = state, error
    cached = s.get("messages")
    return {"session_id": s.get("_sid"), "email": s["email"], "state": state,
            "error": error, "stale": msgs is None, "messages": cached,
            "count": len(cached) if cached is not None else None, "confirmed": s.get("confirmed", [])}


def _busy_inbox(s):
    return {"session_id": s.get("_sid"), "email": s["email"], "state": "in_progress",
            "stale": True, "messages": s.get("messages"), "error": "driver ocupado",
            "confirmed": s.get("confirmed", [])}


def _query_inboxes():
    started = time.monotonic()
    try:
        return _page_loop_all(_driver_locked())
    except HTTPException as e:
        if e.status_code != 503:
            raise
        _on_driver_failure(_DRV.get("error") or "driver desconectado")
        return {"ok": False, "error": "driver desconectado"}
    finally:
        _log_event(stage='inbox', elapsed_ms=round((time.monotonic() - started) * 1000))


def _confirm_cached(token, targets, wait_s):
    try:
        # Lote limitado; proximo sweep continua. Lock liberado entre mensagens.
        for sid, s, msgs in targets[:MAX_BOXES]:
            with _LOCK:
                if sessions.get(sid) is not s or s.get("status") == "disconnected" or _expired(s):
                    continue
                candidates = _confirm_targets(s, msgs)[:1]
                if candidates:
                    s["confirmed"] = [_confirm_one(_driver_locked(), s, candidates[0], wait_s)]
            time.sleep(0)
    except Exception as exc:
        for _, s, _ in targets:
            s['confirm_error'] = 'confirm-background:' + type(exc).__name__
        _log_event(stage="confirm-background", result="falha; tentativas preservadas")
    finally:
        _poll_end(token)


@app.get("/email/check")
def check(session_id: str = Query(...)):
    # GET somente leitura (Etapa 5). Sem efeitos externos.
    s = sessions.get(session_id)
    if not s:
        raise HTTPException(404, "sessao inexistente")
    if not _LOCK.acquire(blocking=False):
        return _busy_inbox(s)
    try:
        return _inbox_result(s, _query_inboxes())
    finally:
        _LOCK.release()


@app.post("/email/poll")
def poll(req: PollReq, background_tasks: BackgroundTasks):
    # Resposta inbox primeiro; auto-confirm default apos envio HTTP.
    # Pulo benigno (200 skipped) se outro poll em andamento — nunca 409.
    s = sessions.get(req.session_id)
    if not s:
        raise HTTPException(404, "sessao inexistente")
    token = _poll_begin("poll")
    if not token:
        out = {"email": s["email"], "state": "skipped", "stale": True}
        out.update(_poll_skipped())
        return out
    try:
        if not _LOCK.acquire(blocking=False):
            return _busy_inbox(s)
        try:
            result = _inbox_result(s, _query_inboxes())
            if req.auto_confirm and result["state"] == "ok":
                background_tasks.add_task(_confirm_cached, token, [(req.session_id, s, result["messages"])], req.wait_s)
                token = None  # ownership transferido ao background ate conclusao
        finally:
            _LOCK.release()
    finally:
        if token:
            _poll_end(token)
    return result


@app.get("/email/debug")
def debug(session_id: str = Query(...)):
    s = sessions.get(session_id)
    if not s:
        raise HTTPException(404, "sessao inexistente")
    d = _DRV.get("d")
    alive = False
    if d is not None:
        try:
            alive = d.execute_script("return 1") == 1
        except Exception:
            alive = False
    if not alive:
        return {"email": s["email"], "driver": False,
                "status": s.get("status"), "error": s.get("error")}
    try:
        raw = d.execute_script(
            'try{const el=document.querySelector(\'[x-data="TemporaryEmail()"]\');'
            "const t=Alpine.$data(el);"
            "return JSON.stringify({n:t.emails.length,"
            "addrs:t.emails.map(e=>e.address),"
            "sel:t.selectedEmail?t.selectedEmail.address:null,"
            "sel_n:(t.selectedEmail&&t.selectedEmail.messages||[]).length});}catch(e){return null}")
        alp = json.loads(raw) if raw else None
    except Exception:
        alp = None
    return {"email": s["email"], "provider": s.get("provider"),
            "driver": True, "status": s.get("status"), "alpine": alp}


@app.get("/email/events")
def events(session_id: str = Query(""), limit: int = Query(100)):
    # Eventos redatidos (sem tokens/cookies/URLs completas).
    out = [e for e in _EVENTS if not session_id or e.get("sid") == session_id]
    return {"events": out[-max(1, min(limit, 500)):]}


@app.get("/email/body")
def body(session_id: str = Query(...), mid: str = Query(...)):
    s = sessions.get(session_id)
    if not s:
        raise HTTPException(404, "sessao inexistente")
    if mid in s.get("bodies", {}):
        b, berr = s["bodies"][mid], ""
    elif not _LOCK.acquire(blocking=False):
        return {"state": "in_progress", "error": "", "status": "driver ocupado; tente novamente"}
    else:
        try:
            if s.get("status") == "disconnected":
                raise HTTPException(503, "sessao desconectada")
            d = _driver_locked()
            b, berr = _cached_body(d, s, mid)
        finally:
            _LOCK.release()
    cands = _extract_links(b) if not berr else []
    confirmations = _extract_links(b, confirmation_only=True) if not berr else []
    return {"email": s["email"], "mid": mid, "body_len": len(b),
            "links": cands[:50], "best": confirmations[0] if len(confirmations) == 1 else "",
            "state": "ambiguous" if len(confirmations) > 1 else "unknown",
            "candidates": confirmations,
            "body_snippet": b[:2000] if b else "",
             "error": berr}


@app.post("/email/open")
def open_message(req: OpenReq):
    s = sessions.get(req.session_id)
    if not s:
        raise HTTPException(404, "sessao inexistente")
    m = next((m for m in s.get("messages", []) if str(m["mid"]) == req.mid), None)
    if m is None:
        raise HTTPException(404, "mensagem inexistente; atualize inbox")
    token = _poll_begin('open')
    if not token:
        return {'state': 'in_progress', 'status': 'confirmacao em andamento', 'skipped': True}
    if not _LOCK.acquire(blocking=False):
        _poll_end(token)
        return {"state": "in_progress", "status": "driver ocupado; tentativa em andamento, tente novamente", "skipped": True}
    try:
        if sessions.get(req.session_id) is not s:
            raise HTTPException(404, "sessao removida")
        manual_link = None
        if req.link:
            try:
                _validate_destination(req.link)
            except ValueError as e:
                raise HTTPException(400, f"link bloqueado: {e}")
            d = _driver_locked()
            b, berr = _cached_body(d, s, req.mid)
            cands = _extract_links(b, confirmation_only=True) if b and not berr else []
            if req.link not in cands:
                raise HTTPException(400, "link nao encontrado nos candidatos de confirmacao do corpo")
            manual_link = req.link
        return _confirm_one(_driver_locked(), s, m, req.wait_s, req.revalidate, manual_link=manual_link)
    finally:
        _LOCK.release()
        _poll_end(token)


@app.post("/email/autoconfirm")
def autoconfirm(req: ConfirmReq):
    s = sessions.get(req.session_id)
    if not s:
        raise HTTPException(404, "sessao inexistente")
    token = _poll_begin("autoconfirm")
    if not token:
        out = {"email": s["email"], "confirmed": [], "deleted": False}
        out.update(_poll_skipped())
        return out
    try:
        with _LOCK:
            d = _driver_locked()
            if s.get("status") == "disconnected":
                raise HTTPException(503, "sessao desconectada")
            if _expired(s):
                return {'email': s['email'], 'confirmed': [], 'deleted': False, 'state': 'expired'}
            result = _inbox_result(s, _query_inboxes()) if "messages" not in s else None
            if (result and result["stale"]) or s.get("inbox_state") not in ("ok", "empty"):
                return {"email": s["email"], "confirmed": [], "deleted": False, "state": s.get("inbox_state")}
            msgs = s["messages"]
            done = []
            for m in _confirm_targets(s, msgs)[:1]:
                done.append(_confirm_one(d, s, m, req.wait_s))
    finally:
        _poll_end(token)
    return {"email": s["email"], "confirmed": done, "deleted": False,
            "confirmation": s.get('confirmation'), "monitoring": _monitoring(s),
            "verified": bool(s.get('verified')), "state": _monitor_state(s)}


@app.post("/email/sweep")
def sweep(req: SweepReq, background_tasks: BackgroundTasks):
    # Um loop; auto-confirm default apos resposta, mantendo token exclusivo.
    token = _poll_begin("sweep")
    if not token:
        out = {"results": []}
        out.update(_poll_skipped())
        return out
    try:
        if not _LOCK.acquire(blocking=False):
            return {"results": [_busy_inbox(s) for s in list(sessions.values())]}
        try:
            data = _query_inboxes()
            out = []
            targets = []
            for sid, s in list(sessions.items()):
                result = _inbox_result(s, data)
                out.append(result)
                if result["state"] == "ok":
                    targets.append((sid, s, result["messages"]))
            if req.auto_confirm and targets:
                background_tasks.add_task(_confirm_cached, token, targets, req.wait_s)
                token = None
        finally:
            _LOCK.release()
    finally:
        if token:
            _poll_end(token)
    return {"results": out}


@app.post("/email/release")
def release(req: ReleaseReq):
    # Ciclo criar > usar > confirmar > apagar: libera a sessao da memoria
    # imediatamente, sem esperar o provedor. A exclusao da caixa entra em
    # fila assincrona; o proximo create nao espera essa limpeza.
    s = _drop(req.session_id, 'release')
    if s is None:
        return {"ok": True, "released": False}
    _enqueue_delete(s.get("email"))
    return {"ok": True, "released": True, "email": s.get("email")}


@app.delete("/email/{sid}")
def remove(sid: str):
    # Exclusao sincrona: tenta excluir no provedor antes de responder.
    # Para liberar rapido e limpar em segundo plano, use POST /email/release.
    s = sessions.get(sid)
    if s is None:
        return {"ok": True}
    with _LOCK:
        d = _DRV.get("d")
        if d is not None:
            try:
                if d.execute_script("return 1") == 1:
                    _page_delete(d, s["email"])
            except Exception:
                pass
        _drop(sid)
    return {"ok": True}


@app.post("/admin/shutdown")
def shutdown():
    # Fecha somente o chrome do projeto (nunca o navegador do usuario).
    with _LOCK:
        _shutdown()
        sessions.clear()
        _log_event(stage="shutdown", result="driver proprio fechado")
    return {"ok": True}
