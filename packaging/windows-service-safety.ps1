# Included in the frozen UAC payload; never dot-source user files after elevation.
function Assert-ServiceEntry([string]$Path) {
    $item=Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse points are forbidden in service paths' }
    $acl=Get-Acl -LiteralPath $Path
    $trusted=@('S-1-5-18','S-1-5-32-544','S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464',[Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
    $owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($trusted -notcontains $owner) { throw 'Untrusted owner of service path' }
    $write=[Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership
    foreach ($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
        if (($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0) { continue }
        if ($rule.AccessControlType -eq 'Allow' -and ($rule.FileSystemRights -band $write) -and $trusted -notcontains $rule.IdentityReference.Value) { throw 'Service path is writable by an untrusted account' }
    }
}
function Assert-ServiceTree([string]$Path) {
    Assert-ServiceEntry $Path
    $item=Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer) {
        foreach ($child in Get-ChildItem -LiteralPath $Path -Force) { Assert-ServiceTree $child.FullName }
    }
}
function Assert-SourceTree([string]$Path) {
    $item=Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Package contains a reparse point' }
    if ($item.PSIsContainer) {
        foreach ($child in Get-ChildItem -LiteralPath $Path -Force) { Assert-SourceTree $child.FullName }
    }
}
function Protect-ServiceDirectory([string]$Path, [switch]$Private) {
    $acl=New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true,$false)
    $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    $sids=@('S-1-5-18','S-1-5-32-544')
    if (-not $Private) { $sids+=@('S-1-5-32-545') }
    foreach ($sid in $sids) {
        $rights=if($sid -eq 'S-1-5-32-545'){'ReadAndExecute'}else{'FullControl'}
        $rule=New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($sid)),$rights,'ContainerInherit,ObjectInherit','None','Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}
function Remove-ServiceTree([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        Assert-ServiceTree $Path
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}
function Get-PackageDigest([string]$Root) {
    $lines=New-Object 'Collections.Generic.List[string]'
    foreach ($part in @('runtime','packages','app')) {
        $base=Join-Path $Root $part
        Assert-SourceTree $base
        foreach ($file in Get-ChildItem -LiteralPath $base -File -Recurse -Force) {
            $relative=$file.FullName.Substring($Root.Length).TrimStart('\').Replace('\','/')
            $stream=[IO.File]::OpenRead($file.FullName)
            try {
                $hasher=[Security.Cryptography.SHA256]::Create()
                try { $digest=[Convert]::ToBase64String($hasher.ComputeHash($stream)) }
                finally { $hasher.Dispose() }
            } finally { $stream.Dispose() }
            $lines.Add($relative+"`t"+$digest)
        }
    }
    $lines.Sort([StringComparer]::Ordinal)
    $hasher=[Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes([string]::Join("`n",$lines)))).Replace('-','') }
    finally { $hasher.Dispose() }
}
function New-PrivateServiceSecurity([bool]$Directory) {
    $acl=if($Directory){New-Object Security.AccessControl.DirectorySecurity}else{New-Object Security.AccessControl.FileSecurity}
    $acl.SetAccessRuleProtection($true,$false)
    $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    $inherit=if($Directory){'ContainerInherit,ObjectInherit'}else{'None'}
    foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {
        $rule=New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($sid)),'FullControl',$inherit,'None','Allow')
        $acl.AddAccessRule($rule)
    }
    return $acl
}
function Copy-ServiceTree([string]$Source,[string]$Destination) {
    $item=Get-Item -LiteralPath $Source -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse point in package' }
    if ($item.PSIsContainer) {
        [IO.Directory]::CreateDirectory($Destination,(New-PrivateServiceSecurity $true)) | Out-Null
        foreach ($child in Get-ChildItem -LiteralPath $Source -Force) { Copy-ServiceTree $child.FullName (Join-Path $Destination $child.Name) }
    } else {
        # Owner and protected ACL are installed by CREATE_NEW, before any bytes.
        # Failed fingerprint verification can never expose a privileged read to Users.
        $output=New-Object IO.FileStream($Destination,[IO.FileMode]::CreateNew,[Security.AccessControl.FileSystemRights]::Write,[IO.FileShare]::None,65536,[IO.FileOptions]::None,(New-PrivateServiceSecurity $false))
        try {
            $sourceStream=[IO.File]::OpenRead($Source)
            try { $sourceStream.CopyTo($output) } finally { $sourceStream.Dispose() }
        } finally { $output.Dispose() }
    }
}
