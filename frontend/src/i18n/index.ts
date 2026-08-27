import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import zh from './zh.json';
import zhBuiltinTools from './zh-builtin-tools.json';
import en from './en.json';

const zhBuiltinCategoryLabels = Object.fromEntries(
    Object.entries(zhBuiltinTools.categories).map(([key, value]) => [key, value.label]),
);
const zhBuiltinCategoryDescriptions = Object.fromEntries(
    Object.entries(zhBuiltinTools.categories).map(([key, value]) => [key, value.description]),
);

const zhTranslation = {
    ...zh,
    agent: {
        ...zh.agent,
        toolCategories: {
            ...zh.agent.toolCategories,
            ...zhBuiltinCategoryLabels,
        },
        toolCategoryDescriptions: {
            ...zh.agent.toolCategoryDescriptions,
            ...zhBuiltinCategoryDescriptions,
        },
        toolTranslations: {
            ...zh.agent.toolTranslations,
            ...zhBuiltinTools.tools,
        },
    },
};

const syncDocumentLanguage = (language?: string) => {
    if (typeof document === 'undefined') return;
    document.documentElement.lang = language?.toLowerCase().startsWith('zh') ? 'zh-CN' : 'en';
};

i18n.on('languageChanged', syncDocumentLanguage);

void i18n
    .use(LanguageDetector)
    .use(initReactI18next)
    .init({
        resources: {
            zh: { translation: zhTranslation },
            en: { translation: en },
        },
        fallbackLng: 'en',
        interpolation: { escapeValue: false },
        supportedLngs: ['en', 'zh'],
        nonExplicitSupportedLngs: true,
        detection: {
            order: ['localStorage', 'navigator'],
            caches: ['localStorage'],
            convertDetectedLanguage: (lng) => {
                if (lng.startsWith('zh')) return 'zh';
                if (lng.startsWith('en')) return 'en';
                return lng;
            },
        },
    })
    .then(() => syncDocumentLanguage(i18n.resolvedLanguage || i18n.language));

export default i18n;
