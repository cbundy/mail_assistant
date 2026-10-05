@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Создаю виртуальную среду...
  python -m venv .venv
)
call .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
pause
