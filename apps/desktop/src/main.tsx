// NEXUS AI desktop shell (Tauri 2 + React).
// Phase 1: shell scaffold. The Python agent service in services/agent is the
// working core; this UI connects to it over HTTP on 127.0.0.1 in Phase 2.
import React, { useState } from "react";
import { createRoot } from "react-dom/client";

function Orb() {
  return (
    <div
      aria-label="NEXUS status orb"
      style={{
        width: 120, height: 120, borderRadius: "50%",
        background: "radial-gradient(circle at 35% 35%, #67e8f9, #3b82f6 45%, #7c3aed 80%)",
        boxShadow: "0 0 60px rgba(59,130,246,.45)",
      }}
    />
  );
}

function App() {
  const [messages, setMessages] = useState<string[]>([]);
  const [input, setInput] = useState("");
  const send = () => {
    if (!input.trim()) return;
    setMessages((m) => [...m, "you: " + input]);
    setInput("");
    // Phase 2: POST to the agent service and stream the reply.
    setMessages((m) => [...m, "nexus: (agent service not connected yet — Phase 2)"]);
  };
  return (
    <div style={{ background: "#111318", color: "#e5e7eb", minHeight: "100vh",
                  display: "flex", fontFamily: "system-ui" }}>
      <aside style={{ width: 220, borderRight: "1px solid #262b36", padding: 16 }}>
        <h2 style={{ fontSize: 14, letterSpacing: 2 }}>NEXUS AI</h2>
        {["New chat", "Memory", "Workflows", "Settings"].map((x) => (
          <div key={x} style={{ padding: "8px 0", color: "#9ca3af" }}>{x}</div>
        ))}
      </aside>
      <main style={{ flex: 1, display: "flex", flexDirection: "column",
                     alignItems: "center", padding: 24 }}>
        <Orb />
        <div style={{ width: "100%", maxWidth: 720, marginTop: 24, flex: 1 }}>
          {messages.map((m, i) => <div key={i} style={{ padding: 6 }}>{m}</div>)}
        </div>
        <div style={{ display: "flex", width: "100%", maxWidth: 720, gap: 8 }}>
          <input value={input} onChange={(e) => setInput(e.target.value)}
                 onKeyDown={(e) => e.key === "Enter" && send()}
                 placeholder="Ask NEXUS…" style={{ flex: 1, padding: 10,
                   background: "#1a1e27", border: "1px solid #2b3242",
                   borderRadius: 8, color: "#e5e7eb" }} />
          <button onClick={send} style={{ padding: "10px 18px", borderRadius: 8,
                   background: "#3b82f6", color: "white", border: 0 }}>Send</button>
        </div>
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
