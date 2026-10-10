param()

$ErrorActionPreference = "Stop"
$source = Join-Path $PSScriptRoot "panorama_360"
$destination = Join-Path $env:APPDATA "ingetrazo\plugins\panorama_360"

New-Item -ItemType Directory -Path $destination -Force | Out-Null
Copy-Item (Join-Path $source "*") $destination -Recurse -Force
Write-Host "Installed IngeTrazo Native 360 Export to:"
Write-Host $destination
Write-Host "Restart IngeTrazo, then open Extensions > Export 360 images."
