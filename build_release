@echo off
REM ==========================================================================
REM  mellojoy release build (ASCII only, to avoid CP932 mojibake / crash)
REM    1) build mellojoy.exe / mellojoy-launcher.exe with icon
REM    2) make mellojoy_app.zip  (mellojoy.exe + fire_engine.exe + libcurl.dll)
REM
REM  Put this .bat in the SAME folder as:
REM    mellojoy.py  launcher.py  fire_engine.exe  libcurl.dll  redcap_squishy.ico
REM  Then double-click it.
REM
REM  Output:
REM    dist\mellojoy-launcher.exe   -> give this to users
REM    mellojoy_app.zip             -> attach this to a GitHub Release
REM
REM  Secret files (accounts.txt / proxies.txt / capsolver key etc.) are NOT
REM  included. The app auto-creates empty templates on first run.
REM ==========================================================================
cd /d "%~dp0"

set "ICON=redcap_squishy.ico"
set "APPDIR=app"
set "ZIP=mellojoy_app.zip"
set "PY=python"

echo.
echo === [0/5] Selecting a 64-bit Python (curl_cffi needs one) ===
REM  Prefer a 64-bit interpreter via the py launcher, so PATH order does not matter.
REM  Order: 3.12 -> 3.13 -> 3.11 -> python -> py. First 64-bit one wins.
set "PY="
for %%C in ("py -3.12" "py -3.13" "py -3.11" "python" "py") do (
    if not defined PY (
        %%~C -c "import struct,sys;sys.exit(0 if struct.calcsize('P')==8 else 1)" >nul 2>&1
        if not errorlevel 1 set "PY=%%~C"
    )
)
if not defined PY (
    echo   [WARN] No 64-bit Python found.
    echo          curl_cffi has no 32-bit wheel and will fail to build.
    echo          Please install 64-bit Python 3.12, then run this again.
    REM  fall back to any python just to show a clear error later
    python --version >nul 2>&1 && set "PY=python"
    if not defined PY ( py --version >nul 2>&1 && set "PY=py" )
    if not defined PY ( echo   [NG] No Python found on PATH. & goto :err )
)
echo   [OK] using: %PY%

echo.
echo === [1/5] Checking required files ===
set "MISSING="
if not exist "mellojoy.py"        ( echo   [NG] mellojoy.py & set "MISSING=1" )   else echo   [OK] mellojoy.py
if not exist "launcher.py"        ( echo   [NG] launcher.py & set "MISSING=1" )   else echo   [OK] launcher.py
if not exist "fire_engine.exe"    ( echo   [NG] fire_engine.exe & set "MISSING=1" ) else echo   [OK] fire_engine.exe
if not exist "libcurl.dll"        ( echo   [NG] libcurl.dll & set "MISSING=1" )   else echo   [OK] libcurl.dll
if not exist "%ICON%"             ( echo   [NG] %ICON% & set "MISSING=1" )         else echo   [OK] %ICON%
if defined MISSING (
    echo.
    echo   [ABORT] Put the missing files in this folder, then run again.
    goto :err
)

echo.
echo === [2/5] Checking PyInstaller and runtime deps ===
%PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo   PyInstaller not found - installing...
    %PY% -m pip install pyinstaller
    if errorlevel 1 goto :err
) else (
    echo   [OK] PyInstaller present
)
REM --- show Python version/bitness. curl_cffi needs a 64-bit wheel. ---
for /f "delims=" %%V in ('%PY% -c "import sys,struct;print(sys.version.split()[0]+' '+str(struct.calcsize('P')*8)+'-bit')"') do set "PYINFO=%%V"
echo   Python: %PYINFO%
%PY% -c "import struct,sys;sys.exit(0 if struct.calcsize('P')==8 else 1)"
if errorlevel 1 (
    echo   [WARN] 32-bit Python detected.
    echo          curl_cffi has NO 32-bit wheel - pip will try a source build and
    echo          usually FAILS. Install 64-bit Python 3.12 and run this again.
)

echo   Installing required deps (curl_cffi / requests)...
%PY% -m pip install "curl_cffi>=0.7" "requests>=2.31"
if errorlevel 1 (
    echo.
    echo   [ERROR] Could not install curl_cffi/requests.
    echo           Most likely cause: your Python has no prebuilt curl_cffi wheel
    echo           ^(32-bit, or a too-new version like 3.14^).
    echo           FIX: install 64-bit Python 3.12, then run build_release.bat again.
    goto :err
)
echo   Installing optional deps (colorama)...
%PY% -m pip install "colorama>=0.4"
if errorlevel 1 echo   [WARN] colorama failed (optional) - continuing without colors.

echo.
echo === [3/5] Building exe with icon ===
REM  mellojoy.exe needs curl_cffi fully collected (C-extension + bundled data),
REM  otherwise it fails at runtime with: No module named 'curl_cffi'.
%PY% -m PyInstaller --onefile --clean --noconfirm --name mellojoy --icon "%ICON%" ^
    --collect-all curl_cffi --hidden-import requests mellojoy.py
if errorlevel 1 goto :err
%PY% -m PyInstaller --onefile --clean --noconfirm --name mellojoy-launcher --icon "%ICON%" launcher.py
if errorlevel 1 goto :err

echo.
echo === [4/5] Staging app folder ===
if exist "%APPDIR%" rmdir /s /q "%APPDIR%"
mkdir "%APPDIR%"
copy /y "dist\mellojoy.exe" "%APPDIR%\" >nul
if errorlevel 1 goto :err
copy /y "fire_engine.exe" "%APPDIR%\" >nul
if errorlevel 1 goto :err
copy /y "libcurl.dll" "%APPDIR%\" >nul
if errorlevel 1 goto :err
echo   app\ = mellojoy.exe + fire_engine.exe + libcurl.dll

echo.
echo === [5/5] Creating %ZIP% ===
if exist "%ZIP%" del /f /q "%ZIP%"
powershell -NoProfile -Command "Compress-Archive -Path '%APPDIR%\*' -DestinationPath '%ZIP%' -Force"
if errorlevel 1 goto :err

echo.
echo ==========================================================================
echo  DONE
echo    ZIP for release : %CD%\%ZIP%
echo    exe for users   : %CD%\dist\mellojoy-launcher.exe
echo.
echo  Next: GitHub Release -> new tag v3.0.0 -> attach mellojoy_app.zip -> Publish
echo ==========================================================================
echo.
pause
exit /b 0

:err
echo.
echo  [ERROR] Build failed. Read the message above.
echo.
pause
exit /b 1
