import React from "react";
import { useCurrentFrame } from "remotion";
import { Appear, Body, Heading, useCue } from "../lib";
import { Braces } from "../parts";
import { C } from "../theme";

export const Title: React.FC = () => {
  const f = useCurrentFrame();
  const cue = useCue();
  void f;
  return (
    <>
      <Braces x={760} y={250} items={[]} size={150} />
      <Appear at={cue("title")} dy={24} style={{ left: 0, right: 0, top: 520, textAlign: "center" }}>
        <Heading size={104}>Authority as a type.</Heading>
        <Body size={34} color={C.muted} style={{ marginTop: 24 }}>Scala 3 capture checking · safe mode · TACIT</Body>
      </Appear>
    </>
  );
};
