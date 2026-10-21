' Portable quiet Windows entry. Keep LAUNCHER inside the application folder.
Option Explicit
Dim fs, shell, entry, host, command, argument, architecture, launchError
Set fs = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
architecture = shell.Environment("PROCESS")("PROCESSOR_ARCHITEW6432")
If Len(architecture) = 0 Then architecture = shell.Environment("PROCESS")("PROCESSOR_ARCHITECTURE")
If UCase(architecture) <> "AMD64" Then
    MsgBox "This release provides MCU Flasher for Windows AMD64. Native Windows ARM64 support is not yet available.", vbCritical, "MCU Flasher - Platform unavailable"
    WScript.Quit 1
End If
entry = fs.BuildPath(fs.GetParentFolderName(fs.GetParentFolderName(WScript.ScriptFullName)), "direct\windows\run.vbs")
host = fs.BuildPath(fs.GetSpecialFolder(1), "wscript.exe")
If Not fs.FileExists(entry) Then
    MsgBox "Cannot locate direct\windows\run.vbs. Keep the whole LAUNCHER folder inside the complete MCU Flasher application folder.", vbCritical, "MCU Flasher"
    WScript.Quit 1
End If
command = QuoteWindowsArgument(host) & " //nologo " & QuoteWindowsArgument(entry)
For Each argument In WScript.Arguments
    command = command & " " & QuoteWindowsArgument(argument)
Next
' Run expands environment names once. Substitute each percent from a private
' process variable so literal paths/arguments such as %TEMP% stay literal.
shell.Environment("PROCESS")("MCU_FLASHER_LITERAL_PERCENT") = "%"
command = Replace(command, "%", "%MCU_FLASHER_LITERAL_PERCENT%")
On Error Resume Next
launchError = shell.Run(command, 0, True)
If Err.Number <> 0 Then
    MsgBox "MCU Flasher could not start its Windows entry: " & Err.Description, vbCritical, "MCU Flasher"
    WScript.Quit 1
End If
On Error GoTo 0
WScript.Quit launchError

' Preserve empty arguments, embedded quotes and trailing backslashes using the
' Windows argv convention. No cmd.exe or shell command interpolation is used.
Function QuoteWindowsArgument(value)
    Dim quoted, slashes, index, character
    quoted = Chr(34)
    slashes = 0
    For index = 1 To Len(value)
        character = Mid(value, index, 1)
        If character = "\" Then
            slashes = slashes + 1
        ElseIf character = Chr(34) Then
            quoted = quoted & String(slashes * 2 + 1, "\") & Chr(34)
            slashes = 0
        Else
            quoted = quoted & String(slashes, "\") & character
            slashes = 0
        End If
    Next
    QuoteWindowsArgument = quoted & String(slashes * 2, "\") & Chr(34)
End Function
