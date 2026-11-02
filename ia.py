# -*- coding: utf-8 -*-
"""
IA locale de Tri Photos (optionnelle, gratuite, fonctionne hors connexion
une fois les modèles téléchargés).

- CLIP (OpenAI, ViT-L/14) : "comprend" le contenu de chaque photo. Sert à
  repérer les photos qui se ressemblent (même scène, même sujet) même si elles
  ont été prises à plusieurs minutes d'écart, pour varier la sélection.
- LAION Aesthetic Predictor : note esthétique de 1 à 10, apprise sur des
  centaines de milliers de photos notées par des humains.

Tourne sur la carte graphique NVIDIA si elle est là (rapide), sinon sur le
processeur (plus lent, mais ça marche : quelques minutes pour 500 photos).

Les modèles sont téléchargés à l'installation (ensuite, plus besoin d'internet) :
    python ia.py --telecharger
"""

import os
import sys
from pathlib import Path

import numpy as np

import config_tri

DOSSIER_MODELES = config_tri.DONNEES / "modeles"
MARQUEUR_PRET = DOSSIER_MODELES / "pret"
os.environ.setdefault("HF_HOME", str(DOSSIER_MODELES / "hf"))
if MARQUEUR_PRET.exists():
    # Modèles déjà téléchargés : on ne cherche pas internet (utile en montagne)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
URL_ESTHETIQUE = ("https://github.com/christophschuhmann/improved-aesthetic-predictor/"
                  "raw/main/sac+logos+ava1-l14-linearMSE.pth")
FICHIER_ESTHETIQUE = DOSSIER_MODELES / "laion_aesthetic_vit_l14.pth"


def disponible() -> bool:
    try:
        import torch  # noqa: F401
        import open_clip  # noqa: F401
        return True
    except ImportError:
        return False


class ModeleIA:
    def __init__(self, avec_esthetique=True):
        import torch
        import open_clip
        from torch import nn

        self.torch = torch
        self.appareil = "cuda" if torch.cuda.is_available() else "cpu"
        DOSSIER_MODELES.mkdir(parents=True, exist_ok=True)

        self.modele, _, self.pretraitement = open_clip.create_model_and_transforms(
            "ViT-L-14", pretrained="openai", cache_dir=str(DOSSIER_MODELES))
        self.modele.eval().to(self.appareil)

        self.esthetique = None
        if avec_esthetique:
            try:
                if not FICHIER_ESTHETIQUE.exists():
                    print("    Téléchargement du modèle esthétique (une seule fois)…")
                    torch.hub.download_url_to_file(URL_ESTHETIQUE, str(FICHIER_ESTHETIQUE))
                mlp = nn.Sequential(
                    nn.Linear(768, 1024), nn.Dropout(0.2),
                    nn.Linear(1024, 128), nn.Dropout(0.2),
                    nn.Linear(128, 64), nn.Dropout(0.1),
                    nn.Linear(64, 16), nn.Linear(16, 1))
                etat = torch.load(str(FICHIER_ESTHETIQUE), map_location="cpu")
                etat = {k.replace("layers.", ""): v for k, v in etat.items()}
                mlp.load_state_dict(etat)
                self.esthetique = mlp.eval().to(self.appareil)
            except Exception as exc:
                print(f"  ! Modèle esthétique indisponible ({exc}). On garde CLIP pour la variété.")
                self.esthetique = None

    def traiter(self, chemins, taille_lot=16, progres=None):
        """Renvoie (empreintes Nx768 normalisées, notes esthétiques N ou None)."""
        from PIL import Image
        torch = self.torch
        empreintes, notes = [], []
        for i in range(0, len(chemins), taille_lot):
            lot = []
            for c in chemins[i:i + taille_lot]:
                with Image.open(c) as im:
                    lot.append(self.pretraitement(im.convert("RGB")))
            x = torch.stack(lot).to(self.appareil)
            with torch.no_grad():
                if self.appareil == "cuda":
                    with torch.autocast("cuda", dtype=torch.float16):
                        e = self.modele.encode_image(x)
                else:
                    e = self.modele.encode_image(x)
                e = e.float()
                e = e / e.norm(dim=-1, keepdim=True)
                if self.esthetique is not None:
                    notes.extend(self.esthetique(e).squeeze(-1).cpu().numpy().tolist())
            empreintes.append(e.cpu().numpy().astype(np.float32))
            fait = min(i + taille_lot, len(chemins))
            if progres:
                progres(fait, len(chemins))
            elif fait % 64 < taille_lot or fait == len(chemins):
                print(f"    IA : {fait}/{len(chemins)}")
        return np.concatenate(empreintes), (np.array(notes, dtype=np.float32) if notes else None)


_MODELE = None


def obtenir():
    """Modèle chargé une seule fois et gardé en mémoire tant que l'appli tourne."""
    global _MODELE
    if _MODELE is None:
        _MODELE = ModeleIA()
    return _MODELE


if __name__ == "__main__":
    if "--telecharger" in sys.argv:
        if not disponible():
            print("L'IA n'est pas installée : relance l'installation de Tri Photos.")
            sys.exit(1)
        m = ModeleIA()
        if m.esthetique is not None:
            MARQUEUR_PRET.write_text("ok", encoding="utf-8")
        print("Modèles prêts dans", DOSSIER_MODELES)
        print("Calcul sur :", "carte graphique" if m.appareil == "cuda" else "processeur")
        print("Note esthétique :", "oui" if m.esthetique is not None else "non")
