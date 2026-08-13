"""Tests de l'export CSV/JSON et du chargement/dédoublonnage des fichiers bruts."""

from __future__ import annotations

import csv
import json
from types import SimpleNamespace

import pytest

import processor


class TestExporterCsv:
    def test_ecrit_les_bonnes_colonnes_et_lignes(self, tmp_path):
        annonces = [
            {
                "id": "p1", "groupe_nom": "Test", "url": "https://x", "date_publication": None,
                "date_incertaine": True, "type_bien": "parcelle", "quartier_zone": "Ouaga 2000",
                "superficie_m2": 600, "prix_fcfa": 15000000, "statut_document": "Titre Foncier",
                "contacts_whatsapp": ["70123456", "76000000"], "mots_cles_pertinents": ["parcelle"],
                "resume_court": "Résumé test", "texte_nettoye": "texte complet",
            }
        ]
        chemin = tmp_path / "sortie.csv"
        processor.exporter_csv(annonces, chemin)

        with chemin.open(encoding="utf-8-sig") as f:
            lignes = list(csv.DictReader(f))

        assert len(lignes) == 1
        assert lignes[0]["id"] == "p1"
        assert lignes[0]["contacts_whatsapp"] == "70123456; 76000000"
        assert lignes[0]["superficie_m2"] == "600"

    def test_liste_vide_produit_un_csv_avec_en_tetes_seulement(self, tmp_path):
        chemin = tmp_path / "vide.csv"
        processor.exporter_csv([], chemin)
        with chemin.open(encoding="utf-8-sig") as f:
            lignes = list(csv.DictReader(f))
        assert lignes == []

    def test_valeurs_manquantes_deviennent_chaine_vide(self, tmp_path):
        annonces = [{"id": "p1"}]  # toutes les autres colonnes absentes
        chemin = tmp_path / "partiel.csv"
        processor.exporter_csv(annonces, chemin)
        with chemin.open(encoding="utf-8-sig") as f:
            ligne = next(csv.DictReader(f))
        assert ligne["prix_fcfa"] == ""
        assert ligne["quartier_zone"] == ""


class TestExporterJsonAudit:
    def test_sauvegarde_les_rejetes(self, tmp_path):
        rejetes = [{"id": "p2", "motif_rejet": "regex_niveau1"}]
        chemin = tmp_path / "audit.json"
        processor.exporter_json_audit(rejetes, chemin)
        contenu = json.loads(chemin.read_text(encoding="utf-8"))
        assert contenu == rejetes


class _FauxCurseurExport:
    def __init__(self, lignes, total_reel, colonnes=("id",)):
        self._lignes = lignes
        self._total_reel = total_reel
        self.description = [SimpleNamespace(name=c) for c in colonnes]

    def execute(self, *_args, **_kwargs):
        pass

    def fetchall(self):
        return self._lignes

    def fetchone(self):
        return (self._total_reel,)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FausseConnexionExport:
    def __init__(self, lignes, total_reel, colonnes=("id",)):
        self._lignes = lignes
        self._total_reel = total_reel
        self._colonnes = colonnes

    def cursor(self):
        return _FauxCurseurExport(self._lignes, self._total_reel, self._colonnes)

    def commit(self):
        pass

    def close(self):
        pass


class TestLireAnnoncesAvecVerification:
    """Garde-fou ajouté le 2026-08-13 après deux exports xlsx consécutifs où
    fetchall() a silencieusement renvoyé moins de lignes que le COUNT(*) réel
    (7996->7000 puis 8374->5000, confirmés par l'utilisateur via Neon) -
    jamais d'exception levée par psycopg, jamais détectable dans le fichier
    produit lui-même. Voir le docstring de _lire_annonces_avec_verification.
    """

    def test_cas_nominal_lignes_et_count_coherents(self, monkeypatch):
        lignes = [("p1",), ("p2",)]
        monkeypatch.setattr(
            processor.psycopg,
            "connect",
            lambda *_a, **_k: _FausseConnexionExport(lignes, total_reel=2),
        )
        colonnes, resultat = processor._lire_annonces_avec_verification("postgresql://x/y")
        assert resultat == lignes
        assert colonnes == ["id"]

    def test_incoherence_resolue_a_la_deuxieme_tentative(self, monkeypatch):
        # 1re connexion : 1 ligne récupérée contre 5 attendues (incohérent).
        # 2e connexion (nouvelle) : cohérent -> doit réussir sans lever.
        connexions = iter(
            [
                _FausseConnexionExport([("p1",)], total_reel=5),
                _FausseConnexionExport([("p1",), ("p2",)], total_reel=2),
            ]
        )
        monkeypatch.setattr(
            processor.psycopg, "connect", lambda *_a, **_k: next(connexions)
        )
        colonnes, resultat = processor._lire_annonces_avec_verification("postgresql://x/y")
        assert len(resultat) == 2

    def test_incoherence_persistante_leve_runtime_error(self, monkeypatch):
        monkeypatch.setattr(
            processor.psycopg,
            "connect",
            lambda *_a, **_k: _FausseConnexionExport([("p1",)], total_reel=5),
        )
        with pytest.raises(RuntimeError, match="COUNT"):
            processor._lire_annonces_avec_verification("postgresql://x/y")


class TestChargerPostsBruts:
    def test_fusionne_plusieurs_fichiers(self, tmp_path):
        f1 = tmp_path / "a.json"
        f2 = tmp_path / "b.json"
        f1.write_text(json.dumps([{"id": "p1", "texte": "A"}]), encoding="utf-8")
        f2.write_text(json.dumps([{"id": "p2", "texte": "B"}]), encoding="utf-8")

        posts = processor.charger_posts_bruts([f1, f2])
        assert {p["id"] for p in posts} == {"p1", "p2"}

    def test_deduplique_par_id_entre_fichiers(self, tmp_path):
        f1 = tmp_path / "a.json"
        f2 = tmp_path / "b.json"
        f1.write_text(json.dumps([{"id": "p1", "texte": "ancienne version"}]), encoding="utf-8")
        f2.write_text(json.dumps([{"id": "p1", "texte": "nouvelle version"}]), encoding="utf-8")

        posts = processor.charger_posts_bruts([f1, f2])
        assert len(posts) == 1
        assert posts[0]["texte"] == "nouvelle version"  # le dernier fichier gagne

    def test_fichier_illisible_est_ignore_sans_planter(self, tmp_path, caplog):
        f_corrompu = tmp_path / "corrompu.json"
        f_corrompu.write_text("{ceci n'est pas du json valide", encoding="utf-8")

        posts = processor.charger_posts_bruts([f_corrompu])
        assert posts == []
