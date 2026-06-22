"""
leistungsnachweis_sync.py
Alcanzar GmbH — Leistungsnachweis-Synchronisation
==================================================
Liest Subitems aus dem Arbeitszeiterfassungs-Board und synchronisiert
sie ins Leistungsnachweise-Board. Kein LLM, kein Agent — deterministisch.

Verarbeitet:
  - Aktueller Monat: 1. bis heute
  - Vormonat: vollständig

Duplikatschutz: Schlüssel = datum|person_name|kunden_item_id
Kein Eintrag wird doppelt angelegt, egal wie oft das Skript läuft.

Verwendung:
  python leistungsnachweis_sync.py
  python leistungsnachweis_sync.py --monat 2026-05   # nur bestimmter Monat
  python leistungsnachweis_sync.py --dry-run          # nur anzeigen, nichts schreiben

Konfiguration: API-Token in Umgebungsvariable MONDAY_API_TOKEN
  oder direkt in der Konstante API_TOKEN unten eintragen.
"""

import os
import sys
import json
import argparse
import requests
from datetime import date, datetime
from collections import defaultdict
from calendar import monthrange

# ── Konfiguration ────────────────────────────────────────────────────────────

API_TOKEN = os.environ.get("MONDAY_API_TOKEN", "HIER_API_TOKEN_EINTRAGEN")
API_URL   = "https://api.monday.com/v2"

# Board-IDs
BOARD_SUBITEMS       = 5097254860   # Subitems of Arbeitszeiterfassung
BOARD_AZ             = 5094282328   # Arbeitszeiterfassung (Parent-Items)
BOARD_LEISTUNG       = 5097778382   # Leistungsnachweise [TEST]

# Spalten-IDs Subitems-Board
COL_SUB_KUNDE        = "board_relation_mm3qpehw"
COL_SUB_STUNDEN      = "numeric_mm3qsmbm"
COL_SUB_TAETIGKEIT   = "dropdown_mm3q3ggs"
COL_SUB_VERRECHENBAR = "color_mm3qgnvz"
COL_SUB_BESCHREIBUNG = "text_mm3zvc8n"

# Spalten-IDs Arbeitszeiterfassung-Board (Parent)
COL_AZ_MITARBEITER   = "multiple_person_mm3qhqt3"
COL_AZ_DATUM         = "date4"

# Spalten-IDs Leistungsnachweise-Board
COL_LN_KUNDE         = "board_relation_mm3z3jnk"
COL_LN_DATUM         = "date_mm3zzepy"
COL_LN_MITARBEITER   = "multiple_person_mm3zpgmx"
COL_LN_STUNDEN       = "numeric_mm3zfzkc"
COL_LN_LEISTUNG      = "text_mm3zzr65"
COL_LN_VERRECHENBAR  = "color_mm3znz4s"
COL_LN_STATUS        = "color_mm3ztsba"

# Steuerungs-Item — wird beim Duplikat-Check ignoriert
STEUERUNGS_ITEM_NAME = "▶ Nachweise generieren"

# ── GraphQL-Hilfsfunktionen ──────────────────────────────────────────────────

def gql(query: str, variables: dict = None) -> dict:
    """Führt eine GraphQL-Anfrage aus und gibt die Daten zurück."""
    headers = {
        "Authorization": API_TOKEN,
        "Content-Type": "application/json",
        "API-Version": "2024-10",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    r = requests.post(API_URL, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL-Fehler: {data['errors']}")
    return data.get("data", {})


def paginate_items(board_id: int, column_ids: list) -> list:
    """Lädt alle Items eines Boards seitenweise (cursor-based)."""
    query = """
    query($board_id: ID!, $cursor: String, $col_ids: [String!]) {
      boards(ids: [$board_id]) {
        items_page(limit: 100, cursor: $cursor) {
          cursor
          items {
            id
            name
            parent_item { id }
            column_values(ids: $col_ids) {
              id
              text
              value
            }
          }
        }
      }
    }
    """
    items = []
    cursor = None
    while True:
        variables = {"board_id": str(board_id), "col_ids": column_ids, "cursor": cursor}
        data = gql(query, variables)
        page = data["boards"][0]["items_page"]
        items.extend(page["items"])
        cursor = page.get("cursor")
        if not cursor:
            break
    return items


def paginate_subitems(board_id: int) -> list:
    """
    Lädt alle Subitems eines Boards mit BoardRelationValue inline fragment.
    Nötig weil text/value für board_relation in Subitems leer zurückkommt —
    linked_items liefert die verknüpften Kunden korrekt.
    """
    query = """
    query($board_id: ID!, $cursor: String) {
      boards(ids: [$board_id]) {
        items_page(limit: 100, cursor: $cursor) {
          cursor
          items {
            id
            name
            parent_item { id }
            column_values(ids: ["board_relation_mm3qpehw", "numeric_mm3qsmbm",
                                 "dropdown_mm3q3ggs", "color_mm3qgnvz", "text_mm3zvc8n"]) {
              id text value
              ... on BoardRelationValue {
                linked_items { id name }
              }
            }
          }
        }
      }
    }
    """
    items = []
    cursor = None
    while True:
        variables = {"board_id": str(board_id), "cursor": cursor}
        data = gql(query, variables)
        page = data["boards"][0]["items_page"]
        items.extend(page["items"])
        cursor = page.get("cursor")
        if not cursor:
            break
    return items


# ── Datums-Hilfsfunktionen ───────────────────────────────────────────────────

def zeitraeume(monat_override: str = None):
    """
    Gibt zwei Zeiträume zurück: (aktueller_monat, vormonat).
    Jeder Zeitraum ist ein Tupel (date_von, date_bis).
    """
    heute = date.today()

    if monat_override:
        # Nur einen bestimmten Monat verarbeiten
        y, m = map(int, monat_override.split("-"))
        erster = date(y, m, 1)
        letzter = date(y, m, monthrange(y, m)[1])
        return [(erster, letzter)]

    # Aktueller Monat: 1. bis heute
    aktuell_von = date(heute.year, heute.month, 1)
    aktuell_bis = heute

    # Vormonat: vollständig
    if heute.month == 1:
        vm_year, vm_month = heute.year - 1, 12
    else:
        vm_year, vm_month = heute.year, heute.month - 1
    vormonat_von = date(vm_year, vm_month, 1)
    vormonat_bis = date(vm_year, vm_month, monthrange(vm_year, vm_month)[1])

    return [(aktuell_von, aktuell_bis), (vormonat_von, vormonat_bis)]


def datum_in_zeitraum(datum_str: str, zeitraum_liste: list) -> bool:
    """Prüft ob ein Datum in einem der Zeiträume liegt."""
    if not datum_str:
        return False
    try:
        d = date.fromisoformat(datum_str[:10])
    except ValueError:
        return False
    return any(von <= d <= bis for von, bis in zeitraum_liste)


def monatsname_de(d: date) -> str:
    monate = ["Januar","Februar","März","April","Mai","Juni",
              "Juli","August","September","Oktober","November","Dezember"]
    return monate[d.month - 1]


# ── Board-Daten laden ────────────────────────────────────────────────────────

def lade_gruppen(board_id: int) -> dict:
    """Gibt dict {gruppenname: group_id} zurück."""
    query = """
    query($board_id: ID!) {
      boards(ids: [$board_id]) {
        groups { id title }
      }
    }
    """
    data = gql(query, {"board_id": str(board_id)})
    return {g["title"]: g["id"] for g in data["boards"][0]["groups"]}


def lade_mitarbeiter_ids(board_id: int) -> dict:
    """Gibt dict {person_name: person_id} aus People-Spalte zurück."""
    # Wir lesen die IDs direkt aus den Items beim Laden
    return {}


def lade_bestehende_items_leistung() -> set:
    """
    Lädt alle bestehenden Items im Leistungsnachweise-Board.
    Gibt ein Set von Lookup-Schlüsseln zurück:
    {datum|person_name|kunden_item_id}
    Nutzt BoardRelationValue inline fragment — text/value liefert None für board_relation.
    """
    query = """
    query($board_id: ID!, $cursor: String) {
      boards(ids: [$board_id]) {
        items_page(limit: 100, cursor: $cursor) {
          cursor
          items {
            id name
            column_values(ids: ["date_mm3zzepy", "multiple_person_mm3zpgmx", "board_relation_mm3z3jnk"]) {
              id text value
              ... on BoardRelationValue {
                linked_items { id name }
              }
            }
          }
        }
      }
    }
    """
    keys = set()
    cursor = None
    while True:
        data = gql(query, {"board_id": str(BOARD_LEISTUNG), "cursor": cursor})
        page = data["boards"][0]["items_page"]
        for item in page["items"]:
            if item["name"] == STEUERUNGS_ITEM_NAME:
                continue
            col = {c["id"]: c for c in item["column_values"]}

            datum = (col.get(COL_LN_DATUM, {}).get("text") or "")[:10]
            mitarbeiter = col.get(COL_LN_MITARBEITER, {}).get("text") or ""
            linked = col.get(COL_LN_KUNDE, {}).get("linked_items") or []
            kunden_id = str(linked[0]["id"]) if linked else ""

            if datum and mitarbeiter:
                keys.add(f"{datum}|{mitarbeiter}|{kunden_id}")

        cursor = page.get("cursor")
        if not cursor:
            break
    return keys


def lade_az_items() -> dict:
    """Lädt Parent-Items aus Arbeitszeiterfassung. Gibt {item_id: {mitarbeiter, datum}} zurück."""
    items = paginate_items(BOARD_AZ, [COL_AZ_MITARBEITER, COL_AZ_DATUM])
    result = {}
    for item in items:
        col = {c["id"]: c for c in item["column_values"]}
        mitarbeiter = ""
        mitarbeiter_ids = []
        datum = ""

        if COL_AZ_MITARBEITER in col and col[COL_AZ_MITARBEITER]["value"]:
            try:
                val = json.loads(col[COL_AZ_MITARBEITER]["value"])
                persons = val.get("personsAndTeams", [])
                mitarbeiter_ids = [str(p["id"]) for p in persons if p.get("kind") == "person"]
                mitarbeiter = col[COL_AZ_MITARBEITER]["text"] or ""
            except (json.JSONDecodeError, KeyError):
                pass

        if COL_AZ_DATUM in col:
            datum = (col[COL_AZ_DATUM]["text"] or "")[:10]

        result[item["id"]] = {
            "mitarbeiter": mitarbeiter,
            "mitarbeiter_ids": mitarbeiter_ids,
            "datum": datum,
        }
    return result


def lade_subitems(zeitraum_liste: list) -> tuple:
    """
    Lädt alle relevanten Subitems aus dem Subitems-Board.
    Filtert auf Zeiträume. Trennt Einträge mit und ohne Kunden-Verknüpfung.
    Gibt (relevante, ohne_kunde) zurück.
    """
    items = paginate_subitems(BOARD_SUBITEMS)

    az_items = lade_az_items()
    print(f"  → {len(az_items)} AZ-Parent-Items geladen")

    relevante = []
    ohne_kunde = []
    for item in items:
        parent_id = item.get("parent_item", {})
        if not parent_id:
            continue
        parent_id = parent_id.get("id", "")
        parent = az_items.get(parent_id, {})
        datum = parent.get("datum", "")

        if not datum_in_zeitraum(datum, zeitraum_liste):
            continue

        col = {c["id"]: c for c in item["column_values"]}

        taetigkeit = col.get(COL_SUB_TAETIGKEIT, {}).get("text", "") or ""
        verrechenbar = col.get(COL_SUB_VERRECHENBAR, {}).get("text", "") or ""
        beschreibung = col.get(COL_SUB_BESCHREIBUNG, {}).get("text", "") or ""
        leistung = beschreibung if beschreibung else taetigkeit

        stunden_raw = col.get(COL_SUB_STUNDEN, {}).get("text", "") or ""
        try:
            stunden = float(stunden_raw) if stunden_raw else 0.0
        except ValueError:
            stunden = 0.0

        # Kunden-Verknüpfung auslesen via linked_items (text/value liefert None für Subitems)
        kunden_item_id = ""
        kunden_name = ""
        if COL_SUB_KUNDE in col:
            linked_items = col[COL_SUB_KUNDE].get("linked_items") or []
            if linked_items:
                kunden_item_id = str(linked_items[0]["id"])
                kunden_name = linked_items[0]["name"]

        if not kunden_item_id:
            ohne_kunde.append({
                "subitem_id": item["id"],
                "datum": datum,
                "mitarbeiter": parent.get("mitarbeiter", ""),
                "leistung": leistung,
                "stunden": stunden,
            })
            continue

        relevante.append({
            "subitem_id": item["id"],
            "parent_id": parent_id,
            "datum": datum,
            "mitarbeiter": parent.get("mitarbeiter", ""),
            "mitarbeiter_ids": parent.get("mitarbeiter_ids", []),
            "kunden_item_id": kunden_item_id,
            "kunden_name": kunden_name,
            "stunden": stunden,
            "leistung": leistung,
            "verrechenbar": verrechenbar,
        })

    return relevante, ohne_kunde


# ── Gruppe anlegen / holen ───────────────────────────────────────────────────

def stelle_sicher_gruppe(gruppenname: str, gruppen_cache: dict, dry_run: bool) -> str:
    """Gibt group_id zurück. Legt Gruppe an falls nicht vorhanden."""
    if gruppenname in gruppen_cache:
        return gruppen_cache[gruppenname]

    if dry_run:
        print(f"    [DRY-RUN] Würde Gruppe anlegen: {gruppenname}")
        gruppen_cache[gruppenname] = f"dry_run_{gruppenname}"
        return gruppen_cache[gruppenname]

    query = """
    mutation($board_id: ID!, $name: String!) {
      create_group(board_id: $board_id, group_name: $name) { id }
    }
    """
    data = gql(query, {"board_id": str(BOARD_LEISTUNG), "name": gruppenname})
    group_id = data["create_group"]["id"]
    gruppen_cache[gruppenname] = group_id
    print(f"    ✓ Gruppe angelegt: {gruppenname}")
    return group_id


# ── Item anlegen ─────────────────────────────────────────────────────────────

def lege_item_an(eintrag: dict, group_id: str, dry_run: bool) -> bool:
    """Legt ein neues Item im Leistungsnachweise-Board an."""
    datum_obj = date.fromisoformat(eintrag["datum"])
    item_name = f"{datum_obj.strftime('%d.%m.%Y')} – {eintrag['mitarbeiter']}"

    # Verrechenbar-Status mappen
    verrechenbar_label = eintrag["verrechenbar"]
    if verrechenbar_label not in ("Ja", "Nein", "Teilweise"):
        verrechenbar_label = "Nein"

    column_values = {
        COL_LN_DATUM:       {"date": eintrag["datum"]},
        COL_LN_STUNDEN:     eintrag["stunden"],
        COL_LN_LEISTUNG:    eintrag["leistung"],
        COL_LN_VERRECHENBAR: {"label": verrechenbar_label},
        COL_LN_STATUS:       {"label": "In Vorbereitung"},
        COL_LN_KUNDE:        {"item_ids": [int(eintrag["kunden_item_id"])]},
    }

    # Mitarbeiter-ID setzen wenn vorhanden
    if eintrag["mitarbeiter_ids"]:
        column_values[COL_LN_MITARBEITER] = {
            "personsAndTeams": [
                {"id": int(pid), "kind": "person"}
                for pid in eintrag["mitarbeiter_ids"]
            ]
        }

    if dry_run:
        print(f"    [DRY-RUN] Würde anlegen: {item_name} | {eintrag['stunden']}h | {eintrag['kunden_name']}")
        return True

    query = """
    mutation($board_id: ID!, $group_id: String!, $name: String!, $col_vals: JSON!) {
      create_item(
        board_id: $board_id,
        group_id: $group_id,
        item_name: $name,
        column_values: $col_vals
      ) { id }
    }
    """
    variables = {
        "board_id": str(BOARD_LEISTUNG),
        "group_id": group_id,
        "name": item_name,
        "col_vals": json.dumps(column_values),
    }
    gql(query, variables)
    return True


# ── Hauptlogik ───────────────────────────────────────────────────────────────

def _zeige_ohne_kunde(ohne_kunde: list):
    """Gibt eine übersichtliche Liste der Einträge ohne Kunden-Zuordnung aus."""
    print("\n" + "-" * 60)
    print(f"HINWEIS: {len(ohne_kunde)} Einträge fehlt die Kunden-Zuordnung.")
    print("Bitte in monday.com das Feld 'Kunden' im jeweiligen Subitem befüllen:")
    print("-" * 60)
    MAX_ANZEIGE = 30
    for e in ohne_kunde[:MAX_ANZEIGE]:
        leistung_kurz = (e["leistung"] or "–")[:45]
        print(f"  {e['datum']}  {e['mitarbeiter']:<20s}  {e['stunden']:4.1f}h  {leistung_kurz}")
    if len(ohne_kunde) > MAX_ANZEIGE:
        print(f"  ... und {len(ohne_kunde) - MAX_ANZEIGE} weitere")
    print("-" * 60)


def sync(monat_override: str = None, dry_run: bool = False):
    print("=" * 60)
    print("Alcanzar — Leistungsnachweis Sync")
    print(f"Datum: {date.today().isoformat()}")
    if dry_run:
        print("MODUS: DRY-RUN (keine Schreibvorgänge)")
    print("=" * 60)

    # 1. Zeiträume
    zeitraum_liste = zeitraeume(monat_override)
    for von, bis in zeitraum_liste:
        print(f"  Zeitraum: {von} bis {bis}")

    # 2. Bestehende Items laden → Duplikat-Lookup
    print("\n[1] Lade bestehende Leistungsnachweise...")
    bestehende_keys = lade_bestehende_items_leistung()
    print(f"  → {len(bestehende_keys)} bestehende Einträge als Duplikat-Schutz geladen")

    # 3. Subitems laden
    print("\n[2] Lade Subitems aus Arbeitszeiterfassung...")
    subitems, ohne_kunde = lade_subitems(zeitraum_liste)
    print(f"  → {len(subitems)} relevante Subitems gefunden")
    if ohne_kunde:
        print(f"  → {len(ohne_kunde)} Subitems ohne Kunden-Zuordnung (werden übersprungen)")

    if not subitems:
        print("\nKeine Daten zum Synchronisieren. Fertig.")
        if ohne_kunde:
            _zeige_ohne_kunde(ohne_kunde)
        return

    # 4. Gruppen-Cache laden
    print("\n[3] Lade bestehende Gruppen...")
    gruppen_cache = lade_gruppen(BOARD_LEISTUNG)
    print(f"  → {len(gruppen_cache)} Gruppen im Board")

    # 5. Subitems nach Monat + Kunde gruppieren
    gruppen_data = defaultdict(list)
    for s in subitems:
        d = date.fromisoformat(s["datum"])
        gruppenname = f"{monatsname_de(d)} {d.year} – {s['kunden_name']}"
        gruppen_data[gruppenname].append(s)

    # 6. Einträge synchronisieren
    print(f"\n[4] Synchronisiere {len(gruppen_data)} Kunde/Monat-Kombinationen...")
    neu_gesamt = 0
    duplikate_gesamt = 0

    for gruppenname, eintraege in sorted(gruppen_data.items()):
        print(f"\n  Gruppe: {gruppenname} ({len(eintraege)} Einträge)")

        group_id = stelle_sicher_gruppe(gruppenname, gruppen_cache, dry_run)
        eintraege_sortiert = sorted(eintraege, key=lambda x: x["datum"])

        neu = 0
        duplikate = 0

        for e in eintraege_sortiert:
            lookup_key = f"{e['datum']}|{e['mitarbeiter']}|{e['kunden_item_id']}"

            if lookup_key in bestehende_keys:
                duplikate += 1
                continue

            try:
                lege_item_an(e, group_id, dry_run)
                bestehende_keys.add(lookup_key)  # sofort eintragen
                neu += 1
            except Exception as ex:
                print(f"    ✗ Fehler bei {e['datum']} {e['mitarbeiter']}: {ex}")

        print(f"    → {neu} neu angelegt, {duplikate} Duplikate übersprungen")
        neu_gesamt += neu
        duplikate_gesamt += duplikate

    # 7. Zusammenfassung
    print("\n" + "=" * 60)
    print("Zusammenfassung:")
    print(f"  Neu angelegt:          {neu_gesamt}")
    print(f"  Duplikate übersprungen: {duplikate_gesamt}")
    print(f"  Gruppen verarbeitet:    {len(gruppen_data)}")
    print("=" * 60)

    if ohne_kunde:
        _zeige_ohne_kunde(ohne_kunde)


# ── Einstiegspunkt ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Alcanzar Leistungsnachweis Sync"
    )
    parser.add_argument(
        "--monat",
        help="Nur diesen Monat verarbeiten, Format YYYY-MM (z.B. 2026-05)",
        default=None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Nur anzeigen was passieren würde, nichts schreiben",
    )
    args = parser.parse_args()

    if API_TOKEN == "HIER_API_TOKEN_EINTRAGEN":
        print("FEHLER: Bitte API_TOKEN konfigurieren.")
        print("  Option 1: Umgebungsvariable setzen:")
        print("    set MONDAY_API_TOKEN=dein_token_hier  (Windows)")
        print("  Option 2: Token direkt in Zeile 34 eintragen")
        sys.exit(1)

    sync(monat_override=args.monat, dry_run=args.dry_run)
