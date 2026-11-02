@echo off
rem Hourly Tiingo price rotation for Task Scheduler. PAPER ONLY.
rem Each run fetches the 45 stalest of the universe's symbols (research names
rem first when tied), under Tiingo's free limits of 50 requests an hour and
rem 1,000 a day. Run it hourly, starting an hour after signalforge-daily.cmd so
rem the two never share a clock hour; eleven runs cover all 481 symbols.
cd /d "%~dp0.."
set PY=.venv\Scripts\python.exe
set LOG=data\warehouse\prices.log

echo ==== %DATE% %TIME% start>> %LOG%
%PY% -m signalforge.collector_cli --config config\collection-tiingo-universe.json --cycles 1 >> %LOG% 2>&1
>> %LOG% echo exit %ERRORLEVEL%
