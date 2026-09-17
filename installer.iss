; ────────────────────────────────────────────────────────────────────
;  Dual-Router Dashboard — Inno Setup installer script (optional)
;  Build with Inno Setup Compiler (https://jrsoftware.org/isinfo.php):
;      iscc installer.iss
;  Output: dist\Output\DualRouterDashboardSetup-<version>.exe
; ────────────────────────────────────────────────────────────────────

#define MyAppName "Dual-Router Dashboard"
#define MyAppVersion "2.0.0"
#define MyAppExeName "DualRouterDashboard.exe"

[Setup]
AppId={{7C1B2E44-8A3C-4E72-9B6F-DUALROUTER001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Home Network Tools
DefaultDirName={autopf}\DualRouterDashboard
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=dist\Output
OutputBaseFilename=DualRouterDashboardSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Files]
Source: "dist\DualRouterDashboard.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "app_icon.ico"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app_icon.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app_icon.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
