' Chay mot file .cmd voi cua so AN (khong co console de agent lo dong nham). Dung: wscript run_hidden.vbs <file.cmd> [args...]
Set sh = CreateObject("WScript.Shell")
cmd = "cmd /c"
For i = 0 To WScript.Arguments.Count - 1
  cmd = cmd & " " & WScript.Arguments(i)
Next
sh.Run cmd, 0, True
