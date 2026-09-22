// Verbatim from Corsair's `db/kysely/sqlite-date-plugin.ts` (Apache-2.0),
// reproduced here only because importing it would require installing their
// whole monorepo for one serialisation adapter.
//
// It is not the code under test. The code under test is
// `core/auth/key-manager.ts`, which this benchmark imports from the clone
// unmodified.
import type {
  KyselyPlugin,
  PluginTransformQueryArgs,
  PluginTransformResultArgs,
  PrimitiveValueListNode,
  QueryResult,
  RootOperationNode,
  UnknownRow,
  ValueNode,
} from "kysely";
import { OperationNodeTransformer } from "kysely";

function serializeValue(v: unknown): unknown {
  if (v instanceof Date) return v.toISOString();
  if (v !== null && typeof v === "object" && !Buffer.isBuffer(v))
    return JSON.stringify(v);
  return v;
}

class SqliteSerializingTransformer extends OperationNodeTransformer {
  protected override transformValue(node: ValueNode): ValueNode {
    const serialized = serializeValue(node.value);
    return serialized === node.value ? node : { ...node, value: serialized };
  }

  protected override transformPrimitiveValueList(
    node: PrimitiveValueListNode,
  ): PrimitiveValueListNode {
    const serialized = node.values.map(serializeValue);
    const changed = serialized.some((v, i) => v !== node.values[i]);
    return changed ? { ...node, values: serialized } : node;
  }
}

const transformer = new SqliteSerializingTransformer();

export class SqliteDatePlugin implements KyselyPlugin {
  transformQuery(args: PluginTransformQueryArgs): RootOperationNode {
    return transformer.transformNode(args.node);
  }
  async transformResult(
    args: PluginTransformResultArgs,
  ): Promise<QueryResult<UnknownRow>> {
    return args.result;
  }
}
