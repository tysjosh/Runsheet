/**
 * Unit tests for the `/api/chat` stream parser and message reducer.
 *
 * Staging finding F3: both chat clients threw on `data.error` inside their
 * per-line `try`, so the parse `catch` swallowed it and the user saw an empty
 * or half-finished answer. An error event must now become a visible error
 * message carrying the server's safe text and request id.
 */

import * as chatStream from "./chatStream";
import {
  applyChatStreamEvent,
  type ChatStreamEvent,
  type ChatStreamMessage,
  parseSseChunk,
} from "./chatStream";

const streamingAssistant = (): ChatStreamMessage => ({
  id: "a1",
  role: "assistant",
  content: "",
  timestamp: new Date(0),
  isStreaming: true,
});

describe("parseSseChunk", () => {
  it("returns complete events and keeps a partial line for the next chunk", () => {
    const first = parseSseChunk(
      'data: {"type":"status","stage":"routing"}\n\ndata: {"type":"te',
    );
    expect(first.events).toEqual([{ type: "status", stage: "routing" }]);
    expect(first.rest).toBe('data: {"type":"te');

    const second = parseSseChunk(
      `${first.rest}xt","content":"Hel"}\n\ndata: {"type":"done"}\n\n`,
    );
    expect(second.events).toEqual([
      { type: "text", content: "Hel" },
      { type: "done" },
    ]);
    expect(second.rest).toBe("");
  });

  it("ignores blank lines, comments and malformed JSON", () => {
    const warn = jest.spyOn(console, "warn").mockImplementation(() => {});
    const { events } = parseSseChunk(
      ': keep-alive\n\ndata: \ndata: {not json}\ndata: {"type":"done"}\n',
    );
    expect(events).toEqual([{ type: "done" }]);
    warn.mockRestore();
  });

  it("surfaces the legacy {error} shape as an error event", () => {
    const { events } = parseSseChunk('data: {"error":"x"}\n');
    expect(events).toEqual([
      { type: "error", code: "AI_SERVICE_UNAVAILABLE", message: "x" },
    ]);
  });

  it("handles CRLF line endings", () => {
    const { events } = parseSseChunk('data: {"type":"done"}\r\n\r\n');
    expect(events).toEqual([{ type: "done" }]);
  });
});

describe("applyChatStreamEvent", () => {
  it("appends text to the streaming assistant message without mutating input", () => {
    const before = [streamingAssistant()];
    const after = applyChatStreamEvent(before, { type: "text", content: "Hi" });
    expect(after[0].content).toBe("Hi");
    expect(before[0].content).toBe("");
  });

  it("turns an error event into an error message with the request id", () => {
    const messages = applyChatStreamEvent(
      [{ ...streamingAssistant(), content: "partial" }],
      {
        type: "error",
        code: "AI_RATE_LIMITED",
        message: "The AI assistant is receiving too many requests right now.",
        request_id: "req-123",
      },
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].isError).toBe(true);
    expect(messages[0].isStreaming).toBe(false);
    expect(messages[0].content).toBe(
      "The AI assistant is receiving too many requests right now.\n\nReference: req-123",
    );
  });

  it("adds an error message when no assistant message is streaming", () => {
    const messages = applyChatStreamEvent([], {
      type: "error",
      code: "AI_SERVICE_UNAVAILABLE",
      message: "unavailable",
    });
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      role: "assistant",
      content: "unavailable",
      isError: true,
    });
  });

  it("surfaces a legacy {error} line end to end", () => {
    const { events } = parseSseChunk('data: {"error":"backend failed"}\n');
    const messages = events.reduce<ChatStreamMessage[]>(
      (acc, event) => applyChatStreamEvent(acc, event),
      [streamingAssistant()],
    );
    expect(messages[0].isError).toBe(true);
    expect(messages[0].content).toBe("backend failed");
  });

  it("splits the answer around a tool call and marks the tool done", () => {
    let messages = applyChatStreamEvent([streamingAssistant()], {
      type: "tool",
      tool_name: "search_fleet_data",
    });
    expect(messages.map((m) => m.role)).toEqual([
      "assistant",
      "tool-indicator",
      "assistant",
    ]);
    expect(messages[0].isStreaming).toBe(false);
    expect(messages[1]).toMatchObject({
      toolName: "search_fleet_data",
      toolStatus: "in-progress",
    });
    expect(messages[2]).toMatchObject({
      isStreaming: true,
      isContinuation: true,
    });
    expect(messages[1].id).not.toBe(messages[2].id);

    messages = applyChatStreamEvent(messages, {
      type: "tool_result",
      tool_name: "search_fleet_data",
      status: "success",
    });
    expect(messages[1].toolStatus).toBe("done");

    messages = applyChatStreamEvent(messages, { type: "text", content: "3" });
    expect(messages[2].content).toBe("3");
  });

  // Staging finding N4: one specialist failing must not wipe the answer.
  it("keeps the answer on a partial error and streams later text after it", () => {
    const events: ChatStreamEvent[] = [
      { type: "text", content: "12 trucks" },
      {
        type: "error",
        code: "AI_RATE_LIMITED",
        message: "The fuel assistant couldn't answer this part.",
        request_id: "req-123",
        partial: true,
        specialist: "fuel",
      },
      { type: "text", content: "more" },
      { type: "done" },
    ];
    const messages = events.reduce<ChatStreamMessage[]>(
      (acc, event) => applyChatStreamEvent(acc, event),
      [streamingAssistant()],
    );

    expect(messages).toHaveLength(3);
    expect(messages[0]).toMatchObject({
      content: "12 trucks",
      isStreaming: false,
    });
    expect(messages[0].isError).toBeFalsy();
    expect(messages[1]).toMatchObject({
      role: "assistant",
      isError: true,
      isStreaming: false,
      content:
        "The fuel assistant couldn't answer this part.\n\nReference: req-123",
    });
    expect(messages[2]).toMatchObject({
      role: "assistant",
      content: "more",
      isStreaming: false,
      isContinuation: true,
    });
  });

  it("still replaces the streaming message on a non-partial error", () => {
    const messages = applyChatStreamEvent(
      [{ ...streamingAssistant(), content: "half" }],
      { type: "error", code: "AI_SERVICE_UNAVAILABLE", message: "down" },
    );
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      content: "down",
      isError: true,
      isStreaming: false,
    });
  });

  it("stops streaming on done and ignores status events", () => {
    const start = [streamingAssistant()];
    expect(
      applyChatStreamEvent(start, { type: "status", stage: "routing" }),
    ).toBe(start);
    const done = applyChatStreamEvent(start, { type: "done" });
    expect(done[0].isStreaming).toBe(false);
  });
});

describe("isTerminalChatEvent", () => {
  // Read through the namespace so the test fails on an assertion, not an
  // import error, when the export is missing.
  const isTerminal = (event: ChatStreamEvent): unknown => {
    const fn = (chatStream as Record<string, unknown>).isTerminalChatEvent;
    return typeof fn === "function" ? fn(event) : undefined;
  };

  it("is true for done and a non-partial error", () => {
    expect(isTerminal({ type: "done" })).toBe(true);
    expect(isTerminal({ type: "error", code: "X", message: "m" })).toBe(true);
  });

  it("is false for a partial error and for text", () => {
    expect(
      isTerminal({ type: "error", code: "X", message: "m", partial: true }),
    ).toBe(false);
    expect(isTerminal({ type: "text", content: "x" })).toBe(false);
  });
});
