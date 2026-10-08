' MT5 Fleet one-click launcher (hidden window). If already running, only opens the web page.
Option Explicit
Dim sh, fso, d, py, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
d = fso.GetParentFolderName(WScript.ScriptFullName)
If InStr(1, d, "\AppData\Local\Temp\", vbTextCompare) > 0 Then
  MsgBox ChrW(35831) & ChrW(20808) & ChrW(25226) & ChrW(21387) & ChrW(32553) & ChrW(21253) & ChrW(25972) & ChrW(20010) & ChrW(35299) & ChrW(21387) & ChrW(21040) & ChrW(19968) & ChrW(20010) & ChrW(25991) & ChrW(20214) & ChrW(22841) & ChrW(65288) & ChrW(20363) & ChrW(22914) & " D:\MT5" & ChrW(25209) & ChrW(37327) & ChrW(32456) & ChrW(31471) & ChrW(65289) & ChrW(65292) & ChrW(20877) & ChrW(21452) & ChrW(20987) & ChrW(37324) & ChrW(38754) & ChrW(30340) & ChrW(12300) & ChrW(19968) & ChrW(38190) & ChrW(21551) & ChrW(21160) & ChrW(12301) & ChrW(12290), vbExclamation, "MT5 " & ChrW(25209) & ChrW(37327) & ChrW(32456) & ChrW(31471)
  WScript.Quit 1
End If
py = d & "\python\pythonw.exe"
If Not fso.FileExists(py) Then py = d & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then
  MsgBox ChrW(27809) & ChrW(26377) & ChrW(25214) & ChrW(21040) & " Python " & ChrW(36816) & ChrW(34892) & ChrW(29615) & ChrW(22659) & ChrW(12290) & vbCrLf & vbCrLf & ChrW(20415) & ChrW(25658) & ChrW(29256) & ChrW(65306) & ChrW(35831) & ChrW(30830) & ChrW(35748) & ChrW(25991) & ChrW(20214) & ChrW(22841) & ChrW(37324) & ChrW(30340) & " python " & ChrW(25991) & ChrW(20214) & ChrW(22841) & ChrW(23436) & ChrW(25972) & ChrW(65288) & ChrW(37325) & ChrW(26032) & ChrW(35299) & ChrW(21387) & ChrW(19968) & ChrW(27425) & ChrW(65289) & ChrW(12290) & vbCrLf & ChrW(28304) & ChrW(30721) & ChrW(29256) & ChrW(65306) & ChrW(35831) & ChrW(20808) & ChrW(21452) & ChrW(20987) & ChrW(12300) & ChrW(23433) & ChrW(35013) & ChrW(20381) & ChrW(36182) & ".bat" & ChrW(12301) & ChrW(12290), vbCritical, "MT5 " & ChrW(25209) & ChrW(37327) & ChrW(32456) & ChrW(31471)
  WScript.Quit 1
End If
If Not fso.FileExists(d & "\app.py") Then
  MsgBox ChrW(25991) & ChrW(20214) & ChrW(19981) & ChrW(23436) & ChrW(25972) & ChrW(65306) & ChrW(32570) & ChrW(23569) & " app.py" & ChrW(12290) & ChrW(35831) & ChrW(37325) & ChrW(26032) & ChrW(35299) & ChrW(21387) & ChrW(12290), vbCritical, "MT5 " & ChrW(25209) & ChrW(37327) & ChrW(32456) & ChrW(31471)
  WScript.Quit 1
End If
sh.CurrentDirectory = d
cmd = """" & py & """ """ & d & "\app.py"" --hidden --reuse"
sh.Run cmd, 0, False
