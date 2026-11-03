# -*- coding: utf-8 -*-
"""
Application locale de Tri Photos : un petit serveur sur TON PC (rien n'est mis
en ligne) qui affiche l'appli dans le navigateur.

- Accueil : sorties déjà triées, import de la carte SD, ouverture d'un dossier.
- Tri d'une sortie : grille, diaporama en pleine qualité, décisions, export.
- Réglages, accès depuis le téléphone (même Wi-Fi), analyse en arrière-plan.
"""

import hashlib
import io
import platform
import sys
import json
import os
import secrets
import shutil
import socket
import threading
import time
import traceback
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image

import analyse
import config_tri
import developpement

DOSSIER_OUTIL = Path(__file__).resolve().parent
STATUTS = ("meilleure", "garder", "supprimer")
EXT_PHOTOS = analyse.EXT_RAW | analyse.EXT_JPG
PORT_TELEPHONE = 8766


def version():
    try:
        return (DOSSIER_OUTIL / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return "dev"


def sid_de(dossier) -> str:
    return hashlib.sha1(str(Path(dossier).resolve()).lower().encode("utf-8")).hexdigest()[:10]


def lire_json(chemin: Path, defaut):
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except Exception:
        return defaut


def ecrire_json(chemin: Path, donnees):
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = chemin.with_suffix(".tmp")
    tmp.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, chemin)


def taille_lisible(dossier: Path):
    o = sum(f.stat().st_size for f in dossier.iterdir() if f.is_file())
    return f"{o / 1e9:.1f} Go".replace(".", ",") if o >= 1e9 else f"{max(1, round(o / 1e6))} Mo"


def noter_erreur(texte):
    """Garde une trace des erreurs dans %LOCALAPPDATA%\\TriPhotos\\erreurs.log (page Diagnostic)."""
    try:
        f = config_tri.DONNEES / "erreurs.log"
        if f.exists() and f.stat().st_size > 500_000:
            f.write_text(f.read_text(encoding="utf-8", errors="replace")[-200_000:], encoding="utf-8")
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"--- {datetime.now():%d/%m/%Y %H:%M:%S}\n{texte.rstrip()}\n")
    except Exception:
        pass


def diagnostic():
    import importlib
    infos = {"Tri Photos": version(), "Python": f"{platform.python_version()} ({sys.executable})",
             "Windows": platform.platform(), "Dossier des données": str(config_tri.DONNEES)}
    for mod in ("numpy", "cv2", "PIL", "rawpy", "exifread", "torch", "open_clip"):
        try:
            m = importlib.import_module(mod)
            infos[mod] = getattr(m, "__version__", "ok")
        except Exception as exc:
            infos[mod] = f"absent ({type(exc).__name__}: {str(exc)[:120]})"

    def fin(nom, n):
        try:
            return "\n".join((config_tri.DONNEES / nom).read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
        except Exception:
            return "(vide)"
    return {"infos": infos, "erreurs": fin("erreurs.log", 80), "installation_ia": fin("installation_ia.log", 40)}


def ouvrir_dossier(chemin: Path):
    try:
        if os.name == "nt":
            os.startfile(str(chemin))  # type: ignore[attr-defined]
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Une sortie en cours de tri
# ---------------------------------------------------------------------------
class Session:
    def __init__(self, dossier: Path):
        self.dossier = dossier
        self.sortie = dossier / "_TRI"
        self.analyse = lire_json(self.sortie / "analyse.json", None)
        if not self.analyse:
            raise FileNotFoundError("Cette sortie n'a pas encore été analysée.")
        self.photos = {p["id"]: p for p in self.analyse["photos"]}
        self.fichier_decisions = self.sortie / "decisions.json"
        self.fichier_deplacements = self.sortie / "deplacements.json"
        self.decisions = lire_json(self.fichier_decisions, {})
        for pid, p in self.photos.items():
            if self.decisions.get(pid) not in STATUTS:
                self.decisions[pid] = {"meilleure": "meilleure", "rejet": "supprimer"}.get(p["suggestion"], "garder")
        self.decisions = {k: v for k, v in self.decisions.items() if k in self.photos}
        self.verrou = threading.Lock()
        self.verrous_rendu = {}
        self.export = {"etat": "repos", "fait": 0, "total": 0, "message": ""}
        self.envoi = {"etat": "repos", "fait": 0, "total": 0, "message": ""}
        self.fichier_recup = self.sortie / "recuperation.json"
        self.recup = {k: v for k, v in lire_json(self.fichier_recup, {}).items() if k in self.photos}
        self.lin_cache = OrderedDict()  # images RAW développées en mémoire pour l'outil « Retoucher »
        self.copies = {}  # copies JPEG retouchées : pid -> état
        self.actif = True
        # identifiant de l'analyse (change à chaque réanalyse)
        self.jeton = hashlib.sha1(str(self.analyse.get("date_analyse", "")).encode()).hexdigest()[:6]
        (self.sortie / "cache" / "original").mkdir(parents=True, exist_ok=True)
        (self.sortie / "cache" / "retouche").mkdir(parents=True, exist_ok=True)
        self.sauver_decisions()

    def photo(self, pid):
        """Infos d'une photo + ses réglages « Récupérer » éventuels (pour les rendus et l'export)."""
        return dict(self.photos[pid], recup=self.recup.get(pid))

    def sauver_analyse(self):
        with self.verrou:
            ecrire_json(self.sortie / "analyse.json", self.analyse)

    def oublier_rendus(self, pid):
        for type_ in ("original", "retouche"):
            for f in (self.sortie / "cache" / type_).glob(f"{pid}_r*.jpg"):
                f.unlink(missing_ok=True)
        self.lin_cache.pop(pid, None)

    # --- Rotation ------------------------------------------------------------------
    def pivoter(self, pid, sens):
        sens = 90 if sens >= 0 else 270
        p = self.photos[pid]
        p["rotation"] = (p.get("rotation", 0) + sens) % 360
        p["largeur"], p["hauteur"] = p["hauteur"], p["largeur"]
        mini = self.sortie / "miniatures" / p["miniature"]
        with Image.open(mini) as im:
            im = im.rotate(-sens, expand=True)  # PIL tourne dans le sens inverse des aiguilles d'une montre
            im.save(mini, quality=90)
        if "redressée automatiquement" in p.get("badges", []) and p["rotation"] != p.get("rotation_auto"):
            p["badges"] = [b for b in p["badges"] if b != "redressée automatiquement"]
        self.oublier_rendus(pid)
        self.sauver_analyse()
        return p["rotation"]

    # --- Outil « Récupérer » ---------------------------------------------------------
    def base_recup(self, pid):
        with self.verrou:
            if pid in self.lin_cache:
                self.lin_cache.move_to_end(pid)
                return self.lin_cache[pid]
        donnees = developpement.charger_lineaire(self.photos[pid], demi_taille=True, taille_max=1800)
        with self.verrou:
            self.lin_cache[pid] = donnees
            while len(self.lin_cache) > 4:
                self.lin_cache.popitem(last=False)
        return donnees

    def rendu_recup(self, pid, reglages, zones, recadrer=True):
        lin, est_raw = self.base_recup(pid)
        img = developpement.developper_reglages(lin, est_raw, reglages, recadrer=recadrer)
        perdu = partiel = None
        if zones:
            img, perdu, partiel = developpement.zones_perdues(lin, img, reglages, recadrer=recadrer)
        tampon = io.BytesIO()
        Image.fromarray(img).save(tampon, "JPEG", quality=90)
        return tampon.getvalue(), perdu, partiel

    def infos_recup(self, pid):
        lin, est_raw = self.base_recup(pid)
        sature = lin >= 0.985
        return {"auto": developpement.reglages_auto(lin), "recuperation": developpement.reglages_recuperation(lin),
                "enregistres": developpement.nettoyer_reglages(self.recup[pid]) if pid in self.recup else None,
                "defaut": developpement.REGLAGES_DEFAUT, "curseurs": developpement.CURSEURS, "est_raw": est_raw,
                "largeur": int(lin.shape[1]), "hauteur": int(lin.shape[0]),
                "copie": self.copies.get(pid),
                "perdu": round(float(sature.all(axis=2).mean() * 100), 1),
                "partiel": round(float((sature.any(axis=2) & ~sature.all(axis=2)).mean() * 100), 1),
                "capteur": self.photos[pid].get("capteur_sature")}

    def sauver_recup(self, pid, reglages):
        if reglages:
            self.recup[pid] = developpement.nettoyer_reglages(reglages)
        else:
            self.recup.pop(pid, None)
        ecrire_json(self.fichier_recup, self.recup)
        for f in (self.sortie / "cache" / "retouche").glob(f"{pid}_r*.jpg"):
            f.unlink(missing_ok=True)

    def enregistrer_copie(self, pid):
        """Copie JPEG pleine résolution avec les réglages de retouche, dans _TRI/retouches."""
        if (self.copies.get(pid) or {}).get("etat") == "en cours":
            return self.copies[pid]
        self.copies[pid] = {"etat": "en cours"}

        def travail():
            try:
                d = self.sortie / "retouches"
                d.mkdir(exist_ok=True)
                chemin = developpement.exporter_copie_retouchee(self.photo(pid), str(d))
                self.copies[pid] = {"etat": "termine", "chemin": chemin}
            except Exception as exc:
                noter_erreur(f"Copie retouchée de {pid}\n{traceback.format_exc()}")
                self.copies[pid] = {"etat": "erreur", "message": str(exc)}
        threading.Thread(target=travail, daemon=True).start()
        return self.copies[pid]

    # --- Envoi d'une sélection vers Google Photos ---------------------------------------
    def preparer_envoi(self, ids, mode, ouvrir=True):
        if self.envoi["etat"] == "en cours":
            raise RuntimeError("Un envoi est déjà en préparation.")
        ids = [i for i in ids if i in self.photos]
        if not ids:
            raise ValueError("Aucune photo sélectionnée.")
        base = self.sortie / "envoi" / datetime.now().strftime("%Y-%m-%d %Hh%M")
        dest, n = base, 2
        while dest.exists():
            dest = base.with_name(f"{base.name} ({n})")
            n += 1
        self.envoi = {"etat": "en cours", "fait": 0, "total": len(ids), "message": "Préparation…", "dossier": str(dest)}
        threading.Thread(target=self._envoyer, args=(ids, mode, dest, ouvrir), daemon=True).start()

    def _envoyer(self, ids, mode, dest, ouvrir):
        en = self.envoi
        try:
            dest.mkdir(parents=True)
            if mode == "retouchees":
                en["message"] = "Développement des photos…"
                with ProcessPoolExecutor(max_workers=2) as pool:
                    futurs = [pool.submit(developpement.exporter_retouchee_simple, self.photo(i), str(dest)) for i in ids]
                    for f in as_completed(futurs):
                        try:
                            f.result()
                        except Exception as exc:
                            print("  ! développement impossible :", exc)
                        en["fait"] += 1
            else:
                for i in ids:
                    p = self.photos[i]
                    fichiers = [p.get("raw"), p.get("jpg")]
                    if mode == "jpeg_appareil":
                        fichiers = [p["jpg"]] if p.get("jpg") else [p.get("raw")]
                    for f in fichiers:
                        if f and os.path.exists(f):
                            developpement.lier(f, str(dest / Path(f).name))
                    en["fait"] += 1
            en.update(etat="termine", message=f"{len(ids)} photos prêtes ({taille_lisible(dest)}).")
            if ouvrir:
                ouvrir_dossier(dest)
                import webbrowser
                webbrowser.open("https://photos.google.com/")
        except Exception as exc:
            traceback.print_exc()
            noter_erreur("Envoi\n" + traceback.format_exc())
            en.update(etat="erreur", message=str(exc))

    def sauver_decisions(self):
        with self.verrou:
            ecrire_json(self.fichier_decisions, self.decisions)

    def compter(self):
        c = {s: 0 for s in STATUTS}
        for v in self.decisions.values():
            c[v] += 1
        return c

    def deplacees(self):
        return lire_json(self.fichier_deplacements, [])

    # --- Rendus haute qualité -------------------------------------------------
    def rendu(self, pid, type_):
        p = self.photo(pid)
        # le nom du fichier en cache dépend de la rotation et de l'analyse : jamais d'image périmée
        chemin = str(self.sortie / "cache" / type_ / f"{pid}_r{p.get('rotation', 0)}_{self.jeton}.jpg")
        if os.path.exists(chemin):
            return chemin
        with self.verrou:
            v = self.verrous_rendu.setdefault((pid, type_), threading.Lock())
        with v:
            if type_ == "original":
                return developpement.apercu_original(p, chemin)
            return developpement.apercu_retouche(p, chemin)

    def prechauffer(self):
        """Prépare à l'avance les images du diaporama (meilleures d'abord)."""
        ordre = sorted(self.photos, key=lambda i: (self.decisions[i] != "meilleure",
                                                   self.photos[i]["suggestion"] == "rejet"))
        for pid in ordre:
            if not self.actif:
                return  # sortie fermée ou en cours de réanalyse
            try:
                self.rendu(pid, "original")
            except Exception:
                pass

    def vider_cache_retouche(self):
        shutil.rmtree(self.sortie / "cache" / "retouche", ignore_errors=True)
        (self.sortie / "cache" / "retouche").mkdir(parents=True, exist_ok=True)

    # --- Export ----------------------------------------------------------------
    def lancer_export(self):
        if self.export["etat"] == "en cours":
            return False
        self.export = {"etat": "en cours", "fait": 0, "total": 0, "message": "Préparation…"}
        threading.Thread(target=self._exporter, daemon=True).start()
        return True

    def _exporter(self):
        cfg, _ = config_tri.charger()
        ex = self.export
        try:
            racine = self.sortie / "export"
            if racine.exists():
                shutil.rmtree(racine)  # ne contient que des liens et copies faits par l'outil
            d_meil = racine / "meilleures"
            d_env = racine / "a_envoyer_google_photos"
            d_meil.mkdir(parents=True)
            d_env.mkdir(parents=True)
            formats = {f: racine / f for f in cfg["formats_actifs"]}
            for d in formats.values():
                d.mkdir(parents=True)

            meilleures = [self.photo(pid) for pid in self.photos if self.decisions[pid] == "meilleure"]
            a_envoyer = [self.photo(pid) for pid in self.photos if self.decisions[pid] in ("meilleure", "garder")]
            a_suppr = [self.photo(pid) for pid in self.photos if self.decisions[pid] == "supprimer"]
            mode = cfg["envoi_google_photos"]
            a_developper = list(meilleures)
            if mode == "retouchees":
                a_developper += [p for p in a_envoyer if self.decisions[p["id"]] != "meilleure"]
            ex["total"] = len(a_developper) + len(a_envoyer)

            ex["message"] = "Développement des meilleures…"
            erreurs = []
            with ProcessPoolExecutor(max_workers=2) as pool:
                futurs = {}
                for p in a_developper:
                    if self.decisions[p["id"]] == "meilleure":
                        f = pool.submit(developpement.exporter_meilleure, p, str(d_meil),
                                        {k: str(v) for k, v in formats.items()})
                    else:
                        f = pool.submit(developpement.exporter_retouchee_simple, p, str(d_env))
                    futurs[f] = p["id"]
                for f in as_completed(futurs):
                    try:
                        f.result()
                    except Exception as exc:
                        erreurs.append(f"{futurs[f]} : {exc}")
                        noter_erreur(f"Développement de {futurs[f]} : {exc!r}")
                    ex["fait"] += 1

            ex["message"] = "Préparation du dossier pour Google Photos…"
            for p in a_envoyer:
                if mode == "retouchees":
                    if self.decisions[p["id"]] == "meilleure":
                        src = d_meil / (p["id"] + "_retouche.jpg")
                        if src.exists():
                            developpement.lier(str(src), str(d_env / (p["id"] + ".jpg")))
                else:
                    fichiers = [p.get("raw"), p.get("jpg")]
                    if mode == "jpeg_appareil" and p.get("jpg"):
                        fichiers = [p["jpg"]]
                    for f in fichiers:
                        if f and os.path.exists(f):
                            developpement.lier(f, str(d_env / Path(f).name))
                ex["fait"] += 1

            with open(racine / "a_supprimer.txt", "w", encoding="utf-8") as fh:
                fh.write("Photos marquées « à supprimer » dans Tri Photos\n\n")
                for p in a_suppr:
                    noms = " + ".join(Path(f).name for f in (p.get("raw"), p.get("jpg")) if f)
                    fh.write(f"{noms}\t{'; '.join(p.get('explications_rejet') or p.get('raisons') or [])}\n")
            taille = sum(f.stat().st_size for f in d_env.iterdir()) / 1e9
            msg = (f"{len(meilleures)} meilleures (RAW + JPEG retouché), {len(a_envoyer)} photos prêtes "
                   f"pour Google Photos ({taille:.1f} Go), {len(a_suppr)} à supprimer.")
            if erreurs:
                msg += f" {len(erreurs)} erreur(s) : " + " ; ".join(erreurs[:3])
            ex.update(etat="termine", message=msg, dossier=str(racine))
            ouvrir_dossier(racine)
        except Exception as exc:
            traceback.print_exc()
            noter_erreur("Export\n" + traceback.format_exc())
            ex.update(etat="erreur", message=str(exc))

    # --- Mise à l'écart des photos à supprimer ------------------------------------
    def mettre_a_lecart(self):
        corbeille = self.sortie / "corbeille_a_verifier"
        corbeille.mkdir(exist_ok=True)
        deja = self.deplacees()
        n = 0
        for pid, statut in self.decisions.items():
            if statut != "supprimer":
                continue
            p = self.photos[pid]
            for cle in ("raw", "jpg"):
                f = p.get(cle)
                if f and os.path.exists(f):
                    dest = corbeille / Path(f).name
                    shutil.move(f, dest)
                    deja.append({"id": pid, "cle": cle, "origine": f, "dest": str(dest)})
                    n += 1
        ecrire_json(self.fichier_deplacements, deja)
        ouvrir_dossier(corbeille)
        return n

    def restaurer(self):
        n, restants = 0, []
        for d in self.deplacees():
            if os.path.exists(d["dest"]) and not os.path.exists(d["origine"]):
                shutil.move(d["dest"], d["origine"])
                n += 1
            elif not os.path.exists(d["origine"]):
                restants.append(d)
        ecrire_json(self.fichier_deplacements, restants)
        return n


# ---------------------------------------------------------------------------
# Carte SD
# ---------------------------------------------------------------------------
def _cle_import(f: Path):
    st = f.stat()
    return f"{f.name.lower()}|{st.st_size}|{int(st.st_mtime)}"


def cartes_sd(deja_importes: set):
    """Cartes (ou appareils) contenant un dossier DCIM, avec le nombre de photos nouvelles."""
    racines = []
    test = os.environ.get("TRI_TEST_SD")
    if test:
        racines.append((test, "Carte de test"))
    if os.name == "nt":
        import ctypes
        masque = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            lettre = chr(65 + i)
            if not masque & (1 << i) or lettre in "AC":
                continue
            racine = f"{lettre}:\\"
            if os.path.isdir(os.path.join(racine, "DCIM")):
                nom = ctypes.create_unicode_buffer(261)
                ctypes.windll.kernel32.GetVolumeInformationW(racine, nom, 261, None, None, None, None, 0)
                racines.append((racine, f"{nom.value or 'Carte'} ({lettre}:)"))
    cartes = []
    for racine, nom in racines:
        dcim = Path(racine) / "DCIM"
        if not dcim.is_dir():
            continue
        fichiers = [f for f in dcim.rglob("*") if f.is_file() and f.suffix.lower() in EXT_PHOTOS]
        nouveaux = [f for f in fichiers if _cle_import(f) not in deja_importes]
        dates = [f.stat().st_mtime for f in nouveaux] or [time.time()]
        cartes.append({
            "racine": racine, "nom": nom, "n_photos": len(fichiers), "n_nouvelles": len(nouveaux),
            "taille_go": round(sum(f.stat().st_size for f in nouveaux) / 1e9, 1),
            "nom_propose": datetime.fromtimestamp(min(dates)).strftime("%Y-%m-%d") + " Sortie",
        })
    return cartes


def choisir_dossier_dialogue(titre="Choisis le dossier de photos à trier"):
    """Fenêtre Windows de choix de dossier (sur le PC, pas dans le navigateur)."""
    resultat = {"chemin": ""}

    def ouvrir():
        try:
            import tkinter as tk
            from tkinter import filedialog
            racine = tk.Tk()
            racine.withdraw()
            racine.attributes("-topmost", True)
            racine.update()
            resultat["chemin"] = filedialog.askdirectory(title=titre, parent=racine) or ""
            racine.destroy()
        except Exception as exc:
            print("  ! fenêtre de choix impossible :", exc)
    t = threading.Thread(target=ouvrir)
    t.start()
    t.join()
    return resultat["chemin"]


# ---------------------------------------------------------------------------
# L'application
# ---------------------------------------------------------------------------
class Appli:
    def __init__(self):
        config_tri.DONNEES.mkdir(parents=True, exist_ok=True)
        self.fichier_biblio = config_tri.DONNEES / "bibliotheque.json"
        self.fichier_imports = config_tri.DONNEES / "imports.json"
        self.sessions = {}
        self.verrou = threading.Lock()
        self.tache = {"etat": "repos"}
        self.etat_ia = {"verifie": False, "dispo": False, "gpu": False, "nom": ""}
        self.cle = secrets.token_urlsafe(8)
        self.serveur_tel = None
        self.port = 0
        self.install_ia = None  # processus d'installation de l'IA en cours
        threading.Thread(target=self._verifier_ia, daemon=True).start()

    # --- Installation de l'IA depuis l'appli ---------------------------------------
    def lancer_installation_ia(self):
        import subprocess
        import sys
        if self.install_ia is not None and self.install_ia.poll() is None:
            return False
        (config_tri.DONNEES / "installation_ia.etat").write_text("en cours", encoding="utf-8")
        self.install_ia = subprocess.Popen([sys.executable, str(DOSSIER_OUTIL / "outils" / "installer_ia.py")],
                                           cwd=str(DOSSIER_OUTIL))

        def attendre():
            self.install_ia.wait()
            import importlib
            importlib.invalidate_caches()
            self._verifier_ia()
        threading.Thread(target=attendre, daemon=True).start()
        return True

    def reparer_installation(self):
        """Relance l'installateur complet dans une nouvelle fenêtre PowerShell (dernière version)."""
        import subprocess
        import urllib.request
        import mise_a_jour
        if os.name != "nt":
            raise RuntimeError("La réparation automatique ne fonctionne que sous Windows.")
        try:
            req = urllib.request.Request(f"https://api.github.com/repos/{mise_a_jour.DEPOT}/commits/main",
                                         headers={"User-Agent": "tri-photos", "Accept": "application/vnd.github.sha"})
            with urllib.request.urlopen(req, timeout=8) as r:
                ref = r.read().decode().strip()[:40]
        except Exception:
            raise RuntimeError("Pas de connexion à internet : la réparation a besoin d'internet.")
        url = f"https://raw.githubusercontent.com/{mise_a_jour.DEPOT}/{ref}/installer.ps1"
        subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-Command",
                          f"irm {url} | iex"], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return True

    def etat_installation_ia(self):
        f_etat = config_tri.DONNEES / "installation_ia.etat"
        f_log = config_tri.DONNEES / "installation_ia.log"
        if not f_etat.exists():
            return None
        try:
            lignes = [l for l in f_log.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        except Exception:
            lignes = []
        etat = f_etat.read_text(encoding="utf-8").strip()
        if etat == "en cours" and (self.install_ia is None or self.install_ia.poll() is not None):
            etat = "interrompue"
        return {"etat": etat, "lignes": lignes[-6:], "journal": str(f_log)}

    def _verifier_ia(self):
        try:
            import ia
            if ia.disponible():
                import torch
                gpu = torch.cuda.is_available()
                self.etat_ia = {"verifie": True, "dispo": True, "gpu": gpu,
                                "nom": torch.cuda.get_device_name(0) if gpu else "processeur"}
                return
        except Exception:
            pass
        self.etat_ia = {"verifie": True, "dispo": False, "gpu": False, "nom": ""}

    # --- Bibliothèque des sorties ---------------------------------------------------
    def biblio(self):
        return lire_json(self.fichier_biblio, [])

    def ajouter_biblio(self, dossier: Path):
        a = lire_json(dossier / "_TRI" / "analyse.json", {})
        with self.verrou:
            liste = [s for s in self.biblio() if s["sid"] != sid_de(dossier)]
            premiere = (a.get("photos") or [{}])[0].get("date")
            liste.append({"sid": sid_de(dossier), "dossier": str(dossier), "nom": dossier.name,
                          "date": premiere or time.time(), "ajoute": time.time()})
            ecrire_json(self.fichier_biblio, liste)

    def sorties(self):
        res = []
        for s in sorted(self.biblio(), key=lambda s: -s.get("date", 0)):
            d = Path(s["dossier"])
            info = dict(s, present=(d / "_TRI" / "analyse.json").exists())
            if info["present"]:
                a = lire_json(d / "_TRI" / "analyse.json", {})
                dec = lire_json(d / "_TRI" / "decisions.json", {})
                photos = a.get("photos", [])
                info["n_photos"] = len(photos)
                info["mode_ia"] = a.get("mode_ia")
                c = {k: 0 for k in STATUTS}
                for p in photos:
                    statut = dec.get(p["id"]) or {"meilleure": "meilleure", "rejet": "supprimer"}.get(
                        p.get("suggestion"), "garder")
                    c[statut] = c.get(statut, 0) + 1
                info["compte"] = c
                couv = next((p["id"] for p in photos if dec.get(p["id"]) == "meilleure"), None) or \
                    next((p["id"] for p in photos if p.get("suggestion") == "meilleure"), None)
                info["couverture"] = couv
                info["exporte"] = (d / "_TRI" / "export").exists()
                info["a_jour"] = a.get("version") == analyse.VERSION_ANALYSE
            res.append(info)
        return res

    def retirer(self, sid):
        with self.verrou:
            ecrire_json(self.fichier_biblio, [s for s in self.biblio() if s["sid"] != sid])
        ancienne_session = self.sessions.pop(sid, None)
        if ancienne_session:
            ancienne_session.actif = False

    def session(self, sid) -> Session:
        if sid not in self.sessions:
            s = next((x for x in self.biblio() if x["sid"] == sid), None)
            if s is None:
                raise KeyError("sortie inconnue")
            self.sessions[sid] = Session(Path(s["dossier"]))
            threading.Thread(target=self.sessions[sid].prechauffer, daemon=True).start()
        return self.sessions[sid]

    # --- Tâches de fond (import, analyse) ------------------------------------------
    def occupe(self):
        return self.tache.get("etat") == "en cours"

    def _progres(self, etape, fait, total, message=""):
        self.tache.update(etape=etape, fait=fait, total=total, message=message)

    def lancer_analyse(self, dossier: Path, reanalyser=False):
        if self.occupe():
            raise RuntimeError("Une autre tâche est en cours, attends qu'elle se termine.")
        if not dossier.is_dir():
            raise FileNotFoundError(f"Dossier introuvable : {dossier}")
        sid = sid_de(dossier)
        if not reanalyser and (dossier / "_TRI" / "analyse.json").exists() and self._analyse_a_jour(dossier):
            self.ajouter_biblio(dossier)
            return {"sid": sid, "deja": True}
        self.tache = {"etat": "en cours", "type": "analyse", "sid": sid, "nom": dossier.name,
                      "etape": "Préparation", "fait": 0, "total": 0, "message": "", "debut": time.time()}
        threading.Thread(target=self._analyser, args=(dossier, reanalyser), daemon=True).start()
        return {"sid": sid, "deja": False}

    def _analyse_a_jour(self, dossier: Path):
        a = lire_json(dossier / "_TRI" / "analyse.json", {})
        if a.get("version") != analyse.VERSION_ANALYSE:
            return False
        deplacees = {d["id"] for d in lire_json(dossier / "_TRI" / "deplacements.json", [])}
        actuelles = {e["id"] for e in analyse.lister_photos(dossier)}
        return actuelles | deplacees == {p["id"] for p in a.get("photos", [])}

    def _analyser(self, dossier: Path, reanalyser):
        sid = sid_de(dossier)
        try:
            ancienne_session = self.sessions.pop(sid, None)
            if ancienne_session:
                ancienne_session.actif = False
            sortie = dossier / "_TRI"
            sortie.mkdir(exist_ok=True)
            # On garde les choix faits à la main ; les autres suivront les nouvelles suggestions
            ancienne = lire_json(sortie / "analyse.json", {})
            anciennes_dec = lire_json(sortie / "decisions.json", {})
            defaut = {p["id"]: {"meilleure": "meilleure", "rejet": "supprimer"}.get(p.get("suggestion"), "garder")
                      for p in ancienne.get("photos", [])}
            manuelles = {pid: d for pid, d in anciennes_dec.items() if d in STATUTS and d != defaut.get(pid)}
            rot_manuelles = {p["id"]: p.get("rotation", 0) for p in ancienne.get("photos", [])
                             if p.get("rotation", 0) != p.get("rotation_auto", 0)}
            shutil.rmtree(sortie / "cache", ignore_errors=True)
            resultat = analyse.lancer(dossier, sortie, max(1, (os.cpu_count() or 2) - 1),
                                      progres=self._progres)
            if resultat is None:
                raise RuntimeError("Aucune photo lisible dans ce dossier.")
            ids = {p["id"] for p in resultat["photos"]}
            ecrire_json(sortie / "decisions.json", {k: v for k, v in manuelles.items() if k in ids})
            if rot_manuelles:  # rotations faites à la main : on les réapplique
                for p in resultat["photos"]:
                    if p["id"] not in rot_manuelles:
                        continue
                    delta = (rot_manuelles[p["id"]] - p.get("rotation", 0)) % 360
                    if delta:
                        mini = sortie / "miniatures" / p["miniature"]
                        with Image.open(mini) as im:
                            im.rotate(-delta, expand=True).save(mini, quality=90)
                        if delta in (90, 270):
                            p["largeur"], p["hauteur"] = p["hauteur"], p["largeur"]
                        p["rotation"] = rot_manuelles[p["id"]]
                        p["badges"] = [b for b in p.get("badges", []) if b != "redressée automatiquement"]
                ecrire_json(sortie / "analyse.json", resultat)
            self.ajouter_biblio(dossier)
            self.tache.update(etat="termine", etape="Terminé",
                              message=f"{resultat['nb_meilleures']} meilleures proposées sur "
                                      f"{len(resultat['photos'])} photos.")
        except Exception as exc:
            traceback.print_exc()
            noter_erreur("Tâche de fond\n" + traceback.format_exc())
            self.tache.update(etat="erreur", message=str(exc))

    def lancer_import(self, racine: str, nom: str, analyser_ensuite=True):
        if self.occupe():
            raise RuntimeError("Une autre tâche est en cours, attends qu'elle se termine.")
        nom = "".join(c for c in (nom or "").strip() if c not in '<>:"/\\|?*').strip() or \
            datetime.now().strftime("%Y-%m-%d Sortie")
        dest = Path(config_tri.config_locale()["dossier_import"]) / nom
        self.tache = {"etat": "en cours", "type": "import", "sid": sid_de(dest), "nom": nom,
                      "etape": "Copie depuis la carte", "fait": 0, "total": 0, "message": "", "debut": time.time()}
        threading.Thread(target=self._importer, args=(Path(racine), dest, analyser_ensuite), daemon=True).start()
        return {"sid": sid_de(dest), "dossier": str(dest)}

    def _importer(self, racine: Path, dest: Path, analyser_ensuite):
        try:
            deja = set(lire_json(self.fichier_imports, []))
            fichiers = sorted((f for f in (racine / "DCIM").rglob("*")
                               if f.is_file() and f.suffix.lower() in EXT_PHOTOS and _cle_import(f) not in deja),
                              key=lambda f: (f.stat().st_mtime, f.name))
            if not fichiers:
                raise RuntimeError("Aucune nouvelle photo sur la carte : tout a déjà été importé.")
            dest.mkdir(parents=True, exist_ok=True)
            total = sum(f.stat().st_size for f in fichiers)
            copie = 0
            for k, f in enumerate(fichiers, 1):
                cible = dest / f.name
                if cible.exists() and cible.stat().st_size != f.stat().st_size:
                    cible = dest / f"{f.parent.name}_{f.name}"  # même nom dans deux sous-dossiers de la carte
                if not cible.exists():
                    shutil.copy2(f, cible)
                deja.add(_cle_import(f))
                copie += f.stat().st_size
                self.tache.update(fait=k, total=len(fichiers),
                                  message=f"{copie / 1e9:.1f} / {total / 1e9:.1f} Go")
                if k % 20 == 0:
                    ecrire_json(self.fichier_imports, sorted(deja))
            ecrire_json(self.fichier_imports, sorted(deja))
            print(f"  {len(fichiers)} fichiers copiés dans {dest}")
            if analyser_ensuite:
                self.tache.update(type="analyse", etape="Préparation", fait=0, total=0, message="")
                self._analyser(dest, reanalyser=True)
            else:
                self.tache.update(etat="termine", message=f"{len(fichiers)} fichiers copiés.")
        except Exception as exc:
            traceback.print_exc()
            noter_erreur("Tâche de fond\n" + traceback.format_exc())
            self.tache.update(etat="erreur", message=str(exc))

    # --- Accès depuis le téléphone ---------------------------------------------------
    def activer_telephone(self):
        if self.serveur_tel is None:
            try:
                self.serveur_tel = ThreadingHTTPServer(("0.0.0.0", PORT_TELEPHONE), creer_gestionnaire(self))
            except OSError:
                self.serveur_tel = ThreadingHTTPServer(("0.0.0.0", 0), creer_gestionnaire(self))
            threading.Thread(target=self.serveur_tel.serve_forever, daemon=True).start()
        port = self.serveur_tel.server_address[1]
        return [f"http://{ip}:{port}/?cle={self.cle}" for ip in adresses_locales()]

    def recharger_reglages(self):
        analyse.recharger_reglages()
        developpement.recharger_reglages()
        for s in self.sessions.values():
            s.vider_cache_retouche()


def adresses_locales():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # aucun paquet envoyé, sert à trouver l'adresse
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


# ---------------------------------------------------------------------------
# Requêtes HTTP
# ---------------------------------------------------------------------------
def creer_gestionnaire(appli: Appli):
    class Gestionnaire(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        # --- utilitaires ---
        def _envoyer(self, code, data: bytes, type_, cache=False, entetes=None):
            self.send_response(code)
            self.send_header("Content-Type", type_)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
            for k, v in (entetes or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._envoyer(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                          "application/json; charset=utf-8")

        def _page(self, nom, entetes=None):
            self._envoyer(200, (DOSSIER_OUTIL / nom).read_bytes(), "text/html; charset=utf-8", entetes=entetes)

        def _image(self, chemin):
            with open(chemin, "rb") as fh:
                self._envoyer(200, fh.read(), "image/jpeg", cache=True)

        def _corps(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

        def _autorise(self, url):
            """Depuis le PC : toujours. Depuis le téléphone : seulement avec la clé."""
            if self.client_address[0] in ("127.0.0.1", "::1") and self.server.server_address[0] != "0.0.0.0":
                return True, None
            cle = parse_qs(url.query).get("cle", [None])[0]
            if cle == appli.cle:
                return True, {"Set-Cookie": f"cle={appli.cle}; Path=/; SameSite=Strict; Max-Age=604800"}
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return ("cle" in c and c["cle"].value == appli.cle), None

        def _erreur(self, exc, code=500):
            if not isinstance(exc, (KeyError, FileNotFoundError, RuntimeError, ValueError)):
                traceback.print_exc()
                noter_erreur(f"{self.command} {self.path}\n{traceback.format_exc()}")
            try:
                self._json({"erreur": str(exc).strip("'")}, code)
            except Exception:
                pass

        # --- GET ---
        def do_GET(self):
            url = urlparse(self.path)
            chemin = unquote(url.path)
            ok, cookie = self._autorise(url)
            if not ok:
                return self._envoyer(403, "Accès refusé : ouvre le lien affiché sur le PC.".encode("utf-8"),
                                     "text/plain; charset=utf-8")
            try:
                if chemin == "/":
                    return self._page("accueil.html", cookie)
                if chemin == "/reglages":
                    return self._page("reglages.html")
                if chemin == "/diagnostic":
                    return self._page("diagnostic.html")
                if chemin == "/api/diagnostic":
                    return self._json(diagnostic())
                if chemin == "/api/ping":
                    return self._json({"ok": True, "appli": "tri-photos"})
                if chemin == "/api/accueil":
                    deja = set(lire_json(appli.fichier_imports, []))
                    return self._json({
                        "version": version(), "ia": appli.etat_ia, "tache": appli.tache,
                        "sorties": appli.sorties(), "cartes": cartes_sd(deja),
                        "dossier_import": config_tri.config_locale()["dossier_import"],
                        "telephone": appli.serveur_tel is not None,
                        "installation_ia": appli.etat_installation_ia(),
                        "python": platform.python_version(),
                        "python_ok": (3, 10) <= sys.version_info[:2] <= (3, 13),
                        "sur_telephone": self.client_address[0] not in ("127.0.0.1", "::1")})
                if chemin == "/api/tache":
                    return self._json(appli.tache)
                if chemin == "/api/reglages":
                    valeurs, infos = config_tri.charger()
                    return self._json({"valeurs": valeurs, "defauts": config_tri.valeurs_par_defaut(),
                                       "schema": config_tri.SCHEMA, "formats": config_tri.FORMATS,
                                       "choix": config_tri.CHOIX, "infos": infos,
                                       "locale": config_tri.config_locale()})
                if chemin.startswith("/s/"):
                    return self._get_sortie(chemin, cookie)
                self._envoyer(404, b"introuvable", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except KeyError as exc:
                self._erreur(exc, 404)
            except Exception as exc:
                self._erreur(exc)

        def _get_sortie(self, chemin, cookie):
            morceaux = chemin.split("/", 3)  # ['', 's', sid, reste]
            sid = morceaux[2]
            reste = morceaux[3] if len(morceaux) > 3 else ""
            if reste == "":
                if not chemin.endswith("/"):
                    return self._envoyer(301, b"", "text/plain", entetes={"Location": f"/s/{sid}/"})
                appli.session(sid)
                return self._page("interface.html", cookie)
            s = appli.session(sid)
            if reste == "donnees":
                cfg, infos = config_tri.charger()
                return self._json({"analyse": s.analyse, "decisions": s.decisions, "export": s.export,
                                   "recup": s.recup,
                                   "reglages_infos": infos, "envoi": cfg["envoi_google_photos"],
                                   "deplacees": len(s.deplacees())})
            if reste == "export":
                return self._json(s.export)
            if reste == "envoi":
                return self._json(s.envoi)
            if reste.startswith("copie/"):
                return self._json(s.copies.get(reste.split("/", 1)[1]) or {"etat": "aucune"})
            if reste.startswith("recup_infos/"):
                pid = reste.split("/", 1)[1]
                if pid not in s.photos:
                    raise KeyError("photo inconnue")
                return self._json(s.infos_recup(pid))
            if reste.startswith("recup/"):
                pid = reste.split("/", 1)[1].rsplit(".", 1)[0]
                if pid not in s.photos:
                    raise KeyError("photo inconnue")
                q = parse_qs(urlparse(self.path).query)
                try:
                    reglages = json.loads(q.get("r", ["{}"])[0])
                except ValueError:
                    reglages = {}
                if not isinstance(reglages, dict):
                    reglages = {}
                for k in developpement.CURSEURS:  # ancien format : un paramètre par réglage
                    if k in q:
                        reglages[k] = q[k][0]
                data, perdu, partiel = s.rendu_recup(pid, reglages, q.get("zones", ["0"])[0] == "1",
                                                     recadrer=q.get("recadrer", ["1"])[0] == "1")
                entetes = {}
                if perdu is not None:
                    entetes = {"X-Perdu": f"{perdu:.2f}", "X-Partiel": f"{partiel:.2f}"}
                return self._envoyer(200, data, "image/jpeg", entetes=entetes)
            type_, _, nom = reste.partition("/")
            pid = nom.rsplit(".", 1)[0]
            if type_ in ("mini", "original", "retouche") and pid in s.photos:
                if type_ == "mini":
                    return self._image(s.sortie / "miniatures" / s.photos[pid]["miniature"])
                return self._image(s.rendu(pid, type_))
            self._envoyer(404, b"introuvable", "text/plain")

        # --- POST ---
        def do_POST(self):
            url = urlparse(self.path)
            chemin = url.path
            ok, _ = self._autorise(url)
            if not ok:
                return self._json({"erreur": "accès refusé"}, 403)
            local = self.client_address[0] in ("127.0.0.1", "::1")
            try:
                corps = self._corps()
                if chemin.startswith("/s/"):
                    return self._post_sortie(chemin, corps, local)
                if chemin == "/api/ouvrir":
                    dossier = corps.get("chemin", "").strip().strip('"')
                    if not dossier:
                        if not local:
                            raise ValueError("Le choix de dossier se fait sur le PC.")
                        dossier = choisir_dossier_dialogue()
                        if not dossier:
                            return self._json({"annule": True})
                    return self._json(appli.lancer_analyse(Path(dossier).expanduser().resolve(),
                                                           bool(corps.get("reanalyser"))))
                if chemin == "/api/reanalyser":
                    s = next(x for x in appli.biblio() if x["sid"] == corps.get("sid"))
                    return self._json(appli.lancer_analyse(Path(s["dossier"]), reanalyser=True))
                if chemin == "/api/importer":
                    return self._json(appli.lancer_import(corps["racine"], corps.get("nom", "")))
                if chemin == "/api/retirer":
                    appli.retirer(corps.get("sid"))
                    return self._json({"ok": True})
                if chemin == "/api/ouvrir_dossier_sortie":
                    s = next(x for x in appli.biblio() if x["sid"] == corps.get("sid"))
                    ouvrir_dossier(Path(s["dossier"]))
                    return self._json({"ok": True})
                if chemin == "/api/installer_ia":
                    if not local:
                        raise ValueError("L'installation se lance depuis le PC.")
                    return self._json({"ok": appli.lancer_installation_ia()})
                if chemin == "/api/reparer":
                    if not local:
                        raise ValueError("La réparation se lance depuis le PC.")
                    return self._json({"ok": appli.reparer_installation()})
                if chemin == "/api/ouvrir_journal_ia":
                    ouvrir_dossier(config_tri.DONNEES / "installation_ia.log")
                    return self._json({"ok": True})
                if chemin == "/api/telephone":
                    return self._json({"urls": appli.activer_telephone()})
                if chemin == "/api/reglages":
                    if not isinstance(corps, dict):
                        raise ValueError("format inattendu")
                    config_tri.sauvegarder(corps)
                    appli.recharger_reglages()
                    return self._json({"ok": True})
                if chemin == "/api/config_locale":
                    if not local:
                        raise ValueError("À modifier depuis le PC.")
                    champs = {k: corps.get(k) for k in ("dossier_partage", "dossier_import") if k in corps}
                    if champs.get("dossier_partage") and not Path(champs["dossier_partage"]).is_dir():
                        raise ValueError("Dossier de partage introuvable.")
                    actuels, _ = config_tri.charger()
                    config_tri.sauver_config_locale(**champs)
                    if not config_tri.fichier_reglages().exists():
                        config_tri.sauvegarder(actuels)  # le nouveau dossier partagé reprend les réglages actuels
                    appli.recharger_reglages()
                    return self._json({"ok": True, "locale": config_tri.config_locale()})
                if chemin == "/api/choisir_dossier":
                    if not local:
                        raise ValueError("Le choix de dossier se fait sur le PC.")
                    return self._json({"chemin": choisir_dossier_dialogue(corps.get("titre") or "Choisis un dossier")})
                if chemin == "/api/quitter":
                    if not local:
                        raise ValueError("L'appli se ferme depuis le PC.")
                    self._json({"ok": True})
                    threading.Thread(target=arreter, daemon=True).start()
                    return
                self._json({"erreur": "inconnu"}, 404)
            except StopIteration:
                self._erreur(KeyError("sortie inconnue"), 404)
            except (KeyError, FileNotFoundError, RuntimeError, ValueError) as exc:
                self._erreur(exc, 400)
            except Exception as exc:
                self._erreur(exc)

        def _post_sortie(self, chemin, corps, local):
            _, _, sid, action = chemin.split("/", 3)
            s = appli.session(sid)
            if action == "decision":
                ids = corps.get("ids") or [corps.get("id")]
                statut = corps.get("statut")
                if statut not in STATUTS or not all(i in s.photos for i in ids):
                    return self._json({"erreur": "décision invalide"}, 400)
                for i in ids:
                    s.decisions[i] = statut
                s.sauver_decisions()
                return self._json({"ok": True})
            if action == "exporter":
                return self._json({"ok": s.lancer_export()})
            if action == "pivoter":
                pid = corps.get("id")
                if pid not in s.photos:
                    return self._json({"erreur": "photo inconnue"}, 400)
                return self._json({"rotation": s.pivoter(pid, int(corps.get("sens", 90))),
                                   "largeur": s.photos[pid]["largeur"], "hauteur": s.photos[pid]["hauteur"],
                                   "badges": s.photos[pid].get("badges", [])})
            if action == "recup":
                pid = corps.get("id")
                if pid not in s.photos:
                    return self._json({"erreur": "photo inconnue"}, 400)
                s.sauver_recup(pid, corps.get("reglages"))
                return self._json({"ok": True, "recup": s.recup})
            if action == "copie":
                pid = corps.get("id")
                if pid not in s.photos:
                    return self._json({"erreur": "photo inconnue"}, 400)
                if corps.get("reglages"):
                    s.sauver_recup(pid, corps["reglages"])
                return self._json(s.enregistrer_copie(pid))
            if action == "ouvrir_retouches":
                d = s.sortie / "retouches"
                d.mkdir(exist_ok=True)
                ouvrir_dossier(d)
                return self._json({"ok": True})
            if action == "preparer_envoi":
                mode = corps.get("mode", "originaux")
                if mode not in ("originaux", "jpeg_appareil", "retouchees"):
                    raise ValueError("mode inconnu")
                s.preparer_envoi(corps.get("ids") or [], mode, ouvrir=local)
                return self._json(s.envoi)
            if action == "mettre_a_lecart":
                return self._json({"deplacees": s.mettre_a_lecart()})
            if action == "restaurer":
                return self._json({"restaurees": s.restaurer()})
            if action == "ouvrir_dossier":
                ouvrir_dossier(s.sortie / "export" if corps.get("quoi") == "export" else s.dossier)
                return self._json({"ok": True})
            self._json({"erreur": "inconnu"}, 404)

    return Gestionnaire


_SERVEURS = []


def arreter():
    time.sleep(0.3)
    for s in _SERVEURS:
        try:
            s.shutdown()
        except Exception:
            pass


def demarrer(port=8765, ouvrir_page=None):
    """Démarre l'appli. ouvrir_page : chemin à ouvrir dans le navigateur (ou None)."""
    import webbrowser
    appli = Appli()
    try:
        serveur = ThreadingHTTPServer(("127.0.0.1", port), creer_gestionnaire(appli))
    except OSError:
        serveur = ThreadingHTTPServer(("127.0.0.1", 0), creer_gestionnaire(appli))
    appli.port = serveur.server_address[1]
    _SERVEURS.append(serveur)
    url = f"http://127.0.0.1:{appli.port}/"
    print(f"Tri Photos {version()} est lancé : {url}")
    print("Garde cette fenêtre ouverte (tu peux la réduire). Pour arrêter : bouton « Quitter » dans l'appli.")
    if ouvrir_page is not None:
        webbrowser.open(url + ouvrir_page.lstrip("/"))
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        pass
    if appli.serveur_tel:
        appli.serveur_tel.shutdown()
    serveur.server_close()
    return appli
