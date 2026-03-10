import streamlit as st
import tempfile
from pathlib import Path
from geolib.models.dstability.dstability_model import DStabilityModel

# --- Pagina Instellingen ---
st.set_page_config(page_title="D-Stability Spiegel App", page_icon="🪞")
st.title("🪞 D-Stability Spiegel Tool")
st.write("Upload een `.stix` bestand. Deze app spiegelt automatisch de hele geometrie (inclusief alle achterliggende waterlijnen en scenario's) om de Y-as.")

