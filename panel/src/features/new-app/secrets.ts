/**
 * Secrets generated in the browser, from the platform's cryptographic random source. Never
 * Math.random: a session key guessed from a PRNG's state is a session key anyone can forge.
 */

/**
 * A random secret, URL-safe base64 without padding: 32 bytes make 43 characters, the size
 * frameworks ask for (NEXTAUTH_SECRET, APP_KEY, SECRET_KEY_BASE are 32 bytes or more).
 */
export function generateSecret(
  bytes = 32,
  random: (array: Uint8Array<ArrayBuffer>) => Uint8Array = (array) => crypto.getRandomValues(array),
): string {
  const buffer = random(new Uint8Array(bytes));
  let binary = "";
  for (const byte of buffer) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
