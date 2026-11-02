@echo off
rem Daily SignalForge run for Task Scheduler. PAPER ONLY.
rem Each step runs even if an earlier one failed; every step's exit code is logged.
rem Order matters: the collector writes the warehouse, the later steps read it.
cd /d "%~dp0.."
set PY=.venv\Scripts\python.exe
set LOG=data\warehouse\daily.log

echo ==== %DATE% %TIME% start>> %LOG%

rem One batch of the full universe, research names first; signalforge-prices.cmd
rem rotates through the rest hourly.
echo -- pull Tiingo prices>> %LOG%
%PY% -m signalforge.collector_cli --config config\collection-tiingo-universe.json --cycles 1 >> %LOG% 2>&1
>> %LOG% echo exit %ERRORLEVEL%

echo -- paper trade the universe book>> %LOG%
%PY% -m signalforge.paper_cli --universe >> %LOG% 2>&1
>> %LOG% echo exit %ERRORLEVEL%

rem Refuses (logged, exit 3) until the book is approved and started with --start.
echo -- paper trade the allocation book>> %LOG%
%PY% -m signalforge.paper_cli --allocation >> %LOG% 2>&1
>> %LOG% echo exit %ERRORLEVEL%

echo -- write the Allocation tab>> %LOG%
%PY% -m signalforge.allocation_cli --state >> %LOG% 2>&1
>> %LOG% echo exit %ERRORLEVEL%

echo -- send reports to the dashboard>> %LOG%
%PY% -m signalforge.report_cli push >> %LOG% 2>&1
>> %LOG% echo exit %ERRORLEVEL%

echo -- mirror market data to D1>> %LOG%
%PY% -m signalforge.mirror_cli sync >> %LOG% 2>&1
>> %LOG% echo exit %ERRORLEVEL%

echo ==== %DATE% %TIME% end>> %LOG%
