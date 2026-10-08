# cross-seed enhanced

Interface web pour piloter [cross-seed](https://www.cross-seed.org) v6 ou v7 avec qBittorrent : rechercher un torrent précis, faire passer certaines releases en priorité et gérer les injections en échec. La gestion des indexers est compatible avec les deux versions.

L'outil ne remplace pas cross-seed. Il utilise son API, lit ses logs et sa base en lecture seule. En v6 seulement, il peut modifier `config.js` à la demande, avec sauvegarde préalable.

> Projet indépendant, sans lien avec l'équipe de cross-seed. Interface en français.

<!-- Captures d'écran : déposer les images dans docs/ puis les référencer ici, par exemple :
![Releases](docs/releases.png) -->

## Fonctions

- **Releases** : tes torrents qBittorrent regroupés par release. Les copies cross-seedées et les hardlinks renommés sont fusionnés, avec une puce par tracker indiquant l'état de la release : ✓ déjà en seed, + trouvée par cross-seed mais pas encore injectée (torrent absent de qBittorrent), – cherchée sans résultat. La couleur identifie le tracker ; la légende est rappelée au-dessus du tableau. Un filtre isole les releases qu'on peut encore cross-seeder. Colonnes catégorie, taille, date d'ajout dans qBittorrent et date de dernière recherche cross-seed, triables d'un clic sur l'en-tête et masquables (choix mémorisé dans le navigateur) ; filtre par tracker et par catégorie. Un bouton « Chercher » par release, ou par lot. Le détail donne, tracker par tracker, l'état, la dernière recherche et la copie présente dans qBittorrent (origine ou cross-seed, catégorie), ainsi que les indexers jamais interrogés pour cette release.
- **File de recherche** : une recherche cross-seed toutes les N secondes, les recherches manuelles passant devant. Les règles prioritaires (groupe de release, mots contenus, début du nom, catégorie qBittorrent) remplissent la file en commençant par les releases les moins cross-seedées ; on peut les lancer toutes ou une seule, par exemple pour ne chercher que la catégorie `radarr`. Le résultat de chaque recherche est lu dans les logs : injectée sur tel tracker, rien trouvé, sautée et pourquoi. La file survit aux redémarrages.
- **Routage par catégorie** : pour une catégorie qBittorrent choisie (par exemple `radarr`), les recherches de la file n'interrogent que les indexers cochés. L'interface les interroge elle-même avec les URL Torznab de `config.js`, puis soumet les résultats de taille proche à cross-seed par `/api/announce` ; cross-seed vérifie et injecte comme d'habitude. Les autres catégories restent cherchées par cross-seed sur tous ses indexers. Le scan complet de cross-seed (sauf s'il est remplacé, voir ci-dessous), son RSS et son announce ne sont pas concernés.
- **Scan planifié** : toutes les N heures, la file reçoit les releases à chercher (règles prioritaires d'abord, puis les moins cross-seedées, puis les plus anciennement cherchées), en sautant celles cherchées récemment et avec un plafond par passage. Une case permet de **remplacer le scan complet de cross-seed** : elle met `searchCadence: null` dans `config.js` (avec sauvegarde) et rétablit l'ancienne valeur quand on la décoche.
- **Injections en attente** : les torrents trouvés mais refusés par qBittorrent, avec l'erreur correspondante, un bouton pour retenter tout de suite et un pour abandonner.
- **Indexers** : état de chaque indexer, pause en cours, et un interrupteur Suspendre/Réactiver qui commente la ligne correspondante dans `config.js`. Avec Prowlarr, ajout et retrait d'indexers sans éditer `config.js`, avec avertissement si un indexer est désactivé ou en échec dans Prowlarr, ou si le même site est déclaré deux fois.
- **Logs** : en direct, avec une couleur par tracker, un filtre texte et un filtre « injections réussies ».
- **Réglages** : règles prioritaires, rythme de la file, noms des trackers, et les principaux réglages de cross-seed (`searchLimit`, `excludeRecentSearch`, cadences…). Ces derniers sont vérifiés avec les mêmes règles que cross-seed 6.13 avant écriture, et un bouton permet de redémarrer cross-seed.

## Prérequis

- cross-seed **v6** (testé avec 6.13.7) ou **v7** (testé avec 7.0.0-22) en mode daemon, avec son API (`apiKey`, ou la clé affichée par `cross-seed api-key`)
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

### cross-seed v7

Utilise `docker-compose.v7.yml` comme stack autonome (Dockge peut importer son contenu), avec `XS_VERSION=7` dans `.env` et le même `XS_CONFIG_PATH` que cross-seed. Le dossier de configuration est monté en lecture seule. Les indexers sont ajoutés, suspendus et retirés via `/api/indexer/v1` avec la clé API de cross-seed ; les modifications sont directes et ne demandent pas de redémarrage. Les réglages généraux restent à modifier dans l'interface native de cross-seed v7 : son API de réglages requiert une session utilisateur, pas la clé API. Le bouton de redémarrage n'est pas proposé par cette stack v7.

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
- Une recherche routée n'est pas enregistrée dans l'historique de recherche de cross-seed (`excludeRecentSearch` ne s'y applique pas) ; l'interface la note dans son propre historique, utilisé par la colonne « Dernière recherche » et par le scan planifié.
- La suspension d'indexers suppose un tableau `torznab` avec une URL par ligne.
- En v7, l'édition des réglages généraux se fait dans l'interface native de cross-seed.

## Licence

MIT
