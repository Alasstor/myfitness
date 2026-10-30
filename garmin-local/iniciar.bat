@echo off
title Garmin Local
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 app.py
  goto fin
)
where python >nul 2>nul
if %errorlevel%==0 (
  python app.py
  goto fin
)
echo No encuentro Python en este ordenador.
echo Instalalo desde https://www.python.org/downloads/ marcando "Add python.exe to PATH"
echo y vuelve a abrir este archivo.
:fin
pause
