"""Dashboard de suivi de la collecte d'annonces foncières (Ouagadougou).

Application Shiny for Python, déployée en Shinylive (WebAssembly) sur GitHub
Pages - tout le code ci-dessous s'exécute dans le navigateur de la personne
qui consulte le dashboard, pas sur un serveur. Les données viennent d'un
fichier JSON statique bundlé avec l'app (data/annonces.json), régénéré
côté serveur par dashboard/export_data.py à chaque run de scraping - voir
ce fichier et .github/workflows/deploy_dashboard.yml pour le pourquoi de
cette architecture (aucune connexion base de données possible ni souhaitable
depuis du code qui tourne dans le navigateur d'un visiteur).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from shiny import App, reactive, render, ui
from shinywidgets import output_widget, render_widget

CHEMIN_DONNEES = Path(__file__).parent / "data" / "annonces.json"
LONGUEUR_LABEL_MAX = 32

# Palette et thème partagés par tous les graphiques - avant, chaque chart
# piochait dans les couleurs par défaut de Plotly indépendamment, ce qui
# donnait un mélange bleu/rouge/orange sans logique d'un onglet à l'autre.
PALETTE = ["#2C5F8A", "#4F9D69", "#D9822B", "#B4436C", "#6C757D", "#3A8FB7"]
px.defaults.template = "plotly_white"
px.defaults.color_discrete_sequence = PALETTE


def _charger_donnees() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    """Lit le JSON exporté et retourne (annonces, runs, runs_ci, horodatage_export).

    Si le fichier est absent (premier déploiement avant le premier export
    réussi), retourne des DataFrames vides plutôt que de faire planter toute
    l'application - un dashboard qui affiche "aucune donnée" est plus utile
    qu'une page blanche.

    `runs_ci` (statut CI des runs via l'API GitHub Actions, ajouté par
    export_data.py le 2026-08-07) est absent des exports antérieurs à ce
    changement - `brut.get("runs_ci", [])` retourne alors une liste vide,
    pas une erreur : l'alerte d'échec ne s'affiche simplement pas tant que
    le dashboard n'a pas été régénéré avec la nouvelle version du script.
    """
    if not CHEMIN_DONNEES.exists():
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), "jamais"

    with CHEMIN_DONNEES.open(encoding="utf-8") as f:
        brut = json.load(f)

    annonces = pd.DataFrame(brut.get("annonces", []))
    if not annonces.empty:
        annonces["date_publication"] = pd.to_datetime(
            annonces["date_publication"], errors="coerce", utc=True
        )
        # premiere_collecte (TIMESTAMPTZ NOT NULL en base, voir processor.py
        # SCHEMA_SQL) : date/heure RÉELLE d'enregistrement en base, à ne pas
        # confondre avec date_publication (date du post sur Facebook, peut
        # être ancienne même pour une annonce collectée aujourd'hui via un
        # run backfill). Utilisée pour le graphique "Collecte quotidienne"
        # ci-dessous - c'est la seule colonne qui reflète fidèlement QUAND le
        # pipeline a effectivement ajouté chaque annonce en base.
        annonces["premiere_collecte"] = pd.to_datetime(
            annonces["premiere_collecte"], errors="coerce", utc=True
        )

    runs = pd.DataFrame(brut.get("runs", []))
    if not runs.empty:
        runs["horodatage"] = pd.to_datetime(runs["horodatage"], errors="coerce", utc=True)

    runs_ci = pd.DataFrame(brut.get("runs_ci", []))
    if not runs_ci.empty:
        runs_ci["horodatage"] = pd.to_datetime(runs_ci["horodatage"], errors="coerce", utc=True)

    return annonces, runs, runs_ci, brut.get("exporte_le", "inconnu")


def _tronquer(texte: object, longueur: int = LONGUEUR_LABEL_MAX) -> object:
    """Coupe un nom de groupe trop long pour tenir sur un axe de graphique.

    Le nom complet reste disponible au survol de la souris (hover_name) -
    on ne perd aucune information, on évite juste que les cartes débordent
    hors de leur conteneur, ce qui forçait un scroll horizontal.
    """
    if not isinstance(texte, str) or len(texte) <= longueur:
        return texte
    return texte[: longueur - 1].rstrip() + "…"


def _ajouter_nom_court(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute une colonne 'groupe_nom_court' pour l'affichage sur les axes."""
    df = df.copy()
    df["groupe_nom_court"] = df["groupe_nom"].map(_tronquer)
    return df


def _formater_milliers(valeur: object) -> str:
    """Formate un nombre avec séparateur de milliers (espace), vide si NA.

    Utilisé pour la table "Annonces filtrées" (prix_fcfa, superficie_m2) -
    12000000 est illisible d'un coup d'œil, 12 000 000 se lit directement.
    """
    if pd.isna(valeur):
        return ""
    return f"{valeur:,.0f}".replace(",", " ")


def _formater_horodatage(valeur: str) -> str:
    """Formate un horodatage ISO 8601 en JJ/MM/AAAA HH:MM UTC.

    Retourne la valeur telle quelle si elle n'est pas parsable (cas
    "jamais"/"inconnu" du placeholder avant le premier export réussi).
    """
    try:
        dt = datetime.fromisoformat(str(valeur).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return str(valeur)
    return dt.strftime("%d/%m/%Y %H:%M UTC")


HAUTEUR_GRAPHIQUE_PX = 380


def _widget(fig: go.Figure, hauteur: int = HAUTEUR_GRAPHIQUE_PX) -> go.FigureWidget:
    """Enveloppe une figure Plotly Express en FigureWidget, barre d'outils masquée.

    La barre d'outils Plotly (zoom/pan/export PNG) n'apporte rien sur un
    dashboard de suivi et ajoute du bruit visuel sur chaque carte.
    shinywidgets fusionne widget._config au moment de l'affichage (vérifié
    dans le code source de la version installée localement) - c'est donc un
    mécanisme réel, pas une supposition. Reste une incertitude assumée : la
    version de shinywidgets réellement chargée par Shinylive/Pyodide en
    production peut différer de celle testée ici. D'où le try/except : si
    l'attribut est ignoré par une autre version, la seule conséquence est
    que la barre d'outils reste visible, pas un plantage du dashboard.

    Hauteur fixée en pixels (BUG CORRIGÉ, retour utilisateur du 2026-08-10) :
    sans ça, les graphiques s'affichaient comme un simple trait quasi invisible
    tant que la carte n'était pas passée en plein écran. Cause : `card(...,
    full_screen=True)` ne donne pas de hauteur définie au widget en mode normal
    - Plotly autosize alors sur un conteneur flex de hauteur ~0, et ne se
    recalcule correctement qu'au redimensionnement déclenché par le plein
    écran. Fixer une hauteur explicite sur la figure ET sur la carte (voir
    ui.card(height=...) dans les définitions ci-dessous) règle le problème
    dans les deux modes.
    """
    fig.update_layout(height=hauteur, margin=dict(l=10, r=10, t=30, b=10))
    widget = go.FigureWidget(fig.data, fig.layout)
    try:
        widget._config = {"displayModeBar": False}
    except Exception:
        pass
    return widget


ANNONCES, RUNS, RUNS_CI, EXPORTE_LE = _charger_donnees()

GROUPES = sorted(ANNONCES["groupe_nom"].dropna().unique()) if not ANNONCES.empty else []
DATE_MIN = ANNONCES["date_publication"].min().date() if not ANNONCES.empty else date.today()
DATE_MAX = ANNONCES["date_publication"].max().date() if not ANNONCES.empty else date.today()

# Bornes pour les filtres de plage des histogrammes daily/backfill (panneau
# "Historique des runs") - basées sur la date d'EXÉCUTION du run
# (horodatage) : seule la table `runs` porte l'information de mode
# (daily/backfill), absente de la table `annonces`. Calculé AVANT les bornes
# de premiere_collecte ci-dessous, qui s'en servent comme plancher de
# cohérence.
PREMIER_RUN_HORODATAGE = RUNS["horodatage"].min() if not RUNS.empty else None
DATE_MIN_RUNS = PREMIER_RUN_HORODATAGE.date() if PREMIER_RUN_HORODATAGE is not None else date.today()
DATE_MAX_RUNS = RUNS["horodatage"].max().date() if not RUNS.empty else date.today()

# Bornes pour le filtre de plage du graphique "Collecte quotidienne" - basées
# sur premiere_collecte (date/heure réelle d'enregistrement en base), pas
# date_publication (voir commentaire dans _charger_donnees).
#
# GARDE-FOU (2026-08-10) : une annonce ne peut logiquement pas avoir été
# enregistrée en base AVANT le tout premier run du pipeline - si c'est le cas
# dans les données exportées, c'est une anomalie (ligne de test insérée
# manuellement lors du développement initial, ou bug de colonne ailleurs dans
# le pipeline), pas une vraie collecte. Sans ce garde-fou, une telle ligne
# fausse la borne basse du filtre de date (observé en conditions réelles :
# borne affichée à 2023 alors que le projet n'existait pas encore). On
# n'écarte PAS ces lignes des graphiques eux-mêmes (pas de perte de donnée),
# on se contente de ne pas les laisser fausser la borne du sélecteur de date.
_PREMIERE_COLLECTE_VALIDES = (
    ANNONCES["premiere_collecte"].dropna() if not ANNONCES.empty else pd.Series(dtype="datetime64[ns, UTC]")
)
_MIN_PREMIERE_COLLECTE_BRUT = (
    _PREMIERE_COLLECTE_VALIDES.min() if not _PREMIERE_COLLECTE_VALIDES.empty else None
)
NB_ANOMALIES_PREMIERE_COLLECTE = 0
if _MIN_PREMIERE_COLLECTE_BRUT is not None and PREMIER_RUN_HORODATAGE is not None:
    NB_ANOMALIES_PREMIERE_COLLECTE = int(
        (_PREMIERE_COLLECTE_VALIDES < PREMIER_RUN_HORODATAGE).sum()
    )

if NB_ANOMALIES_PREMIERE_COLLECTE > 0:
    DATE_MIN_COLLECTE = PREMIER_RUN_HORODATAGE.date()
elif _MIN_PREMIERE_COLLECTE_BRUT is not None:
    DATE_MIN_COLLECTE = _MIN_PREMIERE_COLLECTE_BRUT.date()
else:
    DATE_MIN_COLLECTE = date.today()
DATE_MAX_COLLECTE = _PREMIERE_COLLECTE_VALIDES.max().date() if not _PREMIERE_COLLECTE_VALIDES.empty else date.today()


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #

# Retouches CSS minimales : pas d'icônes/emojis, juste de la lisibilité
# (titres de KPI en petites majuscules espacées, en-têtes de carte teintés
# avec la couleur principale de la palette ci-dessus). Sélecteurs vérifiés
# contre le SCSS de bslib fourni par le paquet shiny installé localement.
STYLE_PERSONNALISE = ui.tags.style(
    """
    .bslib-value-box .value-box-title {
        text-transform: uppercase;
        letter-spacing: 0.04em;
        font-size: 0.75rem;
        opacity: 0.8;
    }
    .card-header {
        font-weight: 600;
        color: #2C5F8A;
    }
    """
)

barre_laterale = ui.sidebar(
    ui.markdown(f"*Dernier export : {_formater_horodatage(EXPORTE_LE)}*"),
    ui.input_selectize(
        "groupes", "Groupes", choices=GROUPES, selected=GROUPES, multiple=True
    ),
    ui.input_date_range(
        "periode", "Période de publication", start=DATE_MIN, end=DATE_MAX
    ),
    width=320,
)

panneau_vue_ensemble = ui.nav_panel(
    "Vue d'ensemble",
    ui.layout_columns(
        ui.value_box("Annonces valides", ui.output_text("kpi_total")),
        ui.value_box("Groupes couverts", ui.output_text("kpi_groupes")),
        ui.value_box("Annonce la plus ancienne couverte", ui.output_text("kpi_anciennete")),
        ui.output_ui("kpi_dernier_run_box"),
        col_widths=[3, 3, 3, 3],
    ),
    ui.card(
        ui.card_header("Annonces par groupe"),
        output_widget("graphe_par_groupe"),
        full_screen=True,
        height="480px",
    ),
)

panneau_runs = ui.nav_panel(
    "Historique des runs",
    ui.output_ui("alerte_run_echoue"),
    ui.card(
        ui.card_header("Collecte quotidienne (annonces enregistrées en base)"),
        ui.input_date_range(
            "periode_collecte_db",
            "Plage de dates",
            start=DATE_MIN_COLLECTE,
            end=DATE_MAX_COLLECTE,
        ),
        ui.output_text("note_anomalie_collecte_db"),
        output_widget("graphe_collecte_db"),
        full_screen=True,
        height="540px",
    ),
    # "quotidien"/"rattrapage" plutôt que "daily"/"backfill" (BUG CORRIGÉ,
    # 2026-08-10) : ces mots anglais isolés dans une interface francophone
    # étaient traduits de façon incohérente par la traduction automatique du
    # navigateur ("backfill" -> "remplissage"), rendant les intitulés absurdes.
    # "rattrapage" reprend le vocabulaire déjà utilisé ailleurs dans le projet
    # (README.md, main.py --mode) pour désigner le mode backfill.
    ui.card(
        ui.card_header("Collecte - mode quotidien"),
        ui.input_date_range(
            "periode_daily",
            "Plage de dates",
            start=DATE_MIN_RUNS,
            end=DATE_MAX_RUNS,
        ),
        output_widget("graphe_collecte_daily"),
        full_screen=True,
        height="540px",
    ),
    ui.card(
        ui.card_header("Collecte - mode rattrapage (backfill)"),
        ui.input_date_range(
            "periode_backfill",
            "Plage de dates",
            start=DATE_MIN_RUNS,
            end=DATE_MAX_RUNS,
        ),
        output_widget("graphe_collecte_backfill"),
        full_screen=True,
        height="540px",
    ),
)

panneau_donnees = ui.nav_panel(
    "Données",
    ui.card(
        ui.card_header("Annonces filtrées"),
        ui.output_data_frame("table_annonces"),
        full_screen=True,
    ),
)

app_ui = ui.page_sidebar(
    barre_laterale,
    STYLE_PERSONNALISE,
    ui.navset_card_tab(panneau_vue_ensemble, panneau_runs, panneau_donnees),
    title="Annonces foncières Ouagadougou - suivi de la collecte",
    fillable=True,
)


# --------------------------------------------------------------------------- #
# Logique serveur
# --------------------------------------------------------------------------- #


def server(input, output, session):  # noqa: A002 - noms imposés par l'API Shiny
    @reactive.calc
    def annonces_filtrees() -> pd.DataFrame:
        if ANNONCES.empty:
            return ANNONCES

        df = ANNONCES
        if input.groupes():
            df = df[df["groupe_nom"].isin(input.groupes())]

        debut, fin = input.periode()
        df = df[
            df["date_publication"].isna()
            | (
                (df["date_publication"].dt.date >= debut)
                & (df["date_publication"].dt.date <= fin)
            )
        ]

        return df

    def _histogramme_run_par_mode(mode: str, debut: date, fin: date) -> pd.DataFrame:
        """Agrège la table `runs` par jour d'exécution pour un mode donné
        (daily ou backfill), sur la plage [debut, fin] fournie par le filtre
        de plage propre à chaque histogramme.

        Source `runs` (et non `annonces`) : c'est la SEULE table qui porte
        l'information de mode - `annonces` n'a pas de colonne équivalente
        (voir SCHEMA_SQL dans processor.py, table `annonces`).
        """
        if RUNS.empty:
            return pd.DataFrame(columns=["jour", "nb_valides"])
        df = RUNS[RUNS["mode"] == mode].copy()
        df = df[(df["horodatage"].dt.date >= debut) & (df["horodatage"].dt.date <= fin)]
        if df.empty:
            return pd.DataFrame(columns=["jour", "nb_valides"])
        df["jour"] = df["horodatage"].dt.date
        return df.groupby("jour", as_index=False)["nb_valides"].sum()

    @reactive.calc
    def collecte_daily() -> pd.DataFrame:
        debut, fin = input.periode_daily()
        return _histogramme_run_par_mode("daily", debut, fin)

    @reactive.calc
    def collecte_backfill() -> pd.DataFrame:
        debut, fin = input.periode_backfill()
        return _histogramme_run_par_mode("backfill", debut, fin)

    @reactive.calc
    def collecte_db_journaliere() -> pd.DataFrame:
        """Agrège la table `annonces` par jour de `premiere_collecte` -
        source de vérité pour "combien d'annonces ont réellement été
        enregistrées en base tel jour", plutôt que le nombre auto-rapporté
        par chaque run (`runs.nb_valides`), qui peut diverger en cas
        d'upsert (ON CONFLICT DO UPDATE - une annonce déjà vue et mise à
        jour n'est PAS une nouvelle collecte au sens de ce graphique, mais
        aurait pu compter dans nb_valides selon le run l'ayant retraitée).
        """
        if ANNONCES.empty or "premiere_collecte" not in ANNONCES.columns:
            return pd.DataFrame(columns=["jour", "nb_annonces"])
        debut, fin = input.periode_collecte_db()
        df = ANNONCES.dropna(subset=["premiere_collecte"]).copy()
        df = df[
            (df["premiere_collecte"].dt.date >= debut) & (df["premiere_collecte"].dt.date <= fin)
        ]
        if df.empty:
            return pd.DataFrame(columns=["jour", "nb_annonces"])
        df["jour"] = df["premiere_collecte"].dt.date
        return df.groupby("jour", as_index=False).size().rename(columns={"size": "nb_annonces"})

    # --- KPIs ---------------------------------------------------------- #

    @render.text
    def kpi_total():
        return str(len(annonces_filtrees()))

    @render.text
    def kpi_groupes():
        df = annonces_filtrees()
        return str(df["groupe_nom"].nunique()) if not df.empty else "0"

    @render.text
    def kpi_anciennete():
        df = annonces_filtrees()
        dates = df["date_publication"].dropna()
        if dates.empty:
            return "n/d"
        return dates.min().strftime("%d/%m/%Y %H:%M UTC")

    @render.ui
    def kpi_dernier_run_box():
        """Value box du dernier run, en rouge si 0 annonce valide.

        Un run à 0 valide peut simplement signifier qu'aucune nouvelle
        annonce n'est parue - mais peut aussi trahir une session Facebook
        expirée ou un sélecteur DOM cassé (déjà vu sur ce pipeline). Le
        signal visuel sert à attirer l'œil sans qu'il faille aller lire les
        logs GitHub Actions pour s'en rendre compte.
        """
        if RUNS.empty:
            return ui.value_box("Dernier run", "aucun run enregistré")

        dernier = RUNS.sort_values("horodatage").iloc[-1]
        nb_valides = int(dernier["nb_valides"]) if pd.notna(dernier["nb_valides"]) else 0
        contenu = ui.div(
            ui.div(str(dernier["mode"]), style="font-size: 0.95rem; opacity: 0.8;"),
            ui.div(
                f"{nb_valides} valide(s)",
                style="font-size: 1.5rem; font-weight: 600; line-height: 1.2;",
            ),
        )
        theme = "bg-danger" if nb_valides == 0 else None
        return ui.value_box("Dernier run", contenu, theme=theme)

    @render.ui
    def alerte_run_echoue():
        """Bandeau d'alerte si le run le plus récent du workflow de scraping
        a échoué - seule source fiable de ce signal : la base de données
        n'enregistre RIEN sur les chemins d'échec du pipeline (voir main.py,
        chaque exception retourne avant enregistrer_run()). Sans donnée
        `runs_ci` (export généré avant l'ajout de cette fonctionnalité, ou
        GITHUB_TOKEN absent au moment de l'export), n'affiche RIEN plutôt que
        de deviner un statut - conformément à la consigne de ne jamais
        inventer une information.
        """
        if RUNS_CI.empty:
            return None
        dernier = RUNS_CI.sort_values("horodatage").iloc[-1]
        if dernier.get("conclusion") != "failure":
            return None
        return ui.div(
            f"Le dernier run du pipeline de scraping a échoué "
            f"({_formater_horodatage(str(dernier['horodatage']))}).",
            ui.a("Voir le run sur GitHub", href=dernier.get("url"), target="_blank"),
            class_="alert alert-danger mb-3",
        )

    @render.text
    def note_anomalie_collecte_db():
        if NB_ANOMALIES_PREMIERE_COLLECTE == 0:
            return ""
        return (
            f"{NB_ANOMALIES_PREMIERE_COLLECTE} annonce(s) avec une date de collecte "
            "antérieure au premier run enregistré - exclue(s) du calcul de la borne "
            "du filtre (anomalie de données à vérifier), mais toujours visibles dans "
            "les graphiques et la table."
        )

    # --- Graphiques ------------------------------------------------------ #

    @render_widget
    def graphe_collecte_db():
        df = collecte_db_journaliere()
        if df.empty:
            return _widget(px.bar(title="Aucune annonce enregistrée sur cette plage"))
        fig = px.bar(
            df,
            x="jour",
            y="nb_annonces",
            labels={"jour": "Jour", "nb_annonces": "Annonces enregistrées en base"},
        )
        return _widget(fig)

    @render_widget
    def graphe_collecte_daily():
        df = collecte_daily()
        if df.empty:
            return _widget(px.bar(title="Aucun run daily sur cette plage"))
        fig = px.bar(
            df,
            x="jour",
            y="nb_valides",
            labels={"jour": "Jour", "nb_valides": "Annonces valides (daily)"},
        )
        return _widget(fig)

    @render_widget
    def graphe_collecte_backfill():
        df = collecte_backfill()
        if df.empty:
            return _widget(px.bar(title="Aucun run backfill sur cette plage"))
        fig = px.bar(
            df,
            x="jour",
            y="nb_valides",
            labels={"jour": "Jour", "nb_valides": "Annonces valides (backfill)"},
        )
        return _widget(fig)

    @render_widget
    def graphe_par_groupe():
        df = annonces_filtrees()
        if df.empty:
            return _widget(px.bar(title="Aucune donnée"))
        compte = df["groupe_nom"].value_counts().reset_index()
        compte.columns = ["groupe_nom", "nb_annonces"]
        compte = _ajouter_nom_court(compte)
        fig = px.bar(
            compte.sort_values("nb_annonces"),
            x="nb_annonces",
            y="groupe_nom_court",
            orientation="h",
            hover_name="groupe_nom",
            labels={"nb_annonces": "Annonces valides", "groupe_nom_court": ""},
        )
        fig.update_yaxes(automargin=True)
        return _widget(fig)

    # NOTE (2026-08-07) : le graphique "Couverture temporelle par groupe"
    # (jours en arrière atteints par groupe) a été retiré à la demande de
    # l'utilisateur ("pas important"). Les graphiques "Annonces valides par
    # run" et "Taux de conversion par run" ont été remplacés par les 3
    # histogrammes ci-dessus (collecte DB globale + daily/backfill,
    # filtrables indépendamment).

    # NOTE (2026-08-07) : les graphiques "Répartition par type de bien" et
    # "Distribution des prix" ont été retirés à la demande explicite de
    # l'utilisateur - l'objet de ce dashboard est de suivre la COLLECTE
    # (volume, échecs), pas d'analyser le marché immobilier. Le filtre de
    # prix (barre latérale) et la colonne prix_fcfa de la table restent
    # disponibles : ce n'est pas une suppression de donnée, seulement le
    # retrait des graphiques de statistiques dédiés.

    # --- Table ------------------------------------------------------------ #

    @render.data_frame
    def table_annonces():
        colonnes = [
            "groupe_nom",
            "date_publication",
            "type_bien",
            "quartier_zone",
            "superficie_m2",
            "prix_fcfa",
            "statut_document",
            "resume_court",
        ]
        df = annonces_filtrees()
        if df.empty:
            return render.DataGrid(pd.DataFrame(columns=colonnes))

        # Tri par date de publication décroissante (annonces récentes en
        # premier) - NaT (date non extraite lors du scraping) relégué en
        # fin de liste plutôt qu'en tête, où il serait pris pour une
        # annonce fraîche.
        df_affiche = df[colonnes].sort_values(
            "date_publication", ascending=False, na_position="last"
        ).copy()
        df_affiche["date_publication"] = df_affiche["date_publication"].dt.strftime("%d/%m/%Y").fillna("")

        # Séparateurs de milliers pour la lisibilité (12 000 000 plutôt que
        # 12000000). Effet de bord assumé : ces colonnes deviennent du
        # texte, donc leur filtre de colonne (plage numérique min/max)
        # devient un filtre texte simple. Acceptable pour prix_fcfa (déjà
        # filtrable via le curseur de la barre latérale) ; plus discutable
        # pour superficie_m2, qui n'a pas d'équivalent ailleurs - à revoir
        # si ce filtre s'avère utile à l'usage.
        for colonne in ("prix_fcfa", "superficie_m2"):
            df_affiche[colonne] = df_affiche[colonne].apply(_formater_milliers)

        return render.DataGrid(df_affiche, filters=True, height="500px")


app = App(app_ui, server)
