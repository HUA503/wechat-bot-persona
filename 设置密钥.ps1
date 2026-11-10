$ErrorActionPreference = 'Stop'
$secret = Read-Host '请输入中转站 API 密钥' -AsSecureString
$secret | ConvertFrom-SecureString | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'secret.txt') -Encoding ascii
Write-Host '已加密保存，仅当前 Windows 用户可解密。'
Read-Host '按回车退出'
