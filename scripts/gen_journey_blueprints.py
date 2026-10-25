"""Build the bundled journey blueprints (engine/shoal/blueprints/library) from the plain journey examples. Run from the repo root after changing them:

    python3 scripts/gen_journey_blueprints.py
"""
import yaml, pathlib, copy

class Lit(str): pass
def lit(d, v): return d.represent_scalar("tag:yaml.org,2002:str", v, style="|" if "\n" in v else None)
yaml.SafeDumper.add_representer(Lit, lit)
def literalize(x):
    if isinstance(x, dict): return {k: literalize(v) for k, v in x.items()}
    if isinstance(x, list): return [literalize(v) for v in x]
    if isinstance(x, str) and "\n" in x: return Lit(x)
    return x

def load(*paths):
    out = []
    for p in paths:
        out += [d for d in yaml.safe_load_all(pathlib.Path(p).read_text()) if d]
    return out

P = "{{ prefix }}"

def strip(doc):
    doc = copy.deepcopy(doc); doc.pop("apiVersion", None); return doc

def jira():
    res = []
    for d in map(strip, load("examples/jira-bugfix/agent.yaml", "examples/jira-bugfix/workflow.yaml", "examples/jira-bugfix/trigger.yaml")):
        d["metadata"]["name"] = P + d["metadata"]["name"]
        s = d["spec"]
        if d["kind"] == "Workflow":
            s["connections"] = {"jira": "{{ params.jira }}", "github": "{{ params.github }}"}
            s["defaults"]["agent"] = P + s["defaults"]["agent"]
        if d["kind"] == "Trigger":
            s["workflow"] = P + s["workflow"]; s["connection"] = "{{ params.jira }}"
            s["inputs"]["repo"] = "{{ event.issue.fields.customfield_10050 }}"
            s["filter"] = "{{ event.status_change and event.status_change.to == 'Ready for AI' and event.issue.fields.customfield_10050 }}"
        res.append(d)
    return res

def github():
    res = []
    for d in map(strip, load("examples/github-uplift/agent.yaml", "examples/github-uplift/workflow.yaml", "examples/github-uplift/campaign.yaml")):
        d["metadata"]["name"] = P + d["metadata"]["name"]
        s = d["spec"]
        if d["kind"] == "Agent":
            s["skills"] = [P + x for x in s.get("skills", [])]
        if d["kind"] == "Workflow":
            s["connections"] = {"github": "{{ params.github }}"}
            s["defaults"]["agent"] = P + s["defaults"]["agent"]
            s["defaults"]["sandbox"] = "{{ params.sandbox }}"
        if d["kind"] == "Campaign":
            s["workflow"] = P + s["workflow"]
            s["targets"]["connection"] = "{{ params.github }}"
            s["targets"]["filter"]["org"] = "{{ params.org }}"
            s["inputs"]["goal"] = "{{ params.goal }}"
            s["policy"]["budget_usd"] = "{{ params.budget_usd }}"
        if d["kind"] == "Trigger":
            s["campaign"] = P + s["campaign"]
        res.append(d)
    return res

blueprints = [
    {"apiVersion": "shoal.dev/v1", "kind": "Blueprint", "metadata": {"name": "jira-bugfix", "description": "Acceptance journey J2"},
     "spec": {"title": "Jira bugfix with writeback", "summary": "When a ticket moves to Ready for AI, an agent fixes the bug, opens a PR and reports back on the ticket.",
              "version": "1.0.0", "status": "featured", "author": "Shoal", "tags": ["jira", "github", "bugfix"], "categories": ["engineering"],
              "readme": "Adopt with your Jira and GitHub connections. Then point a Jira webhook (Issue updated) at the Jira connection's webhook URL.\nTickets moved into the **Ready for AI** column are fixed on a branch, a PR is opened and the ticket gets a comment with the result.\nTickets need the repository (owner/name) in custom field 10050; edit the trigger after adopting to use another field.\n",
              "params": {"jira": {"type": "connection", "integration": "jira", "description": "Jira site to watch and write back to."},
                         "github": {"type": "connection", "integration": "github", "description": "GitHub connection used to push the fix and open the PR."}},
              "resources": jira()}},
    {"apiVersion": "shoal.dev/v1", "kind": "Blueprint", "metadata": {"name": "github-uplift", "description": "Acceptance journey J1"},
     "spec": {"title": "GitHub org uplift", "summary": "Raise every repository in an org towards a goal (100% coverage by default), wait up to a week for review, merge where allowed.",
              "version": "1.0.0", "status": "featured", "author": "Shoal", "tags": ["github", "campaign", "coverage"], "categories": ["engineering"],
              "readme": "Adopt, then launch the campaign from Campaigns (or enable its weekly schedule trigger).\nEach repository gets a PR; the run waits up to 7 days for review and merges when the initiator has merge rights.\n",
              "params": {"github": {"type": "connection", "integration": "github", "description": "GitHub connection with access to the org."},
                         "org": {"type": "string", "description": "GitHub organisation to uplift."},
                         "goal": {"type": "string", "default": "Raise test coverage to 100%", "description": "What each PR should achieve."},
                         "budget_usd": {"type": "number", "default": 500, "description": "Campaign spend limit in USD."},
                         "sandbox": {"type": "string", "default": "default", "description": "Sandbox profile the agent runs in."}},
              "resources": github()}},
]
for bp in blueprints:
    text = yaml.safe_dump(literalize(bp), sort_keys=False, width=120, allow_unicode=True)
    pathlib.Path(f"engine/shoal/blueprints/library/{bp['metadata']['name']}.yaml").write_text(text)
