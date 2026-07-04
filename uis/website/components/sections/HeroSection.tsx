import Link from "next/link";

export function HeroSection() {
  return (
    <section
      id="inicio"
      className="bg-gradient-to-br from-blue-50 to-blue-100 px-4 py-12 sm:px-6 sm:py-16 md:px-8 md:py-20"
    >
      <div className="mx-auto max-w-4xl text-center">
        <h1 className="mb-4 text-3xl font-bold text-gray-900 sm:mb-6 sm:text-4xl md:text-5xl">
          Logística que escala con tu e-commerce
        </h1>
        <p className="mb-8 text-base leading-relaxed text-gray-700 sm:mb-10 sm:text-lg md:text-xl">
          Gestión de almacenes, entregas de última milla y logística inversa en Estados Unidos y España.
          Más de 15 años ayudando a marcas de moda, electrónica y cosmética a crecer sin preocuparse por la
          operación.
        </p>
        <Link
          href="/application"
          className="inline-block rounded-lg bg-blue-600 px-8 py-3 font-semibold text-white shadow-md transition hover:bg-blue-700 hover:shadow-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2"
        >
          Solicitar información
        </Link>
      </div>
    </section>
  );
}
