import { Footer } from "@/components/layout/Footer";
import { Header } from "@/components/layout/Header";
import { BenefitsSection } from "@/components/sections/BenefitsSection";
import { ContactSection } from "@/components/sections/ContactSection";
import { CoverageSection } from "@/components/sections/CoverageSection";
import { HeroSection } from "@/components/sections/HeroSection";
import { ServicesSection } from "@/components/sections/ServicesSection";

const organizationJsonLd = {
  "@context": "https://schema.org",
  "@type": "Organization",
  name: "TrackFlow",
  description: "Gestión de almacenes y entregas de última milla para e-commerce",
  url: "https://trackflow.com",
  foundingDate: "2009",
  address: [
    {
      "@type": "PostalAddress",
      addressCountry: "US",
      addressLocality: "Los Ángeles",
      addressRegion: "California",
    },
    {
      "@type": "PostalAddress",
      addressCountry: "ES",
      addressLocality: "Zaragoza",
      addressRegion: "Aragón",
    },
  ],
  contactPoint: {
    "@type": "ContactPoint",
    telephone: "+1-213-555-0147",
    contactType: "sales",
    availableLanguage: ["Spanish", "English"],
  },
  sameAs: ["https://linkedin.com/company/trackflow"],
  areaServed: [
    {
      "@type": "Country",
      name: "Estados Unidos",
    },
    {
      "@type": "Country",
      name: "Spain",
    },
  ],
};

export default function HomePage() {
  return (
    <>
      <script
        type="application/ld+json"
        // Se inyecta como JSON-LD para mantener paridad SEO con la web del Hito 1.
        dangerouslySetInnerHTML={{ __html: JSON.stringify(organizationJsonLd) }}
      />
      <Header />
      <main>
        <HeroSection />
        <ServicesSection />
        <CoverageSection />
        <BenefitsSection />
        <ContactSection />
      </main>
      <Footer />
    </>
  );
}
