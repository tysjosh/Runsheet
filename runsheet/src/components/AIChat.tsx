"use client";

import { CalendarDays, SendHorizontal, Trash2, X } from "lucide-react";
import type React from "react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import ReportViewer from "./ReportViewer";

interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "tool-indicator";
  content: string;
  timestamp: Date;
  isStreaming?: boolean;
  toolName?: string;
  toolStatus?: "in-progress" | "done";
  isContinuation?: boolean;
}

interface AIChatProps {
  isOpen: boolean;
  onClose: () => void;
}

export default function AIChat({ isOpen, onClose }: AIChatProps) {
  // Custom scrollbar styles
  const scrollbarStyles = `
    .custom-scrollbar::-webkit-scrollbar {
      width: 6px;
    }
    .custom-scrollbar::-webkit-scrollbar-track {
      background: transparent;
    }
    .custom-scrollbar::-webkit-scrollbar-thumb {
      background-color: #d1d5db;
      border-radius: 3px;
    }
    .custom-scrollbar::-webkit-scrollbar-thumb:hover {
      background-color: #9ca3af;
    }
  `;
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: "1",
      role: "assistant",
      content:
        "Hello! I'm your logistics AI assistant. I can help you search orders, track fleet status, analyze delays, and answer questions about your operations. What would you like to know?",
      timestamp: new Date(),
    },
  ]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [_toolStatus, setToolStatus] = useState<string>("");
  const [reportContent, setReportContent] = useState<string | null>(null);
  const [showDatePicker, setShowDatePicker] = useState(false);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const processingRef = useRef(false);

  const isReport = (content: string) => {
    // Match any structured report: has a markdown H1 header and at least one H2 section
    if (!content.includes("##")) return false;
    const hasReportHeader = /^# .*(Report|Analysis)/m.test(content);
    const hasGeneratedMeta =
      content.includes("Generated") && (content.includes("|") || content.includes("*"));
    const hasReportKeyword =
      /Report\b|Analysis\b|Productivity\b|Violations\b|Dispatch\b|Operations\b/i.test(content);
    return (hasReportHeader || hasGeneratedMeta) && hasReportKeyword;
  };

  const getToolIcon = (toolName?: string) => {
    if (!toolName) return "🔧";

    // Search tools
    if (toolName.startsWith("search_")) return "🔍";

    // Report tools
    if (toolName.startsWith("generate_")) return "📊";

    // Summary tools
    if (
      toolName.includes("summary") ||
      toolName.includes("overview") ||
      toolName.includes("insights")
    )
      return "📈";

    // Lookup tools
    if (toolName.startsWith("find_") || toolName.startsWith("get_all_"))
      return "🔎";

    // Specific tools
    switch (toolName) {
      case "search_fleet_data":
        return "🚛";
      case "search_orders":
        return "📦";
      case "search_support_tickets":
        return "🎫";
      case "search_inventory":
        return "📦";
      case "get_fleet_summary":
        return "🚛";
      case "get_inventory_summary":
        return "📦";
      case "get_analytics_overview":
        return "📊";
      case "get_performance_insights":
        return "🎯";
      case "find_truck_by_id":
        return "🚛";
      case "get_all_locations":
        return "📍";
      case "generate_operations_report":
        return "📋";
      case "generate_performance_report":
        return "📊";
      case "generate_incident_analysis":
        return "🔍";
      default:
        return "🔧";
    }
  };

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  useEffect(() => {
    if (isOpen && inputRef.current) {
      inputRef.current.focus();
    }
  }, [isOpen]);

  const streamChatResponse = async (userMessage: string) => {
    // Requirement 9.4: 120-second timeout for AI streaming responses
    const AI_STREAMING_TIMEOUT = 120000; // 120 seconds
    const controller = new AbortController();
    const timeoutId = setTimeout(
      () => controller.abort(),
      AI_STREAMING_TIMEOUT,
    );

    try {
      const API_BASE_URL =
        process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
      const response = await fetch(`${API_BASE_URL}/chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          message: userMessage,
          mode: "chat",
        }),
        signal: controller.signal,
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error("No response body reader available");
      }

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();

        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        buffer += chunk;
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            try {
              const jsonStr = line.slice(6).trim();
              if (!jsonStr) continue;

              const data = JSON.parse(jsonStr);

              if (data.error) {
                throw new Error(data.error);
              }

              if (data.type === "text" && data.content) {
                setMessages((prev) => {
                  const updated = [...prev];
                  // Find the last streaming assistant message
                  const lastStreamingAssistantIndex = updated.findLastIndex(
                    (msg) => msg.role === "assistant" && msg.isStreaming,
                  );
                  if (lastStreamingAssistantIndex !== -1) {
                    updated[lastStreamingAssistantIndex].content +=
                      data.content;
                  }
                  return updated;
                });
              }

              if (data.type === "tool" && data.tool_name) {
                // Tool is being used - split the assistant message and add tool indicator
                setMessages((prev) => {
                  const updated = [...prev];
                  const lastAssistantIndex = updated.findLastIndex(
                    (msg) => msg.role === "assistant",
                  );

                  if (
                    lastAssistantIndex !== -1 &&
                    updated[lastAssistantIndex].isStreaming
                  ) {
                    // Stop streaming on the current assistant message
                    updated[lastAssistantIndex].isStreaming = false;

                    // Add tool indicator
                    updated.push({
                      id: `tool-${Date.now()}`,
                      role: "tool-indicator",
                      content: "",
                      timestamp: new Date(),
                      toolName: data.tool_name,
                      toolStatus: "in-progress",
                    });

                    // Add a new assistant message for post-tool content
                    updated.push({
                      id: `assistant-${Date.now()}`,
                      role: "assistant",
                      content: "",
                      timestamp: new Date(),
                      isStreaming: true,
                      isContinuation: true,
                    });
                  }
                  return updated;
                });
              }

              if (data.type === "tool_result") {
                // Tool finished - update the indicator
                setMessages((prev) => {
                  const updated = [...prev];
                  const toolIndicatorIndex = updated.findIndex(
                    (msg) =>
                      msg.role === "tool-indicator" &&
                      msg.toolStatus === "in-progress",
                  );

                  if (toolIndicatorIndex !== -1) {
                    updated[toolIndicatorIndex].toolStatus = "done";
                    // Remove the tool indicator after a short delay
                    setTimeout(() => {
                      setMessages((prevMsgs) =>
                        prevMsgs.filter(
                          (msg) => msg.id !== updated[toolIndicatorIndex].id,
                        ),
                      );
                    }, 500);
                  }
                  return updated;
                });
              }

              if (data.type === "done") {
                return;
              }
            } catch (parseError) {
              console.warn("Failed to parse streaming data:", parseError);
            }
          }
        }
      }
    } catch (error) {
      console.error("Chat streaming error:", error);
      setMessages((prev) => {
        const updated = [...prev];
        const lastMsg = updated[updated.length - 1];
        if (lastMsg.role === "assistant") {
          // Handle timeout error specifically
          if (error instanceof Error && error.name === "AbortError") {
            lastMsg.content =
              "⏱️ The request timed out after 120 seconds. The AI service may be experiencing high load. Please try again with a simpler query.";
          } else {
            lastMsg.content = `❌ Sorry, I encountered an error connecting to the AI service. Please make sure the backend is running on port 8000.\n\nError: ${error instanceof Error ? error.message : "Unknown error"}`;
          }
        }
        return updated;
      });
    } finally {
      clearTimeout(timeoutId);
    }
  };

  const handleSend = async () => {
    if (!input.trim() || isStreaming || processingRef.current) return;
    await sendMessage(input);
  };

  const sendMessage = async (text: string) => {
    if (!text.trim() || isStreaming || processingRef.current) return;

    processingRef.current = true;
    setIsStreaming(true);

    // Append date range context if dates are selected
    let messageText = text;
    if (startDate && endDate) {
      messageText = `${text} (from ${startDate} to ${endDate})`;
    } else if (startDate) {
      messageText = `${text} (from ${startDate})`;
    } else if (endDate) {
      messageText = `${text} (until ${endDate})`;
    }

    const userMessage: ChatMessage = {
      id: Date.now().toString(),
      role: "user",
      content: messageText,
      timestamp: new Date(),
    };

    setMessages((prev) => [...prev, userMessage]);
    setInput("");

    const assistantMessage: ChatMessage = {
      id: (Date.now() + 1).toString(),
      role: "assistant",
      content: "",
      timestamp: new Date(),
      isStreaming: true,
    };

    setMessages((prev) => [...prev, assistantMessage]);

    try {
      await streamChatResponse(userMessage.content);
    } catch (error) {
      console.error("Chat error:", error);
      setMessages((prev) => {
        const updated = [...prev];
        const lastMsg = updated[updated.length - 1];
        if (lastMsg.role === "assistant") {
          lastMsg.content =
            "❌ Sorry, I encountered an error. Please try again.";
        }
        return updated;
      });
    } finally {
      setIsStreaming(false);
      setToolStatus("");
      setMessages((prev) => {
        const updated = [...prev];
        const lastMsg = updated[updated.length - 1];
        if (lastMsg.role === "assistant") {
          lastMsg.isStreaming = false;
        }
        return updated;
      });
      processingRef.current = false;
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const clearChat = async () => {
    try {
      const API_BASE_URL =
        process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
      const response = await fetch(`${API_BASE_URL}/chat/clear`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({}),
      });

      if (response.ok) {
        setMessages([
          {
            id: "1",
            role: "assistant",
            content:
              "Chat cleared! How can I help you with your logistics operations?",
            timestamp: new Date(),
          },
        ]);
      } else {
        console.error("Failed to clear chat on backend");
      }
    } catch (error) {
      console.error("Error clearing chat:", error);
      setMessages([
        {
          id: "1",
          role: "assistant",
          content:
            "Chat cleared! How can I help you with your logistics operations?",
          timestamp: new Date(),
        },
      ]);
    }
  };

  return (
    <>
      <style dangerouslySetInnerHTML={{ __html: scrollbarStyles }} />
      <div
        className={`fixed top-0 right-0 h-full w-96 bg-gradient-to-br from-gray-50 to-gray-100 shadow-2xl z-50 flex flex-col transition-transform duration-300 ease-in-out ${
          isOpen ? "translate-x-0" : "translate-x-full"
        }`}
      >
        {/* Header */}
        <div className="bg-white/80 backdrop-blur-sm border-b border-gray-200/50 p-4 flex-shrink-0">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center space-x-2">
              <img
                src="/assistant.svg"
                alt="Support Assistant"
                className="w-6 h-6"
              />
              <div>
                <h2 className="text-lg font-bold text-gray-900">Support</h2>
                <p className="text-xs text-gray-500 mt-0.5">
                  Powered by Gemini
                </p>
              </div>
            </div>
            <div className="flex items-center space-x-2">
              <button
                onClick={clearChat}
                className="text-gray-400 hover:text-red-500 p-2 rounded-lg hover:bg-red-50 transition-all duration-200"
                title="Clear chat"
              >
                <Trash2 className="w-4 h-4" />
              </button>
              <button
                onClick={onClose}
                className="text-gray-400 hover:text-gray-700 p-2 rounded-lg hover:bg-gray-100 transition-all duration-200"
                title="Close chat"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          </div>

          {/* Mode Toggle removed — single unified mode */}
        </div>

        {/* Messages */}
        <div
          className="flex-1 overflow-y-auto p-4 space-y-4 min-h-0 custom-scrollbar"
          style={{
            scrollbarWidth: "thin",
            scrollbarColor: "#d1d5db transparent",
          }}
        >
          {messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
            >
              {msg.role === "tool-indicator" ? (
                <div className="max-w-[85%] my-1">
                  <span
                    className="inline-block px-2 py-1 text-xs text-white rounded border"
                    style={{
                      backgroundColor: "#232323",
                      borderColor: "#232323",
                    }}
                  >
                    {getToolIcon(msg.toolName)} {msg.toolName || "tool"}
                  </span>
                </div>
              ) : msg.role === "assistant" ? (
                <div className="max-w-[85%]">
                  <div className="text-sm text-gray-800 leading-relaxed prose prose-sm max-w-none prose-headings:text-gray-900 prose-p:text-gray-800 prose-strong:text-gray-900 prose-ul:text-gray-800 prose-li:text-gray-800 prose-table:text-xs prose-th:bg-gray-100 prose-th:px-2 prose-th:py-1 prose-td:px-2 prose-td:py-1 prose-table:border-collapse">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        strong: ({ children, ...props }) => {
                          const text = typeof children === "string"
                            ? children
                            : Array.isArray(children)
                              ? children.map((c: any) => (typeof c === "string" ? c : "")).join("")
                              : String(children || "");
                          const trimmed = text.trim();
                          const isClickable = !msg.isStreaming && trimmed.length > 5 && trimmed.length < 80 && (
                            /report|analysis|summary|status|overview|productivity|violations|dispatch/i.test(trimmed)
                          );
                          if (isClickable) {
                            return (
                              <button
                                type="button"
                                onClick={() => sendMessage(`Generate ${trimmed}`)}
                                className="w-full text-left px-3 py-2 my-1 rounded-lg text-xs font-semibold border border-gray-200 bg-white hover:bg-blue-50 hover:border-blue-300 transition-all cursor-pointer flex items-center gap-2 shadow-sm"
                                style={{ color: "#232323" }}
                              >
                                <span className="text-blue-500 text-sm">▸</span>
                                {trimmed}
                              </button>
                            );
                          }
                          return <strong {...props}>{children}</strong>;
                        },
                        li: ({ children, ...props }) => {
                          const text = typeof children === "string"
                            ? children
                            : Array.isArray(children)
                              ? children.map((c: any) => (typeof c === "string" ? c : c?.props?.children || "")).join("")
                              : "";
                          const trimmed = text.replace(/\*\*/g, "").trim();
                          const isClickable = !msg.isStreaming && trimmed.length > 5 && trimmed.length < 80 && (
                            /report|analysis|summary|status|overview|productivity|violations|dispatch/i.test(trimmed)
                          );
                          if (isClickable) {
                            return (
                              <li {...props} className="list-none -ml-4 my-1">
                                <button
                                  type="button"
                                  onClick={() => sendMessage(`Generate ${trimmed}`)}
                                  className="text-left w-full px-3 py-2 rounded-lg text-xs font-semibold border border-gray-200 bg-white hover:bg-blue-50 hover:border-blue-300 transition-all cursor-pointer flex items-center gap-2 shadow-sm"
                                  style={{ color: "#232323" }}
                                >
                                  <span className="text-blue-500 text-sm">▸</span>
                                  {trimmed}
                                </button>
                              </li>
                            );
                          }
                          return <li {...props}>{children}</li>;
                        },
                        table: ({ children, ...props }) => (
                          <div className="overflow-x-auto my-2">
                            <table {...props} className="w-full text-xs border border-gray-200 rounded">{children}</table>
                          </div>
                        ),
                        th: ({ children, ...props }) => (
                          <th {...props} className="bg-gray-100 text-left px-2 py-1.5 border-b border-gray-200 font-semibold text-gray-700">{children}</th>
                        ),
                        td: ({ children, ...props }) => (
                          <td {...props} className="px-2 py-1 border-b border-gray-100 text-gray-600">{children}</td>
                        ),
                      }}
                    >
                      {msg.content}
                    </ReactMarkdown>
                    {msg.isStreaming && (
                      <span
                        className="inline-block w-1.5 h-4 ml-1 animate-pulse rounded"
                        style={{ backgroundColor: "#232323" }}
                      />
                    )}
                  </div>
                  {isReport(msg.content) && !msg.isStreaming && (
                    <div className="mt-3 flex gap-2">
                      <button
                        onClick={() => setReportContent(msg.content)}
                        className="px-3 py-1.5 text-xs bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors flex items-center gap-1"
                      >
                        📊 View Report
                      </button>
                    </div>
                  )}
                </div>
              ) : (
                <div className="max-w-[75%]">
                  <div
                    className="text-white rounded-2xl px-4 py-2.5 shadow-lg"
                    style={{ backgroundColor: "#232323" }}
                  >
                    <div className="whitespace-pre-wrap text-sm leading-relaxed">
                      {msg.content}
                    </div>
                  </div>
                </div>
              )}
            </div>
          ))}

          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="bg-white/80 backdrop-blur-sm border-t border-gray-200/50 p-4 flex-shrink-0">
          {/* Date Range Picker */}
          {showDatePicker && (
            <div className="mb-3 p-3 bg-white rounded-xl border border-gray-200 shadow-sm">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-semibold text-gray-600">Date Range</span>
                {(startDate || endDate) && (
                  <button
                    onClick={() => { setStartDate(""); setEndDate(""); }}
                    className="text-xs text-red-500 hover:text-red-600 transition-colors"
                  >
                    Clear
                  </button>
                )}
              </div>
              {/* Quick presets */}
              <div className="flex gap-1.5 mb-2">
                {[
                  { label: "7d", days: 7 },
                  { label: "14d", days: 14 },
                  { label: "30d", days: 30 },
                  { label: "90d", days: 90 },
                ].map(({ label, days }) => {
                  const end = new Date();
                  const start = new Date();
                  start.setDate(end.getDate() - days);
                  const startISO = start.toISOString().split("T")[0];
                  const endISO = end.toISOString().split("T")[0];
                  const isActive = startDate === startISO && endDate === endISO;
                  return (
                    <button
                      key={label}
                      onClick={() => { setStartDate(startISO); setEndDate(endISO); }}
                      className={`px-2.5 py-1 text-[10px] font-medium rounded-md border transition-all ${
                        isActive
                          ? "bg-gray-800 text-white border-gray-800"
                          : "bg-gray-50 text-gray-600 border-gray-200 hover:bg-gray-100"
                      }`}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
              <div className="flex items-center gap-2">
                <div className="flex-1">
                  <label className="block text-[10px] text-gray-400 mb-0.5">From</label>
                  <input
                    type="date"
                    value={startDate}
                    onChange={(e) => setStartDate(e.target.value)}
                    className="w-full px-2 py-1.5 text-xs border border-gray-200 rounded-lg focus:outline-none focus:border-gray-400 bg-gray-50"
                  />
                </div>
                <span className="text-gray-300 mt-3">→</span>
                <div className="flex-1">
                  <label className="block text-[10px] text-gray-400 mb-0.5">To</label>
                  <input
                    type="date"
                    value={endDate}
                    onChange={(e) => setEndDate(e.target.value)}
                    className="w-full px-2 py-1.5 text-xs border border-gray-200 rounded-lg focus:outline-none focus:border-gray-400 bg-gray-50"
                  />
                </div>
              </div>
              {startDate && endDate && (
                <div className="mt-2 text-[10px] text-gray-500 bg-gray-50 rounded-md px-2 py-1">
                  📅 Reports will use: {startDate} → {endDate}
                </div>
              )}
            </div>
          )}
          <div className="mb-3">
            <div className="relative flex items-center gap-2">
              <button
                onClick={() => setShowDatePicker(!showDatePicker)}
                className={`p-2.5 rounded-xl border-2 transition-all duration-200 flex-shrink-0 ${
                  showDatePicker || (startDate && endDate)
                    ? "border-blue-400 bg-blue-50 text-blue-600"
                    : "border-gray-200 bg-white text-gray-400 hover:text-gray-600 hover:border-gray-300"
                }`}
                title={startDate && endDate ? `${startDate} → ${endDate}` : "Set date range"}
              >
                <CalendarDays className="w-4 h-4" />
                {startDate && endDate && (
                  <span className="absolute -top-1 -right-1 w-2 h-2 bg-blue-500 rounded-full" />
                )}
              </button>
              <div className="relative flex-1">
                <input
                  ref={inputRef}
                  type="text"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyPress={handleKeyPress}
                  placeholder={
                    startDate && endDate
                      ? `Ask about ${startDate} to ${endDate}...`
                      : "Ask me anything about your operations..."
                  }
                  disabled={isStreaming}
                  className="w-full px-4 py-3 pr-12 bg-white border-2 border-gray-200 rounded-2xl focus:outline-none focus:border-gray-400 focus:ring-2 focus:ring-gray-100 text-sm transition-all duration-200 disabled:bg-gray-100 disabled:cursor-not-allowed shadow-sm"
                />
                <button
                  onClick={handleSend}
                  disabled={isStreaming || !input.trim()}
                  className="absolute right-2 top-1/2 transform -translate-y-1/2 p-2 text-gray-600 hover:text-gray-800 disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200"
                >
                  {isStreaming ? (
                    <div className="w-4 h-4 border-2 border-gray-600 border-t-transparent rounded-full animate-spin"></div>
                  ) : (
                    <SendHorizontal className="w-4 h-4" />
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Report Viewer Modal */}
      {reportContent && (
        <ReportViewer
          content={reportContent}
          onClose={() => setReportContent(null)}
        />
      )}
    </>
  );
}
