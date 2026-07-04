# Guía de mantenimiento del Memory Bank

## Objetivo

El `memory-bank/` debe conservar el contexto necesario para que una persona o agente pueda continuar el proyecto sin tener que reconstruir toda la historia desde cero.

Su objetivo no es almacenar cada detalle de todo lo que ocurrió.

La información activa debe ser:

- actual;
- útil;
- fácil de localizar;
- suficientemente corta para leerse rápidamente;
- enfocada en decisiones que todavía afectan el proyecto.

---

## Archivos activos

El banco de memoria principal mantiene estos archivos:

### `projectbrief.md`

Contiene el contexto estable del proyecto:

- qué es TrackFlow;
- problema de negocio;
- objetivos principales;
- alcance general;
- usuarios y necesidades.

Este archivo cambia poco.

Solo debe modificarse cuando cambie realmente:

- el negocio;
- el propósito del producto;
- el alcance general;
- los objetivos principales.

No debe convertirse en una bitácora de avances.

---

### `techContext.md`

Contiene el estado técnico actual del proyecto.

Debe responder principalmente:

- qué tecnologías usamos;
- cómo está organizado el repositorio;
- qué decisiones arquitectónicas siguen vigentes;
- qué integraciones existen;
- qué restricciones técnicas no deben romperse.

Cuando una decisión técnica deja de ser válida, debe actualizarse o sustituirse.

No se debe conservar información obsoleta solo por guardar historial.

El historial técnico importante puede archivarse por separado.

---

### `progress.md`

Contiene el estado actual del trabajo.

Debe mostrar:

- qué está completado;
- qué está en progreso;
- qué está pendiente;
- cuál es el siguiente paso real;
- qué bloqueadores existen.

Debe mantenerse enfocado en el presente.

Cuando un hito antiguo ya no necesita detalle operativo, debe resumirse.

Ejemplo:

En vez de conservar para siempre:

- creación del formulario;
- validación del email;
- warning de bajo volumen;
- contador;
- foco al primer error;
- submit;
- mensaje de éxito;
- reset;

puede resumirse como:

- Hito 4 completado:
  - web pública;
  - formulario funcional;
  - backoffice;
  - integración de lógica compartida;
  - build y lint validados.

---

## Principio principal

El Memory Bank no debe guardar todo el pasado.

Debe guardar el contexto mínimo necesario para tomar buenas decisiones en el presente.

---

## Cuándo compactar

No existe un número exacto de líneas.

Debe considerarse una compactación cuando aparezca una o varias de estas señales:

- hay demasiado historial de hitos ya cerrados;
- cuesta distinguir qué información sigue vigente;
- se repiten decisiones o estados antiguos;
- un agente necesita leer demasiado para entender la tarea actual;
- actualizar pocas líneas requiere procesar archivos demasiado grandes;
- existen secciones completas que ya no afectan el trabajo actual.

---

## Estrategia de archivo

Cuando el detalle histórico deje de ser necesario para el trabajo activo, puede moverse a:

```txt
memory-bank/
├─ projectbrief.md
├─ techContext.md
├─ progress.md
└─ archive/
   ├─ hito-4-summary.md
   ├─ hito-5-summary.md
   └─ ...