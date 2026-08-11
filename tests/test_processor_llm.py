"""Tests de l'Étape B (structuration via API OpenAI) - le client HTTP est TOUJOURS
mocké : cette suite ne fait jamais d'appel réseau réel ni ne consomme de crédits API.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import RateLimitError

import config
import processor


ANNONCE_VALIDE_BRUTE = {
    "est_une_annonce_valide": True,
    "type_bien": "parcelle",
    "quartier_zone": "ouaga 2000",  # volontairement en minuscule pour tester la normalisation
    "superficie_m2": 600,
    "prix_fcfa": 15_000_000,
    "statut_document": "Titre Foncier",
    "contacts_whatsapp": ["70123456"],
    "mots_cles_pertinents": ["parcelle", "titre foncier"],
    "resume_court": "Parcelle 600m2 à Ouaga 2000, titre foncier, 15M FCFA.",
}


class _FauxMessage:
    def __init__(self, content: str | None = None, refusal: str | None = None):
        self.content = content
        self.refusal = refusal


class _FauxChoix:
    def __init__(self, message: _FauxMessage):
        self.message = message


class _FausseReponse:
    def __init__(self, message: _FauxMessage):
        self.choices = [_FauxChoix(message)]


def _reponse_json(donnees: dict) -> _FausseReponse:
    return _FausseReponse(_FauxMessage(content=json.dumps(donnees)))


def _erreur_rate_limit() -> RateLimitError:
    requete = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    reponse = httpx.Response(status_code=429, request=requete)
    return RateLimitError("rate limited (test)", response=reponse, body=None)


@pytest.fixture(autouse=True)
def pas_de_vraie_attente(monkeypatch):
    """Neutralise les pauses de backoff pour garder la suite rapide."""
    monkeypatch.setattr(config, "LLM_BACKOFF_BASE_S", 0.001)

    async def _sleep_instantane(_delai):
        return None

    monkeypatch.setattr(processor.asyncio, "sleep", _sleep_instantane)


class TestStructurerAnnonce:
    async def test_reponse_valide_est_parsee_et_normalisee(self):
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=_reponse_json(ANNONCE_VALIDE_BRUTE))
        semaphore = asyncio.Semaphore(1)

        resultat = await processor.structurer_annonce(client, "texte de test", semaphore)

        assert resultat is not None
        assert resultat["est_une_annonce_valide"] is True
        assert resultat["superficie_m2"] == 600
        client.chat.completions.create.assert_awaited_once()

    async def test_refus_du_modele_retourne_none(self):
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            return_value=_FausseReponse(_FauxMessage(refusal="contenu jugé sensible"))
        )
        semaphore = asyncio.Semaphore(1)

        resultat = await processor.structurer_annonce(client, "texte de test", semaphore)

        assert resultat is None

    async def test_contenu_vide_retourne_none(self):
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=_FausseReponse(_FauxMessage(content=None)))
        semaphore = asyncio.Semaphore(1)

        resultat = await processor.structurer_annonce(client, "texte de test", semaphore)

        assert resultat is None

    async def test_json_invalide_retourne_none_sans_retry(self):
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            return_value=_FausseReponse(_FauxMessage(content="{ceci n'est pas du json"))
        )
        semaphore = asyncio.Semaphore(1)

        resultat = await processor.structurer_annonce(client, "texte de test", semaphore)

        assert resultat is None
        client.chat.completions.create.assert_awaited_once()  # pas de retry sur JSON malformé

    async def test_schema_invalide_retourne_none_sans_retry(self):
        client = AsyncMock()
        entree_invalide = {**ANNONCE_VALIDE_BRUTE, "est_une_annonce_valide": "pas_un_booleen"}
        client.chat.completions.create = AsyncMock(return_value=_reponse_json(entree_invalide))
        semaphore = asyncio.Semaphore(1)

        resultat = await processor.structurer_annonce(client, "texte de test", semaphore)

        assert resultat is None
        client.chat.completions.create.assert_awaited_once()  # pas de retry sur erreur de schéma

    async def test_rate_limit_puis_succes_retente(self):
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[_erreur_rate_limit(), _reponse_json(ANNONCE_VALIDE_BRUTE)]
        )
        semaphore = asyncio.Semaphore(1)

        resultat = await processor.structurer_annonce(client, "texte de test", semaphore, max_retries=3)

        assert resultat is not None
        assert client.chat.completions.create.await_count == 2

    async def test_echec_persistant_retourne_none_apres_max_retries(self):
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=_erreur_rate_limit())
        semaphore = asyncio.Semaphore(1)

        resultat = await processor.structurer_annonce(client, "texte de test", semaphore, max_retries=2)

        assert resultat is None
        assert client.chat.completions.create.await_count == 2

    async def test_valeurs_negatives_sont_mises_a_null(self):
        client = AsyncMock()
        entree = {**ANNONCE_VALIDE_BRUTE, "prix_fcfa": -100}
        client.chat.completions.create = AsyncMock(return_value=_reponse_json(entree))
        semaphore = asyncio.Semaphore(1)

        resultat = await processor.structurer_annonce(client, "texte de test", semaphore)

        assert resultat["prix_fcfa"] is None


class TestStructurerLot:
    async def test_liste_vide_ne_construit_pas_de_client(self, monkeypatch):
        appelle = {"valeur": False}

        def _client_espion(*_a, **_k):
            appelle["valeur"] = True
            raise AssertionError("ne devrait pas être appelé pour une liste vide")

        monkeypatch.setattr(processor, "_construire_client", _client_espion)
        valides, non_valides = await processor.structurer_lot([], api_key="cle-test")
        assert valides == [] and non_valides == []
        assert appelle["valeur"] is False

    async def test_separe_valides_et_non_valides(self, monkeypatch):
        candidats = [
            {"id": "p1", "texte_nettoye": "annonce 1"},
            {"id": "p2", "texte_nettoye": "annonce 2"},
        ]

        async def _fausse_structuration(_client, texte, _sem, max_retries=3):
            if texte == "annonce 1":
                return dict(ANNONCE_VALIDE_BRUTE)
            return None  # échec simulé pour le 2e post

        monkeypatch.setattr(processor, "_construire_client", lambda *_a, **_k: AsyncMock())
        monkeypatch.setattr(processor, "structurer_annonce", _fausse_structuration)

        valides, non_valides = await processor.structurer_lot(candidats, api_key="cle-test")

        assert len(valides) == 1 and valides[0]["id"] == "p1"
        assert len(non_valides) == 1 and non_valides[0]["id"] == "p2"
        # La normalisation du quartier doit avoir été appliquée sur le résultat valide.
        assert valides[0]["quartier_zone"] == "Ouaga 2000"
        # Idem pour statut_document (régression : cet appel manquait, voir processor.py).
        assert valides[0]["statut_document"] == "Titre foncier"

    async def test_llm_juge_invalide_va_dans_non_valides(self, monkeypatch):
        candidats = [{"id": "p1", "texte_nettoye": "annonce suspecte"}]

        async def _fausse_structuration(_client, _texte, _sem, max_retries=3):
            return {**ANNONCE_VALIDE_BRUTE, "est_une_annonce_valide": False}

        monkeypatch.setattr(processor, "_construire_client", lambda *_a, **_k: AsyncMock())
        monkeypatch.setattr(processor, "structurer_annonce", _fausse_structuration)

        valides, non_valides = await processor.structurer_lot(candidats, api_key="cle-test")

        assert valides == []
        assert non_valides[0]["motif_rejet"] == "llm_juge_invalide"


class TestAnnonceStructureeValidation:
    """Validateurs Pydantic de AnnonceStructuree - purement Python, pas besoin
    de DB ni de réseau. Couvre le bug corrigé le 2026-08-11 (voir
    upsert_annonces) : une valeur de prix/superficie dépassant les bornes
    INTEGER de Postgres faisait planter tout un batch d'upsert avec
    `NumericValueOutOfRange`. Le validateur doit désormais neutraliser ces
    valeurs (mise à null) AVANT qu'elles n'atteignent la base.
    """

    def _construire(self, **overrides):
        base = dict(
            est_une_annonce_valide=True,
            type_bien="parcelle",
            superficie_m2=600,
            prix_fcfa=15_000_000,
        )
        base.update(overrides)
        return processor.AnnonceStructuree(**base)

    def test_valeur_normale_conservee(self):
        a = self._construire(prix_fcfa=15_000_000, superficie_m2=600)
        assert a.prix_fcfa == 15_000_000
        assert a.superficie_m2 == 600

    def test_valeur_negative_mise_a_null(self):
        a = self._construire(prix_fcfa=-5, superficie_m2=-1)
        assert a.prix_fcfa is None
        assert a.superficie_m2 is None

    def test_valeur_depassant_integer_postgres_mise_a_null(self):
        # processor.POSTGRES_INTEGER_MAX = 2_147_483_647 (INTEGER Postgres,
        # signé 4 octets) - une valeur au-delà ferait planter l'upsert.
        trop_grand = processor.POSTGRES_INTEGER_MAX + 1
        a = self._construire(prix_fcfa=trop_grand, superficie_m2=trop_grand)
        assert a.prix_fcfa is None
        assert a.superficie_m2 is None

    def test_valeur_a_la_borne_exacte_conservee(self):
        # Cas limite : la borne elle-même doit rester une valeur INTEGER
        # valide côté Postgres, donc conservée (pas de rejet trop agressif).
        borne = processor.POSTGRES_INTEGER_MAX
        a = self._construire(prix_fcfa=borne, superficie_m2=borne)
        assert a.prix_fcfa == borne
        assert a.superficie_m2 == borne

    def test_valeur_null_conservee(self):
        a = self._construire(prix_fcfa=None, superficie_m2=None)
        assert a.prix_fcfa is None
        assert a.superficie_m2 is None


class TestConstruireClient:
    def test_leve_erreur_si_cle_absente(self, monkeypatch):
        monkeypatch.delenv(config.ENV_OPENAI_KEY, raising=False)
        with pytest.raises(ValueError):
            processor._construire_client(api_key=None)

    def test_utilise_la_cle_fournie_en_priorite(self, monkeypatch):
        monkeypatch.setenv(config.ENV_OPENAI_KEY, "cle-env")
        client = processor._construire_client(api_key="cle-explicite")
        assert client.api_key == "cle-explicite"
