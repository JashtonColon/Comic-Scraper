@echo off
REM ==========================================================
REM  Builds "Comic Scraper.exe" and (if Inno Setup is installed)
REM  the ComicScraper-Setup.exe installer.  Double-click to run.
REM ==========================================================
cd /d "%~dp0.."

echo [1/3] Installing build tools...
python -m pip install --upgrade -r requirements.txt pyinstaller || goto :error

echo [2/3] Building the app...
python -m PyInstaller --noconfirm --clean --windowed ^
  --name "Comic Scraper" ^
  --icon assets\icon.ico ^
  --add-data "assets\icon.ico;." ^
  --collect-data playwright ^
  app\gui.py || goto :error

echo [3/3] Building the installer...
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist %ISCC% (
  echo.
  echo Inno Setup not found, so no installer was made.
  echo The app itself is ready in:  dist\Comic Scraper\Comic Scraper.exe
  echo To also make an installer, install Inno Setup 6 from https://jrsoftware.org/isdl.php and run this again.
  goto :done
)
%ISCC% installer\installer.iss || goto :error
echo.
echo Done! Installer: Output\ComicScraper-Setup.exe

:done
pause
exit /b 0

:error
echo.
echo Build failed - see the messages above.
pause
exit /b 1
