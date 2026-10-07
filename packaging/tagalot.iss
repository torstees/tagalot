; Windows installer for Tagalot (Inno Setup 6; DESIGN.md section 3, "Packaging", #269).
;
;   iscc /DAppVersion=0.5.0 packaging\tagalot.iss
;
; packs the PyInstaller build in dist\tagalot\ into dist\Tagalot-<version>-Windows-setup.exe.
; It installs for the current user (no administrator needed; %LOCALAPPDATA%\Programs\Tagalot)
; unless the user chooses all users. It adds a Start menu entry, optionally a desktop icon, and
; an uninstaller. Uninstalling removes only the program: keeps, settings, and themes stay.

#ifndef AppVersion
  #error Pass the version: iscc /DAppVersion=x.y.z packaging\tagalot.iss
#endif

[Setup]
; The AppId identifies Tagalot to Windows across versions: never change it.
AppId={{F91EDF78-9564-48ED-96B4-5422F6841F4A}
AppName=Tagalot
AppVersion={#AppVersion}
AppVerName=Tagalot {#AppVersion}
AppPublisher=Eric Torstenson
AppPublisherURL=https://github.com/torstees/tagalot
AppSupportURL=https://github.com/torstees/tagalot/issues
DefaultDirName={autopf}\Tagalot
DefaultGroupName=Tagalot
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=Tagalot-{#AppVersion}-Windows-setup
SetupIconFile=tagalot.ico
UninstallDisplayIcon={app}\tagalot.exe
UninstallDisplayName=Tagalot
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; The tower (#361; rendered by scripts/make_icons.py): the side panel of the first and last
; pages, and the other pages' corner, each at 100%, 150%, and 200% scaling.
WizardImageFile=wizard-100.bmp,wizard-150.bmp,wizard-200.bmp
WizardSmallImageFile=wizard-small-100.bmp,wizard-small-150.bmp,wizard-small-200.bmp
; Close a running Tagalot before replacing its files.
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the bundled libraries wholesale: files an older build had and this
; one doesn't must not linger.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\tagalot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Tagalot"; Filename: "{app}\tagalot.exe"
Name: "{autodesktop}\Tagalot"; Filename: "{app}\tagalot.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\tagalot.exe"; Description: "{cm:LaunchProgram,Tagalot}"; Flags: nowait postinstall skipifsilent
