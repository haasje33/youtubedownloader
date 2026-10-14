; Inno Setup script for the Windows installer. Built by scripts/build_windows.py:
;   ISCC.exe /DAppVersion=1.2.3 packaging\windows\installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "YouTube Downloader"
#define AppExe "YouTube Downloader.exe"

[Setup]
AppId={{7C2E4B8A-3F61-4D5B-9A0E-2B8D6C1F4E73}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=haasje33
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=YouTube-Downloader-{#AppVersion}-Windows-x64-Setup
SetupIconFile=..\..\build\icon\AppIcon.ico
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern
; In-app updates run this installer with /SILENT /CLOSEAPPLICATIONS /RELAUNCH=1.
CloseApplications=yes
RestartApplications=no
; The CUDA libraries are big; fast LZMA keeps build times reasonable.
Compression=lzma2/fast
SolidCompression=no
MinVersion=10.0

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; Restart the app after an in-app update (as the normal user, not as administrator).
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: ShouldRelaunch

[Code]
function ShouldRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;
