# Run as the intended user first; UAC then elevates the same user.
# The engine runs as SYSTEM and keeps its data in %ProgramData%\VCS Network Watch\<id>, which it
# creates and locks itself (SYSTEM/administrators; the user may only read service.key).
param([switch]$Elevated, [string]$UserSID, [string]$LegacyFolder, [string]$SourceRoot, [string]$PackageDigest)
$ErrorActionPreference='Stop'
# Same value as service_paths.folder_id in Python.
function Get-ServiceId([string]$Sid) {
    $digest=[System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::ASCII.GetBytes($Sid.ToUpperInvariant()))
    return [System.BitConverter]::ToString($digest).Replace('-','').Substring(0,16).ToLowerInvariant()
}
# 2.8.1 and older: task and executables were keyed by the user's data folder.
function Get-LegacyId([string]$Folder) {
    $digest=[System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Folder.ToLowerInvariant()))
    return [System.BitConverter]::ToString($digest).Replace('-','').Substring(0,12)
}
# Runs unelevated, as the owner of the folder: an elevated process never deletes there.
function Remove-LegacyFiles([string]$Folder) {
    foreach ($name in @('service-user.sid','service.key','service.lock','errors-service.log','errors-service.log.1','crash-service.log','network.sqlite3','network.sqlite3-wal','network.sqlite3-shm')) {
        Remove-Item -LiteralPath (Join-Path $Folder $name) -Force -ErrorAction SilentlyContinue
    }
}
if (-not $Elevated) {
    $UserSID=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $LegacyFolder=Join-Path $env:LOCALAPPDATA 'VCS Network Watch'
    $SourceRoot=Split-Path -Parent $PSCommandPath
    $payload=[IO.File]::ReadAllText($PSCommandPath)
    $tokens=$null; $syntaxErrors=$null
    $ast=[Management.Automation.Language.Parser]::ParseInput($payload,[ref]$tokens,[ref]$syntaxErrors)
    if ($syntaxErrors.Count) { throw 'Invalid installer syntax' }
    $functions=($ast.EndBlock.Statements | Where-Object { $_ -is [Management.Automation.Language.FunctionDefinitionAst] } | ForEach-Object { $_.Extent.Text }) -join "`n"
    $marker=$payload.IndexOf(([char]10).ToString() + '# Elevated payload begins here.')
    if ($marker -lt 0) { throw 'Missing elevated payload' }
    $payload=$ast.ParamBlock.Extent.Text+"`n"+$functions+"`n"+$payload.Substring($marker+1)
    $guards=[IO.File]::ReadAllText((Join-Path $SourceRoot 'windows-service-safety.ps1'))
    function Quote-PS([string]$Text) { return "'"+$Text.Replace("'","''")+"'" }
    . ([ScriptBlock]::Create($guards))
    $PackageDigest=Get-PackageDigest $SourceRoot
    $code=$guards+"`n& {"+$payload+"`n} -Elevated -UserSID "+(Quote-PS $UserSID)+' -LegacyFolder '+(Quote-PS $LegacyFolder)+' -SourceRoot '+(Quote-PS $SourceRoot)+' -PackageDigest '+(Quote-PS $PackageDigest)
    $bytes=[Text.Encoding]::Unicode.GetBytes($code)
    $memory=[IO.MemoryStream]::new()
    $zip=[IO.Compression.GZipStream]::new($memory,[IO.Compression.CompressionMode]::Compress,$true)
    try { $zip.Write($bytes,0,$bytes.Length) } finally { $zip.Dispose() }
    $packed=[Convert]::ToBase64String($memory.ToArray());$memory.Dispose()
    $loader='$env:PSModulePath=[IO.Path]::Combine([Environment]::SystemDirectory,''WindowsPowerShell\v1.0\Modules'');$m=[IO.MemoryStream]::new([Convert]::FromBase64String('+(Quote-PS $packed)+'));$z=[IO.Compression.GZipStream]::new($m,[IO.Compression.CompressionMode]::Decompress);$r=[IO.StreamReader]::new($z,[Text.Encoding]::Unicode);try { & ([ScriptBlock]::Create($r.ReadToEnd())) } finally {$r.Dispose();$z.Dispose();$m.Dispose()}'
    $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($loader))
    if ($encoded.Length -gt 30000) { throw 'Installer payload exceeds Windows command-line limit' }
    $shell=Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
    $process=Start-Process -FilePath $shell -Verb RunAs -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded) -Wait -PassThru
    if ($process.ExitCode -eq 0 -and (Test-Path -LiteralPath (Join-Path $LegacyFolder 'service-user.sid'))) { Remove-LegacyFiles $LegacyFolder }
    exit $process.ExitCode
}
# Elevated payload begins here.
if (-not (Get-Command Assert-ServiceEntry -ErrorAction SilentlyContinue)) { throw 'Run the installer without -Elevated' }
if ($UserSID -notmatch '^S-1-\d+(-\d+)+$') { throw 'Invalid user SID' }
if ($PackageDigest -notmatch '^[A-Fa-f0-9]{64}$') { throw 'Missing package fingerprint' }
$root=$SourceRoot
$python=Join-Path $root 'runtime\pythonw.exe'
$script=Join-Path $root 'app\desktop.py'
if (-not (Test-Path $python) -or -not (Test-Path $script)) { throw 'Extract the entire ZIP to a permanent folder first.' }
if ($LegacyFolder) {
    $legacyId=Get-LegacyId $LegacyFolder
    $legacyTask='VCS Network Watch '+$legacyId
    if (Get-ScheduledTask -TaskName $legacyTask -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $legacyTask -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $legacyTask -Confirm:$false
    }
    $legacyRoot=Join-Path ([Environment]::GetFolderPath('ProgramFiles')) ('VCS Network Watch Background\'+$legacyId)
    if (Test-Path -LiteralPath $legacyRoot) { Remove-ServiceTree $legacyRoot }
}
$id=Get-ServiceId $UserSID
$taskName='VCS Network Watch '+$id
$installRoot=Join-Path ([Environment]::GetFolderPath('ProgramFiles')) ('VCS Network Watch Background\'+$id)
$parent=Split-Path -Parent $installRoot
if (Test-Path -LiteralPath $parent) { Assert-ServiceEntry $parent }
else { New-Item -ItemType Directory -Path $parent | Out-Null }
Protect-ServiceDirectory $parent -Private
if (Test-Path -LiteralPath $installRoot) { Assert-ServiceTree $installRoot }
$stage=Join-Path $parent ('.stage-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
Protect-ServiceDirectory $stage -Private
try {
    foreach ($part in @('runtime','packages','app')) {
        $source=Join-Path $root $part
        Assert-SourceTree $source
        Copy-ServiceTree $source (Join-Path $stage $part)
    }
    if ((Get-PackageDigest $stage) -ne $PackageDigest) { throw 'Package changed after authorization' }
    Assert-ServiceTree $stage
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $backup=Join-Path $parent ('.previous-'+[Guid]::NewGuid().ToString('N'))
    if (Test-Path -LiteralPath $installRoot) { Move-Item -LiteralPath $installRoot -Destination $backup }
    try { Move-Item -LiteralPath $stage -Destination $installRoot }
    catch {
        if (Test-Path -LiteralPath $backup) { Move-Item -LiteralPath $backup -Destination $installRoot }
        throw
    }
    Remove-ServiceTree $backup
} finally { Remove-ServiceTree $stage }
$python=Join-Path $installRoot 'runtime\pythonw.exe'
$script=Join-Path $installRoot 'app\desktop.py'
$action=New-ScheduledTaskAction -Execute $python -Argument ('"'+$script+'" --engine-service --user-sid '+$UserSID) -WorkingDirectory $installRoot
$trigger=New-ScheduledTaskTrigger -AtStartup
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host 'Background engine installed. Open LinkRadar; active monitoring will show ФОН.'
