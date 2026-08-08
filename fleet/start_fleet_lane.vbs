' Fleet lane startup: real Ollama on the upstream port + the usage proxy on the
' default one, both hidden. Lives in shell:startup (replaces the Ollama tray
' autostart).
'
' Paths are derived from THIS script's own location, never hardcoded, so the
' repo can be cloned anywhere on any box and the same file still works.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)

' pythonw: FLEET_PYTHONW wins, else whatever "pythonw" resolves to on PATH.
pyw = sh.ExpandEnvironmentStrings("%FLEET_PYTHONW%")
If pyw = "" Or InStr(pyw, "%") > 0 Then pyw = "pythonw.exe"

' Startup delay is a knob: a cold NVMe needs longer than a warm page cache.
delay = sh.ExpandEnvironmentStrings("%FLEET_UPSTREAM_WAIT_MS%")
If delay = "" Or InStr(delay, "%") > 0 Or Not IsNumeric(delay) Then delay = 4000

sh.Run """" & fso.BuildPath(here, "ollama_upstream.cmd") & """", 0, False
WScript.Sleep CLng(delay)
sh.Run """" & pyw & """ """ & fso.BuildPath(here, "fleet_proxy_launcher.pyw") & """", 0, False
