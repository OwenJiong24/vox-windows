#!/usr/bin/env python3
"""Vox regression tests — guards for the fixes made in the 2026-10-13 audit session.

These cover failures that the existing test_suite.py could NOT catch, most notably
the paste bug: clipboard writes and Ctrl+V injection returned False silently, so the
tool looked alive (transcription worked) while nothing ever reached the cursor.

Run:
    .venv\\Scripts\\python.exe test_regression.py              # fast, non-intrusive
    .venv\\Scripts\\python.exe test_regression.py --inject     # + real Ctrl+V injection
    .venv\\Scripts\\python.exe test_regression.py --daemon     # + daemon smoke test

--inject sends one real Ctrl+V carrying a single space, so it can type a space into
whatever window has focus. --daemon starts the real daemon, which briefly takes over
the global hotkey. Both are therefore opt-in.
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_PASS, _FAIL = [], []


def check(name, cond, detail=""):
    ( _PASS if cond else _FAIL ).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return bool(cond)


def section(title):
    print(f"\n=== {title} ===")


# ─── 1. Clipboard backend ─────────────────────────────────────────
def test_backend():
    section("1. 剪贴板后端 (关键: 不再依赖 pywin32)")
    import vox_win32 as w
    import vox_core as core

    check("vox_win32 可用", w.available(), str(w.status()))
    check("SmartOutput 后端是 ctypes",
          core.SmartOutput.backend().get("backend") == "ctypes")


# ─── 2. Clipboard round-trip ──────────────────────────────────────
def test_roundtrip():
    section("2. 剪贴板读写往返")
    import vox_win32 as w
    import vox_core as core

    original = w.get_clipboard_text()
    samples = ["帮我列举一些牌子吧。", "Mixed 中英文 !@#$%^&*() 🎙️",
               "a\nb\tc", "x" * 4000]
    try:
        for s in samples:
            wrote = core.SmartOutput.set_clipboard_text(s)
            back = core.SmartOutput.get_clipboard_text()
            check(f"往返 len={len(s)}", wrote and back == s,
                  "" if back == s else f"got {str(back)[:50]!r}")
    finally:
        if original is not None:
            w.set_clipboard_text(original)


# ─── 3. Truthful reporting + never lose text ──────────────────────
def test_truthful_reporting():
    section("3. 失败必须可见, 文本绝不丢失")
    import vox_win32 as w
    import vox_core as core

    original = w.get_clipboard_text()
    try:
        res = core.SmartOutput.output("验证文本", method="clipboard_only")
        check("clipboard_only 成功时 copied=True", res["copied"] is True, str(res))
        check("hint 指向 Ctrl+V", "Ctrl+V" in res["hint"], res["hint"])
        check("读回一致", core.SmartOutput.get_clipboard_text() == "验证文本")

        # Simulate a dead clipboard and assert the failure is loud + lossless.
        real_set = w.set_clipboard_text
        dump = os.path.join(HERE, "_last_transcript.txt")
        try:
            w.set_clipboard_text = lambda *a, **k: False
            if os.path.exists(dump):
                os.unlink(dump)
            res = core.SmartOutput.output("这段文字绝不能丢", method="smart_paste",
                                          restore_clipboard=False)
            check("copied=False 被如实上报", res["copied"] is False, str(res))
            check("hint 有可读原因", bool(res.get("hint")), res["hint"])
            check("error 有兜底原因", bool(res.get("error")), res["error"])
            check("文本已兜底落盘",
                  os.path.exists(dump) and
                  open(dump, encoding="utf-8").read() == "这段文字绝不能丢")
        finally:
            w.set_clipboard_text = real_set
            if os.path.exists(dump):
                os.unlink(dump)
    finally:
        if original is not None:
            w.set_clipboard_text(original)


# ─── 4. Restore guard + token ─────────────────────────────────────
def test_restore_safety():
    section("4. 剪贴板还原的安全保护")
    import vox_win32 as w
    import vox_core as core

    real_fmts = core.SmartOutput.clipboard_formats
    try:
        core.SmartOutput.clipboard_formats = classmethod(lambda cls: {13, 15})  # + CF_HDROP
        check("含复制文件时拒绝还原", core.SmartOutput.clipboard_is_text_only() is False)
        core.SmartOutput.clipboard_formats = classmethod(lambda cls: {13})
        check("纯文本时可还原", core.SmartOutput.clipboard_is_text_only() is True)
    finally:
        core.SmartOutput.clipboard_formats = real_fmts

    # A stale delayed restore must not clobber a newer transcription.
    original = w.get_clipboard_text()
    try:
        core.SmartOutput._schedule_restore("STALE_VALUE", delay_s=0.30)
        time.sleep(0.05)
        core.SmartOutput._schedule_restore("NEWEST_VALUE", delay_s=0.30)
        time.sleep(0.80)
        check("旧还原被跳过, 新值保留",
              core.SmartOutput.get_clipboard_text() == "NEWEST_VALUE",
              repr(core.SmartOutput.get_clipboard_text()))
    finally:
        if original is not None:
            w.set_clipboard_text(original)


# ─── 5. Real SendInput injection (opt-in) ─────────────────────────
def test_injection():
    section("5. 真实 Ctrl+V 注入 (SendInput)")
    import ctypes
    import vox_win32 as w

    check("INPUT 结构体在 x64 下为 40 字节", ctypes.sizeof(w._INPUT) == 40,
          f"sizeof={ctypes.sizeof(w._INPUT)}")

    original = w.get_clipboard_text()
    try:
        w.set_clipboard_text(" ")   # payload = one harmless space
        time.sleep(0.05)
        ok = w.send_paste()
        check("send_paste() 报告成功", ok is True, w.last_error())
    finally:
        if original is not None:
            w.set_clipboard_text(original)


# ─── 6. Local engines stay on-demand (zero startup cost) ──────────
def test_ondemand():
    section("6. 本地引擎按需: 不加载就不花钱")
    import vox_core as core

    cfg = core.ConfigManager.load()
    prio = cfg.get("engine_priority", [])
    print(f"     engine_priority = {prio}")
    for k in ("sensevoice",):
        e = cfg.get("engines", {}).get(k, {})
        print(f"     engines.{k:<11} enabled={e.get('enabled')} device={e.get('device')}")

    g = core.gpu_backed_engines(cfg)
    cloud_only = not any(f in ("sensevoice",)
                         for f, _ in (core.split_slot(s) for s in prio))
    if cloud_only:
        check("云端优先级下 gpu_backed_engines 为空", not g, str(g))

    cfg_cpu = dict(cfg); cfg_cpu["engine_priority"] = list(prio) + ["sensevoice-cpu"]
    check("加 sensevoice-cpu 槽不触发 GPU 探测",
          not core.gpu_backed_engines(cfg_cpu))

    cfg_gpu = dict(cfg); cfg_gpu["engine_priority"] = ["sensevoice-cuda"]
    check("加 sensevoice-cuda 槽仍能识别 (本地未被抛弃)",
          bool(core.gpu_backed_engines(cfg_gpu)),
          str(core.gpu_backed_engines(cfg_gpu)))

    heavy = [m for m in ("torch", "funasr", "qwen_asr", "faster_whisper", "ctranslate2")
             if m in sys.modules]
    check("以上过程未导入任何重型模块", not heavy, str(heavy))


# ─── 7. Overlay render cost + wrap path ───────────────────────────
def test_overlay():
    section("7. overlay 渲染成本与换行路径")
    try:
        import tkinter as tk
        import vox_overlay as vo
    except Exception as e:
        check("overlay 可导入", False, str(e))
        return

    root = tk.Tk()
    root.withdraw()
    try:
        ov = vo.VoxOverlay(master=root, position="bottom_center")

        counts = {"n": 0}
        canvas = ov.canvas
        for name in ("create_polygon", "create_rectangle", "create_oval",
                     "create_text", "create_line", "create_arc"):
            orig = getattr(canvas, name)

            def wrap(orig=orig):
                def f(*a, **k):
                    counts["n"] += 1
                    return orig(*a, **k)
                return f

            setattr(canvas, name, wrap())

        cw = {"n": 0}
        real_cw = ov._char_widths

        def counted_cw(*a, **k):
            cw["n"] += 1
            return real_cw(*a, **k)

        ov._char_widths = counted_cw

        # A long preview forces the multi-line path.
        ov.show_recording(hint="regression", start_time=time.time() - 2.0)
        ov.update_preview("短句")
        short_lines = len(ov._preview_lines or [])
        ov.update_preview(
            "这一段内容更长一些这样必然会被折行从而触发向上生长的多行气泡布局逻辑"
            "并且继续延长以确保绝对超过单行容量进而稳定地产生多于一行的结果")
        long_lines = len(ov._preview_lines or [])

        # Warm up one frame first: the reusable text pool grows on demand the first
        # time a multi-line preview is rendered (_ensure_pool). That is a one-time
        # allocation, not per-frame churn, so steady state is measured after it.
        ov._draw_pill()
        counts["n"] = 0
        for i in range(40):
            ov.update_level(0.2 + 0.6 * ((i % 10) / 10.0))
            ov._draw_pill()
        steady = counts["n"]
        before = cw["n"]
        for _ in range(30):
            ov._draw_pill()
        static_measure = cw["n"] - before

        check("单行预览走内联路径 (0 行)", short_lines == 0, f"lines={short_lines}")
        check("长预览正确折行 (>=2 行)", long_lines >= 2, f"lines={long_lines}")
        check("稳态每帧创建 0 个画布项", steady == 0,
              f"{steady} items over 40 frames (steady state)")
        check("静态帧不重复测量字宽", static_measure == 0,
              f"{static_measure} measurement calls")
    finally:
        root.destroy()


# ─── 8. Daemon smoke test (opt-in) ────────────────────────────────
def test_process_liveness():
    section("8. 进程存活判定 (OpenProcess 不够, 必须看退出码)")
    import subprocess
    import vox_win32 as w
    import vox_core as core

    py = os.path.join(HERE, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable

    p = subprocess.Popen([py, "-c", "import time; time.sleep(60)"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        check("运行中的进程判定为存活", w.process_alive(p.pid) is True)
    finally:
        p.kill()
        p.wait()
    time.sleep(0.5)
    # The regression this guards: OpenProcess alone keeps succeeding on a killed
    # process because Windows retains the process object while a handle is open, and
    # subprocess holds one. That made killed processes look alive forever, which
    # silently disabled the overlay orphan watchdog.
    check("已 kill 的进程判定为不存在", w.process_alive(p.pid) is False)
    check("伪造 pid 判定为不存在", w.process_alive(999999) is False)
    check("自身 pid 判定为存活", w.process_alive(os.getpid()) is True)
    check("core._process_alive 已委托同一实现",
          core._process_alive(p.pid) is False)


def test_orphan_watchdog():
    section("9. 孤儿 overlay 自愈 (卡死气泡不再可能)")
    import subprocess

    py = os.path.join(HERE, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable
    overlay_script = os.path.join(HERE, "vox_overlay.py")

    parent = subprocess.Popen([py, "-c", "import time; time.sleep(120)"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    overlay = subprocess.Popen([py, overlay_script, "--parent-pid", str(parent.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(5)
        check("父进程存活时 overlay 正常运行", overlay.poll() is None)

        parent.kill()
        parent.wait()
        deadline = time.time() + 12
        while overlay.poll() is None and time.time() < deadline:
            time.sleep(0.4)
        check("父进程被杀后 overlay 自行退出 (不留卡死气泡)",
              overlay.poll() is not None)
    finally:
        for proc in (parent, overlay):
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass


def test_daemon():
    section("10. 守护进程冒烟测试 (会短暂占用全局热键)")
    import subprocess

    def _list_orphan_overlays():
        """PIDs of vox_overlay processes currently alive."""
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
                 "Where-Object { $_.CommandLine -match 'vox_overlay' } | "
                 "Select-Object -ExpandProperty ProcessId"],
                capture_output=True, text=True, timeout=20,
            ).stdout
        except Exception:
            return []
        return [t.strip() for t in (out or "").split() if t.strip().isdigit()]

    def _kill_orphan_overlays():
        """Never leave an overlay on the desktop, even when this test fails.

        The daemon normally reaps its overlay via kill_overlay(). But if the daemon
        itself has to be force-killed (as happens on a failed assertion above), the
        overlay is orphaned and its pill stays on screen. That is exactly how a stuck
        bubble reached the user's desktop once; the test must not repeat it.
        """
        for token in _list_orphan_overlays():
            try:
                subprocess.run(["taskkill", "/PID", token, "/F"],
                               capture_output=True, timeout=10)
            except Exception:
                pass

    py = os.path.join(HERE, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable
    log = os.path.join(HERE, "vox.log")
    # MUST match vox_daemon.STOP_FILE. (This test previously wrote vox_stop.flag,
    # which the daemon never watched, so a correct shutdown looked like a hang.)
    stop = os.path.join(HERE, "vox.stop")

    for f in (stop, os.path.join(HERE, "vox.pid")):
        if os.path.exists(f):
            os.unlink(f)
    log_start = os.path.getsize(log) if os.path.exists(log) else 0

    proc = subprocess.Popen([py, os.path.join(HERE, "vox_daemon.py")],
                            cwd=HERE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    time.sleep(12)
    alive = proc.poll() is None
    with open(stop, "w") as f:
        f.write("stop")
    deadline = time.time() + 12
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.4)
    exited = proc.poll() is not None
    if not exited:
        proc.kill()
    try:
        out = proc.communicate(timeout=8)[0] or ""
    except Exception:
        out = ""

    new_log = ""
    try:
        with open(log, "r", encoding="utf-8", errors="replace") as f:
            f.seek(log_start)
            new_log = f.read()
    except Exception:
        pass

    noise = ("pkg_resources", "UserWarning")
    lines = [ln for ln in (out + "\n" + new_log).splitlines()
             if ln.strip() and not any(n in ln for n in noise)]

    # The tray icon is knowingly unavailable in some environments: Windows denies
    # ChangeWindowMessageFilterEx (WinError 5) and the NotifyIconSettings registry
    # write. That is an environment/permission condition, NOT a daemon fault, so it
    # is reported but must not fail the smoke test. Everything else must.
    tray_lines = [ln for ln in lines if "托盘" in ln or "WinError 5" in ln
                  or "ChangeWindowMessageFilter" in ln]
    hard = [ln for ln in lines
            if "Traceback" in ln or "[ERROR]" in ln or "Exception" in ln]
    errors = [ln for ln in hard if ln not in tray_lines]

    check("12 秒后仍存活 (已进入主循环)", alive)
    check("收到停止信号后正常退出", exited)
    check("无致命异常/ERROR", not errors, "; ".join(errors[:3]))
    if tray_lines:
        print("     [注意] 托盘图标不可用(环境权限所致, 非致命):")
        for ln in tray_lines[:2]:
            print("       ", ln.strip())
    # The daemon must SAY the tray failed rather than dying silently on stderr.
    check("托盘失败已被显式记录(不再静默)",
          (not any("ChangeWindowMessageFilter" in ln for ln in lines))
          or any("托盘" in ln for ln in lines))

    print("     --- 本次守护进程日志尾部 ---")
    for ln in lines[-12:]:
        print("       ", ln)

    # Whatever happened above, never leave an overlay behind on the desktop.
    _kill_orphan_overlays()
    time.sleep(1.0)
    check("测试结束后无遗留 overlay (不会留下卡死气泡)",
          not _list_orphan_overlays())


def test_error_classification():
    section("11. B1 引擎错误分类归一化")
    import vox_core as c

    for msg, want in [
        ("groq: API key missing", "auth"),
        ("MiMo API 401: unauthorized", "auth"),
        ("MiMo API 429: quota exhausted", "quota"),
        ("Connection timed out", "timeout"),
        ("funasr not installed", "missing"),
        ("CUDA out of memory", "oom"),
        ("HTTPSConnectionPool: connection refused", "network"),
        ("音频文件过短或为空", "too_short"),
        ("groq: returned empty text", "empty"),
        ("某种全新的错误", "unknown"),
    ]:
        got = c.classify_engine_error(msg)
        check(f"{want:<9} <- {msg[:34]!r}", got == want, "" if got == want else f"got {got}")
    check("优先级: 429 高于 connection",
          c.classify_engine_error("HTTP 429 via connection reset") == "quota")


def test_perf_and_health():
    section("12. B1 记录失败 + 按槽位聚合 (降级不再静默)")
    import json
    import tempfile
    import vox_core as c

    real_perf = c.PERF_HISTORY_FILE
    tmp = os.path.join(tempfile.gettempdir(), "vox_perf_regr.json")
    if os.path.exists(tmp):
        os.unlink(tmp)
    c.PERF_HISTORY_FILE = tmp
    try:
        # A degraded-but-successful take: groq died on quota, mimo served.
        c.record_perf(
            engine="mimo", model="mimo-v2.5-asr", duration_s=3.0, latency_s=1.2,
            text="降级成功", chars=4, ok=True, used_engine="mimo",
            attempts=[{"slot": "groq-l3", "ok": False, "error": "429 quota exhausted",
                       "error_class": "quota", "ms": 812.0},
                      {"slot": "mimo", "ok": True, "error": "", "error_class": "",
                       "ms": 1340.0}],
            device="Test Mic", session_peak=0.21, reason="ok")
        # A silent take and a totally failed take (with a key leaked in the error).
        c.record_perf(engine="(skipped)", duration_s=1.4, latency_s=0.0, text="",
                      chars=0, ok=False, error_class="silent",
                      error="silent: peak=0.00031", device="Second Test Mic",
                      session_peak=0.00031, reason="silent")
        # Fake key strings — these are test fixtures to prove redaction works.
        fake_key = "gsk_" + "FAKE" * 6
        fake_bearer = "Bearer " + "FAKE" * 6
        c.record_perf(engine="(failed)", duration_s=2.0, latency_s=0.0, text="",
                      chars=0, ok=False, error_class="auth",
                      error=f"401 with {fake_key} and {fake_bearer}",
                      attempts=[{"slot": "groq-l3", "ok": False,
                                 "error": f"key {fake_key} rejected",
                                 "error_class": "auth", "ms": 300.0}],
                      device="Test Mic", session_peak=0.1, reason="chain_failed")

        recs = json.load(open(tmp, encoding="utf-8"))
        check("写入 3 条记录", len(recs) == 3, f"got {len(recs)}")
        check("失败记录被保留 (此前完全无痕)",
              any(r.get("ok") is False for r in recs))
        check("记录带麦克风名", recs[2].get("device") == "Test Mic")
        check("记录带 pre-AGC 峰值", recs[2].get("session_peak") == 0.21)
        blob = json.dumps(recs, ensure_ascii=False)
        check("密钥已脱敏 (gsk_ 未落盘)", "gsk_" not in blob and "<redacted>" in blob)
        check("Bearer 已脱敏", "Bearer FAKEFAKE" not in blob)

        health = c.engine_health(records=recs)
        check("失败引擎也出现在统计里 (B1 核心)",
              health.get("groq-l3", {}).get("fail", 0) >= 1, str(health.get("groq-l3")))
        check("mimo 成功率 100%", health.get("mimo", {}).get("ok_rate") == 1.0)
        check("mimo P50 有值", health.get("mimo", {}).get("p50_ms") == 1340.0)
        check("兼容旧版记录(无 attempts)",
              bool(c.engine_health(records=[{"engine": "groq", "latency_ms": 400.0,
                                             "ok": True}])))
        check("空输入不崩", c.engine_health(records=[]) == {})
    finally:
        c.PERF_HISTORY_FILE = real_perf
        if os.path.exists(tmp):
            os.unlink(tmp)


def test_peak_verdict():
    section("13. B2 输入电平归因 (AGC 之前的原始峰值)")
    import vox_core as c

    p = c.AudioPipeline()
    for peak, want in [(0.0, "silent"), (0.0003, "silent"), (0.004, "silent"),
                       (0.01, "quiet"), (0.029, "quiet"), (0.05, "ok"), (0.3, "ok")]:
        p.session_peak = peak
        got = p.peak_verdict()
        check(f"峰值 {peak:<7} -> {want}", got == want, "" if got == want else f"got {got}")
    check("未录音(峰值 0)判为 silent 而非 ok",
          c.AudioPipeline().peak_verdict() == "silent")
    check("阈值有明确分级 (silent<quiet<ok)",
          c.AudioPipeline.SILENT_PEAK < c.AudioPipeline.QUIET_PEAK)


def test_regex_replacements():
    section("14. A5 正则热词表")
    import json
    import tempfile
    import vox_core as c

    tmp = os.path.join(tempfile.gettempdir(), "vox_repl_regr.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({
            "confirmed": {"油画": "优化", "反斜杠测试": r"值\1保留"},
            "high_probability": {"智行": "之前"},
            "regex": {r"qwen\s*3": "Qwen3", r"(接|解)口文档": r"\1口文档",
                      r"vox[_ ]?windows": "Vox Windows",
                      r"版本\$?(\d+)": r"v\1", "坏正则[": "x"},
        }, f, ensure_ascii=False)
    try:
        c.PostProcessor._repl_cache = None
        c.PostProcessor._repl_path = None
        compiled = c.PostProcessor.load_replacements(tmp)
        check("规则已编译(字面+正则)", len(compiled) >= 7, f"{len(compiled)}")
        check("元组为 (pattern, replacement, is_regex)",
              all(len(t) == 3 and isinstance(t[2], bool) for t in compiled))
        check("无效正则被跳过而非崩溃",
              not any(t[0].pattern == "坏正则[" for t in compiled))

        def apply(text):
            c.PostProcessor._repl_cache = None
            c.PostProcessor._repl_path = None
            return c.PostProcessor.process(text, {"replacements_file": tmp,
                                                  "language": "zh",
                                                  "fidelity": {"mode": "verbatim"}})

        out = apply("我在用qwen 3和Vox Windows写代码")
        check("正则: qwen 3 -> Qwen3", "Qwen3" in out, out)
        check("正则: vox windows -> Vox Windows", "Vox Windows" in out, out)
        check("字面规则仍生效", "优化" in apply("油画这个词"))
        check("字面替换含 \\1 时逐字插入且不崩", "值\\1保留" in apply("反斜杠测试"))
        check("正则分组引用 \\1 / $1 可用", "接口文档" in apply("这是接口文档"))
    finally:
        c.PostProcessor._repl_cache = None
        c.PostProcessor._repl_path = None
        if os.path.exists(tmp):
            os.unlink(tmp)


def test_context_lexicon():
    section("15. A1 动态语境词表 (削减版: 不读剪贴板)")
    import vox_core as c

    cfg_static = {"whisper_prompt": "用户在讨论 SQLite 与 FastAPI 的开发工作"}
    real_cfg = c.ConfigManager.load()
    check("config 有 context_lexicon 缺省键",
          isinstance(real_cfg.get("context_lexicon"), dict))

    # HARD requirement: the clipboard must never be read (privacy + it would race
    # with the paste-restore logic).
    real_get = c.SmartOutput.get_clipboard_text
    reads = {"n": 0}

    def _boom(*a, **k):
        reads["n"] += 1
        raise AssertionError("ContextLexicon must not read the clipboard")

    c.SmartOutput.get_clipboard_text = staticmethod(_boom)
    try:
        terms = c.ContextLexicon.collect(cfg_static)
    finally:
        c.SmartOutput.get_clipboard_text = staticmethod(real_get)
    check("采集不读取剪贴板", reads["n"] == 0, f"reads={reads['n']}")
    check("采到静态词表的术语", any(t in ("SQLite", "FastAPI") for t in terms),
          str(terms[:6]))
    check("术语去重(大小写不敏感)", len({t.lower() for t in terms}) == len(terms))

    # A whitespace-separated Chinese PROSE sentence must not yield CJK fragments.
    only_static = c.ContextLexicon._from_static.__func__(
        c.ContextLexicon, {"whisper_prompt": "用户在讨论 SQLite 与 FastAPI 的开发工作"})
    frags = [t for t in only_static if any("\u4e00" <= ch <= "\u9fff" for ch in t)]
    check("中文散句不产生 CJK 碎片", not frags, str(frags))

    # A curated 、-separated list MUST still yield its terms, minus the section label.
    curated = c.ContextLexicon._from_static.__func__(c.ContextLexicon, {
        "whisper_prompt": "用户在讨论软件架构与开发。高频词：Qwen千问、甘特图、路由任务、置信度、上下文。"})
    check("顿号列表中的中文术语被采集",
          all(t in curated for t in ("甘特图", "路由任务", "置信度", "上下文")),
          str(curated))
    check("段落标签未被当作术语", "高频词" not in curated, str(curated))

    # Byte-for-byte fallback whenever nothing is harvestable.
    sources = (c.ContextLexicon._from_static, c.ContextLexicon._from_foreground,
               c.ContextLexicon._from_history)
    c.ContextLexicon._from_static = classmethod(lambda cls, cfg: [])
    c.ContextLexicon._from_foreground = classmethod(lambda cls: [])
    c.ContextLexicon._from_history = classmethod(lambda cls, limit=20: [])
    try:
        bare = c.build_prompt({"whisper_prompt": "。。。",
                               "context_lexicon": {"enabled": True}})
    finally:
        (c.ContextLexicon._from_static, c.ContextLexicon._from_foreground,
         c.ContextLexicon._from_history) = sources
    check("所有来源皆空时逐字节等于静态 prompt", bare == "。。。", repr(bare))
    check("关闭时逐字节等于静态 prompt",
          c.build_prompt({"whisper_prompt": "原文",
                          "context_lexicon": {"enabled": False}}) == "原文")

    # Hard caps and total robustness.
    tight = c.ContextLexicon.collect(cfg_static, max_terms=1)
    check("max_terms 上限生效", len(tight) <= 1, str(tight))
    for bad in (None, {}, {"whisper_prompt": None},
                {"context_lexicon": {"max_terms": "x", "budget_chars": None}}):
        try:
            c.ContextLexicon.collect(bad)
            c.build_prompt(bad)
            ok = True
        except Exception:
            ok = False
        check(f"异常输入不崩: {str(bad)[:30]}", ok)


def test_pipeline_attr_defensive():
    section("16. 归因访问必须防御式 (回归守卫: test_suite #26)")
    import types
    import vox_daemon as d

    # test_suite #26 caught this: the transcription worker reads three pipeline
    # attributes for B1/B2 attribution, and a pipeline object lacking them (a test
    # stub, or a partially built pipeline) used to raise AttributeError — turning a
    # neat-metadata lookup into a FAILED TRANSCRIPTION.
    class Bare:
        TARGET_SR = 16000

    stub = types.SimpleNamespace(pipeline=Bare())

    check("_dev_name() 在缺失时返回空串", d.VoxDaemon._dev_name(stub) == "")
    check("_dev_idx() 在缺失时返回占位符", d.VoxDaemon._dev_idx(stub) == "-")
    check("_session_peak() 在缺失时返回 0.0", d.VoxDaemon._session_peak(stub) == 0.0)
    check("_peak_verdict() 在缺失时返回 ok", d.VoxDaemon._peak_verdict(stub) == "ok")

    # A hostile value must not propagate either.
    class Hostile:
        TARGET_SR = 16000
        device_name = None
        device_idx = None
        session_peak = "not-a-number"

        def peak_verdict(self):
            raise RuntimeError("boom")

    h = types.SimpleNamespace(pipeline=Hostile())
    check("device_name=None 归一为空串", d.VoxDaemon._dev_name(h) == "")
    check("session_peak 非数值归一为 0.0", d.VoxDaemon._session_peak(h) == 0.0)
    check("peak_verdict 抛异常时降级为 ok",
          d.VoxDaemon._peak_verdict(h) == "ok")

    # And the real pipeline must still answer correctly. A fresh AudioPipeline has
    # device_idx=None and device_name="", so the helpers must return their display
    # placeholders rather than raising.
    real = types.SimpleNamespace(pipeline=__import__("vox_core").AudioPipeline())
    check("真实 pipeline 仍正常工作",
          d.VoxDaemon._dev_name(real) == "" and d.VoxDaemon._dev_idx(real) == "-")


def test_vad_segmenter():
    section("17. vox_vad 停顿分段 (流式预览的增量基础)")
    import numpy as np
    import vox_vad

    SR = 16000

    def tone(sec, amp=0.25):
        t = np.arange(int(SR * sec), dtype=np.float32) / SR
        return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    def silence(sec):
        return np.zeros(int(SR * sec), dtype=np.float32)

    check("全静音 -> None",
        vox_vad.last_confirmed_segment_end(silence(3), SR) is None)
    check("连续说话无停顿 -> None",
        vox_vad.last_confirmed_segment_end(tone(5), SR) is None)
    check("空数组 -> None",
        vox_vad.last_confirmed_segment_end(np.zeros(0, dtype=np.float32), SR) is None)

    end = vox_vad.last_confirmed_segment_end(np.concatenate([tone(2.0), silence(0.6)]), SR)
    check("说话+长停顿 -> 返回语音结束点",
        end is not None and abs(end - int(2.0 * SR)) <= int(0.12 * SR), f"end={end}")

    check("停顿过短(100ms) 不结算",
        vox_vad.last_confirmed_segment_end(
            np.concatenate([tone(2.0), silence(0.1), tone(2.0)]), SR) is None)

    end = vox_vad.last_confirmed_segment_end(
        np.concatenate([tone(2.0), silence(0.6), tone(1.5), silence(0.6)]), SR)
    check("两个已确认段 -> 返回最后一段结束",
        end is not None and abs(end - int(4.1 * SR)) <= int(0.12 * SR), f"end={end}")


def test_streaming_worker_is_incremental():
    section("18. 流式预览 worker 真的增量 (不再重转整段)")
    import threading
    import time

    import numpy as np
    import vox_daemon as d

    SR = 16000

    class FakePipeline:
        TARGET_SR = 16000
        device_sr = 16000

        def __init__(self):
            self.frames = []
            self._lock = threading.Lock()

        def resample(self, audio, src, dst):
            return np.asarray(audio, dtype=np.float32).ravel()

    class FakeChain:
        def __init__(self):
            self.pushed = []          # length of every buffer handed to the ASR

        def transcribe_preview(self, audio):
            self.pushed.append(len(audio))
            return "字"

    class Stub:
        pass

    pipeline, chain = FakePipeline(), FakeChain()
    s = Stub()
    s.pipeline, s.chain = pipeline, chain
    s._stop_event = threading.Event()
    s._state_lock = threading.Lock()
    s._rec_session = 1
    s.state = "RECORDING_HOLD"
    s.cfg = {"streaming": {"interim_engine": "sensevoice-cpu", "interval_ms": 350}}
    s._update_overlay = lambda *a, **k: None

    th = threading.Thread(target=d.VoxDaemon._streaming_worker, args=(s, 1), daemon=True)
    th.start()
    try:
        # Feed a 4 s take: speech / pause / speech, in 0.25 s blocks.
        block = int(SR * 0.25)
        def tone(sec, amp=0.25):
            t = np.arange(int(SR * sec), dtype=np.float32) / SR
            return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

        plan = [tone(0.25)] * 7 + [np.zeros(block, dtype=np.float32)] * 5 + [tone(0.25)] * 4
        for blk in plan:
            with pipeline._lock:
                pipeline.frames.append(blk.reshape(-1, 1))
            time.sleep(0.06)
        time.sleep(0.8)
    finally:
        s.state = "IDLE"
        time.sleep(0.6)
        th.join(timeout=5)

    total = sum(chain.pushed)
    take_len = sum(len(b) for b in plan)
    n = len(chain.pushed)
    print(f"     转写调用 {n} 次, 累计推送 {total / SR:.1f}s 音频 "
          f"(录音 {take_len / SR:.1f}s)")

    check("worker 确实转写过预览", n > 0, f"{n} calls")
    check("没有把整段缓冲反复整段重转",
        total < take_len * 4, f"{total / SR:.1f}s pushed for {take_len / SR:.1f}s take")
    check("单次转写不超过录音长度",
        max(chain.pushed) <= take_len, f"max={max(chain.pushed) / SR:.1f}s")
    check("worker 已随状态机退出", not th.is_alive())


def test_backup_wav_is_incrementally_valid():
    section("19. 录音热备份: 追加式写入且中途即合法 (IO 由 O(n²) 变 O(n))")
    import tempfile
    import wave

    import numpy as np
    import vox_daemon as d

    SR = 16000
    tmp = tempfile.mkdtemp(prefix="vox_bk_")
    path = os.path.join(tmp, "b.wav")
    try:
        tone = (0.25 * np.sin(2 * np.pi * 220 * np.arange(SR * 6) / SR)).astype(np.float32)
        fh = d._wav_begin(path, SR)
        written, frames_seen = 0, []
        for i in range(0, len(tone), SR * 2):
            chunk = tone[i:i + SR * 2]
            written = d._wav_append(fh, (chunk * 32767).astype("<i2").tobytes(), written)
            # Read it back MID-WRITE: a crash-recovery file must be playable at any
            # instant, which is exactly what `wave`'s own writer cannot guarantee.
            with wave.open(path, "rb") as w:
                frames_seen.append(w.getnframes())
        fh.close()

        check("中途读取的帧数单调正确",
              frames_seen == [SR * 2 * (i + 1) for i in range(len(frames_seen))],
              f"{frames_seen}")
        with wave.open(path, "rb") as w:
            check("最终时长正确", abs(w.getnframes() / w.getframerate() - 6.0) < 0.01)
            data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        expect = (tone * 32767).astype("<i2")
        check("内容逐样本一致（追加不丢样本/不错位）",
              bool(np.array_equal(expect, data)), f"{len(data)} samples")
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
        try:
            os.rmdir(tmp)
        except Exception:
            pass


# ─── 20. 2026-11-18 fixes ──────────────────────────────────────────
def test_20261118_fixes():
    section("20. 2026-11-18 修复守卫 (假成功 / 原子写 / 词表 / 默认值)")
    import tempfile
    import threading
    import shutil
    import vox_win32 as w
    import vox_core as core

    # BUG-1: a failed SendInput must never be reported as a successful paste.
    real_send = w._send
    try:
        w._send = lambda events: False
        ok = w.send_paste()
        check("SendInput 失败 → send_paste=False", ok is False)
        check("SendInput 失败 → last_error 有原因", bool(w.last_error()))
    finally:
        w._send = real_send

    try:
        w._send = lambda events: True
        w._set_error("stale-error")
        ok = w.send_paste()
        check("SendInput 成功 → send_paste=True", ok is True)
        check("SendInput 成功 → 清空错误", w.last_error() == "")
    finally:
        w._send = real_send
        w._set_error("")

    # BUG-5: concurrent atomic writes must leave valid JSON and no debris.
    tmpdir = tempfile.mkdtemp(prefix="vox-atomic-")
    p = os.path.join(tmpdir, "state.json")
    errors = []

    def writer(v):
        try:
            for i in range(20):
                core.safe_write_json(p, {"v": v, "i": i})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("并发原子写无异常", not errors, str(errors[:2]))
    parsed = core.safe_read_json(p, None)
    check("并发写后 JSON 仍完整可解析",
          isinstance(parsed, dict) and "v" in parsed, str(parsed)[:80])
    debris = [f for f in os.listdir(tmpdir) if f.startswith(".vox-")]
    check("无 .vox-*.tmp 残留", not debris, str(debris[:3]))

    # BUG-11: curated two-character CJK terms must survive the lexicon filter.
    out, seen = [], set()
    core.ContextLexicon._keep("千问", out, seen)
    check("两字中文术语被保留", "千问" in out, str(out))
    out2, seen2 = [], set()
    core.ContextLexicon._keep("ab", out2, seen2)
    check("两字拉丁噪声仍被过滤", not out2, str(out2))

    # BUG-17: an in-memory config mutation must not corrupt DEFAULT_CONFIG.
    orig_path = core.CONFIG_PATH
    orig_state = (core.ConfigManager._current_cfg, core.ConfigManager._last_mtime)
    try:
        core.CONFIG_PATH = os.path.join(tmpdir, "cfg.json")
        core.ConfigManager._current_cfg = None
        core.ConfigManager._last_mtime = 0
        cfg = core.ConfigManager.load()
        before = core.DEFAULT_CONFIG["engines"]["groq"]["model"]
        cfg["engines"]["groq"]["model"] = "MUTATED-BY-TEST"
        core.ConfigManager._current_cfg = None
        core.ConfigManager._last_mtime = 0
        fresh = core.ConfigManager.load()
        check("深拷贝隔离 DEFAULT_CONFIG",
              fresh["engines"]["groq"]["model"] == before,
              f"{fresh['engines']['groq']['model']} vs {before}")
    finally:
        core.CONFIG_PATH = orig_path
        core.ConfigManager._current_cfg, core.ConfigManager._last_mtime = orig_state
        # 清理本测试自己建的临时目录，避免修复守卫反而污染主目录。
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_keyboard_watcher():
    section("21. 自建全局键盘监听 (BUG-19: 焦点无关 + 自愈)")
    import ctypes
    import vox_win32 as w

    watcher = w.KeyboardWatcher()
    try:
        check("start() 装上钩子", watcher.start() is True,
              f"installs={watcher.install_count()}")
        check("is_alive() 为真", watcher.is_alive() is True)
        check("初始无按键", watcher.keys_all_down([(0x11,), (0x5B, 0x5C)]) is False)

        groups = w.resolve_hotkey_vks(["control", "win"])
        check("Ctrl+Win 解析为 [(17,),(91,92)]", groups == [(0x11,), (0x5B, 0x5C)], str(groups))

        # Deterministic state-machine assertion: drive the hook callback directly.
        # (Injecting synthetic input and hoping the OS hook sees it is flaky in a
        # non-interactive test process, so that is only reported as INFO below.)
        def _fire(vk, down, extended_vk=False):
            st = w._KBDLLHOOKSTRUCT()
            st.vkCode = vk
            st.scanCode = 0
            st.flags = 0
            st.time = 0
            st.dwExtraInfo = 0
            return watcher._hook_proc(
                0, 0x0100 if down else 0x0101, ctypes.pointer(st))

        _fire(0xA2, True)          # physical left ctrl arrives as 0xA2
        _fire(0x5B, True)
        check("Ctrl+Win 按下被识别", watcher.keys_all_down(groups) is True,
              str(sorted(watcher.snapshot())))
        check("0xA2 已归一化为 0x11", watcher.is_down(0x11) is True)

        _fire(0x5B, False)
        check("松开 Win 后组合解除", watcher.keys_all_down(groups) is False)
        _fire(0xA2, False)
        check("全部松开后状态清空", watcher.snapshot() == set(),
              str(sorted(watcher.snapshot())))

        # Best-effort: does the OS hook observe synthetic input right now?
        try:
            w._send([w._key_event(0x11, keyup=False), w._key_event(0x5B, keyup=False)])
            time.sleep(0.2)
            injected_seen = watcher.keys_all_down(groups)
        except Exception:
            injected_seen = None
        finally:
            try:
                w._send([w._key_event(0x5B, keyup=True), w._key_event(0x11, keyup=True)])
            except Exception:
                pass
        print(f"  [INFO] OS 钩子是否看到合成输入: {injected_seen} "
              f"(非交互测试进程中可能为 False，由实机验证)")

        # Self-healing: after a stop, watchdog must bring it back.
        before = watcher.install_count()
        watcher.stop()
        check("stop() 后不再 alive", watcher.is_alive() is False)
        watcher.watchdog()
        check("watchdog() 自动重装", watcher.is_alive() is True,
              f"installs {before} -> {watcher.install_count()}")

        # The callback must never raise out of the OS hook.
        try:
            watcher._hook_proc(0, 0x0100, None)
            watcher._hook_proc(0, 0x0100, 0)
            watcher._hook_proc(999, 0x0100, 0)
            ok_proc = True
        except Exception:
            ok_proc = False
        check("钩子回调对畸形入参不抛异常", ok_proc)
    finally:
        watcher.stop()


def test_hotkey_registration_guard():
    section("22. 系统热键注册守卫 (2026-11-18 纯修饰键事故)")
    import vox_win32 as w

    # The 19:30 incident: Ctrl+Alt "registered successfully" via a
    # modifier-as-vk trick and then NEVER fired, while the daemon suppressed
    # its hook path — the hotkey was completely dead. Such chords must now be
    # refused with an explicit error instead of registered.
    mods, vk, err = w.resolve_register_hotkey(["control", "alt"])
    check("Ctrl+Alt 纯修饰组合被拒绝", err is not None and vk is None, str(err))
    mods, vk, err = w.resolve_register_hotkey(["control"])
    check("单修饰键被拒绝", err is not None, str(err))
    mods, vk, err = w.resolve_register_hotkey(["win", "ctrl"])
    check("Win+Ctrl 纯修饰组合被拒绝", err is not None, str(err))

    # The modifier-as-key trap must be gone for good.
    check("_MOD_AS_KEY 陷阱已删除", not hasattr(w, "_MOD_AS_KEY"))

    # Real-key chords must still resolve.
    mods, vk, err = w.resolve_register_hotkey(["control", "win", "z"])
    check("Ctrl+Win+Z 可解析",
          err is None and mods == (w.MOD_CONTROL | w.MOD_WIN) and vk == 0x5A,
          f"mods={mods} vk={vk} err={err}")
    mods, vk, err = w.resolve_register_hotkey(["control", "alt", "f9"])
    check("Ctrl+Alt+F9 可解析",
          err is None and vk == 0x78, f"vk={vk} err={err}")

    # UIPI tripwire: SmartOutput must know whether it runs below Medium.
    import vox_core as c
    c.SmartOutput._IL_CACHE = None  # force a fresh probe
    ok1 = c.SmartOutput._integrity_ok_cached()
    cached = c.SmartOutput._IL_CACHE
    ok2 = c.SmartOutput._integrity_ok_cached()
    check("完整性探测返回 bool 且结果被缓存",
          isinstance(ok1, bool) and ok1 == ok2 and cached is not None,
          f"ok={ok1} cache={cached}")
    # Simulated Low must flip the verdict (rid 0x1000 = Low).
    c.SmartOutput._IL_CACHE = (0x1000, "Low")
    check("Low 完整性时绊线报警", c.SmartOutput._integrity_ok_cached() is False)
    c.SmartOutput._IL_CACHE = None  # don't poison later tests


def main():
    ap = argparse.ArgumentParser(description="Vox regression tests")
    ap.add_argument("--inject", action="store_true",
                    help="包含真实 Ctrl+V 注入 (会向焦点窗口打一个空格)")
    ap.add_argument("--daemon", action="store_true",
                    help="包含守护进程冒烟测试 (会短暂占用全局热键)")
    args = ap.parse_args()

    print("=" * 62)
    print(" Vox 回归测试 — 2026-10-13 审计修复的守卫")
    print("=" * 62)
    print(f"解释器: {sys.executable}")

    test_backend()
    test_roundtrip()
    test_truthful_reporting()
    test_restore_safety()
    if args.inject:
        test_injection()
    else:
        print("\n=== 5. 真实 Ctrl+V 注入 — 已跳过 (加 --inject 启用) ===")
    test_ondemand()
    test_overlay()
    test_process_liveness()
    test_orphan_watchdog()
    test_error_classification()
    test_perf_and_health()
    test_peak_verdict()
    test_regex_replacements()
    test_context_lexicon()
    test_pipeline_attr_defensive()
    test_vad_segmenter()
    test_streaming_worker_is_incremental()
    test_backup_wav_is_incrementally_valid()
    test_20261118_fixes()
    test_keyboard_watcher()
    test_hotkey_registration_guard()
    if args.daemon:
        test_daemon()
    else:
        print("\n=== 10. 守护进程冒烟测试 — 已跳过 (加 --daemon 启用) ===")

    print("\n" + "=" * 62)
    print(f" 通过 {len(_PASS)} 项, 失败 {len(_FAIL)} 项")
    if _FAIL:
        for n in _FAIL:
            print(f"   FAIL: {n}")
    print("=" * 62)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
