"""
app.py — Alcanzar Leistungsnachweis PDF-Generator
Streamlit Community Cloud App

Kunden und Monate werden dynamisch aus monday.com geladen.
Neue Einträge erscheinen automatisch ohne Anpassung.

Corporate Design (Stand 10/2026): Mitternachtsblau, Frischgrün, Porzellan,
Schneeweiß · Schrift Manrope · keine Rahmen, runde Ecken, Schwingen-Deko.
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
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ── Konfiguration ─────────────────────────────────────────────────────────────

API_URL        = "https://api.monday.com/v2"
BOARD_LEISTUNG = 5097778382
STEUERUNGS_ITEM = "▶ Nachweise generieren"
OHNE_AUFTRAG = "(ohne Auftrag)"
ALLE_AUFTRAEGE = "Alle Aufträge"

# ── Corporate Design — vier Markenfarben, mehr gibt es nicht ────────────────────
MITTERNACHTSBLAU = colors.HexColor("#1C2D50")   # Text, Überschriften, dunkle Flächen
FRISCHGRUEN      = colors.HexColor("#76B82A")   # Akzent
PORZELLAN        = colors.HexColor("#F7F7F7")   # gedämpfte Flächen, Zebra
SCHNEEWEISS      = colors.white
BLAU_60          = colors.Color(28/255, 45/255, 80/255, alpha=0.60)  # gedämpfter Text
BLAU_10          = colors.Color(28/255, 45/255, 80/255, alpha=0.10)  # feine Linie (Ausnahme)

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
FONT_DIR  = os.path.join(BASE_DIR, "fonts")
BRAND_DIR = os.path.join(BASE_DIR, "brand")
LOGO_PATH = os.path.join(BRAND_DIR, "alcanzar-logo.png")
SCHWINGE_BLAU  = os.path.join(BRAND_DIR, "schwinge-blau.png")   # Deckkraft bereits eingebacken
SCHWINGE_GRUEN = os.path.join(BRAND_DIR, "schwinge-gruen.png")

MONATE_DE = ["Januar","Februar","März","April","Mai","Juni",
             "Juli","August","September","Oktober","November","Dezember"]

# ── Schrift registrieren (Manrope, mit Fallback auf Helvetica) ──────────────────

def _register_fonts():
    try:
        pdfmetrics.registerFont(TTFont("Manrope",          os.path.join(FONT_DIR, "Manrope-400.ttf")))
        pdfmetrics.registerFont(TTFont("Manrope-SemiBold", os.path.join(FONT_DIR, "Manrope-600.ttf")))
        pdfmetrics.registerFont(TTFont("Manrope-Bold",     os.path.join(FONT_DIR, "Manrope-700.ttf")))
        pdfmetrics.registerFontFamily(
            "Manrope", normal="Manrope", bold="Manrope-Bold", italic="Manrope", boldItalic="Manrope-Bold")
        return "Manrope", "Manrope-SemiBold", "Manrope-Bold"
    except Exception:
        return "Helvetica", "Helvetica-Bold", "Helvetica-Bold"

F_REG, F_SEMI, F_BOLD = _register_fonts()

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
    """
    Lädt alle verfügbaren Monat/Kunde/Auftrag-Kombinationen dynamisch aus monday.com.
    Rückgabe: {monat: {kundenname: [auftrag_labels]}}
    Leerer Auftrag wird als "(ohne Auftrag)" geführt.
    """
    query = """
    query($board_id: ID!, $cursor: String) {
      boards(ids: [$board_id]) {
        items_page(limit: 100, cursor: $cursor) {
          cursor
          items {
            id name
            column_values(ids: ["date_mm3zzepy", "board_relation_mm3z3jnk", "dropdown_mm50zhnf"]) {
              id text value
              ... on BoardRelationValue { linked_items { id name } }
            }
          }
        }
      }
    }
    """
    ergebnisse = defaultdict(lambda: defaultdict(set))
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
            auftrag = (col.get("dropdown_mm50zhnf", {}).get("text") or "").strip() or OHNE_AUFTRAG
            ergebnisse[datum][linked[0]["name"]].add(auftrag)
        cursor = page.get("cursor")
        if not cursor:
            break
    return {
        monat: {kunde: sorted(auftraege) for kunde, auftraege in sorted(kunden.items())}
        for monat, kunden in sorted(ergebnisse.items(), reverse=True)
    }

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
              "color_mm3znz4s", "board_relation_mm3z3jnk",
              "dropdown_mm50zhnf"
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
                "auftrag":      (col.get("dropdown_mm50zhnf", {}).get("text") or "").strip(),
            })
        cursor = page.get("cursor")
        if not cursor:
            break
    return sorted(eintraege, key=lambda x: x["datum"])

# ── PDF-Bausteine ───────────────────────────────────────────────────────────

def _draw_schwinge(c, path, x, y, target_w):
    """Zeichnet eine Schwingen-Grafik (faint PNG) dezent als Markenelement."""
    try:
        if not os.path.exists(path):
            return
        img = ImageReader(path)
        iw, ih = img.getSize()
        h = target_w * ih / iw
        c.drawImage(img, x, y, width=target_w, height=h, mask='auto',
                    preserveAspectRatio=True)
    except Exception:
        # Deko ist optional — ein fehlendes Bild darf das PDF nie sprengen.
        pass

# ── PDF erstellen ─────────────────────────────────────────────────────────────

def erstelle_pdf(kundenname, eintraege, monat, auftrag_label=None):
    heute = date.today().strftime("%d.%m.%Y")
    ml_label = monat_label(monat)
    W, H = A4
    ML = 20*mm
    TW = W - 2*ML

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)

    # Schwingen-Deko (dezent, im Hintergrund — zuerst zeichnen)
    _draw_schwinge(c, SCHWINGE_BLAU,  W-55*mm, H-48*mm, 70*mm)
    _draw_schwinge(c, SCHWINGE_GRUEN, W-78*mm, 2*mm, 95*mm)

    y = H - 15*mm

    # Logo + Adresse
    if os.path.exists(LOGO_PATH):
        logo_img = ImageReader(LOGO_PATH)
        # Logo-Seitenverhältnis ≈ 4,43:1 → 55 mm Breite ≈ 12,4 mm Höhe
        c.drawImage(logo_img, ML, y-13*mm, width=55*mm, height=13*mm,
                    preserveAspectRatio=True, anchor='sw', mask='auto')
    c.setFont(F_REG, 8); c.setFillColor(BLAU_60)
    c.drawRightString(ML+TW, y-5*mm,  "Alcanzar GmbH · Fritz-Haber-Straße 9 · 06217 Merseburg")
    c.drawRightString(ML+TW, y-11*mm, "Tel: 03461 7949251 · info@alcanzar.de · www.alcanzar.de")
    y -= 20*mm

    # Akzentlinie Frischgrün
    c.setStrokeColor(FRISCHGRUEN); c.setLineWidth(2)
    c.line(ML, y, ML+TW, y); y -= 10*mm

    # Titel
    c.setFont(F_BOLD, 17); c.setFillColor(MITTERNACHTSBLAU)
    c.drawString(ML, y, "Leistungsnachweis"); y -= 9*mm

    # Meta
    c.setFont(F_SEMI, 10); c.setFillColor(BLAU_60)
    c.drawString(ML, y, "Kunde")
    c.setFont(F_REG, 10); c.setFillColor(MITTERNACHTSBLAU)
    c.drawString(ML+16*mm, y, kundenname)
    c.setFont(F_SEMI, 10); c.setFillColor(BLAU_60)
    c.drawString(ML+92*mm, y, "Zeitraum")
    c.setFont(F_REG, 10); c.setFillColor(MITTERNACHTSBLAU)
    c.drawString(ML+112*mm, y, ml_label)
    c.setFont(F_SEMI, 10); c.setFillColor(BLAU_60)
    c.drawString(ML+145*mm, y, "Erstellt")
    c.setFont(F_REG, 10); c.setFillColor(MITTERNACHTSBLAU)
    c.drawString(ML+161*mm, y, heute)
    y -= 6.5*mm

    # Auftrag (nur wenn ein konkreter Auftrag gewählt wurde)
    if auftrag_label:
        c.setFont(F_SEMI, 10); c.setFillColor(BLAU_60)
        c.drawString(ML, y, "Auftrag")
        c.setFont(F_REG, 10); c.setFillColor(MITTERNACHTSBLAU)
        c.drawString(ML+16*mm, y, auftrag_label)
        y -= 6.5*mm
    y -= 3*mm

    # Tabelle
    row_h = 9*mm
    col_w = [25*mm, 35*mm, TW-25*mm-35*mm-18*mm-14*mm, 18*mm, 14*mm]
    col_x = [ML] + [ML+sum(col_w[:i+1]) for i in range(len(col_w)-1)]
    radius = 2.5*mm

    # Header — Mitternachtsblau, runde obere Ecken, weiße Schrift
    c.setFillColor(MITTERNACHTSBLAU)
    c.roundRect(ML, y-row_h, TW, row_h+radius, radius, fill=1, stroke=0)
    c.rect(ML, y-row_h, TW, row_h-radius+0.5, fill=1, stroke=0)  # untere Kante bündig
    c.setFillColor(SCHNEEWEISS); c.setFont(F_SEMI, 10)
    for i,(hdr,cx,cw) in enumerate(zip(
            ["Datum","Mitarbeiter/in","Leistungsbeschreibung","Std.","Verr."],col_x,col_w)):
        if i>=3: c.drawRightString(cx+cw-3, y-row_h+3*mm, hdr)
        else:    c.drawString(cx+3, y-row_h+3*mm, hdr)
    y -= row_h
    tab_top = y

    gesamt = 0.0
    for idx, e in enumerate(eintraege):
        d  = date.fromisoformat(e["datum"])
        sh = f"{e['stunden']:.2f}".replace(".",",")
        vs = "Ja" if e["verrechenbar"]=="Ja" else "tw."
        gesamt += e["stunden"]
        rh = 9*mm; ry = y-rh
        if idx%2==1:
            c.setFillColor(PORZELLAN); c.rect(ML, ry, TW, rh, fill=1, stroke=0)
        c.setFillColor(MITTERNACHTSBLAU); c.setFont(F_REG, 10)
        c.drawString(col_x[0]+3, ry+2.8*mm, d.strftime("%d.%m.%Y"))
        c.drawString(col_x[1]+3, ry+2.8*mm, e["mitarbeiter"])
        # Mehrzeilig umbrechen wenn nötig
        leistung = e["leistung"]
        max_w = col_w[2] - 6
        words = leistung.split()
        lines = []
        line = ""
        for word in words:
            test = (line + " " + word).strip()
            if c.stringWidth(test, F_REG, 10) <= max_w:
                line = test
            else:
                if line: lines.append(line)
                line = word
        if line: lines.append(line)
        lines = lines[:2]  # max 2 Zeilen
        if len(lines) == 2:
            c.drawString(col_x[2]+3, ry+4.5*mm, lines[0])
            c.drawString(col_x[2]+3, ry+1.1*mm, lines[1])
        else:
            c.drawString(col_x[2]+3, ry+2.8*mm, lines[0] if lines else "")
        c.drawRightString(col_x[3]+col_w[3]-3, ry+2.8*mm, sh)
        # "Verr." farblich: Ja = Frischgrün, tw. = gedämpftes Blau
        c.setFillColor(FRISCHGRUEN if e["verrechenbar"]=="Ja" else BLAU_60)
        c.setFont(F_SEMI, 10)
        c.drawRightString(col_x[4]+col_w[4]-3, ry+2.8*mm, vs)
        y -= rh

    # Summe — Frischgrün-Band, Text Mitternachtsblau (CD-Farbpaar)
    sum_h = 9*mm
    c.setFillColor(FRISCHGRUEN); c.rect(ML, y-sum_h, TW, sum_h, fill=1, stroke=0)
    c.setFont(F_BOLD, 10); c.setFillColor(MITTERNACHTSBLAU)
    c.drawString(col_x[2]+3, y-sum_h+2.8*mm, "Gesamt verrechenbar")
    c.drawRightString(col_x[3]+col_w[3]-3, y-sum_h+2.8*mm, f"{gesamt:.2f}".replace(".",","))
    c.drawRightString(col_x[4]+col_w[4]-3, y-sum_h+2.8*mm, "h")
    y -= sum_h + 5*mm

    # Legende
    gesamt_str = f"{gesamt:.2f}".replace(".", ",")
    c.setFont(F_REG, 8); c.setFillColor(BLAU_60)
    c.drawString(ML, y, "Ja = verrechenbar  ·  tw. = teilweise verrechenbar  ·  "
                        f"Verrechenbare Stunden gesamt: {gesamt_str} h")
    y -= 12*mm

    # Bestätigung
    c.setFont(F_SEMI, 11); c.setFillColor(MITTERNACHTSBLAU)
    c.drawString(ML, y, "Bestätigung"); y -= 10*mm

    cb_size = 13
    cb_x = ML
    cb_y = y - cb_size

    c.acroForm.checkbox(
        name="leistung_bestaetigt", tooltip="Leistung bestätigt",
        x=cb_x, y=cb_y, size=cb_size, checked=False, buttonStyle="check",
        borderColor=FRISCHGRUEN, fillColor=SCHNEEWEISS,
        textColor=MITTERNACHTSBLAU, forceBorder=True,
    )
    c.setFont(F_REG, 10); c.setFillColor(MITTERNACHTSBLAU)
    c.drawString(cb_x+cb_size+4, cb_y+2.5, "Leistung bestätigt")

    # Name
    name_x = ML+55*mm; name_w = 80*mm; name_h = 13
    c.setFont(F_REG, 8); c.setFillColor(BLAU_60)
    c.drawString(name_x, cb_y+name_h+3, "Name")
    c.acroForm.textfield(
        name="name", tooltip="Name des Unterzeichners",
        x=name_x, y=cb_y, width=name_w, height=name_h,
        borderWidth=0, fillColor=PORZELLAN, borderColor=PORZELLAN,
        textColor=MITTERNACHTSBLAU, fontSize=10, fontName="Helvetica",
        borderStyle="solid",
    )

    # Datum
    dat_x = ML+143*mm; dat_w = 27*mm; dat_h = 13
    c.setFont(F_REG, 8); c.setFillColor(BLAU_60)
    c.drawString(dat_x, cb_y+dat_h+3, "Datum")
    c.acroForm.textfield(
        name="datum", tooltip="Datum der Bestätigung",
        x=dat_x, y=cb_y, width=dat_w, height=dat_h,
        borderWidth=0, fillColor=PORZELLAN, borderColor=PORZELLAN,
        textColor=MITTERNACHTSBLAU, fontSize=10, fontName="Helvetica",
        borderStyle="solid",
    )

    # Fußzeile (schlichte Adresszeile, 7 pt)
    c.setStrokeColor(BLAU_10); c.setLineWidth(0.5)
    c.line(ML, 16*mm, ML+TW, 16*mm)
    c.setFont(F_REG, 7); c.setFillColor(BLAU_60)
    c.drawCentredString(W/2, 11.5*mm,
        "Alcanzar GmbH · Fritz-Haber-Straße 9 · 06217 Merseburg")

    c.save()
    return buf.getvalue()

# ── Streamlit UI ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Alcanzar Leistungsnachweis",
    page_icon=LOGO_PATH if os.path.exists(LOGO_PATH) else None,
    layout="centered",
)

# Corporate Design für die Oberfläche
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700&display=swap');
html, body, [class*="css"], .stMarkdown, .stButton, .stSelectbox, .stTextInput {
    font-family: 'Manrope', system-ui, sans-serif;
}
h1, h2, h3 { color: #1C2D50; font-weight: 700; }
.stApp { background: #FFFFFF; }
div[data-testid="stMetricValue"] { color: #1C2D50; }
hr { border-color: rgba(28,45,80,.10); }
/* Primär-Buttons in Mitternachtsblau */
.stButton > button[kind="primary"],
.stDownloadButton > button {
    background-color: #1C2D50; color: #FFFFFF; border: none;
    border-radius: 12px; font-weight: 600;
}
.stButton > button[kind="primary"]:hover,
.stDownloadButton > button:hover {
    background-color: #76B82A; color: #1C2D50;
}
</style>
""", unsafe_allow_html=True)

if os.path.exists(LOGO_PATH):
    st.image(LOGO_PATH, width=280)

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

kunden    = list(verfuegbar.get(sel_monat, {}).keys())
sel_kunde = st.selectbox("Kunde", kunden)

# Auftrag-Auswahl — nur anzeigen, wenn der Kunde überhaupt Aufträge hat
auftraege_vorhanden = verfuegbar.get(sel_monat, {}).get(sel_kunde, [])
echte_auftraege = [a for a in auftraege_vorhanden if a != OHNE_AUFTRAG]
if echte_auftraege:
    sel_auftrag = st.selectbox("Auftrag", [ALLE_AUFTRAEGE] + auftraege_vorhanden)
else:
    sel_auftrag = ALLE_AUFTRAEGE

st.divider()

if st.button("📄 PDF generieren", type="primary", use_container_width=True):
    with st.spinner(f"Lade Einträge für {sel_kunde}..."):
        try:
            eintraege = lade_eintraege(sel_monat, sel_kunde)
        except Exception as e:
            st.error(f"Fehler: {e}")
            st.stop()

    # Nach gewähltem Auftrag filtern
    if sel_auftrag != ALLE_AUFTRAEGE:
        if sel_auftrag == OHNE_AUFTRAG:
            eintraege = [e for e in eintraege if not e["auftrag"]]
        else:
            eintraege = [e for e in eintraege if e["auftrag"] == sel_auftrag]

    if not eintraege:
        hinweis = f" (Auftrag: {sel_auftrag})" if sel_auftrag != ALLE_AUFTRAEGE else ""
        st.warning(f"Keine verrechenbaren Einträge für diesen Kunden im gewählten Monat{hinweis}.")
        st.stop()

    gesamt = sum(e["stunden"] for e in eintraege)

    # Auftrag im PDF nur als Kopfzeile zeigen, wenn ein konkreter Auftrag gewählt wurde
    pdf_auftrag = sel_auftrag if sel_auftrag not in (ALLE_AUFTRAEGE, OHNE_AUFTRAG) else None

    with st.spinner("Erstelle PDF..."):
        pdf_bytes = erstelle_pdf(sel_kunde, eintraege, sel_monat, auftrag_label=pdf_auftrag)

    c1, c2 = st.columns(2)
    c1.metric("Verrechenbare Einträge", len(eintraege))
    c2.metric("Stunden gesamt", f"{gesamt:.2f} h".replace(".",","))

    sicher = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in sel_kunde).strip()
    auftrag_teil = ""
    if sel_auftrag not in (ALLE_AUFTRAEGE, OHNE_AUFTRAG):
        kurz = sel_auftrag.replace("Auftrag:", "").strip()
        kurz = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in kurz).strip()
        auftrag_teil = f"_{kurz}"
    dateiname = f"Leistungsnachweis_{sel_monat}_{sicher}{auftrag_teil}.pdf"

    st.success(f"PDF erstellt — {len(eintraege)} Einträge, {gesamt:.2f} h verrechenbar")
    st.download_button(
        label="⬇️ PDF herunterladen",
        data=pdf_bytes,
        file_name=dateiname,
        mime="application/pdf",
        use_container_width=True,
        type="primary",
    )

    # Outlook-Button
    st.divider()
    st.markdown("**📧 Per E-Mail versenden**")
    empfaenger = st.text_input(
        "Empfänger E-Mail (optional)",
        placeholder="kunde@beispiel.de",
        key="empfaenger_email"
    )

    betreff_auftrag = f" ({sel_auftrag})" if sel_auftrag not in (ALLE_AUFTRAEGE, OHNE_AUFTRAG) else ""
    betreff = f"Leistungsnachweis {sel_label} – {sel_kunde}{betreff_auftrag}"
    body = (
        f"Sehr geehrte Damen und Herren,%0D%0A%0D%0A"
        f"im Anhang erhalten Sie den Leistungsnachweis für {sel_label}.%0D%0A%0D%0A"
        f"Bitte bestätigen Sie die erbrachten Leistungen und senden Sie das ausgefüllte Dokument zurück.%0D%0A%0D%0A"
        f"Mit freundlichen Grüßen%0D%0AAlcanzar GmbH"
    )

    mailto = f"mailto:{empfaenger}?subject={betreff}&body={body}"

    st.markdown(
        f'''<a href="{mailto}" target="_blank">
            <button style="
                width:100%; padding:10px; font-size:15px; font-weight:600;
                background:#1C2D50; color:white; border:none; border-radius:12px;
                cursor:pointer; margin-top:4px;">
                📨 Outlook öffnen
            </button>
        </a>''',
        unsafe_allow_html=True
    )
    st.caption(f"💡 Das PDF liegt in deinem Download-Ordner als **{dateiname}** — einfach als Anhang in Outlook hinzufügen.")
