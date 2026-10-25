/** Route components: one per kind, binding the generic pages to their extensions. */
import ResourceEditorPage from "./ResourceEditorPage";
import ResourceListPage from "./ResourceListPage";
import { agentExtension, gatewayExtension, modelPolicyExtension, sandboxExtension, skillExtension } from "./extensions";
import { evalExtension } from "./evals/EvalExtension";
import { blueprintExtension } from "./blueprints/blueprintExtension";

const EXT = {
  agents: agentExtension,
  skills: skillExtension,
  "sandbox-profiles": sandboxExtension,
  gateways: gatewayExtension,
  "model-policies": modelPolicyExtension,
  evals: evalExtension,
  blueprints: blueprintExtension,
  "skill-sources": {},
};

export type CatalogPlural = keyof typeof EXT;

export function List({ plural }: { plural: CatalogPlural }) {
  return <ResourceListPage key={plural} plural={plural} />;
}

export function Editor({ plural }: { plural: CatalogPlural }) {
  return <ResourceEditorPage key={plural} plural={plural} ext={EXT[plural]} />;
}

export default { List, Editor };
