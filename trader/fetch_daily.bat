@echo off
:: Daily fetcher — runs at 8 AM, fetches yesterday's TRADES + BID_ASK
:: Scheduled via: schtasks /create ... (see fetch_daily_schedule.md)

set PYTHON=C:\Users\gaviShalev\AppData\Local\Programs\Python\Python311\python.exe
set TRADER=C:\Projects\galgo2026\trader
set LOGDIR=C:\Projects\galgo2026\logs

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

set LOGFILE=%LOGDIR%\fetch_daily_%date:~-4,4%%date:~-10,2%%date:~-7,2%.log

echo [%date% %time%] fetch_daily starting >> "%LOGFILE%"
"%PYTHON%" "%TRADER%\fetcher.py" --bid-ask >> "%LOGFILE%" 2>&1
echo [%date% %time%] fetch_daily done (exit %errorlevel%) >> "%LOGFILE%"
