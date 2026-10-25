/** Starter programs for `script` steps, one per language. */
export type ScriptLanguage = "python" | "node" | "shell" | "bash";

export const SCRIPT_LANGUAGES: { value: ScriptLanguage; label: string }[] = [
  { value: "python", label: "Python" },
  { value: "node", label: "Node.js" },
  { value: "shell", label: "Shell (sh)" },
  { value: "bash", label: "Bash" },
];

export const SCRIPT_TEMPLATES: Record<ScriptLanguage, string> = {
  python: `import shoal_step

# Inputs declared on the step, already rendered: shoal_step.inputs["name"]
items = shoal_step.inputs.get("items", [])

shoal_step.set_output("count", len(items))
`,
  node: `const { inputs, setOutput } = require("shoal_step");

// Inputs declared on the step, already rendered: inputs.name
const items = inputs.items ?? [];

setOutput("count", items.length);
`,
  shell: `# Scalar inputs arrive as $SHOAL_INPUT_<NAME>; all inputs as JSON in $SHOAL_INPUTS.
# Write a JSON object of outputs to $SHOAL_OUTPUTS.
count=$(wc -l < "$SHOAL_INPUTS")
printf '{"count": %s}' "$count" > "$SHOAL_OUTPUTS"
`,
  bash: `set -euo pipefail
# Scalar inputs arrive as $SHOAL_INPUT_<NAME>; all inputs as JSON in $SHOAL_INPUTS.
# Write a JSON object of outputs to $SHOAL_OUTPUTS.
printf '{"count": %s}' 0 > "$SHOAL_OUTPUTS"
`,
};

export function editorLanguage(lang: unknown): "python" | "javascript" | "text" {
  return lang === "python" ? "python" : lang === "node" ? "javascript" : "text";
}
