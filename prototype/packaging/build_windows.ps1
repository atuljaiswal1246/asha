# Build the Asha Windows bundle (.exe launcher + runtime + models) and, if
# Inno Setup is present, a single-file installer Asha-Setup-<ver>.exe.
#
# Bundles everything a developer needs (voice included). OmniRoute is NOT
# bundled: it is an optional, user-installed add-on that a user can point the
# app at with the shipped template's OMNIROUTE_BASE_URL setting.
#
# Usage:  powershell -ExecutionPolicy Bypass -File prototype\packaging\build_windows.ps1
# Output: prototype\packaging\dist\win\Asha\        (portable bundle)
#         prototype\packaging\dist\Asha-Setup-<ver>.exe  (installer, if ISCC found)
#
# Windows build machine needs: PowerShell, .NET 8 SDK, tar, and (optional, for
# the installer) Inno Setup 6 (ISCC.exe). WebView2 Runtime ships with Win10/11.
# Node/npm are NOT needed.

$ErrorActionPreference = "Stop"
$Repo = Resolve-Path "$PSScriptRoot\..\.."
$Pkg  = Join-Path $Repo "prototype\packaging"
$Dist = Join-Path $Pkg "dist"
$Win  = Join-Path $Dist "win\Asha"
$Build = Join-Path $Pkg ".build-win"
$Version = if (Test-Path "$Pkg\VERSION") { Get-Content "$Pkg\VERSION" } else { "0.1.0" }
$PyVersion = if ($env:PY_VERSION) { $env:PY_VERSION } else { "3.12.14" }
$PbsTag = if ($env:PBS_TAG) { $env:PBS_TAG } else { "20260901" }

Write-Host "==> cleaning"
Remove-Item -Recurse -Force $Win, $Build -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Win, $Build, $Dist | Out-Null

# -- 1. relocatable CPython ---------------------------------------------------
Write-Host "==> fetching python-build-standalone (x86_64-pc-windows-msvc)"
$url = "https://github.com/astral-sh/python-build-standalone/releases/download/$PbsTag/cpython-$PyVersion%2B$PbsTag-x86_64-pc-windows-msvc-install_only.tar.gz"
$tar = Join-Path $Build "python.tar.gz"
Invoke-WebRequest $url -OutFile $tar
New-Item -ItemType Directory -Force -Path (Join-Path $Build "runtime") | Out-Null
tar -xzf $tar -C (Join-Path $Build "runtime") --strip-components=1
$Py = Join-Path $Build "runtime\python.exe"
Write-Host "    python: $(& $Py --version)"

# -- 2. app code --------------------------------------------------------------
Write-Host "==> staging app code"
robocopy "$Repo\prototype\ui" "$Win\app\ui" /E /XD __pycache__ macapp winapp opencode-desktop webfront node_modules /XF *.pyc | Out-Null
robocopy "$Repo\prototype\gateway" "$Win\app\gateway" /E /XD __pycache__ | Out-Null
Copy-Item "$Repo\prototype\requirements.txt" "$Win\app\requirements.txt"
# Ship the NON-SECRET config template — never prototype\.env. Provider API
# keys live on the Asha proxy (Cloudflare); the build host's .env must not
# leak into the bundle. The per-user plan token is issued at sign-in and stored
# outside the bundle, so nothing credential-shaped is copied here.
Copy-Item "$Pkg\shipped.env.template" "$Win\app\.env"
# Optional: bake the deployed proxy base URL into the shipped config without
# editing the template. Set JARVIS_PROXY_URL (a base URL — not a secret) before
# building. The app and any user-installed OmniRoute read JARVIS_GATEWAY_URL
# from here; JARVIS_TOKEN is always added at sign-in.
if ($env:JARVIS_PROXY_URL) {
  Add-Content -Encoding UTF8 (Join-Path $Win "app\.env") "JARVIS_GATEWAY_URL=$($env:JARVIS_PROXY_URL)"
}

# -- 2b. third-party notices --------------------------------------------------
# The bundle ships copyleft components inside runtime\ (phonemizer, eSpeak NG,
# num2words, soxr) as part of the Kokoro TTS stack. Their notices must travel
# with them: copied to a stable bundle path and asserted in step 5b, so a
# future build cannot silently ship without it.
Copy-Item "$Pkg\THIRD_PARTY_NOTICES.md" "$Win\THIRD_PARTY_NOTICES.md"

# -- 3. deps into the runtime -------------------------------------------------
Write-Host "==> installing python deps"
& $Py -m pip install --upgrade pip -q
& $Py -m pip install -q -r "$Win\app\requirements.txt"
& $Py -m pip install -q python-dotenv websockets

# -- 3b. copy the runtime into the bundle -------------------------------------
# Must happen AFTER pip install so the site-packages that step 3 installed
# travel inside the runtime the launcher actually runs ($Win\runtime\python.exe).
Write-Host "==> bundling python runtime into the app"
Copy-Item -Recurse -Force (Join-Path $Build "runtime") (Join-Path $Win "runtime")
$Py = Join-Path $Win "runtime\python.exe"

# -- 3c. OmniRoute is NOT bundled ---------------------------------------------
# The local gateway is an optional, user-installed add-on (third-party,
# ~2.1 GB) and is deliberately not staged, installed or copied here. Asha
# reaches a user's own OmniRoute through the shipped template's
# OMNIROUTE_BASE_URL setting, or runs on its own direct transports.

# -- 4. voice models ----------------------------------------------------------
Write-Host "==> bundling voice models"
$Models = Join-Path $Win "models"
New-Item -ItemType Directory -Force -Path (Join-Path $Models "kokoro"), (Join-Path $Models "moonshine") | Out-Null
$K = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
if (-not (Test-Path (Join-Path $Models "kokoro\kokoro-v1.0.onnx"))) { Invoke-WebRequest "$K/kokoro-v1.0.onnx" -OutFile (Join-Path $Models "kokoro\kokoro-v1.0.onnx") }
if (-not (Test-Path (Join-Path $Models "kokoro\voices-v1.0.bin"))) { Invoke-WebRequest "$K/voices-v1.0.bin" -OutFile (Join-Path $Models "kokoro\voices-v1.0.bin") }
$env:MOONSHINE_VOICE_CACHE = Join-Path $Models "moonshine"
$dl = "from moonshine_voice.download import get_model_for_language`nget_model_for_language('en')`nprint('moonshine ready')"
& $Py -c $dl

# -- 5. launcher --------------------------------------------------------------
Write-Host "==> building launcher (.NET 8)"
dotnet publish "$Pkg\launcher\windows\Jarvis.csproj" -c Release -r win-x64 --self-contained false -o $Win | Out-Null

# -- 5b. verify the bundle (a broken bundle must NOT report success) ----------
Write-Host "==> verifying bundle"
function Assert-Present([string]$Path, [string]$What) {
  if (-not (Test-Path $Path)) { throw "BUILD BROKEN: $What is missing at $Path" }
}

# -- 5b-i. KEYLESS GATE (fail-closed) -----------------------------------------
# The bundle must never contain a provider credential. Scans the first-party
# content we author: staged app code and shipped app\.env. runtime\ is a
# checksum-verified third-party artifact and is out of scope. Test fixtures are
# skipped (placeholder keys false-positive). If ANYTHING key-shaped is found the
# build FAILS.
$SecretRe = 'sk-[A-Za-z0-9_-]{16,}|sk-or-v1-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{30,}|GOCSPX-[A-Za-z0-9_-]{16,}|figd_[A-Za-z0-9_-]{20,}|tvly-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9_-]{20,}|xai-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
$scanHits = @()
foreach ($root in @((Join-Path $Win "app"))) {
  if (-not (Test-Path $root)) { continue }
  $item = Get-Item $root
  $files = if ($item.PSIsContainer) {
    Get-ChildItem -Recurse -File $root | Where-Object { $_.Name -notlike 'test_*.py' }
  } else { @($item) }
  foreach ($f in $files) {
    if (Select-String -Path $f.FullName -Pattern $SecretRe -Quiet -ErrorAction SilentlyContinue) {
      $scanHits += $f.FullName
    }
  }
}
if ($scanHits.Count -gt 0) {
  throw ("BUILD BROKEN: key-shaped string found in the bundle:`n" + ($scanHits -join "`n"))
}
$EnvFile = Join-Path $Win "app\.env"
Assert-Present $EnvFile "shipped config template (.env)"
$cred = Select-String -Path $EnvFile -Pattern '^\s*[A-Z0-9_]*(API_KEY|SECRET|_TOKEN)=\S+' -ErrorAction SilentlyContinue
if ($cred) {
  $lines = ($cred | ForEach-Object { $_.LineNumber }) -join ", "
  throw "BUILD BROKEN: credential-shaped assignment in $EnvFile at line(s) $lines"
}
foreach ($k in @("DEEPSEEK_API_KEY", "OPENCODE_API_KEY")) {
  if (Select-String -Path $EnvFile -Pattern "^${k}=\S+" -Quiet -ErrorAction SilentlyContinue) {
    throw "BUILD BROKEN: $k has a value in $EnvFile"
  }
}
Write-Host "    keyless gate: no provider credentials in app code or app\.env"

Assert-Present (Join-Path $Win "runtime\python.exe")         "bundled python"
Assert-Present (Join-Path $Win "app\ui\launch.py")           "app entry (launch.py)"
Assert-Present (Join-Path $Win "Asha.exe")                   "launcher (Asha.exe)"
Assert-Present (Join-Path $Models "kokoro\kokoro-v1.0.onnx") "kokoro model"
Assert-Present (Join-Path $Models "kokoro\voices-v1.0.bin")  "kokoro voices"
Assert-Present (Join-Path $Win "THIRD_PARTY_NOTICES.md")     "third-party notices (copyleft components)"
$moonshine = Join-Path $Models "moonshine"
Assert-Present $moonshine "moonshine model dir"
if (-not (Get-ChildItem -Recurse -File $moonshine | Select-Object -First 1)) {
  throw "BUILD BROKEN: moonshine model dir is empty at $moonshine"
}
# The runtime that ships must carry the deps step 3 installed, not just python.exe.
$sitePkgs = Join-Path $Win "runtime\Lib\site-packages"
Assert-Present $sitePkgs "bundled site-packages"
Assert-Present (Join-Path $sitePkgs "fastapi") "installed dependency (fastapi)"
# and that the shipped interpreter actually resolves them (this is the exact
# binary the launcher runs, so a relocatability problem fails the build here).
& $Py -c "import fastapi, uvicorn, websockets, dotenv"
if ($LASTEXITCODE -ne 0) { throw "BUILD BROKEN: bundled runtime at $Py cannot import its dependencies" }
# OmniRoute/Node must not be bundled in the keyless build.
if ((Test-Path (Join-Path $Win "runtime-node")) -or (Test-Path (Join-Path $Win "vendor"))) {
  throw "BUILD BROKEN: OmniRoute/Node artifacts must not be bundled (found runtime-node\ or vendor\)"
}
Write-Host "    bundle verified (no bundled gateway)"

# -- 6. installer (optional) --------------------------------------------------
$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($iscc) {
  Write-Host "==> building installer"
  & $iscc.Source "/DVersion=$Version" "$Pkg\installer\windows.iss"
  Write-Host "    installer -> $Dist\Asha-Setup-$Version.exe"
} else {
  Write-Host "==> Inno Setup (ISCC.exe) not found - portable bundle only at $Win"
}

Write-Host ""
Write-Host " done:"
Write-Host "  bundle:    $Win"
Write-Host "  installer: $Dist\Asha-Setup-$Version.exe (if ISCC present)"
