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
PIP = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--prefer-binary"]
NE_PAS_INSTALLER = {"pyyaml", "torch", "torchvision"}


def noter(texte, etat=None):
    print(texte, flush=True)
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(texte + "\n")
    if etat:
        ETAT.write_text(etat, encoding="utf-8")


def lancer(cmd, titre):
    noter(f"\n$ {' '.join(cmd[2:])}")
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       encoding="utf-8", errors="replace")
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(p.stdout[-20000:])
    lignes = [l for l in p.stdout.splitlines() if l.strip()]
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

    # 1. PyTorch
    noter("\n[1/4] PyTorch (moteur de l'IA)")
    try:
        import torch  # noqa: F401
        noter(f"  Déjà installé ({torch.__version__}).")
        ok_torch = True
    except ImportError:
        ok_torch = False
        if gpu_nvidia():
            noter("  Carte NVIDIA détectée : version carte graphique (~3 Go).")
            ok_torch = lancer(PIP + ["torch", "torchvision", "--index-url",
                                     "https://download.pytorch.org/whl/cu128"], "PyTorch GPU")
        if not ok_torch:
            noter("  Version processeur (~300 Mo).")
            ok_torch = lancer(PIP + ["torch", "torchvision", "--index-url",
                                     "https://download.pytorch.org/whl/cpu"], "PyTorch processeur")
    if not ok_torch:
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
