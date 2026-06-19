import { ref, computed } from 'vue';
import en from './en';
import es from './es';

const STORAGE_KEY = 'gdx_locale';
const messages = { en, es };
const currentLocale = ref(localStorage.getItem(STORAGE_KEY) || 'en');

function setLocale(locale) {
  if (messages[locale]) {
    currentLocale.value = locale;
    localStorage.setItem(STORAGE_KEY, locale);
    document.documentElement.lang = locale;
  }
}

function t(key) {
  const keys = key.split('.');
  let result = messages[currentLocale.value];
  for (const k of keys) {
    if (result && typeof result === 'object' && k in result) {
      result = result[k];
    } else {
      // Fallback to English
      result = messages.en;
      for (const fk of keys) {
        if (result && typeof result === 'object' && fk in result) {
          result = result[fk];
        } else {
          return key; // Return key if not found in any language
        }
      }
      return result;
    }
  }
  return result;
}

const availableLocales = computed(() => Object.keys(messages));

export function useI18n() {
  return {
    t,
    locale: currentLocale,
    setLocale,
    availableLocales,
  };
}
