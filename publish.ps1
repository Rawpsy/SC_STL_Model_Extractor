# Publie une nouvelle version de SC Extractor sur GitHub.
#   1. construit dist\SC_Extractor.exe
#   2. enregistre le code dans git (commit + etiquette vX.Y.Z) et l'envoie
#   3. cree la Release GitHub vX.Y.Z avec SC_Extractor.exe (c'est elle que lit le bouton "Mises a jour")
# Usage : .\publish.ps1 -Notes "Nouveautes de cette version"
# La version est celle de version.py : l'augmenter avant chaque publication.
#        .\publish.ps1 -ReleaseOnly -Notes "..."   (code deja envoye : cree seulement la Release)
param([string]$Notes = "", [switch]$ReleaseOnly)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Repo = "Rawpsy/SC_STL_Model_Extractor"

if (-not (Test-Path .git)) {  # premiere publication : depot local relie au depot GitHub
    git init -b main | Out-Null
    git remote add origin "https://github.com/$Repo.git"
}

$version = (Select-String -Path version.py -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$tag = "v$version"
$exe = Join-Path $PSScriptRoot "dist\SC_Extractor.exe"
if (-not $ReleaseOnly) {
    if (git tag --list $tag) { throw "L'etiquette $tag existe deja : augmentez la version dans version.py." }
    cmd /c "`"$PSScriptRoot\build_exe.bat`""
    if ($LASTEXITCODE -ne 0) { throw "Echec de la construction de l'exe." }
    git add -A
    git commit -m "Version $version" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" | Out-Null
    git tag $tag
    git push origin HEAD --tags
}
if (-not (Test-Path $exe)) { throw "dist\SC_Extractor.exe introuvable : lancez build_exe.bat." }

# jeton GitHub enregistre par Git Credential Manager (aucun mot de passe dans ce script)
# (PowerShell 5.1 ajoute un retour chariot a chaque ligne envoyee a git : on passe par un fichier en LF)
$credIn = Join-Path $env:TEMP "sc_credential_request.txt"
[IO.File]::WriteAllText($credIn, "protocol=https`nhost=github.com`n`n")
$cred = cmd /c "git credential fill < `"$credIn`""
Remove-Item $credIn -ErrorAction SilentlyContinue
$token = ($cred | Where-Object { $_ -like "password=*" }) -replace "^password=", ""
if (-not $token) { throw "Pas d'identifiant GitHub enregistre : connectez-vous avec git une fois." }
$headers = @{ Authorization = "Bearer $token"; Accept = "application/vnd.github+json"; "User-Agent" = "SC_Extractor-publish" }

$body = @{ tag_name = $tag; name = "SC Extractor $version"; body = $Notes } | ConvertTo-Json
$rel = Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/$Repo/releases" -Headers $headers -Body $body
$upload = $rel.upload_url -replace "\{.*\}", ""
Invoke-RestMethod -Method Post -Uri "$($upload)?name=SC_Extractor.exe" -Headers $headers `
    -ContentType "application/octet-stream" -InFile $exe | Out-Null
Write-Host "Publie : $($rel.html_url)"
