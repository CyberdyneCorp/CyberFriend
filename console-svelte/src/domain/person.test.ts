import { describe, expect, it } from "vitest";

import { parsePerson } from "./person";

describe("parsePerson", () => {
  it("reads the platform and the account id", () => {
    expect(parsePerson("discord:1234567890")).toEqual({
      platform: "discord",
      id: "1234567890",
    });
  });

  it("refuses anything it cannot read, rather than guessing a request", () => {
    // A guessed platform or id would reverse an opt-out for the wrong person.
    for (const unreadable of ["", "discord", ":123", "discord:", "discord:abc"]) {
      expect(parsePerson(unreadable)).toBeNull();
    }
  });
});
