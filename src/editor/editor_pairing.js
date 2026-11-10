/* Use Monaco's native editing transactions for language-aware pairs. */
(() => {
  window.setEditorAutoPairs = enabled => {
    window.__mcuEditorAutoPairs = enabled === true;
    window.editorInstance?.updateOptions({
      autoClosingBrackets: enabled ? 'languageDefined' : 'never',
      autoClosingQuotes: enabled ? 'languageDefined' : 'never',
      autoSurround: enabled ? 'languageDefined' : 'never',
      autoClosingDelete: enabled ? 'auto' : 'never',
      autoClosingOvertype: enabled ? 'auto' : 'never'
    });
  };
  // bundle.js constructs Monaco first in this same event dispatch. Apply the
  // preference before user input or asynchronous source loading can resume.
  const apply = () => window.setEditorAutoPairs(window.__mcuInitialAutoPairs !== false);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', apply);
  else apply();
})();
