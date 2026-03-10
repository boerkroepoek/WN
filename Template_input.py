import streamlit as st
import tempfile
from pathlib import Path
from geolib.models.dstability.dstability_model import DStabilityModel

# --- Pagina Instellingen ---
st.set_page_config(page_title="D-Stability Spiegel App", page_icon="🪞")
st.title("🪞 D-Stability Spiegel Tool")
st.write("Upload een `.stix` bestand. Deze app spiegelt automatisch de hele geometrie (inclusief alle achterliggende waterlijnen en scenario's) om de Y-as.")

# --- De Recursieve Spiegel Functie ---
# def spiegel_alles_recursief(obj, bezocht=None):
#     if bezocht is None:
#         bezocht = set()
        
#     if id(obj) in bezocht:
#         return 0
#     bezocht.add(id(obj))
    
#     # Lijsten en Dictionaries doorlopen
#     if isinstance(obj, list):
#         return sum(spiegel_alles_recursief(item, bezocht) for item in obj)
#     if isinstance(obj, dict):
#         return sum(spiegel_alles_recursief(val, bezocht) for val in obj.values())

#     aantal = 0
#     if hasattr(obj, '__dict__') or hasattr(obj, '__fields__'):
        
#         # 1. Spiegel de X-coördinaten en grid-parameters
#         for attr in ['X', 'x', 'XLeft', 'XRight']:
#             if hasattr(obj, attr):
#                 waarde = getattr(obj, attr)
#                 if isinstance(waarde, (int, float)):
#                     setattr(obj, attr, -waarde)
#                     if attr in ['X', 'x']: 
#                         aantal += 1

#         # 2. Draai de lijsten om (voorkomt binnenstebuiten polygonen)
#         for list_name in ['Points', 'points', 'PointIds', 'pointids']:
#             if hasattr(obj, list_name):
#                 lst = getattr(obj, list_name)
#                 if isinstance(lst, list):
#                     lst.reverse()
                    
#         # 3. Graaf dieper in alle andere attributen
#         for attr in dir(obj):
#             if not attr.startswith('_'):
#                 try:
#                     val = getattr(obj, attr)
#                     if not callable(val):
#                         aantal += spiegel_alles_recursief(val, bezocht)
#                 except Exception:
#                     pass
                    
#     return aantal

# --- File Uploader ---
st.markdown("---")
uploaded_file = st.file_uploader("Kies jouw D-Stability bestand", type=["stix"])

if uploaded_file:
    nieuwe_naam = uploaded_file.name.replace(".stix", "_GESPIEGELD.stix")
    
    with st.spinner("Bestand aan het analyseren en spiegelen..."):
        
        # Creëer een tijdelijke werkmap die zichzelf na afloop automatisch opruimt
        with tempfile.TemporaryDirectory() as tmp_dir:
            input_pad = Path(tmp_dir) / uploaded_file.name
            output_pad = Path(tmp_dir) / nieuwe_naam
            
            # Sla de upload fysiek op in de tijdelijke map
            input_pad.write_bytes(uploaded_file.getvalue())

            try:
                # 1. Inladen, 2. Spiegelen, 3. Opslaan
                dm = DStabilityModel()
                dm.parse(input_pad)
                totaal_aangepast = spiegel_alles_recursief(dm.datastructure)
                dm.serialize(output_pad)
                
                # Succesmelding en Download Knop
                st.success(f"✅ Gelukt! Er zijn {totaal_aangepast} datapunten gevonden en gespiegeld.")
                
                st.download_button(
                    label="📥 Download Gespiegelde .stix",
                    data=output_pad.read_bytes(),
                    file_name=nieuwe_naam,
                    mime="application/octet-stream"
                )

            except Exception as e:
                st.error(f"Er is een fout opgetreden bij het verwerken: {e}")

