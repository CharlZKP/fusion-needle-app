# Building and smoke-testing Fusion Needle on Windows

> **Nothing in this file has been run on Windows.** `Start Fusion Needle.bat`, `build_windows.ps1`, the
> Windows half of `app/supervisor.py` (Job Objects, `taskkill`, process start times through `ctypes`) and the
> packaged `.exe` were written and reviewed on Linux. The Linux build of the same spec works. Treat the first
> Windows run as the test, do the steps in order, and send back what "If a step fails" asks for.

Needed: Windows 10 or 11 (64-bit), a **64-bit Python 3.12 or newer** from python.org with the `py` launcher,
internet access for `pip` (PyInstaller) and for the first start of the model. Fusion is only needed from step 5.

All commands are for **Windows PowerShell** opened in the repository folder (the folder with `pyproject.toml`).
The folder may have spaces in its name; the quoting below is written for that.

## 1. Set the project up (once)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -c "import stepserver, needle, sys; print(stepserver.NEEDLE_VERSION, sys.version)"
```

The last line must print `3.1.1` and a version that says `64 bit`.

## 2. Run the app from source first

This separates "the app does not work on Windows" from "the packaged build does not work".

```powershell
.venv\Scripts\python -m pip install pytest
.venv\Scripts\python -m pytest tests -q
```

`pytest` is a development dependency of the project (`pip install -e ".[dev]"` installs the same).
Expected: everything passes, 2 skipped (the opt-in end-to-end test and one Linux-only test).
`test_supervisor.py` is the part to watch: it starts the app, kills it the way Task Manager does and checks
that the model server and its worker are gone. **On Windows this is its first run.**

Then double-click `Start Fusion Needle.bat` (or run it: `& ".\Start Fusion Needle.bat"`).
A console window stays open, a browser tab (or the app's own window, with `pywebview`) shows the page, the
"Model" pill turns green after the model has loaded (the first start downloads the engine and `needle3.cact`
into `%USERPROFILE%\.cache\cactus-needle`; minutes on a slow line).

Check that the server does not outlive the app:

```powershell
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Select-Object ProcessId, ParentProcessId, CommandLine | Format-List
```

shows, while the app runs, one `fusion_needle.py`, one `supervisor.py ... -- ... -m stepserver.cli serve` and the
server and engine processes under it. End the `fusion_needle.py` process in **Task Manager > Details > End
task**, run the command again within ten seconds: none of them may be left.

## 3. Build

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\packaging\build_windows.ps1" -Console
```

- `-Console` keeps a console window on the executable. Use it for the first build: without it a start-up
  error has nowhere to show. Build without it once the smoke test passes.
- `-WithEngine` copies the engine DLL from `%USERPROFILE%\.cache\cactus-needle` into the build, so a machine
  without internet needs only the `.cact` file. It needs step 2 to have been run once (that is what downloads
  the DLL).
- If your organisation enforces an execution policy by group policy, `-ExecutionPolicy Bypass` is ignored and
  PowerShell says the script "is not digitally signed" or "cannot be loaded". Then run it as text instead:
  `powershell -NoProfile -Command "& ([scriptblock]::Create((Get-Content -Raw '.\packaging\build_windows.ps1'))) -Console"`
- The script creates `build\pyi-venv` (PyInstaller only) and never installs into `.venv`.
- Antivirus software sometimes quarantines a freshly built PyInstaller executable. If `FusionNeedle.exe`
  disappears after the build, that is what happened; say so when you report.

Result: `dist\FusionNeedle\FusionNeedle.exe` with an `_internal` folder next to it. The whole
`FusionNeedle` folder is the app; it can be copied anywhere, also to a path with spaces.

## 4. Smoke-test the build (no Fusion needed)

```powershell
$exe = ".\dist\FusionNeedle\FusionNeedle.exe"
& $exe --version                                  # Fusion Needle 0.1.0
& $exe --window none --weights base --port 8799   # leave it running
```

In a second PowerShell:

```powershell
Invoke-WebRequest http://127.0.0.1:8799/ -UseBasicParsing | Select-Object StatusCode    # 200
Get-CimInstance Win32_Process -Filter "Name = 'FusionNeedle.exe'" | Select-Object ProcessId, ParentProcessId, CommandLine | Format-List
```

Expected: four or more `FusionNeedle.exe` processes: the app, `-m app.supervisor ...`, `-m stepserver.cli serve ...`,
`-m stepserver.engine ...`. Open `http://127.0.0.1:8799/` in a browser: the "Model" pill turns green and names
`needle3.cact`.

Then the two endings:

1. Press `Ctrl+C` in the first window. All `FusionNeedle.exe` processes are gone within about ten seconds.
2. Start it again, wait for the green pill, and end the **first** `FusionNeedle.exe` (the one whose command
   line has `--window none`) with Task Manager > Details > End task. Again none may be left, and
   `%APPDATA%\FusionNeedle\run\` must be empty.

Last, copy `Start Fusion Needle.bat` next to the `FusionNeedle` folder (so that
`FusionNeedle\FusionNeedle.exe` is beside it), put both in a folder whose name has a space, and double-click
the `.bat`: it must start the packaged app.

## 5. First session with Fusion

1. Start Fusion, open a design (an empty new one is fine), enable its MCP server.
2. Start the app, wait for both pills to be green.
3. **Diagnostics > Check Fusion.** It only reads: handshake, tool list, the state script, the dialog check, a
   small screenshot and the up axis. Every line should say `pass`.
   - `Up axis: FAIL` means the document is Y up. The backend assumes Z up; build nothing until
     Preferences > General > Default modeling orientation is "Z up" and you are in a new document.
   - `Dialog check: FAIL` means Fusion answers the "is a command dialog open" question in a form the app does
     not know. Every modelling step will stop and show that answer. If you can see no dialog is open, "Proceed
     anyway (this step)" sends the step; the answer is in the export either way.
4. Run one small goal, for example `plate 40x30x10`.
5. **Diagnostics > Export diagnostics** and send the zip back, whatever happened. It holds the raw requests
   and answers (scripts included, images replaced by their size), the check result, versions and settings. The
   server token is not in it; your feature texts and file names are.

## If a step fails: what to send back

| Step | Send |
| --- | --- |
| 1 | the full PowerShell output; `py -0p`; `python --version`; `where.exe python` |
| 2, tests | the full output of `.venv\Scripts\python -m pytest tests -q -rA 2>&1 \| Tee-Object tests.txt` (the file `tests.txt`) |
| 2, `.bat` | a photo or copy of the console window; the folder's full path; `cmd /c ver` |
| 2 / 4, processes left over | the `Get-CimInstance ...` output from before and after; `%APPDATA%\FusionNeedle\run\*.json`; `%APPDATA%\FusionNeedle\model-server.log` (the supervisor writes why it stopped, or why a Job Object could not be used, into it) |
| 3 | everything the build printed: `powershell ... -File ".\packaging\build_windows.ps1" -Console *> build.txt` and the file `build.txt`; also `build\work\fusion_needle\warn-fusion_needle.txt` |
| 4, the exe does not start | the console output of the `-Console` build; `%APPDATA%\FusionNeedle\model-server.log`; `Get-ChildItem .\dist\FusionNeedle\_internal\needle` |
| 4, model pill stays red | click the pill and copy the "Server log" box; `Get-ChildItem $env:USERPROFILE\.cache\cactus-needle -Recurse \| Select-Object FullName, Length` |
| 5 | the diagnostics zip (`%APPDATA%\FusionNeedle\diagnostics\fusion-needle-diagnostics-*.zip`) and the Fusion version (Help > About) |

Always add: the Windows version (`winver`), the Python version, and whether the repository folder is on a
local disk, a OneDrive-synced folder or a network share.

## What was reviewed in the launcher and the build script

Review only, not a run. Each point names the failure it is meant to prevent.

`Start Fusion Needle.bat`

- CRLF line endings and plain ASCII (pinned in `.gitattributes`): `cmd` misreads blocks and labels
  in an LF file, and a UTF-8 file with a byte-order mark breaks the first line.
- Every path comes from `%~dp0` and is quoted, so the name with spaces and a folder with spaces or `&` work.
- The arguments (`%*`) are no longer expanded inside a `( ... )` block: an argument containing `)` ended the
  block early. The packaged-build branch now uses `goto`.
- Delayed expansion is off explicitly, so `!` in a path survives.
- The `py` launcher and `python` on PATH are accepted only when they are 64-bit and 3.12+. The Microsoft Store
  placeholder `python.exe` fails that test (it exits with an error when given arguments) and is skipped.
- Open points: from a network share (`\\server\share\...`) `cd /d` prints a warning; the app does not need the
  current folder, but this has not been tried. `py -3.14` relies on the installed launcher accepting the
  `-3.14` form; if it does not, that version is skipped and the next one is tried.

`build_windows.ps1`

- Runs with `-ExecutionPolicy Bypass -File`; the fallback for an enforced policy is in step 3.
- No native command has its error stream redirected: with `$ErrorActionPreference = "Stop"`, Windows
  PowerShell 5.1 turns redirected stderr text into a terminating error, which would have stopped the build at
  the first pip warning.
- PyInstaller's presence is tested on every run; before, a build environment whose `pip install` had failed
  was taken for complete on the next run.
- The Python is checked up front (64-bit, 3.12+, `stepserver` and `needle` importable) with a readable message.
- Paths go through `Join-Path` and `-LiteralPath`, so spaces and `[ ]` in the repository path are safe.
- The script is plain ASCII: Windows PowerShell 5.1 reads a script without a byte-order mark in the ANSI code
  page and would corrupt anything else.
- Open points: `PYTHONPATH` puts the repo's site-packages in front of the build environment's own (the Linux
  script does the same and works; on Windows it is untested). The name `libneedle3.dll` for `-WithEngine` is
  derived from needle's `fetch.lib_name`, not observed.
