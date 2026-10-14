; Inno Setup script for the Windows installer. Built by scripts/build_windows.py:
;   ISCC.exe /DAppVersion=1.2.3 packaging\windows\installer.iss
;
; The NVIDIA package (CUDA, cuDNN, TensorRT) is not inside the installer. On PCs
; with an NVIDIA GPU the installer downloads it (details in build\nvidia_pack.iss,
; written by the build) and lets the app unpack it into
; %ProgramData%\YouTube Downloader\nvidia\<version>.
; Command line: /NVIDIAPACK=skip|force   /NVIDIABASEURL=<url ending in />  (for tests)

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "YouTube Downloader"
#define AppExe "YouTube Downloader.exe"
#define CliExe "youtube-downloader-cli.exe"
#if FileExists(AddBackslash(SourcePath) + "..\..\build\nvidia_pack.iss")
  #define HavePack
  #include "..\..\build\nvidia_pack.iss"
#endif

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
Compression=lzma2/max
SolidCompression=yes
MinVersion=10.0

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
#ifdef HavePack
Name: "nvidiapack"; Description: "Download NVIDIA support for fast AI upscaling ({#NvidiaPackSizeMB} MB)"; GroupDescription: "NVIDIA graphics card:"; Check: OfferPack
#endif

[Dirs]
; The app downloads the NVIDIA package here later if it isn't installed now.
Name: "{commonappdata}\{#AppName}"; Permissions: users-modify
Name: "{commonappdata}\{#AppName}\nvidia"; Permissions: users-modify

[InstallDelete]
; Start each install or update with a clean app folder (older versions bundled CUDA here).
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; The AppUserModelID matches the app's, so notifications appear under its name.
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "YouTubeDownloader.App"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; Restart the app after an in-app update (as the normal user, not as administrator).
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: ShouldRelaunch

[UninstallDelete]
Type: filesandordirs; Name: "{commonappdata}\{#AppName}\nvidia"
Type: dirifempty; Name: "{commonappdata}\{#AppName}"

[Code]
function ShouldRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

#ifdef HavePack
var
  DownloadPage: TDownloadWizardPage;
  PackDownloaded: Boolean;

function PackMode: String;
begin
  Result := Lowercase(ExpandConstant('{param:NVIDIAPACK|auto}'));
end;

function PackInstalled: Boolean;
begin
  Result := FileExists(ExpandConstant('{commonappdata}\{#AppName}\nvidia\{#NvidiaPackVersion}\.complete'));
end;

function HasNvidiaGpu: Boolean;
begin
  { Installed into System32 by every current NVIDIA driver. }
  Result := FileExists(ExpandConstant('{sys}\nvidia-smi.exe'));
end;

function OfferPack: Boolean;
begin
  if PackMode = 'skip' then
    Result := False
  else if PackMode = 'force' then
    Result := True
  else
    Result := HasNvidiaGpu and not PackInstalled;
end;

function PackUrl(I: Integer): String;
begin
  Result := ExpandConstant('{param:NVIDIABASEURL|}');
  if Result = '' then
    Result := '{#NvidiaPackBaseUrl}';
  Result := Result + PackPartName(I);
end;

function PackFile(I: Integer): String;
begin
  Result := ExpandConstant('{tmp}\') + PackPartName(I);
end;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  Result := True;
end;

function OnSilentDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  WizardForm.FilenameLabel.Caption := FileName + ': ' + IntToStr(Progress div 1000000) + ' of ' +
    IntToStr(ProgressMax div 1000000) + ' MB';
  Result := True;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage('Downloading NVIDIA support',
    'CUDA, cuDNN and TensorRT for fast AI upscaling on your NVIDIA graphics card.', @OnDownloadProgress);
end;

function DownloadPack: Boolean;
var
  I: Integer;
begin
  Result := False;
  if WizardSilent then begin
    { During silent installs (in-app updates) only the progress window is visible. }
    WizardForm.StatusLabel.Caption := 'Downloading NVIDIA support...';
    try
      for I := 0 to {#NvidiaPackParts} - 1 do
        DownloadTemporaryFile(PackUrl(I), PackPartName(I), PackPartSha256(I), @OnSilentDownloadProgress);
      Result := True;
    except
      Log('NVIDIA package download failed: ' + GetExceptionMessage);
    end;
    exit;
  end;
  DownloadPage.Clear;
  for I := 0 to {#NvidiaPackParts} - 1 do
    DownloadPage.Add(PackUrl(I), PackPartName(I), PackPartSha256(I));
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
      Result := True;
    except
      if not DownloadPage.AbortedByUser then
        SuppressibleMsgBox('Downloading NVIDIA support failed: ' + GetExceptionMessage + #13#10#13#10 +
          'The app will be installed without it. You can download it later in the app.',
          mbInformation, MB_OK, IDOK);
    end;
  finally
    DownloadPage.Hide;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and not WizardSilent and WizardIsTaskSelected('nvidiapack') then
    PackDownloaded := DownloadPack;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  I, ResultCode: Integer;
  Args: String;
begin
  if CurStep <> ssPostInstall then
    exit;
  if WizardSilent and WizardIsTaskSelected('nvidiapack') then
    PackDownloaded := DownloadPack;
  if not PackDownloaded then
    exit;
  WizardForm.StatusLabel.Caption := 'Installing NVIDIA support...';
  Args := '--install-nvidia-pack';
  for I := 0 to {#NvidiaPackParts} - 1 do
    Args := Args + ' "' + PackFile(I) + '"';
  if not Exec(ExpandConstant('{app}\{#CliExe}'), Args, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
     or (ResultCode <> 0) then
    SuppressibleMsgBox('NVIDIA support could not be installed (error ' + IntToStr(ResultCode) + '). ' +
      'You can download it later in the app.', mbInformation, MB_OK, IDOK);
  for I := 0 to {#NvidiaPackParts} - 1 do
    DeleteFile(PackFile(I));
end;
#endif
