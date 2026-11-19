@echo off
REM Builds the desktop app (pahebatcher-gui.exe) and the CLI (pahebatcher.exe) with the icon.
REM Run from the project folder. Needs Python 3.11+ on PATH.
setlocal
set ICON=src\pahebatcher\gui\assets\icon.ico

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv || goto :fail
)
call venv\Scripts\activate.bat || goto :fail

echo Cleaning old build output so the new icon is used...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
del /q *.spec 2>nul

echo Installing dependencies...
pip uninstall -y pahebatcher >nul 2>&1
python -m pip install --upgrade pip >nul
pip install -e ".[browser]" pyinstaller || goto :fail

REM The built-in browser solver drives the Edge/Chrome already installed on the PC, so no browser
REM is bundled. Remove any Chromium left in the package by an earlier build, or it would be packed in.
if exist "venv\Lib\site-packages\patchright\driver\package\.local-browsers" (
    rmdir /s /q "venv\Lib\site-packages\patchright\driver\package\.local-browsers"
)

echo Building desktop app...
pyinstaller scripts\pyinstaller_gui_entry.py --onefile --windowed --clean --noconfirm ^
  --name pahebatcher-gui --icon %ICON% --paths src ^
  --collect-all curl_cffi --collect-all Cryptodome --collect-all patchright --add-data "src\pahebatcher\gui\assets;pahebatcher\gui\assets" ^
  --collect-submodules pahebatcher || goto :fail

echo Building command-line app...
pyinstaller scripts\pyinstaller_entry.py --onefile --clean --noconfirm ^
  --name pahebatcher --icon %ICON% --paths src ^
  --collect-all curl_cffi --collect-all Cryptodome --collect-all patchright ^
  --collect-submodules pahebatcher || goto :fail

echo.
REM Ask Windows to refresh its icon cache so Explorer shows the new icon right away.
ie4uinit.exe -show 2>nul

echo Done:
echo   dist\pahebatcher-gui.exe   (double-click this one)
echo   dist\pahebatcher.exe       (terminal version)
exit /b 0

:fail
echo.
echo Build failed - scroll up for the error.
exit /b 1
