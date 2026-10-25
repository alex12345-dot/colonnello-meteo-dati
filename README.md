# Dati di Colonnello Meteo

Livelli meteo pronti da disegnare (PNG + `manifest.json`) ricavati da **ECMWF open data**
(modello IFS 0,25°) e allerte meteo-idro del **Dipartimento della Protezione Civile**,
aggiornati piu' volte al giorno da GitHub Actions.

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
