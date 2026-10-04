import React from "react";
import { useCurrentFrame } from "remotion";
import { Mono, Spinner, prog } from "./lib";
import { BODY, C, HEAD, MONO } from "./theme";

// ---- the team: the lead above the office, the interns at desks inside it -------------------------------------------------------
export const INTERNS = [
  { id: "gemma", name: "Gemma 4 26B", gb: 16, job: "drafts, summaries", meta: "16 GB · ~85 tok/s", color: C.llm },
  { id: "coder", name: "Qwen3-Coder 30B", gb: 17.5, job: "boilerplate, extraction", meta: "17.5 GB · ~108 tok/s", color: C.llm },
  { id: "jev", name: "Jev-Style 2B", gb: 3, job: "sorting, labelling", meta: "3 GB · ~1 s a decision", color: C.decision },
  { id: "nli", name: "OpenJev 4B", gb: 9.5, job: "fact-checking", meta: "9.5 GB", color: C.nli },
  { id: "tts", name: "Qwen3-TTS", gb: 4.5, job: "voice", meta: "4.5 GB", color: C.speech },
  { id: "stt", name: "Whisper", gb: 3, job: "transcription, timing", meta: "3 GB", color: C.speech },
] as const;
export type InternId = typeof INTERNS[number]["id"];

export const OFFICE = { x: 140, y: 430, w: 1640, h: 470 };
export const DOOR = { x: 860, y: 416, w: 200, h: 28 };
export const LEAD = { x: 760, y: 110, w: 400 };
export const DESK = { w: 250, gap: 22, y: 640 };
export const deskX = (i: number) => 160 + (1600 - 6 * DESK.w - 5 * DESK.gap) / 2 + i * (DESK.w + DESK.gap);
export const deskCenter = (id: InternId): [number, number] => {
  const i = INTERNS.findIndex(x => x.id === id);
  return [deskX(i) + DESK.w / 2, DESK.y];
};

export const Badge: React.FC<{ x: number; y: number; w: number; name: string; role: string; meta: string; color: string; on: boolean;
                               lead?: boolean; opacity?: number; loading?: boolean; note?: string; noteColor?: string; dy?: number }> =
  ({ x, y, w, name, role, meta, color, on, lead, opacity = 1, loading, note, noteColor, dy = 0 }) => (
    <div style={{ position: "absolute", left: x, top: y + dy, width: w, opacity, borderRadius: 14, background: C.panel, border: `2px solid ${on ? color : C.line}`,
                  boxShadow: on ? `0 0 30px ${color}33` : undefined, overflow: "hidden" }}>
      <div style={{ height: 8, background: color, opacity: on ? 1 : 0.35 }} />
      <div style={{ padding: "14px 18px" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", height: 18 }}>
          <Mono size={15} color={C.faint}>{lead ? "LEAD" : "INTERN"}</Mono>
          <div style={{ display: "flex", alignItems: "center", gap: 6, position: "relative" }}>
            {loading ? <div style={{ position: "relative", width: 14, height: 14 }}><Spinner x={7} y={7} r={7} color={C.warn} /></div>
                     : <div style={{ width: 10, height: 10, borderRadius: 5, background: on ? C.good : C.line }} />}
            <Mono size={14} color={loading ? C.warn : on ? C.good : C.faint}>{loading ? "coming in" : on ? "in" : "out"}</Mono>
          </div>
        </div>
        <div style={{ fontFamily: HEAD, fontWeight: 700, fontSize: lead ? 34 : 24, color: C.ink, marginTop: 6 }}>{name}</div>
        <div style={{ fontFamily: BODY, fontSize: lead ? 22 : 18, color: C.ink, marginTop: 4 }}>{role}</div>
        <div style={{ fontFamily: MONO, fontSize: 14, color: C.muted, marginTop: 6 }}>{meta}</div>
      </div>
      {note && <div style={{ padding: "0 18px 12px", fontFamily: MONO, fontSize: 14, color: noteColor ?? C.muted }}>{note}</div>}
    </div>
  );

export const LeadBadge: React.FC<{ opacity?: number; dy?: number }> = ({ opacity = 1, dy = 0 }) => (
  <Badge x={LEAD.x} y={LEAD.y} w={LEAD.w} name="Claude Opus 5.5" role="plans, designs, reviews · decides who does what" meta="hosted · drives Claude Code"
         color={C.ink} on lead opacity={opacity} dy={dy} />
);

/** The office outline, its door, and the interns' desks. `lights` says who is in; `appear` staggers the desks in. */
export const Office: React.FC<{ opacity?: number; lights?: Partial<Record<InternId, boolean>>; loading?: Partial<Record<InternId, boolean>>;
                                notes?: Partial<Record<InternId, [string, string?]>>; appear?: number; doorGlow?: number; dim?: Partial<Record<InternId, number>> }> =
  ({ opacity = 1, lights = {}, loading = {}, notes = {}, appear, doorGlow = 0, dim = {} }) => {
    const f = useCurrentFrame();
    return (
      <div style={{ position: "absolute", inset: 0, opacity }}>
        <div style={{ position: "absolute", left: OFFICE.x, top: OFFICE.y, width: OFFICE.w, height: OFFICE.h, borderRadius: 24, border: `2px dashed ${C.line}` }} />
        <Mono size={18} style={{ position: "absolute", left: OFFICE.x + 30, top: OFFICE.y + 15 }}>this Mac · nothing inside leaves it</Mono>
        <div style={{ position: "absolute", left: DOOR.x, top: DOOR.y, width: DOOR.w, height: DOOR.h, background: C.bg, border: `2px solid ${C.accent}`, borderRadius: 8,
                      display: "flex", alignItems: "center", justifyContent: "center", boxShadow: doorGlow ? `0 0 ${40 * doorGlow}px ${C.accent}` : undefined }}>
          <Mono size={15} color={C.ink}>127.0.0.1:8090</Mono>
        </div>
        {INTERNS.map((it, i) => {
          const a = appear === undefined ? 1 : prog(f, appear + i * 5, 14);
          const n = notes[it.id];
          return a > 0 ? (
            <Badge key={it.id} x={deskX(i)} y={DESK.y} w={DESK.w} name={it.name} role={it.job} meta={it.meta} color={it.color}
                   on={!!lights[it.id]} loading={!!loading[it.id]} opacity={a * (1 - (dim[it.id] ?? 0) * 0.6)} dy={(1 - a) * 20}
                   note={n?.[0]} noteColor={n?.[1]} />
          ) : null;
        })}
      </div>
    );
  };

// ---- job cards ------------------------------------------------------------------------------------------------------------------
export type Card = { id: string; text: string; truth: string; private: boolean; sorted: string; p: number };

export const JobCard: React.FC<{ card: Card; x: number; y: number; w?: number; rot?: number; opacity?: number; scale?: number; tag?: string;
                                 tagColor?: string; lock?: number; outline?: string; small?: boolean }> =
  ({ card, x, y, w = 360, rot = 0, opacity = 1, scale = 1, tag, tagColor, lock = 0, outline, small }) => (
    <div style={{ position: "absolute", left: x, top: y, width: w, opacity, transform: `rotate(${rot}deg) scale(${scale})`, transformOrigin: "50% 50%",
                  padding: small ? "8px 12px" : "12px 16px", borderRadius: 10, background: "#f1ece3", color: "#15130f", boxSizing: "border-box",
                  boxShadow: `0 10px 30px rgba(0,0,0,0.45)${outline ? `, 0 0 0 4px ${outline}` : ""}`, fontFamily: BODY, fontSize: small ? 15 : 19, lineHeight: 1.3 }}>
      {card.text}
      {(tag || lock > 0) && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 6 }}>
          <span style={{ fontFamily: MONO, fontSize: small ? 11 : 13, color: tagColor ?? "#55503f" }}>{tag}</span>
          {lock > 0 && <span style={{ fontSize: small ? 14 : 18, opacity: lock }}>🔒</span>}
        </div>
      )}
    </div>
  );

// ---- the plan rail ----------------------------------------------------------------------------------------------------------------
export const CHAPTERS = ["how the interns are reached", "which jobs suit them", "how their work gets checked"];
export const RAIL_Y = 34;
export const railX = (i: number) => 160 + i * 560;

/** current: 0..2 for the chapter being played, 3 when all are done. */
export const PlanRail: React.FC<{ current: number; opacity?: number }> = ({ current, opacity = 1 }) => (
  <div style={{ position: "absolute", left: 0, top: RAIL_Y, opacity }}>
    {CHAPTERS.map((c, i) => (
      <div key={c} style={{ position: "absolute", left: railX(i), whiteSpace: "nowrap", fontFamily: MONO, fontSize: 20,
                            color: i < current ? C.good : i === current ? C.ink : C.faint,
                            borderBottom: i === current ? `2px solid ${C.accent}` : "2px solid transparent", paddingBottom: 4 }}>
        {i < current ? "✓ " : ""}{i + 1} · {c}
      </div>
    ))}
  </div>
);
