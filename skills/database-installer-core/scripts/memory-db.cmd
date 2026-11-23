@echo off
setlocal
set "PYTHON=%MEMORY_DB_PYTHON%"
if not defined PYTHON set "PYTHON=python"
"%PYTHON%" "%~dp0db_skill.py" %*
exit /b %ERRORLEVEL%
