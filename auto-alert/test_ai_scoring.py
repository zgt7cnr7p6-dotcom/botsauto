"""Quick test: push een nep-listing door de AI scoring pipeline."""
import os
import sys

# Check API key
if not os.environ.get("ANTHROPIC_API_KEY"):
    print("❌ ANTHROPIC_API_KEY niet gezet")
    sys.exit(1)

from scraper import Listing, score_listing_ai, FEATURE_DISPLAY_NAMES, FULL_OPTION_FEATURES

TEST_DESCRIPTION = """Fahrzeug-Nummer: 1046287 Dieses Fahrzeug erfüllt das Hersteller Qualitäts Siegel: Audi GW :plus
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

lst = Listing(
    id="test-1046287",
    source="test",
    title="Audi Q3 Sportback 45 TFSI e",
    price=32995,
    year=2022,
    km=73006,
    url="https://example.com/test",
    description=TEST_DESCRIPTION,
)

print(f"📝 Test listing: {lst.title}")
print(f"📏 Beschrijving: {len(lst.description)} chars")
print()

result = score_listing_ai(lst)

if result:
    print(f"✅ AI scoring gelukt! Score: {result.score}/18")
    print()
    for f in FULL_OPTION_FEATURES:
        name = FEATURE_DISPLAY_NAMES.get(f, f)
        if f in result.features:
            print(f"  ✅ {name}")
        else:
            print(f"  ❌ {name}")
else:
    print("❌ AI scoring MISLUKT — check model ID en API key")
    sys.exit(1)
