' Compatible Windows entry point; implementation lives in windows/run.vbs.
Option Explicit
Dim fs, ws, entry, command, argument
Set fs = CreateObject("Scripting.FileSystemObject")
Set ws = CreateObject("WScript.Shell")
entry = fs.BuildPath(fs.GetParentFolderName(WScript.ScriptFullName), "windows\run.vbs")
command = "wscript.exe //nologo """ & entry & """"
For Each argument In WScript.Arguments
    command = command & " """ & Replace(argument, """", """""") & """"
Next
WScript.Quit ws.Run(command, 0, True)
