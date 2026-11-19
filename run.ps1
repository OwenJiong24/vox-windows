$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[Vox] 未检测到本地虚拟环境，正在为您创建 .venv..." -ForegroundColor Cyan
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[Vox] ❌ 创建虚拟环境失败，请确保已安装 Python 3.10+" -ForegroundColor Red
        Read-Host "按回车键退出..."
        exit 1
    }
    Write-Host "[Vox] 正在安装必要运行依赖包..." -ForegroundColor Cyan
    & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
}

# Prefer the venv: it is where requirements.txt was installed. Preferring a global
# interpreter here is what silently broke the clipboard/paste path.
$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
& $py check_env.py $args
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n[Vox] ⚠️ 启动过程中检测到异常，窗口已保持打开供排查。" -ForegroundColor Yellow
    Read-Host "按回车键退出..."
}
