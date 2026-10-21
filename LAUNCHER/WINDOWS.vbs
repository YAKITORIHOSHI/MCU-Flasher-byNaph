' Portable quiet Windows entry. Keep LAUNCHER inside the application folder.
Option Explicit
Dim fs, shell, entry, host, command, architecture, launchError, launchNumber, launchDescription
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
command = QuoteWshPath(host) & " //nologo " & QuoteWshPath(entry) & " --mcu-flasher-wsh-arguments"
' Run expands environment names once. Substitute each percent from a private
' process variable so literal paths/arguments such as %TEMP% stay literal.
shell.Environment("PROCESS")("MCU_FLASHER_LITERAL_PERCENT") = "%"
command = Replace(command, "%", "%MCU_FLASHER_LITERAL_PERCENT%")
On Error Resume Next
Err.Clear
StoreForwardedArguments WScript.Arguments, shell.Environment("PROCESS")
If Err.Number = 0 Then launchError = shell.Run(command, 0, True)
launchNumber = Err.Number
launchDescription = Err.Description
ClearLauncherArgumentPayload shell.Environment("PROCESS")
If launchNumber <> 0 Then
    MsgBox "MCU Flasher could not start its Windows entry: " & launchDescription, vbCritical, "MCU Flasher"
    WScript.Quit 1
End If
On Error GoTo 0
WScript.Quit launchError

' WSH does not use Python/CRT argument parsing. Only filesystem paths (which
' cannot contain double quotes) enter its command line. Argument values travel
' unchanged in this process and its child, never account/system environment.
Function QuoteWshPath(value)
    QuoteWshPath = Chr(34) & value & Chr(34)
End Function

Sub StoreForwardedArguments(arguments, environment)
    Dim index, units
    ClearLauncherArgumentPayload environment
    If arguments.Count > 1024 Then Err.Raise vbObjectError + 71, "MCU Flasher", "Too many launcher arguments (maximum 1024)."
    units = 0
    For index = 0 To arguments.Count - 1
        If Len(arguments(index)) > 32763 Then Err.Raise vbObjectError + 71, "MCU Flasher", "A launcher argument is too long."
        units = units + Len(arguments(index)) + 1
        If units > 32767 Then Err.Raise vbObjectError + 71, "MCU Flasher", "Launcher arguments are too long."
    Next
    For index = 0 To arguments.Count - 1
        ' The prefix keeps empty values present and distinguishes missing fields.
        environment("MCU_FLASHER_WSH_ARG_" & CStr(index)) = "v1:" & arguments(index)
    Next
    environment("MCU_FLASHER_WSH_ARG_COUNT") = CStr(arguments.Count)
End Sub

Sub ClearLauncherArgumentPayload(environment)
    Dim entry, separator, name, names(), count, index
    count = 0
    ReDim names(environment.Count)
    For Each entry In environment
        separator = InStr(entry, "=")
        If separator > 1 Then
            name = Left(entry, separator - 1)
            If Left(UCase(name), 20) = "MCU_FLASHER_WSH_ARG_" Then
                names(count) = name
                count = count + 1
            End If
        End If
    Next
    For index = 0 To count - 1
        environment.Remove names(index)
    Next
End Sub
