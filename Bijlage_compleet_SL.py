import streamlit as st
import os
import re
import io
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import simpleSplit
from PyPDF2 import PdfReader, PdfWriter, errors

# --- DE FUNCTIE VOOR HET VOORBLAD IS ONGEWIJZIGD ---
# Deze functie werkte al perfect 'in-memory'.
def create_cover_page_in_memory(document_name):
    """
    Maakt een voorblad PDF direct in het geheugen en geeft een PdfReader object terug.
    """
    packet = io.BytesIO()  # Creëer een 'in-memory' binair bestand
    c = canvas.Canvas(packet, pagesize=A4)
    page_width, page_height = A4

    margin = 50
    max_text_width = page_width - (2 * margin)
    font_name = "Helvetica-Bold"
    font_size = 24

    # Dynamisch de tekst en lettergrootte aanpassen
    text_lines = simpleSplit(document_name, font_name, font_size, max_text_width)
    while len(text_lines) * (font_size * 1.2) > (page_height - 2 * margin) and font_size > 10:
        font_size -= 1
        text_lines = simpleSplit(document_name, font_name, font_size, max_text_width)
    
    c.setFont(font_name, font_size)
    line_height = font_size * 1.2
    
    # Bepaal startpositie voor linksboven
    start_y = page_height - margin - font_size
    text_x = margin

    # Teken elke regel linksboven, beginnend bij de marge
    for line in text_lines:
        c.drawString(text_x, start_y, line)
        start_y -= line_height

    c.save()
    packet.seek(0)  # Herwind de 'in-memory file' naar het begin
    return PdfReader(packet)

# --- HELPER FUNCTIE VOOR SORTEREN (NU OP TOP-LEVEL) ---
def natural_sort_key(s):
    """Sorteert tekst met getallen op een 'natuurlijke' manier."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

# --- AANGEPASTE VERWERKINGSFUNCTIE VOOR STREAMLIT ---
def process_uploaded_pdfs(uploaded_files):
    """
    Verwerkt geüploade Streamlit-bestanden: maakt in-memory voorbladen,
    voegt ze samen en geeft een in-memory BytesIO-buffer terug.
    """
    if not uploaded_files:
        st.warning("❌ Geen PDF-bestanden geüpload.")
        return None

    # Sorteer de geüploade bestanden op hun bestandsnaam
    try:
        uploaded_files.sort(key=lambda f: natural_sort_key(f.name))
    except Exception as e:
        st.error(f"Fout bij sorteren van bestanden: {e}")
        # Ga toch door, maar ongesorteerd
        pass
    
    pdf_writer = PdfWriter()
    output_filename = "Bijlage_compleet.pdf"
    
    st.info(f"📄 Start met het samenvoegen van {len(uploaded_files)} PDF-bestand(en)...")
    
    processed_count = 0
    # Een placeholder om de status-updates in te tonen
    status_placeholder = st.empty()

    for uploaded_file in uploaded_files:
        filename = uploaded_file.name
        if filename.lower() == output_filename.lower():
            continue  # Sla het eventuele output-bestand zelf over

        try:
            status_placeholder.text(f"   ➕ Verwerken: {filename}")
            document_name = os.path.splitext(filename)[0]
            
            # Stap 1: Maak voorblad in het geheugen
            cover_reader = create_cover_page_in_memory(document_name)
            pdf_writer.append(fileobj=cover_reader)

            # Stap 2: Voeg het originele PDF-bestand toe
            # Reset de 'file pointer' van het geüploade bestand voor zekerheid
            uploaded_file.seek(0)
            pdf_writer.append(uploaded_file)
            processed_count += 1

        except errors.PdfReadError:
            st.warning(f"   ⚠️ Waarschuwing: Kon '{filename}' niet lezen. Het bestand is mogelijk corrupt en wordt overgeslagen.")
        except Exception as e:
            st.error(f"   ❌ Een onverwachte fout is opgetreden bij het verwerken van '{filename}': {e}")

    status_placeholder.empty()  # Wis de "Verwerken..." tekst

    if processed_count == 0:
        st.error("Geen bestanden konden succesvol worden samengevoegd.")
        return None

    # Schrijf het eindresultaat naar een in-memory buffer
    output_buffer = io.BytesIO()
    pdf_writer.write(output_buffer)
    # Belangrijk: zet de buffer terug naar het begin zodat de download-knop het kan lezen
    output_buffer.seek(0) 
    
    st.success(f"✅ Klaar! {processed_count} PDF('s) zijn samengevoegd.")
    return output_buffer

# --- DE STREAMLIT USER INTERFACE (VERVANGT `if __name__ == "__main__":`) ---

st.set_page_config(page_title="PDF Samenvoeger", page_icon="📄")
st.title("📄 PDF Voorblad & Samenvoeg Tool")
st.write("""
Upload hier uw PDF-bestanden. Deze tool zal:
1.  Voor elke PDF een voorblad maken met de bestandsnaam.
2.  Alle PDF's (met hun nieuwe voorblad) in de juiste volgorde samenvoegen.
3.  Eén gecombineerd PDF-bestand aanbieden als download.
""")

uploaded_files = st.file_uploader(
    "Sleep uw PDF-bestanden hierheen (of klik om te bladeren)",
    type="pdf",
    accept_multiple_files=True
)

# Toon de knop alleen als er bestanden zijn geüpload
if uploaded_files:
    if st.button("Verwerk en voeg samen"):
        # De 'spinner' toont een laad-icoon zolang de verwerking duurt
        with st.spinner("PDF's worden verwerkt... Dit kan even duren."):
            final_pdf_data = process_uploaded_pdfs(uploaded_files)
        
        # Als de verwerking succesvol was en data teruggaf
        if final_pdf_data:
            st.download_button(
                label="Download 'Bijlage_compleet.pdf'",
                data=final_pdf_data,
                file_name="Bijlage_compleet.pdf",
                mime="application/pdf"
            )
else:
    st.info("Wachtend op PDF-bestanden...")