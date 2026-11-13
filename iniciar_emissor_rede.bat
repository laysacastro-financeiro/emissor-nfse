@echo off
title Emissor NFS-e - REDE DA EQUIPE (deixe esta janela aberta)
cd /d "%~dp0"
set EMISSOR_HOST=0.0.0.0
echo.
echo  Emissor NFS-e - acesso pela rede da equipe LIGADO
echo  ------------------------------------------------------------
echo  Neste computador:  http://127.0.0.1:5000
echo.
echo  Os outros computadores da rede acessam por um destes enderecos:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do echo     http:%%a:5000
echo.
echo  Se os outros computadores nao abrirem, a porta 5000 esta bloqueada
echo  no Firewall do Windows deste computador (peca a TI para liberar a
echo  entrada TCP na porta 5000, somente para a rede interna).
echo.
echo  Cada pessoa entra com o proprio login. Deixe esta janela ABERTA
echo  enquanto a equipe usar o sistema e nao deixe o computador entrar
echo  em suspensao. Ao fechar a janela, o sistema desliga e o certificado
echo  A1 sai da memoria (e preciso carregar de novo).
echo  ------------------------------------------------------------
echo.
".venv\Scripts\python.exe" app.py
echo.
echo  O sistema parou. Pressione uma tecla para fechar.
pause >nul
