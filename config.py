"""Configuration centrale du pipeline ETL (mots-clés, quartiers, délais, chemins).

Toute constante "métier" (regex, quartiers, délais anti-bot) vit ici pour que
scraper.py / processor.py / main.py restent des modules de logique pure, sans
valeur hardcodée dispersée.
"""

from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# Arborescence
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"  # posts bruts scrapés, sauvegarde incrémentale
PROCESSED_DIR = DATA_DIR / "processed"  # sorties CSV/JSON structurées
STATE_DIR = DATA_DIR / "state"  # ids déjà vus (déduplication inter-runs)
LOG_DIR = DATA_DIR / "logs"

for _dir in (RAW_DIR, PROCESSED_DIR, STATE_DIR, LOG_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

GROUPS_CSV_PATH = BASE_DIR / "groups.csv"
SEEN_IDS_PATH = STATE_DIR / "seen_post_ids.json"
COOLDOWN_PATH = STATE_DIR / "cooldown_until.json"
STORAGE_STATE_PATH = STATE_DIR / "storage_state.json"
SANTE_PATH = STATE_DIR / "sante_scraper.json"

# Rotation du mode "backfill" (2026-08-07) : mémorise le dernier groupe/page
# effectivement tenté, pour que le run backfill suivant reprenne juste après
# lui plutôt que de repartir systématiquement du haut de groups.csv. Sans ce
# fichier, group_limit prenant toujours les N premiers groupes dans l'ordre
# du CSV, une série de runs manuels ne dépasserait jamais les tout premiers
# groupes (25 cibles / ~3 groupes couverts par run de 45 min en backfill).
# Fichier séparé de sante_scraper.json : concepts indépendants (rotation =
# position dans la liste, santé = confiance/throttle adaptatif).
ROTATION_BACKFILL_PATH = STATE_DIR / "rotation_backfill.json"

# Vue Excel régénérée à chaque run à partir de la base maître PostgreSQL - UN
# SEUL fichier, toujours à jour, plutôt qu'un CSV différent par run (voir
# processor.py). La base maître elle-même n'est plus un fichier local depuis
# la migration SQLite -> PostgreSQL : voir DATABASE_URL ci-dessous.
MASTER_XLSX_PATH = PROCESSED_DIR / "annonces.xlsx"

# --------------------------------------------------------------------------- #
# Variables d'environnement (secrets)
# --------------------------------------------------------------------------- #

ENV_FB_COOKIES = "FB_COOKIES_JSON"
ENV_OPENAI_KEY = "OPENAI_API_KEY"
ENV_DATABASE_URL = "DATABASE_URL"

# Base de données maître PostgreSQL (source de vérité, upsert par id de post).
# Lue depuis l'environnement (secret GitHub Actions en CI, .env en local via
# python-dotenv - voir main.py). Pas de valeur par défaut "pratique" du type
# localhost:5432 : une absence de DATABASE_URL doit échouer bruyamment plutôt
# que de pointer silencieusement vers une base qui n'existe pas chez l'utilisateur.
# `.strip()` : un secret GitHub Actions collé avec un retour à la ligne final
# (piège courant - un simple copier-coller depuis un fichier/dashboard suffit)
# produit une chaîne du type "...sslmode=require\n", que libpq/psycopg refuse
# purement et simplement (`invalid sslmode value`) - confirmé en conditions
# réelles le 2026-08-01 sur le premier run du workflow GitHub Actions.
DATABASE_URL = os.environ.get(ENV_DATABASE_URL, "").strip()

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def configurer_logging(niveau: int = logging.INFO) -> logging.Logger:
    """Configure un logger unique pour tout le pipeline (console + fichier)."""
    logger = logging.getLogger("ouaga_foncier_etl")
    if logger.handlers:  # évite les handlers dupliqués si appelé plusieurs fois
        return logger
    logger.setLevel(niveau)

    formatteur = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler_console = logging.StreamHandler()
    handler_console.setFormatter(formatteur)
    logger.addHandler(handler_console)

    handler_fichier = logging.FileHandler(LOG_DIR / "pipeline.log", encoding="utf-8")
    handler_fichier.setFormatter(formatteur)
    logger.addHandler(handler_fichier)

    return logger


# --------------------------------------------------------------------------- #
# Groupes Facebook à scraper
# --------------------------------------------------------------------------- #


TYPES_GROUPE_VALIDES = ("groupe", "page")


@dataclass(frozen=True)
class Groupe:
    """Représente une cible Facebook à scraper : un groupe ou une page.

    `type` distingue les deux car leur URL de fil d'actualité se construit
    différemment (voir `scraper.scraper_groupe`) :
    - "groupe" : id numérique Facebook -> URL reconstruite en dur
      (`WEB_FACEBOOK_BASE_URL/groups/{id}/`).
    - "page" : pas d'id numérique fiable (slug arbitraire) -> `url` stockée
      est utilisée telle quelle, sans reconstruction.
    """

    id: str
    nom: str
    url: str
    actif: bool = True
    type: str = "groupe"


def charger_groupes(chemin: Path = GROUPS_CSV_PATH, limite: int | None = None) -> list[Groupe]:
    """Charge la liste des groupes/pages depuis groups.csv (source unique de vérité).

    Intègre depuis le 2026-08-07 les groupes et pages fournis dans
    "F:\\Scraping Facebook\\Groupe.xlsx" (passage de 13 à 25 cibles suivies :
    23 groupes + 2 pages Facebook). La colonne `type` ("groupe" ou "page")
    distingue les deux ; absente ou vide, elle vaut "groupe" par défaut pour
    rester compatible avec les lignes historiques du CSV.

    Args:
        chemin: chemin vers le fichier CSV des groupes.
        limite: si fourni, ne retourne que les N premiers groupes actifs
            (utilisé par `--group-limit` en CLI pour les tests/rattrapages).

    Raises:
        FileNotFoundError: si groups.csv est absent.
        ValueError: si le CSV est vide, mal formé, ou contient un `type` invalide.
    """
    if not chemin.exists():
        raise FileNotFoundError(
            f"Fichier de groupes introuvable : {chemin}. "
            "Créez-le à partir de Groupe.xlsx (voir README.md)."
        )

    groupes: list[Groupe] = []
    with chemin.open(encoding="utf-8") as f:
        lecteur = csv.DictReader(f)
        colonnes_attendues = {"id", "nom", "url", "actif"}
        if lecteur.fieldnames is None or not colonnes_attendues.issubset(set(lecteur.fieldnames)):
            raise ValueError(
                f"En-têtes CSV invalides dans {chemin} : attendu {colonnes_attendues}, "
                f"trouvé {lecteur.fieldnames}"
            )
        for ligne in lecteur:
            if ligne["id"].strip().upper().startswith("TODO"):
                continue  # ligne placeholder non complétée : on l'ignore silencieusement
            type_brut = (ligne.get("type") or "").strip().lower() or "groupe"
            if type_brut not in TYPES_GROUPE_VALIDES:
                raise ValueError(
                    f"type invalide '{type_brut}' pour la ligne id={ligne['id']!r} dans "
                    f"{chemin} : attendu un parmi {TYPES_GROUPE_VALIDES}"
                )
            groupes.append(
                Groupe(
                    id=ligne["id"].strip(),
                    nom=ligne["nom"].strip(),
                    url=ligne["url"].strip(),
                    actif=ligne["actif"].strip().lower() in ("1", "true", "vrai", "oui"),
                    type=type_brut,
                )
            )

    groupes_actifs = [g for g in groupes if g.actif]
    if not groupes_actifs:
        raise ValueError(
            f"Aucun groupe actif trouvé dans {chemin}. "
            "Vérifiez que les lignes TODO ont bien été remplacées."
        )

    if limite is not None and limite > 0:
        groupes_actifs = groupes_actifs[:limite]

    return groupes_actifs


# --------------------------------------------------------------------------- #
# Filtrage regex niveau 1 (marché foncier de Ouagadougou)
# --------------------------------------------------------------------------- #

# Mots-clés d'inclusion : présence d'AU MOINS UN de ces motifs = candidat potentiel.
# Regroupés par thème pour faciliter la maintenance.
_MOTS_FONCIER = [
    r"parcelle", r"terrain", r"lotissement", r"non\s+loti", r"zone\s+lotie",
    r"cession", r"hectares?", r"superficie",
]
_MOTS_DOCUMENT = [
    r"attestation", r"titre\s+foncier", r"\btf\b", r"puh", r"permis\s+d[' ]habiter",
    r"apfr", r"acte\s+de\s+cession", r"papier\s+en\s+r[eè]gle",
]
_MOTS_TRANSACTION = [
    r"\b[àa]\s+vendre\b", r"\bvente\b", r"\bvendre\b", r"\bc[ée]der\b", r"\bprix\b",
    r"n[ée]gociable",
]

MOTIF_FONCIER = re.compile(
    r"\b(" + "|".join(_MOTS_FONCIER + _MOTS_DOCUMENT + _MOTS_TRANSACTION) + r")\b",
    re.IGNORECASE,
)

# Unités de superficie ("ha", "m2", "m²") gérées à part avec une ancre sur le
# chiffre qui précède plutôt qu'un \b classique. Raison : dans l'écriture
# courante ("600m2", "5ha", sans espace), le chiffre et la lettre sont tous les
# deux des caractères "mot" pour le moteur regex -> \b ne trouve AUCUNE
# frontière entre eux et ne matche jamais. Idem pour \b après "²", qui n'est
# pas considéré comme un caractère "mot" par Python (donc jamais suivi d'une
# frontière valide devant un espace, lui aussi non-mot). Bug identifié et
# corrigé en relecture - non testé en conditions réelles (sandbox indisponible
# au moment de la génération), à confirmer sur un échantillon réel d'annonces.
MOTIF_SUPERFICIE_NUMERIQUE = re.compile(r"\d\s*(m2|m²|ha)(?=\D|$)", re.IGNORECASE)

# Recherches d'achat ("je cherche/recherche un terrain...") : à exclure de l'envoi au
# LLM car ce ne sont PAS des annonces de vente. Heuristique volontairement prudente :
# si le texte contient un verbe de recherche ET ne contient PAS de signal de vente
# explicite (souvent une recherche republie une annonce trouvée ailleurs), on exclut.
MOTIF_RECHERCHE_ACHAT = re.compile(
    r"\b(je\s+recherche|recherche\s+un[e]?|cherche\s+un[e]?|besoin\s+d[' ]un[e]?|"
    r"suis\s+preneur|qui\s+a\s+un[e]?\s+(terrain|parcelle)\s+[àa]\s+(vendre|proposer))\b",
    re.IGNORECASE,
)
MOTIF_SIGNAL_VENTE = re.compile(
    r"\b([àa]\s+vendre|vends|disponible\s+[àa]\s+la\s+vente)\b|prix\s*:?\s*\d+",
    re.IGNORECASE,
)

# Locations (à ne pas rejeter, mais à taguer - le foncier "vente" reste la cible
# métier principale ; laissé au LLM de trancher via `type_bien`/`resume_court`).
MOTIF_LOCATION = re.compile(r"\b(location|louer|loyer|bail)\b", re.IGNORECASE)

# Spam grossier détectable sans LLM (économie de coûts) : arnaques, contenus hors-sujet
# manifestes. Volontairement restreint pour limiter les faux positifs - un pattern trop
# large rejetterait de vraies annonces. À enrichir avec des cas réels observés.
#
# BUG CORRIGÉ (trouvé en exécutant réellement la suite de tests) : la version
# précédente incluait `whatsapp\s*:?\s*\+?\d{8,}.{0,5}$` pour détecter les posts
# qui ne sont QU'un numéro de téléphone (spam de contact). En pratique, la quasi-
# totalité des vraies annonces immobilières se terminent aussi par un numéro
# WhatsApp ("...Contact WhatsApp 70123456.") - ce motif rejetait donc la majorité
# des annonces légitimes (faux négatif massif, découvert par le test
# `test_separe_correctement_candidats_et_rejetes`). Supprimé plutôt que rafistolé :
# la présence d'un numéro en fin de texte n'est PAS un signal fiable de spam dans
# ce domaine métier précis.
MOTIF_SPAM = re.compile(
    r"(cliquez\s+ici|gagnez\s+\d|投资|forex\s+trading|crypto\s*(monnaie)?\s+gratuit)",
    re.IGNORECASE,
)


def est_candidat_foncier(texte: str) -> bool:
    """Étape A du filtrage : décide si un post mérite d'être envoyé au LLM.

    Règle : (mot-clé foncier présent) ET (pas de spam évident) ET
    (pas une recherche d'achat pure, sauf si un signal de vente cohabite -
    cas fréquent d'un post republié ambigu, laissé au LLM pour trancher).

    Limite connue : détection d'intention (achat vs vente) par regex est
    approximative. Des faux négatifs (annonces rejetées à tort) sont possibles
    sur des tournures inhabituelles. Pas de faux positifs coûteux en revanche,
    car l'étape B (LLM) revalide `est_une_annonce_valide`.
    """
    if not texte or not texte.strip():
        return False
    if MOTIF_SPAM.search(texte):
        return False
    if not (MOTIF_FONCIER.search(texte) or MOTIF_SUPERFICIE_NUMERIQUE.search(texte)):
        return False
    if MOTIF_RECHERCHE_ACHAT.search(texte) and not MOTIF_SIGNAL_VENTE.search(texte):
        return False
    return True


# --------------------------------------------------------------------------- #
# Quartiers / zones de Ouagadougou (normalisation)
# --------------------------------------------------------------------------- #

# Liste non exhaustive des quartiers/secteurs/communes couramment cités dans les
# annonces foncières à Ouagadougou. À COMPLÉTER au fil de l'eau : quand le LLM
# renvoie un `quartier_zone` absent de cette liste, il est conservé tel quel
# (voir processor.py) plutôt que forcé/déformé - on ne veut pas perdre
# d'information par excès de normalisation.
QUARTIERS_OUAGA = [
    "Ouaga 2000", "Karpala", "Pissy", "Saaba", "Komsilga", "Cissin", "Tanghin",
    "Gounghin", "Kossodo", "Nioko", "Bassinko", "Yagma", "Tampouy", "Zagtouli",
    "Kamboinsé", "Nongr-Massom", "Sig-Noghin", "Baskuy", "Bogodogo", "Boulmiougou",
    "Tanghin-Dassouri", "Koubri", "Loumbila", "Pabré", "Dapoya", "Zone du Bois",
    "Patte d'Oie", "Ouidi", "Kilwin", "Rimkiéta", "Yamtenga",
]

_QUARTIERS_NORMALISES = {q.lower(): q for q in QUARTIERS_OUAGA}


def normaliser_quartier(valeur: str | None) -> str | None:
    """Tente de faire correspondre un quartier libre à la liste normalisée.

    Correspondance stricte (insensible à la casse) uniquement - volontairement
    pas de fuzzy-matching (Levenshtein, etc.) pour éviter de fusionner à tort
    deux quartiers distincts. Si aucune correspondance, retourne la valeur
    d'origine nettoyée (pas de perte de donnée), à trier manuellement plus tard.
    """
    if not valeur:
        return None
    nettoye = valeur.strip()
    return _QUARTIERS_NORMALISES.get(nettoye.lower(), nettoye)


# --------------------------------------------------------------------------- #
# Statut du document foncier (normalisation)
# --------------------------------------------------------------------------- #

# Liste initiale constituée le 2026-08-03 à partir des valeurs RÉELLEMENT
# renvoyées par le LLM sur 224 annonces (pas une liste théorique/inventée) -
# à COMPLÉTER au fil de l'eau, même logique que QUARTIERS_OUAGA ci-dessus.
# "Attestation" et "Attestation d'attribution" sont volontairement gardées
# SÉPARÉES : rien ne garantit qu'un vendeur écrivant juste "attestation" veut
# dire "attestation d'attribution" plutôt qu'un autre type - les fusionner
# serait une supposition non vérifiée, pas une normalisation de casse/forme.
STATUTS_DOCUMENT = [
    "Titre foncier",
    "APFR",
    "PUH",
    "Attestation d'attribution",
    "Fiche d'attribution",
    "Attestation",
]

_STATUTS_DOCUMENT_NORMALISES = {s.lower(): s for s in STATUTS_DOCUMENT}


def normaliser_statut_document(valeur: str | None) -> str | None:
    """Tente de faire correspondre un statut de document libre à la liste
    normalisée - même logique et mêmes garanties que `normaliser_quartier`
    (correspondance stricte insensible à la casse, aucune perte de donnée
    sur une valeur non reconnue, pas de fuzzy-matching).
    """
    if not valeur:
        return None
    nettoye = valeur.strip()
    return _STATUTS_DOCUMENT_NORMALISES.get(nettoye.lower(), nettoye)


# --------------------------------------------------------------------------- #
# Paramètres de scraping / anti-détection
# --------------------------------------------------------------------------- #

# Ces valeurs peuvent être surchargées via les arguments CLI de main.py.
MAX_DAYS_BACK_DAILY = 1
MAX_DAYS_BACK_BACKFILL_DEFAULT = 7
GROUPS_BATCH_SIZE_DEFAULT = 5

# --------------------------------------------------------------------------- #
# HISTORIQUE D'ARCHITECTURE (important pour comprendre le code ci-dessous) :
#
# 1. Choix initial : mbasic.facebook.com (HTML léger server-rendered),
#    jamais vérifié en conditions réelles faute d'accès réseau.
# 2. Premier run live (2026-08-01) : mbasic a renvoyé l'app React "Comet"
#    (mêmes marqueurs que le Facebook standard), pas de HTML léger.
# 3. Deux tentatives de contournement par changement de User-Agent (un UA
#    2011 puis un UA Android récent) : la seconde a évité Comet mais a
#    atterri sur une page de "groupes suggérés" générique, jamais le fil du
#    groupe ciblé.
# 4. Test décisif : l'utilisateur a ouvert lui-même l'URL dans SON navigateur
#    réel (aucune automation, aucun UA modifié) et a récupéré le vrai
#    "View Source" de la page. Verdict sans ambiguïté : mbasic.facebook.com
#    redirige tout navigateur réel vers web.facebook.com, qui sert l'app
#    Comet - CE N'EST PAS UN PROBLÈME DE USER-AGENT, c'est que Facebook ne
#    sert plus de HTML léger du tout aux sessions authentifiées en 2026.
# 5. MAIS : l'inspection de ce "View Source" réel a révélé que Comet
#    embarque les données des posts en clair, sous forme de JSON, dans des
#    balises `<script type="application/json" data-sjs>` (payload Relay/
#    GraphQL utilisé pour l'hydratation React) - texte du post, horodatage
#    Unix exact (`creation_time`), id et URL du post, tout y est. Confirmé
#    sur un échantillon réel (une annonce de parcelle avec son vrai texte,
#    son vrai lien permanent, son vrai horodatage).
#
# Nouvelle stratégie retenue (voir `extraire_stories_depuis_json` dans
# scraper.py) : au lieu de scroller/paginer un DOM HTML avec des sélecteurs
# CSS, on parse directement ces blobs JSON - au chargement initial de la
# page (posts "mis en avant") ET dans les réponses GraphQL déclenchées par
# le scroll (fil principal, chargé dynamiquement). C'est plus puissant mais
# aussi plus fragile : la structure interne n'est pas documentée
# publiquement, n'est stabilisée par aucun contrat, et peut changer sans
# préavis à la prochaine mise à jour de Facebook. Conçu pour échouer
# silencieusement poste par poste plutôt que de planter tout le run.
# --------------------------------------------------------------------------- #

WEB_FACEBOOK_BASE_URL = "https://web.facebook.com"

# Conservé pour référence/historique uniquement - mbasic redirige les
# navigateurs réels vers web.facebook.com, voir ci-dessus. Plus utilisé par
# le code de scraping actif.
MBASIC_BASE_URL = "https://mbasic.facebook.com"

# User-Agent desktop standard (pas un UA mobile spoofé - les deux tentatives
# de spoofing UA ont échoué à obtenir autre chose que Comet ou une page
# générique, voir historique ci-dessus). Puisque Comet est de toute façon
# inévitable, autant utiliser un UA cohérent avec le reste du fingerprint
# (viewport desktop) plutôt qu'un mensonge inutile.
MBASIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

PAGE_DELAY_MIN_S = 2.0  # délai entre deux étapes de scroll (remplace l'ancien délai de pagination par lien)
PAGE_DELAY_MAX_S = 5.0

# Distance de scroll par étape, exprimée en multiple de `window.innerHeight`.
# Auparavant fixe (`* 3` en dur dans scraper.py) : une distance identique à
# chaque étape, à intervalle régulier, est un signal comportemental répétitif
# facilement détectable. Rendue variable (tirage uniforme à chaque étape,
# même logique que PAGE_DELAY_MIN_S/MAX_S ci-dessus) pour s'en rapprocher
# d'un comportement humain. Bornes choisies empiriquement : en dessous de 1.5
# le scroll avance trop peu pour déclencher le chargement du fil suivant de
# façon fiable ; au-dessus de 4.5 on s'approche d'un saut brutal, plus proche
# d'un comportement de bot que d'un utilisateur qui lit en scrollant.
SCROLL_DISTANCE_MULTIPLICATEUR_MIN = 1.5
SCROLL_DISTANCE_MULTIPLICATEUR_MAX = 4.5
PAUSE_ENTRE_BATCHES_MIN_S = 15.0
PAUSE_ENTRE_BATCHES_MAX_S = 45.0

MAX_PAGES_SANS_NOUVEAU_POST = 4  # arrêt du scroll si N étapes consécutives sans post inédit
MAX_PAGES_ABSOLU = 60  # garde-fou dur pour éviter un scroll infini (mode "daily")

# Plafond dédié au mode "backfill", plus permissif que MAX_PAGES_ABSOLU.
#
# RÉVISÉ le 2026-08-11 (250 -> 650), après analyse de 16 runs backfill réels
# (2170 annonces) via des exports du dashboard : avec `group_limit=3`, la
# plupart des groupes plafonnaient à 250 étapes (~15 min à ~3,5s/étape) SANS
# avoir atteint 90 jours - certains groupes très actifs (jusqu'à 293k
# membres, plusieurs centaines de posts/jour) épuisaient les 250 étapes en
# quelques jours de contenu seulement. Seuls 3 groupes/pages moins denses
# atteignaient déjà 50-94 jours avec ce plafond, preuve que le plafond - pas
# `days_back` - était le facteur limitant sur les groupes actifs.
#
# 650 est dimensionné pour consommer tout le budget de session en un seul
# groupe : 650 étapes x ~3,5s/étape ≈ 38 min, sous les 45 min de
# SESSION_DUREE_MAX_MINUTES avec une marge de sécurité pour l'échauffement et
# le traitement réseau. CONSÉQUENCE IMPORTANTE : ce dimensionnement suppose
# `group_limit=1` en backfill désormais (pas 3) - avec 3 groupes, le budget
# de session serait dépassé (3 x 38 min > 45 min), un 2e ou 3e groupe d'un
# batch pourrait alors faire déborder la session bien au-delà de 45 min avant
# de s'arrêter (aucune vérification du budget de session PENDANT le scroll
# d'un groupe, seulement ENTRE deux groupes - voir executer_scraping). Un
# seul groupe par run signifie plus de runs nécessaires pour tourner sur les
# 23 cibles (rotation persistée, voir appliquer_rotation_backfill) - à
# automatiser via un déclenchement planifié plutôt que des lancements
# manuels répétés.
#
# Toujours un plafond "best effort", pas une garantie de couverture des 90
# jours sur les groupes les plus denses - aucune donnée réelle ne permet de
# l'affirmer avec certitude avant d'avoir observé des runs à ce nouveau
# plafond.
MAX_PAGES_ABSOLU_BACKFILL = 650

NAVIGATION_TIMEOUT_MS = 30_000

# Fragments d'URL identifiant une requête GraphQL Facebook (pour intercepter
# les réponses réseau déclenchées par le scroll et y chercher des posts).
# INCERTITUDE ASSUMÉE : ce pattern (`/api/graphql/`) est celui documenté
# publiquement par la communauté pour Facebook web, non vérifié en conditions
# réelles depuis mon environnement (pas d'accès réseau). Si le scroll ne
# ramène jamais de nouveau post en conditions réelles alors que le compte a
# clairement plus de contenu, ce pattern est le premier suspect à vérifier
# (ouvrir les DevTools > Network > filtrer "graphql" pendant un scroll réel).
GRAPHQL_URL_FRAGMENTS = ["/api/graphql/"]

# Profondeur maximale de parcours récursif d'un blob JSON à la recherche de
# posts - protection contre un coût CPU excessif sur un payload très large
# et profondément imbriqué (observé : blobs de 170 Ko+, des centaines par page).
JSON_PROFONDEUR_MAX = 12

# --------------------------------------------------------------------------- #
# Stratégie anti-blocage : circuit breaker + budget de session
# --------------------------------------------------------------------------- #
#
# Le facteur qui a le plus d'impact réel sur le risque de blocage n'est PAS le
# code (délais, user-agent, etc.) mais l'infrastructure (réputation de l'IP/ASN
# du runner) et la confiance du compte utilisé - voir README.md, section
# "Stratégie anti-blocage". Ce qui suit ne compense pas ces facteurs, ça réduit
# seulement le risque évitable côté comportement.

# En cas de blocage détecté (checkpoint, mur anti-bot), on arrête TOUT le run
# immédiatement (pas seulement le groupe en cours) et on impose un délai de
# repos avant tout nouveau run - retenter aussitôt après un blocage est le
# signal le plus voyant possible pour un système anti-bot.
COOLDOWN_HEURES_APRES_BLOCAGE = 24
COOLDOWN_HEURES_APRES_SESSION_EXPIREE = 6  # probablement juste les cookies à renouveler, pas un blocage actif

# Durée maximale d'un run, tous groupes confondus. Une session de scraping qui
# tourne des heures d'affilée est un signal comportemental fort ; mieux vaut
# couper proprement (les groupes restants seront traités au run suivant) que
# de pousser un run interminable.
SESSION_DUREE_MAX_MINUTES = 45

# --------------------------------------------------------------------------- #
# Throttle adaptatif (AIMD) : ajuste automatiquement délais et volume selon
# l'historique récent, plutôt que d'appliquer toujours les mêmes réglages.
# Logique "additive increase / multiplicative decrease" - le même principe que
# le contrôle de congestion TCP : on ralentit fort et vite au moindre signal
# de suspicion, on ré-accélère lentement seulement après plusieurs runs propres
# consécutifs. C'est un throttle défensif auto-régulé, pas une technique
# d'évasion : il ne cherche jamais à déjouer Facebook, seulement à réduire le
# volume/rythme de lui-même quand quelque chose semble anormal.
# --------------------------------------------------------------------------- #

NIVEAU_CONFIANCE_MIN = 0.2
NIVEAU_CONFIANCE_MAX = 1.0
NIVEAU_CONFIANCE_INITIAL = 1.0
NIVEAU_CONFIANCE_PALIER_SUSPICION = 0.5  # multiplicateur appliqué en cas de suspicion (decrease)
RUNS_PROPRES_POUR_RAMPUP = 3  # nb de runs propres consécutifs avant d'augmenter la confiance
RAMPUP_INCREMENT = 0.15  # augmentation additive, volontairement lente
RATIO_ANOMALIES_SUSPICION = 0.3  # >30% de groupes en erreur sur un run = signal de suspicion
COOLDOWN_MULTIPLICATEUR_MAX = 8  # plafonne le cooldown exponentiel (24h * 8 = 8 jours max)

# --------------------------------------------------------------------------- #
# Configuration LLM (structuration niveau 2)
# --------------------------------------------------------------------------- #
#
# OpenAI plutôt qu'Anthropic (changement demandé le 2026-08-01, remplacement
# complet - plus de dépendance anthropic dans requirements.txt). gpt-4o-mini
# retenu : c'est le moins cher des modèles OpenAI supportant les Structured
# Outputs au moment du choix ($0.15/1M tokens entrée, $0.60/1M sortie -
# comparé à gpt-4.1-mini à $0.40/$1.60, ~2.7x plus cher, sans intérêt ici vu
# la taille d'un post Facebook), rôle équivalent à claude-3-5-haiku utilisé
# avant. Prix vérifiés via recherche web le 2026-08-01, à revérifier
# périodiquement (les tarifs LLM changent souvent).
OPENAI_MODEL = "gpt-4o-mini"
OPENAI_TEMPERATURE = 0.0
OPENAI_MAX_TOKENS = 1024
LLM_MAX_CONCURRENCE = 5  # requêtes simultanées max (throttling coût + rate limits)
LLM_MAX_RETRIES = 3
LLM_BACKOFF_BASE_S = 2.0

TYPES_BIEN_VALIDES = ["parcelle", "maison", "villa", "ferme", "autre"]

# Schéma JSON envoyé à l'API OpenAI via Structured Outputs (response_format
# json_schema, strict=True) pour forcer une sortie JSON garantie conforme au
# schéma (plus robuste que de parser un bloc de texte libre en JSON) - même
# principe que le "tool use" utilisé avec l'API Claude précédemment.
#
# Contraintes du mode strict OpenAI (différentes d'Anthropic) : TOUTES les
# propriétés doivent figurer dans "required" (l'optionnalité se représente
# par un type nullable `["string", "null"]`, pas par absence de la clé), et
# "additionalProperties": false est obligatoire à chaque niveau d'objet.
# Le schéma "métier" ci-dessous respectait déjà ces deux contraintes par
# hasard (hérité du format Anthropic) - seul "additionalProperties" a été
# ajouté, et l'enveloppe externe (name/strict/schema) a changé de forme.
SCHEMA_ANNONCE_PROPRIETES = {
    "est_une_annonce_valide": {
        "type": "boolean",
        "description": (
            "true si c'est une vraie annonce de vente d'un bien immobilier/foncier "
            "à Ouagadougou ou environs ; false si spam, recherche d'achat, "
            "hors-sujet ou contenu incompréhensible."
        ),
    },
    "type_bien": {
        "type": "string",
        "enum": TYPES_BIEN_VALIDES,
    },
    "quartier_zone": {
        "type": ["string", "null"],
        "description": "Quartier/secteur/commune mentionné, tel qu'écrit dans le texte.",
    },
    "superficie_m2": {
        "type": ["integer", "null"],
        "description": "Superficie convertie en m² (1 ha = 10000 m²). null si absente.",
    },
    "prix_fcfa": {
        "type": ["integer", "null"],
        "description": "Prix en FCFA, sans séparateurs. null si absent ou 'non précisé'.",
    },
    "statut_document": {
        "type": ["string", "null"],
        "description": "Ex: Attestation, Titre Foncier, PUH, Permis d'habiter, APFR.",
    },
    "contacts_whatsapp": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Numéros de téléphone/WhatsApp mentionnés, format brut.",
    },
    "mots_cles_pertinents": {
        "type": "array",
        "items": {"type": "string"},
    },
    "resume_court": {
        "type": "string",
        "description": "Résumé en une phrase (max ~25 mots), en français.",
    },
}

SCHEMA_ANNONCE_JSON_SCHEMA = {
    "name": "structurer_annonce_fonciere",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": SCHEMA_ANNONCE_PROPRIETES,
        "required": list(SCHEMA_ANNONCE_PROPRIETES.keys()),
        "additionalProperties": False,
    },
}

PROMPT_SYSTEME_LLM = (
    "Tu es un extracteur de données structurées spécialisé dans le marché foncier de "
    "Ouagadougou (Burkina Faso). Tu reçois le texte brut d'un post Facebook et tu dois "
    "répondre avec les champs extraits, au format JSON demandé. "
    "Règles strictes : ne devine JAMAIS une valeur absente du texte (mets null) ; "
    "ne convertis pas approximativement un prix ou une superficie ambigus, laisse null ; "
    "si le post est une recherche d'achat, du spam, ou non lié à l'immobilier/foncier "
    "de la région de Ouagadougou, mets `est_une_annonce_valide` à false."
)
