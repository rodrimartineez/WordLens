; Instalador de WordLens (Inno Setup 6).
;
;   ISCC.exe /DAppVersion=1.0.0 packaging\installer.iss
;
; Lo lanza build.ps1 tras generar dist\WordLens con PyInstaller.
; Por defecto instala sólo para el usuario actual (no pide administrador):
;   %LOCALAPPDATA%\Programs\WordLens
; Desde el asistente o con /ALLUSERS se puede instalar en Program Files.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "WordLens"
#define AppExe "WordLens.exe"

[Setup]
AppId={{6F0B7C55-3E5A-4C4B-9D8B-57A2C0E1B7D4}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist\installer
OutputBaseFilename=WordLens-Setup-{#AppVersion}
SetupIconFile=..\assets\wordlens.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; Cierra WordLens si está abierto al actualizar o desinstalar.
AppMutex=WordLens_single_instance
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\WordLens\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "WordLens.WordLens"; Comment: "Learn vocabulary from on-screen subtitles"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "WordLens.WordLens"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; La carpeta de la aplicación sólo contiene archivos de WordLens (los datos del
; usuario viven en %LOCALAPPDATA%\WordLens): se elimina entera, incluido lo que un
; antivirus pudiera tener abierto un instante al desinstalar.
Type: filesandordirs; Name: "{app}"

[CustomMessages]
english.DeleteUserData=Do you also want to delete WordLens settings, logs and backups?
spanish.DeleteUserData=¿Quieres borrar también la configuración, los registros y las copias de seguridad de WordLens?
english.DocumentKept=Your vocabulary document is NOT deleted.
spanish.DocumentKept=Tu documento de vocabulario NO se borra.

[Code]
// Los datos internos (configuración, logs, copias de seguridad) viven en
// %LOCALAPPDATA%\WordLens. Al desinstalar se pregunta si borrarlos; por defecto
// se conservan. El documento de vocabulario del usuario nunca se toca.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\WordLens');
    if DirExists(DataDir) and not UninstallSilent() then
    begin
      if MsgBox(CustomMessage('DeleteUserData') + #13#10 +
                '(' + DataDir + ')' + #13#10#13#10 +
                CustomMessage('DocumentKept'),
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
