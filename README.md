# cross-seed enhanced

Interface web pour piloter [cross-seed](https://www.cross-seed.org) v6 avec qBittorrent : rechercher un torrent précis, faire passer certaines releases en priorité, gérer les injections en échec, suspendre un indexer, régler cross-seed sans éditer `config.js` à la main.

L'outil ne remplace pas cross-seed. Il utilise son API, lit ses logs et sa base en lecture seule, et ne modifie `config.js` qu'à ta demande, avec une sauvegarde à chaque fois.

> Projet indépendant, sans lien avec l'équipe de cross-seed. Interface en français.

<!-- Captures d'écran : déposer les images dans docs/ puis les référencer ici, par exemple :
![Releases](docs/releases.png) -->

## Fonctions

L'interface tient en trois pages.

### 1. Chercher

« Cherche-moi des releases à envoyer vers tels trackers », sur une seule page :

- **Quelles releases** : toutes, un ou plusieurs groupes de trackers, des catégories qBittorrent (ou « sans catégorie »). un champ « mot clé » (HDForever, Remux, 1080p…) toujours visible ; « Affiner » ajoute des filtres : présente sur tel tracker, état (cross-seed trouvé mais pas injecté, sans torrent d'origine, plusieurs copies sur un même tracker), nombre de trackers où la release est déjà en seed, « jamais cherchée sur la cible » ou « pas cherchée depuis N jours », plafond.
- **Vers quels trackers** : « Selon le groupe » (chaque release part vers les trackers de son groupe : un film vers les trackers vidéo, un album vers les trackers musique) ou « Choisir les trackers » (cochés à la main, pour toutes les releases choisies).
- **La liste des releases concernées**, mise à jour en direct : tes torrents qBittorrent regroupés par release (copies cross-seedées et hardlinks renommés fusionnés), avec une puce par tracker : ✓ déjà en seed, + trouvée par cross-seed mais pas encore injectée, – cherchée sans résultat. Tri, colonnes masquables, détail d'une release au clic (état, dernière recherche et copie dans qBittorrent, tracker par tracker), bouton « Chercher » pour une seule release. Cocher des releases limite la recherche à celles-ci. Sur téléphone, une carte par release.
- **La barre de lancement**, toujours visible en bas : nombre de recherches, durée estimée, releases écartées (déjà en seed sur la cible, déjà cherchées…), et le bouton « Lancer ».

Les choix sont mémorisés dans le navigateur. Une release déjà en seed sur tous les trackers visés n'est jamais cherchée.

### 2. Suivre l'avancement

- **Recherches** : une recherche toutes les N secondes. Chaque lancement forme une **demande** affichée en carte : avancement, injections obtenues, fin estimée, détail et bouton « Annuler ». Ordre de passage : recherches d'une seule release, puis demandes, puis scan planifié. Le résultat de chaque recherche est lu dans les logs ou dans la réponse de cross-seed : injectée sur tel tracker, rien trouvé, sautée et pourquoi. La file survit aux redémarrages. Boutons pour lancer le scan RSS ou le scan complet de cross-seed.
- **Injections en attente** : les torrents trouvés mais refusés par qBittorrent, avec l'erreur correspondante, un bouton pour retenter tout de suite et un pour abandonner.
- **Logs** : en direct, avec une couleur par tracker, un filtre texte et un filtre « injections réussies ».

### 3. Réglages

- **Groupes de trackers** : chacun organise les siens selon sa stack, par exemple « Vidéo » (catégories `radarr`, `sonarr` et les trackers films et séries) et « Musique » (`lidarr` et les trackers musique). Un groupe peut aussi recevoir « toutes les autres catégories ». Pour un groupe, l'interface interroge elle-même ses trackers avec les URL Torznab de `config.js`, puis soumet les résultats de taille proche à cross-seed par `/api/announce` ; cross-seed vérifie et injecte comme d'habitude. Une catégorie rangée dans aucun groupe reste cherchée par cross-seed sur tous ses indexers. Le scan complet de cross-seed (sauf s'il est remplacé, voir ci-dessous), son RSS et son announce ne sont pas concernés. Les anciens « routages par catégorie » sont convertis automatiquement en groupes.
- **Scan planifié** : toutes les N heures, une demande avec les releases à chercher sur les trackers de leur groupe (celles de « Passer en premier » d'abord, puis les moins cross-seedées, puis les plus anciennement cherchées), en sautant celles cherchées récemment et avec un plafond par passage. Une case permet de **remplacer le scan complet de cross-seed** : elle met `searchCadence: null` dans `config.js` (avec sauvegarde) et rétablit l'ancienne valeur quand on la décoche. **Passer en premier** (groupe de release, mots contenus, début du nom, catégorie qBittorrent) fixe les releases servies avant les autres, par le scan planifié comme par les recherches lancées depuis Chercher.
- **Indexers** : état de chaque indexer, pause en cours, Suspendre/Réactiver (ligne commentée dans `config.js`). Avec Prowlarr ou Jackett, ajout et retrait d'indexers sans éditer `config.js`, avec avertissement si un indexer est désactivé ou en échec, ou si le même site est déclaré deux fois.
- Rythme de la file, noms des trackers, sources Prowlarr et Jackett, et les principaux réglages de cross-seed (`searchLimit`, `excludeRecentSearch`, cadences…), vérifiés avec les mêmes règles que cross-seed 6.13 avant écriture, avec un bouton pour redémarrer cross-seed.

## Prérequis

- cross-seed **v6** (testé avec 6.13.7) en mode daemon, avec son API (`apiKey`, ou la clé affichée par `cross-seed api-key`)
- qBittorrent **5.2 ou plus récent**, avec une clé API WebUI (Options, WebUI)
- Docker et Docker Compose

## Installation

```bash
git clone https://github.com/Gusdezup/Cross-Seed-Enhanced.git
cd Cross-Seed-Enhanced
cp .env.example .env && chmod 600 .env
nano .env
mkdir -p data
docker compose up -d
```

Dans `.env`, renseigne les adresses et clés API de qBittorrent et cross-seed, ainsi que `XS_CONFIG_PATH`, le dossier de config de cross-seed sur l'hôte (celui monté sur `/config` dans son conteneur). L'interface est ensuite sur `http://<hôte>:2469`.

**Prowlarr et Jackett** (facultatifs, l'un, l'autre ou les deux) : ils permettent d'ajouter des indexers à cross-seed depuis l'onglet Indexers. Rien à configurer si tes lignes `torznab` pointent déjà vers eux (`http://…:9696/<id>/api?apikey=…` pour Prowlarr, `http://…:9117/api/v2.0/indexers/<id>/results/torznab/api?apikey=…` pour Jackett) : l'adresse et la clé en sont déduites. Sinon, renseigne-les dans **Réglages › Sources d'indexers**, avec un bouton pour tester la connexion. L'adresse est écrite telle quelle dans `config.js` : elle doit être joignable par cross-seed comme par l'interface (par exemple `http://jackett:9117` si les trois conteneurs partagent un réseau Docker). Pour Jackett, seule la clé API est nécessaire, même avec un mot de passe admin. Un tracker déclaré à la fois dans Prowlarr et dans Jackett est signalé comme doublon. Les variables `PROWLARR_URL` / `PROWLARR_APIKEY` et `JACKETT_URL` / `JACKETT_APIKEY` du `.env` restent possibles et prioritaires sur les Réglages.

Le compose utilise l'image publiée sur `ghcr.io`. Pour construire depuis les sources, remplace la ligne `image:` par `build: .`.

Sur Synology, crée le dossier `data` avant le premier lancement (`mkdir -p data`) : Docker n'y crée pas les dossiers montés manquants.

## Instance de développement

Pour faire tourner une seconde instance (par exemple construite depuis les sources) à côté de la production, sans effet sur cross-seed :

- `XSE_READONLY=true` : aucune action sur cross-seed (jobs, recherches, redémarrage), file de recherche en pause, suppression des fichiers en attente et écriture de `config.js` désactivées. Un bandeau le signale dans l'interface ; une action refusée renvoie une erreur 403.
- `XSE_ALLOW_CONFIG_WRITE=true` (avec la précédente) : autorise quand même l'écriture de `config.js`, pour tester l'ajout d'indexers ou les réglages. À réserver au cas où l'instance monte une **copie** de `config.js`, jamais celui de la production.

## Sécurité

- Sans `UI_PASSWORD`, l'interface n'a pas d'authentification. Ne l'expose pas sur Internet ; derrière un reverse proxy, renseigne `UI_PASSWORD`.
- Le bouton « Redémarrer cross-seed » passe par [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy), configuré pour n'autoriser que le redémarrage, l'arrêt et le kill d'un conteneur. L'interface ne peut ni créer, ni supprimer, ni lister de conteneur, ni exécuter de commande. Pour s'en passer, supprime le service `xse-docker-proxy` et la variable `DOCKER_URL` du compose : le bouton disparaît.
- Les clés API saisies dans Réglages › Sources d'indexers sont stockées dans `data/settings.json` (droits 600) et ne sont jamais renvoyées au navigateur.
- Seuls `config.js` et le dossier `cross-seeds/` sont montés en écriture. La base `cross-seed.db` et les logs sont en lecture seule.

## Limites connues

- Les résultats de recherche sont déduits des logs `info` de cross-seed. Si le format des messages change dans une future version, la colonne « Résultat » affichera « sans réponse » ; la recherche aura quand même eu lieu.
- Le schéma de `cross-seed.db` n'est pas documenté. Si une version de cross-seed le change, l'historique par indexer affichera « indisponible », le reste continuera de fonctionner.
- Une recherche faite par l'interface sur des trackers choisis (groupe ou demande) n'est pas enregistrée dans l'historique de recherche de cross-seed (`excludeRecentSearch` ne s'y applique pas) ; l'interface la note dans son propre historique, tracker par tracker, utilisé par la colonne « Dernière recherche », l'état par tracker, le critère « jamais cherchée sur la cible » et le scan planifié.
- Pour reconnaître qu'une release est déjà en seed sur un tracker visé, le nom du tracker de ses torrents doit correspondre au nom de l'indexer (automatique avec Prowlarr ou Jackett ; sinon, Réglages › Noms des trackers).
- La suspension d'indexers suppose un tableau `torznab` avec une URL par ligne.
- cross-seed v7 (en préversion) n'a pas été testé.

## Licence

MIT
