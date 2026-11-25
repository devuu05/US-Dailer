@echo off
cd /d "%~dp0"
if not exist .venv (
  py -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
