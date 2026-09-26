import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { sourceLink } from "./SourceLink";

describe("sourceLink", () => {
  it("links an http(s) source, with the external mark", () => {
    render(<>{sourceLink("https://github.com/shop/storefront.git")}</>);
    const link = screen.getByRole("link", { name: /github\.com\/shop\/storefront\.git/ });
    expect(link).toHaveAttribute("href", "https://github.com/shop/storefront.git");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("links a bare http source too", () => {
    render(<>{sourceLink("http://git.internal.example.com/shop.git")}</>);
    expect(screen.getByRole("link")).toHaveAttribute("href", "http://git.internal.example.com/shop.git");
  });

  it("leaves a local path or an SSH remote as plain text, never a link", () => {
    render(<>{sourceLink("git@github.com:shop/storefront.git")}</>);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("git@github.com:shop/storefront.git")).toBeInTheDocument();

    render(<>{sourceLink("/var/www/apps/shop/current")}</>);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("leaves a source with a masked credential as plain text, never a broken link", () => {
    render(<>{sourceLink("https://***@github.com/shop/storefront.git")}</>);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("https://***@github.com/shop/storefront.git")).toBeInTheDocument();

    render(<>{sourceLink("https://x-access-token:***@github.com/shop/storefront.git")}</>);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("never turns a dangerous scheme into a link", () => {
    render(<>{sourceLink("javascript:alert(1)")}</>);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("javascript:alert(1)")).toBeInTheDocument();
  });

  it("is null for no source", () => {
    expect(sourceLink(null)).toBeNull();
  });
});
