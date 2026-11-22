@echo off
setlocal
cd /d "%~dp0"
if exist "runtime\5.2\python\bin\python.exe" (
  "runtime\5.2\python\bin\python.exe" launch.py %*
) else (
  py -3 launch.py %*
)
set "ruster_exit=%errorlevel%"
if "%ruster_exit%"=="0" exit /b 0
:check_automation
if "%~1"=="" goto show_error
set "ruster_option="
for /f "tokens=1 delims==" %%A in ("%~1") do set "ruster_option=%%A"
if /I "%ruster_option%"=="--headless" exit /b %ruster_exit%
if /I "%ruster_option%"=="--validate-map" exit /b %ruster_exit%
if /I "%ruster_option%"=="--export-map" exit /b %ruster_exit%
if /I "%ruster_option%"=="--export-prefab" exit /b %ruster_exit%
if /I "%ruster_option%"=="--export-report" exit /b %ruster_exit%
shift
goto check_automation
:show_error
pause
exit /b %ruster_exit%
