import React from "react";
import { useCurrentFrame } from "remotion";
import { Arrow, Capture, Chip, Heading, INOUT, Mono, lerp, prog, useCue } from "../lib";
import { C, MONO } from "../theme";

const DOORS = [
  { label: "serve.sh  :8080", to: 0 },
  { label: "chat.html → :8080", to: 0 },
  { label: "jev_triage.py", to: 1 },
  { label: "score_worker.py jev", to: 2 },
  { label: "score_worker.py lm", to: 3 },
];
const OLD_MODELS = [
  { label: "Qwen3-Coder · 17 GB", color: C.llm },
  { label: "OpenJev 4B · 9 GB", color: C.nli },
  { label: "Jev-Style 2B · 3 GB", color: C.decision },
  { label: "Qwen3-Coder, private copy · 17 GB", color: C.accent },
];
const BACKENDS = [
  { label: "chat models ×3", color: C.llm },
  { label: "decision", color: C.decision },
  { label: "entailment (NLI)", color: C.nli },
  { label: "speech out ×3", color: C.speech },
  { label: "speech in", color: C.speech },
];
const GW = { x: 760, y: 400, w: 400, h: 190 };

/** Many doors, each with its own model copy, collapse into one front door. */
export const Door: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  const merge = prog(f, cue("merge"), 34, INOUT);
  const gw = prog(f, cue("merge") + 18, 22);
  const shot = prog(f, cue("browser") + 4, 18) * (1 - prog(f, cue("openai") - 8, 16, INOUT));
  const shot2 = prog(f, Math.round((cue("browser") + cue("openai")) / 2), 14, INOUT);

  const doorY = (i: number) => 230 + i * 110;
  const modelY = (i: number) => 260 + i * 130;
  const clients = [
    { label: "Claude Code", via: "MCP  /mcp", at: cue("claude"), y: 250 },
    { label: "Browser", via: "chat /  ·  admin /admin", at: cue("browser"), y: 470 },
    { label: "any OpenAI client", via: "/v1/chat/completions", at: cue("openai"), y: 690 },
  ];
  return (
    <>
      <div style={{ position: "absolute", left: 160, top: 100, opacity: 1 - merge }}>
        <Heading size={48} color={C.muted}>Before: a door per script</Heading>
      </div>
      <div style={{ position: "absolute", left: 160, top: 100, opacity: prog(f, cue("merge") + 20, 20) }}>
        <Heading size={48}>Now: one front door</Heading>
      </div>

      {/* the old world */}
      {DOORS.map((d, i) => {
        const a = prog(f, 6 + i * 6, 16);
        const x = lerp(260, GW.x + 40, merge), y = lerp(doorY(i), GW.y + GW.h / 2 - 25, merge);
        const m = OLD_MODELS[d.to];
        const show = d.to === 3 ? prog(f, cue("copy"), 16) : a;
        return (
          <React.Fragment key={d.label}>
            <Arrow x1={x + 340} y1={y + 25} x2={1250} y2={modelY(d.to) + 25} color={d.to === 3 ? C.accent : C.faint} width={2}
                   p={show * (1 - merge * 3)} bend={(i - 2) * 40} />
            <div style={{ position: "absolute", left: x, top: y, opacity: a * (1 - merge), transform: `scale(${1 - merge * 0.4})` }}>
              <Chip mono size={22} style={{ width: 300 }}>{d.label}</Chip>
            </div>
          </React.Fragment>
        );
      })}
      {OLD_MODELS.map((m, i) => (
        <div key={m.label} style={{ position: "absolute", left: 1250, top: modelY(i), opacity: (i === 3 ? prog(f, cue("copy"), 16) : prog(f, 20 + i * 6, 16)) * (1 - merge) }}>
          <Chip color={m.color} size={24}>{m.label}</Chip>
        </div>
      ))}

      {/* the gateway */}
      {gw > 0 && (
        <>
          <div style={{ position: "absolute", left: GW.x, top: GW.y, width: GW.w, height: GW.h, borderRadius: 18, border: `3px solid ${C.accent}`,
                        background: C.panel, opacity: gw, transform: `scale(${0.85 + 0.15 * gw})`, display: "flex", flexDirection: "column",
                        alignItems: "center", justifyContent: "center", gap: 8, boxShadow: `0 0 60px ${C.accentSoft}` }}>
            <div style={{ fontFamily: MONO, fontSize: 38, color: C.ink, fontWeight: 600 }}>127.0.0.1:8090</div>
            <Mono size={22}>the gateway</Mono>
          </div>
          {BACKENDS.map((b, i) => {
            const p = prog(f, cue("merge") + 30 + i * 4, 16);
            const y = 300 + i * 92;
            return (
              <React.Fragment key={b.label}>
                <Arrow x1={GW.x + GW.w + 6} y1={GW.y + GW.h / 2} x2={1390} y2={y + 26} p={p} width={2} />
                <div style={{ position: "absolute", left: 1400, top: y, opacity: p }}><Chip color={b.color}>{b.label}</Chip></div>
              </React.Fragment>
            );
          })}
        </>
      )}
      {clients.map(c => {
        const p = prog(f, c.at, 18);
        return p > 0 ? (
          <React.Fragment key={c.label}>
            <Arrow x1={470} y1={c.y + 28} x2={GW.x - 8} y2={GW.y + GW.h / 2} p={prog(f, c.at + 8, 18)} color={C.ink} width={3} />
            <div style={{ position: "absolute", left: 160, top: c.y, opacity: p, transform: `translateX(${(1 - p) * -30}px)` }}>
              <Chip size={28} color={C.ink}>{c.label}</Chip>
              <div style={{ marginTop: 8 }}><Mono size={19}>{c.via}</Mono></div>
            </div>
          </React.Fragment>
        ) : null;
      })}

      {shot > 0 && (
        <>
          <div style={{ position: "absolute", inset: 0, background: C.bg, opacity: shot * 0.82 }} />
          <Capture src="captures/chat.png" label="127.0.0.1:8090/" x={460} y={150 + (1 - shot) * 60} w={1000} opacity={shot * (1 - shot2)}
                   frame0={cue("browser")} dur={120} focus={[0.5, 0.45]} />
          <Capture src="captures/admin.png" label="127.0.0.1:8090/admin" x={460} y={150 + (1 - shot) * 60} w={1000} opacity={shot * shot2}
                   frame0={Math.round((cue("browser") + cue("openai")) / 2)} dur={90} focus={[0.3, 0.2]} />
        </>
      )}
    </>
  );
};
