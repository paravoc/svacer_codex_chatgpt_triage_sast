Option Explicit

Dim shell, fso, rootDirectory, bootstrap, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

rootDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
bootstrap = fso.BuildPath(rootDirectory, "app\bootstrap.ps1")

If Not fso.FileExists(bootstrap) Then
    MsgBox "Svacer Triage cannot start because app\bootstrap.ps1 is missing.", 16, "Svacer Triage"
    WScript.Quit 2
End If

command = "powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden " & _
          "-ExecutionPolicy Bypass -File """ & bootstrap & """"
shell.Run command, 0, False
