# -*- coding: utf-8 -*-
"""
Point d'entrée de Tri Photos (lancé par l'icône du bureau).

    python app.py                 ouvre l'accueil
    python app.py "D:\\Photos\\X"   ouvre directement ce dossier (glisser-déposer sur l'icône)
"""

import json
import multiprocessing
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

PORT = 8765


def deja_lance():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")).get("appli") == "tri-photos"
    except Exception:
        return False


def envoyer_dossier(dossier):
    """Appli déjà ouverte : on lui demande d'ouvrir ce dossier."""
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/ouvrir", method="POST",
                                 data=json.dumps({"chemin": dossier}).encode("utf-8"))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rep = json.loads(r.read().decode("utf-8"))
        return f"s/{rep['sid']}/" if rep.get("deja") else ""
    except Exception:
        return ""


def main():
    dossier = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    sans_navigateur = "--pas-de-navigateur" in sys.argv
    if deja_lance():
        page = envoyer_dossier(dossier) if dossier else ""
        if not sans_navigateur:
            webbrowser.open(f"http://127.0.0.1:{PORT}/{page}")
        print("Tri Photos est déjà ouvert : affichage dans le navigateur.")
        time.sleep(1.5)
        return

    import serveur
    page = ""
    if dossier:
        import threading

        def ouvrir_plus_tard():
            time.sleep(1.0)
            envoyer_dossier(str(Path(dossier).resolve()))
        threading.Thread(target=ouvrir_plus_tard, daemon=True).start()
    serveur.demarrer(PORT, ouvrir_page=None if sans_navigateur else page)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
