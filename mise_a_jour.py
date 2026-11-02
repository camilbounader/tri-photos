# -*- coding: utf-8 -*-
"""
Mise à jour automatique de Tri Photos, lancée avant l'appli par l'icône.

Si internet est disponible et qu'une nouvelle version existe sur GitHub, elle
est téléchargée et installée (quelques secondes). Sans internet (en montagne),
on passe directement : l'appli démarre avec la version déjà installée.
"""

import hashlib
import io
import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

DEPOT = "camilbounader/tri-photos"
DOSSIER_APPLI = Path(__file__).resolve().parent
FICHIER_VERSION = DOSSIER_APPLI / "VERSION"
A_IGNORER = {"installer.ps1", "README.md", ".gitignore", "tests"}


def lire(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "tri-photos", "Accept": "application/vnd.github.sha"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def version_locale():
    try:
        return FICHIER_VERSION.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def empreinte(f: Path):
    try:
        return hashlib.sha1(f.read_bytes()).hexdigest()
    except Exception:
        return ""


def main():
    try:
        sha = lire(f"https://api.github.com/repos/{DEPOT}/commits/main", 4).decode().strip()[:40]
    except Exception:
        print("Pas de connexion : Tri Photos démarre avec la version installée.")
        return
    locale = version_locale()
    if len(sha) != 40 or locale.startswith(sha[:7]):
        return
    print(f"Mise à jour de Tri Photos ({locale or '?'} → {sha[:7]})…")
    try:
        archive = zipfile.ZipFile(io.BytesIO(lire(f"https://codeload.github.com/{DEPOT}/zip/{sha}", 60)))
        avant = empreinte(DOSSIER_APPLI / "requirements.txt")
        for info in archive.infolist():
            morceaux = Path(info.filename).parts[1:]  # retire le dossier racine "tri-photos-<sha>"
            if not morceaux or info.is_dir() or morceaux[0] in A_IGNORER:
                continue
            cible = DOSSIER_APPLI.joinpath(*morceaux)
            cible.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(cible, "wb") as dst:
                shutil.copyfileobj(src, dst)
        if empreinte(DOSSIER_APPLI / "requirements.txt") != avant:
            print("Nouvelles librairies à installer…")
            subprocess.call([sys.executable, "-m", "pip", "install", "-q", "-r",
                             str(DOSSIER_APPLI / "requirements.txt")])
        FICHIER_VERSION.write_text(sha[:7], encoding="utf-8")
        print("Mise à jour installée.")
    except Exception as exc:
        print(f"Mise à jour impossible ({exc}) : on continue avec la version installée.")


if __name__ == "__main__":
    main()
