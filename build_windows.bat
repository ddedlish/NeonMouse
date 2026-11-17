@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -q -r requirements.txt keyboard pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name NeonMouse main.py
echo.
echo Готово: dist\NeonMouse.exe
pause
