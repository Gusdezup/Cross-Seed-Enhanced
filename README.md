# cross-seed enhanced

Interface web pour piloter [cross-seed](https://www.cross-seed.org) v6 ou v7 avec un ou plusieurs clients torrent (qBittorrent, rTorrent, Transmission, Deluge) : rechercher un torrent précis, faire passer certaines releases en priorité et gérer les injections en échec. La gestion des indexers est compatible avec les deux versions.

L'outil ne remplace pas cross-seed. Il utilise son API, lit ses logs et sa base en lecture seule. En v6 seulement, il peut modifier `config.js` à la demande, avec sauvegarde préalable.

> Projet indépendant, sans lien avec l'équipe de cross-seed. Interface en français.

<!-- Captures d'écran : déposer les images dans docs/ puis les référencer ici, par exemple :
![Releases](docs/releases.png) -->

## Fonctions

L'interface tient en trois pages.

### 1. Chercher

« Cherche-moi des releases à envoyer vers tels trackers », sur une seule page :

- **Quelles releases** : toutes, un ou plusieurs groupes de trackers, des catégories des clients torrent (ou « sans catégorie »). un champ « mot clé » (HDForever, Remux, 1080p…) toujours visible ; « Affiner » ajoute des filtres : présente sur tel tracker, dans tel client (avec plusieurs clients), état (cross-seed trouvé mais pas injecté, sans torrent d'origine, plusieurs copies sur un même tracker), nombre de trackers où la release est déjà en seed, « jamais cherchée sur la cible » ou « pas cherchée depuis N jours », plafond.
- **Vers quels trackers** : « Selon le groupe » (chaque release part vers les trackers de son groupe : un film vers les trackers vidéo, un album vers les trackers musique) ou « Choisir les trackers » (cochés à la main, pour toutes les releases choisies).
- **La liste des releases concernées**, mise à jour en direct : les torrents de tous tes clients regroupés par release (copies cross-seedées et hardlinks renommés fusionnés), avec une puce par tracker : ✓ déjà en seed, + trouvée par cross-seed mais pas encore injectée, – cherchée sans résultat. Tri, colonnes masquables, détail d'une release au clic (état, dernière recherche et copie dans le client, tracker par tracker), bouton « Chercher » pour une seule release. Avec plusieurs clients, un badge indique le ou les clients de chaque release. Cocher des releases limite la recherche à celles-ci. Sur téléphone, une carte par release.
- **Quand** : « Maintenant » ou « Régulièrement » (toutes les N heures, avec un nom et, au choix, un premier passage tout de suite).
- **La barre de lancement**, toujours visible en bas : nombre de recherches, durée estimée, releases écartées (déjà en seed sur la cible, déjà cherchées…), et le bouton « Lancer » ou « Programmer ».

Les choix sont mémorisés dans le navigateur. Une release déjà en seed sur tous les trackers visés n'est jamais cherchée.

**Programmées** (second sous-onglet de Chercher) : les recherches répétées, avec leur cible, leurs critères, leur fréquence, le prochain passage et le dernier résultat. « Modifier » rouvre la recherche pré-remplie (aperçu en direct de ce qu'elle attrape) ; aussi « Lancer maintenant », pause et suppression. Chaque passage crée une demande dans Suivre l'avancement. L'ancien scan planifié, s'il était actif, y est repris automatiquement.

### 2. Suivre l'avancement

- **Recherches** : une recherche toutes les N secondes. Chaque lancement forme une **demande** affichée en carte : avancement, injections obtenues, fin estimée, détail et bouton « Annuler ». Ordre de passage : recherches d'une seule release, puis demandes lancées à la main, puis recherches programmées. Le résultat de chaque recherche est lu dans les logs ou dans la réponse de cross-seed : injectée sur tel tracker, rien trouvé, sautée et pourquoi. La file survit aux redémarrages. Boutons pour lancer le scan RSS ou le scan complet de cross-seed.
- **Injections en attente** : les torrents trouvés mais pas encore injectés, avec l'erreur correspondante et l'état de leur source probable (cherchée dans tous les clients), un bouton pour retenter tout de suite et un pour abandonner.
- **Logs** : en direct, avec une couleur par tracker, un filtre texte et un filtre « injections réussies ».

### 3. Réglages

- **Groupes de trackers** : chacun organise les siens selon sa stack, par exemple « Vidéo » (catégories `radarr`, `sonarr` et les trackers films et séries) et « Musique » (`lidarr` et les trackers musique). Un groupe peut aussi recevoir « toutes les autres catégories ». Pour un groupe, l'interface interroge elle-même ses trackers avec les URL Torznab de `config.js`, puis soumet les résultats de taille proche à cross-seed par `/api/announce` ; cross-seed vérifie et injecte comme d'habitude. Une catégorie rangée dans aucun groupe reste cherchée par cross-seed sur tous ses indexers. Le scan complet de cross-seed (sauf s'il est remplacé, voir ci-dessous), son RSS et son announce ne sont pas concernés. Les anciens « routages par catégorie » sont convertis automatiquement en groupes.
- **Passer en premier** (groupe de release, mots contenus, début du nom, catégorie) : releases servies avant les autres dans toute recherche, lancée ou programmée. Exemple : groupe de release HDForever.
- **Scan complet de cross-seed** : une case met `searchCadence: null` dans `config.js` (avec sauvegarde) pour le couper, par exemple quand une recherche programmée le remplace ; décocher rétablit l'ancienne valeur.
- **Indexers** : état de chaque indexer, pause en cours, Suspendre/Réactiver (ligne commentée dans `config.js`). Avec Prowlarr ou Jackett, ajout et retrait d'indexers sans éditer `config.js`, avec avertissement si un indexer est désactivé ou en échec, ou si le même site est déclaré deux fois.
- **Clients torrent** : état et version de chacun, nom affiché.
- Rythme de la file, noms des trackers, sources Prowlarr et Jackett, et les principaux réglages de cross-seed (`searchLimit`, `excludeRecentSearch`, cadences…), vérifiés avec les mêmes règles que cross-seed 6.13 avant écriture, avec un bouton pour redémarrer cross-seed.

## Prérequis

- cross-seed **v6** (testé avec 6.13.7) ou **v7** (testé avec 7.0.0-22) en mode daemon, avec son API (`apiKey`, ou la clé affichée par `cross-seed api-key`)
- Un ou plusieurs clients torrent parmi ceux de cross-seed : qBittorrent (clé API WebUI à partir de la 5.2, sinon identifiants), rTorrent, Transmission, Deluge. Seul qBittorrent a été testé sur une vraie installation : les autres le sont sur des réponses simulées, les retours sont bienvenus.
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

Dans `.env`, renseigne l'adresse et la clé API de cross-seed, tes clients torrent (voir ci-dessous) et `XS_CONFIG_PATH`, le dossier de config de cross-seed sur l'hôte (celui monté sur `/config` dans son conteneur). L'interface est ensuite sur `http://<hôte>:2469`.

### Clients torrent

cross-seed enhanced lit les torrents de **tous** tes clients, comme cross-seed, et ne leur envoie aucune action (cross-seed s'occupe des injections). Les clients sont pris, dans cet ordre :

1. **`TORRENT_CLIENT_1`, `TORRENT_CLIENT_2`…** du `.env`, avec la syntaxe de `torrentClients` de cross-seed :

   ```env
   TORRENT_CLIENT_1=qbittorrent:http://192.168.1.20:8081
   TORRENT_CLIENT_1_APIKEY=…          # qBittorrent ≥ 5.2 ; sinon identifiants dans l'URL
   TORRENT_CLIENT_1_NAME=principal    # facultatif
   TORRENT_CLIENT_2=qbittorrent:http://admin:motdepasse@192.168.1.20:8082
   TORRENT_CLIENT_3=rtorrent:http://user:pass@192.168.1.30/RPC2
   TORRENT_CLIENT_4=transmission:http://user:pass@192.168.1.30:9091/transmission/rpc
   TORRENT_CLIENT_5=deluge:http://:pass@192.168.1.30:8112/json
   ```

   Les numéros sont triés et peuvent avoir des trous. Un caractère spécial dans un identifiant s'encode comme dans une URL (`@` → `%40`). `readonly:` est accepté et sans effet. rTorrent accepte aussi le httprpc de ruTorrent (`…/rutorrent/plugins/httprpc/action.php`) et SCGI (`rtorrent:scgi://hôte:5000`, ou `rtorrent:scgi:///chemin/.rtorrent.sock` avec le socket monté dans le conteneur).
2. Sinon, **`torrentClients` de cross-seed** (`config.js` en v6, ses réglages en v7) : rien à configurer, mêmes clients et même ordre que cross-seed. Le qBittorrent du port de `QBT_URL` reprend l'adresse et la clé API de `QBT_URL` ; `localhost` (cross-seed dans le réseau de Gluetun, par exemple) est remplacé par `TORRENT_CLIENT_HOST`, à défaut par l'hôte de `QBT_URL`.
3. Sinon, **`QBT_URL` et `QBT_APIKEY`** (un seul qBittorrent, l'ancien format, toujours valable).

Une même release présente dans plusieurs clients n'apparaît qu'une fois. Un client injoignable est signalé (pastille rouge, liste marquée incomplète) et ses torrents manquent jusqu'à son retour. Catégorie lue selon le client : catégorie qBittorrent, étiquette ruTorrent, premier label Transmission (≥ 4.0), plugin Label de Deluge. Dans **Réglages › Clients torrent** : nom affiché de chaque client (`TORRENT_CLIENT_N_NAME` reste prioritaire) et, avec cross-seed v6, ajout et retrait de clients dans `torrentClients` de `config.js` (sauvegarde préalable, essai de connexion avant écriture, case « lecture seule »), puis redémarrage de cross-seed. Le dernier client ne peut pas être retiré.

### cross-seed v7 (expérimental)

> cross-seed v7 est encore en préversion et son schéma de base évolue d'une préversion à l'autre. Ce mode est testé avec 7.0.0-22 ; une préversion plus récente peut casser une partie de l'interface.

La version de cross-seed est **détectée automatiquement** à partir de sa base (`XS_VERSION` vide). `XS_VERSION=6` ou `7` force la version si besoin.

Utilise `docker-compose.v7.yml` comme stack autonome (Dockge peut importer son contenu), avec le même `XS_CONFIG_PATH` que cross-seed. Le dossier de configuration est monté en lecture seule. Les indexers sont ajoutés, suspendus et retirés via `/api/indexer/v1` avec la clé API de cross-seed ; les modifications sont directes et ne demandent pas de redémarrage. Les réglages généraux restent à modifier dans l'interface native de cross-seed v7 : son API de réglages requiert une session utilisateur, pas la clé API. Le bouton de redémarrage n'est pas proposé par cette stack v7.

Points à connaître avec cross-seed v7 (préversion 7.0.0-22) :

- Au premier démarrage, la v7 importe `config.js` dans sa base et le renomme en `config.js.bak`. Seules les lignes `torznab` actives sont reprises : un indexer suspendu (ligne commentée) disparaît, et l'interface le signale dans tes groupes comme « désactivé dans cross-seed ».
- Le compte de l'interface native se crée dans les minutes qui suivent un démarrage de cross-seed ; passé ce délai, redémarre-le. Ce compte ne concerne pas cross-seed enhanced, qui utilise la clé API.
- **Output Directory** (obligatoire en v7) : règle-le sur `/config/cross-seeds` pour que l'onglet « Injections en attente » retrouve les torrents non injectés avec le montage de `docker-compose.v7.yml`. Un autre dossier sous `/config` est lu, mais en lecture seule (pas de suppression depuis l'interface).

Le mode v7 lit `cross-seed.db` et ses fichiers WAL pour l'historique. Il ne migre pas les données v6 et n'écrit jamais directement dans cette base. Le mode v6 et son `docker-compose.yml` restent inchangés.

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
- Une recherche faite par l'interface sur des trackers choisis (groupe ou demande) n'est pas enregistrée dans l'historique de recherche de cross-seed (`excludeRecentSearch` ne s'y applique pas) ; l'interface la note dans son propre historique, tracker par tracker, utilisé par la colonne « Dernière recherche », l'état par tracker et les critères « jamais cherchée » / « pas cherchée depuis N jours » sur la cible.
- Pour reconnaître qu'une release est déjà en seed sur un tracker visé, le nom du tracker de ses torrents doit correspondre au nom de l'indexer (automatique avec Prowlarr ou Jackett ; sinon, Réglages › Noms des trackers).
- La suspension d'indexers suppose un tableau `torznab` avec une URL par ligne.
- En v7, l'édition des réglages généraux se fait dans l'interface native de cross-seed, y compris `searchCadence` : la case « Remplacer le scan complet de cross-seed » du scan planifié n'est pas disponible.

## Licence

MIT
