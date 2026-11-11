$ErrorActionPreference = 'Stop'
$workflowExecutable = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '穿搭舞蹈工作室.exe'))
if (-not (Test-Path -LiteralPath $workflowExecutable -PathType Leaf)) { throw '请在完整发行文件夹中运行此脚本。' }
$workflowDesktop = [Environment]::GetFolderPath('Desktop')
$workflowShortcutPath = Join-Path $workflowDesktop '穿搭舞蹈工作室.lnk'
$workflowShell = New-Object -ComObject WScript.Shell
$workflowShortcut = $workflowShell.CreateShortcut($workflowShortcutPath)
$workflowShortcut.TargetPath = $workflowExecutable
$workflowShortcut.WorkingDirectory = $PSScriptRoot
$workflowShortcut.IconLocation = $workflowExecutable + ',0'
$workflowShortcut.Description = '服装图片与参考舞蹈视频生成工作室'
$workflowShortcut.Save()
Write-Host '已创建“穿搭舞蹈工作室”桌面快捷方式。'
