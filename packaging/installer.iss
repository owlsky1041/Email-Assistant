; Inno Setup 脚本 —— Windows 安装程序
;
; 用法（在 Windows 上，先跑完 build_windows.ps1 生成 dist\email-assistant）：
;
;   ISCC.exe /DMyAppVersion=0.1.0 packaging\installer.iss
;
; 设计要点
; --------
; * 默认安装到 {autopf}（Program Files），需要管理员权限；
;   若想免安装到用户目录，把 PrivilegesRequired 改成 lowest 并把
;   DefaultDirName 改为 {localappdata}\Programs\邮件管理助手。
; * 用户数据（config / data / logs）默认放在安装目录下，符合绿色版习惯。
;   卸载时**询问**是否删除数据 —— 邮件归档是用户的资产，不能默认删掉。
; * 不打包 ONNX 模型（95MB）。安装后用
;   `email-assistant.exe model import <目录>` 或 `model download --url <地址>` 获取。

#define MyAppName "邮件管理助手"
#define MyAppNameEn "Email Assistant"
#define MyAppPublisher "Email Assistant"
#define MyAppExeName "email-assistant-tray.exe"
#define MyAppCliName "email-assistant.exe"

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

[Setup]
AppId={{8F3C1A72-4B6D-4E19-9C2A-7D5E8B1F0A34}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename={#MyAppName}-{#MyAppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
; 安装包本身较大（300MB+），关闭不必要的动画提示
WizardStyle=modern
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; 卸载时保留用户数据（由 [Code] 段询问）
UninstallDisplayName={#MyAppName}

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"
Name: "startupicon"; Description: "开机自动启动（托盘常驻）"; GroupDescription: "附加任务:"

[Files]
; PyInstaller onedir 产物整体拷入
Source: "..\dist\email-assistant\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\知识库 API 文档"; Filename: "http://127.0.0.1:8990/docs"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
; 安装完成后引导用户初始化（生成 config\config.yaml）
Filename: "{cmd}"; Parameters: "/k ""{app}\{#MyAppCliName}"" init"; \
  Description: "初始化配置（填写邮箱地址与授权码）"; Flags: postinstall nowait skipifsilent
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; \
  Flags: postinstall nowait skipifsilent unchecked

[UninstallDelete]
; 只删除程序自身产生的缓存；config/data/logs 交给 [Code] 段询问
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\{#MyAppCliName}"
Type: files; Name: "{app}\{#MyAppExeName}"

[Code]
var
  RemoveData: Boolean;

function InitializeUninstall(): Boolean;
var
  Answer: Integer;
begin
  Result := True;
  { 邮件归档是用户资产，默认保留，只询问是否一并删除 }
  Answer := MsgBox('是否同时删除邮件归档、数据库与配置？' + #13#10 + #13#10 +
                   '选择「否」将保留 config\、data\、logs\ 目录，' + #13#10 +
                   '以便重新安装后继续使用已有归档。',
                   mbConfirmation, MB_YESNO or MB_DEFBUTTON2);
  RemoveData := (Answer = IDYES);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and RemoveData then
  begin
    DelTree(ExpandConstant('{app}\data'), True, True, True);
    DelTree(ExpandConstant('{app}\logs'), True, True, True);
    DelTree(ExpandConstant('{app}\config'), True, True, True);
  end;
end;
