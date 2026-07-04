import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "TrackFlow - Logística que escala con tu e-commerce",
  description:
    "TrackFlow: soluciones de logística B2B. Gestión de almacenes, entregas de última milla y logística inversa en Estados Unidos y España. Más de 15 años especializados en moda, electrónica y cosmética.",
  keywords: ["logística", "almacenes", "última milla", "logística inversa", "e-commerce", "TrackFlow"],
  authors: [{ name: "TrackFlow" }],
  robots: {
    index: true,
    follow: true,
  },
  openGraph: {
    title: "TrackFlow - Logística que escala con tu e-commerce",
    description: "Gestión de almacenes, entregas de última milla y logística inversa en Estados Unidos y España",
    type: "website",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="es" className="h-full antialiased">
      <body className="min-h-full bg-slate-100 text-slate-900">{children}</body>
    </html>
  );
}
