# -*- coding: utf-8 -*-
"""
Emplacements et réglages de Tri Photos.

- Données de l'appli (liste des sorties, réglages, modèles d'IA) :
  %LOCALAPPDATA%\\TriPhotos
- Réglages de tri : reglages.json, dans ce dossier ou dans un "dossier de
  partage" (par exemple un dossier Google Drive) pour avoir les mêmes
  réglages sur deux PC.
"""

import json
import os
import platform
from datetime import datetime
from pathlib import Path

DONNEES = Path(os.environ.get("TRI_PHOTOS_DONNEES") or
               (Path(os.environ["LOCALAPPDATA"]) / "TriPhotos" if os.environ.get("LOCALAPPDATA")
                else Path.home() / ".tri_photos"))
FICHIER_LOCAL = DONNEES / "config_locale.json"


# ---------------------------------------------------------------------------
# Réglages propres à ce PC
# ---------------------------------------------------------------------------
def dossier_images() -> Path:
    """Dossier "Images" de Windows (suit les redirections OneDrive)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            import uuid
            guid = uuid.UUID("{33E28130-4E1E-4676-835A-98395C3BC3BB}")  # FOLDERID_Pictures

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
            g = GUID(guid.fields[0], guid.fields[1], guid.fields[2],
                     (ctypes.c_ubyte * 8)(*guid.bytes[8:]))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(ptr)) == 0:
                chemin = Path(ptr.value)
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                return chemin
        except Exception:
            pass
    return Path.home() / "Pictures"


def config_locale() -> dict:
    d = {"dossier_partage": "", "dossier_import": str(dossier_images() / "Tri Photos")}
    try:
        d.update(json.loads(FICHIER_LOCAL.read_text(encoding="utf-8")))
    except Exception:
        pass
    return d


def sauver_config_locale(**valeurs):
    d = config_locale()
    d.update({k: str(v) for k, v in valeurs.items() if v is not None})
    DONNEES.mkdir(parents=True, exist_ok=True)
    FICHIER_LOCAL.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def fichier_reglages() -> Path:
    partage = config_locale().get("dossier_partage", "").strip()
    if partage and Path(partage).is_dir():
        return Path(partage) / "reglages.json"
    return DONNEES / "reglages.json"


# ---------------------------------------------------------------------------
# Réglages de tri (partageables)
# Chaque réglage : clé -> (défaut, libellé, explication, min, max, pas, groupe)
# ---------------------------------------------------------------------------
SCHEMA = {
    "seuil_nettete": (16.5, "Sévérité du flou",
                      "Une photo est proposée à la suppression si sa netteté est en dessous de cette valeur. "
                      "16,5 = flou bien visible ; 19 = rejette aussi le léger flou ; 14 = seulement le très flou. "
                      "La mesure ne dépend pas de la luminosité : une photo sombre n'est pas comptée floue.",
                      12, 24, 0.5, "Tri"),
    "seuil_noire": (0.025, "Photo noire",
                    "Luminosité en dessous de laquelle une photo est considérée comme noire (ratée).",
                    0.005, 0.08, 0.005, "Tri"),
    "seuil_cramee_apercu": (0.03, "Détection du cramé",
                            "Part de l'image complètement blanche à partir de laquelle on parle de photo cramée.",
                            0.01, 0.15, 0.01, "Tri"),
    "seuil_cramee_capteur": (0.20, "Cramé irrécupérable (RAW)",
                             "Part du capteur saturée à partir de laquelle même le RAW ne peut plus rien. "
                             "Plus haut = on garde plus de photos.", 0.05, 0.5, 0.01, "Tri"),
    "seuil_cramee_jpeg": (0.25, "Cramé irrécupérable (JPEG seul)",
                          "Même chose pour les photos sans fichier RAW.", 0.05, 0.6, 0.01, "Tri"),
    "ecart_serie_s": (90, "Durée d'une série (secondes)",
                      "Deux photos prises à moins de X secondes et qui se ressemblent forment une série "
                      "(seule la meilleure est proposée).", 5, 900, 5, "Regroupement"),
    "distance_ressemblance": (22, "Ressemblance pour une série",
                              "Plus la valeur est haute, plus des photos assez différentes sont regroupées.",
                              6, 34, 1, "Regroupement"),
    "ecart_moment_min": (45, "Nouveau moment après (minutes)",
                         "Un trou de plus de X minutes entre deux photos démarre un nouveau moment de la sortie.",
                         5, 240, 5, "Regroupement"),
    "part_selection": (0.10, "Taille de la sélection",
                       "Part de la sortie proposée comme « meilleures » (0,10 = 10 %).",
                       0.02, 0.4, 0.01, "Sélection"),
    "min_selection": (10, "Sélection minimum",
                      "Nombre minimum de meilleures, même pour une petite sortie.",
                      1, 60, 1, "Sélection"),
    "diversite": (0.6, "Variété de la sélection",
                  "Plus c'est haut, plus l'outil évite de proposer des photos qui se ressemblent "
                  "(même sujet, même endroit), au profit de scènes différentes.", 0.0, 1.0, 0.05, "Sélection"),
    "force_exposition": (0.6, "Force de la correction d'exposition",
                         "0 = ne touche pas l'exposition, 1 = correction complète vers une luminosité moyenne.",
                         0.0, 1.0, 0.05, "Retouche"),
    "exposition_min_ev": (-1.5, "Assombrissement maximum (IL)",
                          "Correction maximale vers le sombre, en diaphragmes.", -3.0, 0.0, 0.25, "Retouche"),
    "exposition_max_ev": (1.5, "Éclaircissement maximum (IL)",
                          "Correction maximale vers le clair, en diaphragmes.", 0.0, 3.0, 0.25, "Retouche"),
    "contraste_local": (0.5, "Relief et ombres débouchées",
                        "Donne du relief aux grandes formes et éclaircit les ombres, sans faire ressortir le grain. "
                        "0 = aucun.", 0.0, 1.0, 0.05, "Retouche"),
    "vibrance": (0.12, "Vibrance des couleurs",
                 "Relève les couleurs ternes sans saturer celles déjà vives.", 0.0, 0.4, 0.01, "Retouche"),
    "reduction_bruit": (0.5, "Réduction du bruit couleur",
                        "Efface les petites taches de couleur dans les zones sombres (photos de soirée, ISO élevés).",
                        0.0, 1.0, 0.05, "Retouche"),
    "qualite_jpeg": (97, "Qualité JPEG", "Qualité des JPEG retouchés (97 = visuellement sans perte).",
                     85, 100, 1, "Export"),
    "min_aire_recadrage": (0.6, "Recadrage maximum",
                           "Un format n'est créé que s'il garde au moins cette part de la photo "
                           "(0,6 = 60 %). Évite les recadrages qui zooment trop.", 0.3, 0.95, 0.05, "Export"),
}

# Choix à options : clé -> (défaut, libellé, explication, {valeur: libellé})
CHOIX = {
    "envoi_google_photos": (
        "originaux", "Fichiers à envoyer sur Google Photos",
        "Ce que contient le dossier « a_envoyer_google_photos » après l'export.",
        {"originaux": "Les originaux tels quels (RAW et/ou JPEG de l'appareil)",
         "jpeg_appareil": "Seulement les JPEG de l'appareil quand ils existent (moins lourd)",
         "retouchees": "Les versions retouchées automatiquement (JPEG, plus long à exporter)"}),
}

# Formats d'export : nom du dossier -> (largeur, hauteur, libellé)
FORMATS = {
    "fonds_ecran_PC_16-9": (3840, 2160, "Fond d'écran PC / cadre connecté (16:9, photos horizontales)"),
    "fonds_ecran_tel_9-16": (1440, 2560, "Fond d'écran téléphone (9:16, photos verticales)"),
    "instagram": (1080, 0, "Instagram (largeur 1080, format d'origine conservé si possible)"),
}


def valeurs_par_defaut():
    d = {k: v[0] for k, v in SCHEMA.items()}
    d.update({k: v[0] for k, v in CHOIX.items()})
    d["formats_actifs"] = list(FORMATS)
    return d


def nettoyer(valeurs: dict) -> dict:
    """Garde uniquement les réglages connus, dans les bornes autorisées."""
    propre = valeurs_par_defaut()
    for k, (defaut, _l, _e, mn, mx, _p, _g) in SCHEMA.items():
        if k not in valeurs:
            continue
        try:
            v = float(valeurs[k])
        except (TypeError, ValueError):
            continue
        v = min(max(v, mn), mx)
        propre[k] = int(round(v)) if isinstance(defaut, int) else round(v, 4)
    for k, (_d, _l, _e, options) in CHOIX.items():
        if valeurs.get(k) in options:
            propre[k] = valeurs[k]
    if isinstance(valeurs.get("formats_actifs"), list):
        propre["formats_actifs"] = [f for f in valeurs["formats_actifs"] if f in FORMATS]
    return propre


def charger():
    """Réglages actuels, complétés par les valeurs par défaut."""
    f = fichier_reglages()
    infos = {"modifie_par": None, "modifie_le": None, "fichier": str(f)}
    if f.exists():
        try:
            brut = json.loads(f.read_text(encoding="utf-8"))
            infos["modifie_par"] = brut.get("_modifie_par")
            infos["modifie_le"] = brut.get("_modifie_le")
            return nettoyer(brut.get("reglages", brut)), infos
        except Exception as exc:
            print(f"  ! reglages.json illisible ({exc}) : réglages par défaut utilisés.")
    return valeurs_par_defaut(), infos


def sauvegarder(valeurs: dict):
    propre = nettoyer(valeurs)
    contenu = {
        "_explication": "Réglages de Tri Photos (modifiables dans l'appli, page Réglages).",
        "_modifie_par": os.environ.get("COMPUTERNAME") or platform.node() or "inconnu",
        "_modifie_le": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "reglages": propre,
    }
    f = fichier_reglages()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(contenu, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, f)
    return propre
