# AI-aktieexperiment (simulerat)

Claude förvaltar 100 000 kr **på papper** i svenska aktier och försöker slå OMXS30.
Inspirerat av aiaktier.se. **Inga riktiga order läggs någonsin.** Repot innehåller
ingen koppling till mäklare, bank eller orderläggning. Sidan och datan är ett
experiment, inte finansiell rådgivning.

## Grundprinciper

1. **AI-svaret är ett förslag, aldrig en bokföringspost.** Modellens svar får
   aldrig skriva direkt till portföljen. Varje förslag passerar en validerare
   innan det blir en affär. Ogiltiga förslag loggas och ignoreras.
2. **Gissa aldrig priser.** Saknas marknadsdata loggas felet och rundan hoppas
   över. Ingen interpolering, ingen uppskattning, inget senast-kända-pris från
   en annan dag som om det vore dagens.
3. **Ingen lookahead.** Beslut fattas på data fram till körningens tidpunkt.
   Fyllning sker till senast tillgängliga pris vid eller före den tidpunkten.
4. **All state är JSON i repot** och committas efter varje lyckad körning.
   Inga databaser, ingen extern lagring.
5. **Allt är reproducerbart.** Varje affär pekar på beslutet som orsakade den,
   och varje beslut sparar modellens motivering i klartext.

## Arkitektur

- **Språk:** Python 3.12.
- **Schemaläggning:** GitHub Actions, tre gånger per vardag — 09:15, 12:30 och
  17:00 svensk tid.
  - GitHub Actions cron går i UTC och har ingen tidszonshantering. Därför:
    cron-poster för **både sommar- och vintertid** (sex poster totalt), och
    skriptet avgör själv i `Europe/Stockholm` om körningen hör till ett giltigt
    tidsfönster. Fel halva av året → körningen avslutas tyst utan att göra något.
  - GitHub Actions kan försena cron-jobb flera tiotals minuter. Varje slot har
    därför ett toleransfönster, och varje slot körs **högst en gång per dag**
    (dedupliceras mot state).
  - Helger hoppas över. Svenska helgdagar och halvdagar hanteras via
    börskalender i konfigurationen, och utöver det: finns ingen färsk kurs för
    dagen körs ingenting.
- **`workflow_dispatch`** finns alltid, med val för slot och för dry run.
- **Marknadsdata:** `yfinance`. Svenska aktier har suffix `.ST`
  (t.ex. `ERIC-B.ST`), index OMXS30 är `^OMX`. All hämtning ligger bakom ett
  eget lager som kastar ett tydligt fel vid problem — aldrig tysta fallbacks.
- **Beslut:** Claude API, modell `claude-sonnet-5-5`. Nyckeln läses från
  miljövariabeln `ANTHROPIC_API_KEY` (GitHub Secret). Nyckeln får aldrig
  committas, loggas eller skrivas till någon JSON-fil.
- **Publicering:** statisk sida i `/docs` via GitHub Pages. Sidan läser JSON
  och innehåller ingen logik som räknar om bokföringen.

## Handelsregler som koden ska upprätthålla

Reglerna är kodens ansvar, inte modellens. Att prompten ber om dem räcker inte —
valideraren avgör.

- **Universum:** enbart bolagen i OMXS30. Ticker utanför universumet avvisas.
- **Svarsformat:** Claude svarar bara med JSON. Varje förslag har: `action`
  (`buy`/`sell`), `ticker`, `amount_sek`, `stop_loss`, `take_profit`,
  `rationale`. Fel format → hela svaret avvisas och loggas.
- **Validering av varje förslag:**
  - tickern finns i universumet och har färsk kurs
  - belopp > 0 och, vid köp, täcks av tillgänglig kassa inklusive courtage
  - positionen får efter affären utgöra **max 20 %** av portföljens totala värde
    (kassa + innehav)
  - `stop_loss` och `take_profit` finns båda och är rimliga i förhållande till
    priset (köp: `stop_loss` < pris < `take_profit`)
  - vid sälj: innehavet finns och antalet räcker
- **Stopp och mål kontrolleras vid varje körning**, före nya beslut. Utlösta
  nivåer stänger positionen och loggas med orsak (`stop_loss` / `take_profit`).
- **Courtage och spread** räknas på varje affär. Båda är konfigurerbara.
  Courtage har både procentsats och minimiavgift. Spread appliceras så att
  köp fylls över mid och sälj under mid.
- **Hela aktier.** Inga fraktioner.
- **Motiveringen sparas** för varje affär och visas på sidan.

## Jämförelse mot index

OMXS30 räknas om till 100 000 kr från experimentets startdag:
`index_värde(t) = 100000 × (OMX_stängning(t) / OMX_stängning(start))`.
Startdagen och startkursen fryses i `meta.json` vid första körningen och ändras
aldrig därefter.

## Kodregler

- Bokföringen (`portfolio`) är ren logik: in- och utdata, inga nätverksanrop,
  inga filskrivningar, ingen klocka. Det är den som enhetstestas.
- Pengar hanteras som avrundade öresbelopp vid presentation, men beräkningar
  sker i float med explicit avrundning vid bokföring — aldrig ackumulerade
  avrundningsfel.
- Varje JSON-skrivning är atomär (skriv till temp, byt namn) så ett avbrutet
  jobb aldrig lämnar halv state.
- `--dry-run` kör hela flödet — data, beslut, validering, bokföring — men
  skriver ingenting till disk och committar inget. Den skriver ut vad som
  *skulle* hänt.
- Loggning till stdout så att GitHub Actions-loggen blir läsbar i efterhand.

## Test

Enhetstester krävs för bokföringslogiken, minst:
köp, sälj, utlöst stopp, utlöst mål, courtage- och spreadberäkning, samt
otillräcklig kassa. Valideraren testas separat: brott mot 20 %-regeln, ticker
utanför universumet, saknat stopp eller mål, och trasigt AI-svar.

## Vad som inte hör hemma här

- Riktiga order eller mäklar-API:er.
- Att be modellen om något som koden inte kan verifiera.
- Att runda av kraven när datakällan krånglar — hellre en hoppad runda än en
  påhittad kurs.
