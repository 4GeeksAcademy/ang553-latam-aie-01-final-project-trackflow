# Elección de Empresa: TrackFlow

** Nota para el equipo académico (Propuesta de adaptación del contexto):**
Para este proyecto, me gustaría adaptar el contexto base sustituyendo las operaciones en España por operaciones en México (creando un corredor logístico Arizona-Sonora). Mi formación en Comercio Internacional y experiencia en almacenes me permitirá aplicar conocimientos reales sobre cruces fronterizos, aduanas y regulaciones del T-MEC, enriqueciendo significativamente el diseño de los agentes de IA y la arquitectura de datos.

**Por qué he elegido esta empresa:**
He elegido TrackFlow para capitalizar mi background en comercio y llevarlo al siguiente nivel con AI Engineering. La complejidad de unificar operaciones binacionales con sistemas fragmentados es un problema real y costoso. Resolver estos cuellos de botella mediante automatización inteligente me permitirá construir un portafolio especializado y de alto impacto operativo.

**Departamentos más interesantes:**
1. **Logística Inversa (Sofía Ramos):** Es el área con mayor potencial de optimización. Automatizar el triaje de devoluciones internacionales reducirá drásticamente los costos de fletes innecesarios y errores en el manejo de mercancía retornada.
2. **Última Milla y Gestión de Transportistas (Carlos Vega):** La gestión de rutas transfronterizas es crítica. Implementar IA para analizar el rendimiento de transportistas en la frontera permitirá una toma de decisiones basada en datos reales de tiempos de cruce y costos.

**Reto de automatización/IA seleccionado:**
Construcción del **Sistema de Inspección y Triaje de Devoluciones Binacional** para el departamento de Logística Inversa.

## Mi idea de Agente de IA

**Agente de Decisión de Retorno Transfronterizo**

* **Qué haría el agente:** Actuaría como el evaluador inteligente en el almacén de México. Analizaría visualmente el estado del producto (vía IA de visión) y calcularía el "Landed Cost" inverso (costo de envío de retorno + aranceles aduanales) para decidir si es rentable regresar el producto a EE. UU. o si debe liquidarse localmente.
* **Qué información necesitaría:** Fotografías del producto, valor comercial del SKU, reglas de negocio de la marca B2B y costos estimados de logística internacional.
* **Qué produciría o desencadenaría:** Emitiría un veredicto automático ("Retornar a Arizona", "Liquidar en Sonora" o "Revisión Manual"). En caso de retorno, generaría la estructura de datos necesaria para la documentación aduanera y la guía de transporte.