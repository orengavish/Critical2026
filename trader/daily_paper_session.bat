@echo off
:: Daily paper trading session — runs 17:00-19:00 Israel time
:: Generates ~100 random replenished MES trades for backtesting DB

set PYTHON=C:\Users\gaviShalev\AppData\Local\Programs\Python\Python311\python.exe
set TRADER=C:\Projects\galgo2026\trader
set LOGDIR=C:\Projects\galgo2026\logs

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

set LOGFILE=%LOGDIR%\daily_paper_%date:~-4,4%%date:~-10,2%%date:~-7,2%.log

echo [%date% %time%] daily_paper_session starting >> "%LOGFILE%"
"%PYTHON%" "%TRADER%\daily_paper_session.py" >> "%LOGFILE%" 2>&1
echo [%date% %time%] daily_paper_session done (exit %errorlevel%) >> "%LOGFILE%"
