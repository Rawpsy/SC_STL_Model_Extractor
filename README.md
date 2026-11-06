# SC Extractor

Outil Windows pour sortir les modeles 3D de Star Citizen (vaisseaux, objets, lieux) et les preparer
pour l'impression 3D : conversion en STL, reparation, mise a l'echelle, decoupe en morceaux avec trous
d'alignement, evidement pour la resine.

## Utilisation

1. Telecharger `SC_Extractor.exe` dans la derniere Release.
2. Le lancer : au premier demarrage il telecharge Blender 5.2.3 portable (blender.org) et
   StarBreaker 0.3.2 (GitHub), verifies par leur empreinte SHA-256.
3. Choisir le `Data.p4k` du jeu si l'outil ne le trouve pas seul.
4. Cocher un element puis "Preparer pour l'impression...".

Le bouton "Mises a jour" installe la derniere version publiee.

## Developpement

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install pycryptodome zstandard numpy pyinstaller
.venv\Scripts\python.exe sc_browser.py
```

`build_exe.bat` construit `dist\SC_Extractor.exe` ; `publish.ps1` publie une nouvelle version.

## Mentions

Les modeles et donnees du jeu appartiennent a Cloud Imperium Games ; cet outil sert un usage personnel
(impression de maquettes) et ne contient aucun fichier du jeu. Il s'appuie sur StarBreaker (MIT),
Blender (GPL) et le lecteur p4k de scdatatools (MIT).
