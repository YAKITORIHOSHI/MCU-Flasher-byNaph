' ─────────────────────────────────────────────
'  MCU Uploader IDE by Naph — Auto-Bootstrap Launcher
'  Downloads Python if needed, installs deps,
'  and launches the GUI — fully unattended.
'
'  Starts with Administrator permission so environment, junction,
'  driver, and toolchain setup use one consistent Windows token.
' ─────────────────────────────────────────────

Dim fso, shell, scriptDir
Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' ── Require one consistent Administrator token for the complete app chain ──
' If this VBS is already running elevated (for example from a shortcut with
' "Run as administrator" enabled), continue directly without another prompt.
' Otherwise relaunch this exact script through the Windows runas verb.
Dim alreadyElevated, elevatedArgument, argumentValue
alreadyElevated = IsRunningElevated()
elevatedArgument = False
On Error Resume Next
For Each argumentValue In WScript.Arguments
    If LCase(Trim(CStr(argumentValue))) = "/elevated" Or _
       LCase(Trim(CStr(argumentValue))) = "--elevated" Then
        elevatedArgument = True
        Exit For
    End If
Next
Err.Clear
On Error GoTo 0

If (Not alreadyElevated) And (Not elevatedArgument) Then
    Dim shellApp, elevateArgs, launcherRoot, nativeLauncher
    launcherRoot = fso.GetParentFolderName(WScript.ScriptFullName)
    If Not fso.FolderExists(launcherRoot & "\src\modules") Then
        launcherRoot = fso.GetParentFolderName(launcherRoot)
    End If
    nativeLauncher = launcherRoot & "\MCU_Flasher.exe"

    Set shellApp = CreateObject("Shell.Application")
    ' A VBS file cannot carry the application's icon.  Elevating through the
    ' native launcher makes Windows show MCU_Flasher.exe's real icon in
    ' the UAC consent prompt, while the elevated launcher still starts this
    ' same script and preserves the existing bootstrap path.
    If fso.FileExists(nativeLauncher) Then
        elevateArgs = "/vbs-elevated"
        On Error Resume Next
        shellApp.ShellExecute nativeLauncher, elevateArgs, launcherRoot, "runas", 1
    Else
        elevateArgs = Chr(34) & WScript.ScriptFullName & Chr(34) & " /elevated"
        On Error Resume Next
        shellApp.ShellExecute "wscript.exe", elevateArgs, fso.GetParentFolderName(WScript.ScriptFullName), "runas", 1
    End If
    On Error Resume Next
    If shellApp Is Nothing Then
        Err.Raise vbObjectError + 1001, , "Shell.Application is unavailable"
    End If
    If Err.Number <> 0 Then
        MsgBox "MCU Uploader IDE could not obtain Administrator permission." & vbCrLf & vbCrLf & _
               "Please right-click the launcher and choose 'Run as administrator'.", _
               vbCritical, "MCU Uploader IDE — Administrator Permission Required"
        Err.Clear
    End If
    On Error GoTo 0
    WScript.Quit 0
End If


' ── Locate the project root directory (supports running from direct\ or project root) ──
Dim currentFolder
currentFolder = fso.GetParentFolderName(WScript.ScriptFullName)
If fso.FolderExists(currentFolder & "\src\modules") Then
    scriptDir = currentFolder & "\"
ElseIf fso.FolderExists(fso.GetParentFolderName(currentFolder) & "\src\modules") Then
    scriptDir = fso.GetParentFolderName(currentFolder) & "\"
Else
    scriptDir = currentFolder & "\"
End If

' ── Prevent Python from writing or using compiled .pyc bytecode files ──
shell.Environment("PROCESS")("PYTHONDONTWRITEBYTECODE") = "1"

' ── Verify storage drive type (SSD/HDD only; block USB flash drives) ──
Dim driveLetter, driveObj
driveLetter = fso.GetDriveName(scriptDir)
If fso.DriveExists(driveLetter) Then
    On Error Resume Next
    Set driveObj = fso.GetDrive(driveLetter)
    If Err.Number = 0 Then
        If driveObj.DriveType = 1 Then ' 1 = Removable media (USB flash drive / SD card)
            MsgBox "MCU Uploader IDE by Naph cannot be run directly from a USB flash drive or removable disk (" & driveLetter & ")." & vbCrLf & vbCrLf & _
                   "High-speed disk access (SSD/HDD) is required for toolchain compilation and workspace storage." & vbCrLf & vbCrLf & _
                   "Please copy the entire MCU Flasher folder to an internal SSD or HDD (e.g. C:\ or D:\ drive) and launch it from there.", _
                   vbCritical, "MCU Uploader IDE by Naph — Storage Location Notice"
            WScript.Quit 1
        End If
    End If
    Err.Clear
    On Error GoTo 0
End If

' ── Find the launcher script ──
Dim bootstrapFile
bootstrapFile = scriptDir & "src\modules\launcher.py"
If Not fso.FileExists(bootstrapFile) Then
    MsgBox "launcher.py not found in:" & vbCrLf & scriptDir & "src\modules\" & vbCrLf & vbCrLf & _
           "Please make sure launcher.py is in the src\modules folder.", _
           vbCritical, "MCU Uploader IDE by Naph"
    WScript.Quit 1
End If


' ═════════════════════════════════════════════
'  FIND SYSTEM PYTHON (to run bootstrap if needed)
' ═════════════════════════════════════════════
Dim hostPython, systemPython, result
hostPython   = ""
systemPython = ""

' Check for portable python or env python paths
Dim portablePython, envPython, envFolder
portablePython = scriptDir & "src\_python\python.exe"
If Not fso.FileExists(portablePython) Then
    portablePython = scriptDir & "_python\python.exe"
End If
envFolder      = scriptDir & "env"
envPython      = envFolder & "\Scripts\python.exe"

' ── Handle scheduled force rebuild ──
Dim forceRebuildFile
forceRebuildFile = scriptDir & ".force_rebuild"
If fso.FileExists(forceRebuildFile) Then
    On Error Resume Next
    fso.DeleteFile forceRebuildFile, True
    If fso.FolderExists(envFolder) Then
        fso.DeleteFolder envFolder, True
    End If
    On Error GoTo 0
End If

' 1. Check existing project virtual environment (`env`) first
If fso.FileExists(envPython) Then
    ' Never execute a venv whose pyvenv.cfg points back to its own
    ' env\Scripts\python.exe. That malformed state recursively launches the
    ' interpreter and can create thousands of Python processes before the
    ' application can repair itself. Skip it so the real host Python is found
    ' below, then RepairVenvInPlace can rewrite the venv safely.
    If Not VenvConfigNeedsRepair(envFolder) Then
        If IsPythonExeValid(envPython) Then
            hostPython = envPython
        End If
    End If
End If

' 2. Check portable Python next
If hostPython = "" And fso.FileExists(portablePython) Then
    If IsPythonExeValid(portablePython) Then
        hostPython = portablePython
    End If
End If

' 3. Check registry for registered Python installation
If hostPython = "" Then
    hostPython = FindPythonFromRegistry()
End If

' 4. Dynamically scan known Python install directories
If hostPython = "" Then
    Dim localApp, progFiles, progFiles86
    localApp   = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
    progFiles  = shell.ExpandEnvironmentStrings("%ProgramFiles%")
    progFiles86= shell.ExpandEnvironmentStrings("%ProgramFiles(x86)%")

    Dim searchRoots, searchRoot
    searchRoots = Array( _
        localApp & "\Programs\Python", _
        localApp & "\Python", _
        progFiles, _
        progFiles86 _
    )

    For Each searchRoot In searchRoots
        If hostPython <> "" Then Exit For
        If fso.FolderExists(searchRoot) Then
            Dim folder, subfolders
            Set subfolders = fso.GetFolder(searchRoot).SubFolders
            Dim bestFolder, bestName
            bestFolder = ""
            bestName = ""
            For Each folder In subfolders
                Dim fName
                fName = LCase(folder.Name)
                If Left(fName, 6) = "python" Then
                    Dim candidate
                    candidate = folder.Path & "\python.exe"
                    If IsPythonExeValid(candidate) Then
                        If fName > bestName Then
                            bestName = fName
                            bestFolder = candidate
                        End If
                    End If
                End If
            Next
            If bestFolder <> "" Then
                hostPython = bestFolder
            End If
        End If
    Next
End If

' 5. Fall back to `py` launcher via PATH (if valid and not a WindowsApps stub)
If hostPython = "" Then
    Dim resolvedPyLauncher
    resolvedPyLauncher = ResolvePythonExePath("py")
    If resolvedPyLauncher <> "" And resolvedPyLauncher <> "py" Then
        If IsPythonExeValid(resolvedPyLauncher) Then
            hostPython = resolvedPyLauncher
        End If
    End If
End If

' 6. Fall back to `python` command via PATH (if valid and not a WindowsApps stub)
If hostPython = "" Then
    Dim resolvedSysPython
    resolvedSysPython = ResolvePythonExePath("python")
    If resolvedSysPython <> "" And resolvedSysPython <> "python" Then
        If IsPythonExeValid(resolvedSysPython) Then
            hostPython = resolvedSysPython
        End If
    End If
End If

' 6. If no host Python found, install via winget
If hostPython = "" Then
    Dim wingetAvailable
    wingetAvailable = -1
    On Error Resume Next
    wingetAvailable = shell.Run("cmd.exe /c where winget.exe >nul 2>nul", 0, True)
    On Error GoTo 0
    If wingetAvailable <> 0 Then
        MsgBox "Python 3.8 or newer with Tkinter is required, and Windows Package Manager (winget) is not available on this PC." & vbCrLf & vbCrLf & _
               "Install Python for the current user from python.org, include Tcl/Tk, then run this launcher again.", _
               vbCritical, "MCU Uploader IDE by Naph"
        WScript.Quit 1
    End If

    Dim pythonId
    pythonId = GetLatestPythonId()

    Dim msgResult
    msgResult = MsgBox( _
        "Python is not installed on this computer." & vbCrLf & vbCrLf & _
        "MCU Uploader IDE by Naph can install Python automatically using Windows Package Manager (winget)." & vbCrLf & _
        "Package to install: " & pythonId & vbCrLf & vbCrLf & _
        "Click OK to install, or Cancel to exit.", _
        vbOKCancel + vbInformation, "MCU Uploader IDE by Naph — Setup")

    If msgResult = vbCancel Then
        WScript.Quit 0
    End If

    Dim wingetCmd, dlResult
    wingetCmd = "cmd.exe /c winget install --id " & pythonId & " --exact --scope user --override ""/passive Include_tcltk=1 PrependPath=1 Include_test=0"" --accept-package-agreements --accept-source-agreements"
    
    dlResult = shell.Run(wingetCmd, 1, True)

    Dim attempt
    For attempt = 1 To 60
        hostPython = FindPythonFromRegistry()
        localApp   = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
        progFiles  = shell.ExpandEnvironmentStrings("%ProgramFiles%")
        progFiles86= shell.ExpandEnvironmentStrings("%ProgramFiles(x86)%")

        searchRoots = Array( _
            localApp & "\Programs\Python", _
            localApp & "\Python", _
            progFiles, _
            progFiles86 _
        )

        For Each searchRoot In searchRoots
            If hostPython <> "" Then Exit For
            If fso.FolderExists(searchRoot) Then
                Set subfolders = fso.GetFolder(searchRoot).SubFolders
                bestFolder = ""
                bestName = ""
                For Each folder In subfolders
                    fName = LCase(folder.Name)
                    If Left(fName, 6) = "python" Then
                        candidate = folder.Path & "\python.exe"
                        If IsPythonExeValid(candidate) Then
                            If fName > bestName Then
                                bestName = fName
                                bestFolder = candidate
                            End If
                        End If
                    End If
                Next
                If bestFolder <> "" Then
                    hostPython = bestFolder
                End If
            End If
        Next

        If hostPython = "" Then
            result = -1
            On Error Resume Next
            result = shell.Run("py -c ""import encodings""", 0, True)
            On Error GoTo 0
            If result = 0 Then hostPython = "py"
        End If

        If hostPython = "" Then
            result = -1
            On Error Resume Next
            result = shell.Run("python -c ""import encodings""", 0, True)
            On Error GoTo 0
            If result = 0 Then hostPython = "python"
        End If

        If hostPython <> "" Then Exit For
        WScript.Sleep 2000
    Next

    If hostPython = "" Then
        MsgBox "Python installation via winget failed or could not be detected." & vbCrLf & vbCrLf & _
               "Please install Python manually from https://www.python.org", _
               vbCritical, "MCU Uploader IDE by Naph"
        WScript.Quit 1
    End If
End If

' Resolve command aliases ("py" / "python") to an absolute executable path if possible
Dim resolvedHostPython
resolvedHostPython = ResolvePythonExePath(hostPython)

' ── Check and in-place repair existing virtual environment (`env`) ──
If fso.FolderExists(envFolder) And fso.FileExists(envPython) Then
    ' Never repair a venv with the venv interpreter itself. Doing that writes
    ' a self-referential pyvenv.cfg and makes the next Python invocation
    ' recursively spawn. Only a real host/portable Python may repair it.
    If resolvedHostPython <> "" And LCase(resolvedHostPython) <> LCase(envPython) Then
        RepairVenvInPlace envFolder, resolvedHostPython
    End If
    If IsPythonExeValid(envPython) Then
        systemPython = envPython
    Else
        ' Environment could not be salvaged (e.g. incompatible Python major version) — recreate
        QuarantineFolder envFolder
        systemPython = hostPython
    End If
ElseIf fso.FolderExists(envFolder) Then
    QuarantineFolder envFolder
    systemPython = hostPython
Else
    systemPython = hostPython
End If


' ═════════════════════════════════════════════
'  LAUNCH — always via bootstrap
' ═════════════════════════════════════════════
' Bootstrap runs its own Tk GUI window, launched via pythonw.exe if available.
Dim launchPython, pythonwCandidate, runCmd
launchPython = systemPython

If systemPython <> "" And systemPython <> "py" And systemPython <> "python" Then
    pythonwCandidate = fso.GetParentFolderName(systemPython) & "\pythonw.exe"
    If fso.FileExists(pythonwCandidate) Then
        launchPython = pythonwCandidate
    End If
End If

If InStr(launchPython, "\") > 0 Then
    runCmd = """" & launchPython & """ -B """ & bootstrapFile & """ --hidden"
Else
    runCmd = launchPython & " -B """ & bootstrapFile & """ --hidden"
End If

On Error Resume Next
shell.Run runCmd, 1, False
If Err.Number <> 0 Then
    Dim launchErr
    launchErr = Err.Description
    Err.Clear
    MsgBox "MCU Uploader IDE could not start its setup program." & vbCrLf & vbCrLf & _
           "Python: " & launchPython & vbCrLf & _
           "Bootstrap: " & bootstrapFile & vbCrLf & vbCrLf & _
           "Details: " & launchErr, _
           vbCritical, "MCU Uploader IDE by Naph"
End If
On Error GoTo 0


' ═════════════════════════════════════════════
'  HELPERS
' ═════════════════════════════════════════════

' ── Return True when this wscript.exe already has an elevated token ──
Function IsRunningElevated()
    IsRunningElevated = False
    Dim exitCode
    ' fltmc.exe requires an elevated token and is present on supported Windows
    ' installations. It avoids PowerShell policy/profile differences that can
    ' make a split admin token look elevated when this VBS is not.
    On Error Resume Next
    exitCode = shell.Run("cmd.exe /d /c fltmc.exe >nul 2>&1", 0, True)
    If Err.Number = 0 And exitCode = 0 Then IsRunningElevated = True
    Err.Clear
    On Error GoTo 0
End Function

' ── Detect a self-referential/corrupt venv before executing it ──
Function VenvConfigNeedsRepair(venvDir)
    VenvConfigNeedsRepair = False
    Dim cfgPath, ts, cfgText, cfgLower, venvLower, cfgLines, cfgLine, cfgEq, homePath, executablePath, idx
    cfgPath = venvDir & "\pyvenv.cfg"
    If Not fso.FileExists(cfgPath) Then Exit Function

    On Error Resume Next
    Set ts = fso.OpenTextFile(cfgPath, 1)
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        Exit Function
    End If
    cfgText = ts.ReadAll
    ts.Close
    On Error GoTo 0

    cfgLower = LCase(Replace(cfgText, "/", "\"))
    venvLower = LCase(Replace(fso.GetAbsolutePathName(venvDir), "/", "\"))
    If InStr(cfgLower, venvLower & "\scripts") > 0 Then
        VenvConfigNeedsRepair = True
        Exit Function
    End If

    ' A copied venv may still reference a Python installation from another
    ' Windows user or another PC. Detect that before executing the venv so the
    ' normal host-Python discovery can repair it first.
    cfgLines = Split(cfgText, vbCrLf)
    homePath = ""
    executablePath = ""
    For idx = 0 To UBound(cfgLines)
        cfgLine = Trim(cfgLines(idx))
        If InStr(LCase(cfgLine), "home =") = 1 Or InStr(LCase(cfgLine), "home=") = 1 Then
            cfgEq = InStr(cfgLine, "=")
            If cfgEq > 0 Then homePath = Trim(Mid(cfgLine, cfgEq + 1))
        ElseIf InStr(LCase(cfgLine), "executable =") = 1 Or InStr(LCase(cfgLine), "executable=") = 1 Then
            cfgEq = InStr(cfgLine, "=")
            If cfgEq > 0 Then executablePath = Trim(Mid(cfgLine, cfgEq + 1))
        End If
    Next
    If homePath = "" Or Not fso.FolderExists(homePath) Then
        VenvConfigNeedsRepair = True
    ElseIf executablePath = "" Or Not fso.FileExists(executablePath) Then
        VenvConfigNeedsRepair = True
    End If
End Function

' ── Check if a Python executable is valid and working ──
Function IsPythonExeValid(exePath)
    IsPythonExeValid = False
    If Not fso.FileExists(exePath) Then Exit Function

    ' Ignore Windows Store App Execution Alias 0-byte stubs
    If InStr(LCase(exePath), "\windowsapps\") > 0 Then Exit Function
    On Error Resume Next
    If fso.GetFile(exePath).Size < 4096 Then Exit Function
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        Exit Function
    End If
    On Error GoTo 0

    ' Test if the Python executable is actually functional by importing core modules
    Dim runCmd, exitCode
    On Error Resume Next
    If InStr(LCase(exePath), "\env\") > 0 Then
        runCmd = """" & exePath & """ -B -c ""import sys, encodings, pip, tkinter; sys.exit(0 if sys.version_info[:2] >= (3, 8) else 1)"""
    Else
        runCmd = """" & exePath & """ -B -c ""import sys, encodings, tkinter; sys.exit(0 if sys.version_info[:2] >= (3, 8) else 1)"""
    End If
    exitCode = shell.Run(runCmd, 0, True)
    If Err.Number <> 0 Or exitCode <> 0 Then
        Err.Clear
        On Error GoTo 0
        Exit Function
    End If
    On Error GoTo 0

    Dim venvDir, cfgPath, actBatPath, tsBat, batContent, lines, i, line, eqPos, oldPath, foundVenv, absVenvDir, ts, homePath
    venvDir = fso.GetParentFolderName(fso.GetParentFolderName(exePath))
    cfgPath = venvDir & "\pyvenv.cfg"
    
    If fso.FileExists(cfgPath) Then
        actBatPath = venvDir & "\Scripts\activate.bat"
        If fso.FileExists(actBatPath) Then
            foundVenv = False
            absVenvDir = fso.GetAbsolutePathName(venvDir)
            On Error Resume Next
            Set tsBat = fso.OpenTextFile(actBatPath, 1)
            If Err.Number = 0 Then
                batContent = tsBat.ReadAll
                tsBat.Close
                
                lines = Split(batContent, vbCrLf)
                For i = 0 To UBound(lines)
                    line = Trim(lines(i))
                    If InStr(LCase(line), "set ""virtual_env=") = 1 Then
                        eqPos = InStr(line, "=")
                        If eqPos > 0 Then
                            oldPath = Mid(line, eqPos + 1)
                        Else
                            oldPath = ""
                        End If
                        If Right(oldPath, 1) = """" Then oldPath = Left(oldPath, Len(oldPath) - 1)
                        
                        If LCase(oldPath) = LCase(absVenvDir) Then
                            foundVenv = True
                        Else
                            lines(i) = "set ""VIRTUAL_ENV=" & absVenvDir & """"
                            Dim newContent, tsWrite
                            newContent = Join(lines, vbCrLf)
                            Set tsWrite = fso.OpenTextFile(actBatPath, 2, True)
                            If Err.Number = 0 Then
                                tsWrite.Write newContent
                                tsWrite.Close
                                foundVenv = True
                            End If
                        End If
                        Exit For
                    End If
                Next
            End If
            On Error GoTo 0
            If Not foundVenv Then Exit Function
        Else
            Exit Function
        End If

        On Error Resume Next
        Set ts = fso.OpenTextFile(cfgPath, 1)
        If Err.Number = 0 Then
            Do Until ts.AtEndOfStream
                line = Trim(ts.ReadLine)
                If InStr(LCase(line), "home =") = 1 Or InStr(LCase(line), "home=") = 1 Then
                    eqPos = InStr(line, "=")
                    If eqPos > 0 Then
                        homePath = Trim(Mid(line, eqPos + 1))
                    Else
                        homePath = ""
                    End If
                    If Right(homePath, 1) <> "\" Then homePath = homePath & "\"
                    If fso.FolderExists(homePath) Then
                        If fso.FileExists(homePath & "python.exe") Or fso.FileExists(homePath & "pythonw.exe") Then
                            IsPythonExeValid = True
                        End If
                    End If
                    Exit Do
                End If
            Loop
            ts.Close
        End If
        On Error GoTo 0
    Else
        IsPythonExeValid = True
    End If
End Function


' ── Resolve command string ("py" or "python") to absolute executable path ──
Function ResolvePythonExePath(pyCmd)
    ResolvePythonExePath = pyCmd
    If pyCmd = "" Then Exit Function
    If InStr(pyCmd, "\") > 0 Or InStr(pyCmd, "/") > 0 Then Exit Function

    Dim tmpFile, cmd
    tmpFile = shell.ExpandEnvironmentStrings("%TEMP%\py_exec_path.txt")
    cmd = "cmd.exe /c " & pyCmd & " -c ""import sys; print(sys.executable)"" > """ & tmpFile & """"
    On Error Resume Next
    shell.Run cmd, 0, True
    If Err.Number = 0 And fso.FileExists(tmpFile) Then
        Dim ts, line
        Set ts = fso.OpenTextFile(tmpFile, 1)
        If Not ts.AtEndOfStream Then
            line = Trim(ts.ReadLine)
            If fso.FileExists(line) Then ResolvePythonExePath = line
        End If
        ts.Close
        fso.DeleteFile tmpFile
    End If
    On Error GoTo 0
End Function


' ── Repair virtual environment pyvenv.cfg and activate.bat in-place ──
Sub RepairVenvInPlace(venvDir, basePyExe)
    On Error Resume Next
    If Not fso.FolderExists(venvDir) Then Exit Sub
    If Not fso.FileExists(basePyExe) Then Exit Sub

    Dim cfgPath, actBatPath, basePyDir, absVenvDir
    cfgPath = venvDir & "\pyvenv.cfg"
    actBatPath = venvDir & "\Scripts\activate.bat"
    basePyDir = fso.GetParentFolderName(basePyExe)
    absVenvDir = fso.GetAbsolutePathName(venvDir)

    ' 1. Repair pyvenv.cfg
    If fso.FileExists(cfgPath) Then
        Dim ts, cfgText, lines, i, line, updatedCfg
        Set ts = fso.OpenTextFile(cfgPath, 1)
        If Err.Number = 0 Then
            cfgText = ts.ReadAll
            ts.Close
            lines = Split(cfgText, vbCrLf)
            For i = 0 To UBound(lines)
                line = Trim(lines(i))
                If InStr(LCase(line), "home =") = 1 Or InStr(LCase(line), "home=") = 1 Then
                    lines(i) = "home = " & basePyDir
                ElseIf InStr(LCase(line), "executable =") = 1 Or InStr(LCase(line), "executable=") = 1 Then
                    lines(i) = "executable = " & basePyExe
                ElseIf InStr(LCase(line), "base-prefix =") = 1 Or InStr(LCase(line), "base-prefix=") = 1 Then
                    lines(i) = "base-prefix = " & basePyDir
                ElseIf InStr(LCase(line), "base-exec-prefix =") = 1 Or InStr(LCase(line), "base-exec-prefix=") = 1 Then
                    lines(i) = "base-exec-prefix = " & basePyDir
                ElseIf InStr(LCase(line), "base-executable =") = 1 Or InStr(LCase(line), "base-executable=") = 1 Then
                    lines(i) = "base-executable = " & basePyExe
                End If
            Next
            updatedCfg = Join(lines, vbCrLf)
            Dim tsWrite
            Set tsWrite = fso.OpenTextFile(cfgPath, 2, True)
            If Err.Number = 0 Then
                tsWrite.Write updatedCfg
                tsWrite.Close
            End If
        End If
        Err.Clear
    End If

    ' 2. Repair activate.bat
    If fso.FileExists(actBatPath) Then
        Dim tsBat, batContent, batLines, j, bLine, updatedBat
        Set tsBat = fso.OpenTextFile(actBatPath, 1)
        If Err.Number = 0 Then
            batContent = tsBat.ReadAll
            tsBat.Close
            batLines = Split(batContent, vbCrLf)
            For j = 0 To UBound(batLines)
                bLine = Trim(batLines(j))
                If InStr(LCase(bLine), "set ""virtual_env=") = 1 Or InStr(LCase(bLine), "set virtual_env=") = 1 Then
                    batLines(j) = "set ""VIRTUAL_ENV=" & absVenvDir & """"
                    Exit For
                End If
            Next
            updatedBat = Join(batLines, vbCrLf)
            Dim tsBatWrite
            Set tsBatWrite = fso.OpenTextFile(actBatPath, 2, True)
            If Err.Number = 0 Then
                tsBatWrite.Write updatedBat
                tsBatWrite.Close
            End If
        End If
        Err.Clear
    End If
    On Error GoTo 0
End Sub



' ── Dynamically resolve the highest Python 3 version ID from winget ──
Function GetLatestPythonId()
    ' Default fallback
    GetLatestPythonId = "Python.Python.3.14"
    
    Dim tempFile
    tempFile = shell.ExpandEnvironmentStrings("%TEMP%\winget_py_search.txt")
    
    On Error Resume Next
    shell.Run "cmd.exe /c winget search Python.Python > """ & tempFile & """", 0, True
    If Err.Number = 0 And fso.FileExists(tempFile) Then
        Dim ts, line, parts, part, verStr, verVal, highestVer, highestId
        highestVer = 0
        highestId = ""
        Set ts = fso.OpenTextFile(tempFile, 1)
        Do Until ts.AtEndOfStream
            line = Trim(ts.ReadLine)
            If InStr(line, "Python.Python.3.") > 0 Then
                parts = Split(line, " ")
                For Each part In parts
                    part = Trim(part)
                    If InStr(part, "Python.Python.3.") = 1 Then
                        verStr = Mid(part, 17)
                        If IsNumeric(verStr) Then
                            verVal = CInt(verStr)
                            If verVal >= 8 And verVal > highestVer Then
                                highestVer = verVal
                                highestId = part
                            End If
                        End If
                    End If
                Next
            End If
        Loop
        ts.Close
        fso.DeleteFile tempFile
        
        If highestId <> "" Then
            GetLatestPythonId = highestId
        End If
    End If
    On Error GoTo 0
End Function


' ── Search registry database for registered Python 3 installations ──
Function FindPythonFromRegistry()
    FindPythonFromRegistry = ""
    Dim regVersion, regPath, checkKey
    For regVersion = 25 To 8 Step -1
        checkKey = "HKCU\SOFTWARE\Python\PythonCore\3." & regVersion & "\InstallPath\ExecutablePath"
        On Error Resume Next
        regPath = shell.RegRead(checkKey)
        On Error GoTo 0
        If regPath <> "" Then
            If fso.FileExists(regPath) Then
                If IsPythonExeValid(regPath) Then
                    FindPythonFromRegistry = regPath
                    Exit Function
                End If
            End If
        End If
        
        checkKey = "HKLM\SOFTWARE\Python\PythonCore\3." & regVersion & "\InstallPath\ExecutablePath"
        On Error Resume Next
        regPath = shell.RegRead(checkKey)
        On Error GoTo 0
        If regPath <> "" Then
            If fso.FileExists(regPath) Then
                If IsPythonExeValid(regPath) Then
                    FindPythonFromRegistry = regPath
                    Exit Function
                End If
            End If
        End If
    Next
End Function


' Keep incompatible app environments recoverable; never modify a user's
' system Python registration or uninstall records.
' Move an unusable environment out of the active path instead of deleting it.
' This keeps the previous environment recoverable until the new setup works.
Sub QuarantineFolder(folderPath)
    If Not fso.FolderExists(folderPath) Then Exit Sub
    Dim stamp, backupPath, attempt
    stamp = Year(Now) & Right("0" & Month(Now), 2) & Right("0" & Day(Now), 2) & _
            "-" & Right("0" & Hour(Now), 2) & Right("0" & Minute(Now), 2) & Right("0" & Second(Now), 2)
    backupPath = folderPath & ".incompatible-" & stamp
    On Error Resume Next
    For attempt = 1 To 5
        fso.MoveFolder folderPath, backupPath
        If Err.Number = 0 Then Exit For
        Err.Clear
        WScript.Sleep 200
    Next
    On Error GoTo 0
End Sub
