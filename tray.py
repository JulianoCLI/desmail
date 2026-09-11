"""Icone de bandeja do Desmail: mostra que a API esta viva e encerra tudo.

Roda a API como processo filho. Sair pelo menu (ou fechar este processo)
encerra a API e todo chrome/uc_driver que ela abriu -- nada fica orfao.

Uso:
    python tray.py              # inicia API + icone
    python tray.py --port 8000  # porta alternativa
"""
import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from PIL import Image, ImageDraw
import pystray

DRIVER_NAMES = ("uc_driver.exe", "chromedriver.exe", "uc_driver", "chromedriver")


def _make_kill_on_close_job():
    """Job Object do Windows que mata os filhos quando este processo morre.

    Garantia do proprio SO: vale ate para encerramento forcado (Stop-Process
    -Force / Gerenciador de Tarefas), onde nenhum handler Python roda."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(wintypes.ULONG)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                        ("WriteOperationCount", ctypes.c_uint64),
                        ("OtherOperationCount", ctypes.c_uint64),
                        ("ReadTransferCount", ctypes.c_uint64),
                        ("WriteTransferCount", ctypes.c_uint64),
                        ("OtherTransferCount", ctypes.c_uint64)]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BasicLimit),
                        ("IoInfo", _IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info),
                                           ctypes.sizeof(info)):
            return None
        return job
    except Exception:
        return None


def _assign_to_job(job, pid):
    if not job:
        return False
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1F0FFF, False, int(pid))  # ALL_ACCESS
        if not handle:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(job, handle))
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False


def _icon(color):
    """Circulo solido: verde = API no ar, cinza = subindo/fora."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((8, 8, 56, 56), fill=color)
    return img


GREEN, GREY = (46, 160, 67, 255), (128, 128, 128, 255)


class Tray:
    def __init__(self, port):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.proc = None
        self.status = "iniciando"
        self.sessions = 0
        self.stop = threading.Event()
        self.job = _make_kill_on_close_job()
        self.icon = pystray.Icon(
            "desmail", _icon(GREY), "Desmail: iniciando...",
            menu=pystray.Menu(
                pystray.MenuItem(lambda _: f"Status: {self.status}", None, enabled=False),
                pystray.MenuItem(lambda _: f"Caixas ativas: {self.sessions}", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Encerrar Desmail", self.quit),
            ),
        )

    def start_api(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--workers", "1"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # API e todo chrome/driver que ela abrir herdam o Job: se este processo
        # morrer de qualquer jeito, o Windows encerra a arvore inteira.
        _assign_to_job(self.job, self.proc.pid)

    def poll_loop(self):
        while not self.stop.wait(3):
            if self.proc is not None and self.proc.poll() is not None:
                self.status, self.sessions = "API encerrada", 0
                self.icon.icon, self.icon.title = _icon(GREY), "Desmail: API encerrada"
                self.icon.stop()
                return
            try:
                with urllib.request.urlopen(self.base + "/sessions", timeout=2) as r:
                    self.sessions = len(json.loads(r.read()))
                self.status = "no ar"
                self.icon.icon = _icon(GREEN)
                self.icon.title = f"Desmail no ar ({self.port}) - {self.sessions} caixa(s)"
            except Exception:
                self.status = "sem resposta"
                self.icon.icon = _icon(GREY)
                self.icon.title = f"Desmail: sem resposta ({self.port})"

    def _shutdown_api(self):
        """Pede shutdown educado, depois encerra a arvore de processos."""
        try:
            req = urllib.request.Request(self.base + "/admin/shutdown", data=b"",
                                         method="POST")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
        if self.proc is None:
            return
        try:
            import psutil
            parent = psutil.Process(self.proc.pid)
            kids = parent.children(recursive=True)
        except Exception:
            parent, kids = None, []
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        # Rede de seguranca: chrome/driver que sobreviveram ao pai.
        for c in kids:
            try:
                if c.is_running():
                    c.kill()
            except Exception:
                pass
        self._kill_orphan_drivers()

    @staticmethod
    def _kill_orphan_drivers():
        try:
            import psutil
        except Exception:
            return
        for p in psutil.process_iter(["pid", "ppid", "name"]):
            try:
                if (p.info.get("name") or "") not in DRIVER_NAMES:
                    continue
                ppid = p.info.get("ppid")
                if ppid and psutil.pid_exists(ppid):
                    continue
                for c in p.children(recursive=True):
                    try:
                        c.kill()
                    except Exception:
                        pass
                p.kill()
            except Exception:
                continue

    def quit(self, *_):
        self.stop.set()
        self._shutdown_api()
        self.icon.stop()

    def run(self):
        self.start_api()
        threading.Thread(target=self.poll_loop, daemon=True).start()
        try:
            self.icon.run()
        finally:
            self.stop.set()
            if self.proc is not None and self.proc.poll() is None:
                self._shutdown_api()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    Tray(ap.parse_args().port).run()


if __name__ == "__main__":
    main()
