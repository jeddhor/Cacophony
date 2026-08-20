/**
 * The layout, at the widths this is actually used at (sections 45, 46, 48, 54).
 *
 * Two properties, checked on every page: the window never scrolls sideways,
 * and every navigation destination stays reachable. Wide content is allowed to
 * scroll - inside its own panel, which is what `.table-scroll` is for - but the
 * page taking a 1,200px table with it is the bug.
 */

import { expect, test, type Page } from "@playwright/test";

import { selectProject, stubApi } from "./api";

/**
 * Every destination in section 46's navigation, and the heading that proves
 * the page arrived. The Studio's heading is the project's name, which is the
 * point of it, so `link` is what the navigation says and `heading` is what the
 * page says.
 */
const PAGES = [
  { path: "/projects", link: "Projects", heading: "Projects" },
  { path: "/studio", link: "Studio", heading: "Corporate Directory" },
  { path: "/generate", link: "Generate", heading: "Generate" },
  { path: "/stream", link: "Stream", heading: "Stream" },
  { path: "/runs", link: "Runs", heading: "Runs" },
  { path: "/providers", link: "Providers", heading: "Providers" },
  { path: "/assets", link: "Assets", heading: "Assets" },
  { path: "/plugins", link: "Plugins", heading: "Plugins" },
  { path: "/settings", link: "Settings", heading: "Settings" },
];

test.beforeEach(async ({ page }) => {
  await selectProject(page);
  await stubApi(page);
});

/** How far past the viewport the document extends. Zero, or the layout leaks. */
async function overflow(page: Page): Promise<number> {
  return page.evaluate(() => {
    const root = document.documentElement;
    return Math.max(0, root.scrollWidth - root.clientWidth);
  });
}

for (const { path, heading } of PAGES) {
  test(`${path} fits the window`, async ({ page }) => {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
    // One pixel of slack for sub-pixel rounding of a border.
    expect(await overflow(page)).toBeLessThanOrEqual(1);
  });
}

test("every destination is reachable", async ({ page }) => {
  await page.goto("/projects");
  const nav = page.getByRole("navigation", { name: "Primary" });

  for (const { link: label, heading } of PAGES) {
    const link = nav.getByRole("link", { name: label, exact: true });
    await expect(link).toBeVisible();
    // Visible is not the same as reachable: a link scrolled out of an
    // overflowing strip still has a box, so click it rather than trust it.
    await link.click();
    await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
    expect(await overflow(page)).toBeLessThanOrEqual(1);
  }
});

/** The width at which the shell turns its column of navigation into a strip. */
const SHELL_BREAKPOINT = 860;
/** The width at which the Studio's three panes stop being three columns. */
const STUDIO_BREAKPOINT = 1180;

test("the navigation is a column beside the page, or a strip above it", async ({
  page,
}) => {
  await page.goto("/projects");
  const sidebar = page.getByRole("navigation", { name: "Primary" });
  const main = page.getByRole("main");

  const nav = (await sidebar.boundingBox())!;
  const body = (await main.boundingBox())!;

  if (page.viewportSize()!.width <= SHELL_BREAKPOINT) {
    // Above, not beside: 216 fixed pixels of chrome is a third of a 700px
    // window, and the third it takes is the one the work happens in.
    expect(nav.y + nav.height).toBeLessThanOrEqual(body.y + 1);
    expect(nav.height).toBeLessThan(120);
    expect(body.width).toBeGreaterThan(page.viewportSize()!.width * 0.9);
  } else {
    expect(nav.x + nav.width).toBeLessThanOrEqual(body.x + 1);
    expect(nav.width).toBeGreaterThan(150);
  }
});

test("a table wider than the window scrolls inside its panel", async ({ page }) => {
  await page.goto("/studio");
  await expect(
    page.getByRole("heading", { name: "Corporate Directory", level: 1 }),
  ).toBeVisible();
  const table = page.locator(".table-scroll").first();
  await expect(table).toBeVisible();

  const scrollable = await table.evaluate((element) => ({
    content: element.scrollWidth,
    visible: element.clientWidth,
    canScroll: getComputedStyle(element).overflowX === "auto",
  }));
  expect(scrollable.canScroll).toBe(true);
  expect(scrollable.visible).toBeGreaterThan(0);
  // Whatever the content does, the document does not follow it.
  expect(await overflow(page)).toBeLessThanOrEqual(1);
});

test("the studio's three panes stack rather than squeeze", async ({ page }) => {
  await page.goto("/studio");
  await expect(
    page.getByRole("heading", { name: "Corporate Directory", level: 1 }),
  ).toBeVisible();

  const panes = page.locator(".studio > *");
  const boxes = await panes.evaluateAll((elements) =>
    elements.map((element) => element.getBoundingClientRect().top),
  );
  expect(boxes.length).toBeGreaterThanOrEqual(2);

  const stacked = new Set(boxes).size > 1;
  expect(stacked).toBe(page.viewportSize()!.width <= STUDIO_BREAKPOINT);
});

test("the generate form stays usable and its controls stay in the window", async ({
  page,
}) => {
  await page.goto("/generate");
  await expect(page.getByRole("heading", { name: "Generate", level: 1 })).toBeVisible();

  const start = page.getByRole("button", { name: "START CACOPHONY" });
  await expect(start).toBeVisible();

  for (const control of [
    page.getByLabel("Output directory"),
    page.getByLabel("Format"),
    page.getByLabel("Layout"),
    start,
  ]) {
    const box = (await control.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()!.width + 1);
  }

  // And it still works: choosing a layout fills in what that layout decides.
  await page.getByLabel("Layout").selectOption("analytics");
  await expect(page.getByLabel("Output directory")).toHaveValue("out/corporate-analytics");
  expect(await overflow(page)).toBeLessThanOrEqual(1);
});
