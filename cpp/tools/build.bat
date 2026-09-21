@echo off
rem Build the C++ port.
rem   build.bat [ON|OFF]      app target (default ON; OFF builds core + tests only)
rem   build.bat ON test       build, then run the smoke test
setlocal
set APP=%1
if "%APP%"=="" set APP=ON
set RUN=%2

set ROOT=%~dp0..
call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (
  echo [build] vcvars64.bat failed - is the C++ workload installed?
  exit /b 1
)

cmake -S "%ROOT%" -B "%ROOT%\build" -G Ninja -DSP_BUILD_APP=%APP% -DCMAKE_BUILD_TYPE=RelWithDebInfo
set CFG_RC=%errorlevel%
if not "%CFG_RC%"=="0" (
  echo [build] configure failed with exit code %CFG_RC%
  exit /b %CFG_RC%
)

cmake --build "%ROOT%\build"
set BUILD_RC=%errorlevel%
if not "%BUILD_RC%"=="0" (
  echo [build] compile failed with exit code %BUILD_RC%
  exit /b %BUILD_RC%
)
echo [build] ok

if /i "%RUN%"=="test" (
  echo [build] running the smoke test
  pushd "%ROOT%\build"
  sp_smoke.exe
  set RC=%errorlevel%
  popd
  exit /b %RC%
)
exit /b 0
