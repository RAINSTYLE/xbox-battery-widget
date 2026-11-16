@echo off
rem Сборка XboxBatteryWidget.exe (нужен установленный Python 3.10+)
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv || goto :error
)
".venv\Scripts\python.exe" -m pip install -q --upgrade pip || goto :error
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :error
".venv\Scripts\python.exe" make_icon.py || goto :error

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --name XboxBatteryWidget --icon icon.ico ^
    --collect-submodules winrt ^
    xbox_battery_widget.py || goto :error

copy /y "dist\XboxBatteryWidget.exe" "XboxBatteryWidget.exe" >nul || goto :error

echo.
echo Готово: XboxBatteryWidget.exe
exit /b 0

:error
echo Сборка не удалась.
exit /b 1
