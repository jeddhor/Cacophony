/**
 * The asset manager (design document sections 19, 81, 92).
 *
 *     Employee
 *        ├── portrait.png
 *        ├── id_badge.pdf
 *        └── voicemail.wav
 *
 * Section 81 says assets reference their parent, so this browses by run and
 * then by entity and kind, and shows what each file belongs to. Images are
 * shown, audio is playable, documents link out - because the point of
 * generating a portrait is being able to look at it, and a table of paths
 * would make the user open a file manager to find out whether the run worked.
 *
 * Section 19's provenance - provider, workflow, seed, prompt hash - is on
 * every card, since "why does this image look like that" is the question an
 * asset browser exists to answer.
 */

import { type ReactNode, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useRunAssets, useRuns } from "../api/hooks";
import type { AssetRow } from "../api/types";
import { PageHead } from "../components/Layout";
import { Empty, Panel, Spinner } from "../components/ui";

export function AssetsPage(): ReactNode {
  const [params, setParams] = useSearchParams();
  const runId = params.get("run");
  const runs = useRuns({ limit: 50 });

  // Default to the most recent run that actually produced something.
  const candidates = (runs.data ?? []).filter((run) => run.records_written > 0);
  const selected = runId ?? candidates[0]?.id ?? null;

  return (
    <>
      <PageHead
        title="Assets"
        subtitle="Generated media, and the record each file belongs to"
      />

      {runs.isLoading && <Spinner label="Loading runs" />}

      {!runs.isLoading && candidates.length === 0 && (
        <Empty title="Nothing generated yet">
          <p>Assets appear here once a run produces images, audio or documents.</p>
        </Empty>
      )}

      {candidates.length > 0 && (
        <Panel title="Run">
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            {candidates.slice(0, 12).map((run) => (
              <button
                key={run.id}
                type="button"
                className={`chip ${run.id === selected ? "chip-active" : ""}`}
                onClick={() => setParams({ run: run.id })}
              >
                {run.id.slice(0, 8)}
                <span className="faint"> · {run.records_written.toLocaleString()}</span>
              </button>
            ))}
          </div>
        </Panel>
      )}

      {selected && <AssetBrowser runId={selected} />}
    </>
  );
}

function AssetBrowser({ runId }: { runId: string }): ReactNode {
  const [kind, setKind] = useState<string | null>(null);
  const [entity, setEntity] = useState<string | null>(null);
  const assets = useRunAssets(runId, { kind, entity });

  if (assets.isLoading) return <Spinner label="Loading assets" />;
  if (!assets.data || assets.data.total === 0) {
    return (
      <Empty title="This run produced no assets">
        <p>Add an image, tts or document field to the schema and generate again.</p>
      </Empty>
    );
  }

  const data = assets.data;

  return (
    <>
      <div style={{ height: 16 }} />
      <Panel
        title={`${data.total.toLocaleString()} assets`}
        actions={<span className="faint mono">{data.root}</span>}
      >
        <div className="row" style={{ flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
          <Filter label="all kinds" active={kind === null} onClick={() => setKind(null)} />
          {data.kinds.map((value) => (
            <Filter
              key={value}
              label={value}
              active={kind === value}
              onClick={() => setKind(value)}
            />
          ))}
          <span style={{ width: 12 }} />
          <Filter label="all entities" active={entity === null} onClick={() => setEntity(null)} />
          {data.entities.map((value) => (
            <Filter
              key={value}
              label={value}
              active={entity === value}
              onClick={() => setEntity(value)}
            />
          ))}
        </div>

        <div className="asset-grid">
          {data.assets.map((asset) => (
            <AssetCard key={`${asset.entity}-${asset.record_index}-${asset.field}`} asset={asset} />
          ))}
        </div>

        {data.total > data.assets.length && (
          <p className="faint" style={{ marginBottom: 0 }}>
            Showing {data.assets.length.toLocaleString()} of {data.total.toLocaleString()}.
          </p>
        )}
      </Panel>
    </>
  );
}

function Filter({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}): ReactNode {
  return (
    <button type="button" className={`chip ${active ? "chip-active" : ""}`} onClick={onClick}>
      {label}
    </button>
  );
}

/** Read one metadata value as text. The manifest's values are deliberately
 *  untyped - a provider may record anything about a file - so they are
 *  narrowed here rather than trusted. */
function text(meta: Record<string, unknown>, key: string): string | null {
  const value = meta[key];
  if (value === null || value === undefined || value === "") return null;
  return String(value);
}

function AssetCard({ asset }: { asset: AssetRow }): ReactNode {
  const meta = asset.metadata ?? {};
  const heading = text(meta, "workflow") ?? text(meta, "voice") ?? text(meta, "title");
  const seed = text(meta, "seed");
  const duration = text(meta, "duration_seconds");
  const transcript = text(meta, "transcript");
  return (
    <figure className="asset-card">
      <div className="asset-preview">
        {asset.kind === "image" && (
          <img src={asset.url} alt={`${asset.entity} ${asset.field}`} loading="lazy" />
        )}
        {asset.kind === "audio" && (
          // eslint-disable-next-line jsx-a11y/media-has-caption -- the transcript is shown below
          <audio controls preload="none" src={asset.url} style={{ width: "100%" }} />
        )}
        {asset.kind === "document" && (
          <a className="asset-doc" href={asset.url} target="_blank" rel="noreferrer">
            <span className="asset-doc-glyph" aria-hidden="true">
              ▤
            </span>
            <span>{asset.media_type.split("/")[1]?.toUpperCase() ?? "FILE"}</span>
          </a>
        )}
      </div>

      <figcaption>
        <div className="row spread">
          <strong>{asset.field}</strong>
          <span className="faint nums">{formatBytes(asset.size_bytes)}</span>
        </div>
        <div className="faint" style={{ fontSize: "0.74rem" }}>
          {asset.entity} · {asset.record_id || `#${asset.record_index}`}
        </div>

        {/* Section 19's provenance: why this file looks the way it does. */}
        {heading !== null && (
          <div className="faint mono" style={{ fontSize: "0.7rem", marginTop: 4 }}>
            {heading}
            {seed !== null && ` · seed ${seed}`}
            {duration !== null && ` · ${Number(duration).toFixed(1)}s`}
          </div>
        )}

        {transcript !== null && (
          <p className="faint" style={{ fontSize: "0.72rem", marginTop: 6, marginBottom: 0 }}>
            {transcript.slice(0, 140)}
            {transcript.length > 140 ? "…" : ""}
          </p>
        )}
      </figcaption>
    </figure>
  );
}

function formatBytes(count: number): string {
  if (count < 1024) return `${count} B`;
  if (count < 1024 * 1024) return `${(count / 1024).toFixed(1)} KB`;
  return `${(count / (1024 * 1024)).toFixed(1)} MB`;
}

/** A link to a record's assets, for the run inspector. */
export function AssetsLink({ runId }: { runId: string }): ReactNode {
  return <Link to={`/assets?run=${runId}`}>Browse assets</Link>;
}
