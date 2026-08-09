# Accounts Payable Dashboard — Corro / Cavali

Dashboard ejecutivo de cuentas por pagar para la CEO. Fuente principal: **Bill.com**
(con QuickBooks como referencia de categoría contable si hace falta más adelante).

Vista pública en inglés, modo claro/oscuro (☀️/🌙), pensado para leerse en 10 segundos:
cuánto se debe, qué tan urgente es, y en qué se concentra.

## Estructura del proyecto

```
ap-dashboard/
├── index.html                  <- el dashboard (esto es lo que se ve en GitHub Pages)
├── data/
│   ├── ap-data.json            <- datos que consume el dashboard (por ahora, datos de muestra)
│   └── vendor-map.json         <- reglas para clasificar vendors en categorías
├── scripts/
│   ├── bill_client.py          <- cliente de la API de Bill.com (scaffold, falta validar campos reales)
│   ├── transform.py            <- convierte facturas de Bill en ap-data.json
│   └── requirements.txt
└── .github/workflows/
    └── update-ap-data.yml      <- corre diario, jala Bill.com, actualiza datos y publica en Pages
```

## Cómo subirlo a GitHub (una sola vez)

1. Crea un repo nuevo, público o privado, ej. `ap-dashboard`.
2. Sube todo el contenido de esta carpeta a la rama `main`.
3. En el repo: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
   (No uses "Deploy from a branch"; el workflow ya incluido se encarga de publicar.)
4. En **Settings → Secrets and variables → Actions → New repository secret**, agrega:

   | Nombre | Valor |
   |---|---|
   | `BILL_API_KEY` | Dev/App key de la cuenta de Bill.com |
   | `BILL_USERNAME` | Usuario de Bill.com |
   | `BILL_PASSWORD` | Password del usuario de Bill.com |
   | `BILL_ORG_ID` | orgId de la organización en Bill.com |

   Esto es exactamente lo que pediste: las credenciales viven solo en
   **Settings → Secrets and variables → Actions**, nunca en el código ni en el repo.

5. Corre el workflow manualmente la primera vez: pestaña **Actions → Update AP Dashboard Data → Run workflow**.
   Si todo sale bien, en unos minutos el dashboard queda publicado en
   `https://<tu-usuario>.github.io/ap-dashboard/`.

## Qué falta confirmar antes de conectar datos reales

`bill_client.py` y `transform.py` están armados como punto de partida funcional,
pero **necesito validar contra la cuenta real de Bill.com** antes de que jalen datos de verdad:

- Si la cuenta usa la API v3 (BDC, la que asumí) o todavía v2 "Classic" — cambia el cliente.
- Nombres exactos de campos que devuelve `bill/list` (`vendorName`, `amountDue`, `dueDate`, etc. — los puse con los nombres más comunes de la documentación pública, pero hay que confirmarlos).
- Si la categoría contable (Inventory / Advertising / G&A, etc.) conviene traerla desde
  Bill.com directamente o cruzarla con QuickBooks (tú mencionaste que QB ya está conectado con Bill).
- Cómo tratar facturas parcialmente pagadas, vendor credits, y facturas sin `dueDate`.
- Frecuencia real de actualización deseada (el workflow está en diario, se ajusta en un minuto).

En cuanto me pases acceso de prueba (o un export de muestra) a Bill.com, conecto
`transform.py` con datos reales y quito los datos de muestra de `ap-data.json`.

## Cómo clasifica vendors hoy

`data/vendor-map.json` tiene reglas por palabra clave (ej. "google" → Advertising,
"shipstation" → Shipping & Fulfillment). Cualquier vendor que no matchee cae en
**Unclassified**, y el dashboard muestra un aviso visible cuando hay monto ahí,
para que sea fácil detectar vendors nuevos y agregarlos al mapeo.

## Categorías y rangos de aging (según lo que pidió Ceci)

- **Categorías:** Inventory, Shipping & Fulfillment, Advertising, Sales & Marketing, G&A / OPEX, Unclassified.
- **Aging:** Not yet due · Due this month · Overdue < 3 months · Overdue > 3 months.
  (Se dejó esta estructura ejecutiva de 4 rangos en vez del aging estándar de 30/60/90/90+,
  tal como sugirió Ceci para no sobrecargar la vista.)

## Campos que agregué sobre la solicitud original

Para que el reporte realmente sirva como herramienta de decisión y no solo como
un volcado de Bill, agregué:

- **Hero number + "ledger beam"**: el total y su composición por urgencia, de un vistazo,
  antes de bajar a cualquier tabla.
- **Tendencia mensual de AP** (6 meses): para ver si la deuda total sube o baja mes a mes,
  no solo la foto de hoy.
- **Filtros combinables** (categoría + status + búsqueda de vendor/factura) en vez de solo
  clic en categoría, para que la CEO pueda buscar un vendor puntual sin salir del dashboard.
- **Aviso automático de "Unclassified"**: si hay plata sin categorizar, se ve de inmediato
  en vez de quedar escondida en la tabla.
- **Barra de aging mini por categoría** dentro de la tabla resumen, para no tener que
  abrir el drill-down solo para ver si una categoría está mayormente vencida o no.

## Próximos pasos sugeridos

1. Confirmar credenciales y estructura real de la API de Bill.com.
2. Validar con Ceci los nombres finales de categoría y si abrir G&A / OPEX en
   Payroll / Consulting / Software / Rent más adelante.
3. Decidir si el histórico mensual se calcula acumulando corridas del workflow
   (cada corrida guarda un snapshot) o si se trae directo de Bill/QuickBooks.
4. Evaluar permisos del repo (privado, con acceso solo para el equipo de finanzas y la CEO).
