@echo off
setlocal
cd /d "%~dp0"
title SC Extractor

rem --- 1. environnement Python (cree et rempli au premier lancement) ---
if not exist ".venv\Scripts\python.exe" (
    echo Premier lancement : creation de l'environnement Python...
    python -m venv .venv
    if errorlevel 1 (
        echo Python 3 est introuvable. Installez-le depuis python.org puis relancez.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install -q --only-binary=:all: pycryptodome zstandard trimesh numpy
    if errorlevel 1 (
        echo Echec de l'installation des dependances.
        pause
        exit /b 1
    )
)

rem --- 2. outils externes ---
if not exist "tools\starbreaker\starbreaker.exe" echo ATTENTION : tools\starbreaker\starbreaker.exe manquant, la conversion STL ne marchera pas.
if not exist "tools\blender" echo ATTENTION : tools\blender manquant, apercu et reparation indisponibles.

if /i "%~1"=="setup" (
    echo Installation verifiee.
    exit /b 0
)

rem --- 3. lancement (sans console) ; un chemin de Data.p4k peut etre passe en argument ---
start "" ".venv\Scripts\pythonw.exe" sc_browser.py %*
endlocal
