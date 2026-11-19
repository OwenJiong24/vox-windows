$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[Vox] ❌ 未检测到 .venv 运行环境，请先运行 run.bat 或 run.ps1 完成初始化。" -ForegroundColor Red
    Read-Host "按回车键退出..."
    exit 1
}

Write-Host "[Vox] 正在启动可视化管理控制台..." -ForegroundColor Green
$pyw = if (Test-Path ".venv\Scripts\pythonw.exe") { ".venv\Scripts\pythonw.exe" } else { "pythonw" }
Start-Process -FilePath $pyw -ArgumentList "vox_gui.py" -WorkingDirectory $PSScriptRoot
