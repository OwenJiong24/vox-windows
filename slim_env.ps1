<#
.SYNOPSIS
    Vox 依赖瘦身：保留唯一本地引擎 SenseVoice，回收约 13 GB。

.DESCRIPTION
    用户的决定：本地/离线引擎只保留 **SenseVoice (FunASR)** 一个
    （CPU 实测 RTF 0.064，即处理速度约为实时的 15 倍，中文强，模型最小）。
    另外两个本地引擎已从代码中移除：LocalEngine (faster-whisper) 与
    Qwen3ASREngine (qwen-asr)。

    本脚本做三件事：
      1. 卸载只服务于已移除引擎的包
         （faster-whisper / ctranslate2 / onnxruntime / av / qwen-asr /
           qwen-omni-utils / nagisa / gradio / gradio_client / pandas / pydub）
      2. 把 torch / torchaudio 从 CUDA 版换成 CPU 版
         （torch 4.12 GB + nvidia-* 1.96 GB  ->  CPU 版约 0.5 GB）
         SenseVoice 在 CPU 上完全可用，因此这一步不损失任何保留的能力。
      3. 删除已移除引擎的模型缓存
         （Qwen3-ASR-1.7B 4.38 GB、faster-whisper-large-v3 2.88 GB、
           faster-whisper-base 0.14 GB）

    明确保留（funasr 的依赖，删了 SenseVoice 就坏）：
      funasr, torch(CPU), torchaudio(CPU), torch-complex, transformers, tokenizers,
      modelscope, huggingface_hub, safetensors, sentencepiece, librosa, scipy,
      scikit-learn, numba, llvmlite, jieba, kaldiio, hydra-core, omegaconf,
      soundfile, soxr, numpy, sympy, networkx

    默认 **DRY RUN**，只有显式传 -Apply 才真正执行。

.PARAMETER Apply
    真正执行卸载/安装/删除。不加则只打印计划。

.PARAMETER KeepCaches
    不删除模型缓存（万一你还想留着 Qwen3-ASR 或 large-v3 的权重）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File slim_env.ps1
    powershell -ExecutionPolicy Bypass -File slim_env.ps1 -Apply
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$KeepCaches
)

$ErrorActionPreference = "Stop"
$prevEap = $ErrorActionPreference
Set-Location $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "[Vox] 找不到 .venv: $venvPy" -ForegroundColor Red
    exit 1
}

# torch CPU 版：与当前安装的主版本号保持一致，避免兼容性风险。
$torchVer = "2.11.0"
$cpuIndex = "https://download.pytorch.org/whl/cpu"

# 只服务于已移除引擎的包（依据 pip 的反向依赖分析确认无其他使用者）
$removePkgs = @(
    "faster-whisper", "ctranslate2", "onnxruntime", "av",
    "qwen-asr", "qwen-omni-utils", "nagisa",
    "gradio", "gradio_client", "pandas", "pydub"
)

# CUDA 运行时轮子（CPU 版 torch 不需要，约 2 GB）
$cudaPkgs = @(
    "nvidia-cublas-cu12", "nvidia-cuda-nvrtc-cu12", "nvidia-cudnn-cu12",
    "nvidia-cuda-runtime-cu12", "nvidia-cufft-cu12", "nvidia-curand-cu12",
    "nvidia-cusolver-cu12", "nvidia-cusparse-cu12", "nvidia-nccl-cu12",
    "nvidia-nvjitlink-cu12", "nvidia-cusparselt-cu12", "nvidia-nvtx-cu12",
    "triton"
)

# 已移除引擎的模型缓存（保留 modelscope 下的 SenseVoice）
$cacheDirs = @(
    "$env:USERPROFILE\.cache\huggingface\hub\models--Qwen--Qwen3-ASR-1.7B",
    "$env:USERPROFILE\.cache\huggingface\hub\models--Systran--faster-whisper-large-v3",
    "$env:USERPROFILE\.cache\huggingface\hub\models--Systran--faster-whisper-base"
)

function Get-DirSize($p) {
    if (-not (Test-Path $p)) { return 0 }
    return (Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object -Sum Length).Sum
}
function Invoke-Pip {
    # NOTE: the parameter is deliberately NOT named $Args — that is an automatic
    # variable in PowerShell and would shadow the pip arguments.
    param([string[]]$PipArgs)
    $ErrorActionPreference = "Continue"
    & $venvPy -m pip @PipArgs 2>&1 | ForEach-Object { Write-Host "   $_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    return $code
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Vox 依赖瘦身 — 保留 SenseVoice(CPU)，回收约 13 GB" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$venvBefore = Get-DirSize (Join-Path $PSScriptRoot ".venv")
$cacheBefore = 0
foreach ($d in $cacheDirs) { $cacheBefore += Get-DirSize $d }
Write-Host (" .venv        : {0,7:N2} GB" -f ($venvBefore / 1GB))
Write-Host (" 待删模型缓存 : {0,7:N2} GB" -f ($cacheBefore / 1GB))
Write-Host (" 合计可回收约 : {0,7:N2} GB" -f (($venvBefore + $cacheBefore) / 1GB)) -ForegroundColor Yellow

Write-Host ""
Write-Host " [1/3] 卸载已移除引擎专属依赖:" -ForegroundColor Yellow
foreach ($p in $removePkgs) { Write-Host "        - $p" }

Write-Host ""
Write-Host " [2/3] torch/torchaudio 换成 CPU 版:" -ForegroundColor Yellow
Write-Host "        - 卸载 torch / torchaudio (当前 cu128) 与 nvidia-* CUDA 轮子"
Write-Host "        + 安装 torch==$torchVer+cpu torchaudio==$torchVer+cpu  (来源: $cpuIndex)"

Write-Host ""
if ($KeepCaches) {
    Write-Host " [3/3] 模型缓存: 按 -KeepCaches 保留，不删除" -ForegroundColor DarkGray
} else {
    Write-Host " [3/3] 删除已移除引擎的模型缓存:" -ForegroundColor Yellow
    foreach ($d in $cacheDirs) {
        if (Test-Path $d) {
            Write-Host ("        - {0}  ({1:N2} GB)" -f (Split-Path $d -Leaf), ((Get-DirSize $d)/1GB))
        }
    }
}

Write-Host ""
Write-Host " 保留: funasr + SenseVoice 模型 + 其全部依赖(transformers/modelscope/" -ForegroundColor DarkGray
Write-Host "       librosa/scipy/torch-complex 等) + 云端引擎(groq/requests/soundfile)" -ForegroundColor DarkGray

if (-not $Apply) {
    Write-Host ""
    Write-Host " DRY RUN — 未做任何改动。" -ForegroundColor Cyan
    Write-Host " 执行: powershell -ExecutionPolicy Bypass -File slim_env.ps1 -Apply" -ForegroundColor White
    Write-Host ""
    exit 0
}

Write-Host ""
Write-Host " 正在卸载..." -ForegroundColor Yellow
$null = Invoke-Pip (@("uninstall", "-y") + $removePkgs)

Write-Host ""
Write-Host " 正在移除 CUDA 轮子与旧 torch..." -ForegroundColor Yellow
$null = Invoke-Pip (@("uninstall", "-y", "torch", "torchaudio") + $cudaPkgs)

Write-Host ""
Write-Host " 正在安装 CPU 版 torch..." -ForegroundColor Yellow
$code = Invoke-Pip @("install", "--no-cache-dir",
                     "torch==$torchVer+cpu", "torchaudio==$torchVer+cpu",
                     "--index-url", $cpuIndex)
if ($code -ne 0) {
    Write-Host "[Vox] torch CPU 安装失败。恢复命令:" -ForegroundColor Red
    Write-Host "   .venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor White
    exit 1
}

if (-not $KeepCaches) {
    Write-Host ""
    Write-Host " 正在删除模型缓存..." -ForegroundColor Yellow
    foreach ($d in $cacheDirs) {
        if (Test-Path $d) {
            Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue
            Write-Host "   removed: $(Split-Path $d -Leaf)"
        }
    }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " 验证：SenseVoice 必须在 CPU 上仍然可用" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$verify = @'
import sys, os, time
sys.path.insert(0, os.getcwd())
import vox_core as core
import vox_win32

# 1) 剪贴板后端
assert vox_win32.available(), "clipboard backend unavailable"
print("  [OK] 剪贴板后端 =", vox_win32.status()["backend"])

# 2) 云端路径依赖
for m in ("numpy", "sounddevice", "keyboard", "soundfile", "requests", "groq"):
    __import__(m)
print("  [OK] 云端路径依赖齐全")

# 3) torch 必须是 CPU 版
import torch
assert torch.version.cuda is None, f"torch 仍是 CUDA 版: {torch.version.cuda}"
assert not torch.cuda.is_available(), "torch 仍报告 CUDA 可用"
print("  [OK] torch =", torch.__version__, "(CPU 版)")

# 4) 已移除的引擎必须不存在
for name in ("LocalEngine", "Qwen3ASREngine"):
    assert not hasattr(core, name), f"{name} 仍可访问"
print("  [OK] LocalEngine / Qwen3ASREngine 已移除")

# 5) SenseVoice 真的能在 CPU 上转写
sample = os.path.join(core.SCRIPT_DIR, "tts_test.wav.flac")
assert os.path.exists(sample), f"缺少测试样音 {sample}"
t0 = time.perf_counter()
eng = core.SenseVoiceEngine(device="cpu")
eng._get_model()
text, lat, _ = eng.transcribe(sample, language="zh")
text = (text or "").strip()
assert text, "SenseVoice 返回空文本"
print(f"  [OK] SenseVoice CPU 转写: {lat:.3f}s  {text[:60]!r}")

# 6) 云端优先级下不应有任何 GPU/CUDA 探测
cfg = core.ConfigManager.load()
g = core.gpu_backed_engines(cfg)
print("  [OK] gpu_backed_engines =", g)
print("VERIFY: PASS")
'@

$verifyFile = Join-Path $PSScriptRoot "_slim_verify.py"
Set-Content -LiteralPath $verifyFile -Value $verify -Encoding UTF8
$ErrorActionPreference = "Continue"
& $venvPy $verifyFile
$vcode = $LASTEXITCODE
$ErrorActionPreference = $prevEap
Remove-Item $verifyFile -Force -ErrorAction SilentlyContinue

$venvAfter = Get-DirSize (Join-Path $PSScriptRoot ".venv")
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host (" .venv        : {0:N2} GB  ->  {1:N2} GB" -f ($venvBefore/1GB), ($venvAfter/1GB))
Write-Host (" 模型缓存     : {0:N2} GB  ->  {1:N2} GB" -f ($cacheBefore/1GB), (((Get-DirSize $cacheDirs[0]) + (Get-DirSize $cacheDirs[1]) + (Get-DirSize $cacheDirs[2]))/1GB))
Write-Host (" 合计回收     : {0:N2} GB" -f (($venvBefore - $venvAfter + $cacheBefore)/1GB)) -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan

if ($vcode -ne 0) {
    Write-Host " ⚠️ 验证未通过。恢复依赖:" -ForegroundColor Red
    Write-Host "   .venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor White
    exit 1
}
Write-Host " 验证通过 — SenseVoice 在 CPU 上可用，云端路径完好。" -ForegroundColor Green
Write-Host ""
