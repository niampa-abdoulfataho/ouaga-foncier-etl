"""Fixtures partagées pour la suite de tests.

Toutes les données de posts utilisées dans les tests sont SYNTHÉTIQUES
(inventées pour les besoins des tests), aucune ne provient d'un vrai scraping
Facebook.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
import pytest

import config


@pytest.fixture
def repertoires_isoles(tmp_path, monkeypatch):
    """Redirige tous les chemins de données de `config` vers un dossier temporaire,
    pour qu'aucun test n'écrive dans le vrai dossier data/ du projet.
    """
    raw = tmp_path / "raw"
    processed = tmp_path / "processed"
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    for d in (raw, processed, state, logs):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "RAW_DIR", raw)
    monkeypatch.setattr(config, "PROCESSED_DIR", processed)
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "LOG_DIR", logs)
    monkeypatch.setattr(config, "SEEN_IDS_PATH", state / "seen_post_ids.json")
    monkeypatch.setattr(config, "COOLDOWN_PATH", state / "cooldown_until.json")
    monkeypatch.setattr(config, "SANTE_PATH", state / "sante_scraper.json")
    monkeypatch.setattr(config, "ROTATION_BACKFILL_PATH", state / "rotation_backfill.json")
    monkeypatch.setattr(config, "STORAGE_STATE_PATH", state / "storage_state.json")
    monkeypatch.setattr(config, "MASTER_XLSX_PATH", processed / "annonces.xlsx")

    return {"raw": raw, "processed": processed, "state": state, "logs": logs}


# --------------------------------------------------------------------------- #
# Fixture PostgreSQL de test.
#
# Lit l'URL de connexion depuis la variable d'environnement TEST_DATABASE_URL
# (jamais DATABASE_URL directement - on ne veut jamais qu'un test se connecte
# par accident à une vraie base de production si les deux variables sont
# définies en même temps dans un environnement mal isolé). Si la variable est
# absente ou le serveur injoignable, tous les tests qui en dépendent sont
# skippés plutôt qu'en échec - un environnement sans Postgres local reste
# utilisable pour le reste de la suite (regex, parseur d'horodatage, etc.).
# --------------------------------------------------------------------------- #

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()


def _postgres_disponible() -> bool:
    if not TEST_DATABASE_URL:
        return False
    try:
        with psycopg.connect(TEST_DATABASE_URL, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.OperationalError:
        return False


@pytest.fixture
def base_test_isolee(monkeypatch):
    """Fournit une base PostgreSQL de test propre (tables vidées avant/après)
    et redirige `config.DATABASE_URL` dessus.

    Utilise TRUNCATE plutôt qu'une transaction non commitée : le code testé
    (`processor.py`) ouvre ses propres connexions et fait ses propres commits,
    donc une transaction englobante au niveau du test ne pourrait pas être
    annulée proprement (les commits internes la casseraient). TRUNCATE avant
    ET après chaque test isole donc correctement sans dépendre du code testé.
    """
    if not _postgres_disponible():
        pytest.skip(
            "TEST_DATABASE_URL absente ou serveur Postgres de test injoignable "
            "(voir README.md, section Tests)."
        )

    monkeypatch.setattr(config, "DATABASE_URL", TEST_DATABASE_URL)

    def _purger() -> None:
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            conn.execute(
                "DROP TABLE IF EXISTS annonces, runs CASCADE"
            )
            conn.commit()

    _purger()
    yield TEST_DATABASE_URL
    _purger()


@pytest.fixture
def fichier_groupes_valide(tmp_path) -> Path:
    """Un groups.csv de test avec 2 groupes actifs, 1 inactif, 1 placeholder TODO."""
    chemin = tmp_path / "groups.csv"
    chemin.write_text(
        "id,nom,url,actif\n"
        '1111,"Terrains Ouaga Test","https://www.facebook.com/groups/1111/",true\n'
        '2222,"Foncier Burkina Test","https://www.facebook.com/groups/2222/",true\n'
        '3333,"Groupe Inactif Test","https://www.facebook.com/groups/3333/",false\n'
        "TODO_1,\"À compléter\",\"https://www.facebook.com/groups/TODO_1/\",false\n",
        encoding="utf-8",
    )
    return chemin


@pytest.fixture
def posts_bruts_exemple() -> list[dict]:
    """Jeu de posts synthétiques couvrant les cas limites du filtrage niveau 1."""
    return [
        {
            "id": "p1",
            "groupe_id": "1111",
            "groupe_nom": "Terrains Ouaga Test",
            "url": "https://www.facebook.com/groups/1111/posts/1",
            "texte": (
                "A VENDRE : Parcelle de 600 m2 à Ouaga 2000, titre foncier disponible, "
                "prix 15 000 000 FCFA négociable. Contact WhatsApp 70123456."
            ),
            "date_publication": None,
            "date_incertaine": True,
            "scrape_le": "2026-08-01T10:00:00+00:00",
        },
        {
            "id": "p2",
            "groupe_id": "1111",
            "groupe_nom": "Terrains Ouaga Test",
            "url": "https://www.facebook.com/groups/1111/posts/2",
            "texte": "Je recherche un terrain à Saaba, budget limité, qui a une parcelle à proposer ?",
            "date_publication": None,
            "date_incertaine": True,
            "scrape_le": "2026-08-01T10:01:00+00:00",
        },
        {
            "id": "p3",
            "groupe_id": "2222",
            "groupe_nom": "Foncier Burkina Test",
            "url": "https://www.facebook.com/groups/2222/posts/3",
            "texte": "Cliquez ici pour gagner 5000$ en 24h !!! whatsapp: +22670000000",
            "date_publication": None,
            "date_incertaine": True,
            "scrape_le": "2026-08-01T10:02:00+00:00",
        },
        {
            "id": "p4",
            "groupe_id": "2222",
            "groupe_nom": "Foncier Burkina Test",
            "url": "https://www.facebook.com/groups/2222/posts/4",
            "texte": "Joyeux anniversaire à toute l'équipe du groupe !",
            "date_publication": None,
            "date_incertaine": True,
            "scrape_le": "2026-08-01T10:03:00+00:00",
        },
        {
            "id": "p5",
            "groupe_id": "2222",
            "groupe_nom": "Foncier Burkina Test",
            "url": "https://www.facebook.com/groups/2222/posts/5",
            # Doublon exact du texte de p1 (republié dans un autre groupe).
            "texte": (
                "A VENDRE : Parcelle de 600 m2 à Ouaga 2000, titre foncier disponible, "
                "prix 15 000 000 FCFA négociable. Contact WhatsApp 70123456."
            ),
            "date_publication": None,
            "date_incertaine": True,
            "scrape_le": "2026-08-01T10:04:00+00:00",
        },
    ]


@pytest.fixture
def fichier_posts_bruts(tmp_path, posts_bruts_exemple) -> Path:
    chemin = tmp_path / "20260801T100000Z_1111.json"
    chemin.write_text(json.dumps(posts_bruts_exemple, ensure_ascii=False), encoding="utf-8")
    return chemin
