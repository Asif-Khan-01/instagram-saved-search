' Runs update.py with no window, writing its output to update.log.
' Used by the daily scheduled task; see README.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "cmd /c cd /d """ & here & """ && python update.py >> update.log 2>&1"
CreateObject("WScript.Shell").Run cmd, 0, False
