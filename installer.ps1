# Installation de Tri Photos (Windows, sans droits administrateur)
#
# Dans PowerShell, colle cette ligne puis Entree :
#   irm https://raw.githubusercontent.com/camilbounader/tri-photos/main/installer.ps1 | iex
#
# Ce script :
#   1. installe Python 3.12 rien que pour Tri Photos (s'il n'est pas deja la) ;
#   2. telecharge l'appli ;
#   3. installe les librairies et l'IA (version carte graphique NVIDIA si presente) ;
#   4. telecharge les modeles d'IA (ensuite, plus besoin d'internet) ;
#   5. cree l'icone "Tri Photos" sur le bureau et dans le menu Demarrer.
# Relancer la meme ligne repare ou met a jour l'installation.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Depot   = 'camilbounader/tri-photos'
$Base    = Join-Path $env:LOCALAPPDATA 'TriPhotos'
$DirPy   = Join-Path $Base 'python'
$DirApp  = Join-Path $Base 'app'
$DirVenv = Join-Path $Base 'venv'
$PyVenv  = Join-Path $DirVenv 'Scripts\python.exe'
$PyUrl   = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'

function Etape($n, $texte) { Write-Host ""; Write-Host "[$n/6] $texte" -ForegroundColor Cyan }
function Echec($texte) {
    Write-Host ""; Write-Host "ERREUR : $texte" -ForegroundColor Red
    Write-Host "Copie ce message et envoie-le a Claude." -ForegroundColor Yellow
    Read-Host "Appuie sur Entree pour fermer"; exit 1
}

Write-Host "=== Installation de Tri Photos ===" -ForegroundColor Green
Write-Host "Dossier : $Base"
New-Item -ItemType Directory -Force -Path $Base | Out-Null

# Si l'appli tourne deja, on la ferme proprement
try { Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/quitter' -TimeoutSec 2 | Out-Null; Start-Sleep 2 } catch {}

# --- 1. Python ---------------------------------------------------------------
Etape 1 "Python"
$PyBase = Join-Path $DirPy 'python.exe'
function Version-Ok($exe) {
    try { $v = & $exe -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null; return @('3.10','3.11','3.12','3.13') -contains "$v".Trim() } catch { return $false }
}
if (-not (Test-Path $PyBase)) {
    $tmp = Join-Path $env:TEMP 'python-3.12.10-amd64.exe'
    Write-Host "Telechargement de Python 3.12 (25 Mo)..."
    Invoke-WebRequest -Uri $PyUrl -OutFile $tmp -UseBasicParsing
    Write-Host "Installation de Python pour Tri Photos uniquement (1-2 min)..."
    $p = Start-Process -FilePath $tmp -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', "TargetDir=`"$DirPy`"", 'PrependPath=0', 'Shortcuts=0',
        'Include_launcher=0', 'Include_test=0', 'Include_doc=0', 'AssociateFiles=0')
    Remove-Item $tmp -ErrorAction SilentlyContinue
}
if (-not (Test-Path $PyBase)) {
    # Python 3.12 deja installe sur le PC : l'installateur ne le reinstalle pas ailleurs
    $PyBase = $null
    foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
                     "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
                     "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
                     "C:\Program Files\Python312\python.exe")) {
        if ((Test-Path $c) -and (Version-Ok $c)) { $PyBase = $c; break }
    }
    if (-not $PyBase) {
        $c = (Get-Command python -ErrorAction SilentlyContinue).Source
        if ($c -and ($c -notlike '*WindowsApps*') -and (Version-Ok $c)) { $PyBase = $c }
    }
    if (-not $PyBase) { Echec "Python n'a pas pu etre installe. Installe Python 3.12 depuis python.org puis relance cette ligne." }
}
Write-Host "Python : $PyBase" -ForegroundColor DarkGray

# --- 2. L'appli ----------------------------------------------------------------
Etape 2 "Telechargement de Tri Photos"
try {
    $sha = (Invoke-RestMethod -Uri "https://api.github.com/repos/$Depot/commits/main" -Headers @{'User-Agent'='tri-photos'}).sha
} catch { Echec "Impossible de joindre GitHub ($($_.Exception.Message))." }
$zip = Join-Path $env:TEMP 'tri-photos.zip'
$tmpDir = Join-Path $env:TEMP 'tri-photos-extrait'
Invoke-WebRequest -Uri "https://codeload.github.com/$Depot/zip/$sha" -OutFile $zip -UseBasicParsing
if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $tmpDir -Force
$src = Get-ChildItem $tmpDir | Select-Object -First 1
New-Item -ItemType Directory -Force -Path $DirApp | Out-Null
Copy-Item -Path (Join-Path $src.FullName '*') -Destination $DirApp -Recurse -Force
Set-Content -Path (Join-Path $DirApp 'VERSION') -Value $sha.Substring(0,7) -Encoding ASCII
Remove-Item $zip, $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Version $($sha.Substring(0,7))" -ForegroundColor DarkGray

# --- 3. Librairies -------------------------------------------------------------
Etape 3 "Librairies photo"
if (-not (Test-Path $PyVenv)) {
    & $PyBase -m venv $DirVenv
    if ($LASTEXITCODE -ne 0) { Echec "Creation de l'environnement Python impossible." }
}
& $PyVenv -m pip install --upgrade pip --quiet --disable-pip-version-check
& $PyVenv -m pip install -r (Join-Path $DirApp 'requirements.txt') --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Echec "Installation des librairies impossible (pare-feu du PC du travail ?)." }

# --- 4. IA -------------------------------------------------------------------------
Etape 4 "IA locale (la plus longue etape : 5 a 15 min selon la connexion)"
$gpu = (Test-Path "$env:SystemRoot\System32\nvidia-smi.exe") -or [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
if ($gpu) {
    Write-Host "Carte graphique NVIDIA detectee : version rapide (environ 3 Go)."
    & $PyVenv -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128 --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Version carte graphique indisponible, installation de la version processeur." -ForegroundColor Yellow
        $gpu = $false
    }
}
if (-not $gpu) {
    Write-Host "Version processeur (environ 300 Mo). L'analyse sera plus lente qu'avec une carte NVIDIA, mais fonctionne."
    & $PyVenv -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu --disable-pip-version-check
}
& $PyVenv -m pip install open_clip_torch --disable-pip-version-check
$iaOk = ($LASTEXITCODE -eq 0)
if (-not $iaOk) { Write-Host "L'IA n'a pas pu etre installee : l'appli marchera sans (tri sur criteres techniques)." -ForegroundColor Yellow }

# --- 5. Modeles d'IA ---------------------------------------------------------------
Etape 5 "Modeles d'IA (environ 1 Go, une seule fois)"
if ($iaOk) {
    Push-Location $DirApp
    & $PyVenv ia.py --telecharger
    Pop-Location
    if ($LASTEXITCODE -ne 0) { Write-Host "Les modeles seront telecharges au premier tri (avec internet)." -ForegroundColor Yellow }
}

# --- 6. Icones -----------------------------------------------------------------------
Etape 6 "Icone sur le bureau et dans le menu Demarrer"
$lanceur = Join-Path $Base 'Tri Photos.cmd'
@'
@echo off
chcp 65001 >nul
title Tri Photos - garde cette fenetre ouverte (tu peux la reduire)
cd /d "%LOCALAPPDATA%\TriPhotos\app"
set PYTHONDONTWRITEBYTECODE=1
"%LOCALAPPDATA%\TriPhotos\venv\Scripts\python.exe" mise_a_jour.py
"%LOCALAPPDATA%\TriPhotos\venv\Scripts\python.exe" app.py %*
'@ | Set-Content -Path $lanceur -Encoding ASCII

$icone = Join-Path $DirApp 'icone.ico'
$ws = New-Object -ComObject WScript.Shell
$cibles = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Tri Photos.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'Tri Photos.lnk')
)
foreach ($c in $cibles) {
    $s = $ws.CreateShortcut($c)
    $s.TargetPath = $lanceur
    $s.WorkingDirectory = $DirApp
    $s.WindowStyle = 7
    if (Test-Path $icone) { $s.IconLocation = $icone }
    $s.Description = 'Tri Photos : aide au tri de photos, en local'
    $s.Save()
}

Write-Host ""
Write-Host "=== Tri Photos est installe ! ===" -ForegroundColor Green
Write-Host "Une icone 'Tri Photos' est sur ton bureau. L'appli s'ouvre maintenant dans ton navigateur."
Write-Host "Astuce : tu peux glisser un dossier de photos sur l'icone pour le trier directement."
Start-Process -FilePath $lanceur -WindowStyle Minimized
Start-Sleep 3
