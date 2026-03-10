import streamlit as st
import tempfile
import os
from pathlib import Path
from geolib.models.dstability.dstability_model import DStabilityModel

# --- Pagina Instellingen ---
st.set_page_config(page_title="D-Stability Spiegel App", page_icon="🪞")
st.title("🪞 D-Stability Spiegel Tool")
st.write("Upload een `.stix` bestand. Deze app spiegelt automatisch de hele geometrie (inclusief alle achterliggende waterlijnen en scenario's) om de Y-as, zodat je direct een symmetrische som kunt downloaden.")

# --- De Recursieve Spiegel Functie ---
def spiegel_alles_recursief(obj, bezocht=None):
    if bezocht is None:
        bezocht = set()
        
    if id(obj) in bezocht:
        return 0
    bezocht.add(id(obj))
    
    aantal = 0
    
    if isinstance(obj, list):
        for item in obj:
            aantal += spiegel_alles_recursief(item, bezocht)
        return aantal

    if isinstance(obj, dict):
        for val in obj.values():
            aantal += spiegel_alles_recursief(val, bezocht)
        return aantal

    if hasattr(obj, '__dict__') or hasattr(obj, '__fields__'):
        # 1. Spiegel de X-coördinaten
        if hasattr(obj, 'X') and hasattr(obj, 'Z') and isinstance(getattr(obj, 'X'), (int, float)):
            obj.X = -obj.X
            aantal += 1
        elif hasattr(obj, 'x') and hasattr(obj, 'z') and isinstance(getattr(obj, 'x'), (int, float)):
            obj.x = -obj.x
            aantal += 1
            
        for grid_param in ['XLeft', 'XRight']:
            if hasattr(obj, grid_param) and isinstance(getattr(obj, grid_param), (int, float)):
                setattr(obj, grid_param, -getattr(obj, grid_param))

        # 2. Draai de lijsten om (voorkomt binnenstebuiten polygonen)
        for list_name in ['Points', 'points', 'PointIds', 'pointids']:
            if hasattr(obj, list_name):
                lst = getattr(obj, list_name)
                if isinstance(lst, list):
                    lst.reverse()
                    
        # 3. Graaf dieper in het object
        try:
            attributen = dir(obj)
        except Exception:
            attributen = []
            
        for attr in attributen:
            if attr.startswith('_') or callable(getattr(obj, attr, None)):
                continue
            try:
                val = getattr(obj, attr)
                if isinstance(val, (list, dict)) or hasattr(val, '__dict__') or hasattr(val, '__fields__'):
                    aantal += spiegel_alles_recursief(val, bezocht)
            except Exception:
                pass
                
    return aantal

# --- File Uploader ---
st.markdown("---")
uploaded_file = st.file_uploader("Kies jouw D-Stability bestand", type=["stix"])

if uploaded_file is not None:
    # Bepaal de nieuwe bestandsnaam
    oorspronkelijke_naam = uploaded_file.name
    nieuwe_naam = oorspronkelijke_naam.replace(".stix", "_GESPIEGELD.stix")
    
    with st.spinner("Bestand aan het analyseren en spiegelen..."):
        # Maak tijdelijke bestanden aan voor geolib om mee te werken
        with tempfile.NamedTemporaryFile(delete=False, suffix=".stix") as tmp_in:
            tmp_in.write(uploaded_file.getvalue())
            input_pad = Path(tmp_in.name)
            
        output_pad = input_pad.with_name(f"{input_pad.stem}_out.stix")

        try:
            # 1. Inladen
            dm = DStabilityModel()
            dm.parse(input_pad)
            
            # 2. Spiegelen
            totaal_aangepast = spiegel_alles_recursief(dm.datastructure)
            
            # 3. Opslaan naar tijdelijk output bestand
            dm.serialize(output_pad)
            
            # 4. Bestand inlezen voor de download knop
            with open(output_pad, "rb") as f:
                processed_stix_data = f.read()
                
            # Ruim de tijdelijke bestanden netjes op
            os.remove(input_pad)
            os.remove(output_pad)
            
            # --- Succesmelding en Download Knop ---
            st.success(f"✅ Gelukt! Er zijn {totaal_aangepast} datapunten gevonden en gespiegeld.")
            
            st.download_button(
                label="📥 Download Gespiegelde .stix",
                data=processed_stix_data,
                file_name=nieuwe_naam,
                mime="application/octet-stream"
            )

        except Exception as e:
            st.error(f"Er is een fout opgetreden bij het verwerken: {e}")
