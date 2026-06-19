@echo off
setlocal
cd /d "%~dp0"

echo === Gara + Roar Alarm setup ===
where py >nul 2>&1
if errorlevel 1 (
  echo Python launcher "py" was not found.
  echo Install Python 3.10 or newer from python.org, then run this again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating local Python environment...
  py -3 -m venv .venv
  if errorlevel 1 goto :fail
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 goto :fail
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo.
where tesseract >nul 2>&1
if errorlevel 1 (
  if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" goto :tess_ok
  echo WARNING: Tesseract OCR was not found yet.
  echo Install Tesseract 5, normally to C:\Program Files\Tesseract-OCR.
  echo Official installation notes: https://tesseract-ocr.github.io/tessdoc/Installation.html
  echo Then run calibrate.bat.
  goto :done
)

:tess_ok
echo Tesseract OCR found.

:done
echo.
echo Setup complete. Next: run calibrate.bat.
pause
exit /b 0

:fail
echo.
echo Setup failed. Review the error above.
pause
exit /b 1
