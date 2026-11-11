[Setup]
AppId={{E458730F-660A-4D56-9C5E-EE8D25DD6779}
AppName=LinkRadar
AppVersion={#AppVersion}
AppPublisher=LinkRadar
AppPublisherURL=https://github.com/mshnyrev13/linkradar-updates
AppUpdatesURL=https://github.com/mshnyrev13/linkradar-updates/releases/latest
DefaultDirName={localappdata}\Programs\LinkRadar
DefaultGroupName=LinkRadar
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible and not arm64
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
SetupIconFile=..\..\linkradar\branding\NetworkWatch.ico
UninstallDisplayIcon={app}\LinkRadar.exe
OutputDir={#OutputFolder}
OutputBaseFilename=LinkRadar-{#AppVersion}-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\LinkRadar"; Filename: "{app}\LinkRadar.exe"; WorkingDir: "{app}"; AppUserModelID: "VCS.NetworkWatch.Desktop"
Name: "{autodesktop}\LinkRadar"; Filename: "{app}\LinkRadar.exe"; WorkingDir: "{app}"; Tasks: desktopicon; AppUserModelID: "VCS.NetworkWatch.Desktop"
Name: "{group}\{cm:BackgroundInstall}"; Filename: "{app}\support\Install background.cmd"; WorkingDir: "{app}\support"
Name: "{group}\{cm:BackgroundRemove}"; Filename: "{app}\support\Remove background.cmd"; WorkingDir: "{app}\support"

[CustomMessages]
english.BackgroundInstall=Install or update background engine
english.BackgroundRemove=Remove background engine
russian.BackgroundInstall=Установить или обновить фоновый движок
russian.BackgroundRemove=Удалить фоновый движок

[Run]
Filename: "{app}\LinkRadar.exe"; Description: "{cm:LaunchProgram,LinkRadar}"; Flags: nowait postinstall skipifsilent

[Code]
var
  PreviousFlatLayout: Boolean;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    PreviousFlatLayout := FileExists(ExpandConstant('{app}\BUILD-MANIFEST.json')) and
                          FileExists(ExpandConstant('{app}\app\desktop.py'));
  if (CurStep = ssPostInstall) and PreviousFlatLayout then
  begin
    DeleteFile(ExpandConstant('{app}\README.md'));
    DeleteFile(ExpandConstant('{app}\BUILD-MANIFEST.json'));
    DeleteFile(ExpandConstant('{app}\Install background.cmd'));
    DeleteFile(ExpandConstant('{app}\Remove background.cmd'));
    DeleteFile(ExpandConstant('{app}\install-background-windows.ps1'));
    DeleteFile(ExpandConstant('{app}\remove-background-windows.ps1'));
    DeleteFile(ExpandConstant('{app}\windows-service-safety.ps1'));
    DelTree(ExpandConstant('{app}\app\*.py'), False, True, False);
    DelTree(ExpandConstant('{app}\app\branding'), True, True, True);
    DelTree(ExpandConstant('{app}\packages\PySide6\doc'), True, True, True);
    DelTree(ExpandConstant('{app}\packages\PySide6\glue'), True, True, True);
    DelTree(ExpandConstant('{app}\packages\PySide6\include'), True, True, True);
    DelTree(ExpandConstant('{app}\packages\PySide6\lib'), True, True, True);
    DelTree(ExpandConstant('{app}\packages\PySide6\metatypes'), True, True, True);
    DelTree(ExpandConstant('{app}\packages\PySide6\qml'), True, True, True);
    DelTree(ExpandConstant('{app}\packages\PySide6\scripts'), True, True, True);
    DelTree(ExpandConstant('{app}\packages\PySide6\typesystems'), True, True, True);
    DelTree(ExpandConstant('{app}\packages\PySide6\*.exe'), False, True, False);
    DelTree(ExpandConstant('{app}\packages\PySide6\*.lib'), False, True, False);
    DelTree(ExpandConstant('{app}\packages\PySide6\*.pyi'), False, True, False);
    DeleteFile(ExpandConstant('{app}\packages\idna-3.20.dist-info\licenses\LICENSE.md'));
  end;
end;
