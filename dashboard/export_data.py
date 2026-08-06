"""Exporte un instantané léger de la base maître PostgreSQL (Neon) vers un
fichier JSON statique, consommé par le dashboard Shinylive (dashboard/app/).

Pourquoi un export statique plutôt qu'une connexion directe depuis le
dashboard : Shinylive compile l'application en WebAssembly et l'exécute
entièrement dans le navigateur de l'utilisateur - il n'y a AUCUN serveur
Python côté dashboard. Une connexion PostgreSQL directe y est impossible
(pas de socket TCP dans le bac à sable du navigateur) et, même si elle
l'était, le DSN se retrouverait exposé en clair dans le bundle téléchargé
par n'importe quel visiteur. Ce script tourne donc côté serveur (GitHub
Actions, où DATABASE_URL est déjà un secret existant), produit un JSON qui
ne contient aucune donnée d'authentification, et c'est ce JSON qui est
embarqué dans l'export Shinylive.

Champs volontairement EXCLUS de l'export, pour rester public sans risque :
- contacts_whatsapp : numéros de téléphone de tiers, déjà identifiés comme
  point d'exposition légale dans le README (section "Limites connues").
  Publier ceci sur GitHub Pages (public par défaut) serait une fuite de
  données personnelles, pas juste un souci de confidentialité du projet.
- texte_nettoye : texte brut du post, peut contenir les mêmes numéros ou
  d'autres informations personnelles non filtrées.

Usage :
    python dashboard/export_data.py [--sortie CHEMIN]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger("export_dashboard")

CHEMIN_SORTIE_DEFAUT = Path(__file__).parent / "app" / "data" / "annonces.json"

# Colonnes de `annonces` réellement exportées (voir SCHEMA_SQL dans
# processor.py pour la liste complète) - toute nouvelle colonne ajoutée côté
# pipeline doit être explicitement ajoutée ici, elle n'est pas exportée par
# défaut. C'est un choix délibéré : mieux vaut oublier une colonne utile
# (facile à corriger) que publier par mégarde un champ sensible.
COLONNES_ANNONCES = [
    "id",
    "groupe_nom",
    "date_publication",
    "date_incertaine",
    "type_bien",
    "quartier_zone",
    "superficie_m2",
    "prix_fcfa",
    "statut_document",
    "resume_court",
    "premiere_collecte",
    "derniere_maj",
]

COLONNES_RUNS = ["horodatage", "mode", "nb_posts_bruts", "nb_candidats", "nb_valides"]


def _serialiser(valeur: Any) -> Any:
    """Convertit les types psycopg non JSON-natifs (datetime/date) en chaîne
    ISO 8601. `date_publication` est déjà du TEXT en base (voir SCHEMA_SQL),
    donc généralement déjà une chaîne - cette fonction gère aussi le cas
    d'une valeur None ou d'un objet date/datetime venant des colonnes
    TIMESTAMPTZ (premiere_collecte, derniere_maj, horodatage).
    """
    if isinstance(valeur, (datetime, date)):
        return valeur.isoformat()
    return valeur


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit(
            "DATABASE_URL absente ou vide - ce script doit tourner avec le même "
            "secret que le pipeline de scraping (voir .github/workflows/deploy_dashboard.yml)."
        )
    return dsn


def exporter(chemin_sortie: Path = CHEMIN_SORTIE_DEFAUT) -> None:
    dsn = _dsn()
    logger.info("Connexion à la base maître...")
    # Pas de fallback silencieux : une base injoignable doit faire échouer le
    # job CI bruyamment plutôt que publier un dashboard avec un JSON vide ou
    # périmé sans que personne ne le remarque (même philosophie que
    # processor._connexion).
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(COLONNES_ANNONCES)} FROM annonces "
            f"ORDER BY date_publication DESC NULLS LAST"
        )
        annonces = [
            {col: _serialiser(val) for col, val in zip(COLONNES_ANNONCES, ligne)}
            for ligne in cur.fetchall()
        ]

        cur.execute(f"SELECT {', '.join(COLONNES_RUNS)} FROM runs ORDER BY horodatage")
        runs = [
            {col: _serialiser(val) for col, val in zip(COLONNES_RUNS, ligne)}
            for ligne in cur.fetchall()
        ]

    donnees = {
        "exporte_le": datetime.now().astimezone().isoformat(),
        "nb_annonces": len(annonces),
        "nb_runs": len(runs),
        "annonces": annonces,
        "runs": runs,
    }

    chemin_sortie.parent.mkdir(parents=True, exist_ok=True)
    with chemin_sortie.open("w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False, separators=(",", ":"))

    logger.info(
        "Export terminé : %d annonce(s), %d run(s) -> %s (%.1f Ko)",
        len(annonces),
        len(runs),
        chemin_sortie,
        chemin_sortie.stat().st_size / 1024,
    )


def _parser_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parseur = argparse.ArgumentParser(description=__doc__)
    parseur.add_argument(
        "--sortie",
        type=Path,
        default=CHEMIN_SORTIE_DEFAUT,
        help=f"Chemin du JSON produit (défaut : {CHEMIN_SORTIE_DEFAUT}).",
    )
    return parseur.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parser_arguments(argv)
    try:
        exporter(args.sortie)
    except psycopg.OperationalError as exc:
        logger.critical("Base maître injoignable : %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
