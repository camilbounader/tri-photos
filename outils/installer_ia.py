# -*- coding: utf-8 -*-
"""
Installation (ou réparation) de l'IA locale de Tri Photos.

Lancé par l'installateur, ou par le bouton « Installer l'IA » de l'accueil.
Tout est noté dans %LOCALAPPDATA%\\TriPhotos\\installation_ia.log.

Étapes :
  1. PyTorch (version carte graphique NVIDIA si présente, sinon processeur)
  2. PyYAML précompilé ; s'il n'existe pas pour ce PC, remplaçant minimal
  3. open_clip (+ timm, huggingface_hub et leurs dépendances)
  4. Vérification de l'import, puis téléchargement des modèles (~1 Go)
"""

import importlib
import importlib.metadata as md
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import traceback
from pathlib import Path

DOSSIER_APPLI = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DOSSIER_APPLI))
import config_tri  # noqa: E402

JOURNAL = config_tri.DONNEES / "installation_ia.log"
ETAT = config_tri.DONNEES / "installation_ia.etat"
PIP = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--prefer-binary", "--progress-bar", "off"]
NE_PAS_INSTALLER = {"pyyaml", "torch", "torchvision"}


def noter(texte, etat=None):
    print(texte, flush=True)
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(texte + "\n")
    if etat:
        ETAT.write_text(etat, encoding="utf-8")


def lancer(cmd, titre):
    """Lance pip en affichant sa progression en direct (et en la notant dans le journal)."""
    noter(f"\n$ {' '.join(cmd[2:])}")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", bufsize=1)
    lignes = []
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        for l in p.stdout:
            l = l.rstrip()
            if not l:
                continue
            lignes.append(l)
            fh.write(l + "\n")
            fh.flush()
            print("    " + l[:150], flush=True)
    p.wait()
    if p.returncode != 0:
        noter(f"  ! {titre} : échec. Dernières lignes :")
        for l in lignes[-8:]:
            noter("    " + l)
    return p.returncode == 0


def gpu_nvidia():
    if shutil.which("nvidia-smi"):
        return True
    racine = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.exists(os.path.join(racine, "System32", "nvidia-smi.exe"))


def dependances(paquet):
    """Dépendances déclarées d'un paquet installé (en tenant compte de la plateforme)."""
    try:
        from packaging.requirements import Requirement
    except ImportError:
        Requirement = None
    res = []
    for r in md.requires(paquet) or []:
        if Requirement:
            req = Requirement(r)
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue
            res.append(str(req).split(";")[0].strip())
        elif ";" not in r:
            res.append(r.strip())
    return res


def nom_de(req):
    return re.split(r"[<>=!~\[ ]", req, 1)[0].strip().lower().replace("_", "-")


def torch_ok():
    """Teste PyTorch dans un processus séparé (un échec de chargement ne bloque pas la suite)."""
    p = subprocess.run([sys.executable, "-c", "import torch; print('torch', torch.__version__)"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    return p.returncode == 0, p.stdout


def reparer_dll():
    """Ajoute les DLL Microsoft Visual C++ à côté de PyTorch (sans droits administrateur)."""
    if os.name != "nt":
        return
    lancer(PIP + ["msvc-runtime"], "composants Visual C++")
    torch_lib = Path(sysconfig.get_paths()["purelib"]) / "torch" / "lib"
    if not torch_lib.is_dir():
        return
    copiees = 0
    for racine in {Path(sys.prefix), Path(sys.base_prefix)}:
        for dll in racine.rglob("*.dll"):
            n = dll.name.lower()
            if torch_lib in dll.parents:
                continue
            if n.startswith(("msvcp140", "vcruntime140", "concrt140", "vcomp140")):
                cible = torch_lib / dll.name
                if not cible.exists():
                    shutil.copy2(dll, cible)
                    copiees += 1
    noter(f"  {copiees} composant(s) Visual C++ copié(s) à côté de PyTorch.")


def installer_remplacant_yaml():
    dest = Path(sysconfig.get_paths()["purelib"]) / "yaml"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DOSSIER_APPLI / "outils" / "yaml_minimal.py", dest / "__init__.py")
    noter(f"  Remplaçant minimal de PyYAML installé dans {dest}")


def main():
    config_tri.DONNEES.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text("", encoding="utf-8")
    noter("=== Installation de l'IA de Tri Photos ===", "en cours")
    noter(f"Python {sys.version.split()[0]} · {platform.machine()} · {sysconfig.get_platform()} · "
          f"{8 * __import__('struct').calcsize('P')} bits · {sys.executable}")

    # 0. Version de Python : l'IA (PyTorch) a besoin de Python 3.10 à 3.13
    if not ((3, 10) <= sys.version_info[:2] <= (3, 13)):
        noter(f"\nPython {sys.version.split()[0]} est trop récent pour l'IA (PyTorch n'est pas encore prêt pour lui).")
        noter("Il faut réparer l'installation pour passer à Python 3.12 : bouton « Réparer l'installation » de l'accueil.",
              "python_incompatible")
        return 2

    # 1. PyTorch
    noter("\n[1/4] PyTorch (moteur de l'IA)")
    ok_torch, sortie = torch_ok()
    if ok_torch:
        noter(f"  Déjà installé ({sortie.strip().splitlines()[-1]}).")
    else:
        gpu = gpu_nvidia()
        if "No module named" in sortie:
            installe = False
            if gpu:
                noter("  Carte NVIDIA détectée : version carte graphique (~3 Go, plusieurs minutes sans")
                noter("  barre de progression pendant le téléchargement : c'est normal, laisse tourner).")
                installe = lancer(PIP + ["torch", "torchvision", "--index-url",
                                         "https://download.pytorch.org/whl/cu128"], "PyTorch GPU")
            if not installe:
                noter("  Version processeur (~300 Mo).")
                lancer(PIP + ["torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cpu"],
                       "PyTorch processeur")
            ok_torch, sortie = torch_ok()
        if not ok_torch and ("WinError 126" in sortie or "DLL" in sortie):
            noter("  PyTorch ne trouve pas certains composants Windows (Visual C++) : on les ajoute.")
            reparer_dll()
            ok_torch, sortie = torch_ok()
        if not ok_torch and "No module named" not in sortie:
            noter("  On réinstalle la version processeur de PyTorch.")
            lancer(PIP + ["--force-reinstall", "torch", "torchvision", "--index-url",
                          "https://download.pytorch.org/whl/cpu"], "PyTorch processeur")
            reparer_dll()
            ok_torch, sortie = torch_ok()
    if not ok_torch:
        noter("  ! PyTorch ne se charge pas :")
        for l in sortie.strip().splitlines()[-6:]:
            noter("    " + l)
        if "WinError 126" in sortie:
            noter("  Il manque probablement « Microsoft Visual C++ Redistributable 2015-2022 (x64) » sur ce PC.")
            noter("  Sur un PC du travail, demande au service informatique de l'installer.")
        noter("\nÉCHEC : PyTorch n'a pas pu être installé.", "echec")
        return 1

    # 2. PyYAML
    noter("\n[2/4] PyYAML")
    if lancer(PIP + ["pyyaml", "--only-binary=:all:"], "PyYAML précompilé"):
        yaml_ok = True
    else:
        yaml_ok = False
        noter("  Pas de PyYAML précompilé pour ce PC : on s'en passe (Tri Photos n'en a pas besoin).")

    # 3. open_clip
    noter("\n[3/4] open_clip (compréhension des images)")
    if yaml_ok:
        ok = lancer(PIP + ["open_clip_torch"], "open_clip")
    else:
        ok = lancer(PIP + ["--no-deps", "open_clip_torch", "timm", "huggingface_hub"], "open_clip sans dépendances")
        if ok:
            importlib.invalidate_caches()
            a_faire, vus = ["open-clip-torch", "timm", "huggingface-hub"], set()
            deps = []
            while a_faire:
                p = a_faire.pop()
                if p in vus:
                    continue
                vus.add(p)
                for d in dependances(p):
                    n = nom_de(d)
                    if n not in NE_PAS_INSTALLER and n not in vus:
                        deps.append(d)
            deps = sorted(set(deps))
            noter("  Dépendances : " + ", ".join(deps))
            ok = lancer(PIP + deps, "dépendances")
            installer_remplacant_yaml()
    importlib.invalidate_caches()

    # 4. Vérification + modèles
    noter("\n[4/4] Vérification et modèles")
    try:
        import open_clip  # noqa: F401
        noter("  open_clip se charge correctement.")
    except Exception:
        noter("  ! open_clip ne se charge pas :")
        noter(traceback.format_exc())
        noter("\nÉCHEC : l'IA n'est pas utilisable. Envoie le fichier installation_ia.log à Claude.", "echec")
        return 1
    try:
        import ia
        m = ia.ModeleIA()
        if m.esthetique is not None:
            ia.MARQUEUR_PRET.write_text("ok", encoding="utf-8")
        noter(f"  Modèles prêts ({'carte graphique' if m.appareil == 'cuda' else 'processeur'}).")
    except Exception:
        noter("  ! Téléchargement ou chargement des modèles impossible :")
        noter(traceback.format_exc())
        noter("\nÉCHEC partiel : relance l'installation de l'IA avec internet.", "echec")
        return 1
    noter("\nL'IA est installée. Elle fonctionne maintenant hors connexion.", "ok")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        noter(traceback.format_exc(), "echec")
        sys.exit(1)
