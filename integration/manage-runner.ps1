param(
    [Parameter(Mandatory=$true)][ValidateSet('install','start','status','stop')][string]$Action,
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$TaskName
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$taskRoot = (Resolve-Path -LiteralPath $Root).Path
$taskPython = (Resolve-Path -LiteralPath $Python).Path
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$folder = $scheduler.GetFolder('\')
if ($Action -eq 'install') {
    $definition = $scheduler.NewTask(0)
    $definition.RegistrationInfo.Description = 'AutoCST research queue runner; submitted CST jobs survive Codex shutdown.'
    $definition.Principal.UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $definition.Principal.LogonType = 3 # InteractiveToken: same logged-in desktop as CST.
    $definition.Principal.RunLevel = 0
    $definition.Settings.Enabled = $true
    $definition.Settings.ExecutionTimeLimit = 'PT0S'
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Settings.MultipleInstances = 2 # IgnoreNew; the runner also holds an OS lock.
    $trigger = $definition.Triggers.Create(9) # At logon; does not launch CST by itself.
    $trigger.UserId = $definition.Principal.UserId
    $trigger.Enabled = $true
    $exec = $definition.Actions.Create(0)
    $exec.Path = $taskPython
    $exec.Arguments = '-m autocst.research_runner --root "' + $taskRoot + '"'
    $exec.WorkingDirectory = $taskRoot
    $null = $folder.RegisterTaskDefinition($TaskName, $definition, 6, $definition.Principal.UserId, $null, 3)
    Write-Output ('Installed current-user interactive task: ' + $TaskName)
} elseif ($Action -eq 'start') {
    $null = $folder.GetTask($TaskName).Run($null)
    Write-Output ('Started: ' + $TaskName)
} elseif ($Action -eq 'stop') {
    # Graceful stop preserves CST. It is intentionally not Task.Stop(), which can
    # terminate an API child while its native side effect is still unacknowledged.
    [IO.File]::WriteAllText((Join-Path $taskRoot '.autocst\runner_stop.request'), 'stop after current API operation')
    try {
        $signal = [Threading.EventWaitHandle]::OpenExisting(('Local\' + $TaskName + '-queue'))
        $null = $signal.Set()
        $signal.Dispose()
    } catch [Threading.WaitHandleCannotBeOpenedException] {
        # The previous v0.1-style runner checks the request file itself.
    }
    Write-Output ('Graceful runner stop requested: ' + $TaskName)
} else {
    $task = $folder.GetTask($TaskName)
    Write-Output ($task | Select-Object Name,State,LastRunTime,LastTaskResult | ConvertTo-Json -Compress)
}
