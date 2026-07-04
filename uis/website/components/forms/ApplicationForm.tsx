const operationCountries = ["Estados Unidos", "España", "Ambos", "Otro"] as const;
const productTypes = ["Moda", "Electrónica", "Cosmética", "Alimentación", "Otro"] as const;
const monthlyVolumes = ["0-100", "101-500", "501-2000", "2000+", "No estoy seguro"] as const;

const servicesInterest = [
  { id: "service-storage", value: "Almacenaje", label: "Almacenaje" },
  { id: "service-lastmile", value: "Última milla", label: "Última milla" },
  { id: "service-reverse", value: "Logística inversa", label: "Logística inversa" },
] as const;

const current3plOptions = [
  { id: "3pl-yes", value: "Sí", label: "Sí" },
  { id: "3pl-no", value: "No", label: "No" },
  {
    id: "3pl-evaluating",
    value: "Estoy evaluando opciones",
    label: "Estoy evaluando opciones",
  },
] as const;

export function ApplicationForm() {
  return (
    <form id="applicationForm" className="rounded-lg border border-gray-200 bg-white p-8 shadow-sm">
      <fieldset className="mb-8 border-b border-gray-200 pb-8">
        <legend className="mb-6 text-xl font-semibold text-gray-900">Información de la Empresa</legend>

        <div className="mb-6">
          <label htmlFor="companyName" className="mb-2 block text-sm font-medium text-gray-700">
            Nombre de la empresa <span className="text-red-600">*</span>
          </label>
          <input
            type="text"
            id="companyName"
            name="companyName"
            required
            placeholder="Ej: Mi Tienda Online"
            className="w-full rounded-lg border border-gray-300 px-4 py-2 focus:border-transparent focus:ring-2 focus:ring-blue-500"
          />
          <div id="error-companyName" role="alert" className="mt-2 hidden text-sm text-red-600" />
        </div>

        <div className="mb-6">
          <label htmlFor="contactPerson" className="mb-2 block text-sm font-medium text-gray-700">
            Persona de contacto <span className="text-red-600">*</span>
          </label>
          <input
            type="text"
            id="contactPerson"
            name="contactPerson"
            required
            placeholder="Nombre completo"
            className="w-full rounded-lg border border-gray-300 px-4 py-2 focus:border-transparent focus:ring-2 focus:ring-blue-500"
          />
          <div id="error-contactPerson" role="alert" className="mt-2 hidden text-sm text-red-600" />
        </div>

        <div className="mb-6">
          <label htmlFor="corporateEmail" className="mb-2 block text-sm font-medium text-gray-700">
            Email corporativo <span className="text-red-600">*</span>
          </label>
          <input
            type="email"
            id="corporateEmail"
            name="corporateEmail"
            required
            placeholder="contacto@empresa.com"
            className="w-full rounded-lg border border-gray-300 px-4 py-2 focus:border-transparent focus:ring-2 focus:ring-blue-500"
          />
          <div id="error-corporateEmail" role="alert" className="mt-2 hidden text-sm text-red-600" />
        </div>

        <div className="mb-6">
          <label htmlFor="phone" className="mb-2 block text-sm font-medium text-gray-700">
            Teléfono <span className="text-red-600">*</span>
          </label>
          <input
            type="tel"
            id="phone"
            name="phone"
            required
            placeholder="+1 (555) 123-4567"
            className="w-full rounded-lg border border-gray-300 px-4 py-2 focus:border-transparent focus:ring-2 focus:ring-blue-500"
          />
          <div id="error-phone" role="alert" className="mt-2 hidden text-sm text-red-600" />
        </div>

        <div className="mb-6">
          <label htmlFor="website" className="mb-2 block text-sm font-medium text-gray-700">
            Sitio web de la empresa
          </label>
          <input
            type="url"
            id="website"
            name="website"
            placeholder="https://www.mitiendaonline.com"
            className="w-full rounded-lg border border-gray-300 px-4 py-2 focus:border-transparent focus:ring-2 focus:ring-blue-500"
          />
          <div id="error-website" role="alert" className="mt-2 hidden text-sm text-red-600" />
        </div>
      </fieldset>

      <fieldset className="mb-8 border-b border-gray-200 pb-8">
        <legend className="mb-6 text-xl font-semibold text-gray-900">Información Operacional</legend>

        <div className="mb-6">
          <label htmlFor="operationCountry" className="mb-2 block text-sm font-medium text-gray-700">
            País de operación principal <span className="text-red-600">*</span>
          </label>
          <select
            id="operationCountry"
            name="operationCountry"
            required
            defaultValue=""
            className="w-full rounded-lg border border-gray-300 bg-white px-4 py-2 focus:border-transparent focus:ring-2 focus:ring-blue-500"
          >
            <option value="">Selecciona una opción</option>
            {operationCountries.map((country) => (
              <option key={country} value={country}>
                {country}
              </option>
            ))}
          </select>
          <div id="error-operationCountry" role="alert" className="mt-3 hidden text-sm text-red-600" />
        </div>

        <div className="mb-6">
          <label htmlFor="productType" className="mb-2 block text-sm font-medium text-gray-700">
            Tipo de producto <span className="text-red-600">*</span>
          </label>
          <select
            id="productType"
            name="productType"
            required
            defaultValue=""
            className="w-full rounded-lg border border-gray-300 bg-white px-4 py-2 focus:border-transparent focus:ring-2 focus:ring-blue-500"
          >
            <option value="">Selecciona una opción</option>
            {productTypes.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
          <div id="error-productType" role="alert" className="mt-3 hidden text-sm text-red-600" />
        </div>

        <div className="mb-6">
          <label htmlFor="monthlyVolume" className="mb-2 block text-sm font-medium text-gray-700">
            Volumen mensual estimado de pedidos <span className="text-red-600">*</span>
          </label>
          <select
            id="monthlyVolume"
            name="monthlyVolume"
            required
            defaultValue=""
            className="w-full rounded-lg border border-gray-300 bg-white px-4 py-2 focus:border-transparent focus:ring-2 focus:ring-blue-500"
          >
            <option value="">Selecciona una opción</option>
            {monthlyVolumes.map((volume) => (
              <option key={volume} value={volume}>
                {volume}
              </option>
            ))}
          </select>
          <div id="error-monthlyVolume" role="alert" className="mt-3 hidden text-sm text-red-600" />
        </div>
      </fieldset>

      <fieldset className="mb-8 border-b border-gray-200 pb-8">
        <legend className="mb-6 text-xl font-semibold text-gray-900">Servicios e Integraciones</legend>

        <div className="mb-6">
          <fieldset>
            <legend className="mb-3 text-sm font-medium text-gray-700">
              Servicios de interés <span className="text-red-600">*</span>
            </legend>
            <div className="space-y-3">
              {servicesInterest.map((service) => (
                <div key={service.id} className="flex items-center">
                  <input
                    type="checkbox"
                    id={service.id}
                    name="servicesInterest[]"
                    value={service.value}
                    className="h-4 w-4 rounded text-blue-600 focus:ring-2 focus:ring-blue-500"
                  />
                  <label htmlFor={service.id} className="ml-3 text-gray-700">
                    {service.label}
                  </label>
                </div>
              ))}
            </div>
          </fieldset>
          <div id="error-servicesInterest" role="alert" className="mt-3 hidden text-sm text-red-600" />
        </div>

        <div className="mb-6">
          <fieldset>
            <legend className="mb-3 text-sm font-medium text-gray-700">
              ¿Actualmente trabajas con otro 3PL? <span className="text-red-600">*</span>
            </legend>
            <div className="space-y-3">
              {current3plOptions.map((option) => (
                <div key={option.id} className="flex items-center">
                  <input
                    type="radio"
                    id={option.id}
                    name="current3PL"
                    value={option.value}
                    required
                    className="h-4 w-4 text-blue-600 focus:ring-2 focus:ring-blue-500"
                  />
                  <label htmlFor={option.id} className="ml-3 text-gray-700">
                    {option.label}
                  </label>
                </div>
              ))}
            </div>
          </fieldset>
          <div id="error-current3PL" role="alert" className="mt-3 hidden text-sm text-red-600" />
        </div>
      </fieldset>

      <fieldset className="mb-8 border-b border-gray-200 pb-8">
        <legend className="mb-6 text-xl font-semibold text-gray-900">Información Adicional</legend>

        <div className="mb-6">
          <label htmlFor="comments" className="mb-2 block text-sm font-medium text-gray-700">
            Comentarios o necesidades específicas
          </label>
          <textarea
            id="comments"
            name="comments"
            maxLength={500}
            rows={5}
            placeholder="Cuéntanos sobre tus necesidades logísticas específicas..."
            className="w-full resize-none rounded-lg border border-gray-300 px-4 py-2 focus:border-transparent focus:ring-2 focus:ring-blue-500"
          />
          <div className="mt-2 flex items-center justify-between">
            <div id="error-comments" role="alert" className="hidden text-sm text-red-600" />
            <span className="text-sm text-gray-500">
              <span id="charCount">0</span>/500 caracteres
            </span>
          </div>
        </div>
      </fieldset>

      <fieldset className="mb-8">
        <legend className="mb-6 text-xl font-semibold text-gray-900">Consentimiento</legend>

        <div className="mb-6">
          <div className="flex items-start">
            <input
              type="checkbox"
              id="privacyPolicy"
              name="privacyPolicy"
              required
              className="mt-1 h-4 w-4 rounded text-blue-600 focus:ring-2 focus:ring-blue-500"
            />
            <label htmlFor="privacyPolicy" className="ml-3 text-gray-700">
              Acepto la política de privacidad <span className="text-red-600">*</span>
            </label>
          </div>
          <div id="error-privacyPolicy" role="alert" className="mt-2 hidden text-sm text-red-600" />
        </div>
      </fieldset>

      <div className="pt-6">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <button
            type="submit"
            className="w-full rounded-lg bg-blue-600 px-8 py-3 font-semibold text-white shadow-md transition duration-300 hover:bg-blue-700 hover:shadow-lg sm:w-auto"
          >
            Enviar solicitud
          </button>
          <button
            type="reset"
            id="resetButton"
            className="w-full rounded-lg bg-gray-200 px-8 py-3 font-semibold text-gray-800 shadow-sm transition duration-300 hover:bg-gray-300 hover:shadow-md sm:w-auto"
          >
            Limpiar
          </button>
        </div>
      </div>
    </form>
  );
}
