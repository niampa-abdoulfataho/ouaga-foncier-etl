"""Tests de l'orchestrateur CLI (parsing des arguments + gestion des codes de sortie)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import psycopg
import pytest

import config
import main
import scraper


class TestParserArguments:
    def test_mode_daily_par_defaut(self):
        args = main.parser_arguments([])
        assert args.mode == "daily"
        assert args.days_back == config.MAX_DAYS_BACK_DAILY
        assert args.group_limit == 0
        assert args.batch_size == config.GROUPS_BATCH_SIZE_DEFAULT

    def test_mode_backfill_days_back_par_defaut(self):
        args = main.parser_arguments(["--mode", "backfill"])
        assert args.days_back == config.MAX_DAYS_BACK_BACKFILL_DEFAULT

    def test_days_back_explicite_est_respecte(self):
        args = main.parser_arguments(["--mode", "backfill", "--days-back", "30"])
        assert args.days_back == 30

    def test_days_back_zero_est_rejete(self):
        with pytest.raises(SystemExit):
            main.parser_arguments(["--days-back", "0"])

    def test_days_back_negatif_est_rejete(self):
        with pytest.raises(SystemExit):
            main.parser_arguments(["--days-back", "-5"])

    def test_batch_size_zero_est_rejete(self):
        with pytest.raises(SystemExit):
            main.parser_arguments(["--batch-size", "0"])

    def test_group_limit_negatif_est_rejete(self):
        with pytest.raises(SystemExit):
            main.parser_arguments(["--group-limit", "-1"])

    def test_mode_invalide_est_rejete(self):
        with pytest.raises(SystemExit):
            main.parser_arguments(["--mode", "hebdomadaire"])

    def test_skip_llm_par_defaut_desactive(self):
        args = main.parser_arguments([])
        assert args.skip_llm is False

    def test_skip_llm_active(self):
        args = main.parser_arguments(["--skip-llm"])
        assert args.skip_llm is True

    def test_group_id_absent_par_defaut(self):
        args = main.parser_arguments([])
        assert args.group_id is None

    def test_group_id_explicite_est_respecte(self):
        args = main.parser_arguments(["--group-id", "1014671535986718"])
        assert args.group_id == "1014671535986718"


class TestMainEndToEnd:
    def test_aucun_post_collecte_traite_quand_meme_via_la_db_maitre(
        self, monkeypatch, repertoires_isoles, base_test_isolee
    ):
        # Pas de branche spéciale "vide" : le traitement tourne quand même avec
        # 0 post, ce qui alimente la détection de dérive avec le cas le plus
        # révélateur (0 résultat sans aucune erreur).
        monkeypatch.setattr(scraper, "executer_scraping", AsyncMock(return_value=[]))
        code = main.main(["--mode", "daily"])
        assert code == 0
        assert config.MASTER_XLSX_PATH.exists()
        with psycopg.connect(base_test_isolee) as conn:
            nb_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert nb_runs == 1

    def test_erreur_configuration_retourne_1(self, monkeypatch, repertoires_isoles):
        monkeypatch.setattr(
            scraper, "executer_scraping",
            AsyncMock(side_effect=ValueError("FB_COOKIES_JSON absent")),
        )
        code = main.main(["--mode", "daily"])
        assert code == 1

    def test_session_expiree_retourne_2(self, monkeypatch, repertoires_isoles):
        monkeypatch.setattr(
            scraper, "executer_scraping",
            AsyncMock(side_effect=scraper.SessionExpireeError("cookies morts")),
        )
        code = main.main(["--mode", "daily"])
        assert code == 2

    def test_blocage_detecte_retourne_3(self, monkeypatch, repertoires_isoles):
        monkeypatch.setattr(
            scraper, "executer_scraping",
            AsyncMock(side_effect=scraper.BlocageDetecteError("checkpoint")),
        )
        code = main.main(["--mode", "daily"])
        assert code == 3

    def test_cooldown_actif_retourne_0_sans_planter(self, monkeypatch, repertoires_isoles):
        # Un cooldown actif n'est pas un échec : le workflow CI ne doit pas
        # passer au rouge tous les jours où le mécanisme de sécurité agit.
        monkeypatch.setattr(
            scraper, "executer_scraping",
            AsyncMock(side_effect=scraper.CooldownActifError("cooldown jusqu'à demain")),
        )
        code = main.main(["--mode", "daily"])
        assert code == 0

    def test_group_id_est_transmis_a_executer_scraping(
        self, monkeypatch, repertoires_isoles
    ):
        appel = AsyncMock(return_value=[])
        monkeypatch.setattr(scraper, "executer_scraping", appel)
        main.main(["--mode", "backfill", "--group-id", "1014671535986718"])
        appel.assert_awaited_once()
        assert appel.await_args.kwargs["groupe_id"] == "1014671535986718"

    def test_pipeline_complet_appelle_processor(self, monkeypatch, repertoires_isoles, tmp_path):
        faux_fichier = tmp_path / "posts.json"
        faux_fichier.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(scraper, "executer_scraping", AsyncMock(return_value=[faux_fichier]))

        chemin_attendu = config.PROCESSED_DIR / "annonces_test.csv"
        chemin_attendu.write_text("id\n", encoding="utf-8")
        appel_traitement = AsyncMock(return_value=chemin_attendu)

        import processor
        monkeypatch.setattr(processor, "executer_traitement", appel_traitement)

        code = main.main(["--mode", "daily"])

        assert code == 0
        appel_traitement.assert_awaited_once()

    def test_skip_llm_evite_appel_processor_llm(self, monkeypatch, repertoires_isoles, tmp_path):
        faux_fichier = tmp_path / "posts.json"
        faux_fichier.write_text(
            '[{"id": "p1", "groupe_nom": "T", "url": "https://x", '
            '"texte": "Terrain à vendre Ouaga 2000"}]',
            encoding="utf-8",
        )
        monkeypatch.setattr(scraper, "executer_scraping", AsyncMock(return_value=[faux_fichier]))

        import processor
        appel_llm = AsyncMock()
        monkeypatch.setattr(processor, "executer_traitement", appel_llm)

        code = main.main(["--mode", "daily", "--skip-llm"])

        assert code == 0
        appel_llm.assert_not_awaited()
        assert (config.PROCESSED_DIR / "candidats_sans_llm.csv").exists()
