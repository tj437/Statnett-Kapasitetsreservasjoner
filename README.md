# Statnett kapasitetsreservasjoner – daglig overvåking

Henter tabellene fra Statnetts Power BI-rapporter på
[Statistikk om tilknytningssaker](https://www.statnett.no/nettkapasitet-til-produksjon-og-forbruk/foresporsler-og-reservasjon-i-nettet/)
hver ukedag kl. 07 (norsk sommertid), lagrer dem som CSV i `data/`, og varsler ved endringer.

## Hvordan det virker

| Steg | Fil | Hva |
|---|---|---|
| 1 | `scripts/scrape.py` | Leser rapportdefinisjonen fra Power BI (publish-to-web), finner alle tabell-visualer, kjører spørringene og skriver `data/<rapport>/<side>__<tabell>.csv` |
| 2 | `scripts/diff.py` | Sammenligner med forrige commit. Nye/fjernede/endrede rader → `CHANGES.md` |
| 3 | `.github/workflows/scrape.yml` | Cron-kjøring, commit av snapshot, varsling |

Rapportene og tabellene (verifisert 22.09.2026):

| Rapport | Fil | Innhold |
|---|---|---|
| `reservasjoner` | `Liste_over_reservasjoner__Liste_Forbruk.csv` / `…_Produksjon.csv` | Saker med reservert kapasitet |
| `kapasitetsko` | `Liste_over_kapasitetskø__Liste_Forbruk.csv` / `…_Produksjon.csv` | Modne saker i kapasitetskø |
| `tilbaketrukket` | `Liste_over_saker_med_tilbaketrukket_kapasitet__…` | Reservasjoner som er trukket tilbake |
| `tilknyttet` | `Liste_over_saker_som_er_tilknyttet__…` | Saker som er tilknyttet |

Kolonner: Statnett saksnr., Tilko saksnr., Stasjon for tilknytning i transmisjonsnettet, Områdeplan, Prisområde, Statnetts kunde, Sluttkunde, Næringstype, Kapasitet (MW), datoer, Kunde og tilknytningsansvarlig. Datoer skrives som ÅÅÅÅ-MM-DD.

Sidens dato-slicere brukes ikke, så CSV-en er et komplett uttrekk (kan avvike marginalt fra totalen som vises på siden).

Historikken ligger i git: `git log -- data/` viser hvert snapshot, og `git diff <sha1> <sha2> -- data/` viser hva som endret seg mellom to datoer.

## Varsling

* **GitHub-issue (standard, ingen oppsett):** Ved endringer opprettes et issue med endringene i brødteksten. GitHub sender e-post til deg for hvert nytt issue (Settings → Notifications; du må «watche» repoet eller være eier). Lukk issuet når du har lest det.
* **E-post via SMTP (valgfritt):** Sett repo-variabelen `ALERT_EMAIL_TO` og secrets `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`. For Microsoft 365 er server `smtp.office365.com`, port `587` – forutsetter at SMTP AUTH er slått på for brukeren i tenanten. Alternativt bruk en Gmail-konto med app-passord.
* **Teams (valgfritt):** Opprett en *Incoming Webhook* på en Teams-kanal og legg URL-en i secret `TEAMS_WEBHOOK_URL`.
* **Feil i scraperen:** Hvis kjøringen feiler (typisk fordi Statnett har republisert rapporten), opprettes et issue «Scraper feilet».

## Filtrering

`config.yml` → `alert_filters` begrenser hvilke rader som utløser varsel, f.eks.

```yaml
alert_filters:
  område: ["NO2", "NO3"]     # kolonnenavn matches som delstreng ("Prisområde")
  type: ["Forbruk"]
```

All data lagres uansett ufiltrert i `data/`.

## Første gangs oppsett

1. Push dette repoet til GitHub.
2. Settings → Actions → General → *Workflow permissions*: velg **Read and write permissions**.
3. Actions → «Scrape Statnett tilknytningssaker» → *Run workflow* med `debug` = true.
4. Sjekk loggen: hver rapport skal skrive ut «table visuals found: N» og en linje per CSV med antall rader og kolonnenavn.
   Første kjøring gir «Ny tabell» for alt og oppretter *ikke* noe issue.
5. Hvis noe feiler: last ned artefakten `powerbi-debug` fra kjøringen – den inneholder rå forespørsler og svar fra Power BI.

## Når det slutter å virke

Power BI publish-to-web er ikke et offisielt API. Typiske brudd:

* **HTTP 404/401 på modelsAndExploration** – Statnett har republisert rapporten med ny nøkkel. Åpne Statnett-siden, kopier de nye «Fullskjermvisning»-lenkene inn i `config.yml`.
* **«no table visuals on the page»** – visualtypene har endret navn. Kjør med debug, søk etter `visualType` i `debug/<rapport>__modelsAndExploration.json` og legg typen til i `TABLE_VISUAL_TYPES` i `scrape.py`.
* **«Kolonnene er endret»** i varselet – Statnett har lagt til/fjernet kolonner. Ingen handling nødvendig; neste kjøring sammenligner mot ny struktur.
* **Advarsel om restart token (RT)** – tabellen er større enn `max_rows`; øk verdien i `config.yml`.

## Kjøre lokalt

```bash
pip install -r requirements.txt
python scripts/scrape.py --debug
python scripts/diff.py
```
