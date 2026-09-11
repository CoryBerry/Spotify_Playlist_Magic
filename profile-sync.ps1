# Off-machine sync of the personal curation overlay (profile/).
#
# 1) Dumps the app DB to profile/backups/spotify_tools.sql via `python cli.py backup` —
#    SQL text, not a binary copy, so the overlay's git history stays readable and small.
#    The regenerable playlist_cache table is excluded by the dump itself.
# 2) Mirrors this project's agent memory (~/.claude/projects/<slug>/memory) into
#    profile/memory/, and the profile skill's friend-profile pulls into profile/cache/.
#    Mirrors, not moves: ~/.claude stays authoritative, this is purely a backup.
# 3) Commits to the private overlay repo *only if something changed*, then pushes.
#
# Safe to run on a schedule — a no-change run exits 0 without committing.
# Native git exit codes are checked explicitly rather than via ErrorActionPreference,
# because PowerShell 5.1 treats git's normal stderr progress as a terminating error.
#
#   .\profile-sync.ps1            # dump, mirror, commit, push
#   .\profile-sync.ps1 -NoPush    # local commit only

param(
    [switch]$NoPush,
    [switch]$Quiet
)

$repo       = $PSScriptRoot                        # repo root (this script lives there)
$profileDir = Join-Path $repo 'profile'            # NB: not $profile — a reserved PS variable
$log        = Join-Path $repo 'instance\profile-sync.log'

function Log($msg) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    if (Test-Path -LiteralPath (Split-Path $log)) { Add-Content -Path $log -Value "$ts  $msg" -Encoding utf8 }
    if (-not $Quiet) { Write-Host $msg }
}

if (-not (Test-Path -LiteralPath $profileDir)) {
    Write-Error "No profile/ overlay found. Copy the template first:  cp -r profile.sample profile"
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $profileDir '.git'))) {
    Write-Error "profile/ exists but is not a git repo. See profile.sample/README.md."
    exit 1
}

Set-Location $repo

# --- 1) Dump the DB -------------------------------------------------------
$env:PYTHONUTF8 = '1'
$out = & python cli.py backup 2>&1
if ($LASTEXITCODE -ne 0) { Log "ERROR db dump: $out"; exit 1 }

# --- 2) Mirror agent memory + the friend-profile cache --------------------
# robocopy /MIR propagates deletions too; its exit codes 0-7 all mean success.
$slug   = 'C--dev-crate'
$memSrc = Join-Path $HOME ".claude\projects\$slug\memory"
if (Test-Path -LiteralPath $memSrc) {
    robocopy $memSrc (Join-Path $profileDir 'memory') /MIR /XF .gitkeep /NJH /NJS /NDL /NFL /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Log "ERROR robocopy memory: exit $LASTEXITCODE"; exit 1 }
}

$cacheSrc = Join-Path $repo '.profile_cache'
if (Test-Path -LiteralPath $cacheSrc) {
    robocopy $cacheSrc (Join-Path $profileDir 'cache') /MIR /XF .gitkeep /NJH /NJS /NDL /NFL /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Log "ERROR robocopy cache: exit $LASTEXITCODE"; exit 1 }
}

# Personal notes live in the overlay but are edited at the repo root, so copy them across.
foreach ($f in 'TODO.md', 'IDEAS.md', 'ISSUES.md') {
    $src = Join-Path $repo $f
    if (Test-Path -LiteralPath $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $profileDir "notes\$f") -Force
    }
}

# --- 3) Commit only on change, then push ----------------------------------
git -C $profileDir add -A
git -C $profileDir diff --cached --quiet
if ($LASTEXITCODE -eq 0) { Log 'no change - nothing to sync'; exit 0 }

$summary = (git -C $profileDir diff --cached --numstat | Measure-Object).Count
$stamp   = Get-Date -Format 'yyyy-MM-dd HH:mm'
$out = git -C $profileDir commit -m "sync $stamp ($summary files)" 2>&1
if ($LASTEXITCODE -ne 0) { Log "ERROR commit: $out"; exit 1 }

if ($NoPush) { Log "committed sync $stamp ($summary files) - push skipped"; exit 0 }

$out = git -C $profileDir push 2>&1
if ($LASTEXITCODE -ne 0) { Log "ERROR push: $out"; exit 1 }

Log "committed + pushed sync $stamp ($summary files)"
