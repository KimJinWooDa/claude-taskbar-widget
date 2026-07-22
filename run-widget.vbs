Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("Wscript.Shell").Run """pythonw.exe"" """ & dir & "\ClaudeUsageWidget.pyw""", 0, False
