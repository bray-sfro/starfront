@echo off
rem  Starts the collaboration server and leaves this window open showing it.
rem
rem  There is no token in this file on purpose. The server keeps its own beside
rem  its database, outside the program folder, because this folder goes to
rem  Dropbox and a credential that can rewrite every project has no business
rem  being synchronised anywhere.
rem
rem  This listens on this machine only. To let a telescope on another PC reach
rem  it, add   --host 0.0.0.0   to the line below - and be aware that is a real
rem  decision: anything on your network can then talk to it.

title Starfront collaboration server
cd /d "%~dp0"
python "server\run.py" --port 8800
echo.
echo The server has stopped.
pause
