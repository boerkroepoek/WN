from __future__ import annotations

import hashlib
import io
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Sequence

import streamlit as st
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


# =============================================================================
# Configuratie
# =============================================================================

LOGGER = logging.getLogger(__name__)

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


APP_TITLE: Final[str] = "PDF-bijlagen samenvoegen"
APP_ICON: Final[str] = "📄"

DEFAULT_OUTPUT_FILENAME: Final[str] = "Bijlage_compleet.pdf"
DEFAULT_FONT_NAME: Final[str] = "Helvetica-Bold"
DEFAULT_FOOTER_FONT_NAME: Final[str] = "Helvetica"

DEFAULT_FONT_SIZE: Final[int] = 24
MIN_FONT_SIZE: Final[int] = 10
MAX_FONT_SIZE: Final[int] = 42

DEFAULT_MARGIN: Final[float] = 50.0
DEFAULT_MAX_TITLE_LINES: Final[int] = 5
DEFAULT_LINE_HEIGHT_FACTOR: Final[float] = 1.2

PDF_MIME_TYPE: Final[str] = "application/pdf"
MAX_UPLOAD_SIZE_MB: Final[int] = 200

INVALID_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class TitleAlignment(str, Enum):
    """Mogelijke horizontale uitlijningen van de titel."""

    LEFT = "Links"
    CENTER = "Gecentreerd"
    RIGHT = "Rechts"


class DocumentOrder(str, Enum):
    """Mogelijke manieren om documenten te sorteren."""

    UPLOAD = "Uploadvolgorde"
    FILENAME = "Bestandsnaam"


@dataclass(frozen=True)
class PdfDocument:
    """Een geüpload PDF-document inclusief metadata."""

    original_index: int
    filename: str
    title: str
    content: bytes

    @property
    def size_in_bytes(self) -> int:
        """Geef de bestandsgrootte in bytes terug."""
        return len(self.content)


@dataclass(frozen=True)
class CoverPageSettings:
    """Instellingen voor het genereren van voorbladen."""

    font_name: str = DEFAULT_FONT_NAME
    initial_font_size: int = DEFAULT_FONT_SIZE
    minimum_font_size: int = MIN_FONT_SIZE
    maximum_title_lines: int = DEFAULT_MAX_TITLE_LINES
    margin: float = DEFAULT_MARGIN
    line_height_factor: float = DEFAULT_LINE_HEIGHT_FACTOR
    alignment: TitleAlignment = TitleAlignment.LEFT
    show_filename: bool = False
    show_sequence_number: bool = False
    title_color_hex: str = "#000000"
    footer_text: str = ""


@dataclass(frozen=True)
class PdfValidationResult:
    """Resultaat van de validatie van een PDF-bestand."""

    is_valid: bool
    page_count: int = 0
    error_message: str | None = None


class PdfProcessingError(Exception):
    """Fout die optreedt tijdens het verwerken van PDF-bestanden."""


# =============================================================================
# Hulpfuncties
# =============================================================================

def configure_page() -> None:
    """Configureer de algemene Streamlit-pagina."""
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon=APP_ICON,
        layout="wide",
        initial_sidebar_state="expanded",
    )


def format_file_size(size_in_bytes: int) -> str:
    """
    Zet een bestandsgrootte om naar een leesbare tekst.

    Args:
        size_in_bytes: Bestandsgrootte in bytes.

    Returns:
        Leesbare bestandsgrootte.
    """
    units = ("B", "KB", "MB", "GB")
    size = float(size_in_bytes)

    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0

    return f"{size_in_bytes} B"


def title_from_filename(filename: str) -> str:
    """
    Maak een leesbare standaardtitel van een bestandsnaam.

    De extensie wordt verwijderd. Underscores worden vervangen door spaties.
    Koppeltekens blijven behouden omdat ze inhoudelijk betekenis kunnen hebben.

    Args:
        filename: Oorspronkelijke bestandsnaam.

    Returns:
        Voorgestelde documenttitel.
    """
    stem = Path(filename).stem
    normalized_title = re.sub(r"[_\s]+", " ", stem).strip()
    return normalized_title or "Document zonder titel"


def sanitize_output_filename(filename: str) -> str:
    """
    Maak een veilige bestandsnaam voor het downloadbestand.

    Args:
        filename: Door de gebruiker ingevoerde bestandsnaam.

    Returns:
        Veilige bestandsnaam met een .pdf-extensie.
    """
    cleaned_filename = INVALID_FILENAME_CHARACTERS.sub("_", filename).strip()
    cleaned_filename = cleaned_filename.rstrip(". ")

    if not cleaned_filename:
        cleaned_filename = DEFAULT_OUTPUT_FILENAME

    if not cleaned_filename.lower().endswith(".pdf"):
        cleaned_filename = f"{cleaned_filename}.pdf"

    return cleaned_filename


def normalize_hex_color(color_value: str) -> str:
    """
    Valideer en normaliseer een hexadecimale kleurwaarde.

    Args:
        color_value: Kleur in de vorm #RRGGBB.

    Returns:
        Genormaliseerde kleurwaarde.

    Raises:
        ValueError: Als de kleurwaarde ongeldig is.
    """
    normalized = color_value.strip().upper()

    if not re.fullmatch(r"#[0-9A-F]{6}", normalized):
        raise ValueError(
            "De titelkleur moet een hexadecimale kleur zijn, bijvoorbeeld #000000."
        )

    return normalized


def create_cache_key(
    documents: Sequence[PdfDocument],
    settings: CoverPageSettings,
) -> str:
    """
    Maak een hash voor caching op basis van bestanden, titels en instellingen.

    Args:
        documents: Te verwerken PDF-documenten.
        settings: Instellingen voor de voorbladen.

    Returns:
        SHA-256-hash.
    """
    digest = hashlib.sha256()

    for document in documents:
        digest.update(document.filename.encode("utf-8"))
        digest.update(document.title.encode("utf-8"))
        digest.update(document.content)

    digest.update(repr(settings).encode("utf-8"))
    return digest.hexdigest()


# =============================================================================
# PDF-validatie
# =============================================================================

def validate_pdf(pdf_content: bytes) -> PdfValidationResult:
    """
    Controleer of bytes een leesbaar en niet-versleuteld PDF-bestand vormen.

    Args:
        pdf_content: Inhoud van het PDF-bestand.

    Returns:
        Validatieresultaat.
    """
    if not pdf_content:
        return PdfValidationResult(
            is_valid=False,
            error_message="Het bestand is leeg.",
        )

    if not pdf_content.lstrip().startswith(b"%PDF-"):
        return PdfValidationResult(
            is_valid=False,
            error_message="Het bestand heeft geen geldige PDF-header.",
        )

    try:
        reader = PdfReader(io.BytesIO(pdf_content), strict=False)

        if reader.is_encrypted:
            try:
                decrypt_result = reader.decrypt("")
            except Exception:
                decrypt_result = 0

            if decrypt_result == 0:
                return PdfValidationResult(
                    is_valid=False,
                    error_message=(
                        "Het PDF-bestand is met een wachtwoord beveiligd en "
                        "kan niet worden verwerkt."
                    ),
                )

        page_count = len(reader.pages)

        if page_count == 0:
            return PdfValidationResult(
                is_valid=False,
                error_message="Het PDF-bestand bevat geen pagina's.",
            )

        return PdfValidationResult(
            is_valid=True,
            page_count=page_count,
        )

    except (PdfReadError, ValueError, TypeError, OSError) as exc:
        LOGGER.warning("Ongeldig PDF-bestand aangetroffen: %s", exc)
        return PdfValidationResult(
            is_valid=False,
            error_message=f"Het PDF-bestand kan niet worden gelezen: {exc}",
        )
    except Exception as exc:
        LOGGER.exception("Onverwachte fout tijdens PDF-validatie.")
        return PdfValidationResult(
            is_valid=False,
            error_message=f"Onverwachte fout bij het lezen van de PDF: {exc}",
        )


# =============================================================================
# Voorblad genereren
# =============================================================================

def split_title_to_fit(
    title: str,
    settings: CoverPageSettings,
    max_text_width: float,
    max_text_height: float,
) -> tuple[list[str], int]:
    """
    Bepaal regels en lettergrootte zodat de titel op het voorblad past.

    Args:
        title: Titel die op het voorblad moet komen.
        settings: Instellingen voor het voorblad.
        max_text_width: Maximale beschikbare tekstbreedte.
        max_text_height: Maximale beschikbare teksthoogte.

    Returns:
        Tuple met titelregels en de gekozen lettergrootte.
    """
    cleaned_title = " ".join(title.split()).strip() or "Document zonder titel"
    selected_lines: list[str] = [cleaned_title]
    selected_font_size = settings.minimum_font_size

    for font_size in range(
        settings.initial_font_size,
        settings.minimum_font_size - 1,
        -1,
    ):
        lines = simpleSplit(
            cleaned_title,
            settings.font_name,
            font_size,
            max_text_width,
        )
        line_height = font_size * settings.line_height_factor
        total_height = len(lines) * line_height

        selected_lines = lines
        selected_font_size = font_size

        if (
            len(lines) <= settings.maximum_title_lines
            and total_height <= max_text_height
        ):
            return lines, font_size

    return selected_lines, selected_font_size


def calculate_text_x(
    line: str,
    page_width: float,
    margin: float,
    font_name: str,
    font_size: int,
    alignment: TitleAlignment,
) -> float:
    """
    Bereken de horizontale positie van een titelregel.

    Args:
        line: Tekstregel.
        page_width: Breedte van de pagina.
        margin: Paginamarge.
        font_name: Naam van het lettertype.
        font_size: Lettergrootte.
        alignment: Gewenste uitlijning.

    Returns:
        Horizontale x-positie.
    """
    line_width = stringWidth(line, font_name, font_size)

    if alignment == TitleAlignment.CENTER:
        return max(margin, (page_width - line_width) / 2)

    if alignment == TitleAlignment.RIGHT:
        return max(margin, page_width - margin - line_width)

    return margin


def create_cover_page(
    document: PdfDocument,
    settings: CoverPageSettings,
    sequence_number: int,
    total_documents: int,
) -> bytes:
    """
    Genereer één PDF-voorblad in het geheugen.

    Args:
        document: Document waarvoor het voorblad wordt gemaakt.
        settings: Instellingen voor het voorblad.
        sequence_number: Volgnummer van het document.
        total_documents: Totaal aantal documenten.

    Returns:
        Bytes van het gegenereerde PDF-voorblad.
    """
    output_buffer = io.BytesIO()
    pdf_canvas = canvas.Canvas(output_buffer, pagesize=A4)
    page_width, page_height = A4

    footer_area_height = 65.0 if (
        settings.show_filename or settings.footer_text
    ) else 20.0

    max_text_width = page_width - (2 * settings.margin)
    max_text_height = page_height - (
        2 * settings.margin
    ) - footer_area_height

    display_title = document.title
    if settings.show_sequence_number:
        display_title = (
            f"{sequence_number}. {document.title}"
        )

    title_lines, font_size = split_title_to_fit(
        title=display_title,
        settings=settings,
        max_text_width=max_text_width,
        max_text_height=max_text_height,
    )

    title_color: Color = HexColor(settings.title_color_hex)
    pdf_canvas.setFillColor(title_color)
    pdf_canvas.setFont(settings.font_name, font_size)

    line_height = font_size * settings.line_height_factor
    title_block_height = len(title_lines) * line_height

    # De eerste baseline staat één regelhoogte onder de bovenmarge.
    start_y = page_height - settings.margin - line_height

    # Extra veiligheid als een uitzonderlijk lange titel op minimale
    # lettergrootte nog steeds te veel verticale ruimte inneemt.
    minimum_y = settings.margin + footer_area_height
    if start_y - title_block_height < minimum_y:
        start_y = page_height - settings.margin - line_height

    for line_index, line in enumerate(title_lines):
        text_x = calculate_text_x(
            line=line,
            page_width=page_width,
            margin=settings.margin,
            font_name=settings.font_name,
            font_size=font_size,
            alignment=settings.alignment,
        )
        text_y = start_y - (line_index * line_height)
        pdf_canvas.drawString(text_x, text_y, line)

    pdf_canvas.setFillColor(HexColor("#666666"))
    pdf_canvas.setFont(DEFAULT_FOOTER_FONT_NAME, 9)

    footer_y = settings.margin

    if settings.show_filename:
        filename_text = f"Bestand: {document.filename}"
        filename_lines = simpleSplit(
            filename_text,
            DEFAULT_FOOTER_FONT_NAME,
            9,
            max_text_width,
        )

        for line_index, line in enumerate(filename_lines[:2]):
            pdf_canvas.drawString(
                settings.margin,
                footer_y + 14 + (line_index * 11),
                line,
            )

    if settings.footer_text.strip():
        footer_lines = simpleSplit(
            settings.footer_text.strip(),
            DEFAULT_FOOTER_FONT_NAME,
            9,
            max_text_width,
        )

        for line_index, line in enumerate(footer_lines[:2]):
            pdf_canvas.drawString(
                settings.margin,
                footer_y - (line_index * 11),
                line,
            )

    pdf_canvas.drawRightString(
        page_width - settings.margin,
        footer_y,
        f"Bijlage {sequence_number} van {total_documents}",
    )

    pdf_canvas.showPage()
    pdf_canvas.save()

    output_buffer.seek(0)
    return output_buffer.getvalue()


# =============================================================================
# PDF's samenvoegen
# =============================================================================

def append_pdf_to_writer(
    writer: PdfWriter,
    pdf_content: bytes,
    source_name: str,
) -> int:
    """
    Voeg alle pagina's van een PDF toe aan een PdfWriter.

    Args:
        writer: Writer waaraan de pagina's worden toegevoegd.
        pdf_content: PDF-inhoud als bytes.
        source_name: Naam van de bron voor foutmeldingen.

    Returns:
        Aantal toegevoegde pagina's.

    Raises:
        PdfProcessingError: Als de PDF niet kan worden toegevoegd.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_content), strict=False)

        if reader.is_encrypted:
            try:
                decrypt_result = reader.decrypt("")
            except Exception as exc:
                raise PdfProcessingError(
                    f"'{source_name}' is versleuteld en kan niet worden geopend."
                ) from exc

            if decrypt_result == 0:
                raise PdfProcessingError(
                    f"'{source_name}' is met een wachtwoord beveiligd."
                )

        page_count = 0

        for page in reader.pages:
            writer.add_page(page)
            page_count += 1

        return page_count

    except PdfProcessingError:
        raise
    except Exception as exc:
        raise PdfProcessingError(
            f"PDF '{source_name}' kon niet worden toegevoegd: {exc}"
        ) from exc


def merge_documents(
    documents: Sequence[PdfDocument],
    settings: CoverPageSettings,
) -> tuple[bytes, int]:
    """
    Maak voorbladen en voeg alle documenten samen.

    De volgorde per document is:
    1. gegenereerd voorblad;
    2. alle pagina's uit het oorspronkelijke PDF-bestand.

    Args:
        documents: Documenten in de gewenste volgorde.
        settings: Instellingen voor de voorbladen.

    Returns:
        Tuple met het samengevoegde PDF-bestand en het totale aantal pagina's.

    Raises:
        PdfProcessingError: Als geen documenten zijn aangeleverd of verwerking
            mislukt.
    """
    if not documents:
        raise PdfProcessingError("Er zijn geen PDF-documenten geselecteerd.")

    writer = PdfWriter()
    total_page_count = 0
    total_documents = len(documents)

    try:
        for sequence_number, document in enumerate(documents, start=1):
            LOGGER.info(
                "Document verwerken: %s (%s)",
                document.filename,
                format_file_size(document.size_in_bytes),
            )

            cover_content = create_cover_page(
                document=document,
                settings=settings,
                sequence_number=sequence_number,
                total_documents=total_documents,
            )

            total_page_count += append_pdf_to_writer(
                writer=writer,
                pdf_content=cover_content,
                source_name=f"Voorblad voor {document.filename}",
            )

            total_page_count += append_pdf_to_writer(
                writer=writer,
                pdf_content=document.content,
                source_name=document.filename,
            )

            # Voeg een bladwijzer toe die verwijst naar het voorblad.
            cover_page_index = total_page_count - (
                validate_page_count(document.content) + 1
            )

            try:
                writer.add_outline_item(
                    title=document.title,
                    page_number=cover_page_index,
                )
            except Exception:
                # Bladwijzers zijn een nuttige extra, maar mogen de volledige
                # PDF-verwerking niet laten mislukken.
                LOGGER.warning(
                    "Bladwijzer voor '%s' kon niet worden toegevoegd.",
                    document.filename,
                )

        output_buffer = io.BytesIO()
        writer.write(output_buffer)
        output_buffer.seek(0)

        result = output_buffer.getvalue()

        if not result:
            raise PdfProcessingError(
                "Het gegenereerde PDF-bestand is onverwacht leeg."
            )

        # Eindcontrole om te voorkomen dat een beschadigd resultaat wordt
        # aangeboden.
        final_validation = validate_pdf(result)
        if not final_validation.is_valid:
            raise PdfProcessingError(
                "Het eindbestand kon niet worden gevalideerd: "
                f"{final_validation.error_message}"
            )

        return result, final_validation.page_count

    except PdfProcessingError:
        raise
    except Exception as exc:
        LOGGER.exception("Onverwachte fout tijdens het samenvoegen.")
        raise PdfProcessingError(
            f"Het samenvoegen van de PDF-bestanden is mislukt: {exc}"
        ) from exc
    finally:
        writer.close()


def validate_page_count(pdf_content: bytes) -> int:
    """
    Bepaal het aantal pagina's van een reeds gevalideerde PDF.

    Args:
        pdf_content: PDF-inhoud.

    Returns:
        Aantal pagina's.
    """
    reader = PdfReader(io.BytesIO(pdf_content), strict=False)

    if reader.is_encrypted:
        reader.decrypt("")

    return len(reader.pages)


# =============================================================================
# Streamlit-interface
# =============================================================================

def render_sidebar() -> tuple[
    DocumentOrder,
    CoverPageSettings,
    str,
]:
    """
    Toon configuratieopties in de zijbalk.

    Returns:
        Gekozen sorteervolgorde, voorbladinstellingen en uitvoerbestandsnaam.
    """
    with st.sidebar:
        st.header("Instellingen")

        output_filename_input = st.text_input(
            "Naam van het uitvoerbestand",
            value=DEFAULT_OUTPUT_FILENAME,
            help="De extensie .pdf wordt automatisch toegevoegd.",
        )
        output_filename = sanitize_output_filename(output_filename_input)

        order_label = st.selectbox(
            "Volgorde van de documenten",
            options=[order.value for order in DocumentOrder],
            index=0,
        )
        document_order = DocumentOrder(order_label)

        st.subheader("Voorblad")

        initial_font_size = st.slider(
            "Maximale titelgrootte",
            min_value=MIN_FONT_SIZE,
            max_value=MAX_FONT_SIZE,
            value=DEFAULT_FONT_SIZE,
            step=1,
        )

        maximum_title_lines = st.slider(
            "Maximaal aantal titelregels",
            min_value=1,
            max_value=10,
            value=DEFAULT_MAX_TITLE_LINES,
            step=1,
        )

        alignment_label = st.selectbox(
            "Uitlijning van de titel",
            options=[alignment.value for alignment in TitleAlignment],
            index=0,
        )
        alignment = TitleAlignment(alignment_label)

        title_color_hex = st.color_picker(
            "Titelkleur",
            value="#000000",
        )

        show_sequence_number = st.checkbox(
            "Nummer voor de titel tonen",
            value=False,
            help="Voorbeeld: 1. Documenttitel",
        )

        show_filename = st.checkbox(
            "Oorspronkelijke bestandsnaam tonen",
            value=False,
        )

        footer_text = st.text_input(
            "Extra voettekst",
            value="",
            placeholder="Bijvoorbeeld: Vertrouwelijk",
        )

        st.divider()
        st.caption(
            "De bestanden worden tijdens deze sessie in het geheugen verwerkt. "
            "De app schrijft geen tijdelijke voorbladen naar een lokale map."
        )

    settings = CoverPageSettings(
        initial_font_size=initial_font_size,
        maximum_title_lines=maximum_title_lines,
        alignment=alignment,
        show_filename=show_filename,
        show_sequence_number=show_sequence_number,
        title_color_hex=normalize_hex_color(title_color_hex),
        footer_text=footer_text,
    )

    return document_order, settings, output_filename


def build_documents_from_uploads(
    uploaded_files: Sequence[st.runtime.uploaded_file_manager.UploadedFile],
    document_order: DocumentOrder,
) -> tuple[list[PdfDocument], list[str], int]:
    """
    Valideer uploads en maak PdfDocument-objecten.

    Args:
        uploaded_files: Door Streamlit ontvangen bestanden.
        document_order: Gewenste documentvolgorde.

    Returns:
        Geldige documenten, foutmeldingen en totaal aantal bronpagina's.
    """
    upload_data: list[tuple[int, str, bytes]] = []

    for original_index, uploaded_file in enumerate(uploaded_files):
        upload_data.append(
            (
                original_index,
                uploaded_file.name,
                uploaded_file.getvalue(),
            )
        )

    if document_order == DocumentOrder.FILENAME:
        upload_data.sort(key=lambda item: item[1].casefold())
    else:
        upload_data.sort(key=lambda item: item[0])

    documents: list[PdfDocument] = []
    errors: list[str] = []
    total_source_pages = 0

    for original_index, filename, content in upload_data:
        validation = validate_pdf(content)

        if not validation.is_valid:
            errors.append(
                f"**{filename}**: {validation.error_message}"
            )
            continue

        title_key = f"title_{original_index}_{hashlib.md5(content).hexdigest()}"

        if title_key not in st.session_state:
            st.session_state[title_key] = title_from_filename(filename)

        document = PdfDocument(
            original_index=original_index,
            filename=filename,
            title=st.session_state[title_key],
            content=content,
        )
        documents.append(document)
        total_source_pages += validation.page_count

    return documents, errors, total_source_pages


def render_title_editors(
    documents: Sequence[PdfDocument],
) -> list:
    """
    Toon per document een titelveld.

    Args:
        documents: Geldige PDF-documenten.

    Returns:
        Nieuwe documentlijst met aangepaste titels.
    """
    st.subheader("Documenttitels en volgorde")
    st.caption(
        "De onderstaande volgorde wordt gebruikt in het samengevoegde bestand. "
        "Pas desgewenst de titel van ieder voorblad aan."
    )

    updated_documents: list[PdfDocument] = []

    for sequence_number, document in enumerate(documents, start=1):
        content_hash = hashlib.md5(document.content).hexdigest()
        title_key = f"title_{document.original_index}_{content_hash}"

        column_number, column_title, column_metadata = st.columns(
            [0.5, 4.5, 2.0],
            vertical_alignment="center",
        )

        with column_number:
            st.markdown(f"**{sequence_number}.**")

        with column_title:
            edited_title = st.text_input(
                label=f"Titel voor {document.filename}",
                key=title_key,
                label_visibility="collapsed",
                placeholder="Documenttitel",
            ).strip()

        with column_metadata:
            page_count = validate_page_count(document.content)
            st.caption(
                f"{page_count} pagina"
                f"{'' if page_count == 1 else 's'} · "
                f"{format_file_size(document.size_in_bytes)}"
            )

        updated_documents.append(
            PdfDocument(
                original_index=document.original_index,
                filename=document.filename,
                title=edited_title or title_from_filename(document.filename),
                content=document.content,
            )
        )

    return updated_documents


def render_summary(
    documents: Sequence[PdfDocument],
    total_source_pages: int,
) -> None:
    """
    Toon een samenvatting van de geselecteerde documenten.

    Args:
        documents: Geldige documenten.
        total_source_pages: Totaal aantal pagina's zonder voorbladen.
    """
    total_size = sum(document.size_in_bytes for document in documents)
    expected_page_count = total_source_pages + len(documents)

    metric_documents, metric_source_pages, metric_result_pages, metric_size = (
        st.columns(4)
    )

    metric_documents.metric("Documenten", len(documents))
    metric_source_pages.metric("Bronpagina's", total_source_pages)
    metric_result_pages.metric(
        "Verwachte pagina's",
        expected_page_count,
        help="Iedere PDF krijgt één extra voorblad.",
    )
    metric_size.metric("Totale upload", format_file_size(total_size))


def initialize_result_state() -> None:
    """Initialiseer de sessiestatus voor het gegenereerde resultaat."""
    defaults = {
        "result_cache_key": None,
        "result_pdf": None,
        "result_page_count": None,
        "result_filename": None,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def main() -> None:
    """Start de Streamlit-applicatie."""
    configure_page()
    initialize_result_state()

    st.title(f"{APP_ICON} {APP_TITLE}")
    st.write(
        "Upload meerdere PDF-bestanden. De app maakt voor ieder document een "
        "voorblad en bundelt alles in één downloadbaar PDF-bestand."
    )

    document_order, settings, output_filename = render_sidebar()

    uploaded_files = st.file_uploader(
        "Selecteer de PDF-bijlagen",
        type=["pdf"],
        accept_multiple_files=True,
        help=(
            "Je kunt meerdere PDF-bestanden tegelijk selecteren. "
            f"Houd rekening met de ingestelde Streamlit-uploadlimiet. "
            f"De aanbevolen maximale grootte per bestand is "
            f"{MAX_UPLOAD_SIZE_MB} MB."
        ),
    )

    if not uploaded_files:
        st.info(
            "Upload één of meer PDF-bestanden om de verwerking te starten."
        )
        return

    documents, validation_errors, total_source_pages = (
        build_documents_from_uploads(
            uploaded_files=uploaded_files,
            document_order=document_order,
        )
    )

    if validation_errors:
        st.error(
            "Een of meer bestanden kunnen niet worden verwerkt:"
        )
        for validation_error in validation_errors:
            st.markdown(f"- {validation_error}")

    if not documents:
        st.warning("Er zijn geen geldige PDF-bestanden om samen te voegen.")
        return

    documents = render_title_editors(documents)
    render_summary(documents, total_source_pages)

    st.divider()

    current_cache_key = create_cache_key(
        documents=documents,
        settings=settings,
    )

    if (
        st.session_state.result_cache_key is not None
        and st.session_state.result_cache_key != current_cache_key
    ):
        st.session_state.result_pdf = None
        st.session_state.result_page_count = None
        st.session_state.result_filename = None

    generate_button = st.button(
        "Voorbladen maken en PDF's samenvoegen",
        type="primary",
        use_container_width=True,
    )

    if generate_button:
        try:
            with st.spinner("PDF-bestanden worden verwerkt..."):
                merged_pdf, final_page_count = merge_documents(
                    documents=documents,
                    settings=settings,
                )

            st.session_state.result_cache_key = current_cache_key
            st.session_state.result_pdf = merged_pdf
            st.session_state.result_page_count = final_page_count
            st.session_state.result_filename = output_filename

            st.success(
                f"Het PDF-bestand is succesvol gemaakt. "
                f"Het resultaat bevat {final_page_count} pagina's."
            )

        except PdfProcessingError as exc:
            LOGGER.exception("PDF-verwerking mislukt.")
            st.error(str(exc))
            st.session_state.result_pdf = None
            st.session_state.result_page_count = None
            st.session_state.result_filename = None
        except Exception as exc:
            LOGGER.exception("Onverwachte applicatiefout.")
            st.error(
                "Er is een onverwachte fout opgetreden tijdens de verwerking: "
                f"{exc}"
            )
            st.session_state.result_pdf = None
            st.session_state.result_page_count = None
            st.session_state.result_filename = None

    if (
        st.session_state.result_pdf is not None
        and st.session_state.result_cache_key == current_cache_key
    ):
        result_size = format_file_size(
            len(st.session_state.result_pdf)
        )

        st.download_button(
            label=(
                f"Download {st.session_state.result_filename} "
                f"({result_size})"
            ),
            data=st.session_state.result_pdf,
            file_name=st.session_state.result_filename,
            mime=PDF_MIME_TYPE,
            type="primary",
            use_container_width=True,
        )

        st.caption(
            f"Definitief resultaat: "
            f"{st.session_state.result_page_count} pagina's, {result_size}."
        )


if __name__ == "__main__":
    main()
