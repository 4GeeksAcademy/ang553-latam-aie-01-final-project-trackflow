interface CoverageCountry {
  country: string;
  items: string[];
}

const coverageCountries: CoverageCountry[] = [
  {
    country: "Estados Unidos",
    items: ["Almacén en Los Ángeles", "Cobertura nacional", "Carriers: UPS, FedEx, DHL"],
  },
  {
    country: "España",
    items: ["Almacén en Zaragoza", "Cobertura peninsular e islas", "Carriers: MRW, SEUR, DHL"],
  },
];

function CoverageIcon() {
  return (
    <svg className="mt-1 h-5 w-5 flex-shrink-0 text-blue-600" fill="currentColor" viewBox="0 0 20 20" aria-hidden="true">
      <path d="M5.5 13a3.5 3.5 0 01-.369-6.98 4 4 0 117.753-1.3A4.5 4.5 0 1113.5 13H11V9.413l1.293 1.293a1 1 0 001.414-1.414l-3-3a1 1 0 00-1.414 0l-3 3a1 1 0 001.414 1.414L9 9.414V13H5.5z" />
    </svg>
  );
}

export function CoverageSection() {
  return (
    <section id="cobertura" className="bg-gray-50 px-4 py-12 sm:px-6 sm:py-16 md:px-8 md:py-20">
      <div className="mx-auto max-w-6xl">
        <h2 className="mb-12 text-center text-2xl font-bold text-gray-900 sm:mb-16 sm:text-3xl md:text-4xl">
          Cobertura Global
        </h2>

        <div className="grid grid-cols-1 gap-8 md:grid-cols-2 md:gap-12">
          {coverageCountries.map((country) => (
            <article key={country.country} className="rounded-lg border border-gray-200 bg-white p-8 shadow-sm">
              <h3 className="mb-6 text-2xl font-bold text-gray-900">{country.country}</h3>
              <ul className="mb-6 space-y-4 text-gray-700">
                {country.items.map((item) => (
                  <li key={item} className="flex items-start gap-3">
                    <CoverageIcon />
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
