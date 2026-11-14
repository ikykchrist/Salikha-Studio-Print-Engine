#define InstallerPassword GetEnv("SALIKHA_INSTALLER_PASSWORD")
#if InstallerPassword == ""
  #error Set SALIKHA_INSTALLER_PASSWORD before compiling the installer.
#endif
#define BuildDir "protected-dist\win32\Salikha Photoman"
#if FileExists(BuildDir + "\Salikha Photoman.exe")
  #define BuildExe "Salikha Photoman.exe"
#else
  #define BuildExe "Salikha Photoman Pro .exe"
#endif

[Setup]
AppName=Salikha Studio Pro
AppVersion=2.1.0
AppPublisher=Salikha
AppPublisherURL=https://salikha.com
AppSupportURL=https://salikha.com
AppUpdatesURL=https://salikha.com
DefaultDirName={autopf}\Salikha Studio Pro
DefaultGroupName=Salikha Studio Pro
AllowNoIcons=yes
OutputDir=installer
OutputBaseFilename=SalikhaStudioPro_Setup_v2.1.0
SetupIconFile=Salikha Photoman Ico.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
Password={#InstallerPassword}
LicenseFile=license.txt
DisableWelcomePage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked; OnlyBelowVersion: 6.1; Check: not IsAdminInstallMode

[Files]
Source: "{#BuildDir}\{#BuildExe}"; DestDir: "{app}"; DestName: "Salikha Photoman.exe"; Flags: ignoreversion
Source: "{#BuildDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Salikha Studio Pro"; Filename: "{app}\Salikha Photoman.exe"
Name: "{group}\Uninstall Salikha Studio Pro"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Salikha Studio Pro"; Filename: "{app}\Salikha Photoman.exe"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\Salikha Studio Pro"; Filename: "{app}\Salikha Photoman.exe"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\Salikha Photoman.exe"; Description: "{cm:LaunchProgram,Salikha Studio Pro}"; Flags: nowait postinstall skipifsilent
