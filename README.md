# Dati di Colonnello Meteo

Livelli meteo pronti da disegnare (PNG + `manifest.json`) ricavati da **ECMWF open data**
(modello IFS 0,25°) e allerte meteo-idro del **Dipartimento della Protezione Civile**,
aggiornati piu' volte al giorno da GitHub Actions.

La chiave aggiuntiva `manifest.precip_eu` espone la pioggia oraria **ICON-EU DWD**
a 0,0625 gradi (~7 km), indipendente dai livelli ECMWF. Fonte:
[DWD Open Data](https://opendata.dwd.de/weather/nwp/icon-eu/grib/), attribuzione
`Deutscher Wetterdienst (DWD), ICON-EU`, licenza `CC-BY-4.0`.
Il blocco contiene esattamente `model`, `run`, `attribution`, `license`, `grid`,
`encoding`, `transform`, `units`, `interval_hours`, `steps`.
`model` vale `icon-eu`, `encoding` vale `grayscale8`, `transform` vale `sqrt`,
`units` vale `mm/h` e `interval_hours` vale 1. La griglia 1377x657 parte da
`lat0: 70.5`, `lon0: -23.5`, con `dlat` e `dlon` pari a 0.0625 e
`scanning_mode: 0`: righe nord-sud e colonne ovest-est.

I 72 passi contigui da +1 a +72 h contengono solo `valid_time` (UTC con `Z`),
`url: "precip_eu/NNN.png"`, `min`, `max`, `quantization_step`.
Ogni pixel si ricostruisce in mm/h con `(R * quantization_step) ** 2`;
`max` e `quantization_step` sono nella scala sqrt(mm/h), `min` vale 0.
Si calcola `max(0, TOT_PREC[k] - TOT_PREC[k-1])` in float64 sui dati DWD in
kg/m2 (= mm), con cumulata iniziale zero per definizione e nessun PNG al passo 0.
Le ore si abbinano a ECMWF per `valid_time`, mai per indice.

La CLI include sempre ICON-EU dopo ECMWF; nell'API Python si passa
`dwd_source=DWDSource()` a `build`. Si sonda con HEAD il passo 072 della corsa
00/06/12/18 UTC piu' recente, fino a quattro corse precedenti; se HEAD non e'
supportato si legge un solo byte con GET. Timeout 30 s, quattro tentativi con
backoff, budget di richiesta 240 s e budget totale ICON-EU 900 s.
Il workflow ricrea `out/` recuperando solo i JSON: ICON-EU viene quindi
ricostruito sempre. Un passo mancante, un dato non finito o qualsiasi altro
errore omette l'intera chiave `precip_eu`, con `::warning::` nel log e voce in
`warnings`; ECMWF, allerte e vigilanza proseguono. La decisione di pubblicazione
e la verifica Pages considerano anche `precip_eu`, comprese comparsa e scomparsa.

Il workflow pubblica i seguenti livelli, nell'ordine di `manifest.layers`, ogni 3 h
da 0 a 72 h. Le unita' sono quelle dei valori nel file, prima delle conversioni
di visualizzazione dell'app.

| Livello | Parametro ECMWF | Unita' | Significato |
| --- | --- | --- | --- |
| `wind10m` | `10u`, `10v` | `m/s` | Componenti del vento a 10 m |
| `t2m` | `2t` | `K` | Temperatura a 2 m |
| `precip` | `tp` | `m` | Precipitazione cumulata dall'inizio corsa |
| `gust` | `10fg` | `m/s` | Raffica massima a 10 m nel periodo del GRIB |
| `msl` | `msl` | `Pa` | Pressione al livello del mare |
| `tcc` | `tcc` | `0-1` | Copertura nuvolosa totale |
| `rh2m` | `2t`, `2d` | `%` | Umidita' relativa a 2 m, limitata a 0-100 |
| `cape` | `mucape` | `J/kg` | Energia convettiva della particella piu' instabile |
| `snow` | `sf` | `m` | Neve cumulata dall'inizio corsa, equivalente in acqua |
| `swh` | `swh`, stream `wave` | `m` | Altezza significativa delle onde |
| `precip_rate` | Differenza di `tp` prima della quantizzazione | `mm/h` | Intensita' media nell'intervallo che termina al passo |

I livelli scalari usano `{url, encoding: "grayscale8", min, max,
quantization_step, units}`. Per gli scalari lineari il valore ricostruito e' `min + R * quantization_step`.
`wind10m` conserva il formato RGBA esistente: componenti nei canali R/G,
maschera in A, minimi/massimi/passi di quantizzazione a due elementi.
`swh` usa `{url, encoding: "rgba8", channels: {value: "R", mask: "A"},
min, max, quantization_step, units}`: A=0 per dato assente (terra), A=255 per
dato valido, incluso un valore di 0 m. Le onde usano la stessa corsa e griglia
IFS 0.25 degli altri livelli; una griglia diversa causa errore.

`precip_rate` usa le cumulate originali in float64:
`max(0, tp[step] - tp[precedente]) * 1000 / interval_hours`, senza download aggiuntivi.
`precip` e la sua codifica restano invariati.
La voce del nuovo livello contiene `url: "precip_rate/NNN.png"`, `encoding: "grayscale8"`,
`transform: "sqrt"`, `min: 0.0`, `max` (radice del massimo del passo),
`quantization_step: max / 255`, `units: "mm/h"` e `interval_hours` intero.
Il valore in mm/h si ricostruisce con `(min + R * quantization_step) ** 2`;
i pixel non finiti sono byte 0. Gli zeri restano esatti.
`interval_hours` e' la differenza fra i passi effettivamente elaborati:
nella sequenza IFS completa vale 3 h fino a +144, poi 6 h;
selezioni discontinue usano l'intervallo fra i passi selezionati.
Il passo 0 non ha la chiave `precip_rate`. Senza la cumulata del passo precedente
elaborato, incluso il primo passo di una selezione che non parte da 0,
il livello resta assente e `warnings` riporta `missing_layers: ["precip_rate"]`.
Assenza del livello non significa pioggia zero.

`tcc` viene normalizzata in base alle unita' GRIB: percentuali WMO divise per
100, frazioni ECMWF conservate. `rh2m` usa Magnus sull'acqua con coefficienti
17.625 e 243.04 °C, applicati a temperatura e rugiada convertite da K a °C;
20 °C e 10 °C producono 52.5413%. `gust` puo' mancare al passo 0: il livello
resta assente in quel passo e `warnings` riporta `missing_parameters: ["10fg"]`.
Il campo aggiuntivo `period_hours`, quando disponibile, dichiara il periodo
effettivo del massimo: nella corsa verificata e' 1 h, distinto dal passo di
pubblicazione di 3 h. Non viene costruito un massimo su periodi diversi.

Esecuzione: `python pipeline/build.py --model ifs --run latest --steps 0-72
--layers wind10m,t2m,precip,gust,msl,tcc,rh2m,cape,snow,swh,precip_rate --out out/`.
`swh` richiede IFS; lo stream `wave` non viene richiesto ad AIFS.

Origine e licenza dei dati: *ECMWF Open Data © European Centre for Medium-Range Weather
Forecasts (ECMWF), licensed under CC BY 4.0*. Questo repository ridistribuisce un'elaborazione
di quei dati alle stesse condizioni.

Le previsioni ECMWF elaborate qui non sono previsioni ufficiali. Le allerte sono una
riduzione del [Bollettino di criticita' nazionale DPC](https://github.com/pcm-dpc/DPC-Bollettini-Criticita-Idrogeologica-Idraulica),
licenza **CC BY 4.0**. Per il bollettino originale e gli avvisi locali consultare DPC e
Protezione Civile regionale.

La voce `allerte` del manifest contiene `url` (`allerte.json`), `zone_url`
(`zone-allerta.json`), `bollettino` (`AAAAMMGG_HHMM`), `emesso` (ISO con offset
Europe/Rome), `impronta_zone` (SHA-256 delle geometrie originali ordinate per nome) e
`stato` (`aggiornato` oppure `non aggiornato`).

`allerte.json` contiene attribuzione, licenza, data di oggi e domani e le zone abbinate
per nome. Ogni giorno espone `sintesi`, `idrogeologico`, `temporali`, `idraulico`:
**0** nessuna allerta, **1** gialla, **2** arancione, **3** rossa. Livelli sconosciuti o
zone non abbinate fanno fallire l'aggiornamento DPC, senza convertirli in assenza di
allerta. Ogni zona include anche `regione`: maggioranza dei Comuni riconosciuti nel
CSV ISTAT locale `pipeline/data/comuni_regioni.csv` (CC BY 4.0). Il confronto ignora
maiuscole e accenti, uniforma apostrofi e spazi e accetta entrambe le parti dei nomi
bilingui separati da `/`. Gli omonimi fra regioni diverse non sono riconosciuti;
i duplicati nella stessa zona contano una volta. Con meno del 50% di Comuni
riconosciuti, oppure senza maggioranza regionale, `regione` vale `null` e il log
riporta il nome della zona e la percentuale riconosciuta. L'elenco dei Comuni non
viene ridistribuito in questo file.

`zone-allerta.json` e' una FeatureCollection con la sola proprieta' `nome`, semplificata
con Douglas-Peucker e coordinate a quattro decimali, al massimo 700.000 byte.
Gli anelli che collassano sulla griglia vengono quantizzati conservativamente entro
una cella per mantenerli non degeneri. Non e' una cartografia per delimitazioni precise.
Il file viene rigenerato solo quando cambia l'impronta delle geometrie (o manca il file).

La pipeline sceglie la coppia today/tomorrow completa piu' recente, scoperta tramite
alberi GitHub non ricorsivi, e scarica entrambi i file dal medesimo commit DPC, con
timeout e retry limitati. Il workflow usa il `GITHUB_TOKEN` esistente solo per l'API.
Se DPC fallisce, ECMWF prosegue: la voce precedente e i relativi file vengono
conservati con `stato: "non aggiornato"` e un warning nel log e nel manifest. Al primo
avvio senza copia precedente, la voce `allerte` resta assente e viene emesso il warning.
Si pubblicano anche aggiornamenti DPC quando la corsa ECMWF non cambia.

Il [Bollettino di vigilanza meteorologica nazionale DPC](https://github.com/pcm-dpc/DPC-Bollettini-Vigilanza-Meteorologica),
anch'esso **CC BY 4.0**, alimenta `vigilanza.json`. La voce `vigilanza` del manifest
contiene `url` (`vigilanza.json`), `data` (`AAAA-MM-GG`) e `stato` (`aggiornato`
oppure `non aggiornato`). Il prodotto contiene `fonte`, `licenza`, `url_fonte`,
`data`, `titolo` e `giorni`; ogni giorno espone `giorno` (`oggi`, `domani`,
`dopodomani`), `data`, `sezioni` (`titolo`, `testo`) e `fenomeni` (`tipo`, `lat`, `lon`).

La vigilanza sceglie il bollettino della data corrente **Europe/Rome**, oppure il
piu' recente pubblicato entro quella data. Indice, punti TopoJSON e stile opzionale
`*_fenomeni_style.json` provengono dal medesimo commit. L'HTML viene convertito in
testo con entita' decodificate e spazi normalizzati, separando le sezioni sui titoli
in grassetto. Le coordinate provengono dalle proprieta' dei punti e sono arrotondate
a quattro decimali; i poligoni di precipitazione non vengono scambiati per fenomeni.
I nomi dei fenomeni vengono letti dalle etichette della legenda, quando disponibili;
stili che contengono soltanto URL di icone e identificativi senza etichetta producono
`fenomeno <id>`. Un errore HTTP o di schema conserva l'ultimo `vigilanza.json`
valido, con `stato: "non aggiornato"`, warning nel log e nel manifest; allerte ed
ECMWF proseguono. Al primo errore senza copia precedente la voce resta assente.

Per conservare il prodotto tra esecuzioni occorre mantenere nella directory di output
sia il manifest sia `vigilanza.json`. Il workflow attuale ripristina solo i file delle
allerte e confronta soltanto corsa ECMWF e voce `allerte`: il ripristino della vigilanza
e la pubblicazione dei suoi soli aggiornamenti richiedono un intervento distinto
sul workflow.

Verifica offline: `python -m pytest pipeline/tests -q`. I test vietano la rete e usano
le fixture DPC del 25/10/2026 e 07/10/2026; per domani e dopodomani i punti mancanti
nelle fixture sono simulati esplicitamente con collezioni vuote.

Pubblicati su GitHub Pages: <https://alex12345-dot.github.io/colonnello-meteo-dati/>
