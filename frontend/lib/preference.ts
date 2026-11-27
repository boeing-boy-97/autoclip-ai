/** Storage is optional: blocked cookies/storage must not invalidate a valid API session. */
export function readPreference(key: string): string | null {
  try { return window.localStorage.getItem(key); } catch { return null; }
}
export function writePreference(key: string, value: string): void {
  try { window.localStorage.setItem(key, value); } catch { /* private/storage-disabled browser */ }
}
