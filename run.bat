@echo off
rem Запуск pasport-queue-watcher у вікні (подвійний клік по цьому файлу).
rem UTF-8, щоб україномовні логи читались нормально.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title pasport-queue-watcher
echo ==================================================================
echo  pasport-queue-watcher - stezhennya zapushcheno
echo  Perevirka kozhni 5 khv (LOOP_SECONDS u .env).
echo  Shchob zupynyty: zakryy tse vikno abo natysny Ctrl+C.
echo ==================================================================
echo.
".venv\Scripts\python.exe" watch.py
echo.
echo Watcher zupynyvsya. Natysny bud-yaku klavishu, shchob zakryty.
pause >nul
