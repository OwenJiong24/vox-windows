#!/usr/bin/env python3
"""
Vox (Windows) Environment Diagnostic & Bootstrapper.
Performs full pre-flight health checks (Python, dependencies, clipboard backend, audio
devices, config), prevents instant window crashes (闪退), and provides clean daemon
lifecycle management.
"""

import os
import sys

# Before numpy/torch: the BLAS/OpenMP thread cap only applies if it is set before the
# first heavy import. Being the launcher, this also means every child process the
# daemon inherits from here is tuned from birth.
import vox_tuning  # noqa: F401  (import for its side effect; order is load-bearing)

import time
import json
import subprocess
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import vox_core as core
from vox_core import log

DAEMON_SCRIPT = os.path.join(SCRIPT_DIR, "vox_daemon.py")
GUI_SCRIPT = os.path.join(SCRIPT_DIR, "vox_gui.py")
OVERLAY_STATE_FILE = os.path.join(SCRIPT_DIR, "overlay_state.json")


def get_daemon_pid():
    """Returns alive PID of vox_daemon if running, else None."""
    if os.path.exists(core.PID_FILE):
        try:
            with open(core.PID_FILE, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            # BUG-2 fix: OpenProcess alone reports killed processes as alive;
            # always use the shared GetExitCodeProcess-based primitive.
            if core._process_alive(pid):
                return pid
        except Exception:
            pass
    # Pid file missing or stale: fall back to a live command-line scan so an
    # orphaned daemon (e.g. after a crash that left a dead pid behind) is
    # detected as RUNNING. Without this, we would spawn a duplicate that
    # immediately exits on the single-instance mutex and the launcher reports
    # a false "startup timeout" even though Vox is actually alive.
    try:
        others = core.find_existing_daemon_pids()
        if others:
            return others[0]
    except Exception:
        pass
    return None


def ping_overlay(msg="Vox 智能语音已就绪 · 按住 Shift 说话"):
    """Sends a visual greeting to the floating pill overlay."""
    try:
        from datetime import datetime
        data = {
            "state": "done",
            "text": msg,
            "hint": "按住 Shift 说话 · 双击复位",
            "preview": "",
            "level": 0.0,
            "start_time": 0.0,
            "updated_at": datetime.now().isoformat()
        }
        core.safe_write_json(OVERLAY_STATE_FILE, data)
    except Exception:
        pass


def stop_daemon(timeout=3.0):
    """Stops any running daemon cleanly."""
    pid = get_daemon_pid()
    if not pid:
        # Also clean up any orphan pythonw running vox_daemon
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                           "Get-CimInstance Win32_Process -Filter \"name = 'pythonw.exe' and commandline like '%vox_daemon.py%'\" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                           capture_output=True)
        except Exception:
            pass
        return True

    try:
        with open(os.path.join(SCRIPT_DIR, "vox.stop"), "w", encoding="utf-8") as f:
            f.write("stop")
    except Exception:
        pass

    t0 = time.time()
    while time.time() - t0 < timeout:
        if not get_daemon_pid():
            if os.path.exists(core.PID_FILE):
                try: os.unlink(core.PID_FILE)
                except Exception: pass
            return True
        time.sleep(0.1)

    # Force kill if still running
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    except Exception:
        pass
    if os.path.exists(core.PID_FILE):
        try: os.unlink(core.PID_FILE)
        except Exception: pass
    return True


# Modules the daemon genuinely cannot run without.
#
# Deliberately NOT here any more:
#   * funasr / torch — local ASR (SenseVoice) is optional now; requiring the
#     model stack made a perfectly good API-only install report as "broken".
#   * pywin32 (win32clipboard) — the clipboard/paste path moved to ctypes
#     (vox_win32.py), which ships with Python. Requiring pywin32 is what made
#     Vox silently run on an interpreter where nothing could be pasted.
_DAEMON_REQUIRED = ("numpy", "sounddevice", "keyboard")


def _probe_interpreter(py_exe, timeout=40):
    """Check a candidate interpreter can really run the daemon.

    Returns (ok, missing, error). This imports the real dependency set in the
    target interpreter and verifies the Win32 clipboard backend binds, instead of
    the old `import ctypes` smoke test that passed on a useless interpreter.
    """
    if not os.path.exists(py_exe):
        return False, [], "解释器不存在"

    code = (
        "import importlib, json\n"
        "missing = []\n"
        "for m in %r:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception:\n"
        "        missing.append(m)\n"
        "try:\n"
        "    import vox_win32\n"
        "    win = bool(vox_win32.available())\n"
        "except Exception:\n"
        "    win = False\n"
        "print(json.dumps({'missing': missing, 'win32': win}))\n"
    ) % (list(_DAEMON_REQUIRED),)

    try:
        res = subprocess.run(
            [py_exe, "-c", code],
            capture_output=True, text=True, cwd=SCRIPT_DIR, timeout=timeout,
        )
    except Exception as exc:
        return False, [], str(exc)

    if res.returncode != 0:
        lines = [ln for ln in (res.stderr or "").strip().splitlines() if ln.strip()]
        return False, [], lines[-1] if lines else "解释器启动失败"

    try:
        data = json.loads((res.stdout or "").strip().splitlines()[-1])
    except Exception:
        return False, [], "探测输出无法解析"

    missing = list(data.get("missing") or [])
    if not data.get("win32"):
        missing.append("win32-clipboard")
    return (not missing), missing, ""


def _pythonw_for(py_exe):
    """Sibling pythonw.exe of a given python.exe."""
    folder, base = os.path.split(py_exe)
    if base.lower() == "python.exe":
        return os.path.join(folder, "pythonw.exe")
    return py_exe


def _candidate_interpreters():
    """Daemon interpreters in preference order: venv, then us, then system.

    The project venv is FIRST on purpose. A previous order put a hard-coded
    system Python path on top and only tested `import ctypes`, so Vox launched
    its daemon on an interpreter missing its dependencies — silently killing
    every clipboard write and Ctrl+V injection.
    """
    venv_py = os.path.join(SCRIPT_DIR, ".venv", "Scripts", "python.exe")
    candidates = [venv_py, sys.executable]
    seen, ordered = set(), []
    for cand in candidates:
        if not cand:
            continue
        full = os.path.abspath(cand)
        if full.lower() in seen or not os.path.exists(full):
            continue
        seen.add(full.lower())
        ordered.append(full)
    return ordered


def get_working_pythonw():
    """Pick a pythonw.exe whose interpreter can actually run the daemon."""
    for py_exe in _candidate_interpreters():
        ok, missing, err = _probe_interpreter(py_exe)
        if ok:
            log(f"守护进程解释器: {_pythonw_for(py_exe)}", "INFO")
            return _pythonw_for(py_exe)
        detail = ", ".join(missing) if missing else err
        print(f"[Vox] ⚠️ 跳过 {py_exe}（缺少: {detail}）")
        log(f"解释器探测失败 {py_exe}: {detail}", "WARN")
    # Nothing passed — fall back to our own interpreter so the user at least gets
    # a loud diagnostic instead of a daemon that silently cannot paste.
    print("[Vox] ⚠️ 未找到依赖完整的解释器，改用当前解释器启动（可能会出现粘贴失效）")
    return _pythonw_for(sys.executable)


def start_daemon_process():
    """Starts the daemon in the background with pythonw."""
    pythonw = get_working_pythonw()

    DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        # NEVER discard the daemon's output.
        #
        # This used to be stdout=DEVNULL, stderr=DEVNULL. pythonw has no console, so an
        # uncaught exception — including one raised inside the `keyboard` hook thread —
        # disappeared completely. That is why "the hotkey stopped responding" had no
        # evidence anywhere: not in vox.log (which only records what the code chooses to
        # log), not on any console, nowhere. An append-only file costs nothing and makes
        # the difference between guessing and knowing.
        stderr_path = os.path.join(SCRIPT_DIR, "vox_stderr.log")
        try:
            errf = open(stderr_path, "a", encoding="utf-8", errors="replace")
        except Exception:
            errf = subprocess.DEVNULL

        proc = subprocess.Popen(
            [pythonw, DAEMON_SCRIPT],
            cwd=SCRIPT_DIR,
            creationflags=DETACHED | CREATE_NO_WINDOW,
            stdout=errf,
            stderr=subprocess.STDOUT,
        )
        return proc.pid
    except Exception as e:
        print(f"❌ 启动守护进程失败: {e}")
        return None


def run_diagnostics():
    """Performs all pre-flight checks and returns (ok: bool, report: dict)."""
    report = {
        "python": None,
        "packages": None,
        "hardware": None,
        "audio": None,
        "config": None,
        "daemon": None,
        "errors": []
    }

    # 1. Python Check
    py_ver = sys.version.split()[0]
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    report["python"] = {
        "version": py_ver,
        "executable": sys.executable,
        "in_venv": in_venv,
        "ok": sys.version_info >= (3, 10)
    }
    if not report["python"]["ok"]:
        report["errors"].append(f"Python 版本过低 ({py_ver})，需 Python 3.10 以上")

    # 2. Package Dependencies Check
    #
    # Split into REQUIRED (cloud/API path — the supported configuration) and
    # OPTIONAL (local model stack). The old list marked faster_whisper and
    # ctranslate2 as critical while never checking the clipboard backend at all:
    # it blocked working API-only installs and blessed broken paste setups.
    pkg_status = {}
    critical_pkgs = [
        ("numpy", "音频矩阵运算库"),
        ("sounddevice", "PortAudio 录音驱动"),
        ("keyboard", "全局低级按键监听"),
        ("tkinter", "GUI 窗口交互框架"),
    ]
    optional_pkgs = [
        ("groq", "Groq 云端 ASR API（推荐）"),
        ("requests", "MiMo 云端 ASR API"),
        ("pystray", "Windows 任务栏托盘微标"),
        ("PIL", "Pillow 图像渲染库"),
        ("zhconv", "繁简转换"),
    ]
    # Availability probe via find_spec, NOT __import__.
    #
    # This is a pre-flight "is it installed" check, and importing to answer it was
    # expensive: measured +517 ms and +521 modules added to the LAUNCHER process —
    # the first thing the user waits on — because it pulled numpy, sounddevice,
    # groq, requests and zhconv into memory just to prove they exist.
    # find_spec resolves the module's location without executing a single line of it.
    #
    # Trade-off, stated plainly: find_spec cannot detect a package that is present but
    # broken at import time. That case is still caught loudly — vox_tuning/vox_core are
    # imported below, and the daemon logs a real traceback and records it in
    # vox_health.json — so the failure is visible, just later. For the two modules whose
    # absence would break the FIRST interaction (audio capture and the global hotkey),
    # a real import is still performed.
    def _available(mod):
        try:
            import importlib.util
            return importlib.util.find_spec(mod) is not None
        except Exception:
            return False

    import_checked = {"numpy", "sounddevice", "keyboard"}
    for mod, desc in critical_pkgs:
        if mod in import_checked:
            try:
                __import__(mod)
                pkg_status[mod] = True
            except Exception as err:
                pkg_status[mod] = False
                report["errors"].append(f"缺少核心运行库: {desc} ({mod}): {err}")
        else:
            ok = _available(mod)
            pkg_status[mod] = ok
            if not ok:
                report["errors"].append(f"缺少核心运行库: {desc} ({mod})")

    missing_optional = []
    for mod, desc in optional_pkgs:
        ok = _available(mod)
        pkg_status[mod] = ok
        if not ok:
            missing_optional.append(f"{mod} ({desc})")
    report["optional_missing"] = missing_optional

    # Clipboard / paste backend — the thing that actually broke. ctypes-based, so
    # it should always be healthy on Windows; if it is not, say so loudly instead
    # of letting every transcription fail to paste in silence.
    try:
        import vox_win32
        win_status = vox_win32.status()
    except Exception as err:
        win_status = {"available": False, "backend": "error", "last_error": str(err)}
    report["clipboard"] = win_status
    if not win_status.get("available"):
        report["errors"].append(
            f"剪贴板/粘贴后端不可用: {win_status.get('last_error') or win_status.get('backend')}"
        )
    report["packages"] = pkg_status

    # 3. Configuration File — loaded BEFORE the hardware probe, which is now
    #    conditional on it.
    try:
        cfg = core.ConfigManager.load()
        hk = cfg.get("hotkey", {})
        report["config"] = {
            "ok": True,
            "hotkey": hk.get("combo", "shift").upper(),
            "mode": hk.get("mode", "adaptive"),
            "engines": cfg.get("engine_priority", ["groq", "local"]),
            "fidelity": cfg.get("fidelity", {}).get("mode", "verbatim")
        }
    except Exception as e:
        cfg = {}
        report["config"] = {"ok": False, "error": str(e)}
        report["errors"].append(f"config.json 配置文件格式解析错误: {e}")

    # 4. Hardware & GPU Acceleration — DEFERRED unless a GPU engine is really
    #    configured. ctranslate2's CUDA probe has to bring up the driver and then
    #    talks to the card; measured 0.02s when idle but 40s+ when it is
    #    saturated (one real launch took 47.6s for exactly this call). With a
    #    cloud-only priority nothing ever lands on the GPU, so the answer cannot
    #    change any decision — pay for it only once a local engine points there.
    gpu_engines = core.gpu_backed_engines(cfg) if report["config"].get("ok") else []
    if gpu_engines:
        is_cuda, hw_info = core.get_cuda_device_info()
    else:
        is_cuda, hw_info = None, "跳过检测（未配置 GPU 引擎，启用时自动检测）"
    report["hardware"] = {
        "is_cuda": is_cuda,
        "info": hw_info,
        "checked": bool(gpu_engines),
        "gpu_engines": gpu_engines
    }

    # 5. Audio Input Device Check
    try:
        pipeline = core.AudioPipeline()
        dev_idx, dev_name = pipeline.resolve_device(cfg.get("audio", {}).get("preferred_device"))
        devs = pipeline.list_input_devices(force_refresh=True)
        report["audio"] = {
            "resolved_idx": dev_idx,
            "resolved_name": dev_name,
            "device_count": len(devs),
            "ok": dev_idx is not None and len(devs) > 0
        }
        if not report["audio"]["ok"]:
            report["errors"].append("未检测到任何可用的麦克风录音设备，请检查耳机或麦克风插孔")
    except Exception as e:
        report["audio"] = {"ok": False, "error": str(e)}
        report["errors"].append(f"音频采集系统探测异常: {e}")

    # 6. Daemon Process Status
    pid = get_daemon_pid()
    report["daemon"] = {
        "running": pid is not None,
        "pid": pid
    }

    overall_ok = len(report["errors"]) == 0
    return overall_ok, report


def print_banner(report):
    print("\n" + "=" * 62)
    print("      🎙️  Vox (Windows) 智能语音转写 · 启动自检报告")
    print("=" * 62)

    # 1. Python
    py = report["python"]
    venv_str = " (.venv 虚拟环境)" if py["in_venv"] else " (全局环境)"
    print(f" [1/5] Python 运行环境 :  ✅ Python {py['version']}{venv_str}")

    # 2. Packages
    pkgs = report["packages"]
    core_missing = [m for m in ("numpy", "sounddevice", "keyboard", "tkinter") if not pkgs.get(m)]
    if not core_missing:
        print(" [2/5] 核心运行依赖库 :  ✅ 全部就绪 (NumPy, SoundDevice, Keyboard, Tk)")
    else:
        print(f" [2/5] 核心运行依赖库 :  ❌ 缺失依赖: {', '.join(core_missing)}")

    # 2b. Clipboard / paste backend — the failure that used to be completely silent.
    clip = report.get("clipboard", {})
    if clip.get("available"):
        print(f"       └ 剪贴板粘贴链路 :  ✅ {clip.get('backend')} 后端正常 (无需 pywin32)")
    else:
        print(f"       └ 剪贴板粘贴链路 :  ❌ 不可用 · {clip.get('last_error') or '未知原因'}")
        print("          （转写仍可用，但无法自动粘贴；文本会暂存到 _last_transcript.txt）")

    opt_missing = report.get("optional_missing") or []
    if opt_missing:
        names = ", ".join(m.split(" (")[0] for m in opt_missing)
        print(f"       └ 可选组件未装   :  {names}")

    # 3. Hardware
    hw = report["hardware"]
    if not hw.get("checked", True):
        print(f" [3/5] 硬件加速识别   :  ⏭️  {hw['info']}")
    else:
        badge = "🚀" if hw["is_cuda"] else "ℹ️"
        who = "、".join(f"{n}({'/'.join(d)})" for n, d in hw.get("gpu_engines", []))
        print(f" [3/5] 硬件加速识别   :  {badge} {hw['info']}" + (f"  · 待加载: {who}" if who else ""))

    # 4. Audio
    aud = report["audio"]
    if aud.get("ok"):
        print(f" [4/5] 麦克风音频输入 :  ✅ [{aud['resolved_idx']}] {aud['resolved_name']}")
    else:
        print(f" [4/5] 麦克风音频输入 :  ❌ 未找到有效收音设备")

    # 5. Hotkey & Config
    # Named `cfg_report`, not `cfg`: this is the self-check report's config SECTION,
    # not the Vox config itself. Calling it `cfg` made a naive audit read it as
    # config-key access, and made the two dicts easy to confuse while reading.
    cfg_report = report["config"]
    if cfg_report.get("ok"):
        fid_text = "原生口语直录" if cfg_report["fidelity"] == "verbatim" else "书面语自动润色"
        print(f" [5/5] 触发热键与风格 :  ✅ [{cfg_report['hotkey']}] ({cfg_report['mode']} 模式 · {fid_text})")
    else:
        print(f" [5/5] 触发热键与风格 :  ❌ 配置解析异常: {cfg_report.get('error')}")

    print("-" * 62)


def open_manager_gui():
    """Opens the manager window — or wakes the one that is already open.

    The launcher reaches this on every start, so without a check a second click
    on the shortcut stacks another manager window (each ~180MB, each with its own
    config poller). Detection uses the manager's own single-instance named mutex
    (kernel-atomic, and the GUI holds it for its whole lifetime) instead of a
    WMI command-line scan, which is unreliable on this box: the child
    powershell.exe runs under ConstrainedLanguage, so Get-CimInstance yields
    objects whose properties read back as null and every scan returns nothing.

    Returns True when an existing window was woken, False when a new one was started.
    """
    try:
        if core.gui_is_running():
            core.gui_request_restore()
            print(" ℹ️ 管理控制中心已在运行，已将现有窗口调到前台")
            return True
    except Exception:
        pass
    pythonw = get_working_pythonw()
    subprocess.Popen([pythonw, GUI_SCRIPT])
    return False


def interactive_menu(daemon_pid):
    """Interactive control when daemon is already running."""
    print(f" 📌 当前状态: 🟢 Vox 守护进程已在后台平稳运行中 (PID: {daemon_pid})")
    print("-" * 62)
    print(" 💡 请选择操作 (输入数字并按回车):")
    print("   [1] 立即在屏幕下方点亮测试悬浮窗 (Visual Ping)")
    print("   [2] 打开 Vox 可视化设置控制面板 (Manager GUI)")
    print("   [3] 重启后台守护进程 (Restart Daemon)")
    print("   [4] 停止后台守护进程 (Stop Daemon)")
    print("   [Enter / 直接回车] 打开设置控制面板")
    print("=" * 62)

    # Ping overlay automatically on launch so user physically SEES it works
    ping_overlay("Vox 正在后台运行 · 按住 Shift 说话")

    try:
        choice = input("\n请输入选项 [1-4 或 Enter 打开设置]: ").strip()
    except EOFError:
        # Non-interactive launch (no stdin). This used to exit silently, which locked
        # the user out completely: run.bat refuses to start a second daemon, and when
        # the tray icon is unavailable (Windows 11 can bury it in the '^' overflow and
        # the registry write that would promote it is denied) there was then NO way to
        # reach the manager at all. Opening it is the useful default in both paths.
        choice = ""

    if choice == "":
        print("\n⚙️ 正在打开设置控制面板... (如需其它操作，重新运行 run.bat 后输入数字)")
        open_manager_gui()
        time.sleep(1.0)
    elif choice == "1":
        print("\n✨ 正在唤醒悬浮药丸窗...")
        ping_overlay("Vox 正在后台运行 · 按住 Shift 说话")
        print("✅ 已在您的屏幕正中下方点亮灵动岛药丸窗！")
        time.sleep(1.5)
    elif choice == "2":
        print("\n⚙️ 正在启动设置控制中心...")
        open_manager_gui()
        time.sleep(1.0)
    elif choice == "3":
        print("\n🔄 正在重启守护进程...")
        stop_daemon()
        time.sleep(0.5)
        new_pid = start_daemon_process()
        time.sleep(1.5)
        cur_pid = get_daemon_pid()
        if cur_pid:
            print(f"✅ 重启成功！新进程 PID: {cur_pid}")
            ping_overlay("Vox 已成功重启就绪 · 按住 Shift 说话")
        else:
            print("❌ 重启守护进程可能遇到了异常，请检查 vox.log")
        time.sleep(2.0)
    elif choice == "4":
        print("\n🛑 正在停止守护进程...")
        stop_daemon()
        print("✅ 守护进程已完全停止。")
        time.sleep(1.5)
    else:
        print("\n👋 保持 Vox 在后台持续运行中。随时按住 Shift 即可语音输入！")
        time.sleep(1.0)


def start_fresh_daemon():
    """Starts the daemon when it is not currently running, then opens the GUI."""
    print(" 🚀 正在启动 Vox 后台守护进程...")
    start_daemon_process()

    # Wait up to 60 seconds and verify it is alive. A cold start imports
    # funasr/torch (7-15s+ on a busy machine) BEFORE the
    # daemon acquires its lock and writes the pid file, so a 3s window produced
    # false "启动超时" failures while the daemon was actually coming up.
    ok = False
    cur_pid = None
    deadline = time.time() + 60.0
    last_dot = 0.0
    while time.time() < deadline:
        cur_pid = get_daemon_pid()
        if cur_pid:
            ok = True
            break
        if time.time() - last_dot >= 2.0:
            # Do not claim a model is loading. With engines.sensevoice.warmup off
            # (the default) nothing of the sort happens, and naming a step that is
            # not running is exactly the kind of reassuring-but-false message this
            # project has been burned by.
            sys.stdout.write("\r ⏳ 守护进程启动中（注册热键与音频设备）... ")
            sys.stdout.flush()
            last_dot = time.time()
        time.sleep(0.2)

    if ok:
        print(f" ✅ 启动成功！后台守护进程 PID: {cur_pid}")
        ping_overlay("Vox 智能语音已就绪 · 按住 Shift 说话")

        # Promote tray icon to visible taskbar in Windows 11
        try:
            core.promote_tray_icon_in_windows()
        except Exception:
            pass

        # Auto-launch the GUI control center
        print(" ⚙️ 正在打开 Vox 管理控制中心...")
        try:
            open_manager_gui()
        except Exception as e:
            print(f" ⚠️ 打开管理界面失败: {e}")

        print("-" * 62)
        print(" 💡 快捷使用指引:")
        print("   • 长按口述 : 按住 [Shift] 说话，松手即自动打字出字")
        print("   • 免手持录音 : 单击按一下 [Shift] 开始，说完再按一下或按回车结束")
        print("   • 中途撤销 : 录音过程中按 [Esc] 随时撤销")
        print("   • 托盘图标 : 任务栏右下角已常驻绿色微标")
        print("   • 设置中心 : 可随时双击托盘微标或运行 manager.bat 打开管理界面")
        print("=" * 62)
        print("\n提示: 3 秒后将自动关闭此控制台（Vox 在后台持续为您服务）...")
        try:
            for i in range(3, 0, -1):
                sys.stdout.write(f"\r倒计时 {i} 秒关闭窗口 (按 Enter 立即关闭)... ")
                sys.stdout.flush()
                time.sleep(1)
            print("\n已完成启动！祝您使用愉快！")
        except KeyboardInterrupt:
            pass
    else:
        print("❌ 守护进程启动超时或异常退出！请查看 vox.log 错误日志。")
        print("=" * 62)
        try:
            input("\n按回车键退出控制台...")
        except EOFError:
            pass  # non-interactive launch (no stdin); do not traceback


def main():
    action = "default"
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in ("--check", "-c"):
            action = "check"
        elif arg in ("--restart", "-r"):
            action = "restart"
        elif arg in ("--stop", "-s"):
            action = "stop"
        elif arg in ("--gui", "-g"):
            action = "gui"
        elif arg in ("--ping", "-p"):
            action = "ping"

    overall_ok, report = run_diagnostics()
    print_banner(report)

    if not overall_ok:
        print("❌ 检测到以下环境或配置异常:")
        for err in report["errors"]:
            print(f"   • {err}")
        print("=" * 62)
        try:
            input("\n⚠️ 存在上述错误导致无法正常运行，按回车键退出...")
        except EOFError:
            pass
        sys.exit(1)

    if action == "check":
        sys.exit(0)
    elif action == "gui":
        open_manager_gui()
        sys.exit(0)
    elif action == "stop":
        stop_daemon()
        print("✅ 守护进程已停止。")
        sys.exit(0)
    elif action == "restart":
        stop_daemon()
        time.sleep(0.5)
        start_fresh_daemon()
        sys.exit(0)
    elif action == "ping":
        ping_overlay()
        print("✅ 已发送悬浮窗唤醒信号。")
        sys.exit(0)

    # Default action: check running status
    daemon_pid = report["daemon"]["pid"]
    if daemon_pid:
        interactive_menu(daemon_pid)
    else:
        start_fresh_daemon()


if __name__ == "__main__":
    main()
