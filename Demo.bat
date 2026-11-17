@echo off
rem Modo demo: musicas falsas, para testar a interface sem tocar nada.
rem Feche a versao normal antes (icone na bandeja > Sair).
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" main.py --demo
