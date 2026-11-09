<#
.SYNOPSIS
  The first real sandbox run, as ONE command. Sandbox and test money only.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\first_run.ps1
  powershell -ExecutionPolicy Bypass -File scripts\first_run.ps1 -Convert -Docs
  powershell -ExecutionPolicy Bypass -File scripts\first_run.ps1 -Convert -Deposit -Payout -Beneficiaries C:\private\bens.json
  powershell -ExecutionPolicy Bypass -File scripts\first_run.ps1 -DryRun        # prints what would run, asks for nothing

.DESCRIPTION
  1. runs the test suite and stops if anything is red;
  2. asks for the sandbox Client ID and API key (typed, hidden, kept in this process only, never written anywhere);
  3. runs scripts\sandbox_probe.py (login, balances, rate, one quote; with switches: one conversion, test funds, the
     payout leg) and saves a sanitized evidence file under evidence\;
  4. runs `python -m rate_locked check-shapes` on that file: it names every step whose answer the parsers in
     awx\wire.py could not read, which is the list of things to fix before anything else;
  5. with -Reader: also asks for the Anthropic key and runs `python -m rate_locked reader-check` (a few model calls).
  Nothing is printed or stored that contains a key. Paste back the printed tables, never the keys.
#>
param(
  [switch]$Convert,        # P7 and P9: ONE smallest conversion at the quote (test money)
  [switch]$Deposit,        # SIM: add test funds through the global account
  [switch]$Docs,           # check the public docs MCP server
  [switch]$Payout,         # stage 2, P11 to P16: beneficiary, ONE small transfer, lookup, SENT then PAID
  [string]$Beneficiaries,  # stage 2: path to your private beneficiary JSON (outside the repository)
  [switch]$Reader,         # also run the live model reader check (needs the SDK: pip install -e .[model])
  [switch]$SkipTests,
  [switch]$DryRun
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Read-Secret($prompt) {
  $s = Read-Host $prompt -AsSecureString
  return [System.Net.NetworkCredential]::new("", $s).Password
}

$probeArgs = @("scripts/sandbox_probe.py")
if ($Convert) { $probeArgs += "--convert" }
if ($Deposit) { $probeArgs += "--simulate-deposit" }
if ($Docs)    { $probeArgs += "--docs-mcp" }
if ($Payout) {
  if (-not $Beneficiaries -and -not $DryRun) { throw "-Payout needs -Beneficiaries <path to your private beneficiary JSON>. See docs/SANDBOX_RUN.md section 4." }
  $probeArgs += @("--payout", "--beneficiaries", $(if ($Beneficiaries) { $Beneficiaries } else { "<path>" }))
}

if ($DryRun) {
  Write-Host "DRY RUN: nothing is asked and nothing is called."
  if (-not $SkipTests) { Write-Host "would run: python -m pytest -q" }
  Write-Host "would run: python $($probeArgs -join ' ')"
  python @probeArgs --dry-run
  Write-Host "would run: python -m rate_locked check-shapes evidence\<newest probe file>"
  if ($Reader) { Write-Host "would run: python -m rate_locked reader-check" }
  exit 0
}

if (-not $SkipTests) {
  Write-Host "== 1. Test suite =="
  python -m pytest -q
  if ($LASTEXITCODE -ne 0) { throw "The test suite is red. Stop here." }
}

try {
  Write-Host "`n== 2. Sandbox keys (typed here, kept in this process only) =="
  $env:AIRWALLEX_CLIENT_ID = Read-Secret "Airwallex sandbox Client ID"
  $env:AIRWALLEX_API_KEY   = Read-Secret "Airwallex sandbox API key"

  Write-Host "`n== 3. Probe =="
  $before = @(Get-ChildItem evidence -Filter "probe_*.json" -ErrorAction SilentlyContinue | ForEach-Object Name)
  python @probeArgs
  $probeExit = $LASTEXITCODE
  $new = Get-ChildItem evidence -Filter "probe_*.json" -ErrorAction SilentlyContinue |
         Where-Object { $before -notcontains $_.Name } | Sort-Object LastWriteTime | Select-Object -Last 1

  if ($new) {
    Write-Host "`n== 4. Do the parsers still match what the sandbox said? =="
    python -m rate_locked check-shapes $new.FullName
  } else {
    Write-Host "`nNo evidence file was written (the probe stopped before login). Read the message above."
  }

  if ($Reader) {
    Write-Host "`n== 5. Live model reader check =="
    if (-not $env:ANTHROPIC_API_KEY) { $env:ANTHROPIC_API_KEY = Read-Secret "Anthropic API key (Enter to use a logged-in profile instead)" }
    if (-not $env:ANTHROPIC_API_KEY) { Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue }
    python -m rate_locked reader-check
  }

  Write-Host "`nDone. Keep the tables above; do not share keys. Probe exit code: $probeExit"
}
finally {
  Remove-Item Env:AIRWALLEX_CLIENT_ID, Env:AIRWALLEX_API_KEY, Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
}
