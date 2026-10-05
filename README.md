# Radar NSI – mise en place

Un site unique, à mettre en lien dans le Drive des CGP, avec deux pages :

- **Radar des marchés** : contexte de taux, état des 27 familles, points d'entrée sur le top 3, historique des signaux.
- **Sélection Swiss Life** : le top 3 par famille et la recherche sur les 835 fonds.

Chaque lundi matin, GitHub recalcule le radar à partir des valeurs liquidatives (Yahoo Finance) et des données de marché (FRED), puis Netlify met le site à jour tout seul. Aucun e-mail, aucune connexion, rien à valider : les CGP consultent quand ils veulent.

Tout est gratuit. Compte environ 20 minutes la première fois.

---

## Étape 1 – GitHub (10 min)

1. Crée un compte gratuit sur **github.com**.
2. **New repository** → nom `radar-nsi` → coche **Private** → **Create repository**.
3. Sur la page du dépôt vide, clique sur **uploading an existing file** et glisse **tout le contenu** du dossier `radar-nsi`, y compris le dossier `.github`. Clique sur **Commit changes**.
   - Sous Windows, le dossier `.github` peut être masqué : dans l'Explorateur, menu **Affichage → Afficher → Éléments masqués**.
   - Vérifie qu'on voit bien `.github/workflows/radar-hebdo.yml` dans le dépôt.

## Étape 2 – Netlify (5 min)

1. Sur **app.netlify.com** : **Add new site → Import an existing project → GitHub**, autorise l'accès, choisis `radar-nsi`.
2. Réglages :
   - **Build command** : laisser vide
   - **Publish directory** : `web`
3. **Deploy**. Note l'adresse du site, et renomme-la si tu veux dans **Site configuration → Change site name** (par exemple `radar-nsi.netlify.app`).

À partir de là, chaque fois que le radar est recalculé, Netlify republie le site automatiquement.

## Étape 3 – Premier lancement (5 min)

1. Dans GitHub, onglet **Actions** → **Radar NSI – calcul du lundi** → **Run workflow**.
2. Attends 2 à 3 minutes.
   - **Coche verte** : le radar est à jour. Ouvre le site Netlify, la date en haut de page doit être celle de la semaine.
   - **Croix rouge** avec « Trop de fonds indisponibles » : Yahoo bloque les serveurs de GitHub. Le site garde le radar précédent. Envoie le message d'erreur à Claude pour passer à la solution de secours.
3. Mets le lien du site dans le Drive des CGP.

Ensuite, tout tourne seul chaque lundi vers 7h.

---

## Au quotidien

- **Modifier un seuil** : dans GitHub, ouvre `data/regles.json`, clique sur le crayon, change la valeur, **Commit changes**. Pris en compte au calcul suivant (ou tout de suite avec **Run workflow**).
- **Recalculer en cours de semaine** : **Actions → Radar NSI – calcul du lundi → Run workflow**.
- **Vérifier que le calcul tourne** : onglet **Actions**, une ligne par lundi avec une coche verte. GitHub t'envoie un e-mail en cas d'échec.
- **Nouvelle liste Swiss Life (une fois par an)** : envoie-la à Claude, qui régénère `data/fonds_suivis.json` et `web/selection.html` (nouveau top 3, correspondances Yahoo, rapport des entrées et sorties). Tu remplaces les deux fichiers dans GitHub.

## À savoir

Le site n'a pas de mot de passe : toute personne qui a le lien peut le consulter. Il n'est pas référencé par les moteurs de recherche et ne contient aucune donnée client, seulement des données de fonds et de marché. Si tu veux le réserver aux CGP, Netlify propose une protection par mot de passe dans ses offres payantes, ou on peut remettre la connexion Supabase.

## Contenu du dossier

| Fichier | Rôle |
|---|---|
| `radar/calcul.py` | Récupère les données, applique les règles, écrit `web/radar.json` |
| `data/fonds_suivis.json` | Les 81 fonds du top 3 et leur cotation Yahoo |
| `data/regles.json` | Les seuils par famille et les seuils de marché |
| `web/index.html` | Page Radar des marchés |
| `web/selection.html` | Page Sélection Swiss Life |
| `web/radar.json` | Les données du radar (mises à jour chaque lundi) |
| `.github/workflows/radar-hebdo.yml` | Le calcul automatique du lundi |
| `tests/` | Données figées au 2 octobre 2026 pour tester sans réseau : `python radar/calcul.py --offline` |
