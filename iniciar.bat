@echo off
setlocal
cd /d "%~dp0"
title Desmail - Inicializacao

python --version >nul 2>&1
if errorlevel 1 (
    echo Python nao encontrado no PATH.
    pause
    exit /b 1
)

echo Instalando ou verificando dependencias...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Falha ao instalar dependencias. Inicializacao cancelada.
    pause
    exit /b 1
)

python -c "import socket; s=socket.socket(); s.settimeout(1); code=s.connect_ex(('127.0.0.1',8000)); s.close(); raise SystemExit(1 if code == 0 else 0)"
if errorlevel 1 (
    echo Porta 8000 ocupada. Feche a API anterior antes de iniciar.
    echo Nenhum processo sera encerrado automaticamente.
    pause
    exit /b 1
)

start "Desmail" /min pythonw tray.py --port 8000
echo Aguardando API iniciar...
python -c "import time, urllib.request; exec('for attempt in range(60):\n try:\n  with urllib.request.urlopen(\"http://127.0.0.1:8000/openapi.json\", timeout=1) as response:\n   assert response.status == 200\n  break\n except Exception:\n  time.sleep(1)\nelse:\n raise SystemExit(1)')"
if errorlevel 1 (
    echo API nao respondeu. Confira erros na janela Desmail API.
    pause
    exit /b 1
)

if /i not "%~1"=="tui" (
    echo API autonoma iniciada. Icone na bandeja ^(icones ocultos^).
    echo Para encerrar: botao direito no icone ^> Encerrar Desmail.
    echo Para TUI opcional: iniciar.bat tui
    exit /b 0
)
echo Iniciando TUI...
python tui.py
if errorlevel 1 (
    echo TUI terminou com erro. Confira mensagem acima.
    pause
    exit /b 1
)
echo TUI encerrada. API permanece aberta em sua propria janela.
endlocal
