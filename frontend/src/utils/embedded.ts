/** True when running inside an iframe (e.g. DCC Hub). Cross-origin access errors count as embedded. */
export function isEmbedded(): boolean {
  try {
    return window.self !== window.top;
  } catch {
    return true;
  }
}
