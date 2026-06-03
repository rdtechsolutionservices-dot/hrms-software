@echo off
title VPL PayRoll System
color 0A
echo.
echo  =====================================================
echo    VIJAYSHRI PACKAGING LTD. - PAYROLL SYSTEM
echo  =====================================================
echo.

echo  [1/4] Checking Python...
python --version
if %errorlevel% neq 0 (
    echo  ERROR: Python not found! Please install Python 3.x
    echo  Download from: https://www.python.org/downloads/
    pause
    exit
)

echo.
echo  [2/4] Installing required packages...
pip install flask openpyxl pymysql pyodbc pyzk --quiet --no-warn-script-location
echo  Packages ready!

echo.
echo  [3/4] Setting up ADMS Port Redirect (89 to 5000)...
echo  NOTE: This requires Administrator privileges!
netsh interface portproxy delete v4tov4 listenport=89 listenaddress=0.0.0.0 >nul 2>&1
netsh interface portproxy add v4tov4 listenport=89 listenaddress=0.0.0.0 connectport=5000 connectaddress=127.0.0.1
if %errorlevel% neq 0 (
    echo  WARNING: Port redirect failed - Run as Administrator!
    echo  ADMS machines may not connect on port 89.
) else (
    echo  Port 89 successfully redirected to 5000!
)

echo.
echo  [4/4] Starting PayRoll System...
echo.
echo  =====================================================
echo    Browser : http://localhost:5000
echo    LAN     : http://192.168.0.3:5000
echo.
echo    ADMS Machine Setting:
echo    Server Address : 192.168.0.3
echo    Server Port    : 89
echo.
echo    Login   : admin / Admin@123
echo    Manager : manager / Manager@123
echo.
echo    DO NOT CLOSE THIS WINDOW!
echo  =====================================================
echo.

cd /d "%~dp0"
python app.py

if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Server failed to start!
    echo  Please check the error above.
)

echo.
echo  Cleaning up port redirect...
netsh interface portproxy delete v4tov4 listenport=89 listenaddress=0.0.0.0 >nul 2>&1
pause
