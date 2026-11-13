@echo off
title Emissor NFS-e (deixe esta janela aberta)
cd /d "%~dp0"
echo.
echo  Emissor NFS-e
echo  ------------------------------------------------------------
echo  Acesse neste computador:  http://127.0.0.1:5000
echo.
echo  (Acesso pela rede da equipe desligado por enquanto.
echo   Para ligar depois que a TI liberar, veja config.py - HOST.)
echo.
echo  Deixe esta janela ABERTA enquanto usar o sistema.
echo  Ao fechar a janela, o sistema desliga e o certificado sai da memoria.
echo  ------------------------------------------------------------
echo.
".venv\Scripts\python.exe" app.py
echo.
echo  O sistema parou. Pressione uma tecla para fechar.
pause >nul
