interface Benefit {
  title: string;
  description: string;
}

const benefits: Benefit[] = [
  {
    title: "Operación binacional",
    description: "El único operador con infraestructura propia en Estados Unidos y España",
  },
  {
    title: "+130 profesionales",
    description: "Dedicados a tu logística",
  },
  {
    title: "Tecnología propia",
    description: "Visibilidad total de tu inventario",
  },
  {
    title: "Especialización e-commerce",
    description: "En moda, electrónica y cosmética",
  },
];

function CheckCircleIcon() {
  return (
    <svg className="h-6 w-6" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  );
}

export function BenefitsSection() {
  return (
    <section className="bg-white px-4 py-12 sm:px-6 sm:py-16 md:px-8 md:py-20">
      <div className="mx-auto max-w-4xl">
        <h2 className="mb-12 text-center text-2xl font-bold text-gray-900 sm:mb-16 sm:text-3xl md:text-4xl">
          Por qué TrackFlow
        </h2>

        <div className="grid grid-cols-1 gap-8 md:grid-cols-2">
          {benefits.map((benefit) => (
            <article key={benefit.title} className="flex gap-4">
              <div className="flex-shrink-0">
                <div className="flex h-12 w-12 items-center justify-center rounded-md bg-blue-600 text-white">
                  <CheckCircleIcon />
                </div>
              </div>
              <div>
                <h3 className="mb-2 text-lg font-semibold text-gray-900">{benefit.title}</h3>
                <p className="text-gray-700">{benefit.description}</p>
              </div>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
