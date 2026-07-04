import type { Metadata } from "next";
import Link from "next/link";

import { Footer } from "@/components/layout/Footer";
import { ApplicationForm } from "@/components/forms/ApplicationForm";

export const metadata: Metadata = {
  title: "Solicitud de Información - TrackFlow",
  description: "Solicitud de información TrackFlow - Formulario para empresas de e-commerce",
  robots: {
    index: true,
    follow: true,
  },
};

export default function ApplicationPage() {
  return (
    <>
      <header className="sticky top-0 z-50 bg-white shadow-sm">
        <nav className="mx-auto flex max-w-7xl items-center justify-between px-4 py-4 sm:px-6 lg:px-8" aria-label="Navegación de solicitud">
          <div className="flex-shrink-0">
            <Link href="/" className="text-2xl font-bold text-blue-600 transition hover:text-blue-700">
              TrackFlow
            </Link>
          </div>
          <Link
            href="/"
            className="text-sm font-medium text-gray-700 transition hover:text-blue-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2"
          >
            ← Volver al inicio
          </Link>
        </nav>
      </header>

      <main className="mx-auto max-w-3xl px-4 py-12 sm:px-6 md:py-16 lg:px-8">
        <div className="mb-12">
          <h1 className="mb-4 text-3xl font-bold text-gray-900 sm:text-4xl md:text-5xl">Solicita Información</h1>
          <p className="text-base text-gray-700 sm:text-lg">
            Cuéntanos sobre tu empresa y tus necesidades logísticas. Nos pondremos en contacto en 24 horas.
          </p>
        </div>

        {/* Este contenedor se conserva para acoplar la lógica de éxito en el Paso 4 sin cambiar IDs. */}
        <div id="successMessage" className="mb-6 hidden rounded-lg border border-green-200 bg-green-50 p-4" aria-live="polite">
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
            <div>
              <h2 className="mb-1 font-semibold text-green-900">Solicitud enviada correctamente</h2>
              <p className="text-sm text-green-800">Gracias por tu interés. Te contactaremos pronto.</p>
            </div>
          </div>
        </div>

        <ApplicationForm />

        <div className="mt-4 text-center">
          <Link
            href="/"
            className="font-medium text-blue-600 transition hover:text-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2"
          >
            Volver al inicio
          </Link>
        </div>
      </main>

      <div className="mt-16">
        <Footer />
      </div>
    </>
  );
}
