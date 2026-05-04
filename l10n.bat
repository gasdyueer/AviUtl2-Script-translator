@echo off
chcp 65001 > nul
cd /d "%~dp0"

python aviutl2_l10n_cli.py -s Script -o Language 


