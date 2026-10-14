$ErrorActionPreference = "Stop"

python -m PyInstaller --noconfirm --clean dynamics_control_lab.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller завершил сборку с кодом $LASTEXITCODE."
}

$exe = (Resolve-Path "dist\DynamicsControlLab\DynamicsControlLab.exe").Path

# Time from process start to a constructed window, first (cold) and second launch.
function Measure-Startup([string]$Name) {
    $output = Join-Path (Get-Location) "build\startup-$Name.json"
    $ready = [System.IO.Path]::ChangeExtension($output, ".ready")
    Remove-Item -LiteralPath $ready -ErrorAction SilentlyContinue
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $process = Start-Process -FilePath $exe -ArgumentList @("--performance-check", ('"' + $output + '"')) -WindowStyle Hidden -PassThru
    while (-not (Test-Path -LiteralPath $ready) -and $clock.Elapsed.TotalSeconds -lt 120 -and -not $process.HasExited) {
        Start-Sleep -Milliseconds 50
    }
    $seconds = if (Test-Path -LiteralPath $ready) { [math]::Round($clock.Elapsed.TotalSeconds, 3) } else { $null }
    Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
    $process.WaitForExit(10000) | Out-Null
    return $seconds
}
$startup = [ordered]@{ first_launch_to_window_s = (Measure-Startup "first"); second_launch_to_window_s = (Measure-Startup "second") }
$startup | ConvertTo-Json | Set-Content -LiteralPath "build\startup.json" -Encoding utf8
Write-Host "Запуск EXE до готового окна: первый $($startup.first_launch_to_window_s) с, повторный $($startup.second_launch_to_window_s) с."

$taskOutput = Join-Path (Get-Location) "build\release-check.json"
$taskProcess = Start-Process -FilePath (Resolve-Path "dist\DynamicsControlLab\DynamicsControlLab.exe").Path -ArgumentList @("--smoke-test", ('"' + $taskOutput + '"')) -WindowStyle Hidden -PassThru
# Generous limits: a cold CI runner can be slow; a hang still fails the build.
if (-not $taskProcess.WaitForExit(180000)) {
    Stop-Process -Id $taskProcess.Id
    throw "Проверка приложения превысила 180 секунд."
}
$taskProcess.Refresh()
Write-Host "Проверка приложения: $([math]::Round(((Get-Date) - $taskProcess.StartTime).TotalSeconds, 1)) с."
if ($taskProcess.ExitCode -ne 0) {
    Get-Content -LiteralPath $taskOutput -ErrorAction SilentlyContinue
    throw "Проверка приложения завершилась с кодом $($taskProcess.ExitCode)."
}
$taskReport = Get-Content -LiteralPath $taskOutput -Raw | ConvertFrom-Json
if ($taskReport.status -ne "passed" -or -not $taskReport.frozen) {
    throw "Упакованное приложение не прошло проверку."
}

$performanceOutput = Join-Path (Get-Location) "build\performance-check.json"
$performanceProcess = Start-Process -FilePath (Resolve-Path "dist\DynamicsControlLab\DynamicsControlLab.exe").Path -ArgumentList @("--performance-check", ('"' + $performanceOutput + '"')) -WindowStyle Hidden -PassThru
if (-not $performanceProcess.WaitForExit(180000)) {
    Stop-Process -Id $performanceProcess.Id
    throw "Измерение производительности превысило 180 секунд."
}
$performanceProcess.Refresh()
Write-Host "Измерение производительности: $([math]::Round(((Get-Date) - $performanceProcess.StartTime).TotalSeconds, 1)) с."
$performanceReport = Get-Content -LiteralPath $performanceOutput -Raw | ConvertFrom-Json
if ($performanceProcess.ExitCode -ne 0 -or $performanceReport.status -ne "passed" -or -not $performanceReport.frozen) {
    throw "Измерение производительности EXE завершилось с ошибкой."
}

Compress-Archive -Path "dist\DynamicsControlLab" -DestinationPath "dist\DynamicsControlLab.zip" -Force
Write-Host "Готово: dist\DynamicsControlLab.zip"
