import {
  clearSessionRecovery,
  readSessionRecovery,
  writeSessionRecovery,
} from "./sessionRecovery";

describe("DataPilot same-tab session recovery", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("round-trips the opaque current session view", () => {
    writeSessionRecovery({
      sessionId: "session-1",
      mode: "active_session",
    });

    expect(readSessionRecovery()).toEqual({
      sessionId: "session-1",
      mode: "active_session",
    });
  });

  it("clears the pointer when the user enters a new-session draft", () => {
    writeSessionRecovery({
      sessionId: "session-1",
      mode: "history_session",
    });
    clearSessionRecovery();

    expect(readSessionRecovery()).toBeNull();
  });

  it.each([
    "not-json",
    JSON.stringify({ sessionId: "", mode: "active_session" }),
    JSON.stringify({ sessionId: "session-1", mode: "draft_new_session" }),
  ])("fails closed for invalid stored state", (encoded) => {
    window.sessionStorage.setItem("datapilot.session-view.v1", encoded);

    expect(readSessionRecovery()).toBeNull();
  });
});
