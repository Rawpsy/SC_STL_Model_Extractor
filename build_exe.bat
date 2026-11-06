@echo off
rem Construit dist\SC_Extractor.exe (un seul fichier, sans console).
rem Blender et StarBreaker ne sont pas inclus : l'exe les telecharge au premier lancement.
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name SC_Extractor ^
  --add-data "blender_tasks.py;." ^
  --exclude-module trimesh --exclude-module matplotlib --exclude-module PyInstaller ^
  sc_browser.py
if errorlevel 1 (
    echo Echec de la construction.
    exit /b 1
)
certutil -hashfile dist\SC_Extractor.exe SHA256 | findstr /v ":"
echo Construit : dist\SC_Extractor.exe
endlocal
