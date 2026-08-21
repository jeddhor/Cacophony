/**
 * The generate screen and the providers page (sections 33, 34, 43, 54, 63, 69).
 *
 * Both of these pages used to *show* things the run could do without letting
 * anyone do them: four of the six formats, no output layouts, an inventory of
 * providers where section 54 asks for requirements, and no way to configure a
 * provider at all. These check the parts that changed, against the payloads
 * the API actually sends.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../src/api/client";
import { GeneratePage } from "../src/pages/GeneratePage";
import { ProvidersPage } from "../src/pages/ProvidersPage";
import { useStudio } from "../src/state/store";
import {
  outputsFixture,
  planFixture,
  providersFixture,
  renderWithProviders,
  schemaFixture,
} from "./fixtures";

vi.mock("../src/api/client", async () => {
  const actual = await vi.importActual<typeof import("../src/api/client")>(
    "../src/api/client",
  );
  return { ...actual, api: { ...actual.api } };
});

beforeEach(() => {
  useStudio.setState({ projectId: 1, generate: { ...useStudio.getState().generate } });
  useStudio.getState().resetGenerate();

  vi.spyOn(api, "schema").mockResolvedValue(schemaFixture);
  vi.spyOn(api, "plan").mockResolvedValue(planFixture);
  vi.spyOn(api, "lint").mockResolvedValue({ ok: true, issues: [] });
  vi.spyOn(api, "outputs").mockResolvedValue(outputsFixture);
  vi.spyOn(api, "providers").mockResolvedValue(providersFixture);
});

describe("the generate screen (sections 54, 69)", () => {
  it("offers every format the writer registry has, database formats included", async () => {
    renderWithProviders(<GeneratePage />);
    const format = await screen.findByLabelText("Format");
    const offered = within(format).getAllByRole("option").map((option) => option.textContent);
    expect(offered).toEqual(["jsonl", "csv", "json", "parquet", "sqlite", "sql"]);
  });

  it("says where a single-file format puts everything", async () => {
    renderWithProviders(<GeneratePage />);
    await userEvent.selectOptions(await screen.findByLabelText("Format"), "sqlite");
    expect(screen.getByText(/becomes a table in one/i)).toBeInTheDocument();
    expect(screen.getByText("cacophony.db")).toBeInTheDocument();
  });

  it("fills the controls a chosen layout decides, rather than hiding them", async () => {
    renderWithProviders(<GeneratePage />);
    await userEvent.selectOptions(await screen.findByLabelText("Layout"), "analytics");

    expect(screen.getByLabelText("Output directory")).toHaveValue("out/corporate-analytics");
    expect(screen.getByLabelText("Format")).toHaveValue("parquet");
    expect(screen.getByText(/partitioned by/i)).toBeInTheDocument();
  });

  it("sends the layout by name, so the run applies what it declares", async () => {
    const start = vi.spyOn(api, "startRun").mockResolvedValue({
      id: "run-1",
    } as never);
    renderWithProviders(<GeneratePage />);
    await userEvent.selectOptions(await screen.findByLabelText("Layout"), "analytics");
    await userEvent.click(screen.getByRole("button", { name: /start cacophony/i }));

    await waitFor(() => expect(start).toHaveBeenCalled());
    const [, body] = start.mock.calls[0]!;
    expect(body.output_profile).toBe("analytics");
    expect(body.output_format).toBe("parquet");
  });

  it("estimates in the units section 69 names, rather than a guess at tokens", async () => {
    renderWithProviders(<GeneratePage />);
    // 1,100,000 tokens from the plan, not 5,000 calls × a made-up 180.
    expect(await screen.findByText("1,100,000")).toBeInTheDocument();
    // 12,000 bytes a record x the form's 1,000-record batch x two entities in
    // flight, not the plan's figure for whatever batch it assumed.
    const memory = screen.getByText("Peak memory").parentElement!;
    expect(within(memory).getByText("22.9 MB")).toBeInTheDocument();
    expect(within(memory).getByText("1,000 records × 2 at a time")).toBeInTheDocument();
  });

  it("recomputes memory when the batch size changes, instead of repeating a default", async () => {
    renderWithProviders(<GeneratePage />);
    await screen.findByText("Peak memory");
    const memory = () => screen.getByText("Peak memory").parentElement!;
    expect(within(memory()).getByText("22.9 MB")).toBeInTheDocument();

    const batch = screen.getByLabelText("Batch size");
    await userEvent.clear(batch);
    await userEvent.type(batch, "10000");

    expect(within(memory()).getByText("229 MB")).toBeInTheDocument();
    expect(within(memory()).getByText("10,000 records × 2 at a time")).toBeInTheDocument();
  });

  it("reports provider requirements, and which of them nothing serves", async () => {
    renderWithProviders(<GeneratePage />);
    // Scoped, because "LLM" is also the badge on every language-model field
    // in the plan below.
    const panel = (await screen.findByText("Provider requirements")).parentElement!;
    expect(within(panel).getByText("LLM")).toBeInTheDocument();
    expect(within(panel).getByText("assistant / mock-1")).toBeInTheDocument();
    expect(within(panel).queryByText(/none configured/)).not.toBeInTheDocument();
  });

  it("warns when a run needs a backend the project does not configure", async () => {
    vi.spyOn(api, "providers").mockResolvedValue({ ...providersFixture, configured: [] });
    renderWithProviders(<GeneratePage />);
    expect(await screen.findByText(/LLM generation is\s+requested/i)).toBeInTheDocument();
    expect(screen.getByText("none configured")).toBeInTheDocument();
  });
});

describe("configuring a provider (sections 43, 63)", () => {
  it("adds one as a targeted patch rather than a rewritten file", async () => {
    const patch = vi
      .spyOn(api, "patchSchema")
      .mockResolvedValue({ revision_id: 4, applied: [], changed: true });
    renderWithProviders(<ProvidersPage />);

    await userEvent.type(await screen.findByLabelText("Name"), "local_llm");
    await userEvent.selectOptions(
      screen.getByLabelText("Adapter", { selector: "#provider-adapter" }),
      "ollama",
    );
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls[0]![1]).toEqual([
      {
        op: "add_provider",
        name: "local_llm",
        value: { adapter: "ollama", type: "language_model" },
      },
    ]);
  });

  it("refuses a name the project already uses, before sending it", async () => {
    renderWithProviders(<ProvidersPage />);
    await userEvent.type(await screen.findByLabelText("Name"), "assistant");
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    expect(screen.getByText(/already has a provider called assistant/i)).toBeInTheDocument();
  });

  it("edits a setting on blur, sending only that key", async () => {
    const patch = vi
      .spyOn(api, "patchSchema")
      .mockResolvedValue({ revision_id: 5, applied: [], changed: true });
    renderWithProviders(<ProvidersPage />);

    const model = await screen.findByLabelText("Model");
    await userEvent.clear(model);
    await userEvent.type(model, "llama3.1:8b");
    await userEvent.tab();

    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls[0]![1]).toEqual([
      { op: "set_provider", name: "assistant", key: "model", value: "llama3.1:8b" },
    ]);
  });

  it("keeps the declared kind honest when the adapter changes", async () => {
    const patch = vi
      .spyOn(api, "patchSchema")
      .mockResolvedValue({ revision_id: 6, applied: [], changed: true });
    renderWithProviders(<ProvidersPage />);

    await userEvent.selectOptions(
      await screen.findByLabelText("Adapter", { selector: "#adapter-assistant" }),
      "procedural_image",
    );

    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls[0]![1]).toEqual([
      { op: "set_provider", name: "assistant", key: "adapter", value: "procedural_image" },
      { op: "set_provider", name: "assistant", key: "type", value: "image" },
    ]);
  });

  it("says a secret id is not a secret", async () => {
    renderWithProviders(<ProvidersPage />);
    expect(await screen.findByLabelText("Secret id")).toBeInTheDocument();
    expect(
      screen.getByText(/names a credential; it is never the credential/i),
    ).toBeInTheDocument();
  });

  it("does not offer to edit a project with no file to write to", async () => {
    vi.spyOn(api, "schema").mockResolvedValue({ ...schemaFixture, editable: false });
    renderWithProviders(<ProvidersPage />);
    expect(await screen.findByText(/no file to write to/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
  });
});
