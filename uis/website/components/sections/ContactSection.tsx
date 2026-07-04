interface ContactItem {
  title: string;
  href: string;
  label: string;
}

const contacts: ContactItem[] = [
  {
    title: "Email",
    href: "mailto:comercial@trackflow.com",
    label: "comercial@trackflow.com",
  },
  {
    title: "Los Ángeles",
    href: "tel:+12135550147",
    label: "+1 213 555 0147",
  },
  {
    title: "Zaragoza",
    href: "tel:+34976123456",
    label: "+34 976 123 456",
  },
];

export function ContactSection() {
  return (
    <section id="contacto" className="bg-gray-50 px-4 py-12 sm:px-6 sm:py-16 md:px-8 md:py-20">
      <div className="mx-auto max-w-4xl text-center">
        <h2 className="mb-12 text-2xl font-bold text-gray-900 sm:text-3xl md:text-4xl">Contacta con nosotros</h2>

        <div className="grid grid-cols-1 gap-8 md:grid-cols-3">
          {contacts.map((contact) => (
            <article key={contact.title} className="rounded-lg border border-gray-200 bg-white p-6 shadow-sm sm:p-8">
              <h3 className="mb-3 text-lg font-semibold text-gray-900">{contact.title}</h3>
              <a
                href={contact.href}
                className="font-medium text-blue-600 transition hover:text-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2"
              >
                {contact.label}
              </a>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
