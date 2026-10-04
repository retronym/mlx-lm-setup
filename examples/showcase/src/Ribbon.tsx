import React from "react";
import { C, MONO } from "./theme";

/** The memory ribbon: physical memory as a bar, to scale. Children are positioned in GB via `px`. */
export const RIBBON = { x: 160, y: 640, w: 1600, h: 76, gb: 48 };
export const pxPerGb = (w = RIBBON.w, gb = RIBBON.gb) => w / gb;

export const RibbonBar: React.FC<{ x?: number; y?: number; w?: number; h?: number; gb?: number; ticks?: number; budget?: number; budgetP?: number;
                                   label?: string; opacity?: number; children?: React.ReactNode }> =
  ({ x = RIBBON.x, y = RIBBON.y, w = RIBBON.w, h = RIBBON.h, gb = RIBBON.gb, ticks = 0, budget, budgetP = 1, label = "48 GB unified memory", opacity = 1, children }) => {
    const k = w / gb;
    return (
      <div style={{ position: "absolute", left: x, top: y, width: w, height: h, opacity }}>
        <div style={{ position: "absolute", inset: 0, borderRadius: 10, background: "#100e0b", boxShadow: `inset 0 0 0 2px ${C.line}` }} />
        {label && <div style={{ position: "absolute", left: 0, top: -38, fontFamily: MONO, fontSize: 20, color: C.muted }}>{label}</div>}
        {ticks > 0 && Array.from({ length: Math.floor(gb / ticks) + 1 }, (_, i) => (
          <div key={i} style={{ position: "absolute", left: i * ticks * k, top: h + 6, transform: "translateX(-50%)", fontFamily: MONO, fontSize: 17, color: C.faint, opacity: 0.9 }}>
            <div style={{ width: 2, height: 8, background: C.line, margin: "0 auto 4px" }} />{i * ticks}
          </div>
        ))}
        {budget !== undefined && budgetP > 0 && (
          <>
            <div style={{ position: "absolute", left: budget * k + 2, top: 0, width: (gb - budget) * k - 2, height: h, borderRadius: "0 10px 10px 0", opacity: budgetP,
                          background: `repeating-linear-gradient(135deg, transparent 0 10px, rgba(255,255,255,0.045) 10px 20px)` }} />
            <div style={{ position: "absolute", left: budget * k - 1, top: -22, width: 0, height: h + 44, borderLeft: `3px dashed ${C.ink}`, opacity: budgetP }} />
            <div style={{ position: "absolute", left: budget * k + 12, top: -40, fontFamily: MONO, fontSize: 20, color: C.ink, opacity: budgetP }}>budget {budget} GB</div>
          </>
        )}
        {children}
      </div>
    );
  };
