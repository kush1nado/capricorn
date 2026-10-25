@echo off
rem Capricorn launcher (double-click). Without capricorn.yaml runs on the offline mock.
chcp 65001 >nul
cd /d "%~dp0"
set NO_COLOR=
if not exist ".venv\Scripts\capricorn.exe" (
  echo Нет .venv. Установи: python -m venv .venv ^&^& .venv\Scripts\python -m pip install -e .[dev]
  pause
  exit /b 1
)
title Capricorn
".venv\Scripts\capricorn.exe" %*
