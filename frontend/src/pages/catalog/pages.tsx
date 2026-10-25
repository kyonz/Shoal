/** Route components: one per kind, binding the generic pages to their extensions. */
import ResourceEditorPage from "./ResourceEditorPage";
import ResourceListPage from "./ResourceListPage";
import { agentExtension, cloudIdentityExtension, gatewayExtension, modelPolicyExtension, sandboxExtension, skillExtension } from "./extensions";
import { evalExtension } from "./evals/EvalExtension";
import { evalPlatformExtension } from "./evals/Platforms";
import { blueprintExtension } from "./blueprints/blueprintExtension";
import { customIntegrationExtension } from "./customIntegrations";
import { targetListExtension } from "./targetLists";

const EXT = {
  agents: agentExtension,
  skills: skillExtension,
  "sandbox-profiles": sandboxExtension,
  "cloud-identities": cloudIdentityExtension,
  gateways: gatewayExtension,
  "model-policies": modelPolicyExtension,
  evals: evalExtension,
  "target-lists": targetListExtension,
  blueprints: blueprintExtension,
  "skill-sources": {},
  "eval-platforms": evalPlatformExtension,
  "custom-integrations": customIntegrationExtension,
};

export type CatalogPlural = keyof typeof EXT;

export function List({ plural }: { plural: CatalogPlural }) {
  return <ResourceListPage key={plural} plural={plural} />;
}

export function Editor({ plural }: { plural: CatalogPlural }) {
  return <ResourceEditorPage key={plural} plural={plural} ext={EXT[plural]} />;
}

export default { List, Editor };
