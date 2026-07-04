"use client";

import { useState, type ChangeEvent } from "react";
import { useRef, type FormEvent } from "react";

import {
  SIMPLE_FIELD_NAMES,
  isSimpleFieldName,
  type SimpleFieldName,
  validateCurrent3PL,
  validatePrivacyPolicy,
  validateServicesInterest,
  validateSimpleField,
  shouldShowLowVolumeWarning,
} from "@/lib/applicationValidation";
import { initialApplicationFormValues } from "@/types/applicationForm";

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

const INITIAL_SIMPLE_ERRORS: Record<SimpleFieldName, string> = {
  companyName: "",
  contactPerson: "",
  corporateEmail: "",
  phone: "",
  website: "",
  operationCountry: "",
  productType: "",
  monthlyVolume: "",
};

const INITIAL_GROUP_ERRORS = {
  servicesInterest: "",
  current3PL: "",
  privacyPolicy: "",
};

export function ApplicationForm() {
  const formRef = useRef<HTMLFormElement>(null);
  const [formValues, setFormValues] = useState(initialApplicationFormValues);
  const [simpleErrors, setSimpleErrors] = useState<Record<SimpleFieldName, string>>(INITIAL_SIMPLE_ERRORS);
  const [groupErrors, setGroupErrors] = useState(INITIAL_GROUP_ERRORS);
  const [showSuccessMessage, setShowSuccessMessage] = useState(false);

  const fieldBaseClass =
    "w-full rounded-lg border border-gray-300 px-4 py-2 focus:border-transparent focus:ring-2 focus:ring-blue-500";

  function getFieldClass(field: SimpleFieldName): string {
    return simpleErrors[field]
      ? `${fieldBaseClass} border-red-500 ring-2 ring-red-500`
      : fieldBaseClass;
  }

  function getErrorClass(field: SimpleFieldName, spacingClass: string): string {
    return `${spacingClass} text-sm text-red-600 ${simpleErrors[field] ? "" : "hidden"}`;
  }

  function handleFieldChange(event: ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) {
    const { name, value } = event.target;

    setFormValues((prev) => ({
      ...prev,
      [name]: value,
    }));

    if (isSimpleFieldName(name)) {
      const errorMessage = validateSimpleField(name, value);
      setSimpleErrors((prev) => ({
        ...prev,
        [name]: errorMessage,
      }));
    }

    if (name === "current3PL") {
      setGroupErrors((prev) => ({
        ...prev,
        current3PL: validateCurrent3PL(value),
      }));
    }
  }

  // El grupo de servicios se modela como array para reflejar checkboxes multi-seleccion.
  function handleServicesInterestChange(event: ChangeEvent<HTMLInputElement>) {
    const { value, checked } = event.target;

    setFormValues((prev) => {
      const nextServices = checked
        ? [...prev.servicesInterest, value]
        : prev.servicesInterest.filter((item) => item !== value);

      // Se recalcula el error con la seleccion completa del grupo, no con el checkbox individual.
      setGroupErrors((prevErrors) => ({
        ...prevErrors,
        servicesInterest: validateServicesInterest(nextServices),
      }));

      return {
        ...prev,
        servicesInterest: nextServices,
      };
    });
  }

  function handlePrivacyPolicyChange(event: ChangeEvent<HTMLInputElement>) {
    const { checked } = event.target;

    setFormValues((prev) => ({
      ...prev,
      privacyPolicy: checked,
    }));

    setGroupErrors((prev) => ({
      ...prev,
      privacyPolicy: validatePrivacyPolicy(checked),
    }));
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const nextSimpleErrors = SIMPLE_FIELD_NAMES.reduce<Record<SimpleFieldName, string>>((acc, fieldName) => {
      acc[fieldName] = validateSimpleField(fieldName, formValues[fieldName]);
      return acc;
    }, { ...INITIAL_SIMPLE_ERRORS });

    const nextGroupErrors = {
      servicesInterest: validateServicesInterest(formValues.servicesInterest),
      current3PL: validateCurrent3PL(formValues.current3PL),
      privacyPolicy: validatePrivacyPolicy(formValues.privacyPolicy),
    };

    setSimpleErrors(nextSimpleErrors);
    setGroupErrors(nextGroupErrors);

    const hasErrors =
      Object.values(nextSimpleErrors).some(Boolean) || Object.values(nextGroupErrors).some(Boolean);

    if (hasErrors) {
      setShowSuccessMessage(false);

      const errorOrder = [
        ["companyName", nextSimpleErrors.companyName],
        ["contactPerson", nextSimpleErrors.contactPerson],
        ["corporateEmail", nextSimpleErrors.corporateEmail],
        ["phone", nextSimpleErrors.phone],
        ["website", nextSimpleErrors.website],
        ["operationCountry", nextSimpleErrors.operationCountry],
        ["productType", nextSimpleErrors.productType],
        ["monthlyVolume", nextSimpleErrors.monthlyVolume],
        ["servicesInterest[]", nextGroupErrors.servicesInterest],
        ["current3PL", nextGroupErrors.current3PL],
        ["privacyPolicy", nextGroupErrors.privacyPolicy],
      ] as const;

      // Orden de foco alineado al orden visual del formulario para guiar correcciones de forma predecible.
      const firstInvalidName = errorOrder.find(([, message]) => message)?.[0];
      if (!firstInvalidName || !formRef.current) {
        return;
      }

      const firstInvalidField = formRef.current.querySelector<HTMLElement>(`[name="${firstInvalidName}"]`);
      firstInvalidField?.focus();
      return;
    }

    setSimpleErrors(INITIAL_SIMPLE_ERRORS);
    setGroupErrors(INITIAL_GROUP_ERRORS);
    setShowSuccessMessage(true);
  }

  function handleReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    // Reset integral de UX: estado del formulario, errores y feedback visual.
    setFormValues(initialApplicationFormValues);
    setSimpleErrors(INITIAL_SIMPLE_ERRORS);
    setGroupErrors(INITIAL_GROUP_ERRORS);
    setShowSuccessMessage(false);
  }

  return (
    <>
      <div
        id="successMessage"
        className={`mb-6 rounded-lg border border-green-200 bg-green-50 p-4 ${showSuccessMessage ? "" : "hidden"}`}
        aria-live="polite"
      >
        <div className="flex items-start">
          <svg
            className="mt-0.5 mr-3 h-6 w-6 text-green-600"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <div className="text-green-900">
            <p>¡Gracias por tu interés en TrackFlow!</p>
            <p className="mt-3">
              Hemos recibido tu solicitud. Nuestro equipo comercial revisará tu información y te contactará en las próximas 24-48 horas para agendar una llamada y conocer tus necesidades logísticas en detalle.
            </p>
            <p className="mt-3">
              Si tienes alguna consulta urgente, escríbenos directamente a{" "}
              <a href="mailto:comercial@trackflow.com" className="underline hover:text-green-800">
                comercial@trackflow.com
              </a>
            </p>
          </div>
        </div>
      </div>
      <form
        id="applicationForm"
        ref={formRef}
        onSubmit={handleSubmit}
        onReset={handleReset}
        className="rounded-lg border border-gray-200 bg-white p-8 shadow-sm"
      >
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
            value={formValues.companyName}
            onChange={handleFieldChange}
            placeholder="Ej: Mi Tienda Online"
            className={getFieldClass("companyName")}
          />
          <div id="error-companyName" role="alert" className={getErrorClass("companyName", "mt-2")}>
            {simpleErrors.companyName}
          </div>
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
            value={formValues.contactPerson}
            onChange={handleFieldChange}
            placeholder="Nombre completo"
            className={getFieldClass("contactPerson")}
          />
          <div id="error-contactPerson" role="alert" className={getErrorClass("contactPerson", "mt-2")}>
            {simpleErrors.contactPerson}
          </div>
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
            value={formValues.corporateEmail}
            onChange={handleFieldChange}
            placeholder="contacto@empresa.com"
            className={getFieldClass("corporateEmail")}
          />
          <div id="error-corporateEmail" role="alert" className={getErrorClass("corporateEmail", "mt-2")}>
            {simpleErrors.corporateEmail}
          </div>
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
            value={formValues.phone}
            onChange={handleFieldChange}
            placeholder="+1 (555) 123-4567"
            className={getFieldClass("phone")}
          />
          <div id="error-phone" role="alert" className={getErrorClass("phone", "mt-2")}>
            {simpleErrors.phone}
          </div>
        </div>

        <div className="mb-6">
          <label htmlFor="website" className="mb-2 block text-sm font-medium text-gray-700">
            Sitio web de la empresa
          </label>
          <input
            type="url"
            id="website"
            name="website"
            value={formValues.website}
            onChange={handleFieldChange}
            placeholder="https://www.mitiendaonline.com"
            className={getFieldClass("website")}
          />
          <div id="error-website" role="alert" className={getErrorClass("website", "mt-2")}>
            {simpleErrors.website}
          </div>
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
            value={formValues.operationCountry}
            onChange={handleFieldChange}
            className={`${getFieldClass("operationCountry")} bg-white`}
          >
            <option value="">Selecciona una opción</option>
            {operationCountries.map((country) => (
              <option key={country} value={country}>
                {country}
              </option>
            ))}
          </select>
          <div id="error-operationCountry" role="alert" className={getErrorClass("operationCountry", "mt-3")}>
            {simpleErrors.operationCountry}
          </div>
        </div>

        <div className="mb-6">
          <label htmlFor="productType" className="mb-2 block text-sm font-medium text-gray-700">
            Tipo de producto <span className="text-red-600">*</span>
          </label>
          <select
            id="productType"
            name="productType"
            required
            value={formValues.productType}
            onChange={handleFieldChange}
            className={`${getFieldClass("productType")} bg-white`}
          >
            <option value="">Selecciona una opción</option>
            {productTypes.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
          <div id="error-productType" role="alert" className={getErrorClass("productType", "mt-3")}>
            {simpleErrors.productType}
          </div>
        </div>

        <div className="mb-6">
          <label htmlFor="monthlyVolume" className="mb-2 block text-sm font-medium text-gray-700">
            Volumen mensual estimado de pedidos <span className="text-red-600">*</span>
          </label>
          <select
            id="monthlyVolume"
            name="monthlyVolume"
            required
            value={formValues.monthlyVolume}
            onChange={handleFieldChange}
            className={`${getFieldClass("monthlyVolume")} bg-white`}
          >
            <option value="">Selecciona una opción</option>
            {monthlyVolumes.map((volume) => (
              <option key={volume} value={volume}>
                {volume}
              </option>
            ))}
          </select>
          <div id="error-monthlyVolume" role="alert" className={getErrorClass("monthlyVolume", "mt-3")}>
            {simpleErrors.monthlyVolume}
          </div>
          {shouldShowLowVolumeWarning(formValues.monthlyVolume, formValues.productType) && (
            <p className="mt-3 rounded-md border border-orange-200 bg-orange-50 px-4 py-3 text-sm text-orange-800">
              Para volúmenes menores a 100 envíos mensuales, nuestros servicios podrían no ser la solución más eficiente. ¿Seguro que quieres continuar?
            </p>
          )}
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
                    checked={formValues.servicesInterest.includes(service.value)}
                    onChange={handleServicesInterestChange}
                    className={`h-4 w-4 rounded text-blue-600 focus:ring-2 focus:ring-blue-500 ${
                      groupErrors.servicesInterest ? "ring-2 ring-red-500" : ""
                    }`}
                  />
                  <label htmlFor={service.id} className="ml-3 text-gray-700">
                    {service.label}
                  </label>
                </div>
              ))}
            </div>
          </fieldset>
          <div
            id="error-servicesInterest"
            role="alert"
            className={`mt-3 text-sm text-red-600 ${groupErrors.servicesInterest ? "" : "hidden"}`}
          >
            {groupErrors.servicesInterest}
          </div>
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
                    checked={formValues.current3PL === option.value}
                    onChange={handleFieldChange}
                    className={`h-4 w-4 text-blue-600 focus:ring-2 focus:ring-blue-500 ${
                      groupErrors.current3PL ? "ring-2 ring-red-500" : ""
                    }`}
                  />
                  <label htmlFor={option.id} className="ml-3 text-gray-700">
                    {option.label}
                  </label>
                </div>
              ))}
            </div>
          </fieldset>
          <div
            id="error-current3PL"
            role="alert"
            className={`mt-3 text-sm text-red-600 ${groupErrors.current3PL ? "" : "hidden"}`}
          >
            {groupErrors.current3PL}
          </div>
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
            value={formValues.comments}
            onChange={handleFieldChange}
            placeholder="Cuéntanos sobre tus necesidades logísticas específicas..."
            className="w-full resize-none rounded-lg border border-gray-300 px-4 py-2 focus:border-transparent focus:ring-2 focus:ring-blue-500"
          />
          <div className="mt-2 flex items-center justify-between">
            <div id="error-comments" role="alert" className="hidden text-sm text-red-600" />
            <span className="text-sm text-gray-500">
              <span id="charCount">{formValues.comments.length}</span>/500 caracteres
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
              checked={formValues.privacyPolicy}
              onChange={handlePrivacyPolicyChange}
              className={`mt-1 h-4 w-4 rounded text-blue-600 focus:ring-2 focus:ring-blue-500 ${
                groupErrors.privacyPolicy ? "ring-2 ring-red-500" : ""
              }`}
            />
            <label htmlFor="privacyPolicy" className="ml-3 text-gray-700">
              Acepto la política de privacidad <span className="text-red-600">*</span>
            </label>
          </div>
          <div
            id="error-privacyPolicy"
            role="alert"
            className={`mt-2 text-sm text-red-600 ${groupErrors.privacyPolicy ? "" : "hidden"}`}
          >
            {groupErrors.privacyPolicy}
          </div>
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
    </>
  );
}
