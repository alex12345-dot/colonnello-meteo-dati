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
allerta. L'elenco dei Comuni non viene ridistribuito in questo file.

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

Verifica offline: `python -m pytest pipeline/tests -q`.

Pubblicati su GitHub Pages: <https://alex12345-dot.github.io/colonnello-meteo-dati/>
