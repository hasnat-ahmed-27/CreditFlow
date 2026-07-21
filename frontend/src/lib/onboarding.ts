/**
 * "Has this user been through the Create-or-Join screen?"
 *
 * A pure UI preference — not a credential — so localStorage is the right home
 * for it even though tokens deliberately are not (see lib/api/client.ts). The
 * worst case if it's cleared is that the screen is offered once more.
 *
 * Keyed per user so a shared browser doesn't skip onboarding for whoever signs
 * in next.
 */
const PREFIX = "creditflow.onboarded.";

export function hasOnboarded(userId: string | null | undefined): boolean {
  if (!userId) return true; // nobody signed in — nothing to prompt
  try {
    return localStorage.getItem(PREFIX + userId) === "1";
  } catch {
    return true; // storage disabled: never trap the user on onboarding
  }
}

export function markOnboarded(userId: string | null | undefined): void {
  if (!userId) return;
  try {
    localStorage.setItem(PREFIX + userId, "1");
  } catch {
    // Private mode / storage full — harmless, they see the screen again.
  }
}
