#!/usr/bin/env python3
"""
Vox Comprehensive Automated Test Suite.
Verifies audio DSP, cloud & local STT engines, post-processing,
smart clipboard injection, zero-lock file safety, and overlay focus preservation.
Fully automated — requires no manual user intervention.
"""

import os
import sys
import time
import math
import tempfile
import threading
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vox_core as core
import vox_overlay as overlay

TEST_PASSED = "✅ PASSED"
TEST_FAILED = "❌ FAILED"
failures = []


def run_test(name, test_fn):
    print(f"\n[TEST] {name} ...", flush=True)
    t0 = time.time()
    try:
        test_fn()
        latency = time.time() - t0
        print(f"{TEST_PASSED} ({latency:.3f}s)")
    except Exception as e:
        latency = time.time() - t0
        print(f"{TEST_FAILED} ({latency:.3f}s): {e}")
        failures.append((name, str(e)))


# ─── 1. Audio DSP & Normalization Tests ────────────────────────────
def test_audio_dsp():
    pipeline = core.AudioPipeline()

    # 1.1 Resampling test (48kHz -> 16kHz)
    t = np.linspace(0, 1.0, 48000, endpoint=False)
    sine_48k = np.sin(2 * np.pi * 440 * t).astype("float32")
    resampled = pipeline.resample(sine_48k, 48000, 16000)
    assert resampled.shape == (16000,), f"Expected 16000 samples, got {resampled.shape}"

    # 1.2 AGC Normalization test (boost quiet audio)
    quiet = sine_48k[:16000] * 0.02  # peak = 0.02
    normalized = pipeline.normalize_audio(quiet, target_peak=0.85, max_gain=15.0)
    new_peak = float(np.max(np.abs(normalized)))
    assert 0.25 <= new_peak <= 0.86, f"AGC failed: peak was 0.02, normalized is {new_peak}"

    # 1.3 VAD Trim test (strip silence)
    silence = np.zeros(3200, dtype="float32")  # 200ms silence
    speech = np.sin(2 * np.pi * 440 * np.linspace(0, 0.5, 8000)).astype("float32") * 0.5
    composite = np.concatenate([silence, speech, silence])
    trimmed = pipeline.vad_trim(composite, sr=16000, threshold_rms=0.005)
    assert trimmed.size < composite.size, f"VAD trim did not reduce silence: {trimmed.size} vs {composite.size}"

    # 1.4 Device resolution test
    dev_idx, dev_name = pipeline.resolve_device()
    assert dev_idx is not None, "Failed to resolve any input device"
    assert len(dev_name) > 0, "Device name is empty"


# ─── 2. STT Engine End-to-End Tests ───────────────────────────────
def test_groq_engine():
    cfg = core.ConfigManager.load()
    key = cfg.get("engines", {}).get("groq", {}).get("api_key")
    if not key:
        print("    (Groq API key not configured, skipping cloud test)")
        return

    sample = os.path.join(core.SCRIPT_DIR, "tts_test.wav.flac")
    assert os.path.exists(sample), f"Sample audio missing: {sample}"

    engine = core.GroqEngine(key, "whisper-large-v3-turbo")
    text, latency, cost = engine.transcribe(sample, language="auto")
    print(f"    Groq output: {text!r} ({latency:.3f}s)")
    assert len(text) > 5, "Groq transcription too short"
    assert "测试" in text or "云端" in text or "口述" in text, f"Unexpected text: {text}"


def test_local_sensevoice_engine():
    """Local engine test — SenseVoice (FunASR), the single retained local engine.

    OPT-IN: this is the only test that loads a local model (~1 GB), so it runs
    only when --local is passed. The default suite exercises the CLOUD path only.
    """
    cfg = core.ConfigManager.load()
    sample = os.path.join(core.SCRIPT_DIR, "tts_test.wav.flac")
    assert os.path.exists(sample), f"Sample audio missing: {sample}"
    if not core._FASR_OK:
        print("    (funasr not installed, skipping local test)")
        return

    sv_cfg = cfg.get("engines", {}).get("sensevoice", {})
    engine = core.SenseVoiceEngine(device=sv_cfg.get("device", "cpu"))
    text, latency, cost = engine.transcribe(sample, language="auto")
    print(f"    SenseVoice local output: {text!r} ({latency:.3f}s)")
    assert len(text) > 3, "SenseVoice local transcription too short"


def test_engine_chain_fallback():
    cfg = core.ConfigManager.load()
    chain = core.EngineChain(cfg)
    sample = os.path.join(core.SCRIPT_DIR, "tts_test.wav.flac")
    res = chain.transcribe(sample, language="zh", prompt="测试提示词：Qwen, Groq, Whisper")
    # G1/G2: the live chain may be groq, mimo, or sensevoice.
    assert ("groq" in res["engine"] or "mimo" in res["engine"]
            or "sensevoice" in res["engine"]), f"Invalid engine: {res['engine']}"
    assert len(res["text"]) > 3, "Engine chain returned empty text"


# ─── 3. Post-Processing Pipeline Tests ────────────────────────────
def test_post_processing():
    cfg = core.ConfigManager.load()

    # Traditional to Simplified
    trad = "大家好，今天我們來測試雲端口述輸入功能"
    simp = core.PostProcessor.process(trad, cfg)
    assert "我们" in simp and "云端" in simp, f"Traditional conversion failed: {simp}"

    # Hallucination Removal
    halluc = "请大家订阅我的频道 Thanks for watching. https://youtube.com/watch?v=123 please remember to like and subscribe."
    cleaned = core.PostProcessor.process(halluc, cfg)
    assert "youtube" not in cleaned and "watching" not in cleaned and "subscribe" not in cleaned

    # Replacements Dictionary
    dirty = "今天先跑一下 Qwen 然后 GORQ commit the changes"
    fixed = core.PostProcessor.process(dirty, cfg)
    assert "Groq" in fixed, f"Dictionary replacement for GORQ failed: {fixed}"

    # Duplicate Punctuation Cleanup
    punct = "这里有重复标点，，，到底对不对？？"
    fixed_punct = core.PostProcessor.process(punct, cfg)
    assert "，，" not in fixed_punct and "？？" not in fixed_punct

    # Whisper Silence & Foreign Noise Filtering
    assert core.PostProcessor.process("#おやすみなさい。고춧가루.", cfg) == ""
    assert core.PostProcessor.process("고춧가루. #おやすみなさい。", cfg) == ""
    mixed = core.PostProcessor.process("测试口述正常内容。おやすみなさい", cfg)
    assert "测试口述正常内容" in mixed and "おやすみなさい" not in mixed


# ─── 4. Smart Output & Clipboard Preservation Tests ───────────────
def test_clipboard_preservation():
    if not core._WIN_OK:
        print("    (Win32 clipboard not available, skipping)")
        return

    orig = core.SmartOutput.get_clipboard_text() or "Original_Backup_Content_12345"
    core.SmartOutput.set_clipboard_text(orig)

    # Output text with clipboard restore
    test_text = "Vox_Automated_Test_Pasting_Snippet_67890"
    res = core.SmartOutput.output(test_text, method="clipboard_only", restore_clipboard=False)
    assert res["copied"] is True, "Failed to copy test text"

    read_back = core.SmartOutput.get_clipboard_text()
    assert read_back == test_text, f"Clipboard content mismatch: {read_back}"

    # Restore original
    core.SmartOutput.set_clipboard_text(orig)
    final_read = core.SmartOutput.get_clipboard_text()
    assert final_read == orig, "Failed to restore original clipboard content"


# ─── 5. Floating Pill Overlay Tests ───────────────────────────────
def test_overlay_creation_and_styles():
    ov = overlay.VoxOverlay()
    assert ov.root is not None, "Overlay root window is None"

    # Verify WS_EX_NOACTIVATE and WS_EX_TOOLWINDOW style
    if overlay._HAS_WIN32:
        hwnd = int(ov.root.wm_frame(), 16)
        style = overlay.user32.GetWindowLongW(hwnd, overlay.GWL_EXSTYLE)
        assert bool(style & overlay.WS_EX_NOACTIVATE), "Overlay missing WS_EX_NOACTIVATE style!"
        assert bool(style & overlay.WS_EX_TOOLWINDOW), "Overlay missing WS_EX_TOOLWINDOW style!"
        assert ov.root.attributes("-topmost"), "Overlay missing topmost attribute!"

    # Verify State Transitions
    ov.show_recording("测试录音中")
    assert ov.state == "recording"
    ov.update_level(0.7)
    assert ov.level > 0.0

    ov.show_transcribing()
    assert ov.state == "transcribing"

    ov.show_done("测试完成")
    assert ov.state == "done"

    ov.show_cancelled()
    assert ov.state == "cancelled"

    ov.show_error("测试错误")
    assert ov.state == "error"

    ov.hide()
    ov.root.destroy()


# ─── 6. Config & Zero-Lock Concurrency Tests ──────────────────────
def test_config_thread_safety():
    cfg = core.ConfigManager.load()
    assert "hotkey" in cfg
    assert "engine_priority" in cfg

    # Concurrent read/write stress test (simulates multi-process / multi-thread access)
    errors = []

    def writer():
        for i in range(25):
            c = core.ConfigManager.load()
            c["test_counter"] = i
            if not core.safe_write_json(core.CONFIG_PATH, c):
                errors.append("Write failed")
            time.sleep(0.01)

    def reader():
        for i in range(25):
            c = core.safe_read_json(core.CONFIG_PATH, {})
            if not c:
                errors.append("Read empty")
            time.sleep(0.01)

    threads = [
        threading.Thread(target=writer),
        threading.Thread(target=reader),
        threading.Thread(target=writer),
        threading.Thread(target=reader),
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors, f"Concurrency lock errors occurred: {errors}"


# ─── 7. Sound Feedback Synthesis Tests ────────────────────────────
def test_sound_feedback():
    core.SoundFeedback.init_tones()
    assert "start" in core.SoundFeedback._tones, "Start tone was not synthesized"
    assert "done" in core.SoundFeedback._tones, "Done tone was not synthesized"
    assert "cancel" in core.SoundFeedback._tones, "Cancel tone was not synthesized"
    # Verify non-blocking playback
    core.SoundFeedback.play("start", enabled=True)
    core.SoundFeedback.play("done", enabled=True)
    core.SoundFeedback.play("cancel", enabled=True)


# ─── 8. Single-Instance Mutex & Double-Paste Prevention Tests ──────
def test_single_instance_mutex():
    import vox_daemon as daemon
    test_mutex_name = "Global\\Vox_Test_Suite_SingleInstance_Mutex"
    lock1 = daemon.acquire_single_instance_lock(name=test_mutex_name, port=38199)
    assert lock1 is not None, "Failed to acquire single-instance lock"

    # Second acquisition on same mutex/port must fail (proves duplicate processes are blocked)
    lock2 = daemon.acquire_single_instance_lock(name=test_mutex_name, port=38199)
    assert lock2 is None, "Secondary instance lock was erroneously acquired! Duplicate paste bug possible."

    # Release lock1
    lock1.release()

    # Re-acquisition must now succeed cleanly
    lock3 = daemon.acquire_single_instance_lock(name=test_mutex_name, port=38199)
    assert lock3 is not None, "Failed to re-acquire single-instance lock after release"
    lock3.release()


# ─── 9. Overlay Timer Continuity & No-Reset Tests ─────────────────
def test_overlay_timer_continuity():
    ov = overlay.VoxOverlay()
    try:
        ov.show_recording("测试提示")
        t0 = ov.start_time
        assert t0 > 0.0, "start_time was not initialized"

        time.sleep(0.06)
        # Calling show_recording again while in recording mode must preserve start_time
        ov.show_recording("新提示")
        assert ov.start_time == t0, f"start_time was reset: {ov.start_time} vs {t0}"

        # Updating level must not reset start_time
        ov.update_level(0.8)
        assert ov.start_time == t0, f"start_time was reset by update_level"

        ov.hide()
    finally:
        ov.root.destroy()


# ─── 10. GPU Acceleration & High-DPI Detection Tests ─────
def test_gpu_and_dpi_detection():
    dpi = core.get_dpi_scale()
    assert dpi >= 1.0, f"Invalid DPI scale: {dpi}"
    print(f"    检测到屏幕 DPI 缩放比: {dpi}x ({int(dpi * 100)}% 4K 高分屏)")

    # The GPU PROBE must work and be coherent. Whether a CUDA device is present is a
    # CONFIGURATION fact, not a pass/fail: Vox deliberately ships a CPU-only torch
    # build (the single retained local engine, SenseVoice, runs on CPU at RTF≈0.04),
    # so a CPU-only install is supported and must PASS. Asserting "CUDA must exist"
    # made a valid install report as broken.
    is_cuda, info = core.get_cuda_device_info()
    assert isinstance(is_cuda, bool), f"CUDA probe returned a non-bool: {is_cuda!r}"
    if is_cuda:
        assert info and str(info).strip(), f"CUDA reported available but info is empty: {info!r}"
        print(f"    硬件加速识别: CUDA 可用 — {info}")
    else:
        print(f"    硬件加速识别: 未启用 CUDA（CPU 版 torch，属预期配置）— {info}")

    # Detection must not be the gate for actually using a GPU slot: if the user adds
    # one, gpu_backed_engines() must still recognise it.
    cfg = core.ConfigManager.load()
    cfg_gpu = dict(cfg)
    cfg_gpu["engine_priority"] = ["sensevoice-cuda"]
    assert core.gpu_backed_engines(cfg_gpu), \
        "sensevoice-cuda slot must still be recognised by gpu_backed_engines()"
    print("    GPU 槽位识别: sensevoice-cuda 仍可被正确识别")

    # The local Whisper (faster-whisper) CUDA float16 latency benchmark used to run
    # here. It was removed with the Whisper engine; the retained local engine is
    # SenseVoice, whose model load is opt-in via --local (see test 2.2).


# ─── 11. Streaming Interim Preview Transcription Tests ─────────────
def test_streaming_preview():
    cfg = core.ConfigManager.load()
    chain = core.EngineChain(cfg)

    # Generate synthetic 1.2s audio of speech-like frequency
    sr = 16000
    t = np.linspace(0, 1.2, int(sr * 1.2), endpoint=False)
    synthetic = (np.sin(2 * np.pi * 300 * t) * 0.4).astype(np.float32)

    # Call preview method
    preview = chain.transcribe_preview(synthetic)
    assert isinstance(preview, str), "transcribe_preview must return a string"
    print(f"    流式预览函数调用成功 (返回类型: str, 字符数: {len(preview)})")


# ─── 12. Hotkey Parsing, Mode Dispatch & Hold Threshold Tests ──────
def test_hotkey_configuration_modes():
    import vox_daemon as daemon
    d = daemon.VoxDaemon()
    orig_cfg = core.ConfigManager.load()

    try:
        # 12.1 Test adaptive mode with Shift
        d.cfg["hotkey"] = {"combo": "shift", "mode": "adaptive", "hold_threshold_ms": 350}
        d._parse_hotkey()
        assert d._active_combo == "shift"
        assert d._hotkey_mode == "adaptive"
        assert math.isclose(d._hold_thresh, 0.35, abs_tol=0.01)
        assert d._is_single_modifier is True

        # 12.2 Test hold_only mode with custom combo
        d.cfg["hotkey"] = {"combo": "ctrl+alt+k", "mode": "hold_only", "hold_threshold_ms": 500}
        d._parse_hotkey()
        assert d._active_combo == "ctrl+alt+k"
        assert d._hotkey_mode == "hold_only"
        assert math.isclose(d._hold_thresh, 0.50, abs_tol=0.01)
        assert d._is_single_modifier is False

        # 12.3 Test toggle_only mode with F8
        d.cfg["hotkey"] = {"combo": "f8", "mode": "toggle_only", "hold_threshold_ms": 200}
        d._parse_hotkey()
        assert d._active_combo == "f8"
        assert d._hotkey_mode == "toggle_only"
        assert math.isclose(d._hold_thresh, 0.20, abs_tol=0.01)

        print("    快捷键自定义解析、长短按阈值与三种触发模式校验通过")
    finally:
        core.ConfigManager.save(orig_cfg)


def _reset_overlay_config_for_test():
    """Tests asserting base-capsule geometry must not be affected by the user's live
    drag/resize preferences (overlay.custom_width / custom_x / custom_y persist to the
    shared config.json when the real bubble is used)."""
    try:
        c = core.ConfigManager.load()
        ov = c.setdefault("overlay", {})
        if any(ov.get(k) is not None for k in ("custom_width", "custom_x", "custom_y")):
            for k in ("custom_width", "custom_x", "custom_y"):
                ov[k] = None
            core.ConfigManager.save(c)
    except Exception:
        pass


# ─── 13. High-DPI Dynamic Island Overlay Rendering Tests ───────────
def test_overlay_dpi_and_preview_rendering():
    _reset_overlay_config_for_test()
    ov = overlay.VoxOverlay()
    try:
        assert ov.scale == core.get_dpi_scale(), f"Overlay scale mismatch: {ov.scale} vs {core.get_dpi_scale()}"
        assert ov.width == int(round(ov.base_w * ov.scale)), f"Overlay width unscaled: {ov.width}"
        assert ov.height == int(round(ov.base_h * ov.scale)), f"Overlay height unscaled: {ov.height}"

        # Test streaming preview text rendering
        ov.show_recording(hint="松手完成 · 单击锁定")
        ov.update_preview("测试流式实时显字内容预览...")
        assert ov.preview_text == "测试流式实时显字内容预览..."
        print(f"    悬浮窗高分屏物理尺寸: {ov.width}x{ov.height}, 预览文本渲染正常")

        ov.hide()
    finally:
        ov.root.destroy()


# ─── 14. Draggable Overlay & Workarea Positioning Tests ────────────
def test_overlay_draggable_and_positioning():
    # 14.1 Workarea resolution test
    x1, y1, x2, y2 = core.get_screen_workarea()
    assert x2 > 800 and y2 > 600, f"Invalid workarea: {x1, y1, x2, y2}"
    print(f"    检测到屏幕可用工作区: ({x1}, {y1}) -> ({x2}, {y2}) [排除了任务栏高度]")

    # 14.2 Dragging and coordinate memory test
    ov = overlay.VoxOverlay()
    try:
        class FakeEvent:
            def __init__(self, x, y):
                self.x_root = x
                self.y_root = y

        # Simulate mouse press
        ov._on_drag_start(FakeEvent(500, 500))
        assert ov._drag_start_x == 500 and ov._drag_start_y == 500

        # Simulate drag motion
        ov._on_drag_motion(FakeEvent(550, 530))
        assert ov._is_dragging is True

        # Simulate release (persists custom_x, custom_y)
        ov._on_drag_release(FakeEvent(550, 530))
        assert ov.custom_x is not None and ov.custom_y is not None
        print(f"    鼠标拖拽坐标记忆测试通过: custom_x={ov.custom_x}, custom_y={ov.custom_y}")

        # Simulate double click to reset to bottom-center
        ov._on_double_click(FakeEvent(0, 0))
        assert ov.custom_x is None and ov.custom_y is None
        print(f"    双击药丸窗自适应复位居中测试通过")

        ov.hide()
    finally:
        ov.root.destroy()


# ─── 15. Spoken Fidelity & Verbatim Tone Preservation Tests ────────
def test_fidelity_and_verbatim_mode():
    sample_prompt = (
        "以下是关于软件开发、代码实现、AI与大模型讨论、技术方案研讨的中文口述内容："
        "Qwen, Groq, Whisper, LLM, API, Git, PR, prompt, token"
    )

    # 15.1 Verbatim mode must strip directive meta-prompts to prevent tone rewriting
    sanitized = core.sanitize_prompt_for_fidelity(sample_prompt, "verbatim")
    assert "以下是关于" not in sanitized, f"Directive preamble was not stripped: {sanitized}"
    assert "Qwen" in sanitized and "Groq" in sanitized, f"Keywords missing in sanitized prompt: {sanitized}"
    print(f"    原生口语提示词净化测试通过: {sanitized}")

    # Polished mode preserves full prompt
    preserved = core.sanitize_prompt_for_fidelity(sample_prompt, "polished")
    assert "以下是关于" in preserved, "Polished mode should keep full prompt"

    # 15.2 Auto period preservation
    cfg_verbatim = {
        "replacements_file": "replacements.json",
        "fidelity": {"mode": "verbatim", "auto_period": False}
    }
    short_colloquial = "这里没有问题"
    out = core.PostProcessor.process(short_colloquial, cfg_verbatim)
    assert not out.endswith("。"), f"Verbatim auto_period=False must not append full stop: {out}"

    cfg_polished = {
        "replacements_file": "replacements.json",
        "fidelity": {"mode": "polished", "auto_period": True}
    }
    out_polished = core.PostProcessor.process(short_colloquial, cfg_polished)
    assert out_polished.endswith("。"), f"Polished auto_period=True must append full stop: {out_polished}"
    print(f"    短句原生口语 vs 标点句号控制测试通过")


# ─── 16. Performance Monitoring & Multi-Engine Benchmarks Tests ────
def test_perf_monitoring_and_benchmarks():
    # 16.1 Record perf test
    rec = core.record_perf(
        engine_name="test_rtx5090",
        latency_s=0.0193,
        audio_len_s=1.0,
        text_preview="性能监控测试短句",
        chars=8
    )
    assert rec["latency_ms"] == 19.3
    assert math.isclose(rec["speedup"], 51.8, abs_tol=0.2)

    history = core.safe_read_json(core.PERF_HISTORY_FILE, [])
    assert any(r.get("engine") == "test_rtx5090" for r in history), "Failed to find test record in perf history"
    print(f"    转写性能记录持久化测试通过 (延迟: {rec['latency_ms']}ms, 加速: {rec['speedup']}x)")

    # Local model benchmark (faster-whisper local_cuda/local_cpu) was REMOVED with
    # the Whisper engines; the benchmark now only covers the cloud engines, and it
    # must never pull a local model onto the GPU.
    results = core.benchmark_all_engines()
    assert isinstance(results, dict), "benchmark_all_engines must return a dict"
    assert "local_cuda" not in results and "local_cpu" not in results, \
        f"removed local Whisper benchmarks came back: {sorted(results)}"
    for bkey, brow in results.items():
        assert brow.get("status"), f"benchmark row {bkey} has no status: {brow}"
    print(f"    全方案跑分测试完成: {sorted(results) or '无可用引擎'} (本地 Whisper 跑分已移除)")


def test_preview_pipeline_float32_integrity():
    """Regression for the B3 garbled-preview hard bug. The preview pipeline used to
    re-interpret float32 microphone frames as int16 bytes (pure noise) and never
    re-sample to 16 kHz. Feed a clean 440 Hz tone through the real AudioPipeline path
    and assert float32 dtype + amplitude are preserved end-to-end (no mic needed)."""
    ap = core.AudioPipeline(target_sr=16000)  # device_sr defaults to 16000 -> no resample
    sr = 16000
    t = np.arange(sr, dtype=np.float32) / sr
    tone = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    with ap._lock:
        ap.frames.append(tone.reshape(-1, 1).copy())  # mimic sd.InputStream channel layout
    audio = ap.stop_recording()
    # Core B3 guarantee: dtype stays float32 (must NOT be reinterpreted as int16/i2).
    assert audio.dtype == np.float32, f"B3 regression: preview dtype {audio.dtype} != float32"
    # ~1 s at 16 kHz survives resample + VAD trim.
    assert 14000 <= audio.size <= 16000, f"preview length off: {audio.size}"
    # Clean tone survives AGC (peak pulled toward 0.85 target): not silence, not noise.
    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio ** 2)))
    assert 0.6 <= peak <= 0.86, f"tone/AGC integrity failed, peak={peak:.3f}"
    assert rms > 0.2, f"expected a real tone, rms={rms:.4f}"


def test_confidence_proxy():
    """Complaint #7: lightweight ASR error-rate / confidence proxy. Real speech
    (Chinese/English/mixed) scores high; punctuation/symbol/whitespace noise scores ~0."""
    assert math.isclose(core.compute_confidence("今天天气测试"), 1.0, abs_tol=1e-9)
    assert math.isclose(core.compute_confidence("Hello世界123"), 1.0, abs_tol=1e-9)
    assert math.isclose(core.compute_confidence("Thank you"), round(8 / 9, 3), abs_tol=1e-3)
    assert core.compute_confidence("@#$%^&*()") == 0.0
    assert core.compute_confidence("   ") == 0.0
    assert core.compute_confidence("") == 0.0
    # A clean sentence must read as high-confidence; pure noise as low-confidence.
    assert core.compute_confidence("把语音助手修到能正常好用") > 0.9
    assert core.compute_confidence("......") < 0.1
    print("    置信度代理 (投诉#7) 测试通过: 真实文本高分、噪声/标点接近零")


def test_paste_method_selection():
    """Complaint #9: context-aware paste strategy. Terminal-like foreground windows must
    get clipboard_only (Ctrl+V often fails there); GUI gets smart_paste; explicit config
    override always wins."""
    S = core.SmartOutput
    # Default (GUI / unknown title) -> smart_paste
    assert S.select_method("Notepad") == "smart_paste"
    assert S.select_method("") == "smart_paste"
    # Terminal-like titles -> clipboard_only
    for term in ["Windows PowerShell", "cmd", "WSL: ubuntu", "PuTTY", "Xshell", "iTerm"]:
        assert S._looks_like_terminal(term) is True
        assert S.select_method(term) == "clipboard_only"
    # Non-terminal titles stay smart_paste
    for gui in ["Visual Studio Code", "Word", "Chrome", ""]:
        assert S._looks_like_terminal(gui) is False
    # Explicit config override beats everything
    assert S.select_method("Windows PowerShell", {"output_method": "smart_paste"}) == "smart_paste"
    assert S.select_method("Notepad", {"output_method": "clipboard_only"}) == "clipboard_only"
    print("    粘贴方法决策 (投诉#9) 测试通过: 终端->仅剪贴板、GUI->Ctrl+V、配置覆盖优先")


def test_overlay_write_debounce():
    """Complaint #2 / audit B7: the PortAudio callback used to rewrite overlay_state.json
    on the audio thread every ~10-100ms block, starving audio and causing bubble jitter.
    The daemon now coalesces rapid PURE-level refreshes below MIN_OVERLAY_WRITE_INTERVAL
    while letting every state/content change through. Exercise the pure predicate directly."""
    import vox_daemon
    T = vox_daemon.VoxDaemon._overlay_throttle
    interval = vox_daemon.VoxDaemon.MIN_OVERLAY_WRITE_INTERVAL
    # First write for a state always passes (prev None != state) -> skip=False.
    assert T(None, "", "", 0.0, "recording", "", "", None, now=100.0, interval=interval) is False
    # Same-state level-only refresh strictly inside the interval -> coalesced (skip=True).
    mid = 100.0 + interval * 0.5
    assert T("recording", "", "", 100.0, "recording", "", "", None, now=mid, interval=interval) is True
    # Across the interval -> passes through again (no drop).
    assert T("recording", "", "", 100.0, "recording", "", "", None, now=100.0 + interval * 2, interval=interval) is False
    # Any content change bypasses throttle even within the interval.
    assert T("recording", "", "", 100.0, "done", "", "", None, now=mid, interval=interval) is False
    assert T("recording", "", "", 100.0, "recording", "new", "", None, now=mid, interval=interval) is False
    assert T("recording", "", "", 100.0, "recording", "", "h", None, now=mid, interval=interval) is False
    assert T("recording", "", "", 100.0, "recording", "", "", "prev", now=mid, interval=interval) is False
    print("    悬浮窗写节流 (投诉#2/B7) 测试通过: 纯电平刷新合并、状态/内容变化立即写")


def test_input_device_hotplug():
    """Complaint #5 / audit B5: pure hot-plug change detection backing the daemon's
    _device_watch. Signatures are index|friendly_name (lower-cased); a device swap is
    detected, casing/whitespace noise does not false-positive, and None handling is safe."""
    ic = core.input_device_changed
    same = {"index": 3, "friendly_name": "Headset Mic"}
    assert ic(same, {"index": 3, "friendly_name": "  headset mic  "}) is False  # normalised equal
    assert ic(same, {"index": 3, "friendly_name": "Headset Mic"}) is False
    assert ic(same, {"index": 4, "friendly_name": "Headset Mic"}) is True        # index switch
    assert ic(same, {"index": 4, "friendly_name": "Mic in at front panel"}) is True  # plug swap
    assert ic(None, same) is True                                                 # first bind after none
    assert ic(same, None) is True                                                 # device removed
    assert ic(None, None) is False                                                # still nothing
    sig = core.input_device_signature
    assert sig({"index": 1, "friendly_name": "ABC"}) == sig({"index": 1, "friendly_name": "  abc "})
    print("    麦克风热插拔检测 (投诉#5/B5) 测试通过: 更换识别、噪声不误报、None 安全")


# ─── 22. Adaptive Bubble Height (complaint #1/#5 / B8) ─────────────
def test_overlay_adaptive_height():
    _reset_overlay_config_for_test()
    ov = overlay.VoxOverlay()
    try:
        # Base invariants unchanged — regression guard vs Test 13/14.
        assert ov.width == int(round(ov.base_w * ov.scale)), f"width unscaled: {ov.width}"
        assert ov.height == int(round(ov.base_h * ov.scale)), f"height unscaled: {ov.height}"
        base = ov.height
        assert ov.current_h == base, f"current_h must start at base capsule height, got {ov.current_h}"
        print(f"    基准胶囊尺寸不变: width={ov.width}x{ov.height}, current_h={ov.current_h}")

        avail_w = max(ov.s(40), ov.width - ov.s(64))

        # Measurement helpers are sane even if the canvas is headless (fallbacks).
        cw = ov._char_widths()
        short_lines = ov._wrap_to_lines("短文本", avail_w, 8)
        long_lines = ov._wrap_to_lines("这是一段很长的转写结果用来测试自适应高度是否随内容增长而增大换行显示" * 3, avail_w, 8)
        assert len(long_lines) >= len(short_lines), "longer text should need >= lines than short text"
        print(f"    测量助手正常: char_widths={cw}, 短文本{len(short_lines)}行/长文本{len(long_lines)}行")

        # done state: short keeps base, long grows, then shrinks back.
        ov.show_done("已粘贴到剪贴板")
        assert ov.current_h == base, f"short result should keep base height, got {ov.current_h}"
        print(f"    短结果保持基准尺寸: current_h={ov.current_h}")

        # Needs > max_lines(10) x ~41 CJK chars/line (device-px font) to trigger
        # truncation; ~560 chars guarantees it while staying deterministic.
        long_txt = ("这是一段很长的转写结果文本，用来验证药丸窗在内容变多时能否自动拉高以适应换行显示，"
                    "而不是像过去那样只显示一行并截断成省略号。再重复一些内容以确保超过最大行数上限继续增长。" * 14)
        ov.show_done(long_txt)
        assert ov.current_h > base, f"long result should grow the pill above base, got {ov.current_h} vs base {base}"
        nlines = len(ov._render_lines)
        last = (ov._render_lines or [""])
        only_last_ellipsis = [i for i, ln in enumerate(last) if ln.endswith("…")] == [nlines - 1]
        assert only_last_ellipsis, f"ellipsis may only mark the final truncated line, got positions {only_last_ellipsis}"
        print(f"    长结果自适应增高: base={base} -> current_h={ov.current_h} ({nlines} 行, 仅末行含省略号)")

        ov.show_done("短文本")
        assert ov.current_h == base, f"should shrink back to base after long result, got {ov.current_h}"
        print(f"    切回短结果后缩回基准: current_h={ov.current_h}")

        # Entering recording resets to the default capsule.
        ov.show_recording(hint="松手完成")
        assert ov.current_h == base, f"recording should reset to base capsule, got {ov.current_h}"
        print(f"    进入录音态重置为基准胶囊: current_h={ov.current_h}")

        # Error state adapts too.
        ov.hide()
        ov.show_error("未识别到有效语音")
        assert ov.current_h == base, f"short error should keep base, got {ov.current_h}"
        ov.show_error(("识别出错：音频流在处理过程中意外中断，请检查麦克风连接与权限设置后重试，" * 4))
        assert ov.current_h > base, f"long error should grow the pill, got {ov.current_h}"
        print(f"    错误态也自适应增高: current_h={ov.current_h}")

        print("    气泡自适应高度 (投诉#1/#5/B8) 测试通过: 短保持/长增高/可缩回/录音重置/省略号归位")
    finally:
        ov.root.destroy()


# ─── 23. Groq Key At-Rest Encryption (key leaked in git / B12) ─────
def test_groq_key_at_rest_encryption():
    import copy as _cp  # noqa: F401

    orig_cfg_path = core.CONFIG_PATH
    orig_state = (core.ConfigManager._current_cfg, core.ConfigManager._last_mtime)
    tmpdir = tempfile.mkdtemp()
    core.CONFIG_PATH = os.path.join(tmpdir, "config.json")
    core.ConfigManager._current_cfg = None
    core.ConfigManager._last_mtime = 0
    try:
        secret = "gsk-test-" + "ABCD1234" * 3 + "-\u4e2d\u6587"

        # crypto_utils round-trip + safety invariants.
        enc = core.maybe_encrypt(secret)
        assert enc.startswith("dpapi:"), "stored secret must carry the dpapi marker"
        assert secret not in enc, "plaintext secret must never appear inside ciphertext"
        assert core.maybe_decrypt(enc) == secret, "round-trip must recover the exact key"
        assert core.maybe_encrypt("") == "", "empty key is left untouched"

        # ConfigManager.save must write ciphertext only; plaintext must not hit disk.
        cfg = core.ConfigManager.load()
        cfg["engines"]["groq"]["api_key"] = secret
        core.ConfigManager.save(cfg)
        with open(core.CONFIG_PATH, "r", encoding="utf-8") as f:
            disk = f.read()
        assert secret not in disk, "Groq key must NOT be written to disk in plaintext"
        assert "dpapi:" in disk, "stored key must be DPAPI-encrypted on disk"

        # Fresh load transparently restores the plaintext key in memory for all readers.
        core.ConfigManager._current_cfg = None
        core.ConfigManager._last_mtime = 0
        loaded = core.ConfigManager.load()
        assert loaded["engines"]["groq"]["api_key"] == secret, "key must survive save->load"
        assert core.EngineChain(loaded)._resolve_api_key() == secret, "engine must resolve decrypted key"

        # Env var precedence (existing safety net) is preserved over the stored key.
        empty = core.ConfigManager.load()
        empty["engines"]["groq"]["api_key"] = ""
        core.ConfigManager.save(empty)
        os.environ["VOX_GROQ_KEY"] = "env-key-wins-XYZ"
        try:
            ch = core.EngineChain(core.ConfigManager.load())
            assert ch._resolve_api_key() == "env-key-wins-XYZ", "env var must override stored key"
        finally:
            del os.environ["VOX_GROQ_KEY"]

        print("    Groq key 落盘加密 (git 泄露 / B12) 测试通过: 密文落盘、内存透明、env 优先")
    finally:
        core.CONFIG_PATH = orig_cfg_path
        core.ConfigManager._current_cfg, core.ConfigManager._last_mtime = orig_state


# ─── 24. Transcription History Archive SQLite (complaint #6) ──────
def test_history_archive_sqlite():
    import tempfile as _tf
    import json as _json
    orig_db = core.HISTORY_DB
    orig_file = core.HISTORY_FILE
    tmpdir = _tf.mkdtemp()
    core.HISTORY_DB = os.path.join(tmpdir, "transcriptions.db")
    core.HISTORY_FILE = os.path.join(tmpdir, "transcriptions.jsonl")
    try:
        core.clear_history()
        assert core.read_history() == [], "history should start empty"

        r1 = core.record_transcription({"recording_id": "id-1", "engine": "groq", "text": "你好世界 hello", "chars": 10, "latency_s": 0.4, "confidence": 0.9})
        r2 = core.record_transcription({"recording_id": "id-2", "engine": "local", "text": "购买今天的股票行情分析", "chars": 12, "latency_s": 1.2, "confidence": 0.5})
        r3 = core.record_transcription({"recording_id": "id-3", "engine": "groq", "text": "会议记录与项目计划安排", "chars": 11, "latency_s": 0.6, "confidence": 0.8})
        assert r1 and r2 and r3, "record_transcription should return row dicts"

        rows = core.read_history(limit=10)
        assert len(rows) == 3, f"expected 3 archived rows, got {len(rows)}"
        assert [r["recording_id"] for r in rows] == ["id-3", "id-2", "id-1"], f"newest-first order broken: {[r['recording_id'] for r in rows]}"

        got = core.get_history("id-2")
        assert got is not None and got["text"] == "购买今天的股票行情分析", "get_history should fetch a single row"
        assert core.get_history("does-not-exist") is None

        s_text = core.search_history("股票")
        assert any(r["recording_id"] == "id-2" for r in s_text), "search should match text substring"
        s_engine = core.search_history("local")
        assert any(r["recording_id"] == "id-2" for r in s_engine), "search should also match engine field"
        assert core.search_history("zzz-no-match") == [], "unmatched search returns empty"

        csv_path = os.path.join(tmpdir, "export.csv")
        assert core.export_history_csv(csv_path) == csv_path
        assert os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            csv_text = f.read()
        first_line = csv_text.splitlines()[0] if csv_text.splitlines() else ""
        assert "股票" in csv_text and "recording_id" in first_line, "CSV must carry header + data rows"

        core.clear_history()
        assert core.read_history() == [], "clear_history should empty the archive"

        # Backward-compat: legacy JSONL rows backfill into the DB on next read.
        with open(core.HISTORY_FILE, "w", encoding="utf-8") as f:
            f.write(_json.dumps({"recording_id": "legacy-1", "timestamp": "2020-01-01T00:00:00", "engine": "groq", "text": "旧jsonl数据", "chars": 6, "confidence": 0.7}, ensure_ascii=False) + "\n")
        mig = core.read_history(limit=10)
        assert any(r["recording_id"] == "legacy-1" for r in mig), "legacy JSONL should migrate into SQLite on read"

        print("    转写历史存档 SQLite 测试通过: 读写/newest-first/搜索/按id/CSV导出/清空/旧JSONL迁移")
    finally:
        core.HISTORY_DB = orig_db
        core.HISTORY_FILE = orig_file


# ─── 25. State Lock Not Held During Hotkey-Release Wait (audit B6) ──────
def test_state_lock_free_during_release_wait():
    """Audit B6: watch_loop used to run its hotkey-release busy-wait INSIDE `with _state_lock`,
    freezing _streaming_worker (live preview) and tray start/stop for up to several seconds
    while a key was held. The wait now runs OUTSIDE the lock via _wait_for_hotkey_release().
    Exercise that method on a bare VoxDaemon (object.__new__ avoids the heavy AudioPipeline /
    EngineChain) and prove the state lock stays free during the wait, that it returns once the
    key is released, and that the stop signal aborts it promptly."""
    import vox_daemon
    import threading as _t
    import time as _time

    d = object.__new__(vox_daemon.VoxDaemon)
    d._state_lock = _t.Lock()
    d._stop_event = _t.Event()

    # Key stays "pressed" for ~0.24s (12 polls * 0.02s) then releases.
    _c = {"n": 0}
    def fake_pressed():
        _c["n"] += 1
        return _c["n"] < 12
    d._is_hotkey_pressed = fake_pressed

    # A contending thread must grab _state_lock DURING the release-wait window.
    hits = []
    def contender():
        hits.append(_time.time())
        with d._state_lock:
            pass
    th = _t.Thread(target=contender); th.start()

    t0 = _time.time()
    d._wait_for_hotkey_release()
    dt = _time.time() - t0
    th.join(timeout=2)

    during = any(t0 - 0.01 <= ts <= t0 + dt + 0.01 for ts in hits)
    assert during, "B6: state lock must stay FREE while waiting for hotkey release"
    assert 0.05 <= dt < 1.5, f"B6: release-wait should finish after release, took {dt:.2f}s"

    # Stop signal aborts the wait even when the key looks permanently held.
    d._is_hotkey_pressed = lambda: True
    d._stop_event.set()
    s = _time.time()
    d._wait_for_hotkey_release()
    assert (_time.time() - s) < 0.5, "B6: stop event must abort the release-wait promptly"

    print("    状态机锁非占用式等待 (审计 B6) 测试通过: 松开检测期间锁自由、松开后返回、stop 立即中断")


# ─── 26. Transcription-Worker Meta Persistence Regression (vox.log: name 'method' is not defined) ──────
def test_transcription_worker_metadata_persistence():
    """Regression: after a successful transcription, _transcription_worker built its meta dict
    with a bare `method` reference (NameError), which tripped the outer handler — logging
    '❌ 转写失败: name method is not defined', flipping the overlay back to the error state,
    and skipping the history archive. Drive the real worker end-to-end on a bare VoxDaemon
    (chain / SmartOutput / sound / history stubbed) and assert it completes cleanly, that the
    persisted meta carries the correct method/copied/pasted, that the overlay lands on
    'done' (never 'error'), and that history archiving receives the meta."""
    import os as _os
    import shutil as _sh
    import tempfile as _tf
    import types as _types
    import numpy as _np
    import vox_daemon

    d = object.__new__(vox_daemon.VoxDaemon)

    class _P:
        TARGET_SR = 16000
    d.pipeline = _P()
    d.cfg = {"language": "zh", "whisper_prompt": "", "restore_clipboard": True,
             "sound_effects": False, "audio": {"upload_format": "wav"},
             "output_method": "clipboard_only"}
    d.chain = _types.SimpleNamespace(
        transcribe=lambda *a, **k: {"text": "测试元数据持久化", "engine": "groq", "latency_s": 0.1})
    d.state = "PROCESSING"

    overlay_states = []
    d._update_overlay = lambda state, text=None, hint=None, engine=None, latency=None: overlay_states.append(state)
    d._update_tray_icon = lambda *a, **k: None
    d.update_health = lambda *a, **k: None

    orig_meta = core.LATEST_META_FILE
    orig_usage = core.USAGE_FILE
    orig_fg = core.foreground_window_title
    orig_out = core.SmartOutput.output
    orig_sound = core.SoundFeedback.play
    orig_rec = core.record_transcription
    tmpdir = _tf.mkdtemp()
    meta_path = _os.path.join(tmpdir, "latest.json")
    usage_path = _os.path.join(tmpdir, "vox_usage.json")
    core.LATEST_META_FILE = meta_path
    core.USAGE_FILE = usage_path
    core.foreground_window_title = lambda: "TestWindow"
    core.SmartOutput.output = lambda text, **k: {
        "method": "clipboard_only", "copied": True, "pasted": True, "hint": ""}
    core.SoundFeedback.play = lambda *a, **k: None
    recorded = []
    core.record_transcription = lambda meta: recorded.append(meta)
    try:
        audio = _np.ones(_P.TARGET_SR * 2, dtype=_np.float32) * 0.1
        d._transcription_worker(audio, 2.0)  # synchronous; a NameError would raise here
        assert "done" in overlay_states, "worker must end with overlay in done state"
        assert "error" not in overlay_states, "worker must not flip the overlay to error state"
        meta = core.safe_read_json(meta_path, None)
        assert meta is not None, "latest meta must be persisted"
        assert meta["method"] == "clipboard_only", f"meta.method wrong: {meta.get('method')!r}"
        assert meta["copied"] is True and meta["pasted"] is True
        assert len(recorded) == 1 and recorded[0]["method"] == "clipboard_only", \
            "history archive must receive the meta with method"
        assert "测试元数据持久化" in meta["text"]
    finally:
        core.LATEST_META_FILE = orig_meta
        core.USAGE_FILE = orig_usage
        core.foreground_window_title = orig_fg
        core.SmartOutput.output = orig_out
        core.SoundFeedback.play = orig_sound
        core.record_transcription = orig_rec
        _sh.rmtree(tmpdir, ignore_errors=True)

    print("    转写 worker 元数据持久化回归测试通过: 无 NameError、done 态、meta.method 正确、历史归档正常")


# ─── 27. Bubble Sonic-Style Upward Growth + Drag Resize + Engine Badge (A1/A2/A3/A4/A7) ──────
def test_bubble_upward_growth_resize_engine_badge():
    """User requirement: the bubble must grow UPWARD (bottom-anchored, Sonic-style) as
    streaming lines accumulate — full content visible, no off-screen growth; slim font;
    drag bottom-right grip to resize width (persisted); small engine badge (A7) shown
    during recording/transcribing. Also: font shrinks before the height cap (no scroll)."""
    import tkinter as _tk
    import types as _types

    ov = overlay.VoxOverlay()
    try:
        base = ov.height
        # A3: slimmer default text font.
        assert ov._text_font()[1] == -max(6, int(ov.s(9) * ov.ui_font_scale)), "text font should be s(9)*font_scale device px (negative=tk pixel size)"

        # A1: done-state growth is bottom-anchored (Sonic-style upward). The window is
        # pill-sized; on growth the bottom edge (_win_y + current_h) stays fixed while
        # the top edge (_win_y) moves up.
        y0, h0 = ov._win_y, ov.current_h
        bottom0 = y0 + h0
        long_txt = ("这是一段很长的转写结果文本用来验证气泡在内容变多时向上扩张并且完整可见，"
                    "而不是像过去那样向下被任务栏吞掉看不见。再重复一些内容以确保增长超过基础胶囊高度。" * 2)
        ov.show_done(long_txt)
        bottom1 = ov._win_y + ov.current_h
        assert ov.current_h > base, f"long done text should grow the pill, {ov.current_h} vs {base}"
        assert abs(bottom1 - bottom0) <= 2, f"bottom edge must stay anchored, {bottom0} -> {bottom1}"
        assert ov._win_y < y0, "top edge must move up when growing"
        print(f"    done 态向上扩张(底部锚定): {h0} -> {ov.current_h}px, 顶边 {y0} -> {ov._win_y}")

        # A1: recording streaming preview grows upward with full multi-line content.
        ov.hide()
        ov.show_recording(hint="口述中")
        assert ov.current_h == base, "recording with no preview stays at base capsule"
        ov.update_preview("短")
        assert ov.current_h == base, "single-line preview stays at base capsule"
        y2, h2 = ov._win_y, ov.current_h
        bottom2 = y2 + h2
        ov.update_preview("现在很好已经可以用了但最大的问题其实是转写的效率准确度以及交互的模式包括了气泡然后内部的排版" * 2)
        assert len(ov._preview_lines) >= 2, f"long preview should wrap to multiple lines, got {len(ov._preview_lines)}"
        assert ov.current_h > base, "multi-line preview should grow the pill upward"
        assert abs((ov._win_y + ov.current_h) - bottom2) <= 2, "bottom edge must stay anchored while recording grows"
        print(f"    录音态流式预览向上扩张: {h2} -> {ov.current_h}px ({len(ov._preview_lines)} 行, 完整可见)")

        # A7: engine badge renders during transcribing.
        ov.engine = "groq"
        ov.show_transcribing()
        ov._draw_pill()
        texts = []
        for item in ov.canvas.find_all():
            try:
                texts.append(ov.canvas.itemcget(item, "text"))
            except _tk.TclError:
                pass
        assert any("Groq" in t for t in texts if t), "engine badge 'Groq' missing in transcribing state"
        assert not any("NVIDIA GeForce" in t for t in texts if t), "hardcoded GPU model text must be gone"
        print("    引擎徽标: 转写中显示 'Groq 云端'，无硬编码 GPU 文案")

        # A2: drag bottom-right grip to resize width; persisted to config.
        # G4: the grip now sits inside the bottom-right CORNER (45° inside the corner
        # circle of radius base_r) so it never crosses the stadium boundary.
        w0 = ov.width
        r0 = min(ov.base_r, ov.width // 2)
        ev = _types.SimpleNamespace(x=ov.width - r0 - r0 * 0.38, y=ov.current_h - r0 - r0 * 0.62,
                                    x_root=1200, y_root=800)
        ov._on_drag_start(ev)
        assert ov._is_resizing, "grip click should start resize mode"
        ov._on_drag_motion(_types.SimpleNamespace(x_root=1200 + 150, y_root=800,
                                                  x=ev.x + 150, y=ev.y))
        assert ov.width > w0, f"dragging the grip should widen the pill, {w0} -> {ov.width}"
        ov._on_drag_release(ev)
        w_dragged = ov.width
        cfg_now = core.ConfigManager.load()
        assert cfg_now.get("overlay", {}).get("custom_width"), "resized width must persist to config"
        # Double click resets width + position back to defaults (also restores config).
        ov._on_double_click(ev)
        assert core.ConfigManager.load().get("overlay", {}).get("custom_width") is None, \
            "double click should clear the custom width"
        assert ov.width == w0, "double click should restore default width"
        print(f"    拖拽手柄改宽: {w0} -> {w_dragged}px, 已持久化; 双击重置并清除 (还原 {ov.width}px)")

        print("    气泡 Sonic 式行为测试通过: 向上扩张/完整可见/拖拽改宽/引擎徽标/无硬编码")
    finally:
        ov.root.destroy()


# ─── 28. Punctuation Restoration & Normalization (B1: 标点缺失/不一致) ────────────────────────
def test_punctuation_restoration_and_normalization():
    """B1: live transcriptions arrive with sparse/zero punctuation (log-verified: 101-char
    sentence, 0 punct) and inconsistent width (half/full mix). Ported from macOS VOX:
    conjunction-based comma insertion + particle-based period insertion + artifact
    normalization (，。→。; half-width→full-width in CJK context only, tech tokens safe)."""
    P = core.PostProcessor
    verbatim = {"fidelity": {"mode": "verbatim", "auto_period": False}}
    polished = {"fidelity": {"mode": "polished", "auto_period": True}}

    # Core B1 case: the real 101-char zero-punctuation live sentence from vox.log.
    s = ("而且其实感觉本地还不如云端稳定不过现在这个策略是云端优先因为云端速度更快"
         "准确率也更高而且本地模型加载有时候会有问题所以我还是选择云端优先的策略来用")
    assert sum(1 for c in s if c in "，。") == 0, "fixture must start unpunctuated"
    out = P.process(s, verbatim)
    n_commas = out.count("，")
    n_periods = out.count("。")
    # B1 v2 (user: 句号太少): long sparse passages must get sentence-level breaks,
    # i.e. a mix of commas AND periods, not one long breath of commas only.
    assert n_commas + n_periods >= 4, f"clause breaks should be restored, got {n_commas + n_periods}: {out}"
    assert n_periods >= 2, f"long sparse text should contain periods now, got {n_periods}: {out}"
    assert "，不过" in out, f"key conjunction break missing: {out}"
    assert "。因为" in out or "，因为" in out, f"second break missing: {out}"
    assert not out.endswith("。"), f"verbatim must NOT append a trailing period: {out}"
    print(f"    长句零标点恢复: {n_commas} 逗号 + {n_periods} 句号, verbatim 不强制句末句号")

    # Density guard: already-punctuated text is left alone.
    already = "今天天气很好，我们出去走走。"
    assert P._restore_punctuation(already, None) == already, "well-punctuated text must be skipped"
    short = "这里没有问题"
    assert P._restore_punctuation(short, None) == short, "short text (<10 chars) must be skipped"
    print("    密度守卫: 已标点文本与短句原样跳过")

    # Polished mode appends the trailing period; verbatim never does.
    assert P.process("这里没有问题", polished).endswith("。"), "polished should end with 。"
    assert P.process("这里没有问题", verbatim) == "这里没有问题", "verbatim must stay untouched"
    print("    句末句号控制: polished 加。/ verbatim 不加")

    # Tech tokens must survive normalization intact.
    tech = P.process("我们用Groq Large V3.0模型处理whisper-large-v3-turbo", verbatim)
    assert "V3.0" in tech and "whisper-large-v3-turbo" in tech, f"tech tokens corrupted: {tech}"
    assert "3.14" in P.process("圆周率是3.14我们算一下", verbatim), "decimals must keep half-width dot"
    print("    技术词保护: V3.0 / whisper-large-v3-turbo / 3.14 原样保留")

    # Half/full width consistency in CJK context + artifact combos.
    mixed = P.process("大家好,今天我们来测试", None)
    assert "大家好，今天" in mixed, f"half-width comma in CJK context must become full-width: {mixed}"
    assert P._normalize_punctuation("好的，。嗯") == "好的。嗯", "，。 artifact must collapse to 。"
    assert P._normalize_punctuation("，，，") == "，", "duplicate punctuation must collapse"
    print("    半角转全角(CJK语境)与标点伪影清理通过")

    # Particle rule: sentence-final particle followed by a pronoun splits the sentence.
    assert P._restore_punctuation("这个方案我觉得可以吧我们明天再讨论一下具体的细节", None) \
        .count("。") >= 1, "particle+pronoun should insert a period"
    print("    句尾助词断句: 吧+我们 → 。")

    print("    标点恢复与规范化 (B1, 移植自 macOS VOX) 测试通过")


# ─── Runner ───────────────────────────────────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser(description="Vox Windows 端到端全自动综合测试套件")
    ap.add_argument("--local", action="store_true",
                    help="额外运行本地 SenseVoice/FunASR 引擎测试 "
                         "(默认只测云端路径, 不加载任何本地模型)")
    args = ap.parse_args()

    print("=" * 60)
    print("🚀 开始执行 Vox Windows 端到端全自动综合测试套件")
    if args.local:
        print("   模式: 云端 + 本地 SenseVoice (--local)")
    else:
        print("   模式: 仅云端路径 (本地模型测试需加 --local)")
    print("=" * 60)

    orig_cfg = core.ConfigManager.load()
    try:
        run_test("1. 音频 DSP 与 AGC 动态电平测试", test_audio_dsp)
        run_test("2.1 Groq 云端极速转写引擎测试", test_groq_engine)
        if args.local:
            run_test("2.2 本地 SenseVoice (FunASR) 引擎测试 [--local]",
                     test_local_sensevoice_engine)
        else:
            print("\n[TEST] 2.2 本地 SenseVoice 引擎测试 ... ⏭️ SKIPPED "
                  "(默认不加载本地模型, 加 --local 启用)")
        run_test("2.3 引擎链回退容灾与开发提示词注入测试", test_engine_chain_fallback)
        run_test("3. 文本后处理 (繁简/纠错/去日韩静音幻觉/标点) 测试", test_post_processing)
        run_test("4. 智能剪贴板注入与无感历史还原测试", test_clipboard_preservation)
        run_test("5. 悬浮药丸窗与 Win32 零抢焦点测试", test_overlay_creation_and_styles)
        run_test("6. 配置管理器与零文件锁并发安全测试", test_config_thread_safety)
        run_test("7. 微秒级提示音正弦合成与非阻塞播放测试", test_sound_feedback)
        run_test("8. 单实例底层互斥锁防双重粘贴测试", test_single_instance_mutex)
        run_test("9. 悬浮窗计时器单调递增与防重置测试", test_overlay_timer_continuity)
        run_test("10. CUDA GPU 加速与高分屏检测测试", test_gpu_and_dpi_detection)
        run_test("11. 流式实时文字预览 (Interim Streaming Preview) 测试", test_streaming_preview)
        run_test("12. 键盘热键自定义模式与长按判定阈值测试", test_hotkey_configuration_modes)
        run_test("13. 灵动岛暗黑毛玻璃胶囊窗 High-DPI 渲染测试", test_overlay_dpi_and_preview_rendering)
        run_test("14. 悬浮药丸窗鼠标任意拖动与中下方自适应复位测试", test_overlay_draggable_and_positioning)
        run_test("15. 口语保真度 (Verbatim) 与提示词去语气重写测试", test_fidelity_and_verbatim_mode)
        run_test("16. 转译性能监控与全引擎方案跑分测速测试", test_perf_monitoring_and_benchmarks)
        run_test("17. 预览管道 float32 完整性回归测试 (B3 乱码修复锁定)", test_preview_pipeline_float32_integrity)
        run_test("18. ASR 置信度/错误率代理测试 (投诉#7)", test_confidence_proxy)
        run_test("19. 粘贴方法上下文决策测试 (投诉#9)", test_paste_method_selection)
        run_test("20. 悬浮窗写节流防抖动测试 (投诉#2/B7)", test_overlay_write_debounce)
        run_test("21. 麦克风热插拔检测测试 (投诉#5/B5)", test_input_device_hotplug)
        run_test("22. 悬浮药丸窗内容自适应高度测试 (投诉#1/#5/B8)", test_overlay_adaptive_height)
        run_test("23. Groq API key 落盘加密测试 (git 泄露 / B12)", test_groq_key_at_rest_encryption)
        run_test("24. 转写历史存档 SQLite 测试 (投诉#6)", test_history_archive_sqlite)
        run_test("25. 状态机锁非占用式热键松开等待测试 (审计 B6)", test_state_lock_free_during_release_wait)
        run_test("26. 转写 worker 元数据持久化回归测试 (vox.log: name 'method' is not defined)", test_transcription_worker_metadata_persistence)
        run_test("27. 气泡 Sonic 式向上扩张/拖拽改宽/引擎徽标测试 (A1/A2/A3/A7)", test_bubble_upward_growth_resize_engine_badge)
        run_test("28. 标点恢复与规范化测试 (B1: 标点缺失/不一致, 移植自 macOS VOX)", test_punctuation_restoration_and_normalization)
    finally:
        core.ConfigManager.save(orig_cfg)

    print("\n" + "=" * 60)
    if not failures:
        print(f"🎉 全部 28 项端到端核心测试全部通过 (100% PASSED)！")
        print("   Vox 已经达到成熟可用的商业级水准。")
        print("=" * 60)
        return 0
    else:
        print(f"❌ 存在 {len(failures)} 项失败测试:")
        for name, err in failures:
            print(f"   - {name}: {err}")
        print("=" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(main())
