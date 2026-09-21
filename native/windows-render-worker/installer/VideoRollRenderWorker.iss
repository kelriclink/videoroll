#define AppName "VideoRoll Render Worker"
#define AppVersion "0.1.0"
#define AppPublisher "VideoRoll"
#define AppExeName "VideoRollRenderWorker.exe"

[Setup]
AppId={{8F671DC2-96F6-4D0A-AF87-0CDA9D744B65}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={userpf}\VideoRoll Render Worker
DefaultGroupName=VideoRoll
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\..\dist
OutputBaseFilename=VideoRollRenderWorker-Setup-x64
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#AppExeName}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoCompany={#AppPublisher}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Dirs]
Name: "{app}\bin"
Name: "{app}\config"
Name: "{app}\logs"
Name: "{app}\cache"
Name: "{app}\work"

[Files]
Source: "..\..\..\dist\native-windows-render-worker\VideoRollRenderWorker.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\..\dist\native-windows-render-worker\bin\ffmpeg.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "..\..\..\dist\native-windows-render-worker\bin\ffprobe.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "..\..\..\dist\native-windows-render-worker\README.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\VideoRoll Render Worker"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{userdesktop}\VideoRoll Render Worker"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch VideoRoll Render Worker"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\config"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\cache"
Type: filesandordirs; Name: "{app}\work"
