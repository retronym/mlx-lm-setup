import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Arrow, Block, Body, Dot, Heading, INOUT, Mono, Spinner, along, lerp, prog, useAt, useCue, useScene } from "../lib";
import { C, FAMILY, FPS, LABEL, MONO } from "../theme";

const K = 40, SX = 520, SY = 610, SH = 96, WY = 300, BUDGET = 28;
const WING: Record<string, number> = { "qwen3-coder": 420, "openjev-4b": 1150, "jevstyle-2b": 1560, kokoro: 1710 };
const GB: Record<string, number> = { "qwen3-coder": 17.5, "openjev-4b": 9.5, "jevstyle-2b": 3, kokoro: 2.5 };
const ORIGIN: [number, number] = [230, SY + SH / 2];
const tw = (f: number, a: number, b: number) => prog(f, a, b - a, INOUT);

type Shown = { id: string; inStage: number; gb0: number; status?: string; statusColor?: string; spin?: boolean; ttl: number; glow?: number; outline?: string };

/** The gateway as a stage manager: models wait in the wings (on disk) and are called onto the stage (the memory budget). */
export const Stage: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue(), at = useAt();
  const s = (n: string, d = 0) => cue(n, d);
  const diagram = prog(f, s("diagram"), 30, INOUT);

  // ---- kokoro: resident at the start, idle ring running out, passivated at the "idle" cue
  const kLeave = tw(f, s("idle", 0.6), s("idle", 1.4));
  const kTtl = Math.max(0, lerp(0.3, 0, (f) / s("idle", 0.4)));
  // ---- jevstyle: called on at "start", served, then idles
  const jIn = tw(f, s("start", -0.6), s("start", 0.2));
  const jServed = s("start", 1.6);
  const jLeave = tw(f, s("evict", 2.8), s("evict", 3.4));
  const jTtl = f < jServed ? 1 : Math.max(0, 1 - (f - jServed) / (FPS * 30));
  // ---- qwen3-coder: three requests, one start
  const qIn = tw(f, s("single", 0.7), s("single", 1.4));
  const qServed = [s("single", 2.4), s("single", 2.8), s("single", 3.2)];
  const qTtl = f < qServed[2] ? 1 : Math.max(0, 1 - (f - qServed[2]) / (FPS * 40));
  // ---- openjev: the newcomer that does not fit
  const oIn = tw(f, s("evict", 3.9), s("evict", 4.5));
  const kGone = tw(f, s("idle", 1.4), s("idle", 1.9));              // compaction after kokoro leaves
  const jGone = tw(f, s("evict", 3.4), s("evict", 3.9));            // compaction after jevstyle leaves

  const jGb0 = lerp(2.5, 0, kGone);
  const qGb0 = lerp(3, 0, jGone);
  const status = (start: number, ready: number, served: number[], extra?: [string, string]): Pick<Shown, "status" | "statusColor" | "spin" | "glow"> => {
    if (f < start) return {};
    if (f < ready) return { status: "starting", statusColor: C.warn, spin: true };
    const busy = served.some(x => f >= x - 4 && f < x + 8);
    if (busy) return { status: "busy", statusColor: C.accent, glow: 0.7 };
    return extra && f >= 0 ? { status: extra[0], statusColor: extra[1] } : { status: "ready", statusColor: C.good };
  };
  const lru = prog(f, s("evict", 1.7), 10) * (1 - jLeave);
  const shown: Shown[] = [
    { id: "kokoro", inStage: 1 - kLeave, gb0: 0, ttl: kTtl, ...(f >= s("idle", 0.4) ? { status: "passivating", statusColor: C.muted } : { status: "idle", statusColor: C.muted }) },
    { id: "jevstyle-2b", inStage: jIn * (1 - jLeave), gb0: jGb0, ttl: jTtl, ...status(s("start", 0.2), s("start", 1.3), [jServed]),
      ...(lru > 0 ? { status: "idle longest", statusColor: C.accent, outline: C.accent } : {}) },
    { id: "qwen3-coder", inStage: qIn, gb0: qGb0, ttl: qTtl, ...status(s("single", 1.4), s("single", 2.2), qServed) },
    { id: "openjev-4b", inStage: oIn, gb0: 17.5, ttl: 1, ...status(s("evict", 4.5), s("evict", 4.9), [s("evict", 5.0)]) },
  ];
  const resident = shown.reduce((a, m) => a + GB[m.id] * Math.min(1, m.inStage * 1.0), 0);

  // request dots: [target id, launch frame, arrival frame]
  const dots: [string, number, number][] = [
    ["jevstyle-2b", s("start", -1.4), s("start", -0.6)],
    ["qwen3-coder", s("single", 0), s("single", 0.6)], ["qwen3-coder", s("single", 0.1), s("single", 0.7)], ["qwen3-coder", s("single", 0.2), s("single", 0.8)],
    ["openjev-4b", s("evict", 0), s("evict", 0.5)],
  ];
  const stageOp = 1 - diagram;
  const ghost = prog(f, s("evict", 0.6), 10) * (1 - prog(f, s("evict", 3.9), 6));

  return (
    <>
      <div style={{ opacity: stageOp, position: "absolute", inset: 0, transform: `translateY(${-diagram * 80}px)` }}>
        <Appear at={0} style={{ left: 160, top: 96 }}><Heading size={48}>The gateway is a stage manager</Heading></Appear>
        {/* wings */}
        <div style={{ position: "absolute", left: 400, top: WY - 40, width: 1440, height: SH + 70, borderRadius: 16, border: `2px dashed ${C.line}` }} />
        <Mono size={20} style={{ position: "absolute", left: 420, top: WY - 34 }}>the wings · on disk, not loaded</Mono>
        {/* stage */}
        <div style={{ position: "absolute", left: SX - 10, top: SY - 10, width: BUDGET * K + 20, height: SH + 20, borderRadius: 14,
                      background: "radial-gradient(ellipse at 50% 0%, rgba(255,236,200,0.10), rgba(255,236,200,0.02) 70%)", boxShadow: `inset 0 0 0 2px ${C.line}` }} />
        <Mono size={20} style={{ position: "absolute", left: SX, top: SY + SH + 22 }}>the stage · the 28 GB memory budget</Mono>
        <Mono size={20} color={C.ink} style={{ position: "absolute", left: SX + BUDGET * K - 300, top: SY + SH + 22, width: 300, textAlign: "right" }}>
          resident {resident.toFixed(1)} GB
        </Mono>
        <div style={{ position: "absolute", left: SX + BUDGET * K + 10, top: SY - 30, height: SH + 60, borderLeft: `3px dashed ${C.ink}` }} />
        {/* requests come in here */}
        <div style={{ position: "absolute", left: 150, top: SY + 18, textAlign: "center", width: 160 }}><Mono size={18}>requests</Mono></div>

        {ghost > 0 && (
          <div style={{ position: "absolute", left: SX + 20.5 * K, top: SY + 3, width: 9.5 * K - 4, height: SH - 6, borderRadius: 8, border: `3px dashed ${C.accent}`, opacity: ghost }}>
            <Mono size={18} color={C.accent} style={{ position: "absolute", top: -34, left: 0, whiteSpace: "nowrap" }}>needs 9.5 GB · won't fit</Mono>
          </div>
        )}

        {shown.map(m => {
          const x = lerp(WING[m.id], SX + m.gb0 * K + 2, m.inStage), y = lerp(WY, SY + 3, m.inStage);
          const w = GB[m.id] * K - 4;
          return (
            <React.Fragment key={m.id}>
              <Block x={x} y={y} w={w} h={SH - 6} color={FAMILY[m.id]} label={LABEL[m.id]} gb={GB[m.id]} dim={0.65 * (1 - m.inStage)}
                     glow={m.glow ?? 0} outline={m.outline} />
              {m.inStage > 0.95 && (
                <>
                  <div style={{ position: "absolute", left: x, top: y + SH, width: w * m.ttl, height: 5, borderRadius: 3, background: C.ink, opacity: 0.55 }} />
                  {m.status && (
                    <div style={{ position: "absolute", left: x + 4, top: y - 36, display: "flex", alignItems: "center", gap: 10, whiteSpace: "nowrap" }}>
                      {m.spin ? <div style={{ position: "relative", width: 20, height: 20 }}><Spinner x={10} y={10} r={10} color={C.warn} /></div>
                              : <div style={{ width: 12, height: 12, borderRadius: 6, background: m.statusColor }} />}
                      <span style={{ fontFamily: MONO, fontSize: 19, color: m.statusColor }}>{m.status}</span>
                    </div>
                  )}
                </>
              )}
            </React.Fragment>
          );
        })}
        {dots.map(([id, a, b], i) => {
          if (f < a) return null;
          const m = shown.find(x => x.id === id)!;
          const tx = SX + m.gb0 * K + 40, ty = SY + SH / 2;
          const q = f < b ? prog(f, a, b - a, INOUT) : 1;
          const waitX = SX - 30, waitY = ty - 30 + (i % 3) * 30;
          const [px, py] = q < 1 ? along(ORIGIN[0], ORIGIN[1], waitX, waitY, q, -60) : [waitX, waitY];
          // waits at the stage edge until the model is ready, then goes in (one at a time for qwen)
          const served = id === "jevstyle-2b" ? jServed : id === "qwen3-coder" ? qServed[i - 1] : s("evict", 5.0);
          const go = prog(f, served - 8, 8, INOUT);
          const done = f > served + 2;
          return done ? null : <Dot key={i} x={lerp(px, tx, go)} y={lerp(py, ty, go)} r={10} />;
        })}
        <Appear at={s("single", 0.7)} until={s("single", 3.6)} style={{ left: 160, top: SY - 70 }}>
          <Mono size={20} color={C.accent}>3 requests · 1 start</Mono>
        </Appear>
        <Appear at={s("idle", 1.2)} until={s("idle", 3.5)} style={{ left: 1660, top: SY - 70 }}>
          <Mono size={20} color={C.good}>+2.5 GB returned</Mono>
        </Appear>
      </div>
      {diagram > 0 && <Lifecycle start={s("diagram")} />}
    </>
  );
};

const NODES: Record<string, [number, number]> = {
  Stopped: [330, 520], Starting: [720, 340], Ready: [1110, 340], Busy: [1500, 340], Passivating: [920, 700],
};
const EDGES: [string, string, number][] = [["Stopped", "Starting", 0], ["Starting", "Ready", 0], ["Ready", "Busy", -50], ["Busy", "Ready", 50],
                                           ["Ready", "Passivating", 0], ["Passivating", "Stopped", 0]];
const PATH = ["Stopped", "Starting", "Ready", "Busy", "Ready", "Passivating", "Stopped"];
const PATH_BEND = [0, 0, -50, 50, 0, 0];

/** The metaphor flattens into the real state machine; a model traces one lifetime through it. */
const Lifecycle: React.FC<{ start: number }> = ({ start }) => {
  const f = useCurrentFrame();
  const sc = useScene();
  const end = Math.round((sc.lead_s + sc.duration_s) * FPS);
  const appear = prog(f, start, 24);
  const leg = Math.max(1, (end - start - 50) / PATH_BEND.length);
  const t = Math.max(0, (f - start - 30) / leg);
  const i = Math.min(PATH_BEND.length - 1, Math.floor(t));
  const u = Math.min(1, t - i);
  const a = NODES[PATH[i]], b = NODES[PATH[i + 1]];
  const off = (p: [number, number], q: [number, number]) => {         // edge endpoints at the node rims
    const d = Math.hypot(q[0] - p[0], q[1] - p[1]);
    return [p[0] + (q[0] - p[0]) * 120 / d, p[1] + (q[1] - p[1]) * 70 / d, q[0] - (q[0] - p[0]) * 120 / d, q[1] - (q[1] - p[1]) * 70 / d];
  };
  const [x, y] = along(...(off(a, b) as [number, number, number, number]), INOUT(u), PATH_BEND[i]);
  const passiv = prog(f, start + 30 + leg * 4.6, 14);
  return (
    <div style={{ position: "absolute", inset: 0, opacity: appear }}>
      <Heading size={48} style={{ position: "absolute", left: 160, top: 96 }}>Passivation, literally</Heading>
      {EDGES.map(([p, q, bend]) => {
        const [x1, y1, x2, y2] = off(NODES[p], NODES[q]);
        return <Arrow key={p + q} x1={x1} y1={y1} x2={x2} y2={y2} bend={bend} p={prog(f, start + 6, 20)} color={C.faint} width={3} />;
      })}
      {Object.entries(NODES).map(([name, [nx, ny]]) => {
        const active = PATH[i] === name && u < 0.15 || PATH[i + 1] === name && u > 0.85;
        const hot = name === "Passivating" ? passiv : 0;
        return (
          <div key={name} style={{ position: "absolute", left: nx - 120, top: ny - 42, width: 240, height: 84, borderRadius: 42, display: "flex",
                                   alignItems: "center", justifyContent: "center", background: active ? C.panel : C.bg2,
                                   border: `3px solid ${hot ? C.accent : active ? C.ink : C.line}`, fontFamily: MONO, fontSize: 26, color: C.ink }}>{name}</div>
        );
      })}
      <Dot x={x} y={y} r={12} color={C.decision} />
      <div style={{ position: "absolute", left: 1160, top: 660, width: 640, opacity: passiv }}>
        <Body size={30}>the process <b style={{ color: C.accent }}>exits</b>: on Metal, the only reliable way to give memory back</Body>
      </div>
      <Mono size={20} style={{ position: "absolute", left: 210, top: 590, width: 240, textAlign: "center" }}>on disk</Mono>
      <Mono size={20} style={{ position: "absolute", left: 600, top: 405, width: 240, textAlign: "center" }}>≈ 2 s</Mono>
      <Mono size={20} style={{ position: "absolute", left: 990, top: 405, width: 240, textAlign: "center" }}>idle TTL ticking</Mono>
    </div>
  );
};
