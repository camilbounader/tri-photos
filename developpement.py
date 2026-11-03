# -*- coding: utf-8 -*-
"""
Développement automatique (premier jet) et rendus haute qualité.

- apercu_original() : image en pleine qualité pour le diaporama (aperçu
  intégré au RAW, sans retouche), mise en cache.
- apercu_retouche() : même chose avec la retouche automatique, pour comparer.
- exporter_meilleure() : développement pleine résolution + formats.
"""

import os
from datetime import datetime

import cv2
import numpy as np
from PIL import Image, ImageOps

import config_tri
from analyse import (appliquer_orientation, image_integree, orientation_raw,
                     redimensionner, rawpy, tourner)

CONFIG, _ = config_tri.charger()


def recharger_reglages():
    global CONFIG
    CONFIG, _ = config_tri.charger()
TAILLE_ECRAN = 3200  # côté long des images du diaporama (net même sur écran 4K)


# ---------------------------------------------------------------------------
# Développement
# ---------------------------------------------------------------------------
def srgb_vers_lineaire(x):
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)


def lineaire_vers_srgb(x):
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055).astype(np.float32)


def courbe_hautes_lumieres(x, seuil=0.45):
    """Ne touche pas aux ombres ni aux tons moyens, adoucit progressivement les
    hautes lumières au lieu de les couper net (c'est ce qui 'décrame')."""
    haut = seuil + (1 - seuil) * (1 - np.exp(-(x - seuil) / (1 - seuil)))
    return np.where(x <= seuil, x, haut).astype(np.float32)


def flou_rapide(canal: np.ndarray, sigma: float) -> np.ndarray:
    """Flou gaussien large calculé en basse résolution (rapide sur 24 Mpx)."""
    f = 0.25 if sigma > 8 else 1.0
    if f < 1:
        petit = cv2.resize(canal, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        petit = cv2.GaussianBlur(petit, (0, 0), sigma * f)
        return cv2.resize(petit, (canal.shape[1], canal.shape[0]), interpolation=cv2.INTER_LINEAR)
    return cv2.GaussianBlur(canal, (0, 0), sigma)


def developper(lin: np.ndarray, est_raw: bool) -> np.ndarray:
    """Image linéaire float32 (0-1) -> image sRGB uint8 développée."""
    lum = 0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]
    cle = float(np.exp(np.mean(np.log(lum[::8, ::8] + 1e-4))))
    gain = (0.18 / max(cle, 1e-4)) ** CONFIG["force_exposition"]
    if est_raw:
        gain = float(np.clip(gain, 2 ** CONFIG["exposition_min_ev"], 2 ** CONFIG["exposition_max_ev"]))
    else:
        gain = float(np.clip(gain, 2 ** -0.5, 2 ** 0.5))
    y = courbe_hautes_lumieres(lin * gain)
    del lum
    point_blanc = float(np.percentile(y[::8, ::8], 99.5))
    if 0 < point_blanc < 0.9:
        y *= min(0.95 / point_blanc, 1.6)
    srgb = lineaire_vers_srgb(y)  # float 0-1
    del y

    h, w = srgb.shape[:2]
    echelle = max(h, w) / 6000.0  # les rayons sont pensés pour un 24 Mpx

    # Réduction du bruit couleur : on lisse la couleur, pas les détails
    if CONFIG["reduction_bruit"] > 0:
        ycc = cv2.cvtColor(srgb, cv2.COLOR_RGB2YCrCb)
        s = max(0.8, 2.5 * echelle) * (0.5 + CONFIG["reduction_bruit"])
        ycc[..., 1] = cv2.GaussianBlur(ycc[..., 1], (0, 0), s)
        ycc[..., 2] = cv2.GaussianBlur(ycc[..., 2], (0, 0), s)
        srgb = cv2.cvtColor(ycc, cv2.COLOR_YCrCb2RGB)
        del ycc

    # Relief des grandes formes + ombres débouchées, sans toucher au grain fin
    a = CONFIG["contraste_local"]
    if a > 0:
        ycc = cv2.cvtColor(np.clip(srgb, 0, 1), cv2.COLOR_RGB2YCrCb)
        L = ycc[..., 0]
        base = flou_rapide(L, 60 * echelle + 1)
        poids = 1 - (2 * L - 1) ** 2  # surtout dans les tons moyens
        L2 = L + a * 0.5 * (L - base) * poids
        L2 = L2 + a * 0.35 * L2 * (1 - L2) ** 3  # éclaircit doucement les ombres
        ycc[..., 0] = np.clip(L2, 0, 1)
        srgb = cv2.cvtColor(ycc, cv2.COLOR_YCrCb2RGB)
        del ycc, L, base, poids, L2

    # Vibrance : relève surtout les couleurs ternes
    if CONFIG["vibrance"] > 0:
        srgb = np.clip(srgb, 0, 1)
        mx, mn = srgb.max(axis=2), srgb.min(axis=2)
        sat = (mx - mn) / (mx + 1e-6)
        gris = srgb.mean(axis=2, keepdims=True)
        srgb = gris + (srgb - gris) * (1 + CONFIG["vibrance"] * (1 - sat))[..., None]
    return (np.clip(srgb, 0, 1) * 255 + 0.5).astype(np.uint8)


def charger_lineaire(p, demi_taille=False, taille_max=None):
    """Renvoie (image linéaire float32 remise à l'endroit, est_raw)."""
    if p.get("raw") and rawpy is not None:
        with rawpy.imread(p["raw"]) as raw:
            try:
                flip_libraw = int(raw.sizes.flip)
            except Exception:
                flip_libraw = 0
            rgb16 = raw.postprocess(
                use_camera_wb=True, output_bps=16, gamma=(1, 1), no_auto_bright=True,
                highlight_mode=rawpy.HighlightMode.Blend, output_color=rawpy.ColorSpace.sRGB,
                half_size=demi_taille)
        if not flip_libraw and p.get("orientation_appareil", 1) in (3, 6, 8):
            rgb16 = appliquer_orientation(rgb16, p["orientation_appareil"])  # orientation lue dans l'EXIF
        if taille_max:
            rgb16 = redimensionner(rgb16, taille_max)
        lin, est_raw = rgb16.astype(np.float32) / 65535.0, True
    else:
        with Image.open(p["jpg"]) as im:
            rgb = np.asarray(ImageOps.exif_transpose(im).convert("RGB"))
        if demi_taille:
            rgb = redimensionner(rgb, max(rgb.shape[:2]) // 2)
        if taille_max:
            rgb = redimensionner(rgb, taille_max)
        lin, est_raw = srgb_vers_lineaire(rgb.astype(np.float32) / 255.0), False
    return np.ascontiguousarray(tourner(lin, p.get("rotation", 0))), est_raw


# ---------------------------------------------------------------------------
# Récupération des photos cramées (réglages choisis à la main)
# ---------------------------------------------------------------------------
# Curseurs de retouche : clé -> (valeur neutre, minimum, maximum)
CURSEURS = {
    "ev": (0.0, -4.0, 4.0),     # exposition (IL)
    "ct": (0, -100, 100),       # contraste
    "hl": (0, 0, 100),          # hautes lumières : assombrit localement ce qui est trop clair
    "om": (0, 0, 100),          # ombres : éclaircit localement ce qui est trop sombre
    "bl": (0, -100, 100),       # blancs
    "nr": (0, -100, 100),       # noirs
    "te": (0, -100, 100),       # température (bleu <-> jaune)
    "ti": (0, -100, 100),       # teinte (vert <-> magenta)
    "vi": (0, -100, 100),       # vibrance
    "sa": (0, -100, 100),       # saturation
    "cl": (0, -100, 100),       # clarté (relief des tons moyens)
    "ne": (0, 0, 100),          # netteté
    "an": (0.0, -15.0, 15.0),   # redresser (degrés, positif = sens des aiguilles d'une montre)
}
REGLAGES_DEFAUT = dict({k: v[0] for k, v in CURSEURS.items()}, crop=None)
REGLAGES_RECUP_DEFAUT = REGLAGES_DEFAUT  # ancien nom


def nettoyer_reglages(r):
    """Réglages complets et dans les bornes (les clés absentes prennent la valeur neutre)."""
    r = r or {}
    propre = {}
    for k, (neutre, mn, mx) in CURSEURS.items():
        try:
            propre[k] = float(min(max(float(r.get(k, neutre)), mn), mx))
        except (TypeError, ValueError):
            propre[k] = float(neutre)
    c = r.get("crop")
    try:
        x, y, w, h = [float(v) for v in c]
        x, y = min(max(x, 0.0), 0.95), min(max(y, 0.0), 0.95)
        w, h = min(max(w, 0.05), 1 - x), min(max(h, 0.05), 1 - y)
        propre["crop"] = None if (w > 0.999 and h > 0.999) else [round(x, 5), round(y, 5), round(w, 5), round(h, 5)]
    except (TypeError, ValueError):
        propre["crop"] = None
    return propre


def _cle(lin):
    lum = 0.2126 * lin[::4, ::4, 0] + 0.7152 * lin[::4, ::4, 1] + 0.0722 * lin[::4, ::4, 2]
    return float(np.exp(np.mean(np.log(lum + 1e-4))))


def reglages_auto(lin):
    """Point de départ équilibré (comme la retouche automatique)."""
    ev = float(np.clip(0.7 * np.log2(0.18 / max(_cle(lin), 1e-4)), -2.5, 2.0))
    return nettoyer_reglages({"ev": round(ev * 4) / 4, "hl": 35, "om": 25, "ct": 10, "cl": 20, "vi": 15})


def reglages_recuperation(lin):
    """Préréglage pour une photo trop claire ou cramée : on « gomme » les extrêmes."""
    ev = float(np.clip(np.log2(0.14 / max(_cle(lin), 1e-4)), -3.0, 0.5))
    return nettoyer_reglages({"ev": round(ev * 4) / 4, "hl": 90, "om": 35, "bl": -25, "ct": 5, "cl": 25, "vi": 10})


def _base_lissee(log_l):
    """Version lissée de la luminance (en log) qui respecte les contours : sert à
    éclaircir ou assombrir des zones entières sans écraser les détails."""
    h, w = log_l.shape
    f = min(1.0, 512.0 / max(h, w))
    petit = cv2.resize(log_l, (max(1, round(w * f)), max(1, round(h * f))), interpolation=cv2.INTER_AREA)
    petit = cv2.bilateralFilter(petit.astype(np.float32), 9, 1.2, 8)
    petit = cv2.GaussianBlur(petit, (0, 0), 1.0)
    return cv2.resize(petit, (w, h), interpolation=cv2.INTER_LINEAR)


def _douce(z, k=3.0):
    return np.log1p(np.exp(np.clip(k * z, -30, 30))) / k


def geometrie(img, r, interpolation=cv2.INTER_LANCZOS4):
    """Redresser (rotation fine) puis rogner."""
    a = r.get("an", 0)
    if a:
        H, W = img.shape[:2]
        rad = abs(a) * np.pi / 180
        k = np.cos(rad) + np.sin(rad) * max(W / H, H / W)  # zoom pour ne pas voir de coins vides
        M = cv2.getRotationMatrix2D((W / 2, H / 2), -a, k)
        img = cv2.warpAffine(img, M, (W, H), flags=interpolation, borderMode=cv2.BORDER_REFLECT)
    c = r.get("crop")
    if c:
        H, W = img.shape[:2]
        x0, y0 = int(round(c[0] * W)), int(round(c[1] * H))
        x1, y1 = int(round((c[0] + c[2]) * W)), int(round((c[1] + c[3]) * H))
        img = img[y0:max(y1, y0 + 2), x0:max(x1, x0 + 2)]
    return np.ascontiguousarray(img)


def developper_reglages(lin, est_raw, r, recadrer=True):
    """Développement avec les réglages de l'outil « Retoucher » (image linéaire -> sRGB uint8)."""
    r = nettoyer_reglages(r)
    x = lin.astype(np.float32, copy=True)
    h, w = x.shape[:2]
    echelle = max(h, w) / 6000.0

    # Balance des blancs (température / teinte), luminosité conservée
    te, ti = r["te"] / 100, r["ti"] / 100
    if te or ti:
        g = np.array([2 ** (0.35 * te), 2 ** (-0.25 * ti), 2 ** (-0.35 * te)], np.float32)
        x *= g / (0.2126 * g[0] + 0.7152 * g[1] + 0.0722 * g[2])

    x *= np.float32(2.0 ** r["ev"])

    # Hautes lumières / ombres : corrections locales (détails préservés)
    hl, om = r["hl"] / 100, r["om"] / 100
    if hl or om:
        L = np.maximum(0.2126 * x[..., 0] + 0.7152 * x[..., 1] + 0.0722 * x[..., 2], 1e-5)
        log_l = np.log2(L)
        base = _base_lissee(log_l)
        delta = -hl * 0.8 * _douce(base + 1.6) + om * 0.6 * _douce(-3.2 - base)
        x *= np.exp2(delta).astype(np.float32)[..., None]
        del L, log_l, base, delta

    # Épaule douce : les hautes lumières s'arrondissent au lieu d'être coupées net
    y = courbe_hautes_lumieres(np.maximum(x, 0), seuil=0.75)
    del x
    srgb = lineaire_vers_srgb(y)
    del y

    # Réduction du bruit couleur (réglage général de l'appli)
    if CONFIG["reduction_bruit"] > 0:
        ycc = cv2.cvtColor(srgb, cv2.COLOR_RGB2YCrCb)
        sg = max(0.8, 2.5 * echelle) * (0.5 + CONFIG["reduction_bruit"])
        ycc[..., 1] = cv2.GaussianBlur(ycc[..., 1], (0, 0), sg)
        ycc[..., 2] = cv2.GaussianBlur(ycc[..., 2], (0, 0), sg)
        srgb = cv2.cvtColor(ycc, cv2.COLOR_YCrCb2RGB)

    # Blancs, noirs, contraste (sur la luminosité perçue)
    bl, nr, ct, cl, ne = r["bl"] / 100, r["nr"] / 100, r["ct"] / 100, r["cl"] / 100, r["ne"] / 100
    if bl or nr or ct or cl or ne:
        ycc = cv2.cvtColor(np.clip(srgb, 0, 1).astype(np.float32), cv2.COLOR_RGB2YCrCb)
        Y = ycc[..., 0]
        Y0 = Y.copy()
        if bl:
            Y = Y + 0.30 * bl * Y ** 3
        if nr:
            Y = Y + 0.12 * nr * (1 - np.clip(Y, 0, 1)) ** 4
        if ct:
            Yc = np.clip(Y, 0, 1)
            Y = Y + ct * 0.9 * (Yc * Yc * (3 - 2 * Yc) - Yc)
        if cl:
            base = flou_rapide(Y, 40 * echelle + 1)
            poids = 1 - (2 * np.clip(Y, 0, 1) - 1) ** 2
            Y = Y + cl * 0.7 * (Y - base) * poids
        if ne:
            Y = Y + ne * 1.4 * (Y - cv2.GaussianBlur(Y, (0, 0), max(0.7, 1.6 * echelle)))
        # on applique le changement de luminosité aux trois couleurs
        gain = (np.clip(Y, 0, 1.2) + 1e-4) / (Y0 + 1e-4)
        srgb = np.clip(srgb, 0, 1) * np.clip(gain, 0, 4)[..., None]
        del ycc, Y, Y0, gain

    # Vibrance et saturation
    vi, sa = r["vi"] / 100, r["sa"] / 100
    if vi or sa:
        srgb = np.clip(srgb, 0, 1)
        gris = (0.299 * srgb[..., 0] + 0.587 * srgb[..., 1] + 0.114 * srgb[..., 2])[..., None]
        mx, mn = srgb.max(axis=2), srgb.min(axis=2)
        sat = ((mx - mn) / (mx + 1e-6))[..., None]
        facteur = (1 + sa) * (1 + vi * (1 - sat) * (1.0 if vi > 0 else 0.6))
        srgb = gris + (srgb - gris) * facteur

    img = (np.clip(srgb, 0, 1) * 255 + 0.5).astype(np.uint8)
    del srgb
    if recadrer:
        return geometrie(img, r)
    return geometrie(img, dict(r, crop=None))


def zones_perdues(lin, img, r=None, recadrer=True):
    """Colore en rouge les zones sans aucune information (blanc pur même dans le RAW)
    et en orange celles où une partie des couleurs est perdue. Renvoie (image, % perdu, % partiel)."""
    sature = lin >= 0.985
    perdu = sature.all(axis=2)
    partiel = sature.any(axis=2) & ~perdu
    pourcent = (float(perdu.mean() * 100), float(partiel.mean() * 100))
    masque = (perdu * 2 + partiel).astype(np.uint8)
    rr = nettoyer_reglages(r)
    if not recadrer:
        rr["crop"] = None
    masque = geometrie(masque, rr, interpolation=cv2.INTER_NEAREST)
    if masque.shape[:2] != img.shape[:2]:
        masque = cv2.resize(masque, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST)
    out = img.astype(np.float32)
    m2, m1 = masque == 2, masque == 1
    out[m2] = out[m2] * 0.3 + np.array([230, 40, 40]) * 0.7
    out[m1] = out[m1] * 0.55 + np.array([255, 150, 30]) * 0.45
    return out.astype(np.uint8), pourcent[0], pourcent[1]


def developper_photo(p, lin, est_raw):
    """Développement d'une photo : réglages « Récupérer » s'il y en a, sinon automatique."""
    if p.get("recup"):
        return developper_reglages(lin, est_raw, p["recup"])
    return developper(lin, est_raw)


# ---------------------------------------------------------------------------
# Rendus pour le diaporama (mis en cache)
# ---------------------------------------------------------------------------
def _enregistrer(img, chemin, qualite=93):
    Image.fromarray(img).save(chemin, quality=qualite, subsampling=0)


def apercu_original(p, chemin_cache):
    if os.path.exists(chemin_cache):
        return chemin_cache
    img = None
    if p.get("jpg"):
        with Image.open(p["jpg"]) as im:
            img = np.asarray(ImageOps.exif_transpose(im).convert("RGB"))
    elif p.get("raw") and rawpy is not None:
        with rawpy.imread(p["raw"]) as raw:
            try:
                img = image_integree(raw, orientation_raw(raw, p.get("orientation_appareil", 1)))
            except Exception:
                img = None
            if img is None or max(img.shape[:2]) < 2400:  # aperçu intégré trop petit
                img = raw.postprocess(half_size=True, use_camera_wb=True)
    img = tourner(img, p.get("rotation", 0))
    _enregistrer(redimensionner(img, TAILLE_ECRAN), chemin_cache)
    return chemin_cache


def apercu_retouche(p, chemin_cache):
    if os.path.exists(chemin_cache):
        return chemin_cache
    lin, est_raw = charger_lineaire(p, demi_taille=True)
    img = developper_photo(p, lin, est_raw)
    _enregistrer(redimensionner(img, TAILLE_ECRAN), chemin_cache)
    return chemin_cache


# ---------------------------------------------------------------------------
# Recadrages
# ---------------------------------------------------------------------------
def carte_saillance(rgb: np.ndarray) -> np.ndarray:
    """Zones qui attirent l'œil, calculées à grande échelle (on cherche le sujet
    principal, pas les petits détails comme des feuilles) + biais vers le centre."""
    petit = redimensionner(rgb, 64)
    lab = cv2.cvtColor(petit, cv2.COLOR_RGB2LAB).astype(np.float32)
    moyenne = lab.reshape(-1, 3).mean(axis=0)
    contraste = np.linalg.norm(lab - moyenne, axis=2)  # écart à la couleur moyenne
    sal = cv2.GaussianBlur(contraste, (0, 0), 4)
    sal = (sal - sal.min()) / (sal.max() - sal.min() + 1e-8)
    h, w = sal.shape
    yy, xx = np.mgrid[0:h, 0:w]
    centre = np.exp(-(((xx - w / 2) / w) ** 2 + ((yy - h / 2) / h) ** 2) * 3)
    return sal * 0.5 + centre * 0.5


def recadrer(rgb, sal, largeur, hauteur):
    H, W = rgb.shape[:2]
    ratio = largeur / hauteur
    cw, ch = (round(H * ratio), H) if W / H > ratio else (W, round(W / ratio))
    sh, sw = sal.shape
    fx, fy = sw / W, sh / H
    integ = cv2.integral(sal.astype(np.float64))
    pw, ph = max(1, round(cw * fx)), max(1, round(ch * fy))
    meilleur, pos = -1.0, (0, 0)
    for y0 in range(0, sh - ph + 1):
        for x0 in range(0, sw - pw + 1):
            s = integ[y0 + ph, x0 + pw] - integ[y0, x0 + pw] - integ[y0 + ph, x0] + integ[y0, x0]
            if s > meilleur:
                meilleur, pos = s, (x0, y0)
    x = min(max(0, round(pos[0] / fx)), W - cw)
    y = min(max(0, round(pos[1] / fy)), H - ch)
    crop = rgb[y:y + ch, x:x + cw]
    if cw > largeur:
        return cv2.resize(crop, (largeur, hauteur), interpolation=cv2.INTER_AREA)
    return crop  # jamais d'agrandissement : on garde la résolution native


def aire_conservee(W, H, largeur, hauteur):
    ratio = largeur / hauteur
    return (H * ratio * H) / (W * H) if W / H > ratio else (W * W / ratio) / (W * H)


def exif_pour_export(p):
    ex = Image.Exif()
    d = datetime.fromtimestamp(p["date"]).strftime("%Y:%m:%d %H:%M:%S")
    ex[0x0132] = d
    if p.get("marque"):
        ex[0x010F] = p["marque"]
    if p.get("modele"):
        ex[0x0110] = p["modele"]
    ex[0x0131] = "Tri Photos (developpement automatique)"
    ifd = ex.get_ifd(0x8769)
    ifd[0x9003] = d  # date de prise de vue : Google Photos s'en sert pour le classement
    ifd[0x9004] = d
    return ex.tobytes()


def sauver_jpeg(img, chemin, exif):
    Image.fromarray(img).save(chemin, quality=CONFIG["qualite_jpeg"], subsampling=0,
                              optimize=True, exif=exif)


def lier(source, dest):
    """Lien physique (instantané, ne prend pas de place en plus) ou copie si impossible."""
    import shutil
    if os.path.exists(dest):
        return
    try:
        os.link(source, dest)
    except OSError:
        shutil.copy2(source, dest)


def exporter_meilleure(p, dossier_meilleures, dossiers_formats):
    """Pour une meilleure : RAW original intact + JPEG retouché pleine résolution,
    puis les formats adaptés (fonds d'écran, Instagram)."""
    for cle in ("raw", "jpg"):
        f = p.get(cle)
        if f and os.path.exists(f):
            lier(f, os.path.join(dossier_meilleures, os.path.basename(f)))
    lin, est_raw = charger_lineaire(p)
    img = developper_photo(p, lin, est_raw)
    del lin
    exif = exif_pour_export(p)
    nom = p["id"] + ".jpg"
    sauver_jpeg(img, os.path.join(dossier_meilleures, p["id"] + "_retouche.jpg"), exif)
    crees = []
    H, W = img.shape[:2]
    sal = None
    for fmt, dossier in dossiers_formats.items():
        largeur, hauteur, _ = config_tri.FORMATS[fmt]
        if fmt == "instagram":
            ratio = min(max(W / H, 0.8), 1.91)  # Instagram accepte du 4:5 au 1,91:1
            largeur, hauteur = 1080, round(1080 / ratio)
        if aire_conservee(W, H, largeur, hauteur) < CONFIG["min_aire_recadrage"]:
            continue  # recadrage trop serré : on ne le fait pas
        if sal is None:
            sal = carte_saillance(img)
        sauver_jpeg(recadrer(img, sal, largeur, hauteur), os.path.join(dossier, nom), exif)
        crees.append(fmt)
    return p["id"], crees


def exporter_copie_retouchee(p, dossier):
    """Copie JPEG pleine résolution avec les réglages de l'outil « Retoucher »."""
    lin, est_raw = charger_lineaire(p)
    img = developper_photo(p, lin, est_raw)
    del lin
    chemin = os.path.join(dossier, p["id"] + "_retouche.jpg")
    sauver_jpeg(img, chemin, exif_pour_export(p))
    return chemin


def exporter_retouchee_simple(p, dossier):
    lin, est_raw = charger_lineaire(p)
    img = developper_photo(p, lin, est_raw)
    sauver_jpeg(img, os.path.join(dossier, p["id"] + ".jpg"), exif_pour_export(p))
    return p["id"]
