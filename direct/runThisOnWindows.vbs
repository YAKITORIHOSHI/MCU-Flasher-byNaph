' ─────────────────────────────────────────────
'  MCU Flasher by Naph — Auto-Bootstrap Launcher
'  Downloads Python if needed, installs deps,
'  and launches the GUI — fully unattended.
'
'  Starts with the current user token. Bootstrap requests elevation only for
'  a specific machine-level component when that component actually needs it.
' ─────────────────────────────────────────────

Dim fso, shell, scriptDir
Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

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

' ── Verify storage drive type (SSD/HDD only; block USB flash drives) ──
Dim driveLetter, driveObj
driveLetter = fso.GetDriveName(scriptDir)
If fso.DriveExists(driveLetter) Then
    On Error Resume Next
    Set driveObj = fso.GetDrive(driveLetter)
    If Err.Number = 0 Then
        If driveObj.DriveType = 1 Then ' 1 = Removable media (USB flash drive / SD card)
            MsgBox "MCU Flasher by Naph cannot be run directly from a USB flash drive or removable disk (" & driveLetter & ")." & vbCrLf & vbCrLf & _
                   "High-speed disk access (SSD/HDD) is required for toolchain compilation and workspace storage." & vbCrLf & vbCrLf & _
                   "Please copy the entire MCU Flasher folder to an internal SSD or HDD (e.g. C:\ or D:\ drive) and launch it from there.", _
                   vbCritical, "MCU Flasher by Naph — Storage Location Notice"
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
           vbCritical, "MCU Flasher by Naph"
    WScript.Quit 1
End If


' ═════════════════════════════════════════════
'  FIND PRIVATE RUNTIME — src\_python\python.exe ONLY
'  No fallbacks. No system Python. No winget install.
'  If the private runtime is missing, show a clear error and exit.
' ═════════════════════════════════════════════
Dim hostPython, systemPython
hostPython   = ""
systemPython = ""

' Resolve private Python path (relative to project root)
Dim portablePython, envPython, envFolder
portablePython = scriptDir & "src\_python\python.exe"
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

' 1. Validate private Python runtime exists and is functional; auto-heal if missing or broken
If (Not fso.FileExists(portablePython)) Or (Not IsPythonExeValid(portablePython)) Then
    If AutoHealPrivatePython(scriptDir) Then
        If Not IsPythonExeValid(portablePython) Then
            MsgBox "MCU Flasher attempted to auto-heal the private Python runtime, but verification failed." & vbCrLf & vbCrLf & _
                   "Affected file:" & vbCrLf & _
                   "  " & portablePython & vbCrLf & vbCrLf & _
                   "Please reinstall the MCU Flasher application to restore the bundled Python runtime.", _
                   vbCritical, "MCU Flasher by Naph — Runtime Invalid"
            WScript.Quit 1
        End If
    Else
        If Not fso.FileExists(portablePython) Then
            MsgBox "MCU Flasher private Python runtime not found and could not be auto-healed." & vbCrLf & vbCrLf & _
                   "Expected location:" & vbCrLf & _
                   "  " & portablePython & vbCrLf & vbCrLf & _
                   "The bundled Python runtime at src\_python\ is required to run MCU Flasher." & vbCrLf & _
                   "Please ensure the full project folder is intact (not missing src\_python\)." & vbCrLf & vbCrLf & _
                   "Do NOT install a system Python — this application uses its own isolated runtime.", _
                   vbCritical, "MCU Flasher by Naph — Runtime Missing"
            WScript.Quit 1
        Else
            MsgBox "MCU Flasher private Python runtime appears corrupt or incompatible." & vbCrLf & vbCrLf & _
                   "Affected file:" & vbCrLf & _
                   "  " & portablePython & vbCrLf & vbCrLf & _
                   "Please reinstall the MCU Flasher application to restore the bundled Python runtime.", _
                   vbCritical, "MCU Flasher by Naph — Runtime Invalid"
            WScript.Quit 1
        End If
    End If
End If

hostPython   = portablePython
systemPython = portablePython

' ── Check and in-place repair existing virtual environment (`env`) ──
If fso.FolderExists(envFolder) And fso.FileExists(envPython) Then
    RepairVenvInPlace envFolder, portablePython
End If


' ═════════════════════════════════════════════
'  LAUNCH — always via private Python runtime (src\_python)
' ═════════════════════════════════════════════
' Strictly uses the bundled runtime at src\_python\ — never user PC Python.
Dim launchPython, pythonwCandidate, runCmd
launchPython = portablePython

pythonwCandidate = scriptDir & "src\_python\pythonw.exe"
If fso.FileExists(pythonwCandidate) Then
    launchPython = pythonwCandidate
End If

Dim argIdx, allArgs
allArgs = ""
For argIdx = 0 To WScript.Arguments.Count - 1
    allArgs = allArgs & " """ & WScript.Arguments(argIdx) & """"
Next

If InStr(launchPython, "\") > 0 Then
    runCmd = """" & launchPython & """ """ & bootstrapFile & """ --hidden" & allArgs
Else
    runCmd = launchPython & " """ & bootstrapFile & """ --hidden" & allArgs
End If

On Error Resume Next
shell.Run runCmd, 1, False
If Err.Number <> 0 Then
    Dim launchErr
    launchErr = Err.Description
    Err.Clear
    MsgBox "MCU Flasher could not start its setup program." & vbCrLf & vbCrLf & _
           "Python: " & launchPython & vbCrLf & _
           "Bootstrap: " & bootstrapFile & vbCrLf & vbCrLf & _
           "Details: " & launchErr, _
           vbCritical, "MCU Flasher by Naph"
End If
On Error GoTo 0


' ═════════════════════════════════════════════
'  HELPERS
' ═════════════════════════════════════════════

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
        runCmd = """" & exePath & """ -B -c ""import sys, encodings, pip; sys.exit(0 if sys.version_info[:2] >= (3, 8) else 1)"""
    Else
        runCmd = """" & exePath & """ -B -c ""import sys, encodings; sys.exit(0 if sys.version_info[:2] >= (3, 8) else 1)"""
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


' ── Resolve command string to absolute executable path (never queries system Python) ──
Function ResolvePythonExePath(pyCmd)
    ResolvePythonExePath = pyCmd
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


' ── Auto-heal private Python runtime from bundled offline installer ──
Function AutoHealPrivatePython(rootDir)
    AutoHealPrivatePython = False
    Dim handsoffCandidates, hDir, candidate, installerExe, f, subF, targetDir, logDir, logFile, runCmd, exitCode
    handsoffCandidates = Array( _
        rootDir & "installers\.handsoff", _
        fso.GetParentFolderName(rootDir) & "\installers\.handsoff" _
    )

    installerExe = ""
    For Each hDir In handsoffCandidates
        If fso.FolderExists(hDir) Then
            Set subF = fso.GetFolder(hDir)
            For Each f In subF.Files
                If LCase(fso.GetExtensionName(f.Name)) = "exe" And InStr(LCase(f.Name), "python-") = 1 Then
                    installerExe = f.Path
                    Exit For
                End If
            Next
            If installerExe <> "" Then Exit For
        End If
    Next

    If installerExe = "" Then Exit Function

    targetDir = rootDir & "src\_python"
    logDir = rootDir & "logs"
    If Not fso.FolderExists(logDir) Then
        On Error Resume Next
        fso.CreateFolder logDir
        On Error GoTo 0
    End If
    logFile = logDir & "\python_heal_vbs.log"

    ' Remove hidden attribute and clean target directory if partially broken
    If fso.FolderExists(targetDir) Then
        On Error Resume Next
        shell.Run "attrib -h """ & targetDir & """", 0, True
        fso.DeleteFolder targetDir, True
        On Error GoTo 0
    End If

    runCmd = """" & installerExe & """ /quiet TargetDir=""" & targetDir & """ InstallAllUsers=0 PrependPath=0 Include_test=0 Include_doc=0 Include_launcher=0 /log """ & logFile & """"
    On Error Resume Next
    exitCode = shell.Run(runCmd, 0, True)
    If Err.Number = 0 And exitCode = 0 Then
        shell.Run "attrib +h """ & targetDir & """", 0, True
        If fso.FileExists(targetDir & "\python.exe") Then
            AutoHealPrivatePython = True
        End If
    End If
    On Error GoTo 0
End Function

