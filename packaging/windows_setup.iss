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
SetupIconFile=..\branding\NetworkWatch.ico
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
Name: "{group}\{cm:BackgroundInstall}"; Filename: "{app}\Install background.cmd"; WorkingDir: "{app}"
Name: "{group}\{cm:BackgroundRemove}"; Filename: "{app}\Remove background.cmd"; WorkingDir: "{app}"

[CustomMessages]
english.BackgroundInstall=Install or update background engine
english.BackgroundRemove=Remove background engine
russian.BackgroundInstall=Установить или обновить фоновый движок
russian.BackgroundRemove=Удалить фоновый движок

[Run]
Filename: "{app}\LinkRadar.exe"; Description: "{cm:LaunchProgram,LinkRadar}"; Flags: nowait postinstall skipifsilent
