# Propuesta de arquitectura del backend de inventario de TrackFlow

## 1. Propósito y alcance

Este documento propone la organización inicial del backend de inventario de TrackFlow antes de comenzar su implementación.

El alcance se limita a:

- registro y consulta de SKUs;
- registro de entradas de stock;
- registro de salidas por despacho o pérdida;
- cálculo de existencias por SKU y almacén;
- trazabilidad del usuario que confirma cada movimiento.

No forman parte de esta propuesta los dominios de envíos, transportistas, devoluciones, clientes, pagos ni una migración del sistema de autenticación.

---

## 2. Contexto del negocio

TrackFlow gestiona inventario y operaciones logísticas para marcas de comercio electrónico de moda, electrónica y cosmética. Opera almacenes en Los Ángeles y Zaragoza.

Actualmente, los almacenes utilizan sistemas diferentes y no existe una capa de datos compartida. Esto dificulta mantener una visión unificada y confiable del inventario. Para TrackFlow, una discrepancia de stock tiene impacto contractual, porque la empresa administra mercancía que pertenece a sus clientes.

El backend debe establecer una API común para registrar cada entrada y salida de mercancía y calcular las existencias actuales con base en esos movimientos.

### Actores principales

- **Operario de almacén:** confirma recepciones de mercancía.
- **Coordinador logístico:** autoriza despachos y bajas por pérdida.
- **Backoffice de TrackFlow:** consulta SKUs, movimientos y existencias.
- **TinyDB:** mantiene la autenticación existente y proporciona la identidad del usuario mediante `user_uuid`.
- **Base de datos SQL de Supabase:** conserva SKUs y movimientos de inventario; no contiene una tabla de usuarios.

---

## 3. Requisitos confirmados

### 3.1 Entidades

#### SKU

Representa un producto administrado por TrackFlow en una ubicación concreta.

Campos persistentes:

- `id`
- `name`
- `sku`
- `client_name`
- `category`
- `warehouse`

El campo `current_stock` es calculado y debe aparecer únicamente en el schema de respuesta; no se almacena en la tabla.

#### StockEntry

Representa una recepción de mercancía.

Campos:

- `id`
- `sku_id`
- `quantity`
- `reference`
- `warehouse`
- `created_at`
- `user_uuid`

#### StockExit

Representa un despacho o una pérdida confirmada.

Campos:

- `id`
- `sku_id`
- `quantity`
- `exit_type`
- `tracking_number`
- `warehouse`
- `created_at`
- `user_uuid`

### 3.2 Reglas de negocio

1. El stock nunca se establece ni se modifica directamente.
2. El stock se calcula con la fórmula:

   `current_stock = SUM(StockEntry.quantity) - SUM(StockExit.quantity)`

3. El cálculo debe filtrar por `sku_id` y `warehouse`.
4. Los almacenes permitidos son `LA` y `ZGZ`.
5. Una salida no puede provocar stock negativo.
6. La validación de stock debe realizarse antes de guardar la salida.
7. Cuando el stock sea insuficiente, la API debe responder con HTTP `400` y el mensaje:

   `Insufficient stock for SKU '{sku}'. Available: {available}, requested: {quantity}.`

8. `tracking_number` es obligatorio cuando `exit_type` es `dispatch`.
9. `tracking_number` debe ser nulo cuando `exit_type` es `loss`.
10. `created_at` se establece automáticamente al crear un movimiento.
11. `user_uuid` referencia a un usuario existente en TinyDB.
12. No debe crearse un modelo SQL `User`.

### 3.3 Decisión sobre `current_stock`

Cada registro de SKU contiene un almacén. Por ello, la API devolverá `current_stock` correspondiente al almacén indicado en ese registro.

Al listar productos, cada SKU se mostrará con su propia ubicación y con el stock calculado exclusivamente para esa combinación de SKU y almacén. No se devolverá una cifra global que sume Los Ángeles y Zaragoza.

Esta elección reduce ambigüedad y cumple la regla de que el stock debe mantenerse separado por ubicación.

---

## 4. Alternativas arquitectónicas evaluadas

### 4.1 MVC

MVC no se propone como patrón principal. El backend no renderizará vistas HTML: expondrá una API JSON consumida por frontends independientes. Forzar las etiquetas Modelo, Vista y Controlador no añadiría claridad a la organización requerida.

Algunos conceptos son comparables —los routers reciben solicitudes y los modelos representan datos—, pero la estructura se comprenderá mejor mediante responsabilidades propias de una API FastAPI.

### 4.2 Arquitectura en capas

Es la opción que mejor se adapta al alcance actual porque permite separar:

- recepción y respuesta HTTP;
- validación de datos;
- reglas de negocio;
- persistencia;
- configuración de dependencias técnicas.

Esta separación facilita probar las reglas críticas de inventario sin convertir el proyecto en una estructura excesivamente compleja.

### 4.3 Arquitectura hexagonal

Una arquitectura hexagonal completa introduciría puertos, adaptadores, interfaces y casos de uso adicionales para un servicio que actualmente posee un solo dominio, tres entidades y seis endpoints.

No se recomienda como estructura inicial. Sin embargo, sí se aplicará su principio más útil: centralizar el acceso a dependencias externas, especialmente Supabase y TinyDB, para evitar que los detalles técnicos se propaguen por todos los endpoints.

### 4.4 Serverless

No se propone serverless como arquitectura principal. La API realiza operaciones transaccionales contra una base de datos y necesita atender solicitudes regulares del backoffice.

En el futuro podrían evaluarse funciones independientes para tareas breves activadas por eventos, pero no existe actualmente un proceso confirmado que justifique introducir ese modelo.

### 4.5 Microservicios

No se recomienda dividir inventario en varios servicios. Separar SKUs, entradas y salidas en despliegues distintos complicaría las transacciones y el cálculo de stock sin aportar un beneficio proporcional al alcance actual.

---

## 5. Arquitectura seleccionada

Se propone un **monolito modular con arquitectura en capas ligera**, implementado como un único servicio FastAPI dentro del monorepo.

### Unidad de despliegue

El backend de inventario será una sola aplicación FastAPI. Esto simplifica:

- configuración;
- acceso a datos;
- transacciones;
- pruebas;
- despliegue;
- documentación de la API.

### Organización interna

La aplicación conservará responsabilidades diferenciadas:

1. **Entrada HTTP:** FastAPI y `APIRouter`.
2. **Validación y contratos:** schemas Pydantic.
3. **Reglas de negocio:** decisiones sobre stock, almacén y tipo de salida.
4. **Persistencia:** modelos y sesiones SQLModel.
5. **Integraciones técnicas:** conexión SQL y cliente TinyDB.

Esta solución ofrece suficiente separación para evitar que toda la lógica quede concentrada en los routers, pero respeta la estructura pequeña exigida para el proyecto.

---

## 6. Dominio y módulos

El dominio actual es **inventario**. Dentro de él existen dos grupos funcionales:

### Productos

Responsabilidades:

- registrar un SKU;
- listar SKUs;
- consultar un SKU por identificador;
- devolver el stock calculado para su almacén.

Datos administrados:

- `SKU`;
- `current_stock` calculado.

### Movimientos de stock

Responsabilidades:

- registrar recepciones;
- registrar despachos;
- registrar pérdidas;
- listar movimientos con información del SKU;
- impedir salidas sin existencias suficientes.

Datos administrados:

- `StockEntry`;
- `StockExit`.

Aunque existen dos grupos funcionales, se mantendrán en un solo router porque la especificación define `services/routers/inventory.py` y el número de endpoints todavía es reducido. Dividirlos ahora en varios routers añadiría navegación y configuración sin resolver un problema real.

---

## 7. Estructura propuesta

```text
services/
├── main.py
├── database.py
├── models.py
├── schemas.py
└── routers/
    ├── __init__.py
    └── inventory.py
```

El archivo `__init__.py` únicamente permite tratar `routers` como paquete de Python; no contiene lógica de negocio.

### `services/main.py`

Responsabilidad:

- crear la instancia de FastAPI;
- registrar el router de inventario;
- configurar CORS;
- definir metadatos generales de la API;
- coordinar, si corresponde, la inicialización de la aplicación.

No debe contener:

- consultas SQL;
- cálculo de stock;
- validaciones de `dispatch` o `loss`;
- creación directa de entradas o salidas;
- endpoints del dominio de inventario.

### `services/database.py`

Responsabilidad:

- configurar el motor SQLModel;
- administrar la sesión de base de datos;
- exponer la dependencia `get_db`;
- centralizar la configuración del cliente TinyDB;
- leer configuración privada desde variables de entorno.

No debe contener:

- rutas HTTP;
- schemas de respuesta;
- decisiones de presentación;
- reglas específicas repetidas para cada endpoint.

La sesión se proporcionará mediante una dependencia de FastAPI por solicitud. Esto permite cerrar correctamente los recursos y sustituir la base de datos durante las pruebas.

### `services/models.py`

Responsabilidad:

- declarar exclusivamente los modelos persistentes `SKU`, `StockEntry` y `StockExit`;
- definir claves primarias y foráneas;
- representar los campos almacenados.

No debe incluir `current_stock` como columna.

Tampoco debe incluir un modelo `User`, porque la identidad permanece en TinyDB.

### `services/schemas.py`

Responsabilidad:

- declarar los modelos Pydantic de solicitud y respuesta;
- validar los cuerpos JSON;
- controlar qué campos acepta el cliente;
- controlar qué campos devuelve la API;
- aplicar la relación entre `exit_type` y `tracking_number` cuando sea viable en el schema.

Se utilizarán variantes de creación y respuesta para las entidades confirmadas. Los schemas de creación no deben aceptar campos generados por el servidor como `id`, `created_at` o `current_stock`.

El schema de respuesta de `SKU` incluirá `current_stock`. Los modelos SQL no lo almacenarán.

### `services/routers/inventory.py`

Responsabilidad:

- declarar `APIRouter(prefix="/inventory")`;
- recibir parámetros y cuerpos ya validados;
- obtener la sesión mediante `get_db`;
- coordinar consultas, reglas de negocio y respuestas;
- convertir errores esperados en respuestas HTTP coherentes.

No debe:

- crear conexiones globales por endpoint;
- validar manualmente JSON que Pydantic puede validar;
- duplicar la fórmula de stock en múltiples rutas;
- mezclar funcionalidades ajenas al inventario.

Debido a que la estructura obligatoria no incluye todavía un módulo adicional de servicios, la lógica compartida deberá mantenerse en funciones internas claramente identificadas y reutilizables. Si el dominio crece o el router adquiere demasiadas responsabilidades, esas funciones podrán extraerse posteriormente a un módulo de servicio sin cambiar el contrato HTTP.

---

## 8. Organización de rutas

El router utilizará el prefijo `/inventory`.

| Método | Ruta | Responsabilidad | Respuesta principal |
|---|---|---|---|
| `GET` | `/inventory/products` | Listar SKUs con stock calculado para su almacén | Lista de SKUs |
| `POST` | `/inventory/products` | Registrar un nuevo SKU | SKU creado |
| `GET` | `/inventory/products/{id}` | Obtener un SKU con su stock actual | SKU encontrado |
| `POST` | `/inventory/orders/inbound` | Registrar una recepción | `StockEntry` creado |
| `POST` | `/inventory/orders/outbound` | Registrar un despacho o pérdida | `StockExit` creado |
| `GET` | `/inventory/orders` | Listar entradas y salidas con datos del SKU | Lista de movimientos |

No se añadirán rutas de actualización o eliminación porque no forman parte del alcance confirmado.

### Parámetros y cuerpos

- `{id}` es un parámetro de ruta porque identifica un SKU concreto.
- Los endpoints `POST` reciben cuerpos JSON validados con Pydantic.
- Los endpoints `GET` no requieren cuerpo.
- No se agregarán filtros, búsqueda, ordenamiento ni paginación hasta que exista un requisito explícito.
- Los contratos de respuesta se declararán con modelos de respuesta para que FastAPI valide, filtre y documente la salida.

---

## 9. Respuestas HTTP y manejo de errores

| Operación | Resultado | Código |
|---|---|---|
| Consulta exitosa | Recurso o lista devuelta | `200 OK` |
| Creación exitosa | SKU o movimiento creado | `201 Created` |
| SKU inexistente | No se encontró el recurso solicitado | `404 Not Found` |
| Stock insuficiente | La salida incumple una regla de negocio | `400 Bad Request` |
| Cuerpo inválido | Datos ausentes, tipos incorrectos o combinación inválida | `422 Unprocessable Entity` |
| Error inesperado | Fallo no controlado del servidor | `500 Internal Server Error` |

Los errores esperados se comunicarán mediante `HTTPException`. La respuesta de stock insuficiente utilizará exactamente el mensaje especificado.

La validación de `tracking_number` debe ocurrir antes de persistir la salida:

- `dispatch` sin número de seguimiento: rechazo de validación;
- `loss` con número de seguimiento: rechazo de validación.

La API no debe responder `200 OK` cuando una operación no se realizó.

---

## 10. Flujo crítico: registrar una salida de stock

### 1. Frontend

El backoffice envía una solicitud `POST /inventory/orders/outbound` con:

- `sku_id`;
- `quantity`;
- `exit_type`;
- `tracking_number`;
- `warehouse`;
- `user_uuid`.

### 2. FastAPI y Pydantic

FastAPI recibe el JSON y Pydantic valida:

- presencia y tipo de los campos;
- valores permitidos para almacén;
- valores permitidos para `exit_type`;
- relación entre `exit_type` y `tracking_number`;
- formato general de la solicitud.

Un cuerpo inválido se rechaza antes de ejecutar la escritura.

### 3. Router de inventario

El router:

1. obtiene una sesión mediante `get_db`;
2. busca el SKU;
3. responde `404` si no existe;
4. verifica que la ubicación del movimiento sea coherente con el SKU;
5. calcula el stock disponible filtrando por `sku_id` y `warehouse`;
6. compara el stock disponible con la cantidad solicitada.

La verificación de coherencia entre el almacén del SKU y el del movimiento es una recomendación para evitar registros imposibles, no un campo adicional del modelo.

### 4. Regla de stock

Si la cantidad solicitada supera la disponibilidad, la operación termina sin escritura y devuelve HTTP `400` con el mensaje obligatorio.

### 5. Persistencia

Si existe stock suficiente:

1. se crea `StockExit`;
2. se confirma la transacción;
3. se actualiza el objeto persistido;
4. se devuelve la respuesta con HTTP `201`.

### 6. Atomicidad

La consulta de disponibilidad y la inserción de la salida deben formar parte de una operación atómica. De lo contrario, dos solicitudes simultáneas podrían leer el mismo stock y aprobar salidas que, en conjunto, produzcan existencias negativas.

El mecanismo exacto —bloqueo, nivel de aislamiento o estrategia equivalente— dependerá de la configuración final de la base de datos, pero la arquitectura debe preservar esta garantía.

---

## 11. Separación entre frontend y backend

### Comunicación

Los frontends Next.js y el backend FastAPI son sistemas separados que se comunican mediante HTTP y JSON.

El frontend es responsable de:

- interfaz;
- formularios;
- navegación;
- presentación de errores;
- validaciones de experiencia de usuario.

El backend es responsable de:

- validación autoritativa;
- reglas de stock;
- persistencia;
- control de errores;
- integridad de los datos;
- contrato de la API.

Las validaciones del frontend mejoran la experiencia, pero nunca sustituyen las validaciones del backend.

### Variables de entorno

El frontend únicamente debe conocer la URL pública de la API mediante una variable apropiada para Next.js.

El backend debe mantener de forma privada:

- cadena de conexión SQL;
- configuración de Supabase;
- ubicación o configuración de TinyDB;
- secretos de autenticación, si existen;
- lista de orígenes permitidos.

Los secretos no deben escribirse en el código ni exponerse mediante variables públicas del frontend.

### CORS

Como el frontend y el backend pueden ejecutarse en dominios o puertos diferentes, FastAPI configurará CORS.

Los orígenes permitidos deberán declararse explícitamente por entorno. No se recomienda permitir todos los orígenes en producción, especialmente si se utilizan credenciales.

### Monorepo y despliegue

Se conservará el monorepo porque ya separa las interfaces en `uis/` y los servicios en `services/`. Esto facilita mantener en un mismo repositorio la documentación y la evolución coordinada del contrato.

Permanecer en un monorepo no obliga a desplegar todo junto. El frontend y el backend pueden tener procesos de construcción, variables y despliegues independientes.

### Contrato y documentación

FastAPI generará el esquema OpenAPI y la documentación interactiva en `/docs`. Esa documentación será la referencia inicial para revisar rutas, cuerpos, respuestas y errores entre los equipos de frontend y backend.

---

## 12. Persistencia y autenticación

### SQLModel y Supabase

SQLModel representará las tablas y administrará las operaciones SQL. Supabase aparece como la plataforma SQL prevista; la cadena de conexión, el entorno local y la estrategia de migraciones deben documentarse durante la configuración.

### Stock derivado

`current_stock` no debe existir como columna ni aceptar escrituras. Se calcula al consultar productos mediante agregaciones de entradas y salidas.

Centralizar el cálculo reduce el riesgo de que distintos endpoints utilicen fórmulas o filtros diferentes.

### TinyDB

TinyDB conserva la autenticación. La base SQL solo almacena `user_uuid` en los movimientos para auditoría.

No se duplicará la información de usuarios ni se creará una relación foránea SQL hacia una tabla inexistente.

El mecanismo exacto para verificar el usuario de cada solicitud todavía necesita confirmarse. Esa verificación deberá encapsularse como dependencia o función compartida, no repetirse manualmente en cada endpoint.

---

## 13. Riesgos y medidas preventivas

### 13.1 Cálculo global en lugar de cálculo por almacén

**Riesgo:** sumar movimientos de LA y ZGZ y devolver una cifra incorrecta.

**Prevención:** toda consulta de stock debe filtrar simultáneamente por `sku_id` y `warehouse`.

### 13.2 Condiciones de carrera en salidas

**Riesgo:** dos solicitudes simultáneas aprueban salidas con base en la misma disponibilidad.

**Prevención:** ejecutar la validación y la inserción dentro de una transacción atómica y utilizar el mecanismo de concurrencia apropiado para la base de datos.

### 13.3 Lógica duplicada en routers

**Riesgo:** cada endpoint calcula el stock o valida salidas de forma diferente.

**Prevención:** mantener funciones compartidas para el cálculo y las reglas críticas; extraerlas a un módulo de servicio cuando el tamaño lo justifique.

### 13.4 `current_stock` persistido accidentalmente

**Riesgo:** el valor almacenado queda desincronizado de los movimientos.

**Prevención:** excluirlo de los modelos de tabla y de los schemas de entrada; incluirlo solo en respuestas.

### 13.5 Inconsistencia entre SKU y almacén del movimiento

**Riesgo:** registrar un movimiento ZGZ para un SKU asociado a LA.

**Prevención:** comprobar la coherencia antes de guardar y rechazar la operación.

### 13.6 Validación incompleta de `tracking_number`

**Riesgo:** despachos sin seguimiento o pérdidas con un número que no corresponde.

**Prevención:** validar la combinación en Pydantic y conservar una comprobación de negocio antes de persistir.

### 13.7 Dependencia directa de TinyDB en cada endpoint

**Riesgo:** acoplamiento, repetición y respuestas inconsistentes si TinyDB falla.

**Prevención:** centralizar el cliente y la validación de identidad mediante una dependencia compartida.

### 13.8 Configuración CORS demasiado abierta

**Riesgo:** permitir solicitudes desde orígenes no autorizados.

**Prevención:** utilizar listas explícitas por entorno y mantener la configuración fuera del código.

### 13.9 Sobreingeniería

**Riesgo:** introducir repositorios, puertos, adaptadores, microservicios o múltiples routers sin una necesidad actual.

**Prevención:** conservar la estructura definida y extraer nuevas capas únicamente cuando exista crecimiento medible o una dependencia que necesite aislamiento.

---

## 14. Estrategia de pruebas

### Reglas de negocio

- stock calculado como entradas menos salidas;
- separación de stock entre LA y ZGZ;
- rechazo de una salida superior a la disponibilidad;
- aceptación de una salida igual a la disponibilidad;
- obligatoriedad de `tracking_number` en `dispatch`;
- nulidad de `tracking_number` en `loss`.

### Routers

- códigos `200`, `201`, `400`, `404` y `422`;
- mensaje exacto para stock insuficiente;
- consulta de SKU con `current_stock`;
- listado de movimientos con datos del SKU;
- SKU inexistente;
- cuerpos incompletos o con valores no permitidos.

### Persistencia

- `created_at` generado automáticamente;
- relaciones mediante `sku_id`;
- `current_stock` ausente de la tabla;
- `user_uuid` almacenado sin crear una tabla de usuarios.

### Integración y concurrencia

- sesión de base de datos por solicitud;
- sustitución de dependencias durante pruebas;
- aislamiento entre almacenes;
- solicitudes simultáneas que intenten consumir el mismo stock.

---

## 15. Decisiones abiertas

Las siguientes decisiones no deben inventarse durante la implementación:

1. cadena de conexión y configuración exacta de Supabase;
2. base de datos utilizada en desarrollo local;
3. herramienta y estrategia de migraciones;
4. política de unicidad e índices para `sku`;
5. mecanismo exacto para validar `user_uuid` en TinyDB;
6. sistema de autenticación enviado por el frontend;
7. configuración de despliegue;
8. logging y telemetría;
9. filtros o paginación futura para listados.

Estas decisiones pueden resolverse sin cambiar la arquitectura principal.

---

## 16. Plan inicial de implementación

1. Confirmar configuración de Supabase, TinyDB y variables de entorno.
2. Crear la estructura base dentro de `services/`.
3. Definir los modelos SQLModel con los campos exactos.
4. Definir schemas de creación y respuesta.
5. Configurar el motor, la sesión y `get_db`.
6. Implementar el router y registrar sus seis endpoints.
7. Centralizar el cálculo de stock por SKU y almacén.
8. Implementar las validaciones de salida y seguimiento.
9. Crear los datos semilla requeridos.
10. Añadir pruebas de reglas, rutas, persistencia y concurrencia.
11. Configurar CORS y verificar la integración con el frontend.
12. Revisar `/docs` como contrato antes de conectar el backoffice.

---

## 17. Conclusión

TrackFlow necesita una base de inventario confiable antes de ampliar el backend hacia otros procesos logísticos. Para el alcance actual, la solución más adecuada es un único servicio FastAPI organizado como monolito modular con separación ligera por responsabilidades.

La propuesta evita dos extremos:

- concentrar rutas, reglas y persistencia en `main.py`;
- introducir una arquitectura compleja que el tamaño actual no necesita.

La estructura definida permite que el equipo comprenda dónde pertenece cada responsabilidad, protege las reglas críticas de stock y mantiene abierta una evolución gradual. Si el dominio crece, la lógica compartida podrá extraerse a servicios o adaptadores sin romper las rutas ni los contratos iniciales.

---

## Referencias técnicas consultadas

- [FastAPI: Bigger Applications — Multiple Files](https://fastapi.tiangolo.com/tutorial/bigger-applications/)
- [FastAPI: Response Model](https://fastapi.tiangolo.com/tutorial/response-model/)
- [FastAPI: CORS](https://fastapi.tiangolo.com/tutorial/cors/)
- [SQLModel: Session with FastAPI Dependency](https://sqlmodel.tiangolo.com/tutorial/fastapi/session-with-dependency/)