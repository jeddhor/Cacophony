/**
 * Cacophony Studio (design document section 48).
 *
 *     Left    entities
 *     Centre  entity and field editor
 *     Right   generation properties
 *
 * Section 48 calls this the heart of the UI, and lists what it must let a user
 * do: inspect dependencies, test generation, preview records. All four tabs
 * here answer one of those. The fourth - the source view - exists because a
 * form can only edit what it has controls for, and a schema is allowed to be
 * cleverer than the form.
 */

import { type ReactNode, Suspense, lazy, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  useLint,
  usePatchSchema,
  usePreview,
  useSchema,
  useWriteSchema,
} from "../api/hooks";
import type { EntityView, SchemaOperation } from "../api/types";
import { PageHead } from "../components/Layout";
import {
  Empty,
  ErrorNotice,
  GeneratorBadge,
  LintList,
  Notice,
  Panel,
  Spinner,
  formatNumber,
} from "../components/ui";
import { useStudio } from "../state/store";
import { FieldEditor } from "../studio/FieldEditor";
import { PreviewTable } from "../studio/PreviewTable";

type Tab = "fields" | "preview" | "graph" | "source";

// React Flow is the single largest thing the Studio depends on, and only the
// graph tab needs it. Loading it on demand keeps the initial bundle to the
// parts of the interface that are used on every visit.
const RelationshipGraph = lazy(() =>
  import("../graphs/RelationshipGraph").then((module) => ({
    default: module.RelationshipGraph,
  })),
);

export function StudioPage(): ReactNode {
  const projectId = useStudio((state) => state.projectId);
  const entityName = useStudio((state) => state.entity);
  const fieldName = useStudio((state) => state.field);
  const selectEntity = useStudio((state) => state.selectEntity);
  const selectField = useStudio((state) => state.selectField);

  const schema = useSchema(projectId);
  const lint = useLint(projectId);
  const patch = usePatchSchema(projectId ?? -1);
  const preview = usePreview(projectId ?? -1);
  const [tab, setTab] = useState<Tab>("fields");

  const entities = schema.data?.entities ?? {};
  const order = schema.data?.entity_order ?? [];
  const entity: EntityView | undefined = entityName ? entities[entityName] : undefined;

  // Land on the first entity rather than an empty pane.
  useEffect(() => {
    if (!entityName && order.length > 0) selectEntity(order[0] ?? null);
  }, [entityName, order, selectEntity]);

  const field = entity && fieldName ? entity.fields[fieldName] : undefined;

  const applyPatch = (operations: SchemaOperation[]) => {
    patch.mutate(operations, {
      onSuccess: () => {
        const rename = operations.find((operation) => operation.op === "rename_field");
        if (rename?.name) selectField(rename.name);
        if (operations.some((operation) => operation.op === "remove_field")) selectField(null);
      },
    });
  };

  const runPreview = (target?: string) => {
    if (!entityName && !target) return;
    preview.mutate({ entity: target ?? entityName ?? undefined, count: 10 });
    setTab("preview");
  };

  if (projectId === null) {
    return (
      <Empty title="No project selected">
        <p>
          Choose one on the <Link to="/projects">Projects</Link> page.
        </p>
      </Empty>
    );
  }

  if (schema.isLoading) return <Spinner label="Compiling the schema" />;
  if (schema.isError) return <ErrorNotice error={schema.error} />;
  if (!schema.data) return null;

  return (
    <>
      <PageHead
        title={schema.data.name}
        subtitle={
          <>
            {order.length} entit{order.length === 1 ? "y" : "ies"} ·{" "}
            {schema.data.editable ? (
              <>editing {schema.data.source_format.toUpperCase()} in place</>
            ) : (
              <>read-only</>
            )}
          </>
        }
        actions={
          <>
            <button type="button" onClick={() => runPreview()} disabled={preview.isPending}>
              {preview.isPending ? "Generating…" : "Preview 10"}
            </button>
            <Link className="button button-primary" to="/generate">
              Generate
            </Link>
          </>
        }
      />

      {patch.isError && <ErrorNotice error={patch.error} />}
      {patch.isSuccess && patch.data?.changed && (
        <Notice>Saved — schema revision {patch.data.revision_id}.</Notice>
      )}

      <div className="studio">
        <EntityPane
          entities={entities}
          order={order}
          selected={entityName}
          editable={schema.data.editable}
          onSelect={selectEntity}
          onPatch={applyPatch}
        />

        <div>
          <div className="row" style={{ marginBottom: 12, gap: 6 }}>
            {(["fields", "preview", "graph", "source"] as Tab[]).map((name) => (
              <button
                key={name}
                type="button"
                className="button-sm"
                aria-pressed={tab === name}
                style={
                  tab === name
                    ? { borderColor: "var(--violet)", color: "var(--violet)" }
                    : undefined
                }
                onClick={() => setTab(name)}
              >
                {name}
              </button>
            ))}
          </div>

          {tab === "fields" && entity && (
            <FieldsPane
              entity={entity}
              selected={fieldName}
              editable={schema.data.editable}
              onSelect={selectField}
              onPatch={applyPatch}
            />
          )}

          {tab === "preview" && (
            <Panel title={`Preview${entityName ? ` · ${entityName}` : ""}`}>
              {preview.isPending && <Spinner label="Generating records" />}
              <ErrorNotice error={preview.error} />
              {preview.data ? (
                <PreviewTable preview={preview.data} entity={entities[preview.data.entity]} />
              ) : (
                !preview.isPending && (
                  <p className="faint">
                    Nothing sampled yet. Press <strong>Preview 10</strong> above.
                  </p>
                )
              )}
              <p className="faint" style={{ fontSize: "0.76rem", marginBottom: 0 }}>
                These are the records a real run would produce at these indices.
                Sampling cannot disturb a run, because a record's seed comes
                from its position rather than from a shared stream.
              </p>
            </Panel>
          )}

          {tab === "graph" && (
            <Panel title="Entity relationships">
              <Suspense fallback={<Spinner label="Loading the graph" />}>
                <RelationshipGraph
                  entities={entities}
                  order={order}
                  relationships={schema.data.relationships}
                  references={schema.data.references}
                  selected={entityName}
                  onSelect={selectEntity}
                />
              </Suspense>
              <p className="faint" style={{ fontSize: "0.76rem", marginBottom: 0 }}>
                Cyan edges are foreign keys, labelled with the field that
                carries them; they are also why the entities generate in this
                order. Dashed violet edges are relationships the schema
                declares but no field implements.
              </p>
            </Panel>
          )}

          {tab === "source" && (
            <SourcePane
              projectId={projectId}
              source={schema.data.source}
              editable={schema.data.editable}
            />
          )}
        </div>

        <div>
          {field && entity ? (
            <FieldEditor
              entity={entity}
              field={field}
              editable={schema.data.editable}
              onPatch={applyPatch}
              onPreview={() => runPreview(entity.name)}
              pending={patch.isPending || preview.isPending}
            />
          ) : (
            <Panel title="Field">
              <p className="faint">Select a field to edit its generation properties.</p>
            </Panel>
          )}

          <div style={{ height: 16 }} />

          <Panel title="Linter">
            {lint.isLoading && <Spinner />}
            {lint.data && <LintList issues={lint.data.issues} />}
          </Panel>
        </div>
      </div>
    </>
  );
}

function EntityPane({
  entities,
  order,
  selected,
  editable,
  onSelect,
  onPatch,
}: {
  entities: Record<string, EntityView>;
  order: string[];
  selected: string | null;
  editable: boolean;
  onSelect: (name: string) => void;
  onPatch: (operations: SchemaOperation[]) => void;
}): ReactNode {
  return (
    <Panel title="Entities">
      {order.map((name) => (
        <button
          key={name}
          type="button"
          className={`entity-item ${name === selected ? "active" : ""}`}
          aria-current={name === selected}
          onClick={() => onSelect(name)}
        >
          <div>{name}</div>
          <div className="count">
            {formatNumber(entities[name]?.count)} ×{" "}
            {Object.keys(entities[name]?.fields ?? {}).length}
          </div>
        </button>
      ))}

      {editable && (
        <button
          type="button"
          className="button-sm"
          style={{ marginTop: 10, width: "100%" }}
          onClick={() => {
            const name = window.prompt("New entity name");
            if (name?.trim()) onPatch([{ op: "add_entity", name: name.trim() }]);
          }}
        >
          + Entity
        </button>
      )}
    </Panel>
  );
}

/** Exported so the reordering gestures can be tested without the whole page. */
export function FieldsPane({
  entity,
  selected,
  editable,
  onSelect,
  onPatch,
}: {
  entity: EntityView;
  selected: string | null;
  editable: boolean;
  onSelect: (name: string) => void;
  onPatch: (operations: SchemaOperation[]) => void;
}): ReactNode {
  /** The field being dragged, and where it would land. */
  const [dragging, setDragging] = useState<string | null>(null);
  const [dropAt, setDropAt] = useState<number | null>(null);

  const order = entity.field_order;

  /**
   * Field order is the order of the columns in the output, so this is a real
   * edit rather than a view preference - it goes through `move_field` like any
   * other change and lands in the document in place.
   */
  const move = (name: string, index: number): void => {
    const clamped = Math.max(0, Math.min(order.length - 1, index));
    if (order[clamped] === name) return;
    onPatch([{ op: "move_field", entity: entity.name, name, index: clamped }]);
  };

  const nudge = (name: string, by: number): void => move(name, order.indexOf(name) + by);

  return (
    <Panel
      title={`${entity.name} · ${entity.field_order.length} fields`}
      actions={
        editable ? (
          <>
            <input
              type="number"
              min={0}
              defaultValue={entity.count}
              key={`${entity.name}-count-${entity.count}`}
              style={{ width: 110 }}
              aria-label={`${entity.name} record count`}
              onBlur={(event) => {
                const value = Number(event.target.value);
                if (Number.isFinite(value) && value !== entity.count) {
                  onPatch([
                    { op: "set_entity", entity: entity.name, key: "count", value },
                  ]);
                }
              }}
            />
            <button
              type="button"
              className="button-sm"
              onClick={() => {
                const name = window.prompt("New field name");
                if (name?.trim()) {
                  onPatch([{ op: "add_field", entity: entity.name, name: name.trim() }]);
                  onSelect(name.trim());
                }
              }}
            >
              + Field
            </button>
          </>
        ) : undefined
      }
    >
      {editable && (
        <p className="hint" style={{ marginTop: 0 }}>
          Drag a row to reorder the columns, or focus one and press Alt with the
          up and down arrows.
        </p>
      )}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Field</th>
              <th>Type</th>
              <th>Generator</th>
              <th>Reads</th>
              <th>Meaning</th>
            </tr>
          </thead>
          <tbody>
            {entity.field_order.map((name, index) => {
              const field = entity.fields[name];
              if (!field) return null;
              const classes = [
                "field-row-button",
                name === selected ? "selected" : "",
                dragging === name ? "dragging" : "",
                dropAt === index && dragging !== name ? "drop-target" : "",
              ]
                .filter(Boolean)
                .join(" ");
              return (
                <tr
                  key={name}
                  className={classes}
                  onClick={() => onSelect(name)}
                  // Reordering is available from the keyboard as well, because
                  // a pointer gesture is not an interface on its own.
                  tabIndex={0}
                  onKeyDown={(event) => {
                    if (!editable || !event.altKey) return;
                    if (event.key === "ArrowUp" || event.key === "ArrowDown") {
                      event.preventDefault();
                      onSelect(name);
                      nudge(name, event.key === "ArrowUp" ? -1 : 1);
                    }
                  }}
                  draggable={editable}
                  onDragStart={(event) => {
                    setDragging(name);
                    event.dataTransfer.effectAllowed = "move";
                    // Firefox will not start a drag without payload.
                    event.dataTransfer.setData("text/plain", name);
                  }}
                  onDragOver={(event) => {
                    if (!editable || dragging === null) return;
                    event.preventDefault();
                    setDropAt(index);
                  }}
                  onDrop={(event) => {
                    event.preventDefault();
                    if (dragging !== null) move(dragging, index);
                    setDragging(null);
                    setDropAt(null);
                  }}
                  onDragEnd={() => {
                    setDragging(null);
                    setDropAt(null);
                  }}
                >
                  <td>
                    {name}
                    {field.primary_key && (
                      <span className="faint" title="primary key">
                        {" "}
                        ⚷
                      </span>
                    )}
                    {field.inferred && (
                      <span
                        className="inferred-mark"
                        title="Generator inferred by the recommendation engine"
                      >
                        {" "}
                        ~
                      </span>
                    )}
                  </td>
                  <td className="faint">{field.type}</td>
                  <td>
                    <GeneratorBadge
                      generator={field.generator}
                      title={field.generator_describe}
                    />
                  </td>
                  <td className="faint mono" style={{ fontSize: "0.72rem" }}>
                    {field.dependencies.join(", ") || "—"}
                  </td>
                  <td className="faint">
                    <span className="truncate">{field.semantic ?? "—"}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="faint" style={{ fontSize: "0.76rem", marginBottom: 0 }}>
        Fields are listed as the schema declares them. <span className="inferred-mark">~</span>{" "}
        marks a generator the recommendation engine chose.
      </p>
    </Panel>
  );
}

/**
 * The source view.
 *
 * The form editor patches the document and preserves everything around the
 * change. This replaces the document wholesale, so it is the right tool for
 * structural edits and the wrong one for a quick tweak - which is what the
 * notice says.
 */
function SourcePane({
  projectId,
  source,
  editable,
}: {
  projectId: number;
  source: string;
  editable: boolean;
}): ReactNode {
  const write = useWriteSchema(projectId);
  const [draft, setDraft] = useState(source);
  useEffect(() => setDraft(source), [source]);

  const dirty = draft !== source;

  return (
    <Panel
      title="Schema source"
      actions={
        editable ? (
          <>
            <button
              type="button"
              className="button-sm"
              disabled={!dirty}
              onClick={() => setDraft(source)}
            >
              Revert
            </button>
            <button
              type="button"
              className="button-sm button-primary"
              disabled={!dirty || write.isPending}
              onClick={() => write.mutate(draft)}
            >
              {write.isPending ? "Saving…" : "Save"}
            </button>
          </>
        ) : undefined
      }
    >
      <ErrorNotice error={write.error} />
      {write.isSuccess && !dirty && <Notice>Saved — revision {write.data?.revision_id}.</Notice>}
      <textarea
        value={draft}
        readOnly={!editable}
        spellCheck={false}
        rows={28}
        aria-label="Schema source"
        onChange={(event) => setDraft(event.target.value)}
      />
      <p className="faint" style={{ fontSize: "0.76rem", marginBottom: 0 }}>
        Saving here replaces the whole document. Field edits made in the panels
        patch it in place instead, keeping comments and ordering intact. Either
        way, a schema that will not compile is refused and the file is left
        untouched.
      </p>
    </Panel>
  );
}
