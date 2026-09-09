; Inno Setup script for the PyBrowser Windows installer.
;
; Build with (from Windows, after packaging/windows/build.ps1 has produced
; dist\PyBrowser\PyBrowser.exe):
;   iscc packaging\windows\installer.iss
;
; Produces packaging\windows\output\PyBrowserSetup-<version>.exe.
;
; Requires Inno Setup 6: https://jrsoftware.org/isinfo.php

#define MyAppName "PyBrowser"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "AiBrowserTest"
#define MyAppExeName "PyBrowser.exe"
#define MyAppURL "https://github.com/Jeremydabuilder/Ai-Browser-Test"

[Setup]
AppId={{B4C1E9E4-6B7B-4B1A-9C8B-8B6D6E9B7B1E}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
; Per-user install by default: no admin prompt, and it keeps the installer
; from ever needing write access outside the user's own account. PyBrowser's
; own data (app/config.py:user_data_dir) already lives under %LOCALAPPDATA%,
; entirely separate from wherever this installs the program files.
DefaultDirName={autopf}\PyBrowser
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=output
OutputBaseFilename=PyBrowserSetup-{#MyAppVersion}
SetupIconFile=..\common\icons\pybrowser.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
; No code-signing certificate is applied here - see packaging/SIGNING.md.
; An unsigned installer will trigger a Windows SmartScreen warning on first
; run; that is expected until this build is actually signed.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; PyInstaller's --onedir output: the exe plus every DLL/resource it needs,
; Qt WebEngine included. Recurse it wholesale rather than listing files -
; the Qt/Chromium resource set is large and version-dependent, and hand-
; enumerating it is exactly the kind of one-off list that silently goes
; stale the next time PySide6 is upgraded.
Source: "..\..\dist\PyBrowser\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The installed program files only. PyBrowser's user data (history,
; bookmarks, downloads, the Qt profile) lives under %LOCALAPPDATA%\PyBrowser
; and is deliberately NOT touched by uninstall - removing the program should
; not silently delete someone's browsing history and saved Missions.
Type: filesandordirs; Name: "{app}"
