; Inno Setup script for the Asha Windows installer.
; Build (on Windows, after build_windows.ps1 created dist\win\Asha):
;   ISCC.exe /DVersion=0.1.0 prototype\packaging\installer\windows.iss
; Produces: prototype\packaging\dist\Asha-Setup-<ver>.exe  (installs per-user)

#ifndef Version
  #define Version "0.1.0"
#endif

[Setup]
AppName=Asha
AppVersion={#Version}
AppPublisher=Asha
DefaultDirName={localappdata}\Asha
DefaultGroupName=Asha
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir=..\dist
OutputBaseFilename=Asha-Setup-{#Version}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; per-user install: no admin prompt, writes under %LOCALAPPDATA%
PrivilegesRequired=lowest

[Files]
Source: "..\dist\win\Asha\*"; DestDir: "{app}"; \
  Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Asha"; Filename: "{app}\Asha.exe"
Name: "{group}\Uninstall Asha"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Asha"; Filename: "{app}\Asha.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\Asha.exe"; Description: "Launch Asha"; \
  Flags: nowait postinstall skipifsilent
