#!/bin/sh
cd "$(dirname "$0")"
python3 -m pip install --upgrade pyinstaller tkinterdnd2
python3 -m PyInstaller --noconfirm --onefile --windowed --name "Modpack Analyzer 2026" --collect-all tkinterdnd2 modpack_analyzer_2026.py
echo "Done: dist/"
