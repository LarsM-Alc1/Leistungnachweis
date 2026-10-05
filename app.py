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
import re
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

# ── KI-Begriffe neutralisieren (kundenseitige Beschreibung) ─────────────────────
# Hinweise auf KI-Tools dürfen im Leistungsnachweis an Dritte nicht erscheinen.
# Markennamen (Teilstring-Treffer) …
KI_MARKEN = [
    "chatgpt", "chat gpt", "gpt-4o", "gpt-4.1", "gpt-4", "gpt-3.5", "gpt-5", "gpt4", "gpt",
    "openai", "anthropic", "claude", "copilot", "github copilot", "microsoft copilot",
    "gemini", "google gemini", "bard", "perplexity", "mistral", "llama",
    "künstliche intelligenz", "künstlicher intelligenz",
]
# … und Abkürzungen, die nur als eigenständiges Wort zählen (Wortgrenzen):
KI_TOKENS = ["ki", "ai", "llm", "genai", "gen-ai"]

def neutralisiere_ki(text):
    """Entfernt/neutralisiert Hinweise auf KI-Tools im kundenseitigen Text.
    Gibt (neuer_text, geaendert) zurück. Die monday.com-Quelle bleibt unberührt."""
    if not text:
        return text, False
    marken_alt = "|".join(re.escape(m) for m in sorted(KI_MARKEN, key=len, reverse=True))
    tokens_alt = "|".join(re.escape(m) for m in KI_TOKENS)
    nennung = rf"(?:{marken_alt}|(?<![\wäöüß])(?:{tokens_alt})(?![\wäöüß]))"
    t = text
    # 1) "<KI>-Recherche" / "<KI> (gestützte) Recherche" -> "Recherche"
    t = re.sub(rf"{nennung}[\s\-–]*(?:gestützte[rn]?\s+)?(recherche)", r"\1", t, flags=re.IGNORECASE)
    # 2) "Recherche mit/per/via/mittels/durch <KI>" -> "Recherche"
    t = re.sub(rf"(recherche)\s+(?:mit|per|via|mittels|über|durch|unter einsatz von)\s+{nennung}",
               r"\1", t, flags=re.IGNORECASE)
    # 3) "mit/per/via/mittels/durch <KI>" -> entfernen
    t = re.sub(rf"\s*(?:mit|per|via|mittels|durch|unter einsatz von)\s+{nennung}", "", t, flags=re.IGNORECASE)
    # 4) "(<KI>)" / "[<KI>]" -> entfernen
    t = re.sub(rf"\s*[\(\[]\s*{nennung}\s*[\)\]]", "", t, flags=re.IGNORECASE)
    # 5) verbleibende eigenständige Nennung -> "Recherche"
    t = re.sub(nennung, "Recherche", t, flags=re.IGNORECASE)
    # Aufräumen
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([,.;:])", r"\1", t)
    t = re.sub(r"\b([Rr]echerche)(\s+[Rr]echerche)+\b", r"\1", t)
    t = t.strip(" -–:,")
    return t, (t != text)

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

    col_w = [25*mm, 32*mm, TW-25*mm-32*mm-18*mm-14*mm, 18*mm, 14*mm]
    col_x = [ML] + [ML+sum(col_w[:i+1]) for i in range(len(col_w)-1)]

    FS        = 10          # Lesetext ≥ 10 pt (CD-Print)
    LEADING   = 4.9*mm      # Zeilenabstand innerhalb einer Beschreibung
    PAD_V     = 3.2*mm      # Innenabstand oben/unten je Zeile
    ROW_MIN   = 10.5*mm     # Mindest-Zeilenhöhe (Luft)
    HEAD_H    = 9.5*mm      # Höhe Tabellenkopf / Summenband
    RADIUS    = 2.5*mm
    UNTEN     = 24*mm       # Grenze zur Fußzeile (kein Inhalt darunter)

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)

    def deko():
        _draw_schwinge(c, SCHWINGE_BLAU,  W-55*mm, H-48*mm, 70*mm)
        _draw_schwinge(c, SCHWINGE_GRUEN, W-78*mm, 2*mm, 95*mm)

    def fusszeile():
        c.setStrokeColor(BLAU_10); c.setLineWidth(0.5)
        c.line(ML, 16*mm, ML+TW, 16*mm)
        c.setFont(F_REG, 7); c.setFillColor(BLAU_60)
        c.drawCentredString(W/2, 11.5*mm,
            "Alcanzar GmbH · Fritz-Haber-Straße 9 · 06217 Merseburg")

    def kopfbereich(erste_seite):
        deko()
        yy = H - 15*mm
        if os.path.exists(LOGO_PATH):
            c.drawImage(ImageReader(LOGO_PATH), ML, yy-13*mm, width=55*mm, height=13*mm,
                        preserveAspectRatio=True, anchor='sw', mask='auto')
        c.setFont(F_REG, 8); c.setFillColor(BLAU_60)
        c.drawRightString(ML+TW, yy-5*mm,  "Alcanzar GmbH · Fritz-Haber-Straße 9 · 06217 Merseburg")
        c.drawRightString(ML+TW, yy-11*mm, "Tel: 03461 7949251 · info@alcanzar.de · www.alcanzar.de")
        yy -= 20*mm
        c.setStrokeColor(FRISCHGRUEN); c.setLineWidth(2)
        c.line(ML, yy, ML+TW, yy); yy -= 11*mm
        c.setFont(F_BOLD, 17); c.setFillColor(MITTERNACHTSBLAU)
        c.drawString(ML, yy, "Leistungsnachweis" if erste_seite else "Leistungsnachweis (Fortsetzung)")
        yy -= 11*mm
        if erste_seite:
            def feld(label, value, lx, vx):
                c.setFont(F_SEMI, 10); c.setFillColor(BLAU_60); c.drawString(lx, yy, label)
                c.setFont(F_REG, 10); c.setFillColor(MITTERNACHTSBLAU); c.drawString(vx, yy, value)
            feld("Kunde", kundenname, ML, ML+16*mm)
            feld("Zeitraum", ml_label, ML+92*mm, ML+112*mm)
            feld("Erstellt", heute, ML+145*mm, ML+161*mm)
            yy -= 7*mm
            if auftrag_label:
                feld("Auftrag", auftrag_label, ML, ML+16*mm)
                yy -= 7*mm
            yy -= 4*mm
        else:
            yy -= 2*mm
        return yy

    def tabellenkopf(yy):
        c.setFillColor(MITTERNACHTSBLAU)
        c.roundRect(ML, yy-HEAD_H, TW, HEAD_H+RADIUS, RADIUS, fill=1, stroke=0)
        c.rect(ML, yy-HEAD_H, TW, HEAD_H-RADIUS+0.5, fill=1, stroke=0)  # untere Kante bündig
        c.setFillColor(SCHNEEWEISS); c.setFont(F_SEMI, 10)
        for i,(hdr,cx,cw) in enumerate(zip(
                ["Datum","Mitarbeiter/in","Leistungsbeschreibung","Std.","Verr."], col_x, col_w)):
            if i>=3: c.drawRightString(cx+cw-3, yy-HEAD_H+3.2*mm, hdr)
            else:    c.drawString(cx+3, yy-HEAD_H+3.2*mm, hdr)
        return yy - HEAD_H

    def umbrechen(text):
        """Beschreibung in beliebig viele Zeilen umbrechen (nichts abschneiden)."""
        max_w = col_w[2] - 6
        out = []
        for para in (text or "").split("\n"):
            words = para.split()
            if not words:
                out.append("")
                continue
            line = ""
            for w in words:
                test = (line + " " + w).strip()
                if c.stringWidth(test, F_REG, FS) <= max_w:
                    line = test
                else:
                    if line:
                        out.append(line); line = ""
                    # überlanges Einzelwort hart umbrechen
                    while c.stringWidth(w, F_REG, FS) > max_w and len(w) > 1:
                        cut = len(w)
                        while cut > 1 and c.stringWidth(w[:cut] + "-", F_REG, FS) > max_w:
                            cut -= 1
                        out.append(w[:cut] + "-"); w = w[cut:]
                    line = w
            if line:
                out.append(line)
        return out or [""]

    # ── Seite 1 ──
    y = kopfbereich(True)
    y = tabellenkopf(y)

    gesamt = 0.0
    for idx, e in enumerate(eintraege):
        d  = date.fromisoformat(e["datum"])
        sh = f"{e['stunden']:.2f}".replace(".", ",")
        vs = "Ja" if e["verrechenbar"] == "Ja" else "tw."
        gesamt += e["stunden"]

        lines = umbrechen(e["leistung"])
        rh = max(ROW_MIN, 2*PAD_V + len(lines)*LEADING)

        # Seitenumbruch, wenn die Zeile nicht mehr passt
        if y - rh < UNTEN:
            fusszeile(); c.showPage()
            y = kopfbereich(False)
            y = tabellenkopf(y)

        ry = y - rh
        if idx % 2 == 1:
            c.setFillColor(PORZELLAN); c.rect(ML, ry, TW, rh, fill=1, stroke=0)

        center = ry + rh/2 - 1.2*mm          # vertikal zentriert (einzeilige Spalten)
        top    = ry + rh - PAD_V - 3.3*mm     # erste Beschreibungszeile oben

        c.setFillColor(MITTERNACHTSBLAU); c.setFont(F_REG, FS)
        c.drawString(col_x[0]+3, center, d.strftime("%d.%m.%Y"))
        c.drawString(col_x[1]+3, center, e["mitarbeiter"])
        for li, ln in enumerate(lines):
            c.drawString(col_x[2]+3, top - li*LEADING, ln)
        c.drawRightString(col_x[3]+col_w[3]-3, center, sh)
        c.setFillColor(FRISCHGRUEN if e["verrechenbar"] == "Ja" else BLAU_60)
        c.setFont(F_SEMI, FS)
        c.drawRightString(col_x[4]+col_w[4]-3, center, vs)

        y = ry

    gesamt_str = f"{gesamt:.2f}".replace(".", ",")

    # Summenband (ggf. auf neue Seite)
    if y - HEAD_H < UNTEN:
        fusszeile(); c.showPage(); y = kopfbereich(False); y = tabellenkopf(y)
    c.setFillColor(FRISCHGRUEN); c.rect(ML, y-HEAD_H, TW, HEAD_H, fill=1, stroke=0)
    c.setFont(F_BOLD, 10); c.setFillColor(MITTERNACHTSBLAU)
    base = y - HEAD_H + 3.2*mm
    c.drawString(col_x[2]+3, base, "Gesamt verrechenbar")
    c.drawRightString(col_x[3]+col_w[3]-3, base, gesamt_str)
    c.drawRightString(col_x[4]+col_w[4]-3, base, "h")
    y -= HEAD_H + 6*mm

    # Legende
    c.setFont(F_REG, 8); c.setFillColor(BLAU_60)
    c.drawString(ML, y, "Ja = verrechenbar  ·  tw. = teilweise verrechenbar  ·  "
                        f"Verrechenbare Stunden gesamt: {gesamt_str} h")

    # Dokument endet nach Summe + Legende (Rechnungsanlage, keine Unterschrift nötig).
    fusszeile()
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

sel_key = (sel_monat, sel_kunde, sel_auftrag)

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

    # KI-Bezüge in der kundenseitigen Beschreibung neutralisieren (nur fürs PDF)
    ki_aenderungen = []
    for e in eintraege:
        neu, geaendert = neutralisiere_ki(e["leistung"])
        if geaendert:
            ki_aenderungen.append({"datum": e["datum"], "alt": e["leistung"], "neu": neu})
            e["leistung"] = neu

    gesamt = sum(e["stunden"] for e in eintraege)
    pdf_auftrag = sel_auftrag if sel_auftrag not in (ALLE_AUFTRAEGE, OHNE_AUFTRAG) else None

    with st.spinner("Erstelle PDF..."):
        pdf_bytes = erstelle_pdf(sel_kunde, eintraege, sel_monat, auftrag_label=pdf_auftrag)

    sicher = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in sel_kunde).strip()
    auftrag_teil = ""
    if sel_auftrag not in (ALLE_AUFTRAEGE, OHNE_AUFTRAG):
        kurz = sel_auftrag.replace("Auftrag:", "").strip()
        kurz = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in kurz).strip()
        auftrag_teil = f"_{kurz}"
    dateiname = f"Leistungsnachweis_{sel_monat}_{sicher}{auftrag_teil}.pdf"

    # Ergebnis merken — bleibt über Reruns (Download, Mail) hinweg erhalten
    st.session_state["pdf_result"] = {
        "key": sel_key, "bytes": pdf_bytes, "name": dateiname,
        "n": len(eintraege), "gesamt": gesamt, "ki": ki_aenderungen,
        "monat_label": sel_label, "kunde": sel_kunde, "auftrag": pdf_auftrag,
    }

# ── Ergebnis anzeigen (unabhängig vom Button-Klick, damit es erhalten bleibt) ──
res = st.session_state.get("pdf_result")
if res and res["key"] != sel_key:
    st.info("Auswahl geändert — bitte erneut auf „📄 PDF generieren" klicken, "
            "um den Nachweis für die aktuelle Auswahl zu erzeugen.")
elif res:
    gstr = f"{res['gesamt']:.2f}".replace(".", ",")

    if res["ki"]:
        with st.expander(f"⚠️ {len(res['ki'])} Eintrag/Einträge mit KI-Bezug neutralisiert", expanded=True):
            st.caption("Diese Hinweise auf KI-Tools wurden im PDF durch eine neutrale Formulierung "
                       "ersetzt. Die Quelle in monday.com bleibt unverändert — bei Bedarf dort anpassen.")
            for a in res["ki"]:
                st.markdown(f"- **{a['datum']}**: ~~{a['alt']}~~ → **{a['neu']}**")

    c1, c2 = st.columns(2)
    c1.metric("Verrechenbare Einträge", res["n"])
    c2.metric("Stunden gesamt", f"{gstr} h")

    st.success(f"PDF erstellt — {res['n']} Einträge, {gstr} h verrechenbar")
    st.download_button(
        label="⬇️ PDF herunterladen",
        data=res["bytes"],
        file_name=res["name"],
        mime="application/pdf",
        use_container_width=True,
        type="primary",
    )
    st.caption(f"Gespeichert als **{res['name']}** — zur Weitergabe an den Innendienst "
               "für die Abrechnung.")

    # Übergangslösung: PDF per Outlook an den Innendienst
    with st.expander("📧 Per Outlook an den Innendienst senden"):
        if "empf_innendienst" not in st.session_state:
            try:
                st.session_state["empf_innendienst"] = st.secrets.get("INNENDIENST_EMAIL", "")
            except Exception:
                st.session_state["empf_innendienst"] = ""
        empf = st.text_input("E-Mail Innendienst", key="empf_innendienst",
                             placeholder="kollegin@alcanzar.de")
        betreff = f"Leistungsnachweis {res['monat_label']} – {res['kunde']}"
        if res["auftrag"]:
            betreff += f" ({res['auftrag']})"
        body = (
            f"Hallo,%0D%0A%0D%0A"
            f"anbei der Leistungsnachweis für {res['kunde']}, {res['monat_label']}, zur Abrechnung.%0D%0A"
            f"Verrechenbare Stunden gesamt: {gstr} h.%0D%0A%0D%0A"
            f"Viele Grüße"
        )
        mailto = f"mailto:{empf}?subject={betreff}&body={body}"
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
        st.caption(f"Bitte das PDF **{res['name']}** aus dem Download-Ordner als Anhang hinzufügen "
                   "(ein Mail-Link kann keine Datei automatisch anhängen).")
