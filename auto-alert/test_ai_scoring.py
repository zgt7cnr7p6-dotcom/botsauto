"""Quick test: push nep-listings door de AI scoring pipeline."""
import os
import sys

# Check API key
if not os.environ.get("ANTHROPIC_API_KEY"):
    print("❌ ANTHROPIC_API_KEY niet gezet")
    sys.exit(1)

from scraper import Listing, score_listing_ai, score_listing_regex, score_listing, FEATURE_DISPLAY_NAMES, FULL_OPTION_FEATURES, MUST_HAVE_FEATURES


def run_test(label, listing, require_pano=False, expected_true=None, expected_false=None):
    print(f"\n{'='*60}")
    print(f"🧪 TEST: {label}")
    print(f"📝 {listing.title}")
    print(f"📏 Beschrijving: {len(listing.description)} chars")
    if require_pano:
        print(f"🔍 URL 2 modus: pano vereist!")
    print()

    # Test hybrid scoring (AI + regex)
    result = score_listing(listing)

    if not result:
        print("❌ Scoring MISLUKT")
        return False

    max_score = len(FULL_OPTION_FEATURES)
    print(f"✅ Score: {result.score}/{max_score}")
    print()
    for f in FULL_OPTION_FEATURES:
        name = FEATURE_DISPLAY_NAMES.get(f, f)
        star = " ⭐" if f in MUST_HAVE_FEATURES else ""
        if f in result.features:
            print(f"  ✅ {name}{star}")
        else:
            print(f"  ❌ {name}{star}")

    # Check verwachte resultaten
    ok = True
    if expected_true:
        missed = [f for f in expected_true if f not in result.features]
        if missed:
            print(f"\n⚠️  GEMIST (had true moeten zijn):")
            for f in missed:
                print(f"  ❌ {FEATURE_DISPLAY_NAMES.get(f, f)}")
            ok = False
    if expected_false:
        false_pos = [f for f in expected_false if f in result.features]
        if false_pos:
            print(f"\n⚠️  FALSE POSITIVE (had false moeten zijn):")
            for f in false_pos:
                print(f"  ✅ {FEATURE_DISPLAY_NAMES.get(f, f)} (onterecht)")
            ok = False

    if ok and (expected_true or expected_false):
        print(f"\n✅ Alle verwachte resultaten kloppen!")

    if require_pano:
        has_pano = "panoramadak" in result.features
        print()
        if has_pano:
            print(f"  ➡️  URL 2 resultaat: DOORGESTUURD (pano gevonden)")
        else:
            print(f"  ➡️  URL 2 resultaat: GEFILTERD (geen pano)")

    return True


# ── Test 1: Volledige listing MET panoramadak (URL 1 style) ──

TEST_1_DESC = """Fahrzeug-Nummer: 1046287 Dieses Fahrzeug erfüllt das Hersteller Qualitäts Siegel: Audi GW :plus
Ehem. empfohlener Verkaufspreis (UPE) 63.195 EUR Hybrid Benzin
Sportpaket Assistenz-Paket Business-Paket Optikpaket schwarz Ambiente-Lichtpaket plus S line Paket
Klimaautomatik 2 Zonen Elektrische Fensterheber Sitzheizung Einpark-Assistent Tempo-Begrenzer
Komfortschlüssel/KeylessStart Leder-Lenkrad Lichtsensor Wärmedämmendes Glas
Spiegel (el.) klappbar & heizbar Rückfahrkamera Elektronische Parkbremse Anfahrassistent
Fernlicht-Assistent Keyless Entry Innenspiegel autom.abblendend
Außenspiegel mit Bordsteinautomatik, rechts Standklimatisierung
Window/Kopfairbags ESP Elektronische Wegfahrsperre Reifendruckkontrolle
Waschdüsen beheizt Spurhalteassistent Auffahr-Warnsystem LED-Tagfahrlicht
Notrufsystem Notbremsassistent Airbags vorn Kindersicherung elektrisch betätigt
Audi pre sense basic Akustischer Fußgängerschutz
Alufelgen 19 Zoll Einpark-Assistent Anhängerkupplung Spoiler hi.
Panorama-Integral-Dach Heckklappe elektrisch Voll LED-Scheinwerfer
Einstiegsleisten mit Aluminiumeinlegern vorn beleuchtet S-Schriftzug
Ladekantenschutz aus Edelstahl Außenspiegelgehäuse in Wagenfarbe
Blinkleuchten LED in Außenspiegel integriert
Leder - Stoff Interieurfarbe Schwarz Becherhalter Isofix-System Mittelarmlehne
Sitzhöhenverstellung 5 Sitzplätze Armlehne(n) Lendenwirbelstütze(n) verstellbar
Ambiente-Beleuchtung Audi virtual cockpit plus Interieur S line
Pedalerie und Fußstütze aus Edelstahl Dachhimmel in Stoff schwarz
Aluminiumoptik im Interieur Akzentflächen schwarz glänzend Fußmatten vorn und hinten
Audi connect Navigation & Infotainment plus Radio Bluetooth Anbindung
Digitales Cockpit 6 Lautsprecher Smartphone-Interface
S Tronic-Automatik
ABS Traktionskontrolle Tempo-Begrenzer Servo-Lenkung Bordcomputer Sprachbedienung
Schadstoffklasse Euro 6d Multifunktionslenkrad Partikelfilter Umweltplakette grün
Energierückgewinnung (Rekuperation) Motor-Start-Stopp-Funktion
Verkehrsschilder-Assistent Start-Stop-Knopf Halteassistent
Adaptiver Fahrassistent Komfort-Fahrwerk Audi drive select ECO-Funktion
Progressivlenkung Industriestecker CEE 16A 400V für das e-tron Ladesystem
e-tron Aufladesystem Plug-in Hybrid
Sport-Fahrwerk Sport-Sitz(e) Sportausführung Sport-Ausstattung
Scheckheftgepflegt Garantie Nichtraucherfahrzeug
HU/AU neu TYP 2 Unfallfrei Leasing-Fzg
Dekoreinlagen Aluminium matt gebürstet dunkel Dynamisches Blinklicht Heck
Notfall-Assistent Audi connect Remote & Control für MMI Navigation plus
Bremsassistent Audi pre sense front Elektromotor 85 kW Hybridantrieb"""

lst1 = Listing(
    id="test-met-pano",
    source="test",
    title="Audi Q3 Sportback 45 TFSI e",
    price=32995,
    year=2022,
    km=73006,
    url="https://example.com/test1",
    description=TEST_1_DESC,
)


# ── Test 2: Listing ZONDER panoramadak (URL 2 → moet gefilterd worden) ──

TEST_2_DESC = """Fahrzeug-Nummer: 9999999 Hybrid Benzin
S line Paket Klimaautomatik 2 Zonen Elektrische Fensterheber Sitzheizung
Komfortschlüssel/KeylessStart Rückfahrkamera Keyless Entry
Spurhalteassistent Notbremsassistent Audi pre sense basic
Alufelgen 19 Zoll Voll LED-Scheinwerfer
Leder - Stoff Ambiente-Beleuchtung Interieur S line
S Tronic-Automatik ABS Adaptiver Fahrassistent
Audi drive select ECO-Funktion Plug-in Hybrid
Sport-Fahrwerk Scheckheftgepflegt Garantie Nichtraucherfahrzeug
Bremsassistent Audi pre sense front"""

lst2 = Listing(
    id="test-zonder-pano",
    source="test",
    title="Audi Q3 Sportback 45 TFSI e",
    price=29995,
    year=2021,
    km=85000,
    url="https://example.com/test2",
    description=TEST_2_DESC,
)


# ── Test 3: Koblenz listing — ChatGPT vindt 17 opties, bot moet dat ook vinden ──

TEST_3_DESC = """Finanzierungsbeispiel
Audi Bank
Monatliche Rate: 359,00 EUR
Anzahlung: 12.580,00 EUR
Laufzeit: 36 Monate bei 10.000 km/Jahr
Eff. Jahreszins: 4,99%
Schlussrate: 12.730,29 EUR
Sollzinssatz: 4,88% (Gebunden EUR
Nettokreditbetrag: 23.000,00 EUR
Bruttokreditbetrag: 25.654,29 EUR
Ratenabsicherung: 0,00 EUR
Ausstattungspakete
•	Business Paket
•	S-Line Interieurpaket
•	Glanzpaket
•	S line Sportpaket
•	S tronic
Multimedia & Kommunikation
•	MMI Navigation plus
•	USB hinten
•	6 Lautsprecher
•	Audi connect
•	Audi connect Navigation & Infotainment
•	Audi music interface
•	Audi Sound System
•	Audi virtual cockpit
•	Bluetooth Schnittstelle
•	Connect Plus
•	DAB Empfang
•	e-Sound
•	Ladekabel mit Industriestecker CEE
•	MMI Touch
•	Netzladekabel
•	Notruf-Service
•	Sprachdialogsystem
•	Sprachsteuerung
•	USB-Anschluss
Sicherheit
•	Audi pre sense basic
•	Einparkhilfe vorne und hinten
•	Berganfahrassistent
•	Top View Kamera
•	ABS (Antiblockiersystem)
•	Airbag Fahrer und Beifahrer
•	Antischlupfregelung (ASR)
•	Audi pre sense front
•	Beifahrerairbag deaktivierbar
•	Doppelairbag
•	ESP
•	Fahrerairbag
•	Geschwindigkeitsbegrenzer
•	Kindersicherung
•	Kindersitzbefestigung ISOFIX
•	Kopfairbagsystem
•	Kopfstützen hinten
•	Lichtsensor
•	Reifendruckkontrolle
•	Seitenairbags vorne
•	Spurverlassenswarnung
•	Wegfahrsperre
Komfort
•	Multifunktionslederlenkrad
•	Multifunktionslederlenkrad mit Schaltwippen
•	Rückfahrkamera
•	Sitzheizung vorne
•	6-Gang-Doppelkupplungsgetriebe S-Tronic
•	Frontscheibe in Dämm-, Akustikglas
•	Frontscheibe in Dämm-, Wärmeschutzglas
•	Klimaautomatik Climatronic 2-Zonen
•	Schiebedach elektrisch
•	Standklimatisierung
•	4-Wege-Lendenwirbelstütze
•	Adaptiver Geschwindigkeitsassistent
•	Ambientebeleuchtung
•	Automatische Distanzregelung ACC
•	Drive select
•	Fernlichtassistent
•	Geschwindigkeitsregelanlage
•	Heckklappe elektrisch (öffnen und schließen)
•	Komfortfahrwerk
•	Komfortschlüssel
•	Lendenwirbelstütze elektrisch
•	Mittelarmlehne hinten
•	Mittelarmlehne mit Ablagebox
•	Mittelarmlehne vorn
•	Motor-Start-Stopp-System
•	Progressivlenkung
•	Regensensor
•	Rücksitzlehne geteilt umklappbar
•	Servolenkung
•	Start-/Stop-Automatik
Interieur
•	Dachhimmel Schwarz
•	Dekoreinlagen Aluminium
•	Dekoreinlagen Aluminium gebürstet
•	Dekoreinlagen Schwarz
•	Dekoreinlagen Silber
•	Dekoreinlagen Akzentteile Feinlack
•	Alcantara/Leder Kombination
•	Aluminiumoptik im Interieur
•	Einstiegsleisten mit Aluminiumeinlage und Schriftzug beleuchtet
•	Erweiterte Aluminiumoptik Interieur
•	Fußmatten
•	Innenspiegel automatisch abblendend
•	Nichtraucherausführung
•	Pedalerie und Fußstütze in Edelstahl
•	Sportsitze
•	Start-Stop-Knopf
•	Türarmauflage in Kunstleder
Exterieur
•	Leichtmetallfelgen 5-V-Speichen-Design 18 Zoll
•	Anhängerkupplung
•	Außenspiegel el. einstell-, beheiz-, anklappbar, abblendend
•	Außenspiegel el. einstell-, beheiz-, anklappbar, abblendend, Bordsteinautomatik
•	Ladekantenschutz in Edelstahl
•	Scheiben abgedunkelt (Privacy Verglasung)
•	Soundgenerator
•	Anhängerkupplung abnehmbar/ schwenkbar
Licht & Sicht
•	LED-Scheinwerfer
•	LED-Rückleuchten
•	Panoramadach
•	Tagfahrlicht
Sonstiges
•	Umgebungskameras
•	12 Volt-Steckdose
•	Anfahrassistent
•	Bremsassistent
•	Dunkel getönte Seitenscheiben
•	e-tron Ladesystem
•	Elektronische Stabilitätskontrolle (ESC)
•	Partikelfilter
•	Verkehrszeichenerkennung
•	HU+AU neu
•	Metallic-Lackierung
•	Nichtraucherfahrzeug
•	Scheckheft gepflegt"""

lst3 = Listing(
    id="test-koblenz",
    source="test",
    title="Audi Q3 Sportback 45 TFSI e",
    price=35580,
    year=2022,
    km=53203,
    url="https://example.com/test3",
    description=TEST_3_DESC,
)

# Verwachte resultaten op basis van ChatGPT analyse:
KOBLENZ_EXPECTED_TRUE = [
    "panoramadak",       # Panoramadach
    "keyless",           # Komfortschlüssel
    "camera_achteruit",  # Rückfahrkamera
    "camera_360",        # Top View Kamera + Umgebungskameras
    "s_line",            # S-Line Interieurpaket + S line Sportpaket
    "s_line_exterieur",  # S line Sportpaket
    "acc",               # Automatische Distanzregelung ACC + Adaptiver Geschwindigkeitsassistent
    "lane_assist",       # Spurverlassenswarnung
    "ambient_lighting",  # Ambientebeleuchtung
    "drive_select",      # Drive select
    "elektrische_achterklep",  # Heckklappe elektrisch
    "stoelverwarming",   # Sitzheizung vorne + Business Paket
    "emergency_assist",  # Audi pre sense basic + front
]

KOBLENZ_EXPECTED_FALSE = [
    "matrix_led",            # Alleen LED-Scheinwerfer, geen Matrix
    "audio_premium",         # Audi Sound System, geen B&O/Sonos
    "travel_assist",         # Geen Adaptiver Fahrassistent
    "side_assist",           # Geen Spurwechselassistent/Side Assist
    "adaptief_onderstel",    # Komfortfahrwerk, niet adaptief
    "dynamisch_knipperlicht", # Geen dynamisch knipperlicht
]


# ── Run tests ──

ok = True
ok = run_test("URL 1 — listing MET panoramadak", lst1) and ok
ok = run_test("URL 2 — listing ZONDER panoramadak (moet gefilterd worden)", lst2, require_pano=True) and ok
ok = run_test(
    "Koblenz listing — ChatGPT vindt 17 opties",
    lst3,
    expected_true=KOBLENZ_EXPECTED_TRUE,
    expected_false=KOBLENZ_EXPECTED_FALSE,
) and ok

print(f"\n{'='*60}")
if ok:
    print("✅ Alle tests geslaagd!")
else:
    print("❌ Een of meer tests gefaald")
    sys.exit(1)
