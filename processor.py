"""Étape A (filtrage regex local, gratuit) + Étape B (structuration via API Claude).

Sépare volontairement les deux étapes en fonctions indépendantes et testables :
`filtrer_candidats` ne fait aucun appel réseau (100% testable hors-ligne),
`structurer_lot` est la seule partie qui appelle l'API Claude.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from openpyxl import Workbook

from openai import AsyncOpenAI
from openai import APIConnectionError, APIStatusError, RateLimitError
from pydantic import BaseModel, ValidationError, field_validator

import config

logger = logging.getLogger("ouaga_foncier_etl.processor")

_RE_URL = re.compile(r"https?://\S+")
_RE_ESPACES = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Étape A : nettoyage + filtrage regex (aucun coût API)
# --------------------------------------------------------------------------- #


def nettoyer_texte(texte: str | None) -> str:
    """Normalise le texte brut : retire les URLs, compresse les espaces."""
    if not texte:
        return ""
    texte = _RE_URL.sub("", texte)
    texte = _RE_ESPACES.sub(" ", texte)
    return texte.strip()


def dedupliquer_par_texte(posts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Supprime les doublons stricts (texte nettoyé identique).

    Utile car un même post republié/partagé dans plusieurs groupes suivis
    produit souvent un texte identique - inutile de payer 2x l'API pour ça.
    """
    vus: set[str] = set()
    uniques: list[dict[str, Any]] = []
    for p in posts:
        cle = p.get("texte_nettoye", "")
        if cle and cle in vus:
            continue
        if cle:
            vus.add(cle)
        uniques.append(p)
    return uniques, len(posts) - len(uniques)


def filtrer_candidats(posts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Étape A complète : nettoyage -> filtrage regex -> dédoublonnage.

    Returns:
        (candidats à envoyer au LLM, rejetés niveau 1 avec motif de rejet).
    """
    candidats: list[dict[str, Any]] = []
    rejetes: list[dict[str, Any]] = []

    for post in posts:
        texte_nettoye = nettoyer_texte(post.get("texte"))
        enrichi = {**post, "texte_nettoye": texte_nettoye}
        if not texte_nettoye:
            rejetes.append({**enrichi, "motif_rejet": "texte_vide"})
        elif config.est_candidat_foncier(texte_nettoye):
            candidats.append(enrichi)
        else:
            rejetes.append({**enrichi, "motif_rejet": "regex_niveau1"})

    candidats_dedup, nb_doublons = dedupliquer_par_texte(candidats)
    if nb_doublons:
        logger.info("Étape A : %d doublon(s) de texte supprimé(s).", nb_doublons)

    logger.info(
        "Étape A : %d posts -> %d candidats (%.1f%%), %d rejetés.",
        len(posts), len(candidats_dedup),
        100 * len(candidats_dedup) / max(len(posts), 1),
        len(rejetes) + nb_doublons,
    )
    return candidats_dedup, rejetes


# --------------------------------------------------------------------------- #
# Étape B : structuration via API OpenAI (async, Structured Outputs, schéma forcé)
# --------------------------------------------------------------------------- #

# Borne PostgreSQL INTEGER (colonnes superficie_m2/prix_fcfa, voir SCHEMA_SQL)
# - PAS un seuil "métier" inventé, une contrainte technique réelle : un
# INTEGER Postgres est signé sur 4 octets, le dépassement fait planter
# l'upsert avec `psycopg.errors.NumericValueOutOfRange` (observé en
# conditions réelles le 2026-08-11 sur un run backfill profond - une valeur
# de prix/superficie mal extraite par le LLM sur un post ancien a fait
# échouer TOUT le batch d'upsert, y compris les annonces valides qui le
# précédaient - voir aussi le bug d'atomicité corrigé dans upsert_annonces).
# Constante au niveau module plutôt qu'attribut de classe : Pydantic v2
# intercepte tout attribut de classe préfixé par "_" comme un ModelPrivateAttr
# (confirmé en testant en conditions réelles - `cls._X` renvoie l'objet
# descripteur, pas la valeur), donc inutilisable tel quel dans un validator.
POSTGRES_INTEGER_MAX = 2_147_483_647


class AnnonceStructuree(BaseModel):
    """Schéma de sortie validé (voir aussi `config.SCHEMA_ANNONCE_JSON_SCHEMA` côté prompt).

    Utiliser Pydantic ici - plutôt qu'un simple `dict` non validé - permet de
    détecter immédiatement si le LLM dévie du contrat (type incohérent,
    enum invalide) au lieu de laisser une donnée corrompue silencieusement
    polluer le CSV final.
    """

    est_une_annonce_valide: bool
    type_bien: str
    quartier_zone: str | None = None
    superficie_m2: int | None = None
    prix_fcfa: int | None = None
    statut_document: str | None = None
    contacts_whatsapp: list[str] = []
    mots_cles_pertinents: list[str] = []
    resume_court: str = ""

    @field_validator("type_bien")
    @classmethod
    def _valider_type_bien(cls, v: str) -> str:
        if v not in config.TYPES_BIEN_VALIDES:
            logger.warning("type_bien inattendu du LLM : %r (conservé tel quel)", v)
        return v

    @field_validator("superficie_m2", "prix_fcfa")
    @classmethod
    def _valider_positif(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            logger.warning("Valeur numérique négative du LLM ignorée (mise à null) : %s", v)
            return None
        if v is not None and v > POSTGRES_INTEGER_MAX:
            logger.warning(
                "Valeur numérique du LLM hors bornes INTEGER Postgres, ignorée "
                "(mise à null) : %s",
                v,
            )
            return None
        return v


def _construire_client(api_key: str | None = None) -> AsyncOpenAI:
    # .strip() : même piège que DATABASE_URL (voir config.py) - un secret CI
    # collé avec un retour à la ligne final casserait l'en-tête HTTP
    # Authorization envoyé par le client OpenAI.
    cle = (api_key or os.environ.get(config.ENV_OPENAI_KEY, "")).strip()
    if not cle:
        raise ValueError(f"Variable d'environnement {config.ENV_OPENAI_KEY} absente.")
    return AsyncOpenAI(api_key=cle)


async def structurer_annonce(
    client: AsyncOpenAI,
    texte: str,
    semaphore: asyncio.Semaphore,
    max_retries: int = config.LLM_MAX_RETRIES,
) -> dict[str, Any] | None:
    """Appelle l'API OpenAI pour structurer un post, avec retry/backoff exponentiel.

    Retourne None (plutôt que de lever) en cas d'échec définitif, pour que le
    traitement du lot entier ne soit pas interrompu par un seul post en erreur -
    l'appelant compte les échecs et les journalise (cf. `structurer_lot`).

    INCERTITUDE ASSUMÉE : cet appel (Structured Outputs, `response_format`
    json_schema strict) n'a pas pu être testé contre l'API OpenAI réelle -
    aucun accès réseau sortant vers api.openai.com depuis mon environnement
    (confirmé par un échec de connexion direct). La forme de l'appel est
    basée sur le contrat documenté du SDK `openai` (introspection du
    signature de `AsyncCompletions.create`, qui confirme `response_format`,
    `max_tokens`, `temperature` comme paramètres valides) - à valider par un
    run réel avec `--group-limit 1` avant tout usage à volume.
    """
    async with semaphore:
        for tentative in range(1, max_retries + 1):
            try:
                reponse = await client.chat.completions.create(
                    model=config.OPENAI_MODEL,
                    max_tokens=config.OPENAI_MAX_TOKENS,
                    temperature=config.OPENAI_TEMPERATURE,
                    response_format={
                        "type": "json_schema",
                        "json_schema": config.SCHEMA_ANNONCE_JSON_SCHEMA,
                    },
                    messages=[
                        {"role": "system", "content": config.PROMPT_SYSTEME_LLM},
                        {"role": "user", "content": texte},
                    ],
                )
                message = reponse.choices[0].message

                if message.refusal:
                    logger.error(
                        "Le modèle a refusé de structurer ce post : %s", message.refusal
                    )
                    return None
                if not message.content:
                    logger.error("Réponse LLM vide pour le texte : %.80s...", texte)
                    return None

                donnees = json.loads(message.content)
                annonce = AnnonceStructuree.model_validate(donnees)
                return annonce.model_dump()

            except RateLimitError:
                attente = config.LLM_BACKOFF_BASE_S * (2 ** (tentative - 1)) + random.uniform(0, 1)
                logger.warning(
                    "Rate limit API OpenAI (tentative %d/%d) - attente %.1fs",
                    tentative, max_retries, attente,
                )
                await asyncio.sleep(attente)
            except (APIConnectionError, APIStatusError) as exc:
                attente = config.LLM_BACKOFF_BASE_S * (2 ** (tentative - 1))
                logger.warning(
                    "Erreur API OpenAI (%s, tentative %d/%d) - attente %.1fs",
                    exc, tentative, max_retries, attente,
                )
                await asyncio.sleep(attente)
            except (json.JSONDecodeError, ValidationError) as exc:
                logger.error("Sortie LLM invalide (JSON ou schéma non respecté) : %s", exc)
                return None  # inutile de retenter : le modèle a mal répondu, pas un pb réseau

        logger.error("Échec définitif après %d tentatives pour un post.", max_retries)
        return None


async def structurer_lot(
    candidats: list[dict[str, Any]],
    api_key: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Structure tous les candidats en parallèle (borné par `LLM_MAX_CONCURRENCE`).

    Returns:
        (annonces valides et structurées, posts en échec ou jugés invalides par le LLM)
    """
    if not candidats:
        return [], []

    client = _construire_client(api_key)
    semaphore = asyncio.Semaphore(config.LLM_MAX_CONCURRENCE)

    async def _traiter(post: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        resultat = await structurer_annonce(client, post["texte_nettoye"], semaphore)
        return post, resultat

    resultats = await asyncio.gather(*(_traiter(p) for p in candidats))

    valides: list[dict[str, Any]] = []
    non_valides: list[dict[str, Any]] = []

    for post, structure in resultats:
        if structure is None:
            non_valides.append({**post, "motif_rejet": "echec_api_ou_validation"})
        elif not structure["est_une_annonce_valide"]:
            non_valides.append({**post, "motif_rejet": "llm_juge_invalide", **structure})
        else:
            structure["quartier_zone"] = config.normaliser_quartier(structure.get("quartier_zone"))
            # BUG RÉEL trouvé le 2026-08-03 en analysant les 224 premières
            # annonces réelles : `normaliser_statut_document` existait dans
            # config.py mais n'était jamais appelé ici (contrairement à
            # normaliser_quartier, juste au-dessus) - "attestation",
            # "ATTESTATION" et "Attestation" restaient 3 valeurs distinctes
            # en base au lieu d'être fusionnées par casse.
            structure["statut_document"] = config.normaliser_statut_document(structure.get("statut_document"))
            valides.append({**post, **structure})

    logger.info(
        "Étape B : %d candidats -> %d annonces valides, %d rejetées/échouées.",
        len(candidats), len(valides), len(non_valides),
    )
    return valides, non_valides


# --------------------------------------------------------------------------- #
# Export CSV/JSON horodaté
# --------------------------------------------------------------------------- #

COLONNES_CSV = [
    "id", "groupe_nom", "url", "date_publication", "date_incertaine",
    "type_bien", "quartier_zone", "superficie_m2", "prix_fcfa", "statut_document",
    "contacts_whatsapp", "mots_cles_pertinents", "resume_court", "texte_nettoye",
]


def _serialiser_valeur(valeur: Any) -> str:
    if valeur is None:
        return ""
    if isinstance(valeur, list):
        return "; ".join(str(v) for v in valeur)
    return str(valeur)


def exporter_csv(annonces: list[dict[str, Any]], chemin: Path) -> Path:
    """Exporte les annonces structurées en CSV (UTF-8 avec BOM pour Excel)."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("w", encoding="utf-8-sig", newline="") as f:
        ecrivain = csv.DictWriter(f, fieldnames=COLONNES_CSV, extrasaction="ignore")
        ecrivain.writeheader()
        for annonce in annonces:
            ligne = {col: _serialiser_valeur(annonce.get(col)) for col in COLONNES_CSV}
            ecrivain.writerow(ligne)
    logger.info("%d annonces exportées -> %s", len(annonces), chemin)
    return chemin


def exporter_json_audit(rejetes: list[dict[str, Any]], chemin: Path) -> Path:
    """Sauvegarde les posts rejetés (niveau 1 ou 2) pour audit/amélioration des
    règles de filtrage - évite de perdre silencieusement l'information de ce
    qui a été exclu et pourquoi (`motif_rejet`).
    """
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("w", encoding="utf-8") as f:
        json.dump(rejetes, f, ensure_ascii=False, indent=2, default=str)
    return chemin


# --------------------------------------------------------------------------- #
# Base maître PostgreSQL : upsert par id à chaque run (jamais de doublon),
# connexion via `DATABASE_URL` (voir config.py / .env.example). psycopg (v3)
# est le driver utilisé - schéma quasi identique à la version SQLite d'origine
# (Postgres et SQLite partagent la même syntaxe `ON CONFLICT ... DO UPDATE`).
# --------------------------------------------------------------------------- #

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS annonces (
    id TEXT PRIMARY KEY,
    groupe_nom TEXT,
    url TEXT,
    date_publication TEXT,
    date_incertaine BOOLEAN,
    type_bien TEXT,
    quartier_zone TEXT,
    superficie_m2 INTEGER,
    prix_fcfa INTEGER,
    statut_document TEXT,
    contacts_whatsapp TEXT,
    mots_cles_pertinents TEXT,
    resume_court TEXT,
    texte_nettoye TEXT,
    premiere_collecte TIMESTAMPTZ NOT NULL,
    derniere_maj TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    horodatage TIMESTAMPTZ PRIMARY KEY,
    mode TEXT NOT NULL,
    nb_posts_bruts INTEGER NOT NULL,
    nb_candidats INTEGER NOT NULL,
    nb_valides INTEGER NOT NULL
);
"""


def _connexion(dsn: str) -> psycopg.Connection:
    """Ouvre une connexion PostgreSQL et s'assure que le schéma existe.

    Raises:
        psycopg.OperationalError: serveur injoignable, DSN invalide, etc. -
            propagée telle quelle (pas de fallback silencieux sur une base
            locale : mieux vaut un run qui échoue bruyamment qu'un run qui
            écrit dans le vide sans que personne ne le remarque).
    """
    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()
    return conn


_UPSERT_SQL = """
    INSERT INTO annonces (
        id, groupe_nom, url, date_publication, date_incertaine,
        type_bien, quartier_zone, superficie_m2, prix_fcfa, statut_document,
        contacts_whatsapp, mots_cles_pertinents, resume_court, texte_nettoye,
        premiere_collecte, derniere_maj
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (id) DO UPDATE SET
        groupe_nom = EXCLUDED.groupe_nom,
        url = EXCLUDED.url,
        date_publication = EXCLUDED.date_publication,
        date_incertaine = EXCLUDED.date_incertaine,
        type_bien = EXCLUDED.type_bien,
        quartier_zone = EXCLUDED.quartier_zone,
        superficie_m2 = EXCLUDED.superficie_m2,
        prix_fcfa = EXCLUDED.prix_fcfa,
        statut_document = EXCLUDED.statut_document,
        contacts_whatsapp = EXCLUDED.contacts_whatsapp,
        mots_cles_pertinents = EXCLUDED.mots_cles_pertinents,
        resume_court = EXCLUDED.resume_court,
        texte_nettoye = EXCLUDED.texte_nettoye,
        derniere_maj = EXCLUDED.derniere_maj
    """


def upsert_annonces(annonces: list[dict[str, Any]], dsn: str | None = None) -> int:
    """Insère les nouvelles annonces et met à jour celles déjà connues (upsert
    par `id` de post), sans jamais dupliquer de ligne ni perdre la date de
    première collecte d'une annonce déjà vue lors d'un run précédent.

    BUG CORRIGÉ (2026-08-11, observé en conditions réelles sur un run
    backfill profond) : la version précédente exécutait toutes les insertions
    dans UNE SEULE transaction, avec un unique commit() à la fin. Une seule
    ligne en erreur (ex. `NumericValueOutOfRange` sur un prix/superficie
    aberrant extrait par le LLM) faisait échouer tout le batch - y compris
    les dizaines/centaines d'annonces valides déjà insérées avant elle dans
    la boucle, jamais commitées à cause de l'exception. Corrigé en isolant
    chaque ligne dans sa propre transaction imbriquée (savepoint Postgres via
    `conn.transaction()` en psycopg3) : une ligne en erreur est journalisée et
    ignorée, le reste du batch est conservé.
    """
    dsn = dsn or config.DATABASE_URL
    if not annonces:
        return 0

    maintenant = datetime.now(timezone.utc)
    ids_en_echec: list[str] = []
    conn = _connexion(dsn)
    try:
        with conn.transaction():
            for a in annonces:
                try:
                    with conn.transaction():  # savepoint imbriqué : isole cette ligne du reste du batch
                        with conn.cursor() as cur:
                            cur.execute(
                                _UPSERT_SQL,
                                (
                                    a.get("id"), a.get("groupe_nom"), a.get("url"),
                                    a.get("date_publication"), bool(a.get("date_incertaine")),
                                    a.get("type_bien"), a.get("quartier_zone"),
                                    a.get("superficie_m2"), a.get("prix_fcfa"),
                                    a.get("statut_document"),
                                    _serialiser_valeur(a.get("contacts_whatsapp")),
                                    _serialiser_valeur(a.get("mots_cles_pertinents")),
                                    a.get("resume_court"), a.get("texte_nettoye"),
                                    maintenant, maintenant,
                                ),
                            )
                except psycopg.Error as exc:
                    ids_en_echec.append(str(a.get("id")))
                    logger.error(
                        "Échec upsert de l'annonce id=%s (%s) - ignorée, le reste "
                        "du batch est conservé.",
                        a.get("id"), exc,
                    )
    finally:
        conn.close()

    nb_reussies = len(annonces) - len(ids_en_echec)
    if ids_en_echec:
        logger.warning(
            "%d annonce(s) rejetée(s) lors de l'upsert (voir logs ERROR ci-dessus) : %s",
            len(ids_en_echec), ids_en_echec,
        )
    logger.info("%d annonce(s) upsertées dans la base maître", nb_reussies)
    return nb_reussies


def exporter_xlsx_depuis_db(dsn: str | None = None, chemin_xlsx: Path | None = None) -> Path:
    """Régénère UN SEUL fichier Excel à partir de l'état actuel de la base
    maître (écrasé à chaque run, pas de doublon de fichier). C'est une vue de
    consultation ; PostgreSQL reste la source de vérité.
    """
    dsn = dsn or config.DATABASE_URL
    chemin_xlsx = chemin_xlsx or config.MASTER_XLSX_PATH

    conn = _connexion(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, groupe_nom, url, date_publication, date_incertaine, type_bien, "
                "quartier_zone, superficie_m2, prix_fcfa, statut_document, contacts_whatsapp, "
                "mots_cles_pertinents, resume_court, texte_nettoye, premiere_collecte, derniere_maj "
                "FROM annonces ORDER BY derniere_maj DESC"
            )
            colonnes = [d.name for d in cur.description]
            lignes = cur.fetchall()
    finally:
        conn.close()

    classeur = Workbook()
    feuille = classeur.active
    feuille.title = "Annonces"
    feuille.append(colonnes)
    for ligne in lignes:
        # Les timestamps psycopg (datetime tz-aware) ne sont pas acceptés tels
        # quels par openpyxl si le fuseau n'est pas naïf -> conversion en texte.
        feuille.append([v.isoformat() if hasattr(v, "isoformat") else v for v in ligne])
    feuille.freeze_panes = "A2"

    chemin_xlsx.parent.mkdir(parents=True, exist_ok=True)
    classeur.save(chemin_xlsx)
    logger.info("%d annonce(s) exportées -> %s", len(lignes), chemin_xlsx)
    return chemin_xlsx


def enregistrer_run(
    mode: str, nb_posts_bruts: int, nb_candidats: int, nb_valides: int,
    dsn: str | None = None,
) -> None:
    """Journalise les statistiques du run dans la base maître (table `runs`),
    réutilisée par `detecter_derive` pour le suivi de volume dans le temps.
    """
    dsn = dsn or config.DATABASE_URL
    conn = _connexion(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (horodatage, mode, nb_posts_bruts, nb_candidats, nb_valides) "
                "VALUES (%s, %s, %s, %s, %s)",
                (datetime.now(timezone.utc), mode, nb_posts_bruts, nb_candidats, nb_valides),
            )
        conn.commit()
    finally:
        conn.close()


def detecter_derive(
    nb_valides_du_jour: int,
    mode: str,
    dsn: str | None = None,
    fenetre: int = 7,
    seuil_ratio: float = 0.3,
    minimum_historique: int = 3,
) -> str | None:
    """Compare le run actuel à la moyenne glissante des runs `daily` précédents
    (le mode backfill a un volume différent, non comparable, donc jamais
    évalué). Retourne un message d'alerte si le volume est anormalement bas,
    sinon None.

    Ne se déclenche qu'à partir de `minimum_historique` runs quotidiens déjà
    enregistrés, pour éviter les fausses alertes en tout début de projet
    (pas assez d'historique pour juger de ce qui est "normal").

    Objectif : détecter le cas silencieux le plus dangereux d'un scraper -
    "ça tourne sans erreur mais ça ne ramène plus rien" (sélecteur DOM cassé
    après un changement de Facebook), qui ne déclenche AUCUNE des exceptions
    gérées par ailleurs (pas un blocage, pas une session expirée - juste zéro
    résultat qui ressemble à une nuit calme).

    Contrairement à la version SQLite précédente, on ne peut plus se contenter
    de vérifier l'existence d'un fichier pour savoir si la base est "vide" -
    une connexion échouée (serveur injoignable) est traitée comme "pas de
    dérive détectable", pas comme une erreur bloquante à ce stade.
    """
    if mode != "daily":
        return None

    dsn = dsn or config.DATABASE_URL
    try:
        conn = _connexion(dsn)
    except psycopg.OperationalError as exc:
        logger.warning("Détection de dérive ignorée (base injoignable) : %s", exc)
        return None

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nb_valides FROM runs WHERE mode = 'daily' "
                "ORDER BY horodatage DESC LIMIT %s",
                (fenetre,),
            )
            historique = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()

    if len(historique) < minimum_historique:
        return None

    moyenne = sum(historique) / len(historique)
    if moyenne <= 0:
        return None

    if nb_valides_du_jour < moyenne * seuil_ratio:
        return (
            f"Volume anormalement bas : {nb_valides_du_jour} annonce(s) aujourd'hui vs "
            f"{moyenne:.1f} en moyenne sur les {len(historique)} derniers runs quotidiens "
            f"({100 * nb_valides_du_jour / moyenne:.0f}% de la moyenne). Signal probable d'un "
            f"sélecteur DOM cassé plutôt qu'une vraie baisse d'activité sur les groupes - à "
            f"vérifier manuellement avant de faire confiance aux prochains runs."
        )
    return None


# --------------------------------------------------------------------------- #
# Orchestration complète (appelée par main.py)
# --------------------------------------------------------------------------- #


@dataclass
class ResultatTraitement:
    """Regroupe tout ce que main.py a besoin de savoir sur l'issue du run."""

    database_url: str
    chemin_xlsx: Path
    chemin_csv_run: Path
    nb_posts_bruts: int
    nb_candidats: int
    nb_valides: int
    alerte_derive: str | None = None


def charger_posts_bruts(fichiers: list[Path]) -> list[dict[str, Any]]:
    """Charge et fusionne tous les fichiers JSON bruts produits par scraper.py,
    en dédupliquant par `id` (un même post peut apparaître dans deux fichiers
    incrémentaux si le run a été relancé après une coupure).
    """
    tous_posts: dict[str, dict[str, Any]] = {}
    for fichier in fichiers:
        try:
            with fichier.open(encoding="utf-8") as f:
                posts = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Fichier brut illisible, ignoré : %s (%s)", fichier, exc)
            continue
        for p in posts:
            tous_posts[p["id"]] = p  # la dernière occurrence gagne
    return list(tous_posts.values())


async def executer_traitement(
    fichiers_bruts: list[Path], mode: str = "daily", api_key: str | None = None,
) -> ResultatTraitement:
    """Pipeline complet : chargement -> Étape A -> Étape B -> base maître + export.

    La base PostgreSQL (`config.DATABASE_URL`) est la source de vérité, mise à
    jour par upsert (jamais de doublon entre runs). `config.MASTER_XLSX_PATH`
    en est une vue Excel régénérée à chaque run (un seul fichier, toujours à
    jour). Un CSV horodaté par run est en plus conservé dans data/processed/
    comme trace d'audit ponctuelle - la base maître reste la référence.

    Raises:
        ValueError: OPENAI_API_KEY absente ou DATABASE_URL absente.
    """
    if not config.DATABASE_URL:
        # Échec rapide et explicite AVANT tout appel LLM payant : inutile de
        # dépenser des tokens Claude pour découvrir seulement à la fin qu'il
        # n'y a nulle part où écrire le résultat.
        raise ValueError(
            f"Variable d'environnement {config.ENV_DATABASE_URL} absente. "
            "Définissez-la (ex: postgresql://user:password@localhost:5432/ouaga_foncier_etl)."
        )

    posts = charger_posts_bruts(fichiers_bruts)
    logger.info("%d posts bruts chargés depuis %d fichier(s).", len(posts), len(fichiers_bruts))

    candidats, rejetes_niveau1 = filtrer_candidats(posts)
    valides, rejetes_niveau2 = await structurer_lot(candidats, api_key=api_key)

    horodatage = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    alerte = detecter_derive(len(valides), mode=mode)
    if alerte:
        logger.warning(alerte)
    enregistrer_run(mode, len(posts), len(candidats), len(valides))

    upsert_annonces(valides)
    chemin_xlsx = exporter_xlsx_depuis_db()

    chemin_csv_run = config.PROCESSED_DIR / f"annonces_{horodatage}.csv"
    exporter_csv(valides, chemin_csv_run)
    exporter_json_audit(
        rejetes_niveau1 + rejetes_niveau2,
        config.PROCESSED_DIR / f"rejetes_{horodatage}.json",
    )

    return ResultatTraitement(
        database_url=config.DATABASE_URL,
        chemin_xlsx=chemin_xlsx,
        chemin_csv_run=chemin_csv_run,
        nb_posts_bruts=len(posts),
        nb_candidats=len(candidats),
        nb_valides=len(valides),
        alerte_derive=alerte,
    )
