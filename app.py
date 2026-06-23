"""
app.py — Alcanzar Leistungsnachweis PDF-Generator
Streamlit Community Cloud App

Kunden und Monate werden dynamisch aus monday.com geladen.
Neue Einträge erscheinen automatisch ohne Anpassung.
"""

import io
import os
import requests
import streamlit as st
from datetime import date
from calendar import monthrange
from collections import defaultdict
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

# ── Konfiguration ─────────────────────────────────────────────────────────────

API_URL        = "https://api.monday.com/v2"
BOARD_LEISTUNG = 5097778382
STEUERUNGS_ITEM = "▶ Nachweise generieren"

ALCANZAR_ROT = colors.HexColor("#7B2D42")
GRAU_HELL    = colors.HexColor("#F5F5F5")
GRAU_MITTEL  = colors.HexColor("#CCCCCC")
GRAU_DUNKEL  = colors.HexColor("#555555")

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alcanzar-logo-rgb.png")

MONATE_DE = ["Januar","Februar","März","April","Mai","Juni",
             "Juli","August","September","Oktober","November","Dezember"]

# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def get_token():
    try:
        return st.secrets["MONDAY_API_TOKEN"]
    except Exception:
        return os.environ.get("MONDAY_API_TOKEN", "")

def gql(query, variables=None):
    token = get_token()
    if not token:
        st.error("API-Token nicht konfiguriert.")
        st.stop()
    r = requests.post(API_URL,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": token, "Content-Type": "application/json",
                 "API-Version": "2024-10"},
        timeout=30)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL: {data['errors']}")
    return data.get("data", {})

def monat_label(monat):
    y, m = map(int, monat.split("-"))
    return f"{MONATE_DE[m-1]} {y}"

# ── Daten laden ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def lade_monate_und_kunden():
    """Lädt alle verfügbaren Monat/Kunde-Kombinationen dynamisch aus monday.com."""
    query = """
    query($board_id: ID!, $cursor: String) {
      boards(ids: [$board_id]) {
        items_page(limit: 100, cursor: $cursor) {
          cursor
          items {
            id name
            column_values(ids: ["date_mm3zzepy", "board_relation_mm3z3jnk"]) {
              id text value
              ... on BoardRelationValue { linked_items { id name } }
            }
          }
        }
      }
    }
    """
    ergebnisse = defaultdict(set)
    cursor = None
    while True:
        data = gql(query, {"board_id": str(BOARD_LEISTUNG), "cursor": cursor})
        page = data["boards"][0]["items_page"]
        for item in page["items"]:
            if item["name"] == STEUERUNGS_ITEM:
                continue
            col = {c["id"]: c for c in item["column_values"]}
            datum = (col.get("date_mm3zzepy", {}).get("text") or "")[:7]
            if not datum:
                continue
            linked = col.get("board_relation_mm3z3jnk", {}).get("linked_items") or []
            if not linked:
                continue
            ergebnisse[datum].add(linked[0]["name"])
        cursor = page.get("cursor")
        if not cursor:
            break
    return {k: sorted(v) for k, v in sorted(ergebnisse.items(), reverse=True)}

@st.cache_data(ttl=60)
def lade_eintraege(monat, kundenname):
    """Lädt nur verrechenbare Einträge für Monat + Kunde."""
    y, m = map(int, monat.split("-"))
    datum_von = f"{y:04d}-{m:02d}-01"
    datum_bis = f"{y:04d}-{m:02d}-{monthrange(y,m)[1]:02d}"
    query = """
    query($board_id: ID!, $cursor: String) {
      boards(ids: [$board_id]) {
        items_page(limit: 100, cursor: $cursor) {
          cursor
          items {
            id name
            column_values(ids: [
              "date_mm3zzepy", "multiple_person_mm3zpgmx",
              "text_mm3zzr65", "numeric_mm3zfzkc",
              "color_mm3znz4s", "board_relation_mm3z3jnk"
            ]) {
              id text value
              ... on BoardRelationValue { linked_items { id name } }
            }
          }
        }
      }
    }
    """
    eintraege = []
    cursor = None
    while True:
        data = gql(query, {"board_id": str(BOARD_LEISTUNG), "cursor": cursor})
        page = data["boards"][0]["items_page"]
        for item in page["items"]:
            if item["name"] == STEUERUNGS_ITEM:
                continue
            col = {c["id"]: c for c in item["column_values"]}
            datum = (col.get("date_mm3zzepy", {}).get("text") or "")[:10]
            if not datum or not (datum_von <= datum <= datum_bis):
                continue
            linked = col.get("board_relation_mm3z3jnk", {}).get("linked_items") or []
            if not linked or linked[0]["name"] != kundenname:
                continue
            verrechenbar = col.get("color_mm3znz4s", {}).get("text") or ""
            # Nur verrechenbare Einträge
            if verrechenbar not in ("Ja", "Teilweise"):
                continue
            try:
                stunden = float(col.get("numeric_mm3zfzkc", {}).get("text") or "0")
            except ValueError:
                stunden = 0.0
            eintraege.append({
                "datum":        datum,
                "mitarbeiter":  col.get("multiple_person_mm3zpgmx", {}).get("text") or "",
                "leistung":     col.get("text_mm3zzr65", {}).get("text") or "",
                "stunden":      stunden,
                "verrechenbar": verrechenbar,
            })
        cursor = page.get("cursor")
        if not cursor:
            break
    return sorted(eintraege, key=lambda x: x["datum"])

# ── PDF erstellen ─────────────────────────────────────────────────────────────

def erstelle_pdf(kundenname, eintraege, monat):
    heute = date.today().strftime("%d.%m.%Y")
    ml_label = monat_label(monat)
    W, H = A4
    ML = 20*mm
    TW = W - 2*ML

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    y = H - 15*mm

    # Logo + Adresse
    if os.path.exists(LOGO_PATH):
        logo_img = ImageReader(LOGO_PATH)
        c.drawImage(logo_img, ML, y-15*mm, width=55*mm, height=15*mm,
                    preserveAspectRatio=True, mask='auto')
    c.setFont("Helvetica", 8); c.setFillColor(GRAU_DUNKEL)
    c.drawRightString(ML+TW, y-6*mm,  "Alcanzar GmbH · Fritz-Haber-Straße 9 · 06217 Merseburg")
    c.drawRightString(ML+TW, y-12*mm, "Tel: 03461 7949251 · info@alcanzar.de · www.alcanzar.de")
    y -= 20*mm

    c.setStrokeColor(ALCANZAR_ROT); c.setLineWidth(1.5)
    c.line(ML, y, ML+TW, y); y -= 8*mm

    # Titel
    c.setFont("Helvetica-Bold", 14); c.setFillColor(ALCANZAR_ROT)
    c.drawString(ML, y, "Leistungsnachweis"); y -= 7*mm

    # Meta
    c.setFont("Helvetica-Bold", 9); c.setFillColor(GRAU_DUNKEL)
    c.drawString(ML, y, "Kunde:")
    c.setFont("Helvetica", 9); c.drawString(ML+14*mm, y, kundenname)
    c.setFont("Helvetica-Bold", 9); c.drawString(ML+85*mm, y, "Zeitraum:")
    c.setFont("Helvetica", 9); c.drawString(ML+103*mm, y, ml_label)
    c.setFont("Helvetica-Bold", 9); c.drawString(ML+140*mm, y, "Erstellt:")
    c.setFont("Helvetica", 9); c.drawString(ML+154*mm, y, heute)
    y -= 8*mm

    # Tabelle
    row_h = 8*mm
    col_w = [25*mm, 35*mm, TW-25*mm-35*mm-18*mm-12*mm, 18*mm, 12*mm]
    col_x = [ML] + [ML+sum(col_w[:i+1]) for i in range(len(col_w)-1)]

    # Header
    c.setFillColor(ALCANZAR_ROT)
    c.rect(ML, y-row_h, TW, row_h, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 9)
    for i,(hdr,cx,cw) in enumerate(zip(
            ["Datum","Mitarbeiter/in","Leistungsbeschreibung","Std.","Verr."],col_x,col_w)):
        if i>=3: c.drawRightString(cx+cw-2, y-row_h+2.5*mm, hdr)
        else:    c.drawString(cx+2, y-row_h+2.5*mm, hdr)
    y -= row_h
    tab_top = y

    gesamt = 0.0
    for idx, e in enumerate(eintraege):
        d  = date.fromisoformat(e["datum"])
        sh = f"{e['stunden']:.2f}".replace(".",",")
        vs = "✓" if e["verrechenbar"]=="Ja" else "~"
        gesamt += e["stunden"]
        rh = 6.5*mm; ry = y-rh
        if idx%2==1:
            c.setFillColor(GRAU_HELL); c.rect(ML, ry, TW, rh, fill=1, stroke=0)
        c.setFillColor(GRAU_DUNKEL); c.setFont("Helvetica", 9)
        c.drawString(col_x[0]+2, ry+2*mm, d.strftime("%d.%m.%Y"))
        c.drawString(col_x[1]+2, ry+2*mm, e["mitarbeiter"])
        leistung = e["leistung"][:50]+"..." if len(e["leistung"])>52 else e["leistung"]
        c.drawString(col_x[2]+2, ry+2*mm, leistung)
        c.drawRightString(col_x[3]+col_w[3]-2, ry+2*mm, sh)
        c.drawRightString(col_x[4]+col_w[4]-2, ry+2*mm, vs)
        c.setStrokeColor(GRAU_MITTEL); c.setLineWidth(0.3)
        c.line(ML, ry, ML+TW, ry)
        y -= rh

    # Summe
    c.setFillColor(GRAU_HELL); c.rect(ML, y-7*mm, TW, 7*mm, fill=1, stroke=0)
    c.setStrokeColor(ALCANZAR_ROT); c.setLineWidth(1); c.line(ML, y, ML+TW, y)
    c.setFont("Helvetica-Bold", 9); c.setFillColor(colors.black)
    c.drawString(col_x[2]+2, y-5*mm, "Gesamt verrechenbar")
    c.drawRightString(col_x[3]+col_w[3]-2, y-5*mm, f"{gesamt:.2f}".replace(".",","))
    y -= 7*mm

    # Rahmen
    c.setStrokeColor(GRAU_MITTEL); c.setLineWidth(0.5)
    c.rect(ML, y, TW, tab_top-y, fill=0, stroke=1)
    y -= 4*mm

    # Legende
    c.setFont("Helvetica", 8); c.setFillColor(GRAU_DUNKEL)
    c.drawString(ML, y, f"✓ = verrechenbar  ·  ~ = teilweise verrechenbar  ·  Verrechenbare Stunden gesamt: {gesamt:.2f} h".replace(".",","))
    y -= 10*mm

    # Trennlinie
    c.setStrokeColor(GRAU_MITTEL); c.setLineWidth(0.5)
    c.line(ML, y, ML+TW, y); y -= 7*mm

    # Bestätigung
    c.setFont("Helvetica-Bold", 10); c.setFillColor(colors.black)
    c.drawString(ML, y, "Bestätigung"); y -= 10*mm

    cb_size = 12
    cb_x = ML
    cb_y = y - cb_size

    c.acroForm.checkbox(
        name="leistung_bestaetigt", tooltip="Leistung bestätigt",
        x=cb_x, y=cb_y, size=cb_size, checked=False, buttonStyle="check",
        borderColor=ALCANZAR_ROT, fillColor=colors.white,
        textColor=ALCANZAR_ROT, forceBorder=True,
    )
    c.setFont("Helvetica", 9); c.setFillColor(GRAU_DUNKEL)
    c.drawString(cb_x+cb_size+3, cb_y+2, "Leistung bestätigt")

    # Name
    name_x = ML+50*mm; name_w = 85*mm; name_h = 12
    c.setFont("Helvetica", 8); c.setFillColor(colors.HexColor("#999999"))
    c.drawString(name_x, cb_y+name_h+2, "Name")
    c.acroForm.textfield(
        name="name", tooltip="Name des Unterzeichners",
        x=name_x, y=cb_y, width=name_w, height=name_h,
        borderWidth=0, fillColor=colors.white, borderColor=GRAU_MITTEL,
        textColor=colors.black, fontSize=9, fontName="Helvetica",
        borderStyle="underlined",
    )
    c.setStrokeColor(GRAU_DUNKEL); c.setLineWidth(0.6)
    c.line(name_x, cb_y, name_x+name_w, cb_y)

    # Datum
    dat_x = ML+143*mm; dat_w = 27*mm; dat_h = 12
    c.setFont("Helvetica", 8); c.setFillColor(colors.HexColor("#999999"))
    c.drawString(dat_x, cb_y+dat_h+2, "Datum")
    c.acroForm.textfield(
        name="datum", tooltip="Datum der Bestätigung",
        x=dat_x, y=cb_y, width=dat_w, height=dat_h,
        borderWidth=0, fillColor=colors.white, borderColor=GRAU_MITTEL,
        textColor=colors.black, fontSize=9, fontName="Helvetica",
        borderStyle="underlined",
    )
    c.setStrokeColor(GRAU_DUNKEL); c.setLineWidth(0.6)
    c.line(dat_x, cb_y, dat_x+dat_w, cb_y)

    # Fußzeile
    c.setStrokeColor(GRAU_MITTEL); c.setLineWidth(0.5)
    c.line(ML, 17*mm, ML+TW, 17*mm)
    c.setFont("Helvetica", 8); c.setFillColor(colors.grey)
    c.drawCentredString(W/2, 12*mm,
        "Alcanzar GmbH · Fritz-Haber-Straße 9 · 06217 Merseburg")

    c.save()
    return buf.getvalue()

# ── Streamlit UI ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Alcanzar Leistungsnachweis",
    page_icon="alcanzar-logo-rgb.png",
    layout="centered",
)

if os.path.exists(LOGO_PATH):
    st.image(LOGO_PATH, width=260)

st.title("Leistungsnachweis Generator")
st.caption("Daten direkt aus monday.com — neue Kunden und Monate erscheinen automatisch.")
st.divider()

with st.spinner("Lade Daten aus monday.com..."):
    try:
        verfuegbar = lade_monate_und_kunden()
    except Exception as e:
        st.error(f"Verbindungsfehler: {e}")
        st.stop()

if not verfuegbar:
    st.warning("Keine Einträge gefunden.")
    st.stop()

monate      = list(verfuegbar.keys())
monat_labels = [monat_label(m) for m in monate]

sel_label = st.selectbox("Monat", monat_labels, index=0)
sel_monat = monate[monat_labels.index(sel_label)]

kunden    = verfuegbar.get(sel_monat, [])
sel_kunde = st.selectbox("Kunde", kunden)

st.divider()

if st.button("📄 PDF generieren", type="primary", use_container_width=True):
    with st.spinner(f"Lade Einträge für {sel_kunde}..."):
        try:
            eintraege = lade_eintraege(sel_monat, sel_kunde)
        except Exception as e:
            st.error(f"Fehler: {e}")
            st.stop()

    if not eintraege:
        st.warning("Keine verrechenbaren Einträge für diesen Kunden im gewählten Monat.")
        st.stop()

    gesamt = sum(e["stunden"] for e in eintraege)

    with st.spinner("Erstelle PDF..."):
        pdf_bytes = erstelle_pdf(sel_kunde, eintraege, sel_monat)

    c1, c2 = st.columns(2)
    c1.metric("Verrechenbare Einträge", len(eintraege))
    c2.metric("Stunden gesamt", f"{gesamt:.2f} h".replace(".",","))

    sicher = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in sel_kunde).strip()
    dateiname = f"Leistungsnachweis_{sel_monat}_{sicher}.pdf"

    st.success(f"PDF erstellt — {len(eintraege)} Einträge, {gesamt:.2f} h verrechenbar")
    st.download_button(
        label="⬇️ PDF herunterladen",
        data=pdf_bytes,
        file_name=dateiname,
        mime="application/pdf",
        use_container_width=True,
        type="primary",
    )
