import React from "react";
import { useCurrentFrame } from "remotion";
import { Snip, prog, useData } from "./lib";
import { BODY, C, HEAD, MONO } from "./theme";

/** The film's recurring object: a capture set, `->{…}` (what this code can touch). `items` are shown as chips inside the braces. */
export const Braces: React.FC<{ x: number; y: number; items: string[]; size?: number; color?: string; arrow?: boolean; label?: string;
                                strike?: number; style?: React.CSSProperties }> =
  ({ x, y, items, size = 56, color = C.ink, arrow = true, label, strike = 0, style }) => (
    <div style={{ position: "absolute", left: x, top: y, display: "flex", flexDirection: "column", alignItems: "flex-start", ...style }}>
      <div style={{ display: "flex", alignItems: "center", fontFamily: MONO, fontSize: size, color, whiteSpace: "nowrap", position: "relative" }}>
        {arrow && <span style={{ color: C.faint, marginRight: size * 0.1 }}>-&gt;</span>}
        <span>{"{"}</span>
        <span style={{ display: "inline-flex", gap: size * 0.25, padding: `0 ${items.length ? size * 0.15 : size * 0.05}px` }}>
          {items.map((it, i) => <span key={i}>{it}{i < items.length - 1 ? "," : ""}</span>)}
        </span>
        <span>{"}"}</span>
        {strike > 0 && <div style={{ position: "absolute", left: 0, top: "52%", height: 5, width: `${strike * 100}%`, background: C.accent, borderRadius: 3 }} />}
      </div>
      {label && <div style={{ fontFamily: BODY, fontSize: size * 0.36, color: C.muted, marginTop: 4, whiteSpace: "nowrap" }}>{label}</div>}
    </div>
  );

/** One line of Scala with light syntax colouring. */
const Line: React.FC<{ text: string }> = ({ text }) => {
  const parts: React.ReactNode[] = [];
  const re = /("[^"]*")|\b(import|def|val|using)\b|(\/\/.*$)/g;
  let last = 0, m: RegExpExecArray | null, k = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const color = m[1] ? C.str : m[2] ? C.kw : C.faint;
    parts.push(<span key={k++} style={{ color }}>{m[0]}</span>);
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  return <>{parts}</>;
};

/** A code panel. `hl` maps 1-based line numbers to a highlight colour; `upto` reveals lines progressively. */
export const Code: React.FC<{ code: string; x: number; y: number; w: number; size?: number; hl?: Record<number, string>; title?: string;
                              upto?: number; style?: React.CSSProperties }> =
  ({ code, x, y, w, size = 30, hl = {}, title, upto, style }) => {
    const lines = code.replace(/\n+$/, "").split("\n");
    return (
      <div style={{ position: "absolute", left: x, top: y, width: w, borderRadius: 14, background: C.panel, border: `1px solid ${C.line}`,
                    overflow: "hidden", boxShadow: "0 24px 60px rgba(0,0,0,0.45)", ...style }}>
        {title && <div style={{ padding: "10px 22px", borderBottom: `1px solid ${C.line}`, fontFamily: MONO, fontSize: 18, color: C.faint }}>{title}</div>}
        <div style={{ padding: "16px 0" }}>
          {lines.map((l, i) => (
            <div key={i} style={{ display: "flex", fontFamily: MONO, fontVariantLigatures: "none", fontSize: size, lineHeight: 1.5, color: C.ink, whiteSpace: "pre",
                                  background: hl[i + 1] ? `${hl[i + 1]}2e` : undefined, boxShadow: hl[i + 1] ? `inset 5px 0 0 ${hl[i + 1]}` : undefined,
                                  opacity: upto === undefined || i < upto ? 1 : 0 }}>
              <span style={{ width: size * 2.2, textAlign: "right", paddingRight: size * 0.8, color: C.faint, flexShrink: 0 }}>{i + 1}</span>
              <span><Line text={l} /></span>
            </div>
          ))}
        </div>
      </div>
    );
  };

/** The compiler's real output for a snippet (from facts.json), up to the first "where:" note. */
export const Compiler: React.FC<{ snip: Snip; x: number; y: number; w: number; size?: number; maxLines?: number; style?: React.CSSProperties }> =
  ({ snip, x, y, w, size = 22, maxLines = 12, style }) => {
    const d = useData();
    let lines = snip.output.split("\n").filter(l => !/^(\d+ errors? found|Compilation failed)$/.test(l));
    const cut = lines.findIndex(l => /^\s*\|?\s*where:/.test(l) || /longer explanation/.test(l));
    if (cut > 0) lines = lines.slice(0, cut);
    while (lines.length && /^\s*\|?\s*$/.test(lines[lines.length - 1])) lines.pop();
    const more = lines.length > maxLines;
    lines = lines.slice(0, maxLines);
    return (
      <div style={{ position: "absolute", left: x, top: y, width: w, borderRadius: 14, background: "#0d0c0a", border: `1px solid ${C.line}`,
                    overflow: "hidden", boxShadow: "0 24px 60px rgba(0,0,0,0.5)", ...style }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 16, padding: "10px 20px", background: "#1f1c18", fontFamily: MONO,
                      fontSize: w < 1000 ? 14 : 17, color: C.muted, whiteSpace: "nowrap" }}>
          <span>$ scala-cli compile · Scala {d.scala}</span>
          <span style={{ color: snip.compiles ? C.good : C.accent }}>{snip.compiles ? "compiled" : "rejected"} in {snip.secs} s</span>
        </div>
        <div style={{ padding: "14px 20px" }}>
          {lines.map((l, i) => (
            <div key={i} style={{ fontFamily: MONO, fontVariantLigatures: "none", fontSize: size, lineHeight: 1.42, whiteSpace: "pre-wrap", wordBreak: "break-word",
                                  color: /^-- /.test(l) ? C.accent : /^\d+ \|/.test(l) ? C.muted : C.ink }}>{l || " "}</div>
          ))}
          {more && <div style={{ fontFamily: MONO, fontSize: size, color: C.faint }}>…</div>}
        </div>
      </div>
    );
  };

/** A labelled box in a diagram: the agent, a pipeline station, a tool. */
export const Box: React.FC<{ x: number; y: number; w: number; h: number; title: string; sub?: string; color?: string; lit?: number;
                             mono?: boolean; style?: React.CSSProperties; children?: React.ReactNode }> =
  ({ x, y, w, h, title, sub, color = C.line, lit = 0, mono, style, children }) => (
    <div style={{ position: "absolute", left: x, top: y, width: w, height: h, borderRadius: 16, border: `3px solid ${lit ? color : C.line}`,
                  background: lit ? `color-mix(in srgb, ${color} ${Math.round(lit * 16)}%, ${C.panel})` : C.panel,
                  boxShadow: lit ? `0 0 ${40 * lit}px ${color}55` : "0 16px 40px rgba(0,0,0,0.35)",
                  display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", textAlign: "center", ...style }}>
      <div style={{ fontFamily: mono ? MONO : HEAD, fontWeight: mono ? 400 : 700, fontSize: mono ? 28 : 34, color: C.ink }}>{title}</div>
      {sub && <div style={{ fontFamily: BODY, fontSize: 21, color: C.muted, marginTop: 6, lineHeight: 1.3 }}>{sub}</div>}
      {children}
    </div>
  );

/** A grid of trials: the first `inject` cells are user+injection trials, the rest malicious. `bad` marks leaked cells per group. */
export const Grid: React.FC<{ x: number; y: number; n: { inject: number; malicious: number }; badInject: number; badMal: number;
                              fill: number; bad: number; title: string; sub?: string; cols?: number; cell?: number }> =
  ({ x, y, n, badInject, badMal, fill, bad, title, sub, cols = 12, cell = 26 }) => {
    const total = n.inject + n.malicious;
    const gap = 6;
    // spread the leaked cells evenly through each group, so they read as "some of these", not "the last few"
    const leaked = (i: number) => i < n.inject
      ? badInject > 0 && Math.floor((i + 1) * badInject / n.inject) > Math.floor(i * badInject / n.inject)
      : badMal > 0 && Math.floor((i - n.inject + 1) * badMal / n.malicious) > Math.floor((i - n.inject) * badMal / n.malicious);
    return (
      <div style={{ position: "absolute", left: x, top: y }}>
        <div style={{ fontFamily: HEAD, fontWeight: 700, fontSize: 32, color: C.ink }}>{title}</div>
        {sub && <div style={{ fontFamily: BODY, fontSize: 21, color: C.muted, marginTop: 2 }}>{sub}</div>}
        <div style={{ position: "relative", marginTop: 16, width: cols * (cell + gap), height: Math.ceil(total / cols) * (cell + gap) }}>
          {Array.from({ length: total }, (_, i) => {
            const on = fill * total > i;
            const red = leaked(i) && bad > 0;
            const mal = i >= n.inject;
            return <div key={i} style={{ position: "absolute", left: (i % cols) * (cell + gap), top: Math.floor(i / cols) * (cell + gap), width: cell, height: cell,
                                         borderRadius: mal ? cell / 2 : 5,
                                         background: red ? `color-mix(in srgb, ${C.accent} ${Math.round(bad * 100)}%, ${C.good})` : on ? C.good : C.line,
                                         opacity: on ? 1 : 0.5 }} />;
          })}
        </div>
      </div>
    );
  };

/** 0..1 pulse for an element that should draw the eye briefly at `at`. */
export const usePulse = (at: number, dur = 20) => {
  const f = useCurrentFrame();
  const p = prog(f, at, dur);
  return Math.sin(p * Math.PI);
};
