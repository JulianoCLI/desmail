"""python local_browser_check.py — somente localhost, driver instalado, sem downloads."""
import pathlib
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import seleniumbase
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
import app


def main():
    class Page(BaseHTTPRequestHandler):
        visits = []
        def do_GET(self):
            self.visits.append(self.path)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write((app.local_confirm() + '<img src="/blocked"><script>fetch("/blocked")</script>').encode())
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    driver = None
    profile = tempfile.TemporaryDirectory()
    try:
            options = webdriver.ChromeOptions()
            options.add_argument('--headless=new')
            options.add_argument('--disable-background-networking')
            options.add_argument(f'--user-data-dir={profile.name}')
            executable = pathlib.Path(seleniumbase.__file__).parent / 'drivers' / 'chromedriver.exe'
            driver = webdriver.Chrome(service=Service(str(executable)), options=options)
            driver.open = driver.get
            driver.sleep = lambda seconds: None
            url = f'http://127.0.0.1:{server.server_port}/test/local-confirm'
            inbox_url = 'data:text/html,<h1>Inbox offline</h1>'
            driver.get(inbox_url)
            inbox = driver.current_window_handle
            Page.visits.clear()
            # Navegacao leve permite JS/redirects/subresources na aba criada.
            # Somente preflight substituido no teste.
            with patch.object(app, '_log_event'), patch.object(app, '_validate_destination'):
                opened, status, evidence = app._open_confirm_isolated(driver, inbox, url, 0)
                assert opened and not evidence['verified'], status
                assert '/test/local-confirm' in Page.visits, Page.visits
            opened, status, _ = app._open_confirm_isolated(driver, inbox, url, 0)
            assert not opened and 'URL bloqueada' in status
            assert driver.window_handles == [inbox] and driver.current_window_handle == inbox
            assert driver.current_url == inbox_url
            print('PASS: localhost Chrome; isolated tab closed; inbox preserved; opened not verified; JS leve permitido; production localhost rejected')
            driver.quit()
            driver = None
    finally:
        if driver is not None: driver.quit()
        server.shutdown()
        server.server_close()
        thread.join(3)
        profile.cleanup()


if __name__ == '__main__':
    main()
