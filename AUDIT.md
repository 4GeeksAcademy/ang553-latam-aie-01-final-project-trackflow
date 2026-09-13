# Fase 2.5 — Auditoría técnica BEFORE consolidada

## 1. Estado inicial

Esta auditoría documenta el estado **BEFORE** de Website y Backoffice, antes
de cualquier corrección de frontend. La matriz contiene Website `/` desktop y
mobile, Website `/application` desktop y mobile, y Backoffice `/` desktop y
mobile, medidas con Lighthouse Navigation en Codespaces.

### Baseline oficial BEFORE

| Frontend | Ruta | Dispositivo | Performance | Accessibility | Best Practices | SEO |
|---|---|---|---:|---:|---:|---:|
| Website | `/` | Desktop | 100 | 100 | 100 | 60 |
| Website | `/` | Mobile | 89 | 100 | 100 | 60 |
| Website | `/application` | Desktop | 100 | 98 | 100 | 60 |
| Website | `/application` | Mobile | 95 | 98 | 100 | 60 |
| Backoffice | `/` | Desktop | 96 | 95 | 100 | 60 |
| Backoffice | `/` | Mobile | 65 | 95 | 100 | 60 |

| Frontend | Ruta | Dispositivo | FCP | LCP | TBT | CLS | Speed Index |
|---|---|---|---:|---:|---:|---:|---:|
| Website | `/` | Desktop | 0.5 s | 0.5 s | 10 ms | 0 | 0.7 s |
| Website | `/` | Mobile | 1.9 s | 2.1 s | 330 ms | 0 | 3.4 s |
| Website | `/application` | Desktop | 0.4 s | 0.4 s | 10 ms | 0 | 0.6 s |
| Website | `/application` | Mobile | 1.0 s | 1.0 s | 250 ms | 0 | 1.0 s |
| Backoffice | `/` | Desktop | 0.4 s | 1.3 s | 20 ms | 0 | 1.2 s |
| Backoffice | `/` | Mobile | 1.0 s | 5.9 s | 510 ms | 0 | 2.5 s |

Estos valores son los oficiales y no se reemplazan con ejecuciones posteriores.
La evidencia primaria está en [`audit/before/`](audit/before/): JSON completos
y screenshots BEFORE.

### Metodología de evidencia

La jerarquía aplicada es: **1) JSON Lighthouse BEFORE, 2) screenshots BEFORE,
3) código, 4) recomendaciones de skills, 5) interpretación técnica**. Las
skills se usaron como checklist y segunda opinión, no como fuente de verdad.

## 2. Correcciones conceptuales incorporadas

### Lab, field, simulated y observed

No existen datos field, CrUX ni RUM actualmente. Para Backoffice Mobile,
Lighthouse reporta un LCP simulated de aproximadamente **5.939 s**; la traza
capturada muestra un LCP observed de aproximadamente **1.818 s**, compuesto por
TTFB **131 ms** y element render delay **1.687 s**:

```text
131.163 ms + 1686.904 ms ≈ 1818.067 ms
```

Lighthouse usa el valor simulated para scoring; el observed pertenece a la
traza capturada. No existe evidencia de un bloque perdido de aproximadamente
4.12 s. No deben mezclarse ambos como una sola timeline.

INP no tiene una medición válida actual: **TBT no equivale a INP** y no se
inventa un valor. CLS fue **0 en las seis mediciones**, es saludable y no
requiere corrección.

## 3. Performance findings

### Backoffice Mobile — confirmado

Performance **65**; LCP simulated **~5.939 s**; el elemento LCP es el párrafo
“Visión consolidada del inventario...” de `OperationalSummary.tsx`; LCP
observed **~1.818 s**; TTFB observed **~131 ms**; element render delay observed
**~1.687 s**; TBT **510 ms**; main-thread work **~2.1 s**; JavaScript bootup
**~1.3 s**; unused JavaScript **~297 KiB**; **9 long tasks**; CLS **0**; y
render-blocking opportunity **~130 ms**.

También está confirmado que `AuthProvider` y `AuthGuard` añaden JavaScript de
cliente, que `AuthGuard` devuelve `null` mientras `isLoading`, y que existe un
`GET /auth/me` en mount cuando existe un token.

### Backoffice Mobile — probable / requiere más evidencia

`AuthGuard`/`AuthProvider`, hydration, JavaScript inicial, el modo `next dev` y
`/auth/me` pueden contribuir respectivamente al render delay, al LCP, a los
costes amplificados o al tiempo previo a contenido. Son hipótesis, no causas
únicas; requieren atribución antes de decidir cambios.

### Website Home Mobile — confirmado

Performance **89**, FCP **1.9 s**, LCP **2.1 s**, TBT **330 ms**, Speed Index
**3.4 s**, CLS **0**, unused JavaScript **~335 KiB**, main-thread work elevado
y long tasks presentes. Core Web Vitals clasifica LCP 2.1 s como **GOOD**.

La ruta Home inspeccionada no contiene `use client` explícito ni se
identificaron `useState`, `useEffect`, `window`, `document` o `localStorage`.
No hay evidencia suficiente para aplicar dynamic imports o convertir
componentes. Unused JS es oportunidad, no incidente crítico.

## 4. Accessibility / SEO

### Accessibility — causas confirmadas

- Website `/application`: `heading-order` por el `h3` **“TrackFlow”** en
  `Footer.tsx`.
- Backoffice: `color-contrast` en `BackofficeHeader.tsx`; el JSON registra,
  entre otros, ratios **1.48:1** para navegación y **1.27:1** para “Logout”,
  frente al mínimo esperado de 4.5:1.

Se mantienen los scores 98 de `/application` y 95 de Backoffice.

### SEO

Website falla `is-crawlable`: la respuesta contiene
`x-robots-tag: noindex, nofollow`, mientras la metadata declara `index/follow`.
El origen probable es el entorno/proxy; no debe modificarse metadata
arbitrariamente. En Backoffice `noindex, nofollow` es explícito e intencional y
no es un bug SEO del panel interno.

## 5. Refactoring Candidates

1. **`useApiResource`** en `products/page.tsx` y `orders/page.tsx`: patrón
	duplicado, prioridad **ALTA**, candidato a custom hook.
2. **`requestJson` / `ApiError`** en `inventoryApi.ts` y `suppliersApi.ts`:
	candidato a utilidad interna compartida.
3. **`AuthCard`** para login/register/forgot/reset: componente reutilizable
	justificable.

El candidato recomendado para cumplir el ticket es **`useApiResource`**. No se
recomiendan UI library Website↔Backoffice, `Button`/`Card` global, hook
universal de formularios ni otro hook de auth. No se implementa nada aquí.

## 6. Skill-Assisted Review

Skills registradas: `addyosmani/web-quality-skills/core-web-vitals` y
`addyosmani/web-quality-skills/performance`.

Core Web Vitals aportó thresholds, lab vs field, simulated vs observed,
breakdown de LCP, TBT ≠ INP y la decisión de no optimizar CLS saludable.
Performance aportó la distinción entre bytes descargados/unused y ejecución,
la necesidad de atribuir main-thread y long tasks, que unused JS no justifica
dynamic import automáticamente, que dev/prod no deben mezclarse y que los
cambios deben validarse bajo condiciones equivalentes.

No se copió el contenido completo de ninguna skill; ambas son apoyo
metodológico y segunda opinión.

## 7. Backlog BEFORE

1. **Backoffice Mobile — JS/main-thread/TBT y render delay:** identificar
	módulos y fases responsables antes de decidir code splitting o cambios de
	hydration/auth.
2. **Backoffice — color contrast:** revisar estilos de `BackofficeHeader.tsx`.
3. **Website `/application` — heading order:** revisar el `h3` de `Footer.tsx`.
4. **Website — JavaScript/TBT opportunity:** perfilar ejecución y long tasks.
5. **SEO Website:** investigar el `x-robots-tag` del entorno/proxy.

RUM queda excluido por falta de datos. El backlog no prescribe una
implementación; no se afirma que dynamic import vaya a resolver el problema.

## 8. Validaciones

- Preflight ejecutado: rama `feature/frontend-performance-audit`.
- No se ejecutó Lighthouse; no se modificó código; no hubo refactor, commit ni
  push.
- No se creó `REPORT.md`; permanece reservado para AFTER.
- La validación final se hará con `git diff --check`, `git diff -- AUDIT.md` y
  `git status --short`.

## 9. Estado final Git

El único archivo modificado debe ser `AUDIT.md`. Los únicos untracked
preexistentes deben seguir siendo:

```text
?? .agents/skills/core-web-vitals/
?? .agents/skills/performance/
?? skills-lock.json
```

No se realizó commit.
