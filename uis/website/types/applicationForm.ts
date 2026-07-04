export interface ApplicationFormValues {
  companyName: string;
  contactPerson: string;
  corporateEmail: string;
  phone: string;
  website: string;
  operationCountry: string;
  productType: string;
  monthlyVolume: string;
  servicesInterest: string[];
  current3PL: string;
  comments: string;
  privacyPolicy: boolean;
}

export const initialApplicationFormValues: ApplicationFormValues = {
  companyName: "",
  contactPerson: "",
  corporateEmail: "",
  phone: "",
  website: "",
  operationCountry: "",
  productType: "",
  monthlyVolume: "",
  servicesInterest: [],
  current3PL: "",
  comments: "",
  privacyPolicy: false,
};
