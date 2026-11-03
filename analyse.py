# -*- coding: utf-8 -*-
"""
Analyse d'une sortie photo : mesures, diagnostic, regroupement, notation,
sélection variée. Résultat enregistré dans _TRI/analyse.json.
"""

import json
import math
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

import config_tri

try:
    import rawpy
except ImportError:
    rawpy = None

try:
    import exifread
except ImportError:
    exifread = None

CONFIG, INFOS_REGLAGES = config_tri.charger()


def recharger_reglages():
    """Relit les réglages (ils ont pu changer depuis le lancement de l'appli)."""
    global CONFIG, INFOS_REGLAGES
    CONFIG, INFOS_REGLAGES = config_tri.charger()

EXT_RAW = {".nef", ".nrw", ".cr2", ".cr3", ".crw", ".arw", ".srf", ".sr2", ".raf",
           ".orf", ".rw2", ".pef", ".srw", ".rwl", ".dng", ".3fr", ".iiq"}
EXT_JPG = {".jpg", ".jpeg"}
TAILLE_APERCU = 1200    # côté long pour les mesures
TAILLE_MINIATURE = 640  # côté long des vignettes de la grille (et pour l'IA)
VERSION_ANALYSE = 4


# ---------------------------------------------------------------------------
# Lecture des fichiers
# ---------------------------------------------------------------------------
def lister_photos(dossier: Path):
    """Associe RAW et JPG du même nom (une seule entrée par prise de vue)."""
    entrees = {}
    for f in sorted(dossier.iterdir()):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext not in EXT_RAW | EXT_JPG:
            continue
        e = entrees.setdefault(f.stem.lower(), {"id": f.stem, "raw": None, "jpg": None})
        if ext in EXT_RAW:
            e["raw"] = str(f)
        else:
            e["jpg"] = str(f)
    liste = list(entrees.values())
    if rawpy is None:
        sans_jpg = [e for e in liste if e["raw"] and not e["jpg"]]
        if sans_jpg:
            print(f"  ! {len(sans_jpg)} fichiers RAW ignorés : la librairie rawpy n'est pas installée.")
        liste = [e for e in liste if e["jpg"]]
        for e in liste:
            e["raw"] = None
    return liste


def lire_exif(chemin: str):
    infos = {"date": None, "orientation": 1, "marque": "", "modele": "", "iso": None,
             "vitesse": "", "ouverture": "", "focale": ""}
    if exifread is not None:
        try:
            with open(chemin, "rb") as fh:
                tags = exifread.process_file(fh, details=False)
            d = str(tags.get("EXIF DateTimeOriginal", "") or tags.get("Image DateTime", ""))
            sub = str(tags.get("EXIF SubSecTimeOriginal", "") or "0").strip()
            if d.strip():
                infos["date"] = datetime.strptime(d.strip(), "%Y:%m:%d %H:%M:%S").timestamp()
                if sub.isdigit():
                    infos["date"] += float("0." + sub)
            o = tags.get("Image Orientation")
            if o is not None and o.values:
                infos["orientation"] = int(o.values[0])
            infos["marque"] = str(tags.get("Image Make", "")).strip()
            infos["modele"] = str(tags.get("Image Model", "")).strip()
            if tags.get("EXIF ISOSpeedRatings"):
                infos["iso"] = str(tags["EXIF ISOSpeedRatings"])
            if tags.get("EXIF ExposureTime"):
                infos["vitesse"] = str(tags["EXIF ExposureTime"])
            if tags.get("EXIF FNumber"):
                v = tags["EXIF FNumber"].values[0]
                infos["ouverture"] = f"f/{float(v.num) / float(v.den):.1f}" if hasattr(v, "num") else f"f/{v}"
            if tags.get("EXIF FocalLength"):
                v = tags["EXIF FocalLength"].values[0]
                infos["focale"] = f"{float(v.num) / float(v.den):.0f} mm" if hasattr(v, "num") else f"{v} mm"
        except Exception:
            pass
    if infos["date"] is None:
        try:
            with Image.open(chemin) as im:
                ex = im.getexif()
                d = ex.get_ifd(0x8769).get(0x9003) or ex.get(0x0132)
                if d:
                    infos["date"] = datetime.strptime(str(d).strip(), "%Y:%m:%d %H:%M:%S").timestamp()
                infos["orientation"] = int(ex.get(0x0112, 1) or 1)
                infos["marque"] = infos["marque"] or str(ex.get(0x010F, "")).strip()
                infos["modele"] = infos["modele"] or str(ex.get(0x0110, "")).strip()
        except Exception:
            pass
    if infos["date"] is None:
        infos["date"] = os.path.getmtime(chemin)
    return infos


def orientation_raw(raw, defaut):
    """Orientation lue par LibRaw (plus fiable que l'EXIF pour les RAW)."""
    try:
        return {3: 3, 5: 8, 6: 6}.get(int(raw.sizes.flip), 1)
    except Exception:
        return defaut


def appliquer_orientation(img: np.ndarray, orientation: int) -> np.ndarray:
    if orientation == 3:
        return cv2.rotate(img, cv2.ROTATE_180)
    if orientation == 6:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if orientation == 8:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def redimensionner(img: np.ndarray, cote_long: int) -> np.ndarray:
    h, w = img.shape[:2]
    f = cote_long / max(h, w)
    if f >= 1:
        return img
    return cv2.resize(img, (round(w * f), round(h * f)), interpolation=cv2.INTER_AREA)


def image_integree(raw, orientation):
    """Aperçu JPEG intégré au RAW (souvent en pleine taille sur les Nikon récents)."""
    vignette = raw.extract_thumb()
    if vignette.format == rawpy.ThumbFormat.JPEG:
        bgr = cv2.imdecode(np.frombuffer(vignette.data, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            return None
        img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    else:
        img = np.asarray(vignette.data)
    return appliquer_orientation(img, orientation)


def charger_apercu(entree, orientation_exif):
    """Aperçu remis à l'endroit + part du capteur saturée + orientation notée par l'appareil."""
    stats_capteur, img = None, None
    orientation = orientation_exif
    if entree["raw"] and rawpy is not None:
        with rawpy.imread(entree["raw"]) as raw:
            orientation = orientation_raw(raw, orientation_exif)
            brut = raw.raw_image_visible[::4, ::4]
            stats_capteur = float(np.mean(brut >= 0.97 * float(raw.white_level)))
            if not entree["jpg"]:
                try:
                    img = image_integree(raw, orientation)
                except Exception:
                    img = None
                if img is None or max(img.shape[:2]) < 900:
                    img = raw.postprocess(half_size=True, use_camera_wb=True)
    if img is None:
        with Image.open(entree["jpg"]) as im:
            img = np.asarray(ImageOps.exif_transpose(im).convert("RGB"))
    return redimensionner(img, TAILLE_APERCU), stats_capteur, orientation


def tourner(img: np.ndarray, degres: int) -> np.ndarray:
    """Rotation dans le sens des aiguilles d'une montre (0, 90, 180, 270)."""
    degres %= 360
    if degres == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if degres == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    if degres == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def rotation_conseillee(rgb: np.ndarray) -> int:
    """Photo couchée ? On cherche le ciel (clair, bleuté, peu de détails) sur un côté.
    Renvoie la rotation à appliquer (90 = ciel à gauche, 270 = ciel à droite) ou 0.
    Volontairement prudent : en cas de doute, on ne tourne pas."""
    h0, w0 = rgb.shape[:2]
    petit = cv2.resize(rgb, (240, max(2, round(240 * h0 / w0))), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    h, w = petit.shape[:2]
    gris = petit.mean(axis=2)
    tex = np.abs(cv2.Laplacian(cv2.GaussianBlur(gris, (0, 0), 1), cv2.CV_32F))
    tmoy = tex.mean() + 1e-4
    bleu = petit[..., 2] - petit[..., 0]

    def ciel(zone):
        return gris[zone].mean() + 0.5 * max(0.0, float(bleu[zone].mean())) - 0.35 * min(2.0, tex[zone].mean() / tmoy)
    bh, bw = max(2, int(h * 0.18)), max(2, int(w * 0.18))
    s = {"haut": ciel(np.s_[:bh, :]), "bas": ciel(np.s_[-bh:, :]),
         "gauche": ciel(np.s_[:, :bw]), "droite": ciel(np.s_[:, -bw:])}
    if max(s["gauche"], s["droite"]) - max(s["haut"], s["bas"]) > 0.15 and abs(s["gauche"] - s["droite"]) > 0.3:
        return 90 if s["gauche"] > s["droite"] else 270
    return 0


# ---------------------------------------------------------------------------
# Mesures
# ---------------------------------------------------------------------------
def nettete(gris: np.ndarray) -> float:
    """Netteté des contours, indépendante du contraste et de la luminosité.

    Pour les contours les plus marqués, on compare la pente du contour à sa
    hauteur : un contour net passe du clair au sombre en 1-2 pixels, un contour
    flou s'étale. Une photo sombre ou douce (coucher de soleil, ciel, brume)
    n'est donc plus prise pour une photo floue. Mesuré séparément à
    l'horizontale et à la verticale pour repérer aussi le flou de bougé.
    Échelle (aperçu de 1200 px) : ~25-35 = net, ~19 = léger flou, <16 = flou."""
    g = gris.astype(np.float32) / 255.0
    g = cv2.GaussianBlur(g, (0, 0), 0.7)  # le grain ne compte pas comme du détail
    mesures = []
    for dx, dy, fen in ((1, 0, (9, 1)), (0, 1, (1, 9))):
        pente = np.abs(cv2.Sobel(g, cv2.CV_32F, dx, dy, ksize=3)) / 8.0
        noyau = np.ones((fen[1], fen[0]), np.uint8)
        hauteur = cv2.dilate(g, noyau) - cv2.erode(g, noyau)
        contours = hauteur > 0.05
        if contours.sum() < 200:
            mesures.append((0.0, 0.0))
            continue
        pc, hc = pente[contours], hauteur[contours]
        forts = pc >= np.percentile(pc, 97)
        mesures.append((float(np.median(pc[forts] / (hc[forts] + 1e-4))) * 100,
                        float(np.percentile(pc, 99))))
    (sx, fx), (sy, fy) = mesures
    if fx == 0 and fy == 0:
        return 0.0
    if fx >= 0.35 * fy and fy >= 0.35 * fx:
        return min(sx, sy)  # contours dans les deux sens : le plus flou des deux (bougé)
    return sx if fx > fy else sy


def empreinte_visuelle(gris: np.ndarray) -> list:
    petit = cv2.resize(gris, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(petit)[:8, :8]
    return (dct > np.median(dct)).flatten().astype(int).tolist()


def couleurs(rgb: np.ndarray) -> float:
    r, g, b = [c.astype(np.float32) for c in cv2.split(rgb)]
    rg, yb = r - g, 0.5 * (r + g) - b
    return float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def empreinte_simple(rgb: np.ndarray) -> np.ndarray:
    """Empreinte de contenu sans IA : répartition des couleurs + disposition."""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, [12, 4, 4], [0, 180, 0, 256, 0, 256]).flatten()
    hist = np.sqrt(hist / (hist.sum() + 1e-6))
    gris = cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), (12, 12), interpolation=cv2.INTER_AREA)
    g = gris.astype(np.float32).flatten()
    g = (g - g.mean()) / (g.std() + 1e-6) * 0.08
    v = np.concatenate([hist, g]).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-6)


def analyser(entree, dossier_miniatures):
    """Analyse d'une prise de vue. Tourne dans un processus séparé."""
    source = entree["raw"] or entree["jpg"]
    exif = lire_exif(source)
    apercu, capteur_sature, orientation = charger_apercu(entree, exif["orientation"])
    rotation = 0
    # L'appareil n'a pas noté d'orientation et la photo est en paysage : elle est peut-être couchée
    if orientation in (0, 1, None) and apercu.shape[1] > apercu.shape[0]:
        rotation = rotation_conseillee(apercu)
        apercu = tourner(apercu, rotation)
    gris = cv2.cvtColor(apercu, cv2.COLOR_RGB2GRAY)
    lum = gris.astype(np.float32) / 255.0
    mini = redimensionner(apercu, TAILLE_MINIATURE)
    nom_mini = entree["id"] + ".jpg"
    Image.fromarray(mini).save(os.path.join(dossier_miniatures, nom_mini), quality=90)
    return {
        **entree,
        **{k: exif[k] for k in ("date", "marque", "modele", "iso", "vitesse", "ouverture", "focale")},
        "miniature": nom_mini,
        "rotation": rotation,
        "rotation_auto": rotation,
        "largeur": int(apercu.shape[1]),
        "hauteur": int(apercu.shape[0]),
        "nettete": nettete(gris),
        "luminosite": float(lum.mean()),
        "p99": float(np.percentile(lum, 99)),
        "part_blanc": float(np.mean(gris >= 250)),
        "part_noir": float(np.mean(gris <= 5)),
        "capteur_sature": capteur_sature,
        "contraste": float(lum.std()),
        "couleurs": couleurs(mini),
        "phash": empreinte_visuelle(gris),
        "_empreinte_simple": empreinte_simple(mini),
    }


# ---------------------------------------------------------------------------
# Diagnostic, regroupement, notation, sélection
# ---------------------------------------------------------------------------
def diagnostiquer(photos):
    seuil_flou = CONFIG["seuil_nettete"]
    leger = seuil_flou + 3
    for p in photos:
        rejets, badges = [], []
        if p["luminosite"] < CONFIG["seuil_noire"] and p["p99"] < 0.10:
            rejets.append(("noire", f"Photo quasiment noire (luminosité {p['luminosite']:.0%})."))
        elif p["luminosite"] < 0.15:
            badges.append("sombre, rattrapable" if p["raw"] else "sombre")
        if not rejets:
            if p["nettete"] < seuil_flou:
                rejets.append(("floue", f"Floue : netteté {p['nettete']:.1f} (en dessous de {seuil_flou:.1f}). "
                                        f"Vérifie avec le zoom 100 % (touche Z)."))
            elif p["nettete"] < leger:
                badges.append("légèrement floue")
        if p["part_blanc"] > CONFIG["seuil_cramee_apercu"]:
            if p["capteur_sature"] is not None:
                if p["capteur_sature"] > CONFIG["seuil_cramee_capteur"]:
                    rejets.append(("cramée irrécupérable",
                                   f"{p['capteur_sature']:.0%} du capteur est saturé : ces zones sont "
                                   f"définitivement blanches, même en RAW."))
                elif p["capteur_sature"] > 0.02:
                    badges.append(f"partiellement rattrapable ({p['capteur_sature']:.0%} perdu)")
                else:
                    badges.append("cramée, rattrapable")
            elif p["part_blanc"] > CONFIG["seuil_cramee_jpeg"]:
                rejets.append(("cramée irrécupérable",
                               f"{p['part_blanc']:.0%} de l'image est blanche et il n'y a pas de RAW pour la récupérer."))
            else:
                badges.append("hautes lumières perdues")
        if p.get("rotation_auto"):
            badges.append("redressée automatiquement")
        p["rejets"] = [r[0] for r in rejets]
        p["explications_rejet"] = [r[1] for r in rejets]
        p["badges"] = badges
    return seuil_flou


def similarite(a, b):
    return float(np.dot(a, b))


def regrouper(photos, empreintes, seuil_serie):
    ordre = sorted(range(len(photos)), key=lambda i: (photos[i]["date"], photos[i]["id"]))
    moments, series = [], []
    prec = None
    for i in ordre:
        p = photos[i]
        nouveau_moment = prec is None or (p["date"] - photos[prec]["date"]) > CONFIG["ecart_moment_min"] * 60
        if nouveau_moment:
            moments.append([])
        meme_serie = False
        if not nouveau_moment and (p["date"] - photos[prec]["date"]) <= CONFIG["ecart_serie_s"]:
            dist = sum(1 for a, b in zip(p["phash"], photos[prec]["phash"]) if a != b)
            meme_serie = dist <= CONFIG["distance_ressemblance"] or \
                similarite(empreintes[i], empreintes[prec]) >= seuil_serie
        if not meme_serie:
            series.append([])
            moments[-1].append(len(series) - 1)
        series[-1].append(i)
        p["moment"] = len(moments) - 1
        p["serie"] = len(series) - 1
        prec = i
    return moments, series


def rang(valeurs):
    v = np.asarray(valeurs, dtype=np.float64)
    if len(v) <= 1:
        return np.ones_like(v)
    return v.argsort().argsort() / (len(v) - 1)


def noter(photos, notes_esthetiques):
    r_net = rang([math.log1p(p["nettete"]) for p in photos])
    r_coul = rang([p["couleurs"] for p in photos])
    r_contr = rang([p["contraste"] for p in photos])
    r_aes = rang(notes_esthetiques) if notes_esthetiques is not None else None
    for i, p in enumerate(photos):
        # Pénalités seulement au-delà de petites zones : un soleil ou un reflet
        # cramé ne doit pas faire écarter un coucher de soleil.
        if p["capteur_sature"] is not None:
            penalite = max(0.0, p["capteur_sature"] - 0.02) * 5
        else:
            penalite = max(0.0, p["part_blanc"] - 0.03) * 4
        penalite += max(0.0, p["part_noir"] - 0.10) * 2
        technique = 0.6 * r_net[i] + 0.4 * (1 - min(1.0, penalite))
        p["rang_nettete"] = float(r_net[i])
        if r_aes is not None:
            p["note_esthetique"] = round(float(notes_esthetiques[i]), 2)
            note = 0.6 * r_aes[i] + 0.3 * technique + 0.1 * r_coul[i]
        else:
            p["note_esthetique"] = None
            note = 0.45 * technique + 0.35 * r_coul[i] + 0.20 * r_contr[i]
        p["note"] = -1.0 if p["rejets"] else round(float(note) * 100, 1)


def selectionner(photos, moments, series, empreintes, base_sim, seuil_doublon, nombre=None):
    for p in photos:
        p.update({"meilleure_serie": False, "suggestion": "rejet" if p["rejets"] else "garder",
                  "raisons": list(p.get("explications_rejet", []))})
    # Meilleure de chaque série (la netteté compte double entre photos d'une même rafale)
    for s in series:
        valides = [i for i in s if not photos[i]["rejets"]]
        if not valides:
            continue
        nets = rang([photos[i]["nettete"] for i in valides]) if len(valides) > 1 else [1.0]
        scores = {i: photos[i]["note"] + 15 * nets[k] for k, i in enumerate(valides)}
        top = max(valides, key=lambda i: scores[i])
        photos[top]["meilleure_serie"] = True
        for i in valides:
            if i != top:
                photos[i]["suggestion"] = "doublon"
                photos[i]["raisons"].append(f"Série de {len(s)} photos similaires : "
                                            f"{photos[top]['id']} est la meilleure.")
                photos[i]["proche_de"] = photos[top]["id"]
        if len(s) > 1:
            photos[top]["raisons"].append(f"Meilleure de sa série de {len(s)} photos similaires.")

    candidates = [i for i, p in enumerate(photos) if p["meilleure_serie"]]
    if nombre is None:
        nombre = max(CONFIG["min_selection"], round(CONFIG["part_selection"] * len(photos)))
    nombre = min(nombre, len(candidates))
    lam = CONFIG["diversite"]
    seuil_effectif = 1 - (1 - seuil_doublon) * (0.5 + lam) / 1.1

    def penalite(i, choisies):
        if not choisies:
            return 0.0, None
        sims = [(similarite(empreintes[i], empreintes[j]), j) for j in choisies]
        s, j = max(sims)
        pen = max(0.0, (s - base_sim) / (1 - base_sim))
        # trop proche d'une photo déjà retenue : on préfère une sélection plus courte
        trop_proche = s >= seuil_effectif or lam * pen > 0.3
        return pen, (j if trop_proche else None)

    choisies = []
    # 1) la meilleure de chaque moment
    for m in range(len(moments)):
        du_moment = [i for i in candidates if photos[i]["moment"] == m]
        if du_moment:
            choisies.append(max(du_moment, key=lambda i: photos[i]["note"]))
    choisies = sorted(choisies, key=lambda i: -photos[i]["note"])[:nombre]
    for i in choisies:
        photos[i]["raisons"].append(f"Meilleure photo du moment {photos[i]['moment'] + 1}.")
    # 2) on complète en favorisant la variété (pas deux fois la même scène)
    restantes = [i for i in candidates if i not in choisies]
    ecartees = {}
    while len(choisies) < nombre and restantes:
        meilleur, val = None, -1e9
        for i in restantes:
            pen, doublon = penalite(i, choisies)
            if doublon is not None:
                ecartees[i] = doublon
                continue
            v = photos[i]["note"] / 100 - lam * pen
            if v > val:
                meilleur, val = i, v
        if meilleur is None:
            break
        choisies.append(meilleur)
        restantes.remove(meilleur)
        restantes = [i for i in restantes if i not in ecartees]
    for i in choisies:
        photos[i]["suggestion"] = "meilleure"
    for i, j in ecartees.items():
        if photos[i]["suggestion"] != "meilleure":
            photos[i]["raisons"].append(f"Pas proposée : scène très proche de {photos[j]['id']}, déjà retenue.")
            photos[i]["proche_de"] = photos[j]["id"]
    # Explications de la note
    for i in choisies:
        p = photos[i]
        if p["note_esthetique"] is not None:
            p["raisons"].append(f"Note esthétique IA : {p['note_esthetique']:.1f}/10.")
        if p["rang_nettete"] >= 0.7:
            p["raisons"].append("Parmi les plus nettes de la sortie.")
        if any("cramée, rattrapable" in b for b in p["badges"]):
            p["raisons"].append("Paraît cramée, mais le RAW permet de récupérer les hautes lumières.")
        if any(b.startswith("sombre, rattrapable") for b in p["badges"]):
            p["raisons"].append("Sous-exposée, mais le RAW permet de l'éclaircir proprement.")
        if p["note_esthetique"] is None:
            p["raisons"].append(f"Note globale {p['note']:.0f}/100 (netteté, exposition, couleurs, contraste).")
    return choisies


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------
def _progres_console(etape, fait, total, message=""):
    if total and (fait % 25 == 0 or fait == total):
        print(f"    {etape} : {fait}/{total} {message}")
    elif not total:
        print(f"  {etape} {message}")


def lancer(dossier: Path, sortie: Path, processus: int, utiliser_ia=True, nombre=None, progres=None):
    """Analyse complète d'une sortie. progres(etape, fait, total, message) suit l'avancement."""
    progres = progres or _progres_console
    recharger_reglages()
    miniatures = sortie / "miniatures"
    miniatures.mkdir(parents=True, exist_ok=True)
    entrees = lister_photos(dossier)
    print(f"\n{len(entrees)} prises de vue trouvées dans {dossier}")
    if not entrees:
        return None

    photos, erreurs = [], []
    progres("Analyse des photos", 0, len(entrees))
    with ProcessPoolExecutor(max_workers=processus) as pool:
        futurs = {pool.submit(analyser, e, str(miniatures)): e for e in entrees}
        for k, f in enumerate(as_completed(futurs), 1):
            try:
                photos.append(f.result())
            except Exception as exc:
                erreurs.append((futurs[f]["id"], str(exc)))
            progres("Analyse des photos", k, len(entrees))
    for nom, msg in erreurs:
        print(f"  ! {nom} illisible : {msg}")
    if not photos:
        return None
    photos.sort(key=lambda p: (p["date"], p["id"]))

    notes_esth, mode_ia = None, "aucune"
    empreintes = np.stack([p.pop("_empreinte_simple") for p in photos])
    base_sim, seuil_doublon, seuil_serie = 0.70, 0.92, 0.985
    if utiliser_ia:
        import ia
        if ia.disponible():
            try:
                progres("Chargement de l'IA", 0, 0, "(quelques secondes)")
                modele = ia.obtenir()
                ou = "carte graphique" if modele.appareil == "cuda" else "processeur"
                emb, notes_esth = modele.traiter(
                    [str(miniatures / p["miniature"]) for p in photos],
                    progres=lambda f, t: progres(f"IA ({ou})", f, t))
                empreintes = emb
                base_sim, seuil_doublon, seuil_serie = 0.75, 0.93, 0.95
                mode_ia = "clip+esthetique" if notes_esth is not None else "clip"
            except Exception as exc:
                print(f"  ! IA indisponible ({exc}) : analyse sans IA.")
        else:
            print("    IA non installée : tri sur critères techniques et couleurs.")

    progres("Tri et sélection", 0, 0)
    seuil_flou = diagnostiquer(photos)
    moments, series = regrouper(photos, empreintes, seuil_serie)
    noter(photos, notes_esth)
    choisies = selectionner(photos, moments, series, empreintes, base_sim, seuil_doublon, nombre)

    resultat = {
        "version": VERSION_ANALYSE,
        "dossier": str(dossier),
        "nom": dossier.name,
        "date_analyse": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "mode_ia": mode_ia,
        "seuil_flou": seuil_flou,
        "reglages": INFOS_REGLAGES,
        "nb_meilleures": len(choisies),
        "photos": photos,
        "moments": [[photos[i]["id"] for s in idx for i in series[s]] for idx in moments],
        "series": [[photos[i]["id"] for i in s] for s in series],
    }
    (sortie / "analyse.json").write_text(json.dumps(resultat, ensure_ascii=False), encoding="utf-8")
    n_rej = sum(1 for p in photos if p["rejets"])
    n_dbl = sum(1 for p in photos if p["suggestion"] == "doublon")
    print(f"    {len(choisies)} meilleures proposées, {n_dbl} doublons, {n_rej} rejets suggérés.")
    if erreurs:
        resultat["erreurs"] = [f"{n} : {m}" for n, m in erreurs]
        (sortie / "analyse.json").write_text(json.dumps(resultat, ensure_ascii=False), encoding="utf-8")
    return resultat
