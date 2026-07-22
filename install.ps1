# Claude 사용량 위젯 설치 스크립트 (Windows PowerShell 5.1+)
# 1) 파이썬 패키지 설치  2) 훅 스크립트를 ~/.claude 에 복사(경로 치환)
# 3) settings.json 에 붙여넣을 훅 설정을 출력
$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$widget = Join-Path $repo "ClaudeUsageWidget.pyw"
$claudeDir = Join-Path $env:USERPROFILE ".claude"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "python 을 찾을 수 없습니다. Python 3.10+ 를 설치하고 PATH에 추가하세요." -ForegroundColor Red
    exit 1
}

Write-Host "[1/3] 파이썬 패키지 설치 (pystray, Pillow)..."
python -m pip install -r (Join-Path $repo "requirements.txt") --quiet

Write-Host "[2/3] 훅 스크립트 복사 -> $claudeDir"
if (-not (Test-Path $claudeDir)) { New-Item -ItemType Directory $claudeDir | Out-Null }
$start = Get-Content (Join-Path $repo "hooks\start-usage-widget.py") -Raw -Encoding UTF8
$start = $start.Replace("__WIDGET_PATH__", $widget)
[IO.File]::WriteAllText((Join-Path $claudeDir "start-usage-widget.py"), $start, (New-Object Text.UTF8Encoding $false))
Copy-Item (Join-Path $repo "hooks\usage-hook.py") (Join-Path $claudeDir "usage-hook.py") -Force

Write-Host "[3/3] 아래 내용을 $claudeDir\settings.json 의 hooks 에 추가하세요:" -ForegroundColor Yellow
@"
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "python \"$($claudeDir -replace '\\','/')/start-usage-widget.py\"", "timeout": 15, "async": true } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "python \"$($claudeDir -replace '\\','/')/usage-hook.py\"", "timeout": 15, "async": true } ] }
    ]
  }
"@ | Write-Host

Write-Host ""
Write-Host "완료. 지금 바로 켜려면: run-widget.vbs 더블클릭" -ForegroundColor Green
