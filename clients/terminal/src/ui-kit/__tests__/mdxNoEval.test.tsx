/** MdxDoc renders agent-authored workspace markdown WITHOUT compiling it to JavaScript (finding C3).
 *  Pins: the closed component registry still renders; every executable MDX construct is rejected (and
 *  therefore lands in the plain-Markdown fallback); and the render path works with `Function` disabled,
 *  i.e. under a CSP that forbids 'unsafe-eval'. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { MdxDoc, renderWorkspaceMdx } from "../MdxDoc";

const html = (node: React.ReactNode) => renderToStaticMarkup(createElement("div", null, node));

describe("renderWorkspaceMdx — declarative tree → React, no eval", () => {
  it("renders markdown + the closed registry (Note, Steps, Tabs, Wikilink, GFM)", () => {
    const out = html(renderWorkspaceMdx([
      "# Title", "",
      "<Note>heads **up**</Note>", "",
      '<Steps><Step title="One">first</Step><Step title="Two">second</Step></Steps>', "",
      '<Tabs><Tab title="A">alpha</Tab><Tab title="B">beta</Tab></Tabs>', "",
      "[[Acme Corp]] text `x<y` <unknown-tag> [ext](https://example.com) [bad](javascript:alert(1))", "",
      "| a | b |", "|---|---|", "| 1 | 2 |", "",
    ].join("\n")));
    expect(out).toContain("Title");
    expect(out).toContain("heads <strong>up</strong>");
    expect(out).toContain("first");
    expect(out).toContain(">One<");
    expect(out).toContain("<button");                       // Tabs rendered
    expect(out).toContain("Acme Corp");                      // wikilink chip
    expect(out).toContain("&lt;unknown-tag&gt;");            // unknown tag → inert text
    expect(out).toContain('href="https://example.com"');
    expect(out).not.toContain("javascript:");                // javascript: href dropped
    expect(out).toContain("<table");
  });

  it.each([
    ["text expression", "value: {1 + 1}"],
    ["flow expression", "{globalThis.fetch('/api/tokens')}"],
    ["ESM import", "import x from './y'\n\n# a"],
    ["ESM export", "export const a = 1\n\n# a"],
    ["expression attribute", '<Note title={document.cookie}>x</Note>'],
    ["spread attribute", "<Note {...props}>x</Note>"],
  ])("rejects executable MDX: %s", (_label, src) => {
    expect(() => renderWorkspaceMdx(src)).toThrow(/not allowed|Cannot handle MDX estrees|unknown component/);
  });

  it("works with Function/eval disabled (what a no-unsafe-eval CSP does)", () => {
    const g = globalThis as unknown as { Function: FunctionConstructor; eval: typeof eval };
    const origFunction = g.Function;
    const origEval = g.eval;
    const blocked = () => { throw new EvalError("blocked by CSP"); };
    g.Function = blocked as unknown as FunctionConstructor;
    g.eval = blocked as unknown as typeof eval;
    try {
      const out = html(renderWorkspaceMdx("<Warning>careful</Warning>\n\n- one\n- two\n"));
      expect(out).toContain("careful");
      expect(out).toContain("<li");
    } finally {
      g.Function = origFunction;
      g.eval = origEval;
    }
  });
});

describe("<MdxDoc> fallback", () => {
  afterEach(() => vi.restoreAllMocks());

  it("executable MDX degrades to the plain renderer with a visible reason", () => {
    const out = html(createElement(MdxDoc, null, "before {alert(1)} after"));
    expect(out).toContain("simplified rendering (MDX failed:");
    expect(out).toContain("not allowed");
    expect(out).toContain("before");
  });

  it("clean docs render through the registry", () => {
    const out = html(createElement(MdxDoc, null, "<Note>fine</Note>"));
    expect(out).not.toContain("simplified rendering");
    expect(out).toContain("fine");
  });
});
