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
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
from shiny import App, reactive, render, ui
from shinywidgets import output_widget, render_widget

CHEMIN_DONNEES = Path(__file__).parent / "data" / "annonces.json"


def _charger_donnees() -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Lit le JSON exporté et retourne (annonces, runs, horodatage_export).

    Si le fichier est absent (premier déploiement avant le premier export
    réussi), retourne des DataFrames vides plutôt que de faire planter toute
    l'application - un dashboard qui affiche "aucune donnée" est plus utile
    qu'une page blanche.
    """
    if not CHEMIN_DONNEES.exists():
        return pd.DataFrame(), pd.DataFrame(), "jamais"

    with CHEMIN_DONNEES.open(encoding="utf-8") as f:
        brut = json.load(f)

    annonces = pd.DataFrame(brut.get("annonces", []))
    if not annonces.empty:
        annonces["date_publication"] = pd.to_datetime(
            annonces["date_publication"], errors="coerce", utc=True
        )

    runs = pd.DataFrame(brut.get("runs", []))
    if not runs.empty:
        runs["horodatage"] = pd.to_datetime(runs["horodatage"], errors="coerce", utc=True)

    return annonces, runs, brut.get("exporte_le", "inconnu")


ANNONCES, RUNS, EXPORTE_LE = _charger_donnees()

GROUPES = sorted(ANNONCES["groupe_nom"].dropna().unique()) if not ANNONCES.empty else []
DATE_MIN = ANNONCES["date_publication"].min().date() if not ANNONCES.empty else date.today()
DATE_MAX = ANNONCES["date_publication"].max().date() if not ANNONCES.empty else date.today()
PRIX_MAX = int(ANNONCES["prix_fcfa"].max()) if not ANNONCES.empty and ANNONCES["prix_fcfa"].notna().any() else 0


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #

barre_laterale = ui.sidebar(
    ui.markdown(f"*Dernier export : {EXPORTE_LE}*"),
    ui.input_selectize(
        "groupes", "Groupes", choices=GROUPES, selected=GROUPES, multiple=True
    ),
    ui.input_date_range(
        "periode", "Période de publication", start=DATE_MIN, end=DATE_MAX
    ),
    ui.input_slider(
        "prix", "Prix (FCFA)", min=0, max=max(PRIX_MAX, 1), value=(0, max(PRIX_MAX, 1)), step=100_000
    ),
    ui.input_checkbox_group(
        "modes_runs",
        "Historique des runs - mode",
        choices=["daily", "backfill"],
        selected=["daily", "backfill"],
    ),
    width=320,
)

panneau_vue_ensemble = ui.nav_panel(
    "Vue d'ensemble",
    ui.layout_columns(
        ui.value_box("Annonces valides", ui.output_text("kpi_total"), showcase=ui.tags.span("🏠")),
        ui.value_box("Groupes couverts", ui.output_text("kpi_groupes"), showcase=ui.tags.span("📍")),
        ui.value_box(
            "Ancienneté max couverte", ui.output_text("kpi_anciennete"), showcase=ui.tags.span("📅")
        ),
        ui.value_box("Dernier run", ui.output_text("kpi_dernier_run"), showcase=ui.tags.span("⏱️")),
        col_widths=[3, 3, 3, 3],
    ),
    ui.layout_columns(
        ui.card(ui.card_header("Annonces par groupe"), output_widget("graphe_par_groupe")),
        ui.card(
            ui.card_header("Couverture temporelle par groupe (date la plus ancienne)"),
            output_widget("graphe_couverture"),
        ),
        col_widths=[6, 6],
    ),
)

panneau_runs = ui.nav_panel(
    "Historique des runs",
    ui.card(
        ui.card_header("Annonces valides par run (daily vs backfill)"),
        output_widget("graphe_runs"),
    ),
    ui.card(
        ui.card_header("Taux de conversion par run (candidats/bruts, valides/candidats)"),
        output_widget("graphe_conversion"),
    ),
)

panneau_donnees = ui.nav_panel(
    "Données",
    ui.layout_columns(
        ui.card(ui.card_header("Répartition par type de bien"), output_widget("graphe_type_bien")),
        ui.card(ui.card_header("Distribution des prix"), output_widget("graphe_prix")),
        col_widths=[6, 6],
    ),
    ui.card(ui.card_header("Annonces filtrées"), ui.output_data_frame("table_annonces")),
)

app_ui = ui.page_sidebar(
    barre_laterale,
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

        prix_min, prix_max = input.prix()
        df = df[df["prix_fcfa"].isna() | df["prix_fcfa"].between(prix_min, prix_max)]

        return df

    @reactive.calc
    def runs_filtres() -> pd.DataFrame:
        if RUNS.empty:
            return RUNS
        modes = input.modes_runs() or []
        return RUNS[RUNS["mode"].isin(modes)]

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
        jours = (pd.Timestamp.now(tz="UTC") - dates.min()).days
        return f"{jours} j"

    @render.text
    def kpi_dernier_run():
        if RUNS.empty:
            return "aucun run enregistré"
        dernier = RUNS.sort_values("horodatage").iloc[-1]
        return f"{dernier['mode']} - {int(dernier['nb_valides'])} valide(s)"

    # --- Graphiques ------------------------------------------------------ #

    @render_widget
    def graphe_par_groupe():
        df = annonces_filtrees()
        if df.empty:
            return px.bar(title="Aucune donnée")
        compte = df["groupe_nom"].value_counts().reset_index()
        compte.columns = ["groupe_nom", "nb_annonces"]
        return px.bar(
            compte.sort_values("nb_annonces"),
            x="nb_annonces",
            y="groupe_nom",
            orientation="h",
            labels={"nb_annonces": "Annonces valides", "groupe_nom": ""},
        )

    @render_widget
    def graphe_couverture():
        df = annonces_filtrees().dropna(subset=["date_publication"])
        if df.empty:
            return px.scatter(title="Aucune donnée datée")
        plus_ancienne = df.groupby("groupe_nom")["date_publication"].min().reset_index()
        aujourdhui = pd.Timestamp.now(tz="UTC")
        plus_ancienne["jours_couverts"] = (aujourdhui - plus_ancienne["date_publication"]).dt.days
        return px.bar(
            plus_ancienne.sort_values("jours_couverts"),
            x="jours_couverts",
            y="groupe_nom",
            orientation="h",
            labels={"jours_couverts": "Jours en arrière atteints", "groupe_nom": ""},
        )

    @render_widget
    def graphe_runs():
        df = runs_filtres()
        if df.empty:
            return px.line(title="Aucun run")
        return px.bar(
            df.sort_values("horodatage"),
            x="horodatage",
            y="nb_valides",
            color="mode",
            labels={"horodatage": "Date du run", "nb_valides": "Annonces valides"},
        )

    @render_widget
    def graphe_conversion():
        df = runs_filtres().sort_values("horodatage").copy()
        if df.empty:
            return px.line(title="Aucun run")
        df["taux_candidats"] = (df["nb_candidats"] / df["nb_posts_bruts"]).fillna(0)
        df["taux_valides"] = (df["nb_valides"] / df["nb_candidats"]).fillna(0)
        long = df.melt(
            id_vars="horodatage",
            value_vars=["taux_candidats", "taux_valides"],
            var_name="etape",
            value_name="taux",
        )
        return px.line(long, x="horodatage", y="taux", color="etape", markers=True)

    @render_widget
    def graphe_type_bien():
        df = annonces_filtrees()
        if df.empty or df["type_bien"].dropna().empty:
            return px.pie(title="Aucune donnée")
        compte = df["type_bien"].value_counts().reset_index()
        compte.columns = ["type_bien", "nb"]
        return px.pie(compte, names="type_bien", values="nb")

    @render_widget
    def graphe_prix():
        df = annonces_filtrees().dropna(subset=["prix_fcfa"])
        if df.empty:
            return px.histogram(title="Aucune donnée de prix")
        return px.histogram(df, x="prix_fcfa", nbins=30, labels={"prix_fcfa": "Prix (FCFA)"})

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
        return render.DataGrid(df[colonnes], filters=True, height="500px")


app = App(app_ui, server)
