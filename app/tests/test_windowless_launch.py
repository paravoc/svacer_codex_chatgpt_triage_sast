"""Windows helpers stay off the taskbar while the Qt application keeps its identity."""
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import codex_run
import svacer_connection


def test_windowless_python_prefers_pythonw(monkeypatch, tmp_path):
    console = tmp_path / "python.exe"
    windowless = tmp_path / "pythonw.exe"
    console.touch()
    windowless.touch()
    monkeypatch.setattr(codex_run.os, "name", "nt")
    assert codex_run.windowless_python_executable(console) == str(windowless)
    assert codex_run.windowless_python_executable(windowless) == str(windowless)


def test_windowless_python_falls_back_when_pythonw_is_absent(monkeypatch, tmp_path):
    console = tmp_path / "python.exe"
    console.touch()
    monkeypatch.setattr(codex_run.os, "name", "nt")
    assert codex_run.windowless_python_executable(console) == str(console)


def test_windows_hidden_flags_do_not_request_a_console():
    options = codex_run.hidden_subprocess_kwargs(new_process_group=True)
    if os.name == "nt":
        assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert options["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        assert not options["creationflags"] & subprocess.CREATE_NEW_CONSOLE
        assert options["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert options["startupinfo"].wShowWindow == subprocess.SW_HIDE
    else:
        assert options == {}


def test_start_hands_off_to_windowless_bootstrap_without_a_menu():
    root = Path(__file__).resolve().parents[2]
    launcher = (root / "START.cmd").read_text(encoding="utf-8-sig")
    vbs = (root / "START.vbs").read_text(encoding="utf-8-sig")
    bootstrap = (root / "app" / "bootstrap.ps1").read_text(encoding="utf-8-sig")

    assert 'wscript.exe "%~dp0START.vbs"' in launcher
    assert "Read-Host" not in launcher and "service_menu" not in launcher
    assert "-WindowStyle Hidden" in vbs and "bootstrap.ps1" in vbs
    assert "setup_mcp.ps1" in bootstrap and "startup.log" in bootstrap
    assert "Test-Installation" in bootstrap and "triage_gui_qt.py" in bootstrap
    assert "Start-Process -FilePath $pythonw" in bootstrap
    gui = (root / "app" / "triage_gui_qt.py").read_text(encoding="utf-8-sig")
    assert "if args.new_project:" in gui
    assert "if args.new_project or not saved_jobs:" not in gui


def test_automatic_setup_logs_progress_and_shows_a_gui_error():
    root = Path(__file__).resolve().parents[2]
    bootstrap = (root / "app" / "bootstrap.ps1").read_text(encoding="utf-8-sig")

    assert "Write-StartupLog 'Installation is incomplete; starting automatic setup.'" in bootstrap
    assert "function Invoke-AutomaticSetup" in bootstrap
    assert "-RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath" in bootstrap
    assert "if ($process.ExitCode -ne 0)" in bootstrap
    assert "$process.WaitForExit($setupTimeoutMilliseconds)" in bootstrap
    assert "taskkill.exe /PID" in bootstrap
    assert "[string]::IsNullOrWhiteSpace($setupLine)" in bootstrap
    assert "[IO.FileShare]::ReadWrite" in bootstrap
    assert "System.Windows.Forms" in bootstrap and "MessageBox" in bootstrap
    assert "Read-Host" not in bootstrap
    assert "SVACER_LOGIN" not in bootstrap and "SVACER_PASSWORD" not in bootstrap


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1 process handle regression")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_automatic_setup_observes_real_child_exit_code(tmp_path, exit_code):
    bootstrap = Path(__file__).resolve().parents[1] / "bootstrap.ps1"
    (tmp_path / "setup.ps1").write_text(f"Write-Output 'fixture setup'\nexit {exit_code}\n", encoding="utf-8")
    harness = tmp_path / "harness.ps1"
    harness.write_text("""
param([string]$Bootstrap)
$ErrorActionPreference = 'Stop'
$logDirectory = $PSScriptRoot
$setup = Join-Path $PSScriptRoot 'setup.ps1'
$setupTimeoutMilliseconds = 10000
function Write-StartupLog { param([string]$Message) }
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Bootstrap, [ref]$null, [ref]$null)
$node = $ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Invoke-AutomaticSetup'}, $true)
Invoke-Expression $node.Extent.Text
try { Invoke-AutomaticSetup; Write-Output 'SETUP_SUCCESS' }
catch { Write-Output $_.Exception.Message; exit 1 }
""", encoding="utf-8")
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(harness), "-Bootstrap", str(bootstrap)],
        capture_output=True, text=True, timeout=25, **codex_run.hidden_subprocess_kwargs(),
    )
    if exit_code == 0:
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "SETUP_SUCCESS" in completed.stdout
    else:
        assert completed.returncode == 1, completed.stdout + completed.stderr
        assert "exited with code 7" in completed.stdout


def test_startup_splash_shows_animation_stages_and_success():
    root = Path(__file__).resolve().parents[2]
    app = root / "app"
    bootstrap = (app / "bootstrap.ps1").read_text(encoding="utf-8-sig")
    splash = (app / "startup_splash.ps1").read_text(encoding="utf-8-sig")
    package = (app / "make_portable_package.ps1").read_text(encoding="utf-8-sig")

    assert "startup_splash.ps1" in bootstrap
    assert "-WindowStyle Hidden" in bootstrap
    assert "'-STA'" in bootstrap
    assert "Set-StartupState 'checking'" in bootstrap
    assert "Set-StartupState 'installing'" in bootstrap
    assert "Set-StartupState 'starting'" in bootstrap
    assert "Set-StartupState 'success'" in bootstrap
    assert "Set-StartupState 'error'" in bootstrap
    assert "[IO.FileShare]::ReadWrite" in bootstrap
    assert "for ($attempt = 1; $attempt -le 5; $attempt++)" in bootstrap
    assert "--startup-ready-file" in bootstrap
    assert "Graphical application opened successfully." in bootstrap

    assert "System.Windows.Forms" in splash
    assert "Windows.Forms.PictureBox" in splash
    assert "Drawing.Image]::FromFile($GifPath)" in splash
    assert "Готово — приложение открыто" in splash
    assert "Устанавливаю и настраиваю" in splash
    assert "ShowInTaskbar = $false" in splash
    assert "SvacerSplashNativeMethods" in splash
    assert "ShowWindow($form.Handle, 5)" in splash
    assert "Get-Process -Id $ParentProcessId" in splash
    assert "'startup_splash.ps1'" in package


def test_gui_signals_bootstrap_only_after_the_window_is_shown():
    gui = (Path(__file__).resolve().parents[1] / "triage_gui_qt.py").read_text(encoding="utf-8-sig")

    show = gui.index("window.showMaximized()")
    ready = gui.index('ready_path.write_text("ready\\n", encoding="utf-8")')
    assert show < ready
    assert 'parser.add_argument("--startup-ready-file", help=argparse.SUPPRESS)' in gui
    assert "QTimer.singleShot(0, signal_startup_ready)" in gui


def test_release_notes_describe_the_automatic_first_run():
    root = Path(__file__).resolve().parents[2]
    release = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "open START.vbs" in release
    assert "opens the Svacer sign-in form automatically" in release
    assert "choose installation option 6" not in release


def test_setup_finds_bundled_codex_desktop_cli_outside_injected_path():
    app = Path(__file__).resolve().parents[1]
    setup = (app / "setup_mcp.ps1").read_text(encoding="utf-8-sig")

    assert "function Find-CodexExecutable" in setup
    assert "GetFolderPath('LocalApplicationData')" in setup
    assert "OpenAI\\Codex\\bin" in setup
    assert "Sort-Object LastWriteTimeUtc -Descending" in setup
    assert "$codexPath = Find-CodexExecutable" in setup
    assert setup.count("& $codexPath mcp") == 3
    assert "& codex mcp" not in setup
    assert "Запусти скрипт из терминала Codex" not in setup
    assert "& powershell.exe" not in setup
    assert "-EncodedCommand" in setup and "-WindowStyle Hidden" in setup


def test_service_menu_login_uses_themed_windowless_launcher():
    app = Path(__file__).resolve().parents[1]
    restart = (app / "restart_svacer_http.ps1").read_text(encoding="utf-8-sig")
    launcher = (app / "svacer_login_app.py").read_text(encoding="utf-8-sig")
    package = (app / "make_portable_package.ps1").read_text(encoding="utf-8-sig")

    assert '.venv\\Scripts\\pythonw.exe' in restart
    assert 'svacer_login_app.py' in restart
    assert 'Start-Process -FilePath $pythonPath' in restart
    assert '-WindowStyle Hidden' not in restart  # the interactive Qt dialog must stay visible
    assert 'Start-Process -FilePath "powershell.exe"' not in restart
    assert 'start_svacer_http.ps1' not in restart
    assert 'SvacerLoginDialog' in launcher
    assert 'svacer-settings.json' in launcher
    assert 'DEFAULT_MCP_URL' in launcher
    assert "'svacer_login_app.py'" in package


def test_taskbar_identity_and_icon_are_configured_and_packaged():
    app = Path(__file__).resolve().parents[1]
    gui = (app / "triage_gui_qt.py").read_text(encoding="utf-8-sig")
    package = (app / "make_portable_package.ps1").read_text(encoding="utf-8-sig")
    assert "SetCurrentProcessExplicitAppUserModelID" in gui
    assert 'setApplicationDisplayName("Svacer Triage")' in gui
    assert 'assets" / "svacer-triage-v2.ico' in gui
    icon = app / "assets" / "svacer-triage-v2.ico"
    assert icon.is_file()
    data = icon.read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00"
    assert int.from_bytes(data[4:6], "little") >= 8
    assert "'assets/svacer-triage-v2.ico'" in package
    loading = app / "assets" / "svacer-hamster-loading.gif"
    assert loading.is_file() and loading.read_bytes()[:6] in {b"GIF87a", b"GIF89a"}
    assert "'assets/svacer-hamster-loading.gif'" in package


def test_start_shortcut_uses_windowless_bootstrap_and_application_icon():
    app = Path(__file__).resolve().parents[1]
    script = (app / "create_shortcut.ps1").read_text(encoding="utf-8-sig")
    setup = (app / "setup_mcp.ps1").read_text(encoding="utf-8-sig")
    package = (app / "make_portable_package.ps1").read_text(encoding="utf-8-sig")
    assert "System32\\wscript.exe" in script
    assert "START.vbs" in script
    assert "assets\\svacer-triage-v2.ico" in script
    assert "START.lnk" in script and "Svacer Triage.lnk" in script
    assert "create_shortcut.ps1" in setup and "'create_shortcut.ps1'" in package
    assert "'START.vbs'" in package and "'bootstrap.ps1'" in package
    assert "'startup_splash.ps1'" in package


def test_portable_install_uses_locked_poetry_dependencies():
    root = Path(__file__).resolve().parents[2]
    setup = (root / "app" / "setup_mcp.ps1").read_text(encoding="utf-8-sig")
    package = (root / "app" / "make_portable_package.ps1").read_text(encoding="utf-8-sig")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    lock = (root / "poetry.lock").read_text(encoding="utf-8")

    assert 'poetryVersion = "2.2.1"' in setup
    assert "$poetryExecutable sync" in setup and "--with desktop" in setup and "--without dev" in setup
    assert "https://chatgpt.com/codex/install.ps1" in setup
    assert "Programs\\OpenAI\\Codex\\bin" in setup
    assert "-r (Join-Path" not in setup
    assert "'pyproject.toml'" in package and "'poetry.lock'" in package
    assert 'mcp==1.30.0' in project and 'PySide6 = "6.11.2"' in project
    assert 'lock-version = "2.1"' in lock
    assert "svacer-lock.sha256" in setup
    assert "svacer-lock.sha256" in (root / "app" / "bootstrap.ps1").read_text(encoding="utf-8-sig")
    assert "sys.version_info < (3, 15)" in setup


def test_local_mcp_token_prefers_inherited_environment(monkeypatch):
    monkeypatch.setenv("SVACER_LOCAL_MCP_TOKEN", "synthetic-inherited-token")
    assert svacer_connection.read_local_mcp_token() == "synthetic-inherited-token"


def test_direct_windows_launch_reads_user_environment_without_logging(monkeypatch):
    calls = []
    class Key:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        OpenKey=lambda root, path: (calls.append((root, path)) or Key()),
        QueryValueEx=lambda key, name: ("synthetic-user-token", 1),
    )
    monkeypatch.delenv("SVACER_LOCAL_MCP_TOKEN", raising=False)
    monkeypatch.setattr(svacer_connection.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert svacer_connection.read_local_mcp_token() == "synthetic-user-token"
    assert calls == [(fake.HKEY_CURRENT_USER, "Environment")]
