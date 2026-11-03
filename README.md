# Tri Photos

Aide au tri des photos d'une sortie (montagne, voyage…), **100 % locale et gratuite** :
aucune photo n'est envoyée sur internet, et ça marche hors connexion une fois installé.

- Importe la carte SD, repère les photos **floues, noires ou cramées sans espoir** (en lisant les vraies données du capteur RAW)
- Regroupe les **rafales et photos similaires**, propose les **meilleures** (IA locale : note esthétique + variété des scènes)
- Diaporama en pleine qualité pour valider en quelques touches : **S** meilleure · **G** garder · **X** supprimer
- **Récupérer** une photo cramée en un clic (touche **H**) : exposition, hautes lumières, blancs et ombres développés depuis le RAW, avec les zones définitivement perdues en rouge
- Photos couchées **redressées automatiquement** quand l'appareil n'a pas noté l'orientation, et boutons ⟲ ⟳ (touche **P**)
- **Sélection multiple** (clic, Maj+clic, tout un moment) puis **Envoyer sur Google Photos** : un dossier avec seulement ces photos, et Google Photos s'ouvre pour les y glisser
- Export : meilleures en **RAW original + JPEG retouché**, fonds d'écran PC et téléphone, Instagram, et un dossier prêt pour **Google Photos** avec seulement les photos gardées
- Lit les RAW Nikon (NEF), Canon, Sony, Fuji, Olympus, Panasonic, DNG, et les JPEG

## Installer (Windows, sans droits administrateur)

1. Ouvre **PowerShell** (touche Windows, tape `PowerShell`, Entrée).
2. Colle cette ligne, puis Entrée :

```powershell
irm https://raw.githubusercontent.com/camilbounader/tri-photos/main/installer.ps1 | iex
```

3. Attends la fin (10 à 20 min la première fois, surtout pour l'IA). Une icône **Tri Photos** apparaît sur le bureau.

L'installation se fait avec internet ; ensuite l'appli fonctionne hors connexion.
Si l'IA n'a pas pu s'installer, l'accueil de l'appli propose un bouton **Installer l'IA** pour réessayer (le détail est noté dans `%LOCALAPPDATA%\TriPhotos\installation_ia.log`).
Avec une carte graphique NVIDIA, l'IA utilise la carte (rapide) ; sinon le processeur (plus lent, mais ça marche).
Relancer la même ligne répare ou réinstalle l'appli.

## Utiliser

- Double-clic sur l'icône **Tri Photos** : l'appli s'ouvre dans le navigateur. La petite fenêtre noire réduite dans la barre des tâches doit rester ouverte pendant le tri.
- Insère la carte SD : elle est détectée, clique sur **Importer et trier**. Ou choisis un dossier déjà copié. Tu peux aussi glisser un dossier sur l'icône.
- Tes décisions sont enregistrées au fur et à mesure : tu peux reprendre plus tard depuis l'accueil.
- **Exporter** crée, dans `_TRI\export` à côté des photos : `meilleures`, les formats, `a_envoyer_google_photos` (à glisser sur photos.google.com) et la liste des photos à supprimer. Rien n'est jamais effacé automatiquement.
- **Trier depuis le téléphone** : le calcul reste sur le PC, le téléphone (même Wi-Fi ou partage de connexion) sert d'écran de tri.
- **Réglages** : sévérité du tri, variété de la sélection, retouche, formats. Pour avoir les mêmes réglages sur deux PC, choisis un dossier Google Drive partagé comme « dossier de partage des réglages ».

Les mises à jour s'installent toutes seules au lancement quand il y a internet.

## Désinstaller

Supprime le dossier `%LOCALAPPDATA%\TriPhotos` et l'icône du bureau. Tes photos et leurs dossiers `_TRI` ne sont pas touchés.
