"""Tests des fonctions pures/synchrones de scraper.py.

Les fonctions qui pilotent un vrai navigateur (creer_navigateur, scraper_groupe)
nécessitent une session Playwright/Chromium et ne sont PAS couvertes ici - voir
la limite documentée dans README.md et le module docstring de scraper.py (le
scroll + la capture réseau GraphQL de `scraper_groupe` n'ont jamais été
vérifiés en conditions réelles). On teste tout ce qui peut l'être sans
navigateur : parsing, validation, persistance - y compris le parseur JSON des
stories Comet (`extraire_stories_depuis_json` et fonctions associées), sur des
fixtures SYNTHÉTIQUES construites pour reproduire la structure réelle
découverte (jamais de données personnelles réelles dans ce fichier).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

import config
import scraper


class TestChargerCookies:
    def test_cookies_valides_sont_acceptes(self):
        brut = json.dumps([
            {"name": "c_user", "value": "123", "domain": ".facebook.com"},
            {"name": "xs", "value": "abc", "domain": ".facebook.com"},
        ])
        cookies = scraper.charger_cookies(brut)
        assert len(cookies) == 2
        assert cookies[0]["path"] == "/"  # valeur par défaut ajoutée

    def test_json_invalide_leve_value_error(self):
        with pytest.raises(ValueError):
            scraper.charger_cookies("{ceci n'est pas du json")

    def test_liste_vide_leve_value_error(self):
        with pytest.raises(ValueError):
            scraper.charger_cookies("[]")

    def test_objet_au_lieu_de_liste_leve_value_error(self):
        with pytest.raises(ValueError):
            scraper.charger_cookies(json.dumps({"name": "c_user"}))

    def test_champ_requis_manquant_leve_value_error(self):
        brut = json.dumps([{"name": "c_user"}])  # "value" et "domain" manquants
        with pytest.raises(ValueError):
            scraper.charger_cookies(brut)

    def test_export_extension_navigateur_est_converti_au_format_playwright(self):
        # Format réel d'un export d'extension de navigateur (chrome.cookies) :
        # expirationDate au lieu de expires, sameSite en minuscules avec des
        # valeurs hors de l'enum Playwright, clés inconnues de Playwright.
        # Valeurs synthétiques - jamais de vrai cookie de session dans les tests.
        brut = json.dumps([
            {
                "domain": ".facebook.com", "expirationDate": 1999999999.5,
                "hostOnly": False, "httpOnly": True, "name": "xs", "path": "/",
                "sameSite": "no_restriction", "secure": True, "session": False,
                "storeId": None, "value": "test_xs_value",
            },
            {
                "domain": ".facebook.com", "expirationDate": 1999999999.0,
                "hostOnly": False, "httpOnly": False, "name": "c_user", "path": "/",
                "sameSite": "lax", "secure": True, "session": False,
                "storeId": None, "value": "test_c_user_value",
            },
            {
                # cookie de session : pas d'expirationDate, sameSite absent
                "domain": ".facebook.com", "hostOnly": False, "httpOnly": False,
                "name": "presence", "path": "/", "sameSite": None, "secure": True,
                "session": True, "storeId": None, "value": "test_presence_value",
            },
        ])
        cookies = scraper.charger_cookies(brut)
        par_nom = {c["name"]: c for c in cookies}

        # Clés non reconnues par Playwright supprimées.
        for c in cookies:
            assert set(c).issubset({"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"})

        assert par_nom["xs"]["sameSite"] == "None"  # no_restriction -> None
        assert par_nom["xs"]["expires"] == 1999999999.5  # expirationDate -> expires
        assert par_nom["c_user"]["sameSite"] == "Lax"  # lax -> Lax

        assert "expires" not in par_nom["presence"]  # cookie de session : pas de date inventée
        assert "sameSite" not in par_nom["presence"]  # valeur absente : pas de défaut inventé

    def test_sameSite_non_reconnu_est_ignore_sans_lever_derreur(self):
        brut = json.dumps([
            {"name": "c_user", "value": "v", "domain": ".facebook.com", "sameSite": "valeur_inconnue"},
        ])
        cookies = scraper.charger_cookies(brut)
        assert "sameSite" not in cookies[0]

    def test_format_playwright_natif_reste_accepte(self):
        # Rétrocompatibilité : un cookie déjà au format Playwright (expires,
        # sameSite en PascalCase) doit passer sans être altéré.
        brut = json.dumps([
            {"name": "c_user", "value": "v", "domain": ".facebook.com",
             "path": "/", "expires": 1999999999.0, "sameSite": "Strict"},
        ])
        cookies = scraper.charger_cookies(brut)
        assert cookies[0]["expires"] == 1999999999.0
        assert cookies[0]["sameSite"] == "Strict"


def _story_synthetique(
    id_: str = "story_abc123",
    url: str = "https://web.facebook.com/groups/589699498704633/permalink/111/",
    texte: str = "Terrain de 300m2 a vendre a Ouagadougou.",
    creation_time: int | None = 1_753_000_000,
) -> dict:
    """Construit un noeud "story" Comet synthétique, avec la même forme de
    nesting que l'échantillon réel analysé (message.text et creation_time à
    des profondeurs différentes, voir docstring de extraire_stories_depuis_json)
    - mais entièrement fabriqué, aucune donnée personnelle réelle.
    """
    story: dict = {
        "id": id_,
        "url": url,
        "encrypted_tracking": "tracking_synthetique",
        "viewability_config": {},
        "client_view_config": {},
        "feedback": {"id": "feedback_synthetique"},
        "comet_sections": {
            "content": {
                "story": {
                    "comet_sections": {
                        "message_container": {
                            "story": {"message": {"text": texte}}
                        }
                    }
                }
            },
        },
    }
    if creation_time is not None:
        story["comet_sections"]["context_layout"] = {
            "story": {
                "comet_sections": {
                    "metadata": [{"story": {"creation_time": creation_time}}]
                }
            }
        }
    return story


def _story_synthetique_fil_normal(
    id_: str = "story_normal_456",
    post_id_groupe: str = "999999",
    post_id: str = "111222333",
    texte: str = "Deux parcelles a vendre pres du marche.",
    creation_time: int = 1_753_100_000,
    url_profil_auteur: str = "https://www.facebook.com/un.profil.synthetique",
) -> dict:
    """Construit un noeud "story" de FIL NORMAL (pas "mis en avant"), avec le
    nesting `attached_story` observé sur un vrai post capturé par scroll le
    2026-08-01 (voir `_extraire_url_story`) - structure DIFFÉRENTE de
    `_story_synthetique` (posts "mis en avant") : le noeud externe porte
    id + creation_time, mais comet_sections/url/texte réels vivent un niveau
    plus bas, dans un sous-objet `attached_story` qui a SA PROPRE clé `id` et
    `comet_sections`. Contient aussi un piège volontaire (URL de profil
    d'auteur) pour vérifier que `_extraire_url_story` ne s'y trompe pas.
    Entièrement fabriqué, aucune donnée personnelle réelle.
    """
    url_post = f"https://www.facebook.com/groups/{post_id_groupe}/posts/{post_id}/"
    return {
        "id": id_,
        "post_id": post_id,
        "creation_time": creation_time,
        "feedback": {
            "owning_profile": {"url": url_profil_auteur, "name": "Auteur Synthetique"}
        },
        "attached_story": {
            "id": f"{id_}_attached",
            "comet_sections": {
                "content": {
                    "story": {
                        "comet_sections": {
                            "message_container": {
                                "story": {"message": {"text": texte}}
                            }
                        }
                    }
                },
                "context_layout": {
                    "story": {
                        "comet_sections": {
                            "metadata": [
                                {"story": {"creation_time": creation_time, "url": url_post}}
                            ]
                        }
                    }
                },
            },
        },
    }


class TestEstNoeudStory:
    def test_noeud_avec_id_url_comet_sections_est_reconnu(self):
        assert scraper._est_noeud_story(_story_synthetique()) is True

    def test_noeud_avec_id_et_comet_sections_sans_url_est_reconnu(self):
        # Structure "fil normal" (voir _story_synthetique_fil_normal) : url
        # absente au même niveau que id+comet_sections - doit quand même être
        # identifié comme un noeud story (url extraite séparément, voir
        # TestExtraireUrlStory). Régression du bug réel du 2026-08-01.
        obj = {"id": "x", "comet_sections": {}}
        assert scraper._est_noeud_story(obj) is True

    def test_dict_sans_comet_sections_est_rejete(self):
        assert scraper._est_noeud_story({"id": "x", "url": "https://x"}) is False

    def test_dict_avec_id_non_str_est_rejete(self):
        obj = {"id": 123, "url": "https://x", "comet_sections": {}}
        assert scraper._est_noeud_story(obj) is False

    def test_non_dict_est_rejete(self):
        assert scraper._est_noeud_story(["id", "url", "comet_sections"]) is False
        assert scraper._est_noeud_story(None) is False


class TestChercherValeurImbriquee:
    def test_trouve_une_valeur_profondement_imbriquee(self):
        obj = {"a": {"b": {"c": [1, 2, {"cible": True}]}}}
        resultat = scraper._chercher_valeur_imbriquee(
            obj, lambda v: isinstance(v, dict) and v.get("cible") is True
        )
        assert resultat == {"cible": True}

    def test_retourne_none_si_absent(self):
        obj = {"a": {"b": 1}}
        assert scraper._chercher_valeur_imbriquee(obj, lambda v: v == "introuvable") is None

    def test_respecte_la_profondeur_max(self):
        # imbrication de 5 niveaux, mais profondeur_max=1 -> ne doit pas être trouvé
        obj = {"n1": {"n2": {"n3": {"n4": {"cible": True}}}}}
        resultat = scraper._chercher_valeur_imbriquee(
            obj, lambda v: isinstance(v, dict) and v.get("cible") is True, profondeur_max=1
        )
        assert resultat is None


class TestExtraireTexteStory:
    def test_extrait_le_texte_du_message(self):
        story = _story_synthetique(texte="Deux parcelles a Bassinko.")
        assert scraper._extraire_texte_story(story) == "Deux parcelles a Bassinko."

    def test_retourne_none_si_texte_absent(self):
        story = {"id": "x", "url": "https://x", "comet_sections": {}}
        assert scraper._extraire_texte_story(story) is None

    def test_ignore_un_message_texte_vide(self):
        story = _story_synthetique(texte="   ")
        assert scraper._extraire_texte_story(story) is None


class TestExtraireUrlStory:
    def test_extrait_lurl_co_localisee_avec_id_et_comet_sections(self):
        story = _story_synthetique(url="https://web.facebook.com/groups/1/permalink/2/")
        assert scraper._extraire_url_story(story) == "https://web.facebook.com/groups/1/permalink/2/"

    def test_extrait_lurl_imbriquee_dans_attached_story(self):
        # Régression du bug réel du 2026-08-01 : url absente du noeud
        # externe, imbriquée dans attached_story.comet_sections....story.url.
        story = _story_synthetique_fil_normal(post_id_groupe="42", post_id="777")
        assert scraper._extraire_url_story(story) == "https://www.facebook.com/groups/42/posts/777/"

    def test_ne_confond_pas_une_url_de_profil_avec_le_permalien(self):
        # L'URL de profil d'auteur (sans /posts/ ni /permalink/) apparaît
        # AVANT le vrai permalien lors du parcours en largeur - ne doit pas
        # être retournée à sa place.
        story = _story_synthetique_fil_normal(url_profil_auteur="https://www.facebook.com/quelquun")
        resultat = scraper._extraire_url_story(story)
        assert resultat is not None
        assert "/posts/" in resultat or "/permalink/" in resultat

    def test_retourne_none_si_aucune_url_de_post_trouvee(self):
        story = {"id": "x", "comet_sections": {"auteur": {"url": "https://www.facebook.com/quelquun"}}}
        assert scraper._extraire_url_story(story) is None


class TestExtraireCreationTimeStory:
    def test_extrait_et_convertit_le_timestamp_unix(self):
        story = _story_synthetique(creation_time=1_753_000_000)
        resultat = scraper._extraire_creation_time_story(story)
        assert resultat == datetime.fromtimestamp(1_753_000_000, tz=timezone.utc)

    def test_retourne_none_si_absent(self):
        story = _story_synthetique(creation_time=None)
        assert scraper._extraire_creation_time_story(story) is None

    def test_ignore_un_booleen_meme_si_techniquement_un_int(self):
        # En Python, bool est une sous-classe d'int - piège classique, doit
        # être explicitement exclu (voir garde `not isinstance(..., bool)`).
        story = {
            "id": "x", "url": "https://x",
            "comet_sections": {"creation_time": True},
        }
        assert scraper._extraire_creation_time_story(story) is None


class TestExtraireStoriesDepuisJson:
    def test_extrait_une_story_valide(self):
        payload = {"data": {"group": {"story": _story_synthetique(id_="s1")}}}
        posts = scraper.extraire_stories_depuis_json(payload, "grp1", "Groupe Test")
        assert len(posts) == 1
        assert posts[0]["id"] == "s1"
        assert posts[0]["groupe_id"] == "grp1"
        assert posts[0]["groupe_nom"] == "Groupe Test"
        assert posts[0]["texte"] == "Terrain de 300m2 a vendre a Ouagadougou."
        assert posts[0]["date_incertaine"] is False
        assert posts[0]["date_publication"] is not None

    def test_ignore_une_story_sans_texte(self):
        story = _story_synthetique(id_="s2")
        story["comet_sections"]["content"]["story"]["comet_sections"]["message_container"]["story"]["message"]["text"] = ""
        payload = {"root": story}
        posts = scraper.extraire_stories_depuis_json(payload, "grp1", "Groupe Test")
        assert posts == []

    def test_deduplique_par_id_dans_un_meme_payload(self):
        story = _story_synthetique(id_="s3")
        payload = {"edges": [{"node": story}, {"autre": story}]}
        posts = scraper.extraire_stories_depuis_json(payload, "grp1", "Groupe Test")
        assert len(posts) == 1

    def test_plusieurs_stories_distinctes(self):
        payload = {
            "edges": [
                {"node": _story_synthetique(id_="s4", texte="Annonce A")},
                {"node": _story_synthetique(id_="s5", texte="Annonce B")},
            ]
        }
        posts = scraper.extraire_stories_depuis_json(payload, "grp1", "Groupe Test")
        assert {p["id"] for p in posts} == {"s4", "s5"}

    def test_date_incertaine_si_creation_time_absent(self):
        story = _story_synthetique(id_="s6", creation_time=None)
        payload = {"root": story}
        posts = scraper.extraire_stories_depuis_json(payload, "grp1", "Groupe Test")
        assert posts[0]["date_incertaine"] is True
        assert posts[0]["date_publication"] is None

    def test_payload_non_dict_ni_liste_ne_leve_pas(self):
        assert scraper.extraire_stories_depuis_json("juste une chaine", "grp1", "Groupe Test") == []
        assert scraper.extraire_stories_depuis_json(None, "grp1", "Groupe Test") == []

    def test_extrait_une_story_de_fil_normal_type_attached_story(self):
        # RÉGRESSION du bug réel du 2026-08-01 (2 groupes sur 5 revenaient à
        # 0 post en CI) : les stories du fil normal (capturées par scroll,
        # structure attached_story) n'étaient pas détectées avant l'ajout de
        # _extraire_url_story - _est_noeud_story exigeait url co-localisée
        # avec id+comet_sections, ce qui n'est vrai que pour les posts "mis
        # en avant", pas pour le fil normal. Voir _story_synthetique_fil_normal.
        story = _story_synthetique_fil_normal(
            id_="wrapper_789", post_id_groupe="42", post_id="777", texte="Villa a louer."
        )
        payload = {"path": ["group", "group_feed", "edges", 1], "data": {"node": story}}
        posts = scraper.extraire_stories_depuis_json(payload, "42", "Groupe Test")
        assert len(posts) == 1
        # id retenu = celui d'attached_story (le post réel), pas celui du
        # noeud "wrapper" externe.
        assert posts[0]["id"] == "wrapper_789_attached"
        assert posts[0]["texte"] == "Villa a louer."
        assert posts[0]["url"] == "https://www.facebook.com/groups/42/posts/777/"
        assert posts[0]["date_incertaine"] is False


class TestExtraireStoriesDepuisScriptsJson:
    def test_extrait_depuis_un_bloc_script_json_valide(self):
        story = _story_synthetique(id_="s7")
        payload = json.dumps({"data": {"group": story}})
        html = (
            "<html><body>"
            f'<script type="application/json" data-sjs>{payload}</script>'
            "</body></html>"
        )
        posts = scraper._extraire_stories_depuis_scripts_json(html, "grp1", "Groupe Test")
        assert len(posts) == 1
        assert posts[0]["id"] == "s7"

    def test_ignore_un_bloc_script_json_invalide(self):
        html = '<script type="application/json">{ceci n est pas du json}</script>'
        posts = scraper._extraire_stories_depuis_scripts_json(html, "grp1", "Groupe Test")
        assert posts == []

    def test_deduplique_entre_plusieurs_blocs_script(self):
        story = _story_synthetique(id_="s8")
        payload = json.dumps({"root": story})
        html = (
            f'<script type="application/json">{payload}</script>'
            f'<script type="application/json">{payload}</script>'
        )
        posts = scraper._extraire_stories_depuis_scripts_json(html, "grp1", "Groupe Test")
        assert len(posts) == 1

    def test_html_sans_script_json_retourne_liste_vide(self):
        html = "<html><body><p>Rien ici.</p></body></html>"
        assert scraper._extraire_stories_depuis_scripts_json(html, "grp1", "Groupe Test") == []


class _FausseLocator:
    """Simule `page.locator(...)` (uniquement `.count()`, seul appel utilisé
    par `detecter_blocage_ou_session_expiree`)."""

    def __init__(self, count: int = 0):
        self._count = count

    async def count(self):
        return self._count


class _FaussePage:
    """Simule les 3 accès Playwright utilisés par
    `detecter_blocage_ou_session_expiree` (url, content(), locator(...).count()) -
    évite d'avoir besoin d'un vrai navigateur pour tester cette logique pure.
    """

    def __init__(
        self,
        url: str = "https://web.facebook.com/groups/1/",
        contenu: str = "<html><body>GroupsCometFeed contenu normal</body></html>",
        nb_mur_connexion: int = 0,
    ):
        self.url = url
        self._contenu = contenu
        self._nb_mur_connexion = nb_mur_connexion

    async def content(self):
        return self._contenu

    def locator(self, _selecteur):
        return _FausseLocator(self._nb_mur_connexion)


class TestDetecterBlocageOuSessionExpiree:
    async def test_page_saine_ne_leve_rien(self):
        page = _FaussePage()
        await scraper.detecter_blocage_ou_session_expiree(page)  # ne doit pas lever

    async def test_url_de_checkpoint_leve_blocage_detecte(self):
        page = _FaussePage(url="https://www.facebook.com/checkpoint/123/")
        with pytest.raises(scraper.BlocageDetecteError):
            await scraper.detecter_blocage_ou_session_expiree(page)

    async def test_texte_de_verification_leve_blocage_detecte(self):
        page = _FaussePage(contenu="<html>Nous voulons juste vérifier que c'est bien vous.</html>")
        with pytest.raises(scraper.BlocageDetecteError):
            await scraper.detecter_blocage_ou_session_expiree(page)

    async def test_user_id_zero_leve_session_expiree(self):
        # RÉGRESSION du bug réel du 2026-08-01 (run 30715788089) : 5 groupes
        # revenus à 0 post sans qu'aucune erreur ne soit levée - la page
        # Comet déconnectée ne redirige pas l'URL et n'affiche aucun texte de
        # vérification, juste USER_ID/actorID à "0" (confirmé sur le dump
        # HTML réel).
        page = _FaussePage(contenu='<html><script>{"USER_ID":"0","actorID":"0"}</script></html>')
        with pytest.raises(scraper.SessionExpireeError):
            await scraper.detecter_blocage_ou_session_expiree(page)

    async def test_user_id_non_nul_ne_leve_rien(self):
        page = _FaussePage(contenu='<html><script>{"USER_ID":"61592012785019"}</script></html>')
        await scraper.detecter_blocage_ou_session_expiree(page)  # ne doit pas lever

    async def test_mur_connexion_dom_leve_session_expiree(self):
        page = _FaussePage(nb_mur_connexion=1)
        with pytest.raises(scraper.SessionExpireeError):
            await scraper.detecter_blocage_ou_session_expiree(page)


class TestSeenIds:
    def test_charger_sans_fichier_retourne_dict_vide(self, repertoires_isoles):
        assert scraper.charger_seen_ids() == {}

    def test_sauvegarder_puis_charger_roundtrip(self, repertoires_isoles):
        maintenant = datetime.now(timezone.utc).isoformat()
        scraper.sauvegarder_seen_ids({"p1": maintenant, "p2": maintenant})
        recharge = scraper.charger_seen_ids()
        assert set(recharge) == {"p1", "p2"}

    def test_purge_les_entrees_trop_anciennes(self, repertoires_isoles):
        recent = datetime.now(timezone.utc).isoformat()
        ancien = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        scraper.sauvegarder_seen_ids({"recent": recent, "ancien": ancien}, retention_jours=90)
        recharge = scraper.charger_seen_ids()
        assert "recent" in recharge
        assert "ancien" not in recharge

    def test_fichier_corrompu_retourne_dict_vide_sans_planter(self, repertoires_isoles):
        config.SEEN_IDS_PATH.write_text("{pas du json valide", encoding="utf-8")
        assert scraper.charger_seen_ids() == {}


class TestRotationBackfill:
    """Mécanisme de reprise entre runs backfill (2026-08-07, voir config.py -
    ROTATION_BACKFILL_PATH) : sans lui, group_limit retombait toujours sur
    les mêmes premiers groupes de groups.csv d'un run manuel à l'autre.
    """

    def _groupe(self, id_: str) -> config.Groupe:
        return config.Groupe(id=id_, nom=f"Groupe {id_}", url=f"https://x/{id_}/")

    def test_charger_sans_fichier_retourne_none(self, repertoires_isoles):
        assert scraper.charger_rotation_backfill() is None

    def test_sauvegarder_puis_charger_roundtrip(self, repertoires_isoles):
        scraper.sauvegarder_rotation_backfill("42")
        assert scraper.charger_rotation_backfill() == "42"

    def test_fichier_corrompu_retourne_none_sans_planter(self, repertoires_isoles):
        config.ROTATION_BACKFILL_PATH.write_text("{pas du json", encoding="utf-8")
        assert scraper.charger_rotation_backfill() is None

    def test_rotation_demarre_apres_le_dernier_traite(self):
        groupes = [self._groupe(str(i)) for i in range(1, 6)]  # 1..5
        resultat = scraper.appliquer_rotation_backfill(groupes, dernier_id="2")
        assert [g.id for g in resultat] == ["3", "4", "5", "1", "2"]

    def test_rotation_reboucle_sur_le_dernier_groupe_de_la_liste(self):
        # Le run précédent s'est arrêté sur le tout dernier groupe -> on
        # reboucle intégralement sur la liste (round-robin complet).
        groupes = [self._groupe(str(i)) for i in range(1, 4)]  # 1..3
        resultat = scraper.appliquer_rotation_backfill(groupes, dernier_id="3")
        assert [g.id for g in resultat] == ["1", "2", "3"]

    def test_rotation_sans_dernier_id_retourne_liste_inchangee(self):
        groupes = [self._groupe(str(i)) for i in range(1, 4)]
        resultat = scraper.appliquer_rotation_backfill(groupes, dernier_id=None)
        assert resultat == groupes

    def test_rotation_id_absent_de_la_liste_retourne_liste_inchangee(self):
        # Groupe désactivé/retiré de groups.csv entre deux runs - repli sûr :
        # on ne plante pas, on repart du début plutôt que de deviner.
        groupes = [self._groupe(str(i)) for i in range(1, 4)]
        resultat = scraper.appliquer_rotation_backfill(groupes, dernier_id="999")
        assert resultat == groupes


class _FauxCurseurDB:
    def __init__(self, lignes):
        self._lignes = lignes

    def execute(self, *_args, **_kwargs):
        pass

    def fetchall(self):
        return self._lignes

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FausseConnexionDB:
    def __init__(self, lignes):
        self._lignes = lignes

    def cursor(self):
        return _FauxCurseurDB(self._lignes)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TestRecupererProfondeurActuelleParGroupe:
    """Priorisation du backfill par profondeur DB (2026-08-13, voir
    prioriser_groupes_backfill) : lecture best-effort, ne doit jamais faire
    planter un run de scraping même si la DB est absente/injoignable.
    """

    def test_database_url_absente_retourne_dict_vide(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "")
        assert scraper.recuperer_profondeur_actuelle_par_groupe() == {}

    def test_erreur_psycopg_retourne_dict_vide_sans_planter(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "postgresql://x/y")

        def _connect_qui_echoue(*_args, **_kwargs):
            raise psycopg.OperationalError("base injoignable (test)")

        monkeypatch.setattr(scraper.psycopg, "connect", _connect_qui_echoue)
        assert scraper.recuperer_profondeur_actuelle_par_groupe() == {}

    def test_cas_nominal_calcule_profondeur_et_densite(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "postgresql://x/y")
        lignes = [
            ("Groupe A", 100, "2026-05-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
        ]
        monkeypatch.setattr(
            scraper.psycopg, "connect", lambda *_a, **_k: _FausseConnexionDB(lignes)
        )

        resultat = scraper.recuperer_profondeur_actuelle_par_groupe()

        assert set(resultat) == {"Groupe A"}
        assert resultat["Groupe A"]["plus_ancien"] == datetime(2026, 5, 1, tzinfo=timezone.utc)
        # 100 annonces / 92 jours d'écart (1er mai -> 1er août) ≈ 1.09/j
        assert resultat["Groupe A"]["densite_annonces_jour"] == pytest.approx(100 / 92, rel=0.01)

    def test_fenetre_trop_courte_donne_densite_none(self, monkeypatch):
        # Moins d'un jour d'écart entre le plus ancien et le plus récent post
        # observé - pas assez de recul pour une densité fiable.
        monkeypatch.setattr(config, "DATABASE_URL", "postgresql://x/y")
        lignes = [
            ("Groupe B", 5, "2026-08-01T10:00:00+00:00", "2026-08-01T12:00:00+00:00"),
        ]
        monkeypatch.setattr(
            scraper.psycopg, "connect", lambda *_a, **_k: _FausseConnexionDB(lignes)
        )

        resultat = scraper.recuperer_profondeur_actuelle_par_groupe()
        assert resultat["Groupe B"]["densite_annonces_jour"] is None

    def test_date_invalide_est_ignoree(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "postgresql://x/y")
        lignes = [("Groupe C", 3, None, None)]
        monkeypatch.setattr(
            scraper.psycopg, "connect", lambda *_a, **_k: _FausseConnexionDB(lignes)
        )

        assert scraper.recuperer_profondeur_actuelle_par_groupe() == {}


class TestPrioriserGroupesBackfill:
    """Tri des groupes backfill par urgence (2026-08-13) : priorise les
    groupes sous l'objectif de profondeur ET dont la densité laisse penser
    que l'objectif est réellement atteignable, au lieu de traiter tous les
    groupes à égalité (voir le diagnostic détaillé dans config.py, commentaire
    au-dessus de OBJECTIF_PROFONDEUR_BACKFILL_JOURS).
    """

    def _groupe(self, id_: str, nom: str) -> config.Groupe:
        return config.Groupe(id=id_, nom=nom, url=f"https://x/{id_}/")

    def test_dict_vide_retourne_liste_inchangee(self):
        groupes = [self._groupe("1", "A"), self._groupe("2", "B")]
        assert scraper.prioriser_groupes_backfill(groupes, {}) == groupes

    def test_groupe_jamais_backfille_passe_devant_un_groupe_dense(self):
        maintenant = datetime.now(timezone.utc)
        groupes = [
            self._groupe("1", "Dense"),
            self._groupe("2", "JamaisBackfille"),
        ]
        profondeur = {
            # Dense : encore loin de l'objectif (7j atteints sur 90), mais
            # très forte densité -> énormément de runs restants estimés.
            "Dense": {
                "plus_ancien": maintenant - timedelta(days=7),
                "densite_annonces_jour": 100.0,
            },
            # "JamaisBackfille" absent du dict -> traité en priorité maximale.
        }
        resultat = scraper.prioriser_groupes_backfill(groupes, profondeur, objectif_jours=90)
        assert [g.nom for g in resultat] == ["JamaisBackfille", "Dense"]

    def test_groupe_proche_objectif_passe_devant_groupe_dense_loin_de_lobjectif(self):
        maintenant = datetime.now(timezone.utc)
        groupes = [self._groupe("1", "Dense"), self._groupe("2", "ProcheObjectif")]
        profondeur = {
            "Dense": {
                "plus_ancien": maintenant - timedelta(days=7),
                "densite_annonces_jour": 100.0,  # très dense -> peu de progrès/run
            },
            "ProcheObjectif": {
                "plus_ancien": maintenant - timedelta(days=85),
                "densite_annonces_jour": 1.0,  # peu dense -> objectif presque atteint
            },
        }
        resultat = scraper.prioriser_groupes_backfill(groupes, profondeur, objectif_jours=90)
        assert [g.nom for g in resultat] == ["ProcheObjectif", "Dense"]

    def test_groupe_deja_a_lobjectif_relegue_en_fin(self):
        maintenant = datetime.now(timezone.utc)
        groupes = [self._groupe("1", "DejaAtteint"), self._groupe("2", "EnRetard")]
        profondeur = {
            "DejaAtteint": {
                "plus_ancien": maintenant - timedelta(days=120),
                "densite_annonces_jour": 5.0,
            },
            "EnRetard": {
                "plus_ancien": maintenant - timedelta(days=10),
                "densite_annonces_jour": 5.0,
            },
        }
        resultat = scraper.prioriser_groupes_backfill(groupes, profondeur, objectif_jours=90)
        assert [g.nom for g in resultat] == ["EnRetard", "DejaAtteint"]

    def test_densite_nulle_traitee_comme_priorite_optimiste_sans_division_par_zero(self):
        maintenant = datetime.now(timezone.utc)
        groupes = [self._groupe("1", "DensiteInconnue")]
        profondeur = {
            "DensiteInconnue": {
                "plus_ancien": maintenant - timedelta(days=5),
                "densite_annonces_jour": None,
            },
        }
        # Ne doit pas lever ZeroDivisionError.
        resultat = scraper.prioriser_groupes_backfill(groupes, profondeur, objectif_jours=90)
        assert [g.nom for g in resultat] == ["DensiteInconnue"]


class TestParserHorodatageRelatif:
    """Le parseur d'horodatage mbasic est une fonction pure - contrairement à
    l'ancienne extraction (jamais implémentée faute d'accès DOM live), elle
    est intégralement testable hors-ligne.
    """

    MAINTENANT = datetime(2026, 8, 1, 15, 0, 0, tzinfo=timezone.utc)

    def test_minutes(self):
        resultat = scraper._parser_horodatage_relatif("12 min", self.MAINTENANT)
        assert resultat == self.MAINTENANT - timedelta(minutes=12)

    def test_heures(self):
        resultat = scraper._parser_horodatage_relatif("3 h", self.MAINTENANT)
        assert resultat == self.MAINTENANT - timedelta(hours=3)

    def test_jours(self):
        resultat = scraper._parser_horodatage_relatif("5 j", self.MAINTENANT)
        assert resultat == self.MAINTENANT - timedelta(days=5)

    def test_a_linstant(self):
        assert scraper._parser_horodatage_relatif("à l'instant", self.MAINTENANT) == self.MAINTENANT

    def test_hier_sans_heure(self):
        resultat = scraper._parser_horodatage_relatif("Hier", self.MAINTENANT)
        assert resultat == datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc)

    def test_hier_avec_heure(self):
        resultat = scraper._parser_horodatage_relatif("Hier à 14:30", self.MAINTENANT)
        assert resultat == datetime(2026, 7, 31, 14, 30, tzinfo=timezone.utc)

    def test_aujourdhui_avec_heure(self):
        resultat = scraper._parser_horodatage_relatif("Aujourd'hui à 09:15", self.MAINTENANT)
        assert resultat == datetime(2026, 8, 1, 9, 15, tzinfo=timezone.utc)

    def test_date_avec_mois_sans_annee_passee(self):
        # "1 août" un 1er août à 15h -> plus tôt le même jour, pas dans le futur.
        resultat = scraper._parser_horodatage_relatif("1 août", self.MAINTENANT)
        assert resultat == datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    def test_date_sans_annee_dans_le_futur_recule_dun_an(self):
        maintenant = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        resultat = scraper._parser_horodatage_relatif("1 août", maintenant)
        assert resultat == datetime(2025, 8, 1, 12, 0, tzinfo=timezone.utc)

    def test_date_avec_annee_explicite(self):
        resultat = scraper._parser_horodatage_relatif("3 mars 2024", self.MAINTENANT)
        assert resultat == datetime(2024, 3, 3, 12, 0, tzinfo=timezone.utc)

    def test_date_avec_heure_et_annee(self):
        resultat = scraper._parser_horodatage_relatif("3 mars 2024 à 18:45", self.MAINTENANT)
        assert resultat == datetime(2024, 3, 3, 18, 45, tzinfo=timezone.utc)

    def test_date_invalide_retourne_none(self):
        assert scraper._parser_horodatage_relatif("31 février", self.MAINTENANT) is None

    def test_texte_non_reconnu_retourne_none(self):
        assert scraper._parser_horodatage_relatif("mardi prochain", self.MAINTENANT) is None

    def test_texte_vide_ou_none_retourne_none(self):
        assert scraper._parser_horodatage_relatif("", self.MAINTENANT) is None
        assert scraper._parser_horodatage_relatif(None, self.MAINTENANT) is None

    def test_insensible_a_la_casse(self):
        resultat = scraper._parser_horodatage_relatif("3 H", self.MAINTENANT)
        assert resultat == self.MAINTENANT - timedelta(hours=3)


class TestCooldown:
    def test_aucun_fichier_pas_de_cooldown(self, repertoires_isoles):
        assert scraper.verifier_cooldown() is None

    def test_cooldown_actif_est_detecte(self, repertoires_isoles):
        scraper.activer_cooldown(heures=24, raison="test")
        fin = scraper.verifier_cooldown()
        assert fin is not None
        assert fin > datetime.now(timezone.utc)

    def test_cooldown_expire_nest_plus_actif(self, repertoires_isoles):
        # Cooldown déjà terminé dans le passé -> ne doit plus bloquer.
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        passe = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        config.COOLDOWN_PATH.write_text(
            json.dumps({"jusqu_a": passe, "raison": "expiré"}), encoding="utf-8"
        )
        assert scraper.verifier_cooldown() is None

    def test_fichier_cooldown_corrompu_nest_pas_bloquant(self, repertoires_isoles):
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        config.COOLDOWN_PATH.write_text("{pas du json", encoding="utf-8")
        assert scraper.verifier_cooldown() is None


class TestThrottleAdaptatif:
    """Le throttle AIMD est composé de fonctions pures (état -> état), donc
    testable sans navigateur ni fichier - sauf charger_sante/sauvegarder_sante
    qui touchent le disque et utilisent `repertoires_isoles`.
    """

    def test_charger_sante_sans_fichier_retourne_confiance_maximale(self, repertoires_isoles):
        etat = scraper.charger_sante()
        assert etat["niveau_confiance"] == config.NIVEAU_CONFIANCE_INITIAL
        assert etat["runs_propres_consecutifs"] == 0
        assert etat["cooldown_multiplicateur"] == 1

    def test_sauvegarder_puis_charger_roundtrip(self, repertoires_isoles):
        scraper.sauvegarder_sante({"niveau_confiance": 0.5, "runs_propres_consecutifs": 2, "cooldown_multiplicateur": 4})
        etat = scraper.charger_sante()
        assert etat["niveau_confiance"] == 0.5
        assert etat["runs_propres_consecutifs"] == 2
        assert etat["cooldown_multiplicateur"] == 4

    def test_charger_sante_fichier_corrompu_retourne_defaut(self, repertoires_isoles):
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        config.SANTE_PATH.write_text("{pas du json", encoding="utf-8")
        etat = scraper.charger_sante()
        assert etat["niveau_confiance"] == config.NIVEAU_CONFIANCE_INITIAL

    def test_ajustements_confiance_maximale(self):
        ajustements = scraper.calculer_ajustements({"niveau_confiance": 1.0})
        assert ajustements.delai_multiplicateur == 1.0
        assert ajustements.ratio_groupes == 1.0

    def test_ajustements_confiance_reduite_rallonge_delais_et_reduit_volume(self):
        ajustements = scraper.calculer_ajustements({"niveau_confiance": 0.5})
        assert ajustements.delai_multiplicateur == 2.0
        assert ajustements.ratio_groupes == 0.5

    def test_ajustements_bornes_respectees(self):
        # Valeur hors bornes dans le fichier (corruption/édition manuelle) -> clampée.
        bas = scraper.calculer_ajustements({"niveau_confiance": 0.0})
        assert bas.ratio_groupes == config.NIVEAU_CONFIANCE_MIN
        haut = scraper.calculer_ajustements({"niveau_confiance": 5.0})
        assert haut.ratio_groupes == config.NIVEAU_CONFIANCE_MAX

    def test_blocage_fait_chuter_la_confiance_au_plancher(self):
        etat = {"niveau_confiance": 1.0, "runs_propres_consecutifs": 2, "cooldown_multiplicateur": 1}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5, bloque=True)
        assert nouvel_etat["niveau_confiance"] == config.NIVEAU_CONFIANCE_MIN
        assert nouvel_etat["runs_propres_consecutifs"] == 0

    def test_blocage_double_le_multiplicateur_de_cooldown(self):
        etat = {"niveau_confiance": 1.0, "runs_propres_consecutifs": 0, "cooldown_multiplicateur": 2}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5, bloque=True)
        assert nouvel_etat["cooldown_multiplicateur"] == 4

    def test_cooldown_multiplicateur_plafonne(self):
        etat = {"niveau_confiance": 1.0, "runs_propres_consecutifs": 0, "cooldown_multiplicateur": config.COOLDOWN_MULTIPLICATEUR_MAX}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5, bloque=True)
        assert nouvel_etat["cooldown_multiplicateur"] == config.COOLDOWN_MULTIPLICATEUR_MAX

    def test_session_expiree_reduit_la_confiance_moins_durement_quun_blocage(self):
        etat = {"niveau_confiance": 1.0, "runs_propres_consecutifs": 0, "cooldown_multiplicateur": 1}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5, session_expiree=True)
        assert config.NIVEAU_CONFIANCE_MIN < nouvel_etat["niveau_confiance"] < 1.0

    def test_beaucoup_danomalies_declenche_une_suspicion(self):
        etat = {"niveau_confiance": 1.0, "runs_propres_consecutifs": 0, "cooldown_multiplicateur": 1}
        # 4 anomalies sur 5 groupes = 80% > seuil de 30%
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=4, total_groupes=5)
        assert nouvel_etat["niveau_confiance"] < 1.0
        assert nouvel_etat["runs_propres_consecutifs"] == 0

    def test_peu_danomalies_ne_declenche_rien_et_compte_le_run_propre(self):
        etat = {"niveau_confiance": 0.8, "runs_propres_consecutifs": 0, "cooldown_multiplicateur": 1}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5)
        assert nouvel_etat["niveau_confiance"] == 0.8  # pas encore de ramp-up
        assert nouvel_etat["runs_propres_consecutifs"] == 1

    def test_runs_propres_consecutifs_font_remonter_la_confiance(self):
        etat = {"niveau_confiance": 0.5, "runs_propres_consecutifs": config.RUNS_PROPRES_POUR_RAMPUP - 1, "cooldown_multiplicateur": 3}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5)
        assert nouvel_etat["niveau_confiance"] == pytest.approx(0.5 + config.RAMPUP_INCREMENT)
        assert nouvel_etat["runs_propres_consecutifs"] == 0
        assert nouvel_etat["cooldown_multiplicateur"] == 1  # reset après un vrai streak propre

    def test_ramp_up_ne_depasse_jamais_le_maximum(self):
        etat = {
            "niveau_confiance": config.NIVEAU_CONFIANCE_MAX,
            "runs_propres_consecutifs": config.RUNS_PROPRES_POUR_RAMPUP - 1,
            "cooldown_multiplicateur": 1,
        }
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5)
        assert nouvel_etat["niveau_confiance"] == config.NIVEAU_CONFIANCE_MAX

    def test_zero_groupe_ne_plante_pas(self):
        etat = {"niveau_confiance": 1.0, "runs_propres_consecutifs": 0, "cooldown_multiplicateur": 1}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=0)
        assert nouvel_etat["niveau_confiance"] == 1.0


class TestSauvegarderPostsGroupe:
    def test_cree_un_fichier_json_dans_raw_dir(self, repertoires_isoles):
        posts = [{"id": "p1", "texte": "test"}]
        chemin = scraper.sauvegarder_posts_groupe(posts, "1111")
        assert chemin.exists()
        assert chemin.parent == config.RAW_DIR
        contenu = json.loads(chemin.read_text(encoding="utf-8"))
        assert contenu == posts
