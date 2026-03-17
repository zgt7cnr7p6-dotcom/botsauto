"""Quick test: push nep-listings door de AI scoring pipeline."""
import os
import sys

# Check API key
if not os.environ.get("ANTHROPIC_API_KEY"):
    print("❌ ANTHROPIC_API_KEY niet gezet")
    sys.exit(1)

from scraper import Listing, score_listing_ai, FEATURE_DISPLAY_NAMES, FULL_OPTION_FEATURES


def run_test(label, listing, require_pano=False):
    print(f"\n{'='*60}")
    print(f"🧪 TEST: {label}")
    print(f"📝 {listing.title}")
    print(f"📏 Beschrijving: {len(listing.description)} chars")
    if require_pano:
        print(f"🔍 URL 2 modus: pano vereist!")
    print()

    result = score_listing_ai(listing)

    if not result:
        print("❌ AI scoring MISLUKT — check model ID en API key")
        return False

    print(f"✅ AI scoring gelukt! Score: {result.score}/18")
    print()
    for f in FULL_OPTION_FEATURES:
        name = FEATURE_DISPLAY_NAMES.get(f, f)
        if f in result.features:
            print(f"  ✅ {name}")
        else:
            print(f"  ❌ {name}")

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


# ── Run tests ──

ok = True
ok = run_test("URL 1 — listing MET panoramadak", lst1) and ok
ok = run_test("URL 2 — listing ZONDER panoramadak (moet gefilterd worden)", lst2, require_pano=True) and ok

print(f"\n{'='*60}")
if ok:
    print("✅ Alle tests geslaagd!")
else:
    print("❌ Een of meer tests gefaald")
    sys.exit(1)
