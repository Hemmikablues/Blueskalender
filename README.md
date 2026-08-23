# Jazzkalender, prototyp

Prototypen samlar program från sex källor och genererar tre prenumererbara iCalendar-filer:

* `alla.ics`
* `goteborg.ics`
* `stockholm.ics`

Källor: Fasching, Nefertiti, Playhouse, Skeppet GBG, Unity Jazz och Utopia Jazz via Billetto.

## Kör lokalt

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python jazz_calendar.py --output-dir public
```

Filerna hamnar i `public/`. `status.json` visar vilka källor som lyckades och `events.json` visar normaliserade evenemang för felsökning.

## Publicera som prenumeration

Projektet innehåller `.github/workflows/publish.yml`. Lägg filerna i ett GitHub-repository och aktivera **Settings > Pages > Source: GitHub Actions**. Workflow-filen kör insamlingen varje natt och publicerar katalogen `public` via GitHub Pages.

Om repositoryt exempelvis heter `jazzkalender` under användaren `namn` blir prenumerationsadresserna normalt:

```text
https://namn.github.io/jazzkalender/alla.ics
https://namn.github.io/jazzkalender/goteborg.ics
https://namn.github.io/jazzkalender/stockholm.ics
```

I Outlook väljer du att lägga till en kalender från webben och använder en av ICS-adresserna. Outlook bestämmer själv hur ofta prenumerationen hämtas om.

## Hur källorna läses

* **Fasching:** kalendariet används för att hitta evenemangssidor. Datum och scenstart hämtas från respektive sida. JSON-LD används om det finns.
* **Nefertiti:** länkar under `/nefertiti_event/` läses. Scenstart används som kalenderns starttid och insläpp läggs i beskrivningen.
* **Playhouse:** länkar under `/arrangemang/` läses. `på scen` används som starttid.
* **Skeppet GBG:** The Events Calendar REST API används i första hand. Befintligt iCalendar-flöde används som reserv.
* **Unity Jazz:** skriptet provar Squarespaces `?format=ical` för varje evenemang och faller annars tillbaka på programsidan.
* **Utopia Jazz:** skriptet letar efter Billetto-eventlänkar både på arrangörssidan och på Utopias egen webbplats. Billetto laddar delar av sidan dynamiskt, så just denna källa är mest känslig och rapporteras tydligt i `status.json` om den inte kan läsas.

## Regler i prototypen

* Tidszon: `Europe/Stockholm`.
* Scenstart prioriteras framför dörröppning när båda finns.
* Kända sluttider används. Annars används tre timmar som provisorisk längd.
* Evenemang äldre än ett dygn tas bort från den publicerade kalendern.
* Nära identiska titel, stad och starttid slås ihop för att minska dubletter.
* Ett fel i en källa stoppar inte övriga kalendrar. Statusen sparas separat.

## Begränsningar

Detta är en robust prototyp, inte ett produktionsavtal med webbplatserna. HTML-struktur och bot-skydd kan ändras. Framför allt Nefertiti, Playhouse och Billetto kan behöva justeras om de ändrar skydd eller sidstruktur. Därför är `status.json` en viktig del av lösningen.
