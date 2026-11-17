@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title build fire_engine.exe

echo ============================================================
echo   fire_engine.exe build  (MinGW-w64 g++, C++17, static)
echo ============================================================

rem ---- 1) source files -------------------------------------------------------
set "MISSING="
for %%F in (fire_engine.cpp curl_min.h json.hpp) do (
  if not exist "%%F" set "MISSING=!MISSING! %%F"
)
if defined MISSING (
  echo [ERROR] Missing in this folder:!MISSING!
  echo         Put fire_engine.cpp, curl_min.h and json.hpp next to this bat.
  goto :fail
)

rem ---- 2) find g++ -----------------------------------------------------------
set "GPP="
where g++ >nul 2>nul && set "GPP=g++"
if not defined GPP call :try "C:\msys64\ucrt64\bin"
if not defined GPP call :try "C:\msys64\mingw64\bin"
if not defined GPP call :try "C:\msys64\clang64\bin"
if not defined GPP call :try "C:\mingw64\bin"
if not defined GPP call :try "C:\MinGW\bin"
if not defined GPP call :try "C:\TDM-GCC-64\bin"
if not defined GPP call :try "%ProgramFiles%\mingw64\bin"
if not defined GPP (
  echo [ERROR] g++ was not found.
  echo   Install MSYS2 from https://www.msys2.org/ and run in "MSYS2 UCRT64":
  echo       pacman -S --needed mingw-w64-ucrt-x86_64-gcc
  echo   then run this bat again.
  goto :fail
)
echo [OK] compiler: !GPP!
for /f "tokens=*" %%L in ('"!GPP!" -dumpfullversion 2^>nul') do echo      version : %%L
rem --- thread model（入れ子パイプを避けて temp 経由で判定。posix 必須）---
set "TM="
"!GPP!" -v > "%TEMP%\fe_gpp_v.txt" 2>&1
for /f "tokens=2 delims=:" %%L in ('findstr /c:"Thread model" "%TEMP%\fe_gpp_v.txt"') do set "TM=%%L"
del /f /q "%TEMP%\fe_gpp_v.txt" >nul 2>nul
if defined TM echo      threads :!TM!

rem ---- 3) compile to a temp name (keeps the old exe if it fails) -------------
echo.
echo [..] compiling fire_engine.cpp ...
if exist fire_engine.new.exe del /f /q fire_engine.new.exe >nul 2>nul
"!GPP!" -std=c++17 -O2 -s -finput-charset=UTF-8 -fexec-charset=UTF-8 -static -static-libgcc -static-libstdc++ -pthread ^
  -o fire_engine.new.exe fire_engine.cpp
if errorlevel 1 (
  echo.
  echo [ERROR] compile failed. Copy the messages above.
  echo         If it says "thread is not a member of std", your g++ uses the
  echo         win32 thread model. Use MSYS2 UCRT64 gcc ^(see above^).
  goto :fail
)
if not exist fire_engine.new.exe (
  echo [ERROR] fire_engine.new.exe was not produced.
  goto :fail
)

rem ---- 4) replace (backup the old one) ---------------------------------------
if exist fire_engine.exe (
  copy /y fire_engine.exe fire_engine.bak.exe >nul
  del /f /q fire_engine.exe >nul 2>nul
  if exist fire_engine.exe (
    echo [ERROR] fire_engine.exe is in use. Close mellojoy and run again.
    echo         New build is kept as fire_engine.new.exe
    goto :fail
  )
  echo [OK] old exe backed up to fire_engine.bak.exe
)
ren fire_engine.new.exe fire_engine.exe
echo [OK] built fire_engine.exe

rem ---- 5) smoke test (libcurl.dll load + JSON input) -------------------------
echo.
echo [..] smoke test ...
if not exist libcurl.dll (
  echo [WARN] libcurl.dll is not in this folder. fire_engine.exe needs it at run time.
)
echo {"packets":[]}| fire_engine.exe > "%TEMP%\fe_smoke.txt" 2>nul
set "OUT="
for /f "usebackq delims=" %%L in ("%TEMP%\fe_smoke.txt") do set "OUT=%%L"
rem (raw output is UTF-8 JSON; not echoed to avoid mojibake on cp932 console)
findstr /c:"no packets" "%TEMP%\fe_smoke.txt" >nul
if not errorlevel 1 (
  echo [OK] engine starts and libcurl.dll loads correctly.
) else (
  findstr /c:"libcurl" "%TEMP%\fe_smoke.txt" >nul
  if not errorlevel 1 (
    echo [WARN] engine starts, but libcurl.dll could not be loaded.
    echo        Put libcurl.dll next to fire_engine.exe.
  ) else (
    echo [WARN] unexpected output. Check the build.
  )
)
del /f /q "%TEMP%\fe_smoke.txt" >nul 2>nul

echo.
echo ============================================================
echo   DONE  - place fire_engine.exe and libcurl.dll next to mellojoy
echo ============================================================
pause
exit /b 0

:try
if exist "%~1\g++.exe" (
  set "GPP=%~1\g++.exe"
  set "PATH=%~1;%PATH%"
)
exit /b 0

:fail
echo.
echo BUILD FAILED
pause
exit /b 1
