$Host.UI.RawUI.WindowTitle = 'Claude Max - 官方账号授权'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
& python (Join-Path $PSScriptRoot 'accounts.py') enroll
Write-Host "`n授权窗口执行结果: $LASTEXITCODE"
Read-Host '按 Enter 关闭此窗口'
