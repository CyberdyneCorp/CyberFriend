/**
 * Reading a person back out of `platform:id`.
 *
 * The opt-out list returns a person as one string, and the endpoint that
 * reverses an opt-out takes the platform and the id as separate path
 * segments. Splitting on the *last* colon rather than the first is the whole
 * of this module: a platform name cannot contain one (the audit reserves the
 * character), an id is digits, and a row this cannot parse gets its button
 * disabled with a reason rather than a guessed request.
 */

export interface Person {
  platform: string;
  id: string;
}

export function parsePerson(person: string): Person | null {
  const cut = person.lastIndexOf(":");
  if (cut <= 0) return null;
  const platform = person.slice(0, cut).trim();
  const id = person.slice(cut + 1).trim();
  if (platform === "" || !/^\d+$/.test(id)) return null;
  return { platform, id };
}
