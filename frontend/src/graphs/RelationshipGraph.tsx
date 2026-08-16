/**
 * The relationship graph (design document sections 40, 53).
 *
 *     Company
 *        ├── Department
 *        │       └── Employee
 *        │              ├── Device
 *        │              ├── LoginEvent
 *        │              └── Email
 *
 * Section 40 names React Flow as particularly suitable for this, and section 53
 * wants an interactive canvas. Edges come from three places, in decreasing
 * order of how much they tell you:
 *
 *   - **References.** An actual foreign key, labelled with the field that
 *     carries it. `order.customer -> customer_id` says what the relationship
 *     *is*, not merely that one exists.
 *   - **Derived dependencies.** Why the entities generate in this order, for
 *     the pairs no reference explains - a template reading another entity's
 *     field, say.
 *   - **Declared relationships.** What the schema stated outright.
 *
 * A reference edge supersedes the dependency edge for the same pair, because
 * the two describe one connection and the reference describes it better.
 */

import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { type ReactNode, useMemo } from "react";

import type { EntityView, ReferenceEdge, Relationship } from "../api/types";

interface EntityNodeData extends Record<string, unknown> {
  name: string;
  count: number;
  fields: number;
  aiFields: number;
  selected: boolean;
}

function EntityNode({ data }: NodeProps): ReactNode {
  const entity = data as EntityNodeData;
  return (
    <div
      className={`graph-node ${entity.aiFields > 0 ? "has-ai" : ""}`}
      style={entity.selected ? { borderColor: "var(--cyan)" } : undefined}
    >
      <Handle type="target" position={Position.Top} style={{ opacity: 0.4 }} />
      <div className="name">{entity.name}</div>
      <div className="meta">
        {entity.count.toLocaleString()} × {entity.fields} field
        {entity.fields === 1 ? "" : "s"}
      </div>
      {entity.aiFields > 0 && (
        <div className="meta" style={{ color: "var(--violet)" }}>
          {entity.aiFields} model-written
        </div>
      )}
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0.4 }} />
    </div>
  );
}

const NODE_TYPES = { entity: EntityNode };

export function RelationshipGraph({
  entities,
  order,
  relationships,
  references = [],
  selected,
  onSelect,
}: {
  entities: Record<string, EntityView>;
  order: string[];
  relationships: Relationship[];
  references?: ReferenceEdge[];
  selected?: string | null;
  onSelect?: (entity: string) => void;
}): ReactNode {
  const { nodes, edges } = useMemo(
    () => build(entities, order, relationships, references, selected ?? null),
    [entities, order, relationships, references, selected],
  );

  if (order.length === 0) {
    return <p className="faint">This project defines no entities.</p>;
  }

  return (
    <div className="graph-wrap">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        fitView
        proOptions={{ hideAttribution: true }}
        nodesDraggable
        nodesConnectable={false}
        onNodeClick={(_event, node) => onSelect?.(node.id)}
      >
        <Background color="#2a2a3a" gap={18} />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          maskColor="rgba(12,12,17,0.75)"
          nodeColor={(node) =>
            (node.data as EntityNodeData).aiFields > 0 ? "#b388ff" : "#3a3a4d"
          }
          style={{ background: "#14141c" }}
        />
      </ReactFlow>
    </div>
  );
}

/**
 * Lay the graph out in dependency layers.
 *
 * The compiler already produced a topological order, so an entity's depth is
 * one more than the deepest thing it depends on. That puts sources at the top
 * and the things built from them underneath - which is the shape section 53
 * draws, and it comes out of information the backend already computed rather
 * than a physics simulation.
 */
function build(
  entities: Record<string, EntityView>,
  order: string[],
  relationships: Relationship[],
  references: ReferenceEdge[],
  selected: string | null,
): { nodes: Node[]; edges: Edge[] } {
  const depth = new Map<string, number>();
  for (const name of order) {
    const entity = entities[name];
    const parents = entity?.depends_on ?? [];
    const deepest = parents.reduce((best, parent) => Math.max(best, (depth.get(parent) ?? -1)), -1);
    depth.set(name, deepest + 1);
  }

  const byDepth = new Map<number, string[]>();
  for (const name of order) {
    const level = depth.get(name) ?? 0;
    byDepth.set(level, [...(byDepth.get(level) ?? []), name]);
  }

  const nodes: Node[] = order.map((name) => {
    const entity = entities[name];
    const level = depth.get(name) ?? 0;
    const row = byDepth.get(level) ?? [];
    const index = row.indexOf(name);
    const fields = Object.values(entity?.fields ?? {});

    return {
      id: name,
      type: "entity",
      position: {
        x: index * 220 - ((row.length - 1) * 220) / 2,
        y: level * 130,
      },
      data: {
        name,
        count: entity?.count ?? 0,
        fields: fields.length,
        aiFields: fields.filter((field) => field.requires_provider !== null).length,
        selected: name === selected,
      } satisfies EntityNodeData,
    };
  });

  const edges: Edge[] = [];
  const seen = new Set<string>();
  // Pairs a reference already explains, so the generic dependency edge for the
  // same pair is not drawn on top of it.
  const referenced = new Set<string>();

  // Foreign keys: the relationships that actually exist in the data.
  for (const reference of references) {
    const pair = `${reference.to_entity}->${reference.from_entity}`;
    const id = `ref:${pair}:${reference.from_field}`;
    if (seen.has(id)) continue;
    seen.add(id);
    referenced.add(pair);
    edges.push({
      id,
      source: reference.to_entity,
      target: reference.from_entity,
      animated: true,
      style: { stroke: "var(--cyan)", strokeWidth: 2 },
      label: reference.unique
        ? `${reference.from_field} (1:1)`
        : reference.distribution === "skewed"
          ? `${reference.from_field} (skewed)`
          : reference.from_field,
      labelStyle: { fill: "#c8c8dc", fontSize: 10 },
      labelBgStyle: { fill: "#14141c" },
    });
  }

  // Derived dependencies: why the entities generate in this order, for the
  // pairs no foreign key accounts for.
  for (const name of order) {
    for (const parent of entities[name]?.depends_on ?? []) {
      const id = `dep:${parent}->${name}`;
      if (seen.has(id) || referenced.has(`${parent}->${name}`)) continue;
      seen.add(id);
      edges.push({
        id,
        source: parent,
        target: name,
        animated: true,
        style: { stroke: "var(--cyan)", strokeWidth: 1.5 },
        label: "depends on",
        labelStyle: { fill: "#a4a4b8", fontSize: 10 },
        labelBgStyle: { fill: "#14141c" },
      });
    }
  }

  // Declared relationships, where they say something the dependency did not.
  for (const relationship of relationships) {
    const id = `rel:${relationship.from}->${relationship.to}`;
    const pair = `${relationship.from}->${relationship.to}`;
    if (seen.has(id) || seen.has(`dep:${pair}`) || referenced.has(pair)) continue;
    seen.add(id);
    edges.push({
      id,
      source: relationship.from,
      target: relationship.to,
      style: { stroke: "var(--violet)", strokeWidth: 1.5, strokeDasharray: "4 3" },
      label: relationship.cardinality.replace(/_/g, " "),
      labelStyle: { fill: "#a4a4b8", fontSize: 10 },
      labelBgStyle: { fill: "#14141c" },
    });
  }

  return { nodes, edges };
}
