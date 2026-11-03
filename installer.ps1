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
# Fichiers temporaires dans notre dossier : le dossier TEMP de Windows a parfois un nom
# court (ex. C:\Users\CB2E1~1.BOU) que PowerShell gere mal.
$Tmp     = Join-Path $Base 'tmp'

function Supprimer($chemin) {
    try { if (Test-Path -LiteralPath $chemin) { Remove-Item -LiteralPath $chemin -Recurse -Force -ErrorAction Stop } } catch {}
}

function Etape($n, $texte) { Write-Host ""; Write-Host "[$n/6] $texte" -ForegroundColor Cyan }
function Echec($texte) {
    Write-Host ""; Write-Host "ERREUR : $texte" -ForegroundColor Red
    Write-Host "Copie ce message et envoie-le a Claude." -ForegroundColor Yellow
    Read-Host "Appuie sur Entree pour fermer"; exit 1
}

Write-Host "=== Installation de Tri Photos ===" -ForegroundColor Green
Write-Host "Dossier : $Base"
New-Item -ItemType Directory -Force -Path $Base | Out-Null
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

# Si l'appli tourne deja, on la ferme proprement
try { Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/quitter' -TimeoutSec 2 | Out-Null; Start-Sleep 2 } catch {}

# --- 1. Python ---------------------------------------------------------------
Etape 1 "Python"
$PyBase = Join-Path $DirPy 'python.exe'
function Version-Ok($exe) {
    try { $v = & $exe -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null; return @('3.10','3.11','3.12','3.13') -contains "$v".Trim() } catch { return $false }
}
if (-not (Test-Path -LiteralPath $PyBase)) {
    $tmp = Join-Path $Tmp 'python-3.12.10-amd64.exe'
    Write-Host "Telechargement de Python 3.12 (25 Mo)..."
    Invoke-WebRequest -Uri $PyUrl -OutFile $tmp -UseBasicParsing
    Write-Host "Installation de Python pour Tri Photos uniquement (1-2 min)..."
    $p = Start-Process -FilePath $tmp -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', "TargetDir=`"$DirPy`"", 'PrependPath=0', 'Shortcuts=0',
        'Include_launcher=0', 'Include_test=0', 'Include_doc=0', 'AssociateFiles=0')
    Supprimer $tmp
}
if (-not (Test-Path -LiteralPath $PyBase)) {
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
    if (-not $PyBase) {
        # Dernier recours : Python 3.12 autonome via uv (sans droits administrateur)
        Write-Host "Installation de Python 3.12 par une autre methode (uv)..." -ForegroundColor Yellow
        try {
            $env:UV_INSTALL_DIR = Join-Path $Base 'uv'
            Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
            $uv = Join-Path $Base 'uv\uv.exe'
            $env:UV_PYTHON_INSTALL_DIR = Join-Path $Base 'python-uv'
            & $uv python install 3.12
            $c = (& $uv python find 3.12).Trim()
            if ($c -and (Version-Ok $c)) { $PyBase = $c }
        } catch { Write-Host "uv : $($_.Exception.Message)" -ForegroundColor DarkGray }
    }
    if (-not $PyBase) { Echec "Python n'a pas pu etre installe. Installe Python 3.12 depuis python.org puis relance cette ligne." }
}
if (-not (Version-Ok $PyBase)) { Echec "Python trouve ($PyBase) mais dans une version non prise en charge. Installe Python 3.12 depuis python.org puis relance." }
Write-Host "Python : $PyBase" -ForegroundColor DarkGray

# --- 2. L'appli ----------------------------------------------------------------
Etape 2 "Telechargement de Tri Photos"
try {
    $sha = (Invoke-RestMethod -Uri "https://api.github.com/repos/$Depot/commits/main" -Headers @{'User-Agent'='tri-photos'}).sha
} catch { Echec "Impossible de joindre GitHub ($($_.Exception.Message))." }
$zip = Join-Path $Tmp 'tri-photos.zip'
$tmpDir = Join-Path $Tmp 'tri-photos-extrait'
Invoke-WebRequest -Uri "https://codeload.github.com/$Depot/zip/$sha" -OutFile $zip -UseBasicParsing
Supprimer $tmpDir
Expand-Archive -LiteralPath $zip -DestinationPath $tmpDir -Force
$src = Get-ChildItem -LiteralPath $tmpDir | Select-Object -First 1
New-Item -ItemType Directory -Force -Path $DirApp | Out-Null
Copy-Item -Path (Join-Path $src.FullName '*') -Destination $DirApp -Recurse -Force
Set-Content -Path (Join-Path $DirApp 'VERSION') -Value $sha.Substring(0,7) -Encoding ASCII
Supprimer $zip
Supprimer $tmpDir
Write-Host "Version $($sha.Substring(0,7))" -ForegroundColor DarkGray

# --- 3. Librairies -------------------------------------------------------------
Etape 3 "Librairies photo"
& $PyBase -c "import sys,platform,struct;print('Python',sys.version.split()[0],platform.machine(),struct.calcsize('P')*8,'bits')"
if ((Test-Path -LiteralPath $PyVenv) -and -not (Version-Ok $PyVenv)) {
    Write-Host "L'environnement existant utilise une version de Python incompatible avec l'IA : on le recree avec Python 3.12." -ForegroundColor Yellow
    Get-Process python, pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$DirVenv*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep 1
    Supprimer $DirVenv
    if (Test-Path -LiteralPath $PyVenv) { Echec "Impossible de remplacer l'ancien environnement : ferme la fenetre noire de Tri Photos puis relance l'installation." }
}
if (-not (Test-Path -LiteralPath $PyVenv)) {
    & $PyBase -m venv $DirVenv
    if ($LASTEXITCODE -ne 0) { Echec "Creation de l'environnement Python impossible." }
}
& $PyVenv -m pip install --upgrade pip --quiet --disable-pip-version-check --prefer-binary
& $PyVenv -m pip install -r (Join-Path $DirApp 'requirements.txt') --disable-pip-version-check --prefer-binary
if ($LASTEXITCODE -ne 0) { Echec "Installation des librairies impossible (pare-feu du PC du travail ?)." }

# --- 4 et 5. IA ----------------------------------------------------------------------
Etape 4 "IA locale (la plus longue etape : 5 a 15 min selon la connexion)"
Write-Host "Le detail est note dans $Base\installation_ia.log"
Push-Location $DirApp
& $PyVenv (Join-Path $DirApp 'outils\installer_ia.py')
$iaOk = ($LASTEXITCODE -eq 0)
Pop-Location
Etape 5 "Bilan de l'IA"
if ($iaOk) { Write-Host "IA installee." -ForegroundColor Green }
else { Write-Host "L'IA n'a pas pu etre installee. L'appli marche sans ; tu pourras reessayer avec le bouton 'Installer l'IA' de l'accueil." -ForegroundColor Yellow }

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
