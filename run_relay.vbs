' workmom - local relay (port from config.json relay_port, default 18793), hidden background
Option Explicit

Dim fso, base, py, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)

py = ResolvePy()
If py = "" Then
    Die "pythonw.exe not found: put its full path in pythonw.txt, or install Python 3.9+"
End If

Dim sh, q
q = Chr(34)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = base
' Do NOT pass a port here: server.py reads relay_port from config.json itself.
' (It used to be hardcoded " 18793", so editing relay_port had no effect.)
cmd = q & py & q & " " & q & base & "\server.py" & q
sh.Run cmd, 0, False


' ---- helpers ----
Function ResolvePy()
    Dim sh, p, f, t, dirs, d, i
    Set sh = CreateObject("WScript.Shell")
    p = sh.ExpandEnvironmentStrings("%PATH%")
    f = base & "\pythonw.txt"
    If fso.FileExists(f) Then
        t = Trim(fso.OpenTextFile(f, 1).ReadLine)
        If t <> "" And fso.FileExists(t) Then
            ResolvePy = t
            Exit Function
        End If
    End If
    f = base & "\.venv\Scripts\pythonw.exe"
    If fso.FileExists(f) Then
        ResolvePy = f
        Exit Function
    End If
    dirs = Split(p, ";")
    For i = 0 To UBound(dirs)
        d = dirs(i)
        If d <> "" Then
            If fso.FileExists(d & "\pythonw.exe") Then
                ResolvePy = d & "\pythonw.exe"
                Exit Function
            End If
        End If
    Next
    ResolvePy = ""
End Function

Sub Die(msg)
    Dim f, fd
    On Error Resume Next
    If Not fso.FolderExists(base & "\logs") Then fso.CreateFolder base & "\logs"
    f = base & "\logs\launch_error.log"
    Set fd = fso.OpenTextFile(f, 8, True)
    fd.WriteLine Now & "  " & msg
    fd.Close
    WScript.Quit 1
End Sub
