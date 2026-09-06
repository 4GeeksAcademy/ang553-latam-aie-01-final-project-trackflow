# TrackFlow — Guía de desarrollo y QA local en GitHub Codespaces

## Objetivo

Esta guía documenta el procedimiento comprobado para levantar correctamente
TrackFlow dentro de GitHub Codespaces y realizar pruebas del Backoffice contra
el backend FastAPI.

Usar esta guía especialmente después de:

- reiniciar un Codespace;
- recrear un Codespace;
- perder dependencias Python;
- cambiar de Codespace;
- comenzar QA manual;
- encontrar errores de CORS o forwarding;
- encontrar que frontend y backend funcionan por separado pero no se comunican.

> Esta guía describe un entorno de desarrollo/QA.
> No sustituye la configuración de producción de TrackFlow.

---

# 1. Arquitectura relevante

TrackFlow utiliza:

```text
Browser
   │
   ▼
Next.js Backoffice
uis/backoffice
Port 3000
   │
   │ HTTP / Bearer JWT
   ▼
FastAPI
services/api
Port 8000
   │
   ├── TinyDB
   │     Auth
   │
   └── SQLModel
         Inventory
```

Para QA local de Inventory se puede utilizar SQLite temporal sin modificar
la configuración productiva del proyecto.

---

# 2. Regla principal de diagnóstico

Depurar siempre en este orden:

```text
1. Python / dependencias
        ↓
2. Variables obligatorias del backend
        ↓
3. Base de datos
        ↓
4. Backend arranca
        ↓
5. /health responde
        ↓
6. Puerto 8000 accesible
        ↓
7. URL forwarded correcta
        ↓
8. CORS
        ↓
9. Frontend arranca
        ↓
10. Browser → Backend
        ↓
11. Login / JWT
        ↓
12. Feature concreta
```

No empezar depurando JWT, Inventory, React o formularios si todavía no se ha
demostrado que la request llega al backend.

---

# 3. Posicionarse en el repositorio

```bash
cd /workspaces/ang553-latam-aie-01-final-project-trackflow
```

Confirmar:

```bash
git branch --show-current
git status --short
```

No continuar si existen cambios inesperados.

---

# 4. Recuperar dependencias Python después de reiniciar Codespace

Comprobar Python:

```bash
which python
python --version
```

Si aparece:

```text
No module named uvicorn
```

instalar las dependencias runtime declaradas en `pyproject.toml`:

```bash
python -m pip install -e .
```

Verificar:

```bash
python -m pip show uvicorn
```

> Usar `python -m uvicorn` en vez de depender de que el ejecutable `uvicorn`
> esté incluido en `$PATH`.

---

# 5. Dependencias de desarrollo / tests

Para ejecutar pytest:

```bash
python -m pip install --group dev
```

Verificar:

```bash
python -m pytest --version
```

---

# 6. JWT_SECRET_KEY obligatorio

El backend TrackFlow no arranca sin:

```text
JWT_SECRET_KEY
```

Para QA temporal:

```bash
export JWT_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

Comprobar que existe sin imprimir el secreto:

```bash
echo ${#JWT_SECRET_KEY}
```

Debe ser mayor que `0`.

> Esta variable existe solo en esa terminal.
> Después de reiniciar terminal/Codespace puede ser necesario volver a exportarla.

Nunca guardar esta clave temporal en Git.

---

# 7. DATABASE_URL obligatorio

Inventory utiliza SQLModel.

El startup de FastAPI ejecuta:

```text
create_db_and_tables()
```

por lo que el backend necesita `DATABASE_URL`.

Para QA local temporal se ha comprobado que puede utilizarse SQLite:

```bash
export DATABASE_URL="sqlite:////tmp/trackflow_inventory_qa.db"
```

Verificar:

```bash
echo "$DATABASE_URL"
```

Esperado:

```text
sqlite:////tmp/trackflow_inventory_qa.db
```

## Importante

Esta SQLite es solo para QA/desarrollo.

Producción continúa diseñada para PostgreSQL/Supabase.

No modificar el código backend para convertir SQLite en configuración productiva.

---

# 8. Levantar backend

Desde la raíz:

```bash
python -m uvicorn services.api.main:app --host 0.0.0.0 --port 8000
```

Esperado:

```text
Application startup complete.
Uvicorn running on http://0.0.0.0:8000
```

Dejar esta terminal abierta.

---

# 9. Comprobar backend internamente

Desde otra terminal:

```bash
curl http://127.0.0.1:8000/health
```

Esperado:

```json
{"status":"ok"}
```

También puede abrirse la URL forwarded:

```text
https://<CODESPACE_NAME>-8000.<FORWARDING_DOMAIN>/health
```

y debe responder:

```json
{"status":"ok"}
```

---

# 10. GitHub Codespaces — Ports

En VS Code / Codespaces abrir:

```text
PORTS
```

Configuración comprobada para este workflow:

```text
Frontend  3000 → Private
Backend   8000 → Public
```

## Por qué

El frontend privado puede abrirse en el navegador porque el usuario está
autenticado en Codespaces.

El backend se deja temporalmente Public durante QA para evitar que requests
cross-origin de `fetch()` sean interceptadas por la autenticación del tunnel
antes de llegar a FastAPI.

Al terminar QA:

```text
8000 → volver a Private
```

No dejar el backend público innecesariamente.

---

# 11. Nunca copiar manualmente el nombre del Codespace

El nombre cambia entre Codespaces.

Comprobar siempre:

```bash
echo "$CODESPACE_NAME"
echo "$GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN"
```

Generar URLs:

```bash
echo "FRONTEND=https://${CODESPACE_NAME}-3000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
echo "BACKEND=https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
```

Ejemplo conceptual:

```text
FRONTEND=https://<codespace>-3000.app.github.dev
BACKEND=https://<codespace>-8000.app.github.dev
```

## Lección importante

NO copiar URLs de un Codespace anterior.

Una sola letra incorrecta en `CODESPACE_NAME` puede provocar:

```text
CORS error
OPTIONS 404
request no llega al backend
```

aunque frontend y backend estén funcionando.

---

# 12. Configurar variables del Backoffice

El archivo debe estar aquí:

```text
uis/backoffice/.env.local
```

NO en la raíz del monorepo.

Crear dinámicamente:

```bash
cat > uis/backoffice/.env.local <<EOF
NEXT_PUBLIC_API_URL=https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}
NEXT_PUBLIC_INVENTORY_API_URL=https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}
EOF
```

Verificar:

```bash
cat uis/backoffice/.env.local
```

Confirmar que Git lo ignora:

```bash
git check-ignore -v uis/backoffice/.env.local
```

Esperado:

```text
uis/backoffice/.gitignore ... .env*
```

Después:

```bash
git status --short
```

`.env.local` NO debe aparecer.

---

# 13. Error común — crear `.env.local` en la raíz

Incorrecto:

```text
/.env.local
```

Correcto:

```text
/uis/backoffice/.env.local
```

Si accidentalmente se creó en raíz:

```bash
rm .env.local
```

y volver a crear el archivo dentro de `uis/backoffice`.

Nunca hacer commit del `.env.local`.

---

# 14. Reiniciar Next.js después de cambiar NEXT_PUBLIC_*

Las variables:

```text
NEXT_PUBLIC_API_URL
NEXT_PUBLIC_INVENTORY_API_URL
```

se cargan al iniciar Next.js.

Después de modificarlas:

```text
Ctrl+C
```

y volver a ejecutar:

```bash
cd uis/backoffice
npm run dev -- -H 0.0.0.0 -p 3000
```

No asumir que un frontend ya levantado leerá automáticamente las nuevas URLs.

---

# 15. Levantar frontend

```bash
cd /workspaces/ang553-latam-aie-01-final-project-trackflow/uis/backoffice

npm run dev -- -H 0.0.0.0 -p 3000
```

Esperado:

```text
Local:   http://localhost:3000
Network: http://0.0.0.0:3000
Ready
```

---

# 16. Verificar CORS antes de probar features

Abrir DevTools:

```text
Network
```

Entrar a una página que consulte backend.

Comprobar que la Request URL apunta a:

```text
https://<CODESPACE_NAME>-8000.app.github.dev/...
```

y NO a:

```text
https://<CODESPACE_NAME>-3000.app.github.dev/...
```

## Preflight correcto

Puede aparecer:

```text
OPTIONS /inventory/products
Status 200
```

Esto es normal.

## Preflight incorrecto

Si aparece:

```text
OPTIONS /inventory/products
404
```

junto con:

```text
No 'Access-Control-Allow-Origin' header
```

comprobar primero:

```bash
echo "$CODESPACE_NAME"
cat uis/backoffice/.env.local
```

La causa comprobada anteriormente fue una forwarded URL perteneciente a otro
Codespace.

---

# 17. CORS de TrackFlow

El backend construye automáticamente el origin de Codespaces utilizando:

```text
CODESPACE_NAME
GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN
```

y permite conceptualmente:

```text
http://localhost:3000
http://127.0.0.1:3000
https://<CODESPACE_NAME>-3000.app.github.dev
```

Por eso es importante que el frontend real corresponda al mismo Codespace
del backend.

---

# 18. Crear datos de Inventory para QA

Con el backend apuntando a:

```text
sqlite:////tmp/trackflow_inventory_qa.db
```

desde otra terminal ejecutar:

```bash
DATABASE_URL="sqlite:////tmp/trackflow_inventory_qa.db" \
python scripts/seed_inventory.py
```

El script es idempotente.

Dataset esperado:

```text
6 SKU
4 StockEntry
3 StockExit
```

Stocks iniciales esperados:

```text
CLT-SNK-W-42      @ LA  → 30
CLT-SNK-W-42-Z    @ ZGZ → 27
TEC-EAR-001       @ LA  → 10
CSM-SRM-030       @ ZGZ → 0
CLT-CHN-N-32      @ LA  → 0
TEC-CHG-065       @ ZGZ → 0
```

---

# 19. Auth / usuario QA

TinyDB Auth vive en:

```text
services/api/data/auth.json
```

Después de recrear/reiniciar el entorno puede estar vacío.

Comprobar:

```bash
ls -la services/api/data/
```

Si no existe usuario de prueba disponible:

1. abrir `/register`;
2. crear un usuario temporal de QA;
3. usar una contraseña exclusiva de pruebas;
4. iniciar sesión;
5. no reutilizar credenciales personales.

Flujo esperado:

```text
Register
   ↓
Login
   ↓
POST /auth/login
   ↓
JWT
   ↓
GET /auth/me
   ↓
Dashboard
```

---

# 20. Verificar protección de rutas

Sin sesión:

abrir directamente:

```text
/backoffice/inventory/products
/backoffice/inventory/orders
/backoffice/inventory/orders/inbound
/backoffice/inventory/orders/outbound
```

Esperado:

```text
AuthGuard
   ↓
/login
```

## Importante al probar manualmente

Usar:

```text
Ctrl+L
```

y reemplazar la URL COMPLETA.

No concatenar accidentalmente:

```text
/login/backoffice/inventory/orders
```

porque esa ruta naturalmente devuelve 404.

---

# 21. QA mínimo de Inventory

## Products

Abrir:

```text
/backoffice/inventory/products
```

Verificar:

- 6 SKUs;
- stock real;
- warehouse;
- Healthy / Low / Critical;
- Register inbound;
- Register outbound.

Seed esperado:

```text
30 → Healthy
27 → Healthy
10 → Low
0  → Critical
```

---

# 22. QA Inbound

Desde Products:

```text
Register inbound
```

Debe abrir:

```text
/backoffice/inventory/orders/inbound?sku_id=<id>
```

Verificar:

- SKU preseleccionado;
- warehouse derivado;
- Quantity;
- Reference.

Ejemplo de QA:

```text
Quantity: 5
Reference: QA-INBOUND-001
```

Después del POST:

- success visible;
- quantity limpia;
- reference limpia;
- SKU permanece seleccionado.

Volver a Products.

Ejemplo:

```text
stock 30
+ inbound 5
= backend muestra 35
```

No calcularlo manualmente en frontend.

---

# 23. QA Outbound — validación client-side

Desde Products:

```text
Register outbound
```

Verificar:

```text
Available stock
```

antes de enviar.

Si stock = 35:

```text
Quantity: 36
Dispatch
Tracking: QA-TRACK-001
```

esperado:

```text
Quantity cannot exceed available stock (35).
```

NO debe ejecutarse POST.

---

# 24. QA Outbound — Dispatch válido

Ejemplo:

```text
Available stock: 35
Quantity: 5
Exit type: Dispatch
Tracking: QA-TRACK-001
```

Esperado:

```text
success
stock 35 → 30
quantity limpia
tracking limpia
SKU permanece
Dispatch permanece
```

El stock debe refrescarse desde backend mediante API.

NO calcular:

```text
current_stock - quantity
```

manualmente en frontend.

---

# 25. QA Outbound — HTTP 400 real por concurrencia

Este es un caso crítico.

Objetivo:

probar que frontend puede tener stock obsoleto y backend sigue siendo la autoridad.

Procedimiento:

1. abrir Outbound en pestaña A;
2. observar stock, por ejemplo `30`;
3. sin recargar A, abrir pestaña B;
4. registrar otra salida válida que reduzca stock real;
5. volver a pestaña A, que todavía muestra stock anterior;
6. solicitar una cantidad válida según A pero superior al stock real.

Ejemplo:

```text
Pestaña A cree: 30
Pestaña B reduce backend a: 5
Pestaña A solicita: 10
```

Esperado:

```text
HTTP 400
```

con mensaje legible:

```text
Insufficient stock for SKU '...'. Available: 5, requested: 10.
```

Además:

```text
Available stock
```

debe refrescarse con el valor real del backend.

La salida fallida NO debe persistirse.

---

# 26. QA Outbound — Loss

Seleccionar SKU con stock disponible.

Ejemplo:

```text
Quantity: 2
Exit type: Loss
```

Verificar:

- Tracking number no aparece/no aplica;
- POST exitoso;
- tracking_number enviado como null;
- stock actualizado;
- Quantity limpia;
- Loss permanece seleccionado.

---

# 27. QA Inventory History

Abrir:

```text
/backoffice/inventory/orders
```

Verificar:

- Inbound;
- Outbound;
- Dispatch;
- Loss;
- Product name;
- SKU;
- Quantity;
- Warehouse;
- Reference;
- Tracking number;
- Created At;
- user_uuid;
- read-only.

Movimientos QA deben aparecer junto con los movimientos seed.

---

# 28. React duplicate keys — historial

Inventory History combina:

```text
StockEntry
StockExit
```

Ambas tablas tienen IDs autoincrementales independientes.

Por eso NO usar:

```tsx
key={order.id}
```

porque pueden existir:

```text
inbound id=2
outbound id=2
```

La key correcta utilizada es:

```tsx
key={`${order.movement_type}-${order.id}`}
```

Si aparece:

```text
Encountered two children with the same key
```

revisar este punto.

---

# 29. Tests backend de Inventory

Instalar dev dependencies:

```bash
python -m pip install --group dev
```

Ejecutar únicamente Inventory:

```bash
python -m pytest \
  tests/test_inventory_stock.py \
  tests/test_inventory_products.py \
  tests/test_inventory_movements.py \
  tests/test_inventory_order_endpoints.py \
  tests/test_inventory_orders.py
```

Baseline comprobado:

```text
104 passed
```

---

# 30. QA técnico frontend

Desde:

```bash
cd uis/backoffice
```

Ejecutar ESLint sobre archivos Inventory:

```bash
npx eslint \
  types/inventory.ts \
  lib/inventoryApi.ts \
  components/inventory/ProductList.tsx \
  components/inventory/InboundStockForm.tsx \
  components/inventory/OutboundStockForm.tsx \
  components/inventory/OrderHistory.tsx \
  components/layout/BackofficeHeader.tsx \
  app/backoffice/inventory/products/page.tsx \
  app/backoffice/inventory/orders/page.tsx \
  app/backoffice/inventory/orders/inbound/page.tsx \
  app/backoffice/inventory/orders/inbound/InboundStockPageContent.tsx \
  app/backoffice/inventory/orders/outbound/page.tsx \
  app/backoffice/inventory/orders/outbound/OutboundStockPageContent.tsx
```

Después:

```bash
npm run build
```

Las rutas esperadas deben incluir:

```text
/backoffice/inventory/products
/backoffice/inventory/orders
/backoffice/inventory/orders/inbound
/backoffice/inventory/orders/outbound
```

---

# 31. Warning de múltiples package-lock.json

Next.js puede mostrar:

```text
Next.js inferred your workspace root...
Detected multiple lockfiles...
```

Actualmente existen lockfiles tanto en:

```text
monorepo root
uis/backoffice
```

Este warning NO bloqueó:

```text
npm run dev
npm run build
```

No realizar un refactor de workspace únicamente para silenciarlo durante un
ticket que no lo requiere.

---

# 32. Diagnóstico rápido

| Síntoma | Revisar primero |
|---|---|
| `No module named uvicorn` | `python -m pip install -e .` |
| `JWT_SECRET_KEY ... required` | exportar JWT_SECRET_KEY |
| `DATABASE_URL ... required` | exportar DATABASE_URL |
| backend no inicia | variables + DB |
| `/health` falla internamente | backend |
| navegador no abre backend | PORTS / forwarding |
| `www-authenticate: tunnel` | backend probablemente Private |
| OPTIONS 404 | URL forwarded / CORS |
| CORS error | CODESPACE_NAME + `.env.local` |
| request apunta al 3000 | NEXT_PUBLIC API URL |
| request nunca aparece en Uvicorn | forwarding/tunnel |
| FastAPI 401/403 | conectividad funciona; revisar auth |
| FastAPI 404 | revisar path |
| Inventory vacío | seed / DATABASE_URL |
| stock incorrecto | verificar DB/warehouse |
| React duplicate key | movement_type + id |
| frontend no toma nueva env | reiniciar Next.js |

---

# 33. Comandos rápidos — sesión nueva de Codespaces

## Terminal Backend

```bash
cd /workspaces/ang553-latam-aie-01-final-project-trackflow

python -m pip install -e .

export JWT_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

export DATABASE_URL="sqlite:////tmp/trackflow_inventory_qa.db"

python -m uvicorn services.api.main:app \
  --host 0.0.0.0 \
  --port 8000
```

---

## Terminal Seed

```bash
cd /workspaces/ang553-latam-aie-01-final-project-trackflow

DATABASE_URL="sqlite:////tmp/trackflow_inventory_qa.db" \
python scripts/seed_inventory.py
```

---

## Configurar Backoffice

```bash
cd /workspaces/ang553-latam-aie-01-final-project-trackflow

cat > uis/backoffice/.env.local <<EOF
NEXT_PUBLIC_API_URL=https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}
NEXT_PUBLIC_INVENTORY_API_URL=https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}
EOF
```

---

## Terminal Frontend

```bash
cd /workspaces/ang553-latam-aie-01-final-project-trackflow/uis/backoffice

npm run dev -- -H 0.0.0.0 -p 3000
```

---

# 34. Preflight rápido antes de depurar una feature

Antes de tocar código comprobar:

```text
[ ] Python dependencies installed
[ ] JWT_SECRET_KEY set
[ ] DATABASE_URL set
[ ] Backend startup complete
[ ] /health = 200
[ ] Port 8000 Public for browser QA
[ ] Port 3000 accessible
[ ] CODESPACE_NAME verified
[ ] .env.local belongs to current Codespace
[ ] Next restarted after env changes
[ ] OPTIONS preflight = 200
[ ] login works
[ ] request reaches Uvicorn
```

Solo después:

```text
depurar la feature
```

---

# 35. Limpieza al terminar QA

Detener servidores:

```text
Ctrl+C
```

en frontend y backend.

En Ports:

```text
8000 → volver a Private
```

Eliminar SQLite temporal si ya no se necesita:

```bash
rm -f /tmp/trackflow_inventory_qa.db
```

La próxima ejecución puede volver a crearla y ejecutar el seed.

Revisar:

```bash
git status --short
```

Confirmar que no aparezcan:

```text
.env.local
database files
tokens
secrets
temporary files
```

---

# 36. Regla de seguridad Git

Nunca hacer commit de:

```text
.env
.env.local
JWT_SECRET_KEY
DATABASE_URL productiva
tokens JWT
contraseñas
credenciales
bases temporales
```

Antes de commit:

```bash
git status --short
git diff --check
git diff
```

---

# 37. Resumen operativo

Cuando un Codespace nuevo necesite levantar TrackFlow:

```text
pip install
    ↓
JWT_SECRET_KEY
    ↓
DATABASE_URL
    ↓
backend :8000
    ↓
/health
    ↓
8000 Public
    ↓
seed Inventory
    ↓
generar forwarded URLs desde CODESPACE_NAME
    ↓
uis/backoffice/.env.local
    ↓
frontend :3000
    ↓
CORS preflight 200
    ↓
register/login
    ↓
QA Inventory
```

Si esa cadena está verde, recién entonces investigar problemas específicos de
la feature.