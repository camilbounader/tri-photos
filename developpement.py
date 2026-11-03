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
            rgb16 = raw.postprocess(
                use_camera_wb=True, output_bps=16, gamma=(1, 1), no_auto_bright=True,
                highlight_mode=rawpy.HighlightMode.Blend, output_color=rawpy.ColorSpace.sRGB,
                half_size=demi_taille)
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
REGLAGES_RECUP_DEFAUT = {"ev": 0.0, "hl": 60, "bl": 0, "om": 30}


def reglages_auto(lin):
    """Réglages de départ pour récupérer une photo trop claire (ou trop sombre)."""
    lum = 0.2126 * lin[::4, ::4, 0] + 0.7152 * lin[::4, ::4, 1] + 0.0722 * lin[::4, ::4, 2]
    cle = float(np.exp(np.mean(np.log(lum + 1e-4))))
    ev = float(np.clip(np.log2(0.16 / max(cle, 1e-4)), -3.0, 1.5))
    return {"ev": round(ev * 4) / 4, "hl": 75, "bl": -20, "om": 35}


def developper_reglages(lin, est_raw, r):
    """Développement avec les curseurs de l'outil « Récupérer » :
    ev = exposition (IL), hl = récupération des hautes lumières (0-100),
    bl = blancs (-100 à 100), om = ombres débouchées (0-100)."""
    ev = float(r.get("ev", 0)); hl = float(r.get("hl", 60)); bl = float(r.get("bl", 0)); om = float(r.get("om", 30))
    x = lin * (2.0 ** ev)
    y = courbe_hautes_lumieres(x, seuil=0.95 - 0.65 * np.clip(hl, 0, 100) / 100)
    srgb = lineaire_vers_srgb(y)
    w = np.clip(bl, -100, 100) / 100
    if w:
        srgb = srgb + 0.35 * w * srgb ** 3  # agit surtout sur les tons les plus clairs
    if om > 0:
        ycc = cv2.cvtColor(np.clip(srgb, 0, 1).astype(np.float32), cv2.COLOR_RGB2YCrCb)
        L = ycc[..., 0]
        ycc[..., 0] = np.clip(L + (om / 100) * 0.6 * L * (1 - L) ** 3, 0, 1)
        srgb = cv2.cvtColor(ycc, cv2.COLOR_YCrCb2RGB)
    if CONFIG["vibrance"] > 0:
        srgb = np.clip(srgb, 0, 1)
        mx, mn = srgb.max(axis=2), srgb.min(axis=2)
        sat = (mx - mn) / (mx + 1e-6)
        gris = srgb.mean(axis=2, keepdims=True)
        srgb = gris + (srgb - gris) * (1 + CONFIG["vibrance"] * (1 - sat))[..., None]
    return (np.clip(srgb, 0, 1) * 255 + 0.5).astype(np.uint8)


def zones_perdues(lin, img):
    """Colore en rouge les zones sans aucune information (blanc pur même dans le RAW)
    et en orange celles où une partie des couleurs est perdue. Renvoie (image, % perdu, % partiel)."""
    sature = lin >= 0.985
    perdu = sature.all(axis=2)
    partiel = sature.any(axis=2) & ~perdu
    out = img.astype(np.float32)
    out[perdu] = out[perdu] * 0.3 + np.array([230, 40, 40]) * 0.7
    out[partiel] = out[partiel] * 0.55 + np.array([255, 150, 30]) * 0.45
    return out.astype(np.uint8), float(perdu.mean() * 100), float(partiel.mean() * 100)


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
                img = image_integree(raw, orientation_raw(raw, 1))
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


def exporter_retouchee_simple(p, dossier):
    lin, est_raw = charger_lineaire(p)
    img = developper_photo(p, lin, est_raw)
    sauver_jpeg(img, os.path.join(dossier, p["id"] + ".jpg"), exif_pour_export(p))
    return p["id"]
