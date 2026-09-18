from __future__ import annotations

import csv
import io
import logging
import math
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from fpdf import FPDF, XPos, YPos
from pandas.errors import EmptyDataError, ParserError


# =============================================================================
# Streamlit-paginaconfiguratie
# =============================================================================

st.set_page_config(
    page_title="Grondwaterrapportage",
    page_icon="💧",
    layout="wide",
    initial_sidebar_state="expanded",
)


# =============================================================================
# Configuratie
# =============================================================================


@dataclass(frozen=True)
class AppConfig:
    """Centrale configuratie van de Streamlit-applicatie."""

    encodings_to_try: tuple[str, ...] = (
        "utf-8-sig",
        "utf-8",
        "cp1252",
        "latin-1",
        "iso-8859-1",
    )

    delimiter: str = ";"
    expected_date_column: str = "datum"
    expected_measurement_column: str = "meting NAP"
    hydrological_year_column: str = "hydrologisch_jaar"

    hydrological_year_start_month: int = 4
    minimum_measurements_per_year: int = 2
    outlier_threshold_meters: float = 0.5

    graph_dpi: int = 200
    plot_width_inches: float = 10.5
    plot_height_inches: float = 6.0

    pdf_margin_mm: float = 10.0
    pdf_bottom_margin_mm: float = 18.0

    output_suffix: str = "_rapport.pdf"

    csv_na_values: tuple[str, ...] = (
        "",
        "NA",
        "N/A",
        "null",
        "None",
        "-",
    )


DEFAULT_CONFIG = AppConfig()


# =============================================================================
# Datamodellen
# =============================================================================


@dataclass(frozen=True)
class CsvMetadata:
    """Metadata die tijdens de eerste scan van een CSV wordt gevonden."""

    filternummer: str
    header_row_index: int
    encoding: str


@dataclass(frozen=True)
class OutlierRecord:
    """Gegevens van één verwijderde uitschieter."""

    measurement_date: pd.Timestamp
    measurement_value: float
    hydrological_year: int
    yearly_mean: float
    absolute_deviation: float


@dataclass(frozen=True)
class OutlierFilterResult:
    """Resultaat van de uitschieterfiltering."""

    filtered_data: pd.DataFrame
    removed_outliers: tuple[OutlierRecord, ...]


@dataclass(frozen=True)
class GroundwaterStatistics:
    """Resultaat van de GHG- en GLG-proxyberekening."""

    ghg: Optional[float]
    glg: Optional[float]
    yearly_max: pd.Series
    yearly_min: pd.Series
    yearly_count: pd.Series
    reference_years: tuple[int, ...]
    excluded_years: tuple[int, ...]
    removed_outliers: tuple[OutlierRecord, ...]
    original_measurement_count: int = 0
    filtered_measurement_count: int = 0

    @property
    def removed_outlier_count(self) -> int:
        """Geef het aantal verwijderde uitschieters terug."""

        return len(self.removed_outliers)


@dataclass
class ProcessingResult:
    """Resultaat van de verwerking van één geüpload CSV-bestand."""

    source_name: str
    output_name: Optional[str]
    success: bool
    message: str
    filternummer: Optional[str] = None
    pdf_bytes: Optional[bytes] = None
    graph_bytes: Optional[bytes] = None
    dataframe: Optional[pd.DataFrame] = None
    filtered_dataframe: Optional[pd.DataFrame] = None
    statistics: Optional[GroundwaterStatistics] = None
    log_messages: list[str] = field(default_factory=list)


# =============================================================================
# Logging
# =============================================================================


class ListLogHandler(logging.Handler):
    """Logging-handler die logregels in een lijst opslaat."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s - %(levelname)s - %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        """Sla een geformatteerd logrecord op."""

        try:
            self.messages.append(self.format(record))
        except Exception:
            self.handleError(record)


def create_logger(source_name: str) -> tuple[logging.Logger, ListLogHandler]:
    """Maak een geïsoleerde logger voor één bestand."""

    logger_name = f"grondwaterrapport.{source_name}.{id(source_name)}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    handler = ListLogHandler()
    logger.addHandler(handler)

    return logger, handler


# =============================================================================
# Algemene hulpfuncties
# =============================================================================


def normalize_text(value: object) -> str:
    """Normaliseer tekst voor betrouwbare vergelijkingen."""

    if value is None:
        return ""

    text = str(value).replace("\ufeff", "").strip()
    return re.sub(r"\s+", " ", text)


def normalize_column_name(value: object) -> str:
    """Normaliseer een CSV-kolomnaam."""

    return normalize_text(value).rstrip(";").strip()


def canonical_column_name(value: object) -> str:
    """Maak een kolomnaam geschikt voor casusongevoelige vergelijking."""

    return normalize_column_name(value).casefold()


def sanitize_filternummer(value: object) -> str:
    """Maak een filternummer geschikt voor rapportage."""

    filternummer = normalize_text(value)

    if not filternummer:
        return "Onbekend"

    filternummer = "".join(
        character
        for character in filternummer
        if character.isprintable()
    )

    return filternummer[:200] or "Onbekend"


def sanitize_filename(value: str) -> str:
    """Maak een veilige bestandsnaam zonder padcomponenten."""

    filename = value.replace("\\", "/").split("/")[-1]
    filename = re.sub(r"[^A-Za-z0-9._ -]", "_", filename)
    filename = filename.strip(" .")

    return filename or "rapport.csv"


def encode_for_pdf(value: object) -> str:
    """Converteer tekst naar Latin-1 voor standaard PDF-lettertypen."""

    return str(value).encode("latin-1", errors="replace").decode("latin-1")


def format_nap_value(value: float) -> str:
    """Formatteer een NAP-waarde met twee decimalen."""

    return f"{value:.2f} m NAP"


def validate_uploaded_bytes(file_bytes: bytes, source_name: str) -> None:
    """Controleer of een geüpload bestand bruikbare inhoud bevat."""

    if not file_bytes:
        raise ValueError(f"Bestand is leeg: {source_name}")

    if len(file_bytes) > 100 * 1024 * 1024:
        raise ValueError(
            f"Bestand {source_name} is groter dan 100 MB en wordt niet verwerkt."
        )


# =============================================================================
# CSV-detectie en inlezen
# =============================================================================


def parse_csv_line(
    line: str,
    delimiter: str,
) -> list"""Parse één CSV-regel met ondersteuning voor gequote velden."""

    if not line:
        return []

    try:
        reader = csv.reader(
            [line],
            delimiter=delimiter,
            skipinitialspace=True,
        )
        return next(reader)
    except (csv.Error, StopIteration):
        return []


def line_starts_with_expected_header(
    line: str,
    config: AppConfig,
) -> bool:
    """Controleer of een regel begint met de vereiste kolommen."""

    parts = [
        canonical_column_name(part)
        for part in parse_csv_line(line, config.delimiter)
    ]

    if len(parts) < 2:
        return False

    return (
        parts[0] == config.expected_date_column.casefold()
        and parts[1] == config.expected_measurement_column.casefold()
    )


def extract_filternummer_from_first_line(
    first_line: str,
    config: AppConfig,
) -> str:
    """Haal het filternummer uit het eerste veld van de eerste regel."""

    if line_starts_with_expected_header(first_line, config):
        return "Onbekend"

    fields = parse_csv_line(first_line, config.delimiter)

    if not fields:
        return "Onbekend"

    return sanitize_filternummer(fields[0])


def decode_uploaded_file(
    file_bytes: bytes,
    source_name: str,
    config: AppConfig,
) -> tuple[str, str]:
    """Decodeer een geüpload bestand met een ondersteunde encoding."""

    errors: list[str] = []

    for encoding in config.encodings_to_try:
        try:
            return file_bytes.decode(encoding, errors="strict"), encoding
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")

    raise UnicodeError(
        f"Geen geschikte tekstencoding gevonden voor {source_name}. "
        f"Geprobeerd: {', '.join(config.encodings_to_try)}. "
        f"Details: {'; '.join(errors)}"
    )


def find_csv_metadata_and_header(
    text: str,
    encoding: str,
    source_name: str,
    config: AppConfig,
) -> CsvMetadata:
    """Zoek filternummer en headerregel in gedecodeerde CSV-inhoud."""

    lines = text.splitlines()

    if not lines:
        raise ValueError(f"CSV-bestand is leeg: {source_name}")

    filternummer = extract_filternummer_from_first_line(lines[0], config)

    for row_index, line in enumerate(lines):
        if line_starts_with_expected_header(line, config):
            return CsvMetadata(
                filternummer=filternummer,
                header_row_index=row_index,
                encoding=encoding,
            )

    raise ValueError(
        f"De verwachte header met kolommen "
        f"'{config.expected_date_column}' en "
        f"'{config.expected_measurement_column}' is niet gevonden in "
        f"{source_name}."
    )


def standardize_required_columns(
    dataframe: pd.DataFrame,
    config: AppConfig,
) -> pd.DataFrame:
    """Normaliseer kolomnamen en geef vereiste kolommen vaste namen."""

    cleaned_df = dataframe.copy()
    cleaned_df.columns = [
        normalize_column_name(column)
        for column in cleaned_df.columns
    ]

    valid_columns = [
        column
        for column in cleaned_df.columns
        if column
        and not canonical_column_name(column).startswith("unnamed")
    ]
    cleaned_df = cleaned_df.loc[:, valid_columns]

    canonical_lookup: dict[str, list[str]] = {}

    for column in cleaned_df.columns:
        canonical_lookup.setdefault(
            canonical_column_name(column),
            [],
        ).append(column)

    required_mapping = {
        config.expected_date_column.casefold():
            config.expected_date_column,
        config.expected_measurement_column.casefold():
            config.expected_measurement_column,
    }

    rename_mapping: dict[str, str] = {}

    for canonical_name, target_name in required_mapping.items():
        matches = canonical_lookup.get(canonical_name, [])

        if not matches:
            raise ValueError(
                f"Vereiste kolom '{target_name}' ontbreekt. "
                f"Gevonden kolommen: {list(cleaned_df.columns)}"
            )

        if len(matches) > 1:
            raise ValueError(
                f"Kolom '{target_name}' komt meerdere keren voor: {matches}"
            )

        rename_mapping[matches[0]] = target_name

    return cleaned_df.rename(columns=rename_mapping)


def parse_numeric_value(value: object) -> float:
    """Converteer één meetwaarde robuust naar een float."""

    if value is None or pd.isna(value) or isinstance(value, bool):
        return math.nan

    if isinstance(value, (int, float)):
        numeric_value = float(value)
        return numeric_value if math.isfinite(numeric_value) else math.nan

    text = normalize_text(value)
    text = text.replace("\u00a0", "").replace(" ", "")
    text = re.sub(r"[^0-9,.\-+]", "", text)

    if not text or text in {"-", "+", ".", ","}:
        return math.nan

    sign_count = text.count("+") + text.count("-")

    if sign_count > 1:
        return math.nan

    if sign_count == 1 and text[0] not in {"+", "-"}:
        return math.nan

    sign = text[0] if text[:1] in {"+", "-"} else ""
    unsigned_text = text[1:] if sign else text

    if not unsigned_text or not any(
        character.isdigit()
        for character in unsigned_text
    ):
        return math.nan

    comma_count = unsigned_text.count(",")
    dot_count = unsigned_text.count(".")
    comma_position = unsigned_text.rfind(",")
    dot_position = unsigned_text.rfind(".")

    if comma_count and dot_count:
        if comma_position > dot_position:
            unsigned_text = unsigned_text.replace(".", "")
            unsigned_text = unsigned_text.replace(",", ".")
        else:
            unsigned_text = unsigned_text.replace(",", "")

    elif comma_count == 1:
        unsigned_text = unsigned_text.replace(",", ".")

    elif comma_count > 1:
        integer_part, decimal_part = unsigned_text.rsplit(",", maxsplit=1)
        unsigned_text = integer_part.replace(",", "") + "." + decimal_part

    elif dot_count > 1:
        integer_part, decimal_part = unsigned_text.rsplit(".", maxsplit=1)
        unsigned_text = integer_part.replace(".", "") + "." + decimal_part

    try:
        numeric_value = float(sign + unsigned_text)
    except ValueError:
        return math.nan

    return numeric_value if math.isfinite(numeric_value) else math.nan


def parse_dates(series: pd.Series) -> pd.Series:
    """Parse datums en retourneer timezone-naive timestamps."""

    raw_values = series.astype("string").str.strip()

    try:
        parsed = pd.to_datetime(
            raw_values,
            format="mixed",
            dayfirst=True,
            errors="coerce",
            utc=True,
        )
    except (TypeError, ValueError):
        parsed = pd.to_datetime(
            raw_values,
            dayfirst=True,
            errors="coerce",
            utc=True,
        )

    return parsed.dt.tz_convert(None)


def load_and_prepare_data(
    text: str,
    metadata: CsvMetadata,
    source_name: str,
    config: AppConfig,
    logger: logging.Logger,
) -> pd.DataFrame:
    """Laad, normaliseer en valideer grondwatermetingen."""

    try:
        dataframe = pd.read_csv(
            io.StringIO(text),
            sep=config.delimiter,
            skiprows=metadata.header_row_index,
            dtype=str,
            keep_default_na=True,
            na_values=list(config.csv_na_values),
            engine="python",
            on_bad_lines="warn",
        )
    except EmptyDataError as exc:
        raise ValueError(
            f"Geen tabelgegevens gevonden in {source_name}."
        ) from exc
    except ParserError as exc:
        raise ValueError(
            f"CSV-structuur van {source_name} is ongeldig: {exc}"
        ) from exc

    if dataframe.empty:
        raise ValueError(f"Geen datarijen gevonden in {source_name}.")

    dataframe = standardize_required_columns(dataframe, config)
    original_row_count = len(dataframe)

    dataframe[config.expected_date_column] = parse_dates(
        dataframe[config.expected_date_column]
    )

    dataframe[config.expected_measurement_column] = (
        dataframe[config.expected_measurement_column]
        .map(parse_numeric_value)
        .astype("float64")
    )

    invalid_date_count = int(
        dataframe[config.expected_date_column].isna().sum()
    )
    invalid_measurement_count = int(
        dataframe[config.expected_measurement_column].isna().sum()
    )

    if invalid_date_count:
        logger.warning(
            "%d ongeldige datumwaarde(n) verwijderd.",
            invalid_date_count,
        )

    if invalid_measurement_count:
        logger.warning(
            "%d ongeldige of lege meetwaarde(n) verwijderd.",
            invalid_measurement_count,
        )

    dataframe = dataframe.dropna(
        subset=[
            config.expected_date_column,
            config.expected_measurement_column,
        ]
    ).copy()

    if dataframe.empty:
        raise ValueError(
            f"Na validatie zijn geen bruikbare metingen "
            f"overgebleven in {source_name}."
        )

    duplicate_subset = [
        config.expected_date_column,
        config.expected_measurement_column,
    ]
    duplicate_count = int(
        dataframe.duplicated(subset=duplicate_subset).sum()
    )

    if duplicate_count:
        logger.warning(
            "%d exacte dubbele meting(en) verwijderd.",
            duplicate_count,
        )
        dataframe = dataframe.drop_duplicates(
            subset=duplicate_subset,
            keep="first",
        )

    dataframe = dataframe.sort_values(
        config.expected_date_column,
        kind="stable",
    ).reset_index(drop=True)

    logger.info(
        "Data geladen met encoding %s: %d geldige metingen, "
        "%d verwijderde rijen.",
        metadata.encoding,
        len(dataframe),
        original_row_count - len(dataframe),
    )

    return dataframe


# =============================================================================
# Hydrologische berekeningen
# =============================================================================


def add_hydrological_year(
    dataframe: pd.DataFrame,
    config: AppConfig,
) -> pd.DataFrame:
    """Voeg het hydrologische startjaar toe."""

    start_month = config.hydrological_year_start_month

    if not 1 <= start_month <= 12:
        raise ValueError("De startmaand moet tussen 1 en 12 liggen.")

    enriched_df = dataframe.copy()
    dates = enriched_df[config.expected_date_column]

    if not pd.api.types.is_datetime64_any_dtype(dates):
        raise TypeError("De datumkolom moet datetime-waarden bevatten.")

    enriched_df[config.hydrological_year_column] = dates.dt.year.where(
        dates.dt.month >= start_month,
        dates.dt.year - 1,
    ).astype("int64")

    return enriched_df


def remove_outliers_per_hydrological_year(
    dataframe: pd.DataFrame,
    config: AppConfig,
    logger: logging.Logger,
) -> OutlierFilterResult:
    """Verwijder uitschieters op basis van afstand tot het jaargemiddelde."""

    if dataframe.empty:
        return OutlierFilterResult(dataframe.copy(), ())

    measurement_column = config.expected_measurement_column
    year_column = config.hydrological_year_column
    threshold = config.outlier_threshold_meters

    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError(
            "De uitschieterdrempel moet een eindig, niet-negatief getal zijn."
        )

    result = dataframe.copy()

    result["_yearly_mean"] = result.groupby(
        year_column,
        sort=False,
    )[measurement_column].transform("mean")

    result["_absolute_deviation"] = (
        result[measurement_column] - result["_yearly_mean"]
    ).abs()

    keep_mask = result["_absolute_deviation"] <= threshold
    retained_counts = keep_mask.groupby(result[year_column]).sum()
    fully_rejected_years = retained_counts[retained_counts == 0].index

    if len(fully_rejected_years) > 0:
        keep_mask = keep_mask | result[year_column].isin(
            fully_rejected_years
        )

        for year in fully_rejected_years:
            logger.warning(
                "Hydrologisch jaar %s zou volledig verdwijnen. "
                "De oorspronkelijke metingen zijn behouden.",
                int(year),
            )

    removed_data = result.loc[~keep_mask].sort_values(
        config.expected_date_column,
        kind="stable",
    )

    removed_outliers = tuple(
        OutlierRecord(
            measurement_date=pd.Timestamp(
                row[config.expected_date_column]
            ),
            measurement_value=float(
                row[config.expected_measurement_column]
            ),
            hydrological_year=int(
                row[config.hydrological_year_column]
            ),
            yearly_mean=float(row["_yearly_mean"]),
            absolute_deviation=float(row["_absolute_deviation"]),
        )
        for _, row in removed_data.iterrows()
    )

    filtered_df = (
        result.loc[keep_mask]
        .drop(columns=["_yearly_mean", "_absolute_deviation"])
        .sort_values(config.expected_date_column, kind="stable")
        .reset_index(drop=True)
    )

    logger.info(
        "Totaal %d uitschieter(s) verwijderd.",
        len(removed_outliers),
    )

    return OutlierFilterResult(
        filtered_data=filtered_df,
        removed_outliers=removed_outliers,
    )


def empty_groundwater_statistics(
    original_count: int = 0,
    filtered_count: int = 0,
    removed_outliers: tuple[OutlierRecord, ...] = (),
    excluded_years: tuple[int, ...] = (),
) -> GroundwaterStatistics:
    """Maak een leeg statistiekresultaat."""

    return GroundwaterStatistics(
        ghg=None,
        glg=None,
        yearly_max=pd.Series(dtype="float64"),
        yearly_min=pd.Series(dtype="float64"),
        yearly_count=pd.Series(dtype="int64"),
        reference_years=(),
        excluded_years=excluded_years,
        removed_outliers=removed_outliers,
        original_measurement_count=original_count,
        filtered_measurement_count=filtered_count,
    )


def calculate_ghg_glg_proxy(
    dataframe: pd.DataFrame,
    config: AppConfig,
    logger: logging.Logger,
) -> tuple[GroundwaterStatistics, pd.DataFrame]:
    """Bereken GHG- en GLG-proxywaarden per hydrologisch jaar."""

    if dataframe.empty:
        return empty_groundwater_statistics(), dataframe.copy()

    original_count = len(dataframe)
    calculation_df = add_hydrological_year(dataframe, config)

    filter_result = remove_outliers_per_hydrological_year(
        calculation_df,
        config,
        logger,
    )
    filtered_df = filter_result.filtered_data

    if filtered_df.empty:
        return (
            empty_groundwater_statistics(
                original_count=original_count,
                removed_outliers=filter_result.removed_outliers,
            ),
            filtered_df,
        )

    grouped = filtered_df.groupby(
        config.hydrological_year_column,
        sort=True,
    )[config.expected_measurement_column]

    yearly_max_all = grouped.max()
    yearly_min_all = grouped.min()
    yearly_count_all = grouped.count().astype("int64")

    reference_years = tuple(
        int(year)
        for year, count in yearly_count_all.items()
        if int(count) >= config.minimum_measurements_per_year
    )
    excluded_years = tuple(
        int(year)
        for year, count in yearly_count_all.items()
        if int(count) < config.minimum_measurements_per_year
    )

    if excluded_years:
        logger.warning(
            "Uitgesloten hydrologische jaren: %s.",
            ", ".join(map(str, excluded_years)),
        )

    if not reference_years:
        return (
            empty_groundwater_statistics(
                original_count=original_count,
                filtered_count=len(filtered_df),
                removed_outliers=filter_result.removed_outliers,
                excluded_years=excluded_years,
            ),
            filtered_df,
        )

    indexes = list(reference_years)
    yearly_max = yearly_max_all.loc[indexes]
    yearly_min = yearly_min_all.loc[indexes]
    yearly_count = yearly_count_all.loc[indexes]

    statistics = GroundwaterStatistics(
        ghg=float(yearly_max.mean()),
        glg=float(yearly_min.mean()),
        yearly_max=yearly_max,
        yearly_min=yearly_min,
        yearly_count=yearly_count,
        reference_years=reference_years,
        excluded_years=excluded_years,
        removed_outliers=filter_result.removed_outliers,
        original_measurement_count=original_count,
        filtered_measurement_count=len(filtered_df),
    )

    return statistics, filtered_df


# =============================================================================
# Grafiek
# =============================================================================


def create_graph_bytes(
    dataframe: pd.DataFrame,
    filternummer: str,
    statistics: GroundwaterStatistics,
    config: AppConfig,
) -> bytes:
    """Genereer een PNG-grafiek en retourneer de bytes."""

    figure, axis = plt.subplots(
        figsize=(
            config.plot_width_inches,
            config.plot_height_inches,
        )
    )

    try:
        axis.plot(
            dataframe[config.expected_date_column],
            dataframe[config.expected_measurement_column],
            marker="o",
            markersize=3.5,
            linewidth=1.2,
            color="#1f77b4",
            label="Gevalideerde waterstand",
            zorder=2,
        )

        if statistics.removed_outliers:
            axis.scatter(
                [
                    record.measurement_date
                    for record in statistics.removed_outliers
                ],
                [
                    record.measurement_value
                    for record in statistics.removed_outliers
                ],
                marker="x",
                s=55,
                linewidths=1.8,
                color="#d32f2f",
                label=(
                    "Verwijderde uitschieters "
                    f"(n={statistics.removed_outlier_count})"
                ),
                zorder=5,
            )

        if statistics.ghg is not None:
            axis.axhline(
                statistics.ghg,
                color="#c62828",
                linestyle="--",
                linewidth=1.2,
                label=f"GHG-proxy ({format_nap_value(statistics.ghg)})",
            )

        if statistics.glg is not None:
            axis.axhline(
                statistics.glg,
                color="#1565c0",
                linestyle="--",
                linewidth=1.2,
                label=f"GLG-proxy ({format_nap_value(statistics.glg)})",
            )

        axis.set_title(
            f"Waterstanden peilfilter {filternummer} "
            f"(n={len(dataframe)})"
        )
        axis.set_xlabel("Datum")
        axis.set_ylabel("Waterstand (m NAP)")
        axis.grid(
            visible=True,
            linestyle="--",
            linewidth=0.5,
            alpha=0.7,
        )
        axis.legend(loc="best")
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        axis.xaxis.set_major_locator(
            mdates.AutoDateLocator(minticks=5, maxticks=10)
        )

        figure.autofmt_xdate()
        figure.tight_layout()

        buffer = io.BytesIO()
        figure.savefig(
            buffer,
            format="png",
            dpi=config.graph_dpi,
            bbox_inches="tight",
            facecolor="white",
        )
        return buffer.getvalue()

    finally:
        plt.close(figure)


# =============================================================================
# PDF
# =============================================================================


class PDFReport(FPDF):
    """PDF-rapport met vaste kop- en voettekst."""

    def __init__(
        self,
        filternummer: str,
        config: AppConfig,
    ) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.filternummer = encode_for_pdf(filternummer)
        self.config = config

        self.set_margins(
            config.pdf_margin_mm,
            config.pdf_margin_mm,
            config.pdf_margin_mm,
        )
        self.set_auto_page_break(
            auto=True,
            margin=config.pdf_bottom_margin_mm,
        )
        self.set_title(
            encode_for_pdf(f"Waterstanden peilfilter {filternummer}")
        )
        self.set_author("Grondwaterrapportage")

    @property
    def usable_width(self) -> float:
        """Geef de beschikbare paginabreedte terug."""

        return self.w - self.l_margin - self.r_margin

    def header(self) -> None:
        """Plaats de rapportkop."""

        self.set_font("Helvetica", "B", 15)
        self.cell(
            0,
            9,
            f"Waterstanden peilfilter: {self.filternummer}",
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
            align="C",
        )
        self.ln(3)

    def footer(self) -> None:
        """Plaats het paginanummer."""

        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(90, 90, 90)
        self.cell(
            0,
            10,
            f"Pagina {self.page_no()}",
            align="C",
        )
        self.set_text_color(0, 0, 0)

    def section_title(self, title: str) -> None:
        """Schrijf een sectietitel."""

        self.set_font("Helvetica", "B", 11)
        self.set_fill_color(240, 240, 240)
        self.cell(
            0,
            8,
            encode_for_pdf(title),
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
            fill=True,
        )
        self.ln(2)


def add_summary_to_pdf(
    pdf: PDFReport,
    dataframe: pd.DataFrame,
    statistics: GroundwaterStatistics,
    config: AppConfig,
) -> None:
    """Voeg een analysesamenvatting toe."""

    pdf.section_title("Samenvatting")

    rows = [
        ("Aantal geldige metingen", str(len(dataframe))),
        (
            "Metingen na uitschieterfilter",
            str(statistics.filtered_measurement_count),
        ),
        (
            "Aantal verwijderde uitschieters",
            str(statistics.removed_outlier_count),
        ),
        (
            "Eerste meetdatum",
            dataframe[config.expected_date_column]
            .min()
            .strftime("%d-%m-%Y"),
        ),
        (
            "Laatste meetdatum",
            dataframe[config.expected_date_column]
            .max()
            .strftime("%d-%m-%Y"),
        ),
        (
            "Gebruikte hydrologische jaren",
            ", ".join(map(str, statistics.reference_years)) or "Geen",
        ),
    ]

    if statistics.ghg is not None:
        rows.append(("GHG-proxy", format_nap_value(statistics.ghg)))

    if statistics.glg is not None:
        rows.append(("GLG-proxy", format_nap_value(statistics.glg)))

    if statistics.excluded_years:
        rows.append(
            (
                "Uitgesloten hydrologische jaren",
                ", ".join(map(str, statistics.excluded_years)),
            )
        )

    label_width = pdf.usable_width * 0.40
    value_width = pdf.usable_width - label_width

    for label, value in rows:
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(
            label_width,
            7,
            encode_for_pdf(label),
            border=1,
            new_x=XPos.RIGHT,
            new_y=YPos.TOP,
        )
        pdf.set_font("Helvetica", "", 8)
        pdf.cell(
            value_width,
            7,
            encode_for_pdf(value),
            border=1,
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )

    pdf.ln(4)


def add_yearly_table_to_pdf(
    pdf: PDFReport,
    statistics: GroundwaterStatistics,
) -> None:
    """Voeg de hydrologische jaarresultaten toe."""

    pdf.section_title("Jaarlijkse hoogste en laagste standen")

    if not statistics.reference_years:
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(
            0,
            8,
            "Geen bruikbare jaargegevens beschikbaar.",
            border=1,
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
            align="C",
        )
        return

    widths = (
        pdf.usable_width * 0.24,
        pdf.usable_width * 0.21,
        pdf.usable_width * 0.275,
        pdf.usable_width * 0.275,
    )
    headers = (
        "Hydrologisch jaar",
        "Aantal metingen",
        "Hoogste stand",
        "Laagste stand",
    )

    def add_header() -> None:
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(225, 225, 225)

        for index, (header, width) in enumerate(zip(headers, widths)):
            last = index == len(headers) - 1
            pdf.cell(
                width,
                8,
                encode_for_pdf(header),
                border=1,
                new_x=XPos.LMARGIN if last else XPos.RIGHT,
                new_y=YPos.NEXT if last else YPos.TOP,
                align="C",
                fill=True,
            )

    add_header()

    for year in statistics.reference_years:
        if pdf.get_y() + 14 > pdf.h - pdf.b_margin:
            pdf.add_page()
            pdf.section_title(
                "Jaarlijkse hoogste en laagste standen (vervolg)"
            )
            add_header()

        values = (
            str(year),
            str(int(statistics.yearly_count.loc[year])),
            format_nap_value(float(statistics.yearly_max.loc[year])),
            format_nap_value(float(statistics.yearly_min.loc[year])),
        )

        pdf.set_font("Helvetica", "", 8)

        for index, (value, width) in enumerate(zip(values, widths)):
            last = index == len(values) - 1
            pdf.cell(
                width,
                7,
                encode_for_pdf(value),
                border=1,
                new_x=XPos.LMARGIN if last else XPos.RIGHT,
                new_y=YPos.NEXT if last else YPos.TOP,
                align="C",
            )


def add_outliers_to_pdf(
    pdf: PDFReport,
    statistics: GroundwaterStatistics,
) -> None:
    """Voeg de verwijderde uitschieters toe."""

    pdf.ln(5)
    pdf.section_title("Verwijderde uitschieters")

    if not statistics.removed_outliers:
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(
            0,
            8,
            "Er zijn geen uitschieters verwijderd.",
            border=1,
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
            align="C",
        )
        return

    widths = tuple(pdf.usable_width * value for value in (
        0.18,
        0.18,
        0.20,
        0.22,
        0.22,
    ))
    headers = (
        "Datum",
        "Hydr. jaar",
        "Meting",
        "Jaargemiddelde",
        "Afwijking",
    )

    def add_header() -> None:
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_fill_color(255, 220, 220)

        for index, (header, width) in enumerate(zip(headers, widths)):
            last = index == len(headers) - 1
            pdf.cell(
                width,
                8,
                encode_for_pdf(header),
                border=1,
                new_x=XPos.LMARGIN if last else XPos.RIGHT,
                new_y=YPos.NEXT if last else YPos.TOP,
                align="C",
                fill=True,
            )

    add_header()

    for outlier in statistics.removed_outliers:
        if pdf.get_y() + 9 > pdf.h - pdf.b_margin:
            pdf.add_page()
            pdf.section_title("Verwijderde uitschieters (vervolg)")
            add_header()

        values = (
            outlier.measurement_date.strftime("%d-%m-%Y"),
            str(outlier.hydrological_year),
            f"{outlier.measurement_value:.3f}",
            f"{outlier.yearly_mean:.3f}",
            f"{outlier.absolute_deviation:.3f}",
        )

        pdf.set_font("Helvetica", "", 7)

        for index, (value, width) in enumerate(zip(values, widths)):
            last = index == len(values) - 1
            pdf.cell(
                width,
                7,
                encode_for_pdf(value),
                border=1,
                new_x=XPos.LMARGIN if last else XPos.RIGHT,
                new_y=YPos.NEXT if last else YPos.TOP,
                align="C",
            )


def add_method_note_to_pdf(
    pdf: PDFReport,
    config: AppConfig,
) -> None:
    """Voeg een methodologische toelichting toe."""

    pdf.ln(5)
    pdf.section_title("Methodologische toelichting")
    pdf.set_font("Helvetica", "", 8)

    note = (
        "De weergegeven GHG- en GLG-waarden zijn proxywaarden. "
        "Per hydrologisch jaar worden metingen verwijderd waarvan de "
        "absolute afwijking ten opzichte van het jaargemiddelde groter "
        f"is dan {config.outlier_threshold_meters:.2f} meter. "
        "Een hydrologisch jaar wordt alleen gebruikt wanneer na "
        f"filtering minimaal {config.minimum_measurements_per_year} "
        "metingen overblijven. Vervolgens wordt per gebruikt jaar de "
        "hoogste en laagste gemeten waterstand bepaald. De uiteindelijke "
        "proxy is het gemiddelde van deze jaarlijkse waarden. Deze "
        "werkwijze is niet gelijk aan een formele GxG-bepaling."
    )

    pdf.multi_cell(
        0,
        5,
        encode_for_pdf(note),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )


def create_pdf_bytes(
    graph_bytes: bytes,
    filternummer: str,
    dataframe: pd.DataFrame,
    statistics: GroundwaterStatistics,
    config: AppConfig,
) -> bytes:
    """Genereer het volledige PDF-rapport in het geheugen."""

    pdf = PDFReport(filternummer, config)
    pdf.add_page()

    pdf.image(
        io.BytesIO(graph_bytes),
        x=pdf.l_margin,
        w=pdf.usable_width,
    )
    pdf.ln(3)

    add_summary_to_pdf(pdf, dataframe, statistics, config)
    add_yearly_table_to_pdf(pdf, statistics)
    add_outliers_to_pdf(pdf, statistics)
    add_method_note_to_pdf(pdf, config)

    output = pdf.output()

    if isinstance(output, bytearray):
        return bytes(output)

    if isinstance(output, bytes):
        return output

    return bytes(output)


# =============================================================================
# Verwerking
# =============================================================================


def process_uploaded_file(
    source_name: str,
    file_bytes: bytes,
    config: AppConfig,
) -> ProcessingResult:
    """Verwerk één geüpload CSV-bestand."""

    safe_name = sanitize_filename(source_name)
    logger, handler = create_logger(safe_name)

    try:
        validate_uploaded_bytes(file_bytes, safe_name)
        logger.info("Start verwerking van %s.", safe_name)

        text, encoding = decode_uploaded_file(
            file_bytes,
            safe_name,
            config,
        )
        metadata = find_csv_metadata_and_header(
            text,
            encoding,
            safe_name,
            config,
        )
        dataframe = load_and_prepare_data(
            text,
            metadata,
            safe_name,
            config,
            logger,
        )
        statistics, filtered_dataframe = calculate_ghg_glg_proxy(
            dataframe,
            config,
            logger,
        )
        graph_bytes = create_graph_bytes(
            dataframe,
            metadata.filternummer,
            statistics,
            config,
        )
        pdf_bytes = create_pdf_bytes(
            graph_bytes,
            metadata.filternummer,
            dataframe,
            statistics,
            config,
        )

        base_name = safe_name.rsplit(".", maxsplit=1)[0]
        output_name = f"{base_name}{config.output_suffix}"

        logger.info("Rapport %s is succesvol aangemaakt.", output_name)

        return ProcessingResult(
            source_name=safe_name,
            output_name=output_name,
            success=True,
            message="Rapport succesvol aangemaakt.",
            filternummer=metadata.filternummer,
            pdf_bytes=pdf_bytes,
            graph_bytes=graph_bytes,
            dataframe=dataframe,
            filtered_dataframe=filtered_dataframe,
            statistics=statistics,
            log_messages=handler.messages.copy(),
        )

    except Exception as exc:
        logger.exception("Verwerking mislukt: %s", exc)

        return ProcessingResult(
            source_name=safe_name,
            output_name=None,
            success=False,
            message=str(exc),
            log_messages=handler.messages.copy(),
        )


def create_zip_bytes(
    results: Sequence[ProcessingResult],
) -> bytes:
    """Bundel succesvol gemaakte PDF-rapporten in een ZIP-bestand."""

    buffer = io.BytesIO()

    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for result in results:
            if (
                result.success
                and result.output_name
                and result.pdf_bytes
            ):
                archive.writestr(
                    result.output_name,
                    result.pdf_bytes,
                )

    return buffer.getvalue()


def outliers_to_dataframe(
    records: Sequence[OutlierRecord],
) -> pd.DataFrame:
    """Converteer uitschieterrecords naar een DataFrame."""

    return pd.DataFrame(
        [
            {
                "Datum": record.measurement_date,
                "Hydrologisch jaar": record.hydrological_year,
                "Meting (m NAP)": record.measurement_value,
                "Jaargemiddelde (m NAP)": record.yearly_mean,
                "Afwijking (m)": record.absolute_deviation,
            }
            for record in records
        ]
    )


def yearly_statistics_to_dataframe(
    statistics: GroundwaterStatistics,
) -> pd.DataFrame:
    """Maak een presentatietabel van de jaarstatistieken."""

    return pd.DataFrame(
        [
            {
                "Hydrologisch jaar": year,
                "Aantal metingen": int(
                    statistics.yearly_count.loc[year]
                ),
                "Hoogste stand (m NAP)": float(
                    statistics.yearly_max.loc[year]
                ),
                "Laagste stand (m NAP)": float(
                    statistics.yearly_min.loc[year]
                ),
            }
            for year in statistics.reference_years
        ]
    )


# =============================================================================
# Streamlit-interface
# =============================================================================


def render_sidebar() -> AppConfig:
    """Toon instellingen en retourneer de geselecteerde configuratie."""

    with st.sidebar:
        st.header("Instellingen")

        delimiter = st.text_input(
            "CSV-scheidingsteken",
            value=DEFAULT_CONFIG.delimiter,
            max_chars=1,
            help="Meestal een puntkomma voor Nederlandse CSV-bestanden.",
        )

        start_month = st.selectbox(
            "Startmaand hydrologisch jaar",
            options=list(range(1, 13)),
            index=DEFAULT_CONFIG.hydrological_year_start_month - 1,
            format_func=lambda month: (
                f"{month:02d} - "
                f"{datetime(2000, month, 1).strftime('%B')}"
            ),
        )

        minimum_measurements = st.number_input(
            "Minimumaantal metingen per jaar",
            min_value=1,
            max_value=365,
            value=DEFAULT_CONFIG.minimum_measurements_per_year,
            step=1,
        )

        outlier_threshold = st.number_input(
            "Uitschieterdrempel in meter",
            min_value=0.0,
            max_value=100.0,
            value=DEFAULT_CONFIG.outlier_threshold_meters,
            step=0.05,
            format="%.2f",
        )

        graph_dpi = st.select_slider(
            "Grafiekresolutie",
            options=[100, 150, 200, 250, 300],
            value=DEFAULT_CONFIG.graph_dpi,
        )

        st.divider()
        st.caption(
            "De GHG- en GLG-waarden zijn proxywaarden en vormen geen "
            "formele GxG-bepaling."
        )

    return AppConfig(
        delimiter=delimiter or ";",
        hydrological_year_start_month=int(start_month),
        minimum_measurements_per_year=int(minimum_measurements),
        outlier_threshold_meters=float(outlier_threshold),
        graph_dpi=int(graph_dpi),
    )


def render_result(result: ProcessingResult) -> None:
    """Toon één verwerkingsresultaat."""

    with st.expander(
        f"{'✅' if result.success else '❌'} {result.source_name}",
        expanded=len(st.session_state.processing_results) == 1,
    ):
        if not result.success:
            st.error(result.message)

            with st.expander("Technische logmeldingen"):
                st.code(
                    "\n".join(result.log_messages),
                    language="text",
                )
            return

        statistics = result.statistics

        if statistics is None or result.dataframe is None:
            st.error("Het resultaat bevat geen statistiekgegevens.")
            return

        metric_columns = st.columns(5)

        metric_columns[0].metric(
            "Peilfilter",
            result.filternummer or "Onbekend",
        )
        metric_columns[1].metric(
            "Geldige metingen",
            statistics.original_measurement_count,
        )
        metric_columns[2].metric(
            "Uitschieters",
            statistics.removed_outlier_count,
        )
        metric_columns[3].metric(
            "GHG-proxy",
            (
                f"{statistics.ghg:.2f} m NAP"
                if statistics.ghg is not None
                else "Niet beschikbaar"
            ),
        )
        metric_columns[4].metric(
            "GLG-proxy",
            (
                f"{statistics.glg:.2f} m NAP"
                if statistics.glg is not None
                else "Niet beschikbaar"
            ),
        )

        tabs = st.tabs(
            [
                "Grafiek",
                "Jaarstatistieken",
                "Meetgegevens",
                "Uitschieters",
                "Logmeldingen",
            ]
        )

        with tabsif result.graph_bytes:
                st.image(
                    result.graph_bytes,
                    caption=(
                        f"Grondwaterstanden peilfilter "
                        f"{result.filternummer}"
                    ),
                    use_container_width=True,
                )

        with tabsyearly_df = yearly_statistics_to_dataframe(statistics)

            if yearly_df.empty:
                st.info(
                    "Er zijn geen hydrologische jaren met voldoende "
                    "metingen."
                )
            else:
                st.dataframe(
                    yearly_df,
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "Hoogste stand (m NAP)": st.column_config.NumberColumn(
                            format="%.3f"
                        ),
                        "Laagste stand (m NAP)": st.column_config.NumberColumn(
                            format="%.3f"
                        ),
                    },
                )

            if statistics.excluded_years:
                st.warning(
                    "Uitgesloten hydrologische jaren: "
                    + ", ".join(map(str, statistics.excluded_years))
                )

        with tabsdisplay_df = result.dataframe.copy()
            date_column = DEFAULT_CONFIG.expected_date_column

            if date_column in display_df.columns:
                display_df[date_column] = display_df[
                    date_column
                ].dt.strftime("%d-%m-%Y")

            st.dataframe(
                display_df,
                hide_index=True,
                use_container_width=True,
            )

        with tabsoutlier_df = outliers_to_dataframe(
                statistics.removed_outliers
            )

            if outlier_df.empty:
                st.success("Er zijn geen uitschieters verwijderd.")
            else:
                st.dataframe(
                    outlier_df,
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "Datum": st.column_config.DatetimeColumn(
                            format="DD-MM-YYYY"
                        ),
                        "Meting (m NAP)": st.column_config.NumberColumn(
                            format="%.3f"
                        ),
                        "Jaargemiddelde (m NAP)":
                            st.column_config.NumberColumn(format="%.3f"),
                        "Afwijking (m)": st.column_config.NumberColumn(
                            format="%.3f"
                        ),
                    },
                )

        with tabsst.code(
                "\n".join(result.log_messages),
                language="text",
            )

        if result.pdf_bytes and result.output_name:
            st.download_button(
                label=f"Download {result.output_name}",
                data=result.pdf_bytes,
                file_name=result.output_name,
                mime="application/pdf",
                key=f"download_{result.source_name}_{result.output_name}",
                type="primary",
            )


def main() -> None:
    """Start de Streamlit-applicatie."""

    st.title("💧 Grondwaterrapportage")
    st.write(
        "Upload één of meerdere CSV-bestanden met grondwaterstanden. "
        "De applicatie valideert de metingen, detecteert uitschieters, "
        "berekent GHG- en GLG-proxywaarden en genereert PDF-rapporten."
    )

    config = render_sidebar()

    with st.expander("Verwacht CSV-formaat"):
        st.markdown(
            """
De applicatie zoekt in het bestand naar een header die begint met:

```text
datum;meting NAP
