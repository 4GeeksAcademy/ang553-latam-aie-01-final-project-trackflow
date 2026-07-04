interface Service {
  title: string;
  items: string[];
}

const services: Service[] = [
  {
    title: "Gestión de Almacenes",
    items: [
      "Almacenamiento, picking y packing",
      "Inventario en tiempo real",
      "Operamos almacenes en Los Ángeles y Zaragoza",
    ],
  },
  {
    title: "Entregas de Última Milla",
    items: [
      "Red de carriers certificados en ambos países",
      "Seguimiento unificado de envíos",
      "Gestión de incidencias y devoluciones",
    ],
  },
  {
    title: "Logística Inversa",
    items: [
      "Gestión completa de devoluciones",
      "Inspección y reacondicionamiento",
      "Integración con tu plataforma de ventas",
    ],
  },
];

function CheckIcon() {
  return (
    <svg className="mt-1 h-5 w-5 flex-shrink-0 text-blue-600" fill="currentColor" viewBox="0 0 20 20" aria-hidden="true">
      <path
        fillRule="evenodd"
        d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
        clipRule="evenodd"
      />
    </svg>
  );
}

export function ServicesSection() {
  return (
    <section id="servicios" className="bg-white px-4 py-12 sm:px-6 sm:py-16 md:px-8 md:py-20">
      <div className="mx-auto max-w-6xl">
        <h2 className="mb-12 text-center text-2xl font-bold text-gray-900 sm:mb-16 sm:text-3xl md:text-4xl">
          Nuestros Servicios
        </h2>

        <div className="grid grid-cols-1 gap-8 md:grid-cols-3">
          {services.map((service) => (
            <article
              key={service.title}
              className="rounded-lg border border-gray-200 bg-gray-50 p-6 transition hover:shadow-lg sm:p-8"
            >
              <h3 className="mb-4 text-xl font-bold text-gray-900 sm:text-2xl">{service.title}</h3>
              <ul className="space-y-3 text-gray-700">
                {service.items.map((item) => (
                  <li key={item} className="flex items-start gap-3">
                    <CheckIcon />
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
