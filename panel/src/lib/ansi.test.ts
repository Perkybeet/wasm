import { describe, expect, it } from "vitest";

import { ansiClassName, applySgr, nearestAnsiColor, parseAnsi, stripAnsi } from "./ansi";

const ESC = "\u001b";

describe("parseAnsi", () => {
  it("returns plain text as one unstyled segment", () => {
    expect(parseAnsi("added 812 packages")).toEqual([{ text: "added 812 packages", style: {} }]);
  });

  it("returns nothing for an empty line", () => {
    expect(parseAnsi("")).toEqual([]);
  });

  it("splits coloured runs and resets", () => {
    expect(parseAnsi(`${ESC}[31mError:${ESC}[0m health check failed`)).toEqual([
      { text: "Error:", style: { fg: "red" } },
      { text: " health check failed", style: {} },
    ]);
  });

  it("combines attributes in one sequence and turns them off individually", () => {
    const segments = parseAnsi(`${ESC}[1;32mok${ESC}[22m still green${ESC}[39m plain`);
    expect(segments).toEqual([
      { text: "ok", style: { bold: true, fg: "green" } },
      { text: " still green", style: { fg: "green" } },
      { text: " plain", style: {} },
    ]);
  });

  it("treats ESC[m as a reset", () => {
    expect(parseAnsi(`${ESC}[33mwarn${ESC}[m done`)[1]).toEqual({ text: " done", style: {} });
  });

  it("maps bright and background colours", () => {
    expect(parseAnsi(`${ESC}[91mx`)[0]?.style).toEqual({ fg: "red" });
    expect(parseAnsi(`${ESC}[90mx`)[0]?.style).toEqual({ fg: "gray" });
    expect(parseAnsi(`${ESC}[41mx`)[0]?.style).toEqual({ bg: "red" });
    expect(parseAnsi(`${ESC}[41mx${ESC}[49my`)[1]?.style).toEqual({});
  });

  it("collapses 256-colour and truecolour to the nearest token hue", () => {
    expect(parseAnsi(`${ESC}[38;5;196mx`)[0]?.style.fg).toBe("red");
    expect(parseAnsi(`${ESC}[38;5;46mx`)[0]?.style.fg).toBe("green");
    expect(parseAnsi(`${ESC}[38;5;244mx`)[0]?.style.fg).toBe("gray");
    expect(parseAnsi(`${ESC}[38;2;30;144;255mx`)[0]?.style.fg).toBe("blue");
    expect(parseAnsi(`${ESC}[38;2;255;170;0mx`)[0]?.style.fg).toBe("yellow");
    expect(parseAnsi(`${ESC}[48;5;1mx`)[0]?.style.bg).toBe("red");
  });

  it("drops cursor movement, erase and OSC hyperlinks but keeps their text", () => {
    expect(stripAnsi(`${ESC}[2K${ESC}[1Gdone`)).toBe("done");
    expect(stripAnsi(`${ESC}]8;;https://example.com${ESC}\\docs${ESC}]8;;${ESC}\\`)).toBe("docs");
    expect(stripAnsi(`${ESC}]0;title\u0007body`)).toBe("body");
  });

  it("lets a carriage return redraw the line, as a progress bar does", () => {
    expect(stripAnsi("progress 10%\rprogress 55%\rprogress 100%")).toBe("progress 100%");
    expect(stripAnsi("windows line ending\r")).toBe("windows line ending");
  });

  it("removes stray control characters but keeps tabs", () => {
    expect(stripAnsi("a\u0007b\tc\u0008")).toBe("ab\tc");
  });

  it("merges adjacent runs with the same style", () => {
    expect(parseAnsi(`${ESC}[31mfoo${ESC}[31mbar`)).toEqual([{ text: "foobar", style: { fg: "red" } }]);
  });
});

describe("applySgr", () => {
  it("ignores unknown codes and keeps the style", () => {
    expect(applySgr({ fg: "cyan" }, "58;5;3")).toEqual({ fg: "cyan" });
  });
});

describe("nearestAnsiColor", () => {
  it("treats low-saturation colours as greys", () => {
    expect(nearestAnsiColor(10, 12, 11)).toBe("black");
    expect(nearestAnsiColor(128, 130, 126)).toBe("gray");
    expect(nearestAnsiColor(240, 240, 240)).toBe("white");
    expect(nearestAnsiColor(200, 40, 200)).toBe("magenta");
    expect(nearestAnsiColor(0, 200, 200)).toBe("cyan");
  });
});

describe("ansiClassName", () => {
  it("maps program colours onto state tokens", () => {
    expect(ansiClassName({ fg: "red" })).toBe("text-fail");
    expect(ansiClassName({ fg: "green" })).toBe("text-ok");
    expect(ansiClassName({ fg: "yellow" })).toBe("text-warn");
    expect(ansiClassName({ fg: "blue", bold: true, underline: true })).toBe("text-ansi-blue font-semibold underline");
    expect(ansiClassName({ bg: "red" })).toBe("bg-fail-soft");
  });

  it("renders dim text as muted and inverse as swapped grounds", () => {
    expect(ansiClassName({ dim: true })).toBe("text-fg-muted");
    expect(ansiClassName({ inverse: true, fg: "red" })).toBe("bg-fg text-bg");
  });

  it("returns an empty string for unstyled text", () => {
    expect(ansiClassName({})).toBe("");
  });
});
