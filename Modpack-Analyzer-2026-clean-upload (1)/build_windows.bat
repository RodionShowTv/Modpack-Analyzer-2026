@echo off
cd /d "%~dp0"
echo Installing build tools...
python -m pip install --upgrade pyinstaller tkinterdnd2
echo Building exe...
python -m PyInstaller --noconfirm --onefile --windowed --name "Modpack Analyzer 2026" --icon icon.ico --collect-all tkinterdnd2 modpack_analyzer_2026.py
echo.
echo Done: dist\Modpack Analyzer 2026.exe
pause
