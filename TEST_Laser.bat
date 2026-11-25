@echo off
rem Laser connection test (no CNC needed). Close sensorTOOL first. Optional: TEST_Laser.bat COM5
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" ( echo Run START_BowMapper.bat once first to set up Python. & pause & exit /b 1 )
".venv\Scripts\python.exe" tools\laser_probe.py %*
pause
