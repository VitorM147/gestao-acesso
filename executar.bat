@echo off
REM Executa a automacao de reset de senha XCash. Pode ser usado no Agendador de Tarefas.
cd /d "%~dp0"
python main.py %*
