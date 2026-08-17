; installer.iss — Inno Setup 6 script for GridBot.
; Compile:  ISCC.exe installer.iss   (paths below are relative to this file)
; Output:   dist\GridBot-Setup.exe
;
; PrivilegesRequired=lowest + PrivilegesRequiredOverridesAllowed=dialog:
; without admin rights {autopf} resolves to {localappdata}\Programs, so the
; app installs per-user; with elevation it goes to Program Files.

#define MyAppName "GridBot"
#define MyAppVersion "1.3.0"
#define MyAppPublisher "MaxArti"
#define MyAppExeName "GridBot.exe"

[Setup]
; Fixed AppId — never change between versions, it identifies the app to the
; uninstaller and upgrade logic.
AppId={{BC10576B-1160-4278-B28E-E4B112B68E37}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; 64-bit exe: an elevated install must land in Program Files, not (x86)
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
OutputDir=dist
OutputBaseFilename=GridBot-Setup
SetupIconFile=gridbot.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}

[Languages]
; First entry is the fallback (English); Russian is auto-selected on Russian
; locales.
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
; Desktop shortcut — offered as a checkbox, enabled by default (no 'unchecked'
; flag). Start Menu shortcut is created unconditionally in [Icons].
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

; NOTE (intentional): there is NO [UninstallDelete] section for user data.
; GridBot stores state/logs/config in {localappdata}\GridBot — that folder
; belongs to the user and MUST survive uninstall. Do not add it here.
