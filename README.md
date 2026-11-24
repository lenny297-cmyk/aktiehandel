# AI-aktieexperimentet

> **Pausat.** Schemat i `.github/workflows/trade.yml` är avstängt och inga
> rundor körs. Experimentet har inte startat, så inget startdatum är fryst.
> Hur det återupptas står överst i workflow-filen.

Claude förvaltar **100 000 kr på papper** i OMXS30-bolag och försöker slå index.
Tre körningar per vardag, all bokföring i JSON i det här repot, en statisk sida
i `/docs`.

> Simulerat experiment. **Inga riktiga order läggs** och inget riktigt kapital
> förvaltas. Repot har ingen koppling till mäklare eller bank. Detta är inte
> finansiell rådgivning.

Reglerna som gäller, och varför de ser ut som de gör, står i [CLAUDE.md](CLAUDE.md).

## Hur det hänger ihop

Modellen lämnar **förslag**. Koden avgör. Inget AI-svar skriver någonsin direkt
till portföljen — varje förslag prövas mot kassa, positionstak, universum och
nivåer innan det blir en affär, och det som inte håller loggas med skäl och
ignoreras.

Varje runda, i ordning:

1. avgör om körningen hör till en giltig slot (svensk lokaltid, inte helg eller helgdag)
2. hämtar kurser och index via yfinance, med upp till fyra försök och växande
   väntetid emellan — går alla fel hoppas rundan över
3. rättar gårdagens värde mot officiella stängningskurser
4. kontrollerar stopp och mål på öppna positioner
5. värderar portföljen
6. frågar Claude (`claude-sonnet-5-5`) och får tillbaka strikt JSON
7. validerar förslagen
8. bokför de godkända med courtage och spread
9. skriver state och publicerar till `/docs`

Steg 6 och 7 får falla bort utan att rundan går förlorad: stoppkontrollen och
dagens värde sparas ändå. Sviker marknadsdatan avbryts allt — då finns det
ingenting att vara säker på, och en hoppad runda är bättre än en gissad kurs.

## Komma igång

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
pip install -r requirements.txt
```

Kör testsviten:

```bash
PYTHONPATH=src python -m pytest tests -q
```

Kör en runda utan att skriva någonting:

```bash
PYTHONPATH=src python -m aiaktier.run --dry-run --slot close
```

Dry run går igenom hela flödet — data, stopp, beslut, validering, bokföring —
och skriver ut vad som *skulle* ha hänt. Ingenting rörs på disk.

### Flaggor

| Flagga | Betydelse |
| --- | --- |
| `--dry-run` | kör allt, skriv ingenting |
| `--slot morning\|midday\|close` | kör en namngiven slot oavsett klockslag |
| `--force` | kör även om sloten redan körts i dag |
| `--now <ISO>` | låtsas att klockan är något annat, för felsökning |
| `-v` | mer loggning |

## Sätta upp på GitHub

1. **Secret:** lägg `ANTHROPIC_API_KEY` under Settings → Secrets and variables → Actions.
2. **Pages:** Settings → Pages → Source: *Deploy from a branch*, branch `main`, mapp `/docs`.
3. **Actions:** behöver skrivrättigheter. Workflowen sätter `permissions: contents: write`
   själv, men kontrollera att Settings → Actions → *Workflow permissions* inte är
   låst till read-only.
4. Kör workflowen `Handelsrunda` manuellt en första gång, gärna med `dry_run: true`.

Den första riktiga körningen fryser startdagen, startkapitalet och indexets
startkurs i `data/meta.json`. De rörs sedan aldrig av koden — annars går
jämförelsen mot index inte att lita på. Att starta om experimentet är ett
medvetet manuellt steg: radera `data/`.

## Schemaläggningen

GitHub Actions cron går i UTC och känner inte till sommartid. Varje slot ligger
därför som **två** cron-poster, en för vintertid och en för sommartid, och
skriptet avgör själv i `Europe/Stockholm` vilken som är den riktiga. Fel halva
av året avslutas körningen tyst.

Cron i Actions kan dessutom bli flera tiotals minuter försenad. Därför har varje
slot ett toleransfönster (`schedule.tolerance_minutes`), och `data/runs.json`
ser till att en slot aldrig körs två gånger samma dag.

Klockan 17:00 ligger före börsens stängning 17:30, så dagens sista mätning är
inte dagens stängningskurs. Nästa morgon räknas gårdagen om mot de officiella
kurserna, med de innehav som faktiskt fanns den dagen.

## Konfiguration

Allt som går att ställa in ligger i [`config.yaml`](config.yaml): courtage,
spread, positionstak, minsta affär, tidsfönster, börskalender och modellval.

Courtage har både procentsats och minimiavgift. Spreaden läggs symmetriskt kring
mid, så köp fylls över och sälj under. Båda räknas på varje affär.

## Universumet

`data/universe.json` innehåller OMXS30-bolagen med ett `as_of`-datum. **Listan är
daterad 2025-07-01**, eftersom Nasdaqs egen viktningssida kräver inloggning.
Indexet revideras halvårsvis i januari och juli, så sammansättningen kan ligga
en eller flera revideringar efter. Samtliga 30 tickers är verifierade mot Yahoo
Finance och ger kursdata.

Efter varje revidering: uppdatera listan och `as_of`, och kör

```bash
PYTHONPATH=src python scripts/check_universe.py
```

som kontrollerar att varje ticker fortfarande ger kurs.

## Filer

```
src/aiaktier/
  portfolio.py        bokföringen: köp, sälj, värdering. Ren logik, hårdast testad
  fees.py             courtage och spread
  validator.py        handelsreglerna. Allt modellen föreslår passerar här
  stops.py            stopp- och målkontroll
  trading_calendar.py tidsfönster, handelsdagar, dedupe
  market_data.py      yfinance bakom ett eget lager som felar tydligt
  decide.py           prompt, Claude-anrop, strikt JSON-svar
  benchmark.py        OMXS30 omräknat till startkapitalet
  state.py            atomär JSON, plus dry run-läget
  report.py           underlaget sidan läser
  run.py              orkestreringen
  config.py           config.yaml och sökvägar

data/                 portfölj, affärer, dagliga värden, beslutslogg, meta, runs
docs/                 den publicerade sidan; docs/data är en kopia av data/
tests/                200 tester, inget rör nätet eller API:et
scripts/              check_universe.py
```

## Vad som medvetet *inte* finns

- Riktiga order eller mäklar-API:er.
- Krav på modellen som koden inte kan verifiera.
- Interpolerade eller uppskattade kurser. Saknas data hoppas rundan över.
