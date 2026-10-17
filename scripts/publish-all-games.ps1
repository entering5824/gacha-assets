[CmdletBinding()]
param(
    [ValidateSet("dry-run", "verify-only", "publish")]
    [string]$Mode = "dry-run",
    [string]$PythonPath = "python",
    [string]$AssetsRoot = "cache",
    [string]$ReportDirectory = "reports"
)

$ErrorActionPreference = "Stop"
$serviceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([System.IO.Path]::IsPathRooted($AssetsRoot)) {
    $resolvedAssetsRoot = $AssetsRoot
} else {
    $resolvedAssetsRoot = Join-Path $serviceRoot $AssetsRoot
}
if ([System.IO.Path]::IsPathRooted($ReportDirectory)) {
    $resolvedReportDirectory = $ReportDirectory
} else {
    $resolvedReportDirectory = Join-Path $serviceRoot $ReportDirectory
}
if (-not (Test-Path -LiteralPath $resolvedAssetsRoot -PathType Container)) {
    throw "Asset cache directory not found: $resolvedAssetsRoot"
}
New-Item -ItemType Directory -Path $resolvedReportDirectory -Force | Out-Null

$games = @("genshin", "hsr", "zzz", "wuwa")
$aggregateName = "zenlentering"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$priorPythonPath = $env:PYTHONPATH
$sourcePath = Join-Path $serviceRoot "src"
$env:PYTHONPATH = if ($priorPythonPath) { "$sourcePath$([System.IO.Path]::PathSeparator)$priorPythonPath" } else { $sourcePath }
$summary = [System.Collections.Generic.List[object]]::new()
$failures = [System.Collections.Generic.List[string]]::new()

Push-Location $serviceRoot
try {
    foreach ($game in $games) {
        $manifestPath = Join-Path $serviceRoot "manifests\$game.json"
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
            $failures.Add("$game manifest not found: $manifestPath")
            if ($Mode -eq "publish") { break }
            continue
        }
        $reportPath = Join-Path $resolvedReportDirectory "$game-$Mode-$stamp.json"
        $arguments = @(
            "-m", "gacha_assets.cli", "publish-supabase",
            "--manifest", $manifestPath,
            "--assets-root", $resolvedAssetsRoot,
            "--report", $reportPath
        )
        if ($Mode -eq "dry-run") { $arguments += "--dry-run" }
        if ($Mode -eq "verify-only") { $arguments += "--verify-only" }

        try {
            $cliOutput = & $PythonPath @arguments 2>&1
            $exitCode = $LASTEXITCODE
            if ($exitCode -ne 0) {
                $detail = ($cliOutput | Out-String).Trim()
                throw "gacha-assets exited with code $exitCode. $detail"
            }
            if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) { throw "publisher report was not created" }
            $report = Get-Content -Raw -LiteralPath $reportPath | ConvertFrom-Json
            $validated = @($report.assets | Where-Object status -eq "validated").Count
            $statusCounts = @{}
            foreach ($group in @($report.assets | Group-Object status)) { $statusCounts[$group.Name] = $group.Count }

            $summary.Add([pscustomobject]@{
                game = $game
                expected = $report.expected
                ready = $report.ready
                validated = $validated
                coverage = $report.coverage
                manifestVerified = $report.manifestVerified
                manifestStatus = $report.manifestStatus
                writeAttempted = $report.supabaseWriteAttempted
                statuses = $statusCounts
                report = $reportPath
            })
            if ($Mode -eq "dry-run" -and ($validated -ne $report.expected -or $report.supabaseWriteAttempted)) {
                throw "dry-run did not validate every expected object without writes"
            }
            if ($Mode -eq "verify-only" -and ($report.ready -ne $report.expected -or -not $report.manifestVerified -or $report.supabaseWriteAttempted)) {
                throw "verify-only did not verify every object and remote manifest without writes"
            }
            if ($Mode -eq "publish" -and ($report.ready -ne $report.expected -or -not $report.manifestVerified)) {
                throw "publish did not verify every object and the remote manifest"
            }
        } catch {
            $failures.Add("${game}: $($_.Exception.Message)")
            if ($Mode -eq "publish") { break }
        }
    }

    if ($Mode -ne "publish" -or $failures.Count -eq 0) {
        $aggregateManifestPath = Join-Path $serviceRoot "manifests\$aggregateName.json"
        $mergeArguments = @("-m", "gacha_assets.cli", "merge-manifests")
        foreach ($game in $games) {
            $mergeArguments += @("--input", (Join-Path $serviceRoot "manifests\$game.json"))
        }
        $mergeArguments += @("--manifest", $aggregateManifestPath)
        $mergeOutput = & $PythonPath @mergeArguments 2>&1
        if ($LASTEXITCODE -ne 0) {
            $detail = ($mergeOutput | Out-String).Trim()
            $failures.Add("${aggregateName}: manifest merge failed. $detail")
        } else {
            $reportPath = Join-Path $resolvedReportDirectory "$aggregateName-$Mode-$stamp.json"
            $arguments = @(
                "-m", "gacha_assets.cli", "publish-supabase",
                "--manifest", $aggregateManifestPath,
                "--assets-root", $resolvedAssetsRoot,
                "--manifest-path", "manifests/$aggregateName.json",
                "--report", $reportPath
            )
            if ($Mode -eq "dry-run") { $arguments += "--dry-run" }
            if ($Mode -eq "verify-only") { $arguments += "--verify-only" }
            try {
                $cliOutput = & $PythonPath @arguments 2>&1
                $exitCode = $LASTEXITCODE
                if ($exitCode -ne 0) {
                    $detail = ($cliOutput | Out-String).Trim()
                    throw "gacha-assets exited with code $exitCode. $detail"
                }
                if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) { throw "publisher report was not created" }
                $report = Get-Content -Raw -LiteralPath $reportPath | ConvertFrom-Json
                $validated = @($report.assets | Where-Object status -eq "validated").Count
                $statusCounts = @{}
                foreach ($group in @($report.assets | Group-Object status)) { $statusCounts[$group.Name] = $group.Count }
                $summary.Add([pscustomobject]@{
                    game = $aggregateName
                    expected = $report.expected
                    ready = $report.ready
                    validated = $validated
                    coverage = $report.coverage
                    manifestVerified = $report.manifestVerified
                    manifestStatus = $report.manifestStatus
                    writeAttempted = $report.supabaseWriteAttempted
                    statuses = $statusCounts
                    report = $reportPath
                })
                if ($Mode -eq "dry-run" -and ($validated -ne $report.expected -or $report.supabaseWriteAttempted)) {
                    throw "aggregate dry-run did not validate every expected object without writes"
                }
                if ($Mode -eq "verify-only" -and ($report.ready -ne $report.expected -or -not $report.manifestVerified -or $report.supabaseWriteAttempted)) {
                    throw "aggregate verify-only did not verify every object and manifest without writes"
                }
                if ($Mode -eq "publish" -and ($report.ready -ne $report.expected -or -not $report.manifestVerified)) {
                    throw "aggregate publish did not verify every object and manifest"
                }
            } catch {
                $failures.Add("${aggregateName}: $($_.Exception.Message)")
            }
        }
    }
} finally {
    Pop-Location
    $env:PYTHONPATH = $priorPythonPath
}

$result = [pscustomobject]@{
    mode = $Mode
    games = @($summary)
    failures = @($failures)
    reportsDirectory = $resolvedReportDirectory
}
$result | ConvertTo-Json -Depth 8
if ($failures.Count -gt 0) { throw "Asset distribution $Mode had $($failures.Count) failure(s). See reports above." }



