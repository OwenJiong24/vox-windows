#!/usr/bin/env python3
"""
Vox Microphone Calibrator — Rapidly pin the active, highest-quality microphone.
"""

import os
import sys
import time
import json
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sounddevice as sd
import vox_core as core

PROBE_SECONDS = 0.6
MIN_ACCEPTABLE_RMS = 0.002


def list_devices():
    pipe = core.AudioPipeline()
    devs = pipe.list_input_devices(force_refresh=True)
    print("\n=== 可用音频输入设备列表 ===")
    for d in devs:
        print(f"[{d['index']:2d}] [{d['hostapi']:18s}] {d['friendly_name']}")
    if not devs:
        print("(未检测到输入设备)")
    return devs


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        list_devices()
        return 0

    cfg = core.ConfigManager.load()
    pipe = core.AudioPipeline()
    devs = pipe.list_input_devices(force_refresh=True)
    if not devs:
        print("未检测到任何输入设备。")
        return 1

    print(f"\n检测到 {len(devs)} 个输入通道。")
    print("准备开始校准 —— 请现在【正常对着麦克风说话】(持续约 3~5 秒)...")
    for i in (3, 2, 1):
        print(f"  {i}...", flush=True)
        time.sleep(1)

    results = []
    # Test candidates (prioritize WASAPI / DirectSound / USB)
    for d in devs:
        idx = d["index"]
        sr = d.get("native_sr", 16000)
        try:
            data = sd.rec(int(sr * PROBE_SECONDS), samplerate=sr, channels=1, dtype="float32", device=idx, blocking=True)
            a = data.ravel().astype("float32")
            rms = float(np.sqrt(np.mean(a ** 2)))
            peak = float(np.max(np.abs(a))) if a.size else 0.0
            results.append((rms, idx, d["friendly_name"], peak, d["hostapi"]))
            mark = "  <== 发现有效声音信号!" if rms > MIN_ACCEPTABLE_RMS else ""
            print(f"[{idx:2d}] RMS={rms:7.5f} 峰值={peak:6.4f} | {d['friendly_name']}{mark}", flush=True)
        except Exception as e:
            pass

    if not results:
        print("\n未能从任何设备获取音频。")
        return 1

    results.sort(key=lambda r: r[0], reverse=True)
    best_rms, best_idx, best_name, best_peak, best_api = results[0]

    if best_rms < MIN_ACCEPTABLE_RMS:
        print(f"\n⚠️ 所有设备探测电平偏弱 (最高 RMS={best_rms:.5f} < {MIN_ACCEPTABLE_RMS})。")
        print("建议: 离麦克风近一点重试，或在管理界面手动指定设备。")
        return 2

    # Save to config by friendly name
    cfg.setdefault("audio", {})["preferred_device"] = best_name
    core.ConfigManager.save(cfg)

    print(f"\n✅ 已自动锁定最佳麦克风: \"{best_name}\"")
    print(f"   设备通道: [{best_idx}] ({best_api}) | 信号 RMS: {best_rms:.5f} | 峰值: {best_peak:.4f}")
    print("   已通过内存信号通知守护进程实时生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
