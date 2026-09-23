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

rem Ollama 0.32.6 defaults OLLAMA_VULKAN=true even with the var unset, and the
rem Vulkan backend restricts OLLAMA_LIBRARY_PATH to [lib\ollama, lib\ollama\vulkan]
rem - cuda_v12 and cuda_v13 are never searched. Measured 2026-09-07: dav1d:e2b
rem loaded 5.7 GB at "100%% CPU" with the RTX 2080 Super idle at 298 MiB. With
rem this set to 0 the same model loads 2.48 GB fully into VRAM (GPU 3,049 MiB).
rem Vulkan does detect both GPUs, so the symptom is not "no device found".
if "%OLLAMA_VULKAN%"=="" set OLLAMA_VULKAN=0

rem Binary: OLLAMA_BIN wins, else the standard per-user install location,
rem else whatever is on PATH.
if not "%OLLAMA_BIN%"=="" goto run
set OLLAMA_BIN=%LOCALAPPDATA%\Programs\Ollama\ollama.exe
if not exist "%OLLAMA_BIN%" set OLLAMA_BIN=ollama

:run
rem %LOCALAPPDATA%\Ollama\server.log only receives output when the desktop app
rem launches the server; started from here its stderr went nowhere, so the GPU
rem discovery lines had to be recovered from a throwaway probe server on a spare
rem port. Capture them so the next diagnosis reads a file instead.
if "%OLLAMA_LOG%"=="" set OLLAMA_LOG=%USERPROFILE%\.nougen\logs\ollama_upstream.log
if not exist "%USERPROFILE%\.nougen\logs" mkdir "%USERPROFILE%\.nougen\logs" 2>nul
echo [fleet] upstream %OLLAMA_HOST%  models %OLLAMA_MODELS%  bin %OLLAMA_BIN%  vulkan %OLLAMA_VULKAN%
echo [fleet] %DATE% %TIME% starting upstream >>"%OLLAMA_LOG%"
"%OLLAMA_BIN%" serve >>"%OLLAMA_LOG%" 2>&1
