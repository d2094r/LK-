@echo off
cd /d "%~dp0"
if not defined DISCORD_TOKEN (
    echo DISCORD_TOKEN is not set.
    echo Example: set "DISCORD_TOKEN=your_bot_token"
    pause
    exit /b 1
)
py -3 lk_bot.py
pause
