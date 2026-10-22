param([switch]$Elevated,[string]$UserSID,[string]$LegacyFolder, [string]$SourceRoot)
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
function Remove-Task([string]$Name) {
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
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
    $code=$guards+"`n& {"+$payload+"`n} -Elevated -UserSID "+(Quote-PS $UserSID)+' -LegacyFolder '+(Quote-PS $LegacyFolder)+' -SourceRoot '+(Quote-PS $SourceRoot)
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
$id=Get-ServiceId $UserSID
Remove-Task ('VCS Network Watch '+$id)
$installRoot=Join-Path ([Environment]::GetFolderPath('ProgramFiles')) ('VCS Network Watch Background\'+$id)
if (Test-Path -LiteralPath $installRoot) { Remove-ServiceTree $installRoot }
if ($LegacyFolder) {
    $legacyId=Get-LegacyId $LegacyFolder
    Remove-Task ('VCS Network Watch '+$legacyId)
    $legacyRoot=Join-Path ([Environment]::GetFolderPath('ProgramFiles')) ('VCS Network Watch Background\'+$legacyId)
    if (Test-Path -LiteralPath $legacyRoot) { Remove-ServiceTree $legacyRoot }
}
# rmdir removes a junction itself and never descends into one (Remove-Item -Recurse in
# Windows PowerShell 5.1 may follow it).
$dataRoot=Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'VCS Network Watch'
$data=Join-Path $dataRoot $id
if (Test-Path -LiteralPath $data) { Remove-ServiceTree $data }
if ((Test-Path -LiteralPath $dataRoot) -and -not (Get-ChildItem -LiteralPath $dataRoot -Force)) { Remove-ServiceTree $dataRoot }
Write-Host 'Background engine removed; its data folder in ProgramData was deleted.'
