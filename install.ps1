# Claude 사용량 위젯 설치 스크립트 (Windows PowerShell 5.1+)
# 1) 파이썬 패키지 설치  2) 훅 스크립트를 ~/.claude 에 복사(경로 치환)
# 3) settings.json 에 붙여넣을 훅 설정을 출력
$ErrorActionPreference = "Stop"

# `irm ... | iex` 로 실행되면 스크립트 경로가 없다 — 저장소를 직접 내려받는다.
$scriptPath = $MyInvocation.MyCommand.Path
if ($scriptPath) { $repo = Split-Path -Parent $scriptPath } else { $repo = $null }
if (-not $repo -or -not (Test-Path (Join-Path $repo "ClaudeUsageWidget.pyw"))) {
    $repo = Join-Path $env:LOCALAPPDATA "claude-taskbar-widget"
    Write-Host "[0/3] 위젯 다운로드 -> $repo"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $zip = Join-Path $env:TEMP "claude-taskbar-widget.zip"
    Invoke-WebRequest "https://github.com/KimJinWooDa/claude-taskbar-widget/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
    $tmp = Join-Path $env:TEMP ("ctw-" + [guid]::NewGuid().ToString("N"))
    Expand-Archive $zip -DestinationPath $tmp -Force
    if (Test-Path $repo) { Remove-Item $repo -Recurse -Force }
    Move-Item (Join-Path $tmp "claude-taskbar-widget-main") $repo
    Remove-Item $zip -Force
    Remove-Item $tmp -Recurse -Force
}
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
Write-Host "완료. 지금 바로 켜려면: $(Join-Path $repo 'run-widget.vbs') 더블클릭" -ForegroundColor Green
