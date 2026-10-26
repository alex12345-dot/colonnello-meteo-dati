GRIB2 sintetici 2x2 (simple packing 5.0, 16 bit, senza ecCodes).
Nessun dato reale o dipendenza aggiuntiva. Totale: 2663 byte.

- `2t`: 293.15 K ovunque; `2d`: 283.15, 293.15, 303.15, 273.15 K.
- `10u`, `10v`: componenti del vento; `10fg`: 0, 10, 20, 30 m/s,
  prodotto 4.8 con massimo nelle ultime 3 h.
- `msl`: codice ECMWF 0.3.0, superficie 101; 80000, 80800, 81600, 82400 Pa.
- `tcc`: codice locale ECMWF 0.6.192, frazioni 0, 0.25, 0.5, 1.
- `tcc-percent`: codice WMO 0.6.1, percentuali 0, 25, 50, 100.
- `tcc-low-percent`: 0, 0, 0, 1%; verifica che la conversione non dipenda dal massimo.
- `mucape`: 0, 100, 500, 2000 J/kg, superficie 17.
- `sf`: 0, 0.001, 0.002, 0.01 m; `tp`: 0, 0.001, 0.01, 0.1 m.
- `swh`: 0, assente, 1, 4 m; bitmap 1011. Lo zero e' mare valido.
- `swh-aec-padding`: stessi valori e bitmap, packing AEC 5.42, blocchi da
  32 campioni con padding finale; riproduce l'errore del buffer a +27 h.

