@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_windows.bat first.
  pause
  exit /b 1
)
echo What do you want to recalibrate?
echo   1. all
echo   2. Splinter Storm only
echo   3. Roar ready/off icon only
set /p CHOICE=Choose [1/all]: 
set TARGET=all
if "%CHOICE%"=="2" set TARGET=splinter_storm
if /I "%CHOICE%"=="splinter" set TARGET=splinter_storm
if /I "%CHOICE%"=="splinter_storm" set TARGET=splinter_storm
if "%CHOICE%"=="3" set TARGET=roar
if /I "%CHOICE%"=="roar" set TARGET=roar
".venv\Scripts\python.exe" gara_roar_alarm.py --calibrate --calibrate-buff %TARGET%
pause
