/* Small localization foundation. Disease/pest names and scientific terms stay untouched. */
(() => {
  const dictionaries = {
    English: {},
    Marathi: {
      Home: 'मुख्यपृष्ठ', Dashboard: 'आढावा', Crops: 'पिके', Scan: 'तपासणी', History: 'इतिहास', Alerts: 'सूचना', Profile: 'प्रोफाइल',
      'Expert cases': 'तज्ज्ञ प्रकरणे', Administration: 'प्रशासन', 'Log in': 'लॉग इन', 'Create account': 'खाते तयार करा', 'Log out': 'लॉग आउट',
      'Detect Crop Diseases Early. Protect Your Harvest.': 'पिकांचे रोग लवकर ओळखा. आपले पीक सुरक्षित ठेवा.',
      'Key Features': 'मुख्य वैशिष्ट्ये', 'How CropGuard Works': 'CropGuard कसे कार्य करते'
    },
    Tamil: {
      Home: 'முகப்பு', Dashboard: 'கண்ணோட்டம்', Crops: 'பயிர்கள்', Scan: 'பரிசோதனை', History: 'வரலாறு', Alerts: 'எச்சரிக்கைகள்', Profile: 'சுயவிவரம்',
      'Expert cases': 'நிபுணர் வழக்குகள்', Administration: 'நிர்வாகம்', 'Log in': 'உள்நுழை', 'Create account': 'கணக்கை உருவாக்கு', 'Log out': 'வெளியேறு',
      'Detect Crop Diseases Early. Protect Your Harvest.': 'பயிர் நோய்களை முன்கூட்டியே கண்டறிந்து அறுவடையைப் பாதுகாக்கவும்.',
      'Key Features': 'முக்கிய அம்சங்கள்', 'How CropGuard Works': 'CropGuard செயல்முறை'
    }
  };
  const dictionary = (language) => dictionaries[language] || dictionaries.English;
  function apply(language) {
    const selected = dictionaries[language] ? language : 'English';
    localStorage.setItem('cropguard.language', selected);
    document.documentElement.lang = selected === 'Marathi' ? 'mr' : selected === 'Tamil' ? 'ta' : 'en';
    document.querySelectorAll('[data-i18n]').forEach((node) => {
      const original = node.dataset.i18n;
      node.textContent = dictionary(selected)[original] || original;
    });
  }
  window.CropGuardI18n = { apply, selected: () => localStorage.getItem('cropguard.language') || 'English' };
  document.addEventListener('DOMContentLoaded', () => apply(window.CropGuardI18n.selected()));
})();
