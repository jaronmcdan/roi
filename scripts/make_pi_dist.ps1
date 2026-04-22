param(
    [switch]$Offline,
    [string]$Python = "python",
    [switch]$Deploy,
    [string]$DeployHost = "192.168.45.1",
    [string]$DeployUser = "",
    [string]$DeployPath = "/tmp",
    [switch]$InstallOnPi,
    [switch]$RebootAfterInstall,
    [string]$InstallPrefix = "/opt/roi",
    [string]$InstallWorkDir = "/tmp/roi-deploy",
    [string]$PiPlatform = "manylinux2014_aarch64",
    [string]$PiPythonVersion = "auto",
    [string]$PiAbi = "",
    [switch]$Help
)

$ErrorActionPreference = "Stop"

function Show-Usage {
    @"
Usage: .\scripts\make_pi_dist.ps1 [-Offline] [-Python <python-exe>] [-Deploy] [-DeployHost <host>] [-DeployUser <user>] [-DeployPath <remote-dir>] [-InstallOnPi] [-RebootAfterInstall] [-InstallPrefix <prefix>] [-InstallWorkDir <dir>] [-PiPlatform <tag>] [-PiPythonVersion <ver>] [-PiAbi <abi>]

Options:
  -Offline               Bundle Python wheels/sdists for offline Pi install
  -Python <python-exe>   Python interpreter used to download/build artifacts
  -Deploy                Upload the built tarball to a Pi via SCP (PuTTY pscp)
  -DeployHost            Target host/IP (default: 192.168.45.1)
  -DeployUser            SSH username (prompted if omitted)
  -DeployPath            Remote directory path (default: /tmp)
  -InstallOnPi           After upload, install ROI offline, hardcode by-id env, and install/start service
  -RebootAfterInstall    Reboot Pi after successful install (requires -InstallOnPi)
  -InstallPrefix         Install prefix passed to pi_install.sh (default: /opt/roi)
  -InstallWorkDir        Remote temp dir used for extract/install (default: /tmp/roi-deploy)
  -PiPlatform            Target wheel platform for offline bundle (default: manylinux2014_aarch64)
  -PiPythonVersion       Target Python version (auto, 3.13, or 313; default: auto=3.10..3.13)
  -PiAbi                 Target ABI tag (default derived from version, e.g. cp311)

Examples:
  .\scripts\make_pi_dist.ps1
  .\scripts\make_pi_dist.ps1 -Offline
  .\scripts\make_pi_dist.ps1 -Offline -Python py
  .\scripts\make_pi_dist.ps1 -Offline -Deploy
  .\scripts\make_pi_dist.ps1 -Offline -Deploy -InstallOnPi
  .\scripts\make_pi_dist.ps1 -Offline -Deploy -InstallOnPi -RebootAfterInstall
  .\scripts\make_pi_dist.ps1 -Offline -PiPythonVersion 3.13
  .\scripts\make_pi_dist.ps1 -Offline -PiPlatform manylinux2014_armv7l -PiPythonVersion 3.11
  .\scripts\make_pi_dist.ps1 -Offline -Deploy -DeployHost 192.168.45.1 -DeployUser pete -DeployPath /tmp
"@
}

if ($PSBoundParameters.ContainsKey("Help")) {
    Show-Usage
    exit 0
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$distDir = Join-Path $root "dist"
New-Item -ItemType Directory -Force -Path $distDir | Out-Null

function Test-CommandExists([string]$Name) {
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Convert-FileToLf([string]$Path) {
    $raw = [System.IO.File]::ReadAllText($Path)
    $normalized = $raw.Replace("`r`n", "`n").Replace("`r", "`n")
    if ($normalized -ne $raw) {
        [System.IO.File]::WriteAllText($Path, $normalized, [System.Text.UTF8Encoding]::new($false))
    }
}

function ConvertTo-PlainText([SecureString]$Value) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function Resolve-PscpPath {
    $cmd = Get-Command pscp -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    $candidates = @(
        (Join-Path ${env:ProgramFiles} "PuTTY\pscp.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "PuTTY\pscp.exe")
    )
    foreach ($cand in $candidates) {
        if ($cand -and (Test-Path -LiteralPath $cand)) {
            return $cand
        }
    }
    return $null
}

function Resolve-PlinkPath {
    $cmd = Get-Command plink -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    $candidates = @(
        (Join-Path ${env:ProgramFiles} "PuTTY\plink.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "PuTTY\plink.exe")
    )
    foreach ($cand in $candidates) {
        if ($cand -and (Test-Path -LiteralPath $cand)) {
            return $cand
        }
    }
    return $null
}

function Test-GitTreeDirty([string]$RepoPath) {
    & git -C $RepoPath diff --quiet
    $unstagedDirty = ($LASTEXITCODE -ne 0)
    & git -C $RepoPath diff --cached --quiet
    $stagedDirty = ($LASTEXITCODE -ne 0)
    return ($unstagedDirty -or $stagedDirty)
}

if ((Test-CommandExists "git")) {
    & git -C $root rev-parse --is-inside-work-tree *> $null
    if ($LASTEXITCODE -eq 0) {
        $sha = (& git -C $root rev-parse --short HEAD).Trim()
        $dirty = if (Test-GitTreeDirty $root) { "-dirty" } else { "" }
        $ver = "$sha$dirty"
    } else {
        $ver = (Get-Date -Format "yyyyMMdd-HHmmss")
    }
} else {
    $ver = (Get-Date -Format "yyyyMMdd-HHmmss")
}

if ($RebootAfterInstall -and -not $InstallOnPi) {
    throw "-RebootAfterInstall requires -InstallOnPi."
}
if ($InstallOnPi) {
    $Deploy = $true
    if (-not $Offline) {
        throw "-InstallOnPi currently requires -Offline so the wheelhouse is bundled."
    }
}

$PiPythonTags = @()
$PiVersionModeAuto = $false
if ($PiPythonVersion.Trim().ToLowerInvariant() -eq "auto") {
    $PiVersionModeAuto = $true
    # Build a wheelhouse that works across common Pi Python versions.
    $PiPythonTags = @("310", "311", "312", "313")
    if (-not [string]::IsNullOrWhiteSpace($PiAbi)) {
        throw "-PiAbi cannot be used with -PiPythonVersion auto."
    }
}
elseif ($PiPythonVersion -match '^\d+\.\d+$') {
    $PiPythonTags = @($PiPythonVersion.Replace(".", ""))
}
elseif ($PiPythonVersion -match '^\d+$') {
    $PiPythonTags = @($PiPythonVersion)
}
else {
    throw "Invalid -PiPythonVersion '$PiPythonVersion'. Use auto, 3.11, or 311."
}

$out = Join-Path $distDir "roi-$ver.tar.gz"
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("roi-dist-" + [Guid]::NewGuid().ToString("N"))
$stage = Join-Path $tmp "roi"

try {
    New-Item -ItemType Directory -Force -Path $stage | Out-Null

    Get-ChildItem -LiteralPath $root -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $stage -Recurse -Force
    }

    $removeDirNames = @(
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        "wheelhouse"
    )

    Get-ChildItem -LiteralPath $stage -Recurse -Directory -Force |
        Where-Object { $removeDirNames -contains $_.Name } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

    Get-ChildItem -LiteralPath $stage -Recurse -File -Force -Filter "*.pyc" |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }

    # Ensure shell scripts remain Linux-compatible when packaging from Windows.
    Get-ChildItem -LiteralPath $stage -Recurse -File -Filter "*.sh" |
        ForEach-Object { Convert-FileToLf -Path $_.FullName }

    if ($Offline) {
        if (-not (Test-CommandExists $Python)) {
            throw "Python interpreter not found: '$Python'."
        }

        $wheelhouse = Join-Path $stage "deploy\wheelhouse"
        New-Item -ItemType Directory -Force -Path $wheelhouse | Out-Null

        Write-Host "[ROI] Building offline wheelhouse with $Python"
        if ($PiVersionModeAuto) {
            Write-Host "[ROI] Target platform: $PiPlatform ; Python: auto ($($PiPythonTags -join ', '))"
        }
        else {
            $displayAbi = if ([string]::IsNullOrWhiteSpace($PiAbi)) { "cp$($PiPythonTags[0])" } else { $PiAbi }
            Write-Host "[ROI] Target platform: $PiPlatform ; Python: $($PiPythonTags[0]) ; ABI: $displayAbi"
        }
        & $Python -m pip --disable-pip-version-check download --dest $wheelhouse pip setuptools wheel
        if ($LASTEXITCODE -ne 0) { throw "pip download failed." }

        # Build ROI wheel once on the PC so Pi install does not need to build ROI.
        & $Python -m pip --disable-pip-version-check wheel --wheel-dir $wheelhouse --no-deps $root
        if ($LASTEXITCODE -ne 0) { throw "Failed to build ROI wheel." }

        # Extract runtime dependencies from pyproject.toml, then fetch Linux-target wheels.
        $requirementsFile = Join-Path $tmp "roi-runtime-requirements.txt"
        $pyprojectPath = Join-Path $root "pyproject.toml"
        $depExtractScript = Join-Path $tmp "extract_runtime_deps.py"
        $depExtractBody = @'
import pathlib
import sys

try:
    import tomllib as _toml
except Exception:
    import tomli as _toml

p = pathlib.Path(sys.argv[1])
data = _toml.loads(p.read_text(encoding="utf-8"))
for dep in data.get("project", {}).get("dependencies", []):
    print(dep)
'@
        [System.IO.File]::WriteAllText($depExtractScript, $depExtractBody, [System.Text.UTF8Encoding]::new($false))
        $depLines = & $Python $depExtractScript $pyprojectPath
        if ($LASTEXITCODE -ne 0) { throw "Failed to read runtime dependencies from pyproject.toml." }
        $depLines = @($depLines) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        if (-not $depLines -or $depLines.Count -eq 0) { throw "No runtime dependencies found in pyproject.toml." }

        [System.IO.File]::WriteAllLines($requirementsFile, $depLines, [System.Text.UTF8Encoding]::new($false))
        $anyWheelDownloadSucceeded = $false
        foreach ($pyTag in $PiPythonTags) {
            $abiTag = if (-not [string]::IsNullOrWhiteSpace($PiAbi) -and $PiPythonTags.Count -eq 1) { $PiAbi } else { "cp$pyTag" }
            Write-Host "[ROI] Downloading dependency wheels for cp$pyTag / $abiTag"
            $downloadArgs = @(
                "-m", "pip", "--disable-pip-version-check", "download",
                "--dest", $wheelhouse,
                "--only-binary=:all:",
                "--platform", $PiPlatform,
                "--implementation", "cp",
                "--python-version", $pyTag,
                "--abi", $abiTag,
                "-r", $requirementsFile
            )
            & $Python @downloadArgs
            if ($LASTEXITCODE -eq 0) {
                $anyWheelDownloadSucceeded = $true
            }
            elseif (-not $PiVersionModeAuto) {
                throw "pip dependency wheel download failed for Python $pyTag ($abiTag). Try adjusting -PiPlatform / -PiPythonVersion / -PiAbi."
            }
            else {
                Write-Host "[ROI] WARNING: dependency wheel download failed for cp$pyTag ($abiTag); continuing."
            }
        }
        if (-not $anyWheelDownloadSucceeded) {
            throw "Unable to download dependency wheels for any target Python version. Try setting explicit -PiPlatform / -PiPythonVersion."
        }
    }

    if (-not (Test-CommandExists "tar")) {
        throw "tar.exe was not found on PATH."
    }

    if (Test-Path -LiteralPath $out) {
        Remove-Item -LiteralPath $out -Force
    }

    & tar -czf $out -C $tmp "roi"
    if ($LASTEXITCODE -ne 0) { throw "tar packaging failed." }

    Write-Host "Built: $out"
    if ($Offline) {
        Write-Host "Includes offline wheelhouse at: deploy/wheelhouse/"
    }

    if ($Deploy) {
        if ([string]::IsNullOrWhiteSpace($DeployHost)) {
            throw "Deploy host is empty. Set -DeployHost."
        }
        if ([string]::IsNullOrWhiteSpace($DeployUser)) {
            $DeployUser = Read-Host "Pi SSH username"
        }
        if ([string]::IsNullOrWhiteSpace($DeployUser)) {
            throw "Deploy user is empty."
        }

        $pscp = Resolve-PscpPath
        if (-not $pscp) {
            throw "pscp.exe not found. Install PuTTY or add pscp to PATH."
        }

        $securePassword = Read-Host "Password for $DeployUser@$DeployHost" -AsSecureString
        $plainPassword = ConvertTo-PlainText -Value $securePassword
        if ([string]::IsNullOrWhiteSpace($plainPassword)) {
            throw "Empty password entered. Re-run and enter a non-empty password (input is hidden)."
        }
        try {
            Write-Host "[ROI] Uploading package to $DeployUser@${DeployHost}:$DeployPath"
            $pscpArgs = @(
                "-scp",
                "-pw", $plainPassword,
                $out,
                "$DeployUser@${DeployHost}:$DeployPath/"
            )
            & $pscp @pscpArgs
            if ($LASTEXITCODE -ne 0) {
                throw "pscp upload failed."
            }
            Write-Host "[ROI] Upload complete: $DeployUser@${DeployHost}:$DeployPath/$(Split-Path -Leaf $out)"

            if ($InstallOnPi) {
                $plink = Resolve-PlinkPath
                if (-not $plink) {
                    throw "plink.exe not found. Install PuTTY or add plink to PATH."
                }

                $tarName = Split-Path -Leaf $out
                $remoteTar = "$DeployPath/$tarName"
                $rootScript = @'
set -euo pipefail
WORKDIR='__WORKDIR__'
TARBALL='__TARBALL__'
PREFIX='__PREFIX__'
rm -rf "$WORKDIR"
mkdir -p "$WORKDIR"
tar -xzf "$TARBALL" -C "$WORKDIR"
cd "$WORKDIR/roi"
bash scripts/pi_install.sh --offline --prefix "$PREFIX"
# Build a stable by-id env and install/start the service automatically.
"$PREFIX/.venv/bin/roi-env-hardcode" --apply --verify-serial
bash "$PREFIX/scripts/service_install.sh" --prefix "$PREFIX" --enable --start
'@
                $rootScript = $rootScript.Replace("__WORKDIR__", $InstallWorkDir)
                $rootScript = $rootScript.Replace("__TARBALL__", $remoteTar)
                $rootScript = $rootScript.Replace("__PREFIX__", $InstallPrefix)
                $rootScript += "`n"
                if ($RebootAfterInstall) {
                    $rootScript += @'
echo "[ROI] Scheduling reboot in 2 seconds"
(sleep 2; reboot) >/dev/null 2>&1 &
'@
                }
                else {
                    $rootScript += @'
systemctl status roi --no-pager || true
'@
                }

                Write-Host "[ROI] Running remote install on $DeployUser@${DeployHost}"
                # Normalize to LF only and upload as a script file so PowerShell
                # does not transform line endings while piping to plink.
                $rootScript = $rootScript.Replace("`r", "")
                $localInstallScript = Join-Path $tmp "roi-remote-install.sh"
                [System.IO.File]::WriteAllText($localInstallScript, $rootScript, [System.Text.UTF8Encoding]::new($false))

                $remoteInstallScript = "$DeployPath/roi-remote-install.sh"
                Write-Host "[ROI] Uploading remote install script to $DeployUser@${DeployHost}:$remoteInstallScript"
                $pscpScriptArgs = @(
                    "-scp",
                    "-pw", $plainPassword,
                    $localInstallScript,
                    "$DeployUser@${DeployHost}:$remoteInstallScript"
                )
                & $pscp @pscpScriptArgs
                if ($LASTEXITCODE -ne 0) {
                    throw "pscp upload failed (install script)."
                }

                $stdinPayload = "$plainPassword`n"
                $remoteCmd = "chmod 700 '$remoteInstallScript' && sudo -S -p '' bash '$remoteInstallScript' && rm -f '$remoteInstallScript'"
                $plinkArgs = @(
                    "-ssh",
                    "-pw", $plainPassword,
                    "$DeployUser@${DeployHost}",
                    $remoteCmd
                )
                $stdinPayload | & $plink @plinkArgs
                if ($LASTEXITCODE -ne 0) {
                    throw "Remote install failed."
                }
                if ($RebootAfterInstall) {
                    Write-Host "[ROI] Install+autodetect+service complete. Pi reboot initiated."
                }
                else {
                    Write-Host "[ROI] Install+autodetect+service complete."
                }
            }
        }
        finally {
            $plainPassword = $null
            $securePassword = $null
        }
    }
}
finally {
    if (Test-Path -LiteralPath $tmp) {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}
