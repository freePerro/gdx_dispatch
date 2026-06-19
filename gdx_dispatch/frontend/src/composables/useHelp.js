import { useHelpStore } from '../stores/help';

export function useHelp() {
  const store = useHelpStore();
  return {
    open: (slug = null) => store.open(slug),
    close: () => store.close(),
    showArticle: (slug, source) => store.showArticle(slug, source),
    isOpen: () => store.isOpen,
    store,
  };
}
