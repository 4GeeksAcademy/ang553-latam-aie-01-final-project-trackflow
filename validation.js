// validation.js
// JavaScript vanilla para validar el formulario de solicitud de TrackFlow.

window.addEventListener('DOMContentLoaded', () => {
  // Referencias al formulario y sus elementos.
  const form = document.getElementById('applicationForm');
  const successMessage = document.getElementById('successMessage');
  const successText = successMessage.querySelector('p');
  const companyName = document.getElementById('companyName');
  const contactPerson = document.getElementById('contactPerson');
  const corporateEmail = document.getElementById('corporateEmail');
  const phone = document.getElementById('phone');
  const website = document.getElementById('website');
  const operationCountry = document.getElementById('operationCountry');
  const productType = document.getElementById('productType');
  const monthlyVolume = document.getElementById('monthlyVolume');
  const servicesInterest = document.querySelectorAll('input[name="servicesInterest[]"]');
  const current3PL = document.querySelectorAll('input[name="current3PL"]');
  const comments = document.getElementById('comments');
  const charCount = document.getElementById('charCount');
  const privacyPolicy = document.getElementById('privacyPolicy');

  // Advertencia especial para volumen 0-100.
  const warningNode = document.createElement('p');
  warningNode.className = 'mt-3 text-sm text-orange-800 bg-orange-50 border border-orange-200 rounded-md px-4 py-3 hidden';
  warningNode.textContent =
    'Para volúmenes menores a 100 envíos mensuales, nuestros servicios podrían no ser la solución más eficiente. ¿Seguro que quieres continuar?';
  monthlyVolume.parentNode.appendChild(warningNode);

  // Configuración inicial del mensaje de éxito.
  successMessage.classList.add('hidden');
  successMessage.style.whiteSpace = 'pre-line';

  // Eventos de validación en tiempo real.
  companyName.addEventListener('input', () => validateCompanyName());
  contactPerson.addEventListener('input', () => validateContactPerson());
  corporateEmail.addEventListener('input', () => validateCorporateEmail());
  phone.addEventListener('input', () => validatePhone());
  website.addEventListener('input', () => validateWebsite());
  operationCountry.addEventListener('change', () => validateOperationCountry());
  productType.addEventListener('change', () => validateProductType());
  monthlyVolume.addEventListener('change', () => {
    validateMonthlyVolume();
    updateVolumeWarning();
  });
  comments.addEventListener('input', () => {
    updateCommentCount();
    validateComments();
  });
  privacyPolicy.addEventListener('change', () => validatePrivacyPolicy());

  servicesInterest.forEach(checkbox => {
    checkbox.addEventListener('change', () => validateServicesInterest());
  });

  current3PL.forEach(radio => {
    radio.addEventListener('change', () => validateCurrent3PL());
  });

  // Envío del formulario.
  form.addEventListener('submit', event => {
    event.preventDefault();
    const isValid = validateForm();

    if (isValid) {
      clearAllErrors();
      showSuccessMessage();
    } else {
      hideSuccessMessage();
      focusFirstError();
    }
  });

  // Función para validar todo el formulario.
  function validateForm() {
    const validations = [
      validateCompanyName(),
      validateContactPerson(),
      validateCorporateEmail(),
      validatePhone(),
      validateWebsite(),
      validateOperationCountry(),
      validateProductType(),
      validateMonthlyVolume(),
      validateServicesInterest(),
      validateCurrent3PL(),
      validateComments(),
      validatePrivacyPolicy()
    ];

    updateVolumeWarning();
    return validations.every(valid => valid === true);
  }

  // Validaciones individuales.
  function validateCompanyName() {
    const value = companyName.value.trim();
    const valid = value.length >= 2;
    if (!valid) {
      showError(companyName, 'El nombre de la empresa debe tener al menos 2 caracteres');
      return false;
    }
    clearError(companyName);
    return true;
  }

  function validateContactPerson() {
    const value = contactPerson.value.trim();
    const words = value.split(/\s+/).filter(Boolean);
    const valid = words.length >= 2 && words.every(word => word.length >= 2);
    if (!valid) {
      showError(contactPerson, 'Ingresa nombre y apellido del contacto');
      return false;
    }
    clearError(contactPerson);
    return true;
  }

  function validateCorporateEmail() {
    const value = corporateEmail.value.trim();
    const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    const valid = value !== '' && emailPattern.test(value);
    if (!valid) {
      showError(corporateEmail, 'Ingresa un email corporativo válido (ejemplo: nombre@empresa.com)');
      return false;
    }
    clearError(corporateEmail);
    return true;
  }

  function validatePhone() {
    const value = phone.value.trim();
    const phonePattern = /^\+\d[\d\s().-]{6,}$/;
    const valid = value !== '' && phonePattern.test(value);
    if (!valid) {
      showError(phone, 'El teléfono debe incluir código de país (ejemplo: +1 213 555 0147)');
      return false;
    }
    clearError(phone);
    return true;
  }

  function validateWebsite() {
    const value = website.value.trim();
    if (value === '') {
      clearError(website);
      return true;
    }
    const valid = /^https?:\/\/.+$/i.test(value);
    if (!valid) {
      showError(website, 'Si incluyes sitio web, debe ser una URL válida');
      return false;
    }
    clearError(website);
    return true;
  }

  function validateOperationCountry() {
    const valid = operationCountry.value.trim() !== '';
    if (!valid) {
      showError(operationCountry, 'Selecciona el país de operación principal');
      return false;
    }
    clearError(operationCountry);
    return true;
  }

  function validateProductType() {
    const valid = productType.value.trim() !== '';
    if (!valid) {
      showError(productType, 'Selecciona el tipo de producto que manejas');
      return false;
    }
    clearError(productType);
    return true;
  }

  function validateMonthlyVolume() {
    const valid = monthlyVolume.value.trim() !== '';
    if (!valid) {
      showError(monthlyVolume, 'Selecciona el volumen mensual estimado');
      return false;
    }
    clearError(monthlyVolume);
    return true;
  }

  function validateServicesInterest() {
    const anyChecked = Array.from(servicesInterest).some(checkbox => checkbox.checked);
    if (!anyChecked) {
      showGroupError('error-servicesInterest', servicesInterest, 'Selecciona al menos un servicio de interés');
      return false;
    }
    clearGroupError('error-servicesInterest', servicesInterest);
    return true;
  }

  function validateCurrent3PL() {
    const selected = Array.from(current3PL).some(radio => radio.checked);
    if (!selected) {
      showGroupError('error-current3PL', current3PL, 'Indica si actualmente trabajas con otro proveedor logístico');
      return false;
    }
    clearGroupError('error-current3PL', current3PL);
    return true;
  }

  function validateComments() {
    const value = comments.value;
    const remaining = 500 - value.length;
    if (remaining < 0) {
      showError(comments, `Los comentarios no pueden exceder 500 caracteres (quedan ${remaining})`);
      return false;
    }
    clearError(comments);
    return true;
  }

  function validatePrivacyPolicy() {
    const valid = privacyPolicy.checked;
    if (!valid) {
      showError(privacyPolicy, 'Debes aceptar la política de privacidad para continuar');
      return false;
    }
    clearError(privacyPolicy);
    return true;
  }

  // Actualiza el contador de caracteres para el campo de comentarios.
  function updateCommentCount() {
    const length = comments.value.length;
    charCount.textContent = String(length);
  }

  // Muestra o oculta la advertencia de volumen 0-100.
  function updateVolumeWarning() {
    if (monthlyVolume.value === '0-100') {
      warningNode.classList.remove('hidden');
    } else {
      warningNode.classList.add('hidden');
    }
  }

  // Muestra un error para un campo individual.
  function showError(element, message) {
    const errorContainer = document.getElementById(`error-${element.id}`);
    if (!errorContainer) return;
    errorContainer.textContent = message;
    errorContainer.classList.remove('hidden');
    setInvalidState(element);
  }

  // Limpia el error de un campo individual.
  function clearError(element) {
    const errorContainer = document.getElementById(`error-${element.id}`);
    if (errorContainer) {
      errorContainer.textContent = '';
      errorContainer.classList.add('hidden');
    }
    clearInvalidState(element);
  }

  // Muestra error para grupos de inputs.
  function showGroupError(errorId, inputs, message) {
    const errorContainer = document.getElementById(errorId);
    if (!errorContainer) return;
    errorContainer.textContent = message;
    errorContainer.classList.remove('hidden');
    inputs.forEach(input => setInvalidState(input));
  }

  // Limpia el error para grupos de inputs.
  function clearGroupError(errorId, inputs) {
    const errorContainer = document.getElementById(errorId);
    if (errorContainer) {
      errorContainer.textContent = '';
      errorContainer.classList.add('hidden');
    }
    inputs.forEach(input => clearInvalidState(input));
  }

  // Agrega estilo de campo inválido.
  function setInvalidState(element) {
    element.classList.add('border-red-500', 'ring-2', 'ring-red-500');
  }

  // Elimina estilo de campo inválido.
  function clearInvalidState(element) {
    element.classList.remove('border-red-500', 'ring-2', 'ring-red-500');
  }

  // Borra todos los errores visibles.
  function clearAllErrors() {
    const errorContainers = form.querySelectorAll('[id^="error-"]');
    errorContainers.forEach(container => {
      container.textContent = '';
      container.classList.add('hidden');
    });

    const invalidFields = form.querySelectorAll('.border-red-500');
    invalidFields.forEach(field => {
      field.classList.remove('border-red-500', 'ring-2', 'ring-red-500');
    });
  }

  // Muestra el mensaje de éxito con texto exacto.
  function showSuccessMessage() {
    successText.textContent =
      '¡Gracias por tu interés en TrackFlow!\n\n' +
      'Hemos recibido tu solicitud. Nuestro equipo comercial revisará tu información y te contactará en las próximas 24-48 horas para agendar una llamada y conocer tus necesidades logísticas en detalle.\n\n' +
      'Si tienes alguna consulta urgente, escríbenos directamente a comercial@trackflow.com';
    successMessage.classList.remove('hidden');
    successMessage.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // Oculta el mensaje de éxito.
  function hideSuccessMessage() {
    successMessage.classList.add('hidden');
  }

  // Lleva el foco al primer campo con error.
  function focusFirstError() {
    const firstError = form.querySelector('.border-red-500');
    if (firstError) {
      firstError.focus({ preventScroll: true });
    }
  }
});
