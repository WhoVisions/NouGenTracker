@echo off
rem Real Ollama server, moved off the default port so the fleet usage proxy can
rem interpose. OLLAMA_HOST is scoped to THIS process only - clients keep
rem defaulting to 11434, which is the proxy. Do not setx this globally.

if "%FLEET_OLLAMA_UPSTREAM_PORT%"=="" set FLEET_OLLAMA_UPSTREAM_PORT=11436
set OLLAMA_HOST=127.0.0.1:%FLEET_OLLAMA_UPSTREAM_PORT%

rem The model store is derived from the current user profile rather than
rem inherited. A stale user-scope OLLAMA_MODELS pointing at an external drive
rem once produced a false "drive failure" diagnosis while every model sat in
rem the per-user store, so this line deliberately overrides it.
if "%OLLAMA_MODELS_DIR%"=="" set OLLAMA_MODELS_DIR=%USERPROFILE%\.ollama\models
set OLLAMA_MODELS=%OLLAMA_MODELS_DIR%

rem Binary: OLLAMA_BIN wins, else the standard per-user install location,
rem else whatever is on PATH.
if not "%OLLAMA_BIN%"=="" goto run
set OLLAMA_BIN=%LOCALAPPDATA%\Programs\Ollama\ollama.exe
if not exist "%OLLAMA_BIN%" set OLLAMA_BIN=ollama

:run
echo [fleet] upstream %OLLAMA_HOST%  models %OLLAMA_MODELS%  bin %OLLAMA_BIN%
"%OLLAMA_BIN%" serve
