# Finals demo, in order. Run from the repo root:
#   .\scripts\demo.ps1                          # live sources (finals)
#   .\scripts\demo.ps1 -Mode fixtures           # rehearsal without Crustdata credits
#   .\scripts\demo.ps1 -Request data\requests\my_request.json -Approver "Sales Head"
#
# 1 run a request   2 show pending approvals   3 approve one
# 4 run with SERVICE_CRASH_AT=after_send (process is hard-killed mid-send)
# 5 run again (resumes; sends nothing twice)   6 outbox: one email + one invite
# 7 cost ledger
#
# Uses its own data dir (data\demo, wiped at start unless -Keep) so the outbox
# count is exactly what this demo sent.
param(
    [ValidateSet("live", "fixtures")] [string]$Mode = "live",
    [string]$Request = "data\requests\pune_mumbai_live.json",
    [string]$Approver = "Sales Head (demo)",
    [string]$DataDir = "data\demo",
    [switch]$Keep
)
$ErrorActionPreference = "Continue"   # native stderr is not fatal; exit codes are checked below
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:PYTHONPATH = "src"
$env:PYTHONIOENCODING = "utf-8"
$env:SERVICE_DATA_DIR = (Join-Path (Get-Location) $DataDir)
Remove-Item Env:SERVICE_CRASH_AT -ErrorAction SilentlyContinue

function Show-Step([string]$title) { Write-Host "`n==== $title ====" -ForegroundColor Cyan }
function Invoke-Svc { python -m service.cli @args }

if (-not $Keep -and (Test-Path $env:SERVICE_DATA_DIR)) {
    Remove-Item -Recurse -Force $env:SERVICE_DATA_DIR
}
New-Item -ItemType Directory -Force $env:SERVICE_DATA_DIR | Out-Null

if (-not (Test-Path $Request)) {
    # Default request: Pune to Mumbai lane, dated today.
    New-Item -ItemType Directory -Force (Split-Path $Request -Parent) | Out-Null
    $today = Get-Date -Format "yyyy-MM-dd"
    @{ job_id = "demo-pune-mumbai-$today"; request_date = $today
       text = "Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week" } |
        ConvertTo-Json | Set-Content -Encoding utf8 $Request
}
$req = Get-Content -Raw $Request | ConvertFrom-Json
if ($req.request) { $req = $req.request }
$jobId = $req.job_id
if (-not $jobId) { throw "Request $Request needs a job_id so the demo can show its outbox and costs." }

Show-Step "1. Run the request ($Mode sources): $Request"
Invoke-Svc run $Request --mode $Mode
if ($LASTEXITCODE -ne 0) { throw "run failed (exit $LASTEXITCODE)" }

Show-Step "2. Pending approvals"
Invoke-Svc pending
$pending = @((Invoke-Svc pending --json | Out-String | ConvertFrom-Json) | ForEach-Object { $_ } |
    Where-Object { $_.run_id -eq $jobId })
if ($pending.Count -eq 0) {
    Write-Host "No drafts are waiting for approval: see the summary above for why each prospect was held/dropped." -ForegroundColor Yellow
    Show-Step "Cost ledger"
    Invoke-Svc cost $jobId
    exit 1
}

$first = $pending[0]
Show-Step "3. Approve one: $($first.company) -> $($first.to)"
Invoke-Svc approve $first.key --by $Approver
if ($LASTEXITCODE -ne 0) { throw "approve failed (exit $LASTEXITCODE)" }

Show-Step "4. Run with SERVICE_CRASH_AT=after_send (email written, outbox not yet marked done)"
$env:SERVICE_CRASH_AT = "after_send"
Invoke-Svc run $Request --mode $Mode
$crashCode = $LASTEXITCODE
Remove-Item Env:SERVICE_CRASH_AT
if ($crashCode -ne 137) { throw "expected the injected crash (exit 137), got exit $crashCode" }
Write-Host "process died mid-send (exit $crashCode), as planned" -ForegroundColor Yellow

Show-Step "5. Run again: resumes from checkpoints, finishes the send once"
Invoke-Svc run $Request --mode $Mode
if ($LASTEXITCODE -ne 0) { throw "re-run failed (exit $LASTEXITCODE)" }

Show-Step "6. Outbox"
$outbox = Invoke-Svc outbox $jobId
$outbox | ForEach-Object { Write-Host $_ }
$counts = $outbox | Select-Object -Last 1
if ($counts -notmatch "^emails=1 invites=1 ") { throw "expected exactly one email and one invite, got: $counts" }
Write-Host "exactly one email and one invite" -ForegroundColor Green

Show-Step "7. Cost ledger"
Invoke-Svc cost $jobId
