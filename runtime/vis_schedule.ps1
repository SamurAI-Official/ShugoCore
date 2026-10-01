param(
    [Parameter(Mandatory = $true)][string]$BaseTime,
    [ValidateSet('full', 'dark')][string]$Plan = 'full'
)

# Marks the staged vision phases for a capture run, on the host clock.
#
# The capture tool reads phase markers in host time and the presence series is
# converted to host time too, so the markers only have to line up with what the
# operator physically did -- not with when the tool cleared logcat. BaseTime is
# therefore chosen well after the capture's pre-flight, and the whole plan hangs
# off it.
#
# Run detached:
#   powershell -NoProfile -ExecutionPolicy Bypass -File runtime\vis_schedule.ps1 -BaseTime <iso>

$ErrorActionPreference = 'Continue'
Set-Location 'g:\Program Prototype\shugocore'

$t0 = [datetime]::Parse($BaseTime)
$log = 'runtime\vis_phases_log.txt'

if ($Plan -eq 'dark') {
    # A dark-only run: shorter holds, the A51 first and longest because it samples most thinly,
    # and "empty" meaning *nobody in frame*. The first session said "nobody moving", the operator
    # reasonably stayed in view, and the phase recorded a face -- so it could not serve as the
    # empty reference the stretch-invention test needs.
    $plan = @(
        @{ Offset = 30;  Text = 'dark empty, nobody in frame' },
        @{ Offset = 120; Text = 'dark person a51 still' },
        @{ Offset = 195; Text = 'dark person a51 moving' },
        @{ Offset = 255; Text = 'dark person s9fe still' },
        @{ Offset = 330; Text = 'dark person s9fe moving' },
        @{ Offset = 390; Text = 'dark person a16 still' },
        @{ Offset = 465; Text = 'dark person a16 moving' },
        @{ Offset = 510; Text = 'end' }
    )
} else {
    $plan = @(
        @{ Offset = 30;  Text = 'lights on, empty' },
        @{ Offset = 90;  Text = 'lit person a51' },
        @{ Offset = 150; Text = 'lit person s9fe' },
        @{ Offset = 210; Text = 'lit person a16' },
        @{ Offset = 270; Text = 'lit empty' },
        @{ Offset = 315; Text = 'claps a51 position' },
        @{ Offset = 360; Text = 'claps s9fe position' },
        @{ Offset = 405; Text = 'claps a16 position' },
        @{ Offset = 450; Text = 'lights off' },
        @{ Offset = 540; Text = 'dark empty' },
        @{ Offset = 600; Text = 'dark person a51 still' },
        @{ Offset = 660; Text = 'dark person a51 moving' },
        @{ Offset = 705; Text = 'dark person s9fe still' },
        @{ Offset = 750; Text = 'dark person s9fe moving' },
        @{ Offset = 795; Text = 'dark person a16 still' },
        @{ Offset = 840; Text = 'end' }
    )
}

$header = @('=== vision phase schedule, T = ' + $t0.ToString('HH:mm:ss') + ' (host clock) ===')
foreach ($step in $plan) {
    $header += ('  {0:HH:mm:ss}  +{1,4}s  {2}' -f $t0.AddSeconds($step.Offset), $step.Offset, $step.Text)
}
$header | Set-Content $log -Encoding UTF8
$header | ForEach-Object { Write-Output $_ }

foreach ($step in $plan) {
    $at = $t0.AddSeconds($step.Offset)
    while ((Get-Date) -lt $at) { Start-Sleep -Milliseconds 400 }
    $when = Get-Date
    $out = (& python runtime\fleet_correlation.py --out runtime\corr_vis --label vis --mark-phase $step.Text 2>&1 | Out-String).Trim()
    ('  {0:HH:mm:ss}  marked  {1}   ({2})' -f $when, $step.Text, $out) | Add-Content $log
}

('  {0:HH:mm:ss}  schedule complete' -f (Get-Date)) | Add-Content $log
