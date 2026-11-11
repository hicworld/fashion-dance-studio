param([switch]$SkipInstall, [switch]$SkipSmokeTest, [switch]$PortableRelease, [string]$OutputDirectory = 'dist')
$ErrorActionPreference = 'Stop'
$workflowProject = [IO.Path]::GetFullPath($PSScriptRoot)
$workflowBuildDir = if ($PortableRelease) {'build\portable'} else {'build\desktop'}
$workflowBuild = [IO.Path]::GetFullPath((Join-Path $workflowProject $workflowBuildDir))
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) { throw 'OutputDirectory must not be empty.' }
if ([IO.Path]::IsPathRooted($OutputDirectory)) {
    $workflowDist = [IO.Path]::GetFullPath($OutputDirectory)
} else {
    $workflowDist = [IO.Path]::GetFullPath((Join-Path $workflowProject $OutputDirectory))
}
foreach ($workflowTarget in @($workflowBuild, $workflowDist)) {
    if (-not $workflowTarget.StartsWith($workflowProject + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Build/output directory must remain inside this project.'
    }
}
$workflowPython = Join-Path $workflowProject '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $workflowPython -PathType Leaf)) { throw '请先创建项目 .venv，或使用启动.cmd 安装环境。' }
$env:PYTHONUTF8 = '1'
$workflowOriginalPath = $env:PATH
$workflowOriginalPortable = $env:FASHION_STUDIO_PORTABLE_RELEASE
$workflowOriginalDataHome = $env:FASHION_DANCE_HOME
Push-Location -LiteralPath $workflowProject
try {
    if (-not $SkipInstall) {
        & $workflowPython -m pip install -r requirements-desktop.txt
        if ($LASTEXITCODE -ne 0) { throw 'Desktop dependency installation failed.' }
    }
    $env:FASHION_STUDIO_PORTABLE_RELEASE = if ($PortableRelease) {'1'} else {'0'}
    if ($PortableRelease) {
        $workflowBasePython = & $workflowPython -c "import sys; print(sys.base_prefix)"
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $workflowBasePython -PathType Container)) { throw 'Cannot resolve the base Python installation.' }
        # Keep DLL discovery away from unrelated tools on the developer PATH.
        $env:PATH = @(
            (Split-Path $workflowPython -Parent), $workflowBasePython,
            (Join-Path $workflowBasePython 'DLLs'), (Join-Path $workflowBasePython 'Library\bin'),
            (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot
        ) -join ';'
    } else {
        $env:FASHION_STUDIO_FFMPEG = (Get-Command ffmpeg -CommandType Application).Path
        $env:FASHION_STUDIO_FFPROBE = (Get-Command ffprobe -CommandType Application).Path
        & $workflowPython prepare_realesrgan.py --check
        if ($LASTEXITCODE -ne 0) { throw 'Run .venv\Scripts\python.exe prepare_realesrgan.py before packaging.' }
    }
    & $workflowPython create_desktop_icon.py
    if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed.' }
    & $workflowPython -m PyInstaller --noconfirm --distpath $workflowDist --workpath $workflowBuild FashionDanceStudio.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    $workflowRelease = Join-Path $workflowDist '穿搭舞蹈工作室'
    foreach ($workflowFile in @('README.md', '启动说明.txt', '安装AI超分.cmd', '创建桌面快捷方式.ps1', '创建桌面快捷方式.cmd')) {
        Copy-Item -LiteralPath (Join-Path $workflowProject $workflowFile) -Destination $workflowRelease -Force
    }
    $workflowReleaseDocs = Join-Path $workflowRelease 'docs'
    New-Item -ItemType Directory -Path $workflowReleaseDocs -Force | Out-Null
    foreach ($workflowGuide in @('workflows.md', 'third-party.md')) {
        Copy-Item -LiteralPath (Join-Path $workflowProject ('docs\' + $workflowGuide)) -Destination $workflowReleaseDocs -Force
    }
    if ($PortableRelease) {
        & $workflowPython collect_release_notices.py $workflowRelease --analysis-dir (Join-Path $workflowBuild 'FashionDanceStudio')
        if ($LASTEXITCODE -ne 0) { throw 'Release notices could not be collected.' }
    } else {
        $workflowFfmpegLicense = Join-Path (Split-Path (Split-Path $env:FASHION_STUDIO_FFMPEG -Parent) -Parent) 'LICENSE'
        if (Test-Path -LiteralPath $workflowFfmpegLicense -PathType Leaf) {
            Copy-Item -LiteralPath $workflowFfmpegLicense -Destination (Join-Path $workflowRelease 'FFmpeg-LICENSE.txt') -Force
        }
    }
    if (-not $SkipSmokeTest) {
        $workflowExe = Join-Path $workflowRelease '穿搭舞蹈工作室.exe'
        $env:FASHION_DANCE_HOME = Join-Path $workflowBuild ('smoke-profile-' + [Guid]::NewGuid().ToString('N'))
        $workflowSmokeArgs = if ($PortableRelease) {'--smoke-test'} else {'--smoke-test --require-media'}
        $workflowSmokeProcess = Start-Process -FilePath $workflowExe -ArgumentList $workflowSmokeArgs -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $workflowBuild 'smoke-stdout.txt') -RedirectStandardError (Join-Path $workflowBuild 'smoke-stderr.txt')
        if ($workflowSmokeProcess.ExitCode -ne 0) { throw 'Packaged local smoke test failed. See logs/desktop-smoke.json in the application data directory.' }
        Write-Host 'Packaged local smoke test passed. No cloud generation was submitted.'
    }
    Write-Host ('发行目录：' + $workflowRelease)
} finally {
    $env:PATH = $workflowOriginalPath
    $env:FASHION_STUDIO_PORTABLE_RELEASE = $workflowOriginalPortable
    $env:FASHION_DANCE_HOME = $workflowOriginalDataHome
    Pop-Location
}
