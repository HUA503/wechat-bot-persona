param([ValidateSet('start','stop')][string]$Action)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$pidPath = Join-Path $root 'bot.pid'
if ($Action -eq 'stop') {
    if (Test-Path -LiteralPath $pidPath) {
        $botPid = [int](Get-Content -LiteralPath $pidPath -Raw)
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $botPid" -ErrorAction SilentlyContinue
        if ($proc -and $proc.CommandLine -match 'bot\.py' -and $proc.CommandLine -match 'python') { & taskkill.exe /PID $botPid /T /F | Out-Null }
        Remove-Item -LiteralPath $pidPath -ErrorAction SilentlyContinue
    }
    exit
}
if (Test-Path -LiteralPath $pidPath) {
    $oldPid = [int](Get-Content -LiteralPath $pidPath -Raw)
    $old = Get-CimInstance Win32_Process -Filter "ProcessId = $oldPid" -ErrorAction SilentlyContinue
    if ($old -and $old.CommandLine -match 'bot\.py' -and $old.CommandLine -match 'python') { exit }
}
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先运行 install_env.bat' }
$env:PYTHONUTF8 = '1'
$p = Start-Process -FilePath $python -ArgumentList '-u','bot.py' -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root 'bot.log') -RedirectStandardError (Join-Path $root 'error.log')
Set-Content -LiteralPath $pidPath -Value $p.Id -Encoding ascii
